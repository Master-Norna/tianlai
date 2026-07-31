"""Restore the large, redistributable instrument resources omitted from Git.

The restore manifest is data, not executable installer code.  This module
implements the common safety contract once:

* stable upstream URL and fixed archive digest, or a fixed commit plus a
  complete extracted-tree digest;
* resumable ``.part`` downloads with a hard size ceiling;
* traversal-safe extraction into a same-volume staging directory;
* complete tree verification before an atomic directory rename;
* no merge into, or replacement of, a mismatched existing resource tree.

The default manifest covers the 38 catalogue entries whose resources were not
restorable by the older per-library installers.  It deliberately does not
mirror or repack third-party samples.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import errno
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from typing import Any, Iterable, Sequence
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile
import zlib


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "tianlai.resource_restore_manifest"
TREE_HASH_ALGORITHM = "tianlai-tree-sha256-v1"
_DOWNLOAD_CHUNK = 1024 * 1024
_AUTOMATIC_RESTART_MAX_BYTES = 512 * 1024 * 1024
_ALLOWED_ARCHIVE_FORMATS = frozenset({"zip", "tar.xz", "7z"})
_ALLOWED_LICENSE_STATUSES = frozenset({"approved", "grandfathered"})
_MANAGEMENT_PREFIX = ".tianlai-"
_WINDOWS_INVALID_MEMBER_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_MEMBER_STEMS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_CONTENT_RANGE_PATTERN = re.compile(
    r"bytes[ \t]+(?P<start>[0-9]+)-(?P<end>[0-9]+)/(?P<total>[0-9]+)",
    flags=re.IGNORECASE,
)
_EXPECTED_ARCHIVE_ERRORS = (
    EOFError,
    UnicodeError,
    NotImplementedError,
    lzma.LZMAError,
    tarfile.TarError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
)


def _windows_extended_path(path: str | Path) -> str:
    r"""Return an OS path that is not subject to the legacy Windows MAX_PATH.

    Callers must keep using their ordinary, unprefixed :class:`Path` objects
    for containment checks, archive-member validation, diagnostics and tree
    records.  The extended-length spelling is introduced only at the actual
    filesystem boundary.  This avoids making ``\\?\`` part of any portable
    resource identity while also avoiding a machine-wide LongPathsEnabled
    prerequisite.
    """

    value = os.fspath(path)
    if os.name != "nt":
        return value
    if value.startswith("\\\\?\\"):
        _without_windows_extended_prefix(value)
        return value
    if value.startswith("\\\\.\\"):
        raise ResourceRestoreError(
            f"unsupported Windows device namespace path: {value}"
        )
    value = os.path.abspath(value).replace("/", "\\")
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _without_windows_extended_prefix(path: str) -> str:
    folded = path.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        tail = path[8:]
        parts = tail.split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ResourceRestoreError(
                f"invalid extended UNC path: {path}"
            )
        return "\\\\" + tail
    if path.startswith("\\\\?\\"):
        tail = path[4:]
        if (
            len(tail) >= 3
            and tail[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            and tail[1:3] == ":\\"
        ):
            return tail
        raise ResourceRestoreError(
            f"unsupported Windows extended namespace path: {path}"
        )
    if path.startswith("\\\\.\\"):
        raise ResourceRestoreError(
            f"unsupported Windows device namespace path: {path}"
        )
    return path


def _resolve_path(path: str | Path) -> Path:
    """Resolve a logical path while using long-path-safe Windows I/O."""

    candidate = Path(path).expanduser()
    try:
        if os.name != "nt":
            return candidate.resolve()
        resolved = os.path.realpath(_windows_extended_path(candidate))
        return Path(_without_windows_extended_prefix(resolved))
    except OSError as exc:
        raise ResourceRestoreError(f"cannot resolve filesystem path {candidate}: {exc}") from exc


def _path_lstat(path: str | Path) -> os.stat_result:
    return os.stat(_windows_extended_path(path), follow_symlinks=False)


def _path_exists(path: str | Path) -> bool:
    try:
        _path_lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise ResourceRestoreError(f"cannot inspect filesystem path {path}: {exc}") from exc
    return True


def _path_is_plain_file(path: str | Path) -> bool:
    try:
        metadata = _path_lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise ResourceRestoreError(f"cannot inspect filesystem path {path}: {exc}") from exc
    return stat.S_ISREG(metadata.st_mode) and not _is_reparse_point(metadata)


def _path_is_plain_directory(path: str | Path) -> bool:
    try:
        metadata = _path_lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise ResourceRestoreError(f"cannot inspect filesystem path {path}: {exc}") from exc
    return stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata)


def _mkdir_path(
    path: str | Path,
    *,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    system_path = _windows_extended_path(path)
    if parents:
        os.makedirs(system_path, exist_ok=exist_ok)
    else:
        try:
            os.mkdir(system_path)
        except FileExistsError:
            if not exist_ok:
                raise


def _replace_path(source: str | Path, destination: str | Path) -> None:
    os.replace(
        _windows_extended_path(source),
        _windows_extended_path(destination),
    )


def _link_path(source: str | Path, destination: str | Path) -> None:
    """Create a same-volume hard link without replacing ``destination``."""

    os.link(
        _windows_extended_path(source),
        _windows_extended_path(destination),
        follow_symlinks=False,
    )


def _rename_path_noreplace(source: str | Path, destination: str | Path) -> None:
    """Atomically rename a file or directory only when the target is absent.

    Plain POSIX ``rename`` can silently replace an empty destination directory,
    so it is not a no-clobber primitive.  The supported platforms use their
    native exclusive rename operation.  Unknown POSIX implementations fail
    closed instead of weakening the install contract.
    """

    if os.name == "nt":
        # Unlike os.replace(), Windows os.rename() refuses every existing
        # destination.  The extended spelling also avoids MAX_PATH.
        os.rename(
            _windows_extended_path(source),
            _windows_extended_path(destination),
        )
        return

    source_bytes = os.fsencode(os.fspath(source))
    destination_bytes = os.fsencode(os.fspath(destination))
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ResourceRestoreError(
                "this Linux runtime has no renameat2; refusing a potentially "
                "clobbering resource-tree publish"
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,  # AT_FDCWD
            source_bytes,
            -100,
            destination_bytes,
            1,  # RENAME_NOREPLACE
        )
    elif sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise ResourceRestoreError(
                "this macOS runtime has no renamex_np; refusing a potentially "
                "clobbering resource-tree publish"
            )
        renamex_np.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renamex_np.restype = ctypes.c_int
        result = renamex_np(
            source_bytes,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    else:
        raise ResourceRestoreError(
            f"{sys.platform} has no configured atomic no-replace rename; "
            "refusing a potentially clobbering resource-tree publish"
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(
            error,
            os.strerror(error),
            os.fspath(destination),
        )


def _unlink_path(path: str | Path) -> None:
    os.unlink(_windows_extended_path(path))


class ResourceRestoreError(RuntimeError):
    """A resource cannot be restored without weakening the safety contract."""


class RestoreManifestError(ResourceRestoreError):
    """The tracked resource restore manifest is invalid."""


class _ArchiveVerificationError(ResourceRestoreError):
    """A controlled download does not match its frozen archive contract."""


class _FamilyRestoreLock:
    """One process-owned lock spanning a complete family restoration.

    The small lock file is persistent, but its byte-range/advisory lock belongs
    to the open descriptor.  Closing the descriptor or terminating the process
    releases ownership without stale-lock recovery.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    def __enter__(self) -> _FamilyRestoreLock:
        try:
            _mkdir_path(self.path.parent, parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(
                _windows_extended_path(self.path),
                flags,
                0o600,
            )
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot open resource-family lock {self.path}: {exc}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ResourceRestoreError(
                    f"resource-family lock is not a plain file: {self.path}"
                )
            path_metadata = _path_lstat(self.path)
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or _is_reparse_point(path_metadata)
                or (
                    metadata.st_ino
                    and path_metadata.st_ino
                    and (
                        metadata.st_dev != path_metadata.st_dev
                        or metadata.st_ino != path_metadata.st_ino
                    )
                )
            ):
                raise ResourceRestoreError(
                    f"resource-family lock changed while opening: {self.path}"
                )
            if metadata.st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            if os.name == "nt":
                import msvcrt

                while True:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {
                            errno.EACCES,
                            errno.EAGAIN,
                            errno.EDEADLK,
                        }:
                            raise
                        time.sleep(0.05)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                        break
                    except InterruptedError:
                        continue
        except OSError as exc:
            os.close(descriptor)
            raise ResourceRestoreError(
                f"cannot acquire resource-family lock {self.path}: {exc}"
            ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, *_error: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            # Both msvcrt byte locks and POSIX flock ownership are released by
            # close, including when the operating system tears down a process.
            os.close(descriptor)


def _family_restore_lock_path(resource_root: Path, family_id: object) -> Path:
    safe_id = _normalised_relative(
        family_id,
        label="resource family lock id",
        allow_slash=False,
    )
    return resource_root / ".tianlai" / "locks" / f"{safe_id}.lock"


@dataclass(frozen=True, slots=True)
class TreeDigest:
    files: int
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DownloadedArchive:
    path: Path
    cache_path: Path
    pending_promotion: bool


def default_manifest_path(home: str | Path | None = None) -> Path:
    """Return the source-release manifest path without guessing site-packages."""

    if home is not None:
        return _resolve_path(home) / "resource_restore_manifest.json"
    package_parent = _resolve_path(__file__).parent.parent
    return package_parent / "resource_restore_manifest.json"


def _normalised_relative(
    raw: object,
    *,
    label: str,
    allow_slash: bool = True,
) -> str:
    value = str(raw).replace("\\", "/").strip()
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
        or (not allow_slash and len(path.parts) != 1)
    ):
        raise RestoreManifestError(f"{label} must be a safe relative path: {raw!r}")
    return path.as_posix()


def _normalised_sha256(raw: object, *, label: str, optional: bool = False) -> str | None:
    if raw is None and optional:
        return None
    value = str(raw).strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise RestoreManifestError(f"{label} must be a lowercase 64-digit SHA-256")
    return value


def _positive_int(raw: object, *, label: str, allow_zero: bool = False) -> int:
    if isinstance(raw, bool):
        raise RestoreManifestError(f"{label} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RestoreManifestError(f"{label} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RestoreManifestError(f"{label} must be >= {minimum}")
    return value


def _validate_tree(raw: object, *, label: str) -> dict[str, int | str]:
    if not isinstance(raw, dict):
        raise RestoreManifestError(f"{label} must be an object")
    return {
        "files": _positive_int(raw.get("files"), label=f"{label}.files"),
        "bytes": _positive_int(
            raw.get("bytes"),
            label=f"{label}.bytes",
            allow_zero=True,
        ),
        "sha256": _normalised_sha256(
            raw.get("sha256"),
            label=f"{label}.sha256",
        ),
    }


def validate_restore_manifest(
    document: object,
    *,
    allow_file_urls: bool = False,
) -> dict[str, Any]:
    """Validate and normalise one machine-readable restore manifest."""

    if not isinstance(document, dict):
        raise RestoreManifestError("restore manifest root must be an object")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RestoreManifestError(
            "unsupported restore manifest schema_version: "
            f"{document.get('schema_version')!r}"
        )
    if document.get("kind") != MANIFEST_KIND:
        raise RestoreManifestError(f"restore manifest kind must be {MANIFEST_KIND!r}")
    tree_hash = document.get("tree_hash")
    if not isinstance(tree_hash, dict) or tree_hash.get("algorithm") != TREE_HASH_ALGORITHM:
        raise RestoreManifestError(
            f"tree_hash.algorithm must be {TREE_HASH_ALGORITHM!r}"
        )
    families = document.get("families")
    if not isinstance(families, list) or not families:
        raise RestoreManifestError("restore manifest families must be a non-empty array")

    family_ids: set[str] = set()
    instrument_ids: set[str] = set()
    normalised_families: list[dict[str, Any]] = []
    estimated_download_total = 0
    installed_total = 0
    for index, raw_family in enumerate(families):
        label = f"families[{index}]"
        if not isinstance(raw_family, dict):
            raise RestoreManifestError(f"{label} must be an object")
        family = dict(raw_family)
        family_id = _normalised_relative(
            family.get("id"),
            label=f"{label}.id",
            allow_slash=False,
        )
        if family_id in family_ids:
            raise RestoreManifestError(f"duplicate resource family id: {family_id}")
        family_ids.add(family_id)
        family["id"] = family_id
        family["group"] = _normalised_relative(
            family.get("group"),
            label=f"{label}.group",
            allow_slash=False,
        )
        if not str(family.get("display_name", "")).strip():
            raise RestoreManifestError(f"{label}.display_name must not be empty")

        raw_instruments = family.get("instrument_ids")
        if not isinstance(raw_instruments, list) or not raw_instruments:
            raise RestoreManifestError(f"{label}.instrument_ids must not be empty")
        normalised_instruments: list[str] = []
        for raw_instrument in raw_instruments:
            instrument_id = _normalised_relative(
                raw_instrument,
                label=f"{label}.instrument_ids",
            )
            if instrument_id in instrument_ids:
                raise RestoreManifestError(
                    f"instrument is mapped to more than one resource family: "
                    f"{instrument_id}"
                )
            instrument_ids.add(instrument_id)
            normalised_instruments.append(instrument_id)
        family["instrument_ids"] = normalised_instruments

        licence = family.get("license")
        if not isinstance(licence, dict):
            raise RestoreManifestError(f"{label}.license must be an object")
        if licence.get("status") not in _ALLOWED_LICENSE_STATUSES:
            raise RestoreManifestError(
                f"{label}.license.status must be approved or grandfathered"
            )
        if not str(licence.get("expression", "")).strip():
            raise RestoreManifestError(f"{label}.license.expression must not be empty")
        raw_evidence = licence.get("evidence_files")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise RestoreManifestError(
                f"{label}.license.evidence_files must not be empty"
            )
        licence = dict(licence)
        licence["evidence_files"] = [
            _normalised_relative(item, label=f"{label}.license.evidence_files")
            for item in raw_evidence
        ]
        family["license"] = licence

        source = family.get("source")
        if not isinstance(source, dict):
            raise RestoreManifestError(f"{label}.source must be an object")
        source = dict(source)
        commit = source.get("commit")
        if commit is not None:
            commit_text = str(commit).strip().lower()
            if len(commit_text) != 40 or any(
                character not in "0123456789abcdef" for character in commit_text
            ):
                raise RestoreManifestError(f"{label}.source.commit must be a full Git SHA")
            source["commit"] = commit_text
        family["source"] = source

        archive = family.get("archive")
        if not isinstance(archive, dict):
            raise RestoreManifestError(f"{label}.archive must be an object")
        archive = dict(archive)
        parsed = urlparse(str(archive.get("url", "")))
        allowed_scheme = parsed.scheme == "https" or (
            allow_file_urls and parsed.scheme == "file"
        )
        if not allowed_scheme or not parsed.path:
            raise RestoreManifestError(
                f"{label}.archive.url must use HTTPS"
                + (" or a test-only file URL" if allow_file_urls else "")
            )
        archive["filename"] = _normalised_relative(
            archive.get("filename"),
            label=f"{label}.archive.filename",
            allow_slash=False,
        )
        archive_format = str(archive.get("format", "")).strip().lower()
        if archive_format not in _ALLOWED_ARCHIVE_FORMATS:
            raise RestoreManifestError(
                f"{label}.archive.format must be one of "
                f"{sorted(_ALLOWED_ARCHIVE_FORMATS)}"
            )
        archive["format"] = archive_format
        exact_bytes = archive.get("bytes")
        if exact_bytes is not None:
            archive["bytes"] = _positive_int(
                exact_bytes,
                label=f"{label}.archive.bytes",
            )
        archive["estimated_bytes"] = _positive_int(
            archive.get("estimated_bytes"),
            label=f"{label}.archive.estimated_bytes",
        )
        archive["max_bytes"] = _positive_int(
            archive.get("max_bytes"),
            label=f"{label}.archive.max_bytes",
        )
        if archive["max_bytes"] < (
            archive["bytes"]
            if archive.get("bytes") is not None
            else archive["estimated_bytes"]
        ):
            raise RestoreManifestError(
                f"{label}.archive.max_bytes is below its expected size"
            )
        archive["sha256"] = _normalised_sha256(
            archive.get("sha256"),
            label=f"{label}.archive.sha256",
            optional=True,
        )
        if (
            archive_format != "7z"
            and archive["sha256"] is None
            and source.get("commit") is None
        ):
            raise RestoreManifestError(
                f"{label} needs an archive SHA-256 or a fixed full commit"
            )
        host = (parsed.hostname or "").casefold()
        decoded_path = unquote(parsed.path).casefold()
        github_generated_archive = (
            host in {"api.github.com", "codeload.github.com", "github.com"}
            and "/releases/download/" not in decoded_path
            and (
                "/archive/" in decoded_path
                or "/zipball/" in decoded_path
                or host == "codeload.github.com"
            )
        )
        if github_generated_archive:
            commit_text = source.get("commit")
            if not commit_text or commit_text not in decoded_path:
                raise RestoreManifestError(
                    f"{label}: GitHub generated archives must use the fixed "
                    "full source commit in the download URL"
                )
            if archive.get("bytes") is not None or archive["sha256"] is not None:
                raise RestoreManifestError(
                    f"{label}: GitHub generated archive container bytes are "
                    "not stable; verify the fixed commit and complete "
                    "extracted tree instead"
                )
        if archive_format == "7z" and (
            archive["sha256"] is None or archive.get("bytes") is None
        ):
            raise RestoreManifestError(
                f"{label}: external 7z extraction requires a fixed archive "
                "SHA-256 and exact byte length"
            )
        family["archive"] = archive

        install = family.get("install")
        if not isinstance(install, dict):
            raise RestoreManifestError(f"{label}.install must be an object")
        install = dict(install)
        install["target"] = _normalised_relative(
            install.get("target"),
            label=f"{label}.install.target",
        )
        if install.get("strip_single_root") is not True:
            raise RestoreManifestError(
                f"{label}.install.strip_single_root must currently be true"
            )
        install["tree"] = _validate_tree(
            install.get("tree"),
            label=f"{label}.install.tree",
        )
        derived_items = install.get("derived", [])
        if not isinstance(derived_items, list):
            raise RestoreManifestError(f"{label}.install.derived must be an array")
        normalised_derived: list[dict[str, Any]] = []
        for derived_index, raw_derived in enumerate(derived_items):
            derived_label = f"{label}.install.derived[{derived_index}]"
            if not isinstance(raw_derived, dict):
                raise RestoreManifestError(f"{derived_label} must be an object")
            derived = dict(raw_derived)
            derived["target"] = _normalised_relative(
                derived.get("target"),
                label=f"{derived_label}.target",
            )
            derived["recipe"] = _normalised_relative(
                derived.get("recipe"),
                label=f"{derived_label}.recipe",
            )
            derived["tree"] = _validate_tree(
                derived.get("tree"),
                label=f"{derived_label}.tree",
            )
            normalised_derived.append(derived)
            installed_total += int(derived["tree"]["bytes"])
        install["derived"] = normalised_derived
        family["install"] = install
        estimated_download_total += int(archive["estimated_bytes"])
        installed_total += int(install["tree"]["bytes"])
        normalised_families.append(family)

    totals = document.get("totals")
    if not isinstance(totals, dict):
        raise RestoreManifestError("restore manifest totals must be an object")
    expected_totals = {
        "family_count": len(normalised_families),
        "instrument_count": len(instrument_ids),
        "estimated_download_bytes": estimated_download_total,
        "installed_bytes_including_derived": installed_total,
    }
    for key, expected in expected_totals.items():
        actual = _positive_int(totals.get(key), label=f"totals.{key}")
        if actual != expected:
            raise RestoreManifestError(
                f"totals.{key} is {actual}, but family data sums to {expected}"
            )
    _positive_int(
        totals.get("recommended_free_bytes"),
        label="totals.recommended_free_bytes",
    )
    normalised = dict(document)
    normalised["families"] = normalised_families
    normalised["totals"] = dict(totals)
    return normalised


def load_restore_manifest(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    allow_file_urls: bool = False,
) -> dict[str, Any]:
    source = (
        _resolve_path(path)
        if path is not None
        else default_manifest_path(home)
    )
    try:
        with open(_windows_extended_path(source), encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreManifestError(f"cannot load restore manifest {source}: {exc}") from exc
    return validate_restore_manifest(document, allow_file_urls=allow_file_urls)


def family_for_instrument(
    manifest: dict[str, Any],
    instrument_id: str,
) -> dict[str, Any] | None:
    wanted = str(instrument_id).replace("\\", "/")
    return next(
        (
            family
            for family in manifest["families"]
            if wanted in family["instrument_ids"]
        ),
        None,
    )


def select_families(
    manifest: dict[str, Any],
    *,
    family_ids: Sequence[str] = (),
    groups: Sequence[str] = (),
) -> list[dict[str, Any]]:
    requested_families = {
        item.strip()
        for value in family_ids
        for item in str(value).split(",")
        if item.strip()
    }
    requested_groups = {
        item.strip()
        for value in groups
        for item in str(value).split(",")
        if item.strip()
    }
    known_families = {family["id"] for family in manifest["families"]}
    known_groups = {family["group"] for family in manifest["families"]}
    unknown_families = requested_families - known_families
    unknown_groups = requested_groups - known_groups
    if unknown_families:
        raise ResourceRestoreError(
            f"unknown resource family: {', '.join(sorted(unknown_families))}"
        )
    if unknown_groups:
        raise ResourceRestoreError(
            f"unknown resource group: {', '.join(sorted(unknown_groups))}"
        )
    if not requested_families and not requested_groups:
        return list(manifest["families"])
    return [
        family
        for family in manifest["families"]
        if family["id"] in requested_families or family["group"] in requested_groups
    ]


def _is_ignored_tree_part(part: str) -> bool:
    return part == ".git" or part.startswith(_MANAGEMENT_PREFIX)


def _plain_tree_entries(
    root: Path,
) -> Iterable[tuple[Path, PurePosixPath, os.stat_result]]:
    """Yield a tree without ever following links or Windows reparse points."""

    try:
        root_metadata = _path_lstat(root)
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot inspect resource tree root {root}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or _is_reparse_point(root_metadata)
    ):
        raise ResourceRestoreError(
            f"resource tree is not a plain directory: {root}"
        )

    def walk(
        directory: Path,
        relative_directory: PurePosixPath | None,
    ) -> Iterable[tuple[Path, PurePosixPath, os.stat_result]]:
        try:
            with os.scandir(_windows_extended_path(directory)) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot enumerate resource tree directory {directory}: {exc}"
            ) from exc
        entries.sort(key=lambda item: item.name)
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if relative_directory is None
                else relative_directory / entry.name
            )
            logical = directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ResourceRestoreError(
                    f"cannot inspect resource tree entry {logical}: {exc}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                raise ResourceRestoreError(
                    f"resource trees may not contain links: {logical}"
                )
            yield logical, relative, metadata
            if stat.S_ISDIR(metadata.st_mode):
                yield from walk(logical, relative)

    yield from walk(root, None)


def tree_digest(root: str | Path) -> TreeDigest:
    """Hash every regular file by content and portable relative path."""

    source = _resolve_path(root)
    records: list[tuple[str, Path]] = []
    for path, relative, metadata in _plain_tree_entries(source):
        if any(_is_ignored_tree_part(part) for part in relative.parts):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ResourceRestoreError(f"unsupported resource tree entry: {path}")
        records.append((relative.as_posix(), path))
    records.sort(key=lambda item: item[0])
    combined = hashlib.sha256()
    byte_count = 0
    for relative, path in records:
        file_hash = hashlib.sha256()
        try:
            with open(_windows_extended_path(path), "rb") as stream:
                while chunk := stream.read(_DOWNLOAD_CHUNK):
                    file_hash.update(chunk)
                    byte_count += len(chunk)
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot read resource tree file {path}: {exc}"
            ) from exc
        combined.update(file_hash.hexdigest().encode("ascii"))
        combined.update(b"  ")
        combined.update(relative.encode("utf-8"))
        combined.update(b"\n")
    return TreeDigest(
        files=len(records),
        bytes=byte_count,
        sha256=combined.hexdigest(),
    )


def _expected_tree(raw: dict[str, Any]) -> TreeDigest:
    return TreeDigest(
        files=int(raw["files"]),
        bytes=int(raw["bytes"]),
        sha256=str(raw["sha256"]).lower(),
    )


def verify_tree(root: str | Path, expected: dict[str, Any]) -> TreeDigest:
    actual = tree_digest(root)
    wanted = _expected_tree(expected)
    if actual != wanted:
        raise ResourceRestoreError(
            f"resource tree mismatch for {_resolve_path(root)}: "
            f"expected {wanted.to_dict()}, got {actual.to_dict()}"
        )
    return actual


def _safe_join(root: Path, relative: str, *, label: str) -> Path:
    normalised = _normalised_relative(relative, label=label)
    candidate = _resolve_path(root / Path(*PurePosixPath(normalised).parts))
    try:
        candidate.relative_to(_resolve_path(root))
    except ValueError as exc:
        raise ResourceRestoreError(f"{label} escapes {root}: {relative}") from exc
    return candidate


def _archive_member_path(raw: str) -> PurePosixPath:
    value = unicodedata.normalize("NFC", str(raw)).replace("\\", "/")
    while value.endswith("/"):
        value = value[:-1]
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\x00" in value
    ):
        raise ResourceRestoreError(f"unsafe archive member path: {raw!r}")
    for part in path.parts:
        if (
            part.endswith((" ", "."))
            or any(character in _WINDOWS_INVALID_MEMBER_CHARACTERS for character in part)
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
        ):
            raise ResourceRestoreError(
                f"archive member is not a portable Windows path: {raw!r}"
            )
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_MEMBER_STEMS:
            raise ResourceRestoreError(
                f"archive member uses a reserved Windows device name: {raw!r}"
            )
    return path


def _register_archive_member(
    seen: dict[str, tuple[str, bool]],
    path: PurePosixPath,
    *,
    is_directory: bool,
) -> None:
    portable = path.as_posix()
    for depth in range(1, len(path.parts)):
        implicit = PurePosixPath(*path.parts[:depth]).as_posix()
        implicit_key = unicodedata.normalize("NFC", implicit).casefold()
        previous = seen.get(implicit_key)
        if previous is None:
            seen[implicit_key] = (implicit, True)
            continue
        previous_path, previous_is_directory = previous
        if previous_path != implicit:
            raise ResourceRestoreError(
                f"archive contains a duplicate/case-colliding path: "
                f"{previous_path!r} and {implicit!r}"
            )
        if not previous_is_directory:
            raise ResourceRestoreError(
                f"archive member uses a file as its parent directory: "
                f"{previous_path!r} and {portable!r}"
            )
    collision_key = unicodedata.normalize("NFC", portable).casefold()
    previous = seen.get(collision_key)
    if previous is not None:
        previous_path, previous_is_directory = previous
        if previous_path == portable and previous_is_directory and is_directory:
            return
        raise ResourceRestoreError(
            f"archive contains a duplicate/case-colliding path: "
            f"{previous_path!r} and {portable!r}"
        )
    seen[collision_key] = (portable, is_directory)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _assert_plain_extracted_tree(
    root: Path,
    *,
    max_total_bytes: int | None = None,
) -> None:
    total_bytes = 0
    for path, _relative, metadata in _plain_tree_entries(_resolve_path(root)):
        if not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise ResourceRestoreError(
                f"archive extracted an unsupported entry: {path}"
            )
        if stat.S_ISREG(metadata.st_mode):
            total_bytes += int(metadata.st_size)
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                raise ResourceRestoreError(
                    "extracted archive size exceeds the frozen "
                    "resource-tree limit"
                )


def _extract_zip(archive: Path, destination: Path, *, max_unpacked_bytes: int) -> None:
    seen: dict[str, tuple[str, bool]] = {}
    declared_bytes = 0
    with zipfile.ZipFile(_windows_extended_path(archive)) as bundle:
        for item in bundle.infolist():
            member = _archive_member_path(item.filename)
            mode = (item.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ResourceRestoreError(
                    f"ZIP archive contains a symbolic link: {item.filename}"
                )
            is_directory = item.is_dir()
            _register_archive_member(seen, member, is_directory=is_directory)
            if item.flag_bits & 0x1:
                raise ResourceRestoreError(
                    f"encrypted ZIP members are not supported: {item.filename}"
                )
            if not is_directory:
                declared_bytes += int(item.file_size)
                if declared_bytes > max_unpacked_bytes:
                    raise ResourceRestoreError(
                        "ZIP declared size exceeds the frozen resource-tree limit"
                    )
        for item in bundle.infolist():
            member = _archive_member_path(item.filename)
            output = destination.joinpath(*member.parts)
            if item.is_dir():
                _mkdir_path(output, parents=True, exist_ok=True)
                continue
            _mkdir_path(output.parent, parents=True, exist_ok=True)
            with bundle.open(item) as source, open(
                _windows_extended_path(output),
                "xb",
            ) as target:
                shutil.copyfileobj(source, target, length=_DOWNLOAD_CHUNK)


def _extract_tar_xz(
    archive: Path,
    destination: Path,
    *,
    max_unpacked_bytes: int,
) -> None:
    seen: dict[str, tuple[str, bool]] = {}
    declared_bytes = 0
    with tarfile.open(_windows_extended_path(archive), mode="r:xz") as bundle:
        members = bundle.getmembers()
        for item in members:
            member = _archive_member_path(item.name)
            if not (item.isdir() or item.isfile()):
                raise ResourceRestoreError(
                    f"TAR archive contains a link or special entry: {item.name}"
                )
            _register_archive_member(seen, member, is_directory=item.isdir())
            if item.isfile():
                declared_bytes += int(item.size)
                if declared_bytes > max_unpacked_bytes:
                    raise ResourceRestoreError(
                        "TAR declared size exceeds the frozen resource-tree limit"
                    )
        for item in members:
            member = _archive_member_path(item.name)
            output = destination.joinpath(*member.parts)
            if item.isdir():
                _mkdir_path(output, parents=True, exist_ok=True)
                continue
            _mkdir_path(output.parent, parents=True, exist_ok=True)
            source = bundle.extractfile(item)
            if source is None:
                raise ResourceRestoreError(f"cannot read TAR member: {item.name}")
            with source, open(_windows_extended_path(output), "xb") as target:
                shutil.copyfileobj(source, target, length=_DOWNLOAD_CHUNK)


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _find_bsdtar_executable() -> str:
    """Return a verified libarchive bsdtar executable.

    Linux packages the libarchive frontend as ``bsdtar`` while supported
    Windows releases normally expose the same program as ``tar.exe``.  GNU
    tar cannot inspect or extract 7z archives, so a generic ``tar`` fallback
    is both Linux-incompatible and unsafe to assume without checking its
    implementation.
    """

    candidates = (
        ("bsdtar", "tar")
        if _is_windows_runtime()
        else ("bsdtar",)
    )
    for command in candidates:
        executable = shutil.which(command)
        if executable is None:
            continue
        try:
            version = subprocess.run(
                [executable, "--version"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            continue
        if (
            version.returncode == 0
            and b"bsdtar" in version.stdout.lower()
        ):
            return executable

    if _is_windows_runtime():
        hint = (
            "Windows 10/11 normally provides a libarchive-based tar.exe; "
            "install libarchive if it is unavailable"
        )
    else:
        hint = (
            "install libarchive-tools "
            "(Debian/Ubuntu: sudo apt install libarchive-tools)"
        )
    raise ResourceRestoreError(
        "7z extraction requires bsdtar/libarchive; "
        f"{hint}. GNU tar is not a supported 7z extractor."
    )


def _inspect_7z(
    archive: Path,
    *,
    max_unpacked_bytes: int,
) -> str:
    """Fail closed on every property bsdtar exposes before extraction.

    ``bsdtar -tf`` is the authoritative member-name stream while ``-tvf``
    supplies the entry type and declared size.  We deliberately parse only
    the stable ASCII metadata prefix, not its locale-dependent date or
    filename columns.  Unknown output is rejected rather than guessed.
    """

    tar_executable = _find_bsdtar_executable()
    names_listing = subprocess.run(
        [tar_executable, "-tf", _windows_extended_path(archive)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if names_listing.returncode:
        detail = names_listing.stderr.decode("utf-8", errors="replace").strip()
        raise ResourceRestoreError(f"cannot inspect 7z archive: {detail}")
    try:
        names = names_listing.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ResourceRestoreError("7z member list is not valid UTF-8") from exc

    metadata_listing = subprocess.run(
        [tar_executable, "-tvf", _windows_extended_path(archive)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if metadata_listing.returncode:
        detail = metadata_listing.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise ResourceRestoreError(f"cannot inspect 7z archive metadata: {detail}")
    metadata_lines = metadata_listing.stdout.splitlines()
    if len(names) != len(metadata_lines):
        raise ResourceRestoreError(
            "7z member and metadata listings disagree; refusing extraction"
        )

    seen: dict[str, tuple[str, bool]] = {}
    declared_bytes = 0
    permission_bytes = frozenset(b"rwxStTs-")
    for raw_name, metadata_line in zip(names, metadata_lines, strict=True):
        fields = metadata_line.split(None, 5)
        if len(fields) != 6:
            raise ResourceRestoreError(
                "unsupported bsdtar verbose listing; refusing 7z extraction"
            )
        mode = fields[0]
        if (
            len(mode) not in {10, 11}
            or any(character not in permission_bytes for character in mode[1:10])
            or (len(mode) == 11 and mode[10:11] not in {b"+", b"@", b"."})
        ):
            raise ResourceRestoreError(
                "unsupported bsdtar entry metadata; refusing 7z extraction"
            )
        entry_type = mode[:1]
        if entry_type not in {b"-", b"d"}:
            raise ResourceRestoreError(
                "7z archive contains a link or special entry: "
                f"{raw_name}"
            )
        try:
            declared_size = int(fields[4].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ResourceRestoreError(
                "7z archive contains an invalid declared member size"
            ) from exc
        if declared_size < 0:
            raise ResourceRestoreError(
                "7z archive contains a negative declared member size"
            )
        is_directory = entry_type == b"d"
        if is_directory and declared_size != 0:
            raise ResourceRestoreError(
                "7z directory has a non-zero declared size"
            )
        member = _archive_member_path(raw_name)
        _register_archive_member(seen, member, is_directory=is_directory)
        if not is_directory:
            declared_bytes += declared_size
            if declared_bytes > max_unpacked_bytes:
                raise ResourceRestoreError(
                    "7z declared size exceeds the frozen resource-tree limit"
                )
    return tar_executable


def _extract_7z(
    archive: Path,
    destination: Path,
    *,
    max_unpacked_bytes: int,
    tar_executable: str,
) -> None:
    extracted = subprocess.run(
        [
            tar_executable,
            "-xf",
            _windows_extended_path(archive),
            "-C",
            _windows_extended_path(destination),
            "--no-same-owner",
            "--no-same-permissions",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if extracted.returncode:
        detail = extracted.stderr.decode("utf-8", errors="replace").strip()
        raise ResourceRestoreError(f"7z extraction failed: {detail}")
    _assert_plain_extracted_tree(
        destination,
        max_total_bytes=max_unpacked_bytes,
    )


def _verify_frozen_7z_archive(
    archive: Path,
    *,
    expected_archive_sha256: str | None,
    expected_archive_bytes: int | None,
) -> None:
    if expected_archive_sha256 is None or expected_archive_bytes is None:
        raise ResourceRestoreError(
            "7z extraction requires a fixed archive SHA-256 and exact byte length"
        )
    expected_sha = str(expected_archive_sha256).strip().lower()
    if len(expected_sha) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha
    ):
        raise ResourceRestoreError(
            "7z extraction received an invalid expected archive SHA-256"
        )
    if isinstance(expected_archive_bytes, bool):
        raise ResourceRestoreError(
            "7z extraction received an invalid expected archive byte length"
        )
    try:
        expected_bytes = int(expected_archive_bytes)
    except (TypeError, ValueError) as exc:
        raise ResourceRestoreError(
            "7z extraction received an invalid expected archive byte length"
        ) from exc
    if expected_bytes <= 0:
        raise ResourceRestoreError(
            "7z extraction received an invalid expected archive byte length"
        )
    actual_bytes = int(_path_lstat(archive).st_size)
    if actual_bytes != expected_bytes:
        raise ResourceRestoreError(
            f"7z archive size mismatch: expected {expected_bytes}, "
            f"got {actual_bytes}"
        )
    actual_sha = _sha256_file(archive)
    if actual_sha != expected_sha:
        raise ResourceRestoreError(
            f"7z archive SHA-256 mismatch: expected {expected_sha}, "
            f"got {actual_sha}"
        )


def safe_extract_archive(
    archive: str | Path,
    destination: str | Path,
    *,
    archive_format: str,
    max_unpacked_bytes: int,
    expected_archive_sha256: str | None = None,
    expected_archive_bytes: int | None = None,
) -> None:
    source = _resolve_path(archive)
    target = _resolve_path(destination)
    if not _path_is_plain_file(source):
        raise ResourceRestoreError(f"archive is missing: {source}")
    try:
        tar_executable: str | None = None
        if archive_format == "7z":
            _verify_frozen_7z_archive(
                source,
                expected_archive_sha256=expected_archive_sha256,
                expected_archive_bytes=expected_archive_bytes,
            )
            tar_executable = _inspect_7z(
                source,
                max_unpacked_bytes=max_unpacked_bytes,
            )
        _mkdir_path(target, parents=True, exist_ok=False)
        if archive_format == "zip":
            _extract_zip(source, target, max_unpacked_bytes=max_unpacked_bytes)
        elif archive_format == "tar.xz":
            _extract_tar_xz(source, target, max_unpacked_bytes=max_unpacked_bytes)
        elif archive_format == "7z":
            assert tar_executable is not None
            _extract_7z(
                source,
                target,
                max_unpacked_bytes=max_unpacked_bytes,
                tar_executable=tar_executable,
            )
        else:  # validated manifests make this unreachable
            raise ResourceRestoreError(f"unsupported archive format: {archive_format}")
        _assert_plain_extracted_tree(target)
    except ResourceRestoreError:
        raise
    except _EXPECTED_ARCHIVE_ERRORS as exc:
        raise ResourceRestoreError(
            f"{archive_format} archive is corrupt or unsupported: {source}: {exc}"
        ) from exc
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot safely extract archive {source} into {target}: {exc}"
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(_windows_extended_path(path), "rb") as stream:
            while chunk := stream.read(_DOWNLOAD_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise ResourceRestoreError(f"cannot read archive {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_archive(path: Path, archive: dict[str, Any]) -> None:
    try:
        size = _path_lstat(path).st_size
    except OSError as exc:
        raise _ArchiveVerificationError(
            f"cannot inspect archive {path}: {exc}"
        ) from exc
    exact_size = archive.get("bytes")
    if exact_size is not None and size != int(exact_size):
        raise _ArchiveVerificationError(
            f"archive size mismatch for {path}: expected {exact_size}, got {size}"
        )
    if size > int(archive["max_bytes"]):
        raise _ArchiveVerificationError(
            f"archive exceeds its safety ceiling for {path}: "
            f"{size} > {archive['max_bytes']}"
        )
    expected_sha = archive.get("sha256")
    if expected_sha is not None:
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            raise _ArchiveVerificationError(
                f"archive SHA-256 mismatch for {path}: "
                f"expected {expected_sha}, got {actual_sha}"
            )


def _http_response_length(raw: object, *, header: str) -> int | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or not value.isascii() or not value.isdecimal():
        raise HTTPException(f"invalid {header} response header: {raw!r}")
    return int(value)


def _http_content_range(
    raw: object,
    *,
    expected_start: int,
    max_bytes: int,
) -> tuple[int, int, int]:
    if raw is None:
        raise HTTPException("HTTP 206 response is missing Content-Range")
    match = _CONTENT_RANGE_PATTERN.fullmatch(str(raw).strip())
    if match is None:
        raise HTTPException(f"invalid Content-Range response header: {raw!r}")
    start = int(match.group("start"))
    end = int(match.group("end"))
    total = int(match.group("total"))
    if start != expected_start:
        raise HTTPException(
            "HTTP 206 Content-Range starts at "
            f"{start}, but the local partial ends at {expected_start}"
        )
    if end < start or total <= end:
        raise HTTPException(
            f"impossible Content-Range response header: {raw!r}"
        )
    if total > max_bytes:
        raise ResourceRestoreError(
            "server Content-Range total exceeds the archive safety ceiling: "
            f"{total} > {max_bytes}"
        )
    return start, end, total


def _download_once(
    url: str,
    partial: Path,
    *,
    max_bytes: int,
    timeout: float,
    allow_file_urls: bool,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "file" and not allow_file_urls:
        raise ResourceRestoreError("file URLs are only accepted by explicit local tests")
    existing = _path_lstat(partial).st_size if _path_is_plain_file(partial) else 0
    if existing > max_bytes:
        raise ResourceRestoreError(
            f"partial download already exceeds its safety ceiling: {partial}"
        )
    headers = {
        "User-Agent": "Tianlai-Resource-Restore/1",
        "Accept-Encoding": "identity",
    }
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        if status not in {None, 200, 206}:
            raise HTTPException(f"unexpected archive response status: {status}")
        content_length = _http_response_length(
            response.headers.get("Content-Length"),
            header="Content-Length",
        )
        raw_content_range = response.headers.get("Content-Range")
        range_end: int | None = None
        range_total: int | None = None
        if status == 206:
            _range_start, range_end, range_total = _http_content_range(
                raw_content_range,
                expected_start=existing,
                max_bytes=max_bytes,
            )
            range_length = range_end - existing + 1
            if content_length is not None and content_length != range_length:
                raise HTTPException(
                    "Content-Length disagrees with Content-Range: "
                    f"{content_length} != {range_length}"
                )
            expected_response_bytes = range_length
        else:
            if raw_content_range is not None:
                raise HTTPException(
                    f"unexpected Content-Range on status {status}: "
                    f"{raw_content_range!r}"
                )
            expected_response_bytes = content_length

        append = bool(existing and status == 206)
        mode = "ab" if append else "wb"
        current = existing if append else 0
        if expected_response_bytes is not None:
            projected = current + expected_response_bytes
            if projected > max_bytes:
                raise ResourceRestoreError(
                    f"server response exceeds the archive safety ceiling: "
                    f"{projected} > {max_bytes}"
                )
        _mkdir_path(partial.parent, parents=True, exist_ok=True)
        with open(_windows_extended_path(partial), mode) as destination:
            response_bytes = 0
            while True:
                try:
                    chunk = response.read(_DOWNLOAD_CHUNK)
                except IncompleteRead as exc:
                    chunk = bytes(exc.partial)
                    if chunk:
                        if (
                            expected_response_bytes is not None
                            and response_bytes + len(chunk)
                            > expected_response_bytes
                        ):
                            raise HTTPException(
                                "response body exceeds its declared byte length"
                            ) from exc
                        response_bytes += len(chunk)
                        current += len(chunk)
                        if current > max_bytes:
                            raise ResourceRestoreError(
                                "download exceeded the archive safety ceiling"
                            ) from exc
                        destination.write(chunk)
                        destination.flush()
                        os.fsync(destination.fileno())
                    raise
                if not chunk:
                    break
                if (
                    expected_response_bytes is not None
                    and response_bytes + len(chunk) > expected_response_bytes
                ):
                    raise HTTPException(
                        "response body exceeds its declared byte length"
                    )
                response_bytes += len(chunk)
                current += len(chunk)
                if current > max_bytes:
                    raise ResourceRestoreError(
                        "download exceeded the archive safety ceiling"
                    )
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
            if (
                expected_response_bytes is not None
                and response_bytes != expected_response_bytes
            ):
                raise HTTPException(
                    "response ended before its declared byte length: "
                    f"received {response_bytes} of {expected_response_bytes} bytes"
                )
            if (
                range_total is not None
                and range_end is not None
                and current != range_total
            ):
                raise HTTPException(
                    "HTTP range response ended before the complete archive: "
                    f"received through byte {range_end} of {range_total}"
                )


def _discard_controlled_partial(partial: Path, cache_root: Path) -> bool:
    """Delete only the exact ``<manifest filename>.part`` inside its cache."""

    cache = _resolve_path(cache_root)
    candidate = Path(os.path.abspath(os.fspath(partial)))
    try:
        relative = candidate.relative_to(cache)
    except ValueError as exc:
        raise ResourceRestoreError(
            f"refusing to remove a partial outside the controlled cache: {candidate}"
        ) from exc
    if (
        len(relative.parts) != 1
        or not candidate.name.endswith(".part")
        or candidate.name == ".part"
    ):
        raise ResourceRestoreError(
            f"refusing to remove an unrecognised partial download: {candidate}"
        )
    try:
        metadata = _path_lstat(candidate)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        raise ResourceRestoreError(
            f"partial download path is unexpectedly a directory: {candidate}"
        )
    _unlink_path(candidate)
    return True


def _partial_restart_hint(family: dict[str, Any], partial: Path) -> str:
    return (
        f"受控临时下载保留在 {partial}。核对网络/上游后可显式重跑 "
        f"`install --family {family['id']} --restart-download`；该开关只删除"
        "这一个 .part，不会删除已发布缓存或现有音源目标。"
    )


def download_archive(
    family: dict[str, Any],
    cache_root: str | Path,
    *,
    allow_file_urls: bool = False,
    restart_download: bool = False,
    retries: int = 3,
    timeout: float = 60.0,
) -> DownloadedArchive:
    archive = family["archive"]
    cache = _resolve_path(cache_root)
    _mkdir_path(cache, parents=True, exist_ok=True)
    cache_path = _safe_join(
        cache,
        archive["filename"],
        label="archive filename",
    )
    partial = cache_path.with_name(cache_path.name + ".part")
    if restart_download:
        _discard_controlled_partial(partial, cache)
    if _path_is_plain_file(cache_path):
        _verify_archive(cache_path, archive)
        return DownloadedArchive(cache_path, cache_path, False)
    if _path_exists(cache_path):
        raise ResourceRestoreError(f"archive cache target is not a file: {cache_path}")
    if _path_exists(partial) and not _path_is_plain_file(partial):
        raise ResourceRestoreError(
            f"partial download target is not a plain file: {partial}"
        )

    partial_existed_at_entry = _path_is_plain_file(partial)
    automatic_restart_allowed = (
        partial_existed_at_entry
        and int(archive["max_bytes"]) <= _AUTOMATIC_RESTART_MAX_BYTES
        and not restart_download
    )
    restarted = False
    if (
        partial_existed_at_entry
        and _path_lstat(partial).st_size > int(archive["max_bytes"])
    ):
        if automatic_restart_allowed:
            _discard_controlled_partial(partial, cache)
            restarted = True
        else:
            raise ResourceRestoreError(
                f"partial download exceeds its safety ceiling: {partial}. "
                f"{_partial_restart_hint(family, partial)}"
            )
    while True:
        exact_size = archive.get("bytes")
        if (
            _path_is_plain_file(partial)
            and exact_size is not None
            and _path_lstat(partial).st_size == exact_size
        ):
            try:
                _verify_archive(partial, archive)
                return DownloadedArchive(partial, cache_path, True)
            except _ArchiveVerificationError as exc:
                if automatic_restart_allowed and not restarted:
                    _discard_controlled_partial(partial, cache)
                    restarted = True
                    continue
                raise ResourceRestoreError(
                    f"{exc} {_partial_restart_hint(family, partial)}"
                ) from exc

        last_error: Exception | None = None
        restart_after_attempts = False
        for attempt in range(1, retries + 1):
            try:
                _download_once(
                    archive["url"],
                    partial,
                    max_bytes=int(archive["max_bytes"]),
                    timeout=timeout,
                    allow_file_urls=allow_file_urls,
                )
                _verify_archive(partial, archive)
                return DownloadedArchive(partial, cache_path, True)
            except _ArchiveVerificationError as exc:
                if automatic_restart_allowed and not restarted:
                    _discard_controlled_partial(partial, cache)
                    restarted = True
                    restart_after_attempts = True
                    break
                raise ResourceRestoreError(
                    f"{exc} {_partial_restart_hint(family, partial)}"
                ) from exc
            except HTTPError as exc:
                if exc.code == 416 and _path_is_plain_file(partial):
                    try:
                        _verify_archive(partial, archive)
                        # Codeload families intentionally have no stable archive
                        # SHA/size.  A 416 may mean the partial is complete; the
                        # authoritative extracted-tree check runs next.
                        return DownloadedArchive(partial, cache_path, True)
                    except _ArchiveVerificationError as verification_error:
                        if automatic_restart_allowed and not restarted:
                            _discard_controlled_partial(partial, cache)
                            restarted = True
                            restart_after_attempts = True
                            break
                        raise ResourceRestoreError(
                            f"{verification_error} "
                            f"{_partial_restart_hint(family, partial)}"
                        ) from verification_error
                last_error = exc
                if attempt < retries:
                    time.sleep(float(attempt))
            except (URLError, HTTPException, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(float(attempt))
        if restart_after_attempts:
            continue
        if last_error is not None:
            raise ResourceRestoreError(
                f"download failed after {retries} attempts for {family['id']}: "
                f"{last_error}. {_partial_restart_hint(family, partial)}"
            ) from last_error
        raise ResourceRestoreError(f"download failed for {family['id']}")


def _safe_remove_staging(path: Path, parent: Path) -> None:
    resolved = _resolve_path(path)
    resolved_parent = _resolve_path(parent)
    try:
        relative = resolved.relative_to(resolved_parent)
    except ValueError as exc:
        raise ResourceRestoreError(
            f"refusing to clean staging outside {resolved_parent}: {resolved}"
        ) from exc
    if len(relative.parts) != 1 or ".tianlai-" not in relative.name:
        raise ResourceRestoreError(f"refusing to clean an unrecognised staging path: {resolved}")
    if _path_is_plain_directory(resolved):
        try:
            shutil.rmtree(_windows_extended_path(resolved))
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot clean controlled staging directory {resolved}: {exc}"
            ) from exc
    elif _path_exists(resolved):
        try:
            _unlink_path(resolved)
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot clean controlled staging path {resolved}: {exc}"
            ) from exc


def _cleanup_staging(path: Path, parent: Path) -> None:
    """Clean a controlled stage without replacing an active primary failure."""

    active_error = sys.exception()
    try:
        if _path_exists(path):
            _safe_remove_staging(path, parent)
    except ResourceRestoreError as cleanup_error:
        if active_error is None:
            raise
        active_error.add_note(
            f"staging cleanup also failed: {cleanup_error}"
        )


def _single_extracted_root(unpack: Path) -> Path:
    try:
        with os.scandir(_windows_extended_path(unpack)) as iterator:
            children = [unpack / entry.name for entry in iterator]
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot inspect extracted archive root {unpack}: {exc}"
        ) from exc
    if len(children) != 1 or not _path_is_plain_directory(children[0]):
        raise ResourceRestoreError(
            "archive must contain exactly one top-level directory before stripping"
        )
    return children[0]


def _check_evidence(root: Path, family: dict[str, Any]) -> None:
    for relative in family["license"]["evidence_files"]:
        evidence = _safe_join(root, relative, label="licence evidence")
        if not _path_is_plain_file(evidence):
            raise ResourceRestoreError(
                f"licence/provenance evidence is missing after extraction: {evidence}"
            )


def _promote_verified_archive_cache(
    download: DownloadedArchive,
    family: dict[str, Any],
    *,
    cache_root: Path,
) -> None:
    """Publish a verified ``.part`` without clobbering a racing cache file."""

    if not download.pending_promotion:
        return
    try:
        _link_path(download.path, download.cache_path)
    except OSError as exc:
        if not _path_exists(download.cache_path):
            raise ResourceRestoreError(
                "cannot atomically create verified archive cache "
                f"{download.cache_path} without replacement: {exc}"
            ) from exc
        if not _path_is_plain_file(download.cache_path):
            raise ResourceRestoreError(
                "refusing to overwrite a competing non-file archive cache "
                f"target: {download.cache_path}"
            ) from exc
        try:
            _verify_archive(download.cache_path, family["archive"])
        except ResourceRestoreError as verification_error:
            raise ResourceRestoreError(
                "refusing to overwrite a competing archive cache that does "
                f"not match the frozen archive contract: {download.cache_path}"
            ) from verification_error

        # GitHub fixed-commit archives intentionally have no stable container
        # hash.  A size ceiling alone cannot prove that a racing file is the
        # same already-verified download, so require exact local bytes or stop.
        if family["archive"].get("sha256") is None and (
            _path_lstat(download.path).st_size
            != _path_lstat(download.cache_path).st_size
            or _sha256_file(download.path)
            != _sha256_file(download.cache_path)
        ):
            raise ResourceRestoreError(
                "refusing to overwrite or trust a byte-distinct competing "
                f"fixed-commit archive cache: {download.cache_path}"
            ) from exc
    _discard_controlled_partial(download.path, cache_root)


def _atomic_install_tree(
    staged: Path,
    target: Path,
    expected: dict[str, Any],
    *,
    staged_already_verified: bool = False,
) -> str:
    if not staged_already_verified:
        verify_tree(staged, expected)
    if _path_exists(target):
        if not _path_is_plain_directory(target):
            raise ResourceRestoreError(
                f"refusing to replace a non-directory resource target: {target}"
            )
        verify_tree(target, expected)
        return "already_verified"
    try:
        _mkdir_path(target.parent, parents=True, exist_ok=True)
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot create resource target parent {target.parent}: {exc}"
        ) from exc
    try:
        _rename_path_noreplace(staged, target)
    except OSError as exc:
        if _path_exists(target):
            if not _path_is_plain_directory(target):
                raise ResourceRestoreError(
                    "refusing to replace a competing non-directory resource "
                    f"target: {target}"
                ) from exc
            verify_tree(target, expected)
            return "already_verified"
        raise ResourceRestoreError(
            "cannot atomically publish resource tree without replacement "
            f"{staged} as {target}: {exc}"
        ) from exc
    return "installed"


def _restore_source_tree(
    family: dict[str, Any],
    *,
    resource_root: Path,
    cache_root: Path,
    allow_file_urls: bool,
    restart_download: bool,
) -> tuple[str, TreeDigest]:
    install = family["install"]
    target = _safe_join(resource_root, install["target"], label="resource target")
    if _path_exists(target):
        verified = verify_tree(target, install["tree"])
        return "already_verified", verified

    if family["archive"]["format"] == "7z":
        # Fail before a potentially multi-gigabyte download when the host
        # cannot safely inspect or extract the frozen 7z container.
        _find_bsdtar_executable()

    try:
        _mkdir_path(target.parent, parents=True, exist_ok=True)
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot create resource staging parent {target.parent}: {exc}"
        ) from exc
    automatic_tree_retry = (
        family["archive"].get("sha256") is None
        and int(family["archive"]["max_bytes"])
        <= _AUTOMATIC_RESTART_MAX_BYTES
    )
    for tree_attempt in range(2):
        retry_after_tree_mismatch = False
        download = download_archive(
            family,
            cache_root,
            allow_file_urls=allow_file_urls,
            restart_download=restart_download and tree_attempt == 0,
        )
        identifier = uuid.uuid4().hex
        unpack = target.parent / f".{target.name}.tianlai-unpacking-{identifier}"
        try:
            safety_limit = int(install["tree"]["bytes"])
            safe_extract_archive(
                download.path,
                unpack,
                archive_format=family["archive"]["format"],
                max_unpacked_bytes=safety_limit,
                expected_archive_sha256=family["archive"].get("sha256"),
                expected_archive_bytes=family["archive"].get("bytes"),
            )
            staged = _single_extracted_root(unpack)
            _check_evidence(staged, family)
            verified = verify_tree(staged, install["tree"])
            _promote_verified_archive_cache(
                download,
                family,
                cache_root=cache_root,
            )
            status = _atomic_install_tree(
                staged,
                target,
                install["tree"],
                staged_already_verified=True,
            )
            return status, verified
        except ResourceRestoreError as exc:
            can_restart = (
                tree_attempt == 0
                and automatic_tree_retry
                and download.pending_promotion
                and _path_exists(download.path)
            )
            if can_restart:
                _discard_controlled_partial(download.path, cache_root)
                retry_after_tree_mismatch = True
            else:
                hint = (
                    _partial_restart_hint(family, download.path)
                    if download.pending_promotion
                    else (
                        "已发布缓存不会被自动删除；它曾通过完整树门禁。"
                        "请先核对清单版本与本地文件。"
                    )
                )
                raise ResourceRestoreError(f"{exc} {hint}") from exc
        finally:
            _cleanup_staging(unpack, target.parent)
        if retry_after_tree_mismatch:
            continue
    raise AssertionError("tree restore retry loop exhausted")  # pragma: no cover


def _restore_derived_tree(
    derived: dict[str, Any],
    *,
    home: Path,
    resource_root: Path,
) -> tuple[str, TreeDigest]:
    target = _safe_join(resource_root, derived["target"], label="derived target")
    if _path_exists(target):
        verified = verify_tree(target, derived["tree"])
        return "already_verified", verified
    canonical_resources = _resolve_path(home / "音源")
    if _resolve_path(resource_root) != canonical_resources:
        raise ResourceRestoreError(
            "the current frozen derived recipes are workspace-relative; "
            "TIANLAI_RESOURCE_DIR cannot be used for VCSL recorder or FreePats "
            "bagpipe derivation yet"
        )
    recipe = _safe_join(home, derived["recipe"], label="derived recipe")
    if not _path_is_plain_file(recipe):
        raise ResourceRestoreError(f"derived resource recipe is missing: {recipe}")
    from .derived_samples import build_derived_resources

    try:
        _mkdir_path(target.parent, parents=True, exist_ok=True)
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot create derived-resource staging parent {target.parent}: {exc}"
        ) from exc
    staged = target.parent / f".{target.name}.tianlai-building-{uuid.uuid4().hex}"
    try:
        try:
            build_derived_resources(recipe, output_root=staged)
        except ResourceRestoreError:
            raise
        except Exception as exc:
            raise ResourceRestoreError(
                f"cannot build derived resource tree {derived['target']}: {exc}"
            ) from exc
        verified = verify_tree(staged, derived["tree"])
        status = _atomic_install_tree(
            staged,
            target,
            derived["tree"],
            staged_already_verified=True,
        )
        return status, verified
    finally:
        _cleanup_staging(staged, target.parent)


def _write_receipt(
    family: dict[str, Any],
    *,
    resource_root: Path,
    source_digest: TreeDigest,
    derived_digests: list[tuple[str, TreeDigest]],
) -> Path:
    receipts = resource_root / ".tianlai" / "receipts"
    try:
        _mkdir_path(receipts, parents=True, exist_ok=True)
    except OSError as exc:
        raise ResourceRestoreError(
            f"cannot create resource receipt directory {receipts}: {exc}"
        ) from exc
    destination = receipts / f"{family['id']}.json"
    payload = {
        "schema_version": 1,
        "family_id": family["id"],
        "source": family["source"],
        "archive": {
            "url": family["archive"]["url"],
            "sha256": family["archive"].get("sha256"),
            "verification": family["archive"]["verification"],
        },
        "target": family["install"]["target"],
        "tree": source_digest.to_dict(),
        "derived": [
            {"target": target, "tree": digest.to_dict()}
            for target, digest in derived_digests
        ],
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = receipts / (
        f".tianlai-receipt-{family['id']}-{uuid.uuid4().hex}.part"
    )
    try:
        try:
            with open(_windows_extended_path(temporary), "xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_path(temporary, destination)
        except OSError as exc:
            raise ResourceRestoreError(
                f"cannot atomically write resource receipt {destination}: {exc}"
            ) from exc
    finally:
        active_error = sys.exception()
        try:
            if _path_exists(temporary):
                _unlink_path(temporary)
        except (OSError, ResourceRestoreError) as cleanup_error:
            message = (
                f"cannot clean temporary resource receipt {temporary}: "
                f"{cleanup_error}"
            )
            if active_error is None:
                raise ResourceRestoreError(message) from cleanup_error
            active_error.add_note(message)
    return destination


def restore_family(
    family: dict[str, Any],
    *,
    home: str | Path,
    resource_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    allow_file_urls: bool = False,
    restart_download: bool = False,
) -> dict[str, Any]:
    """Restore and fully verify one family, returning a machine-readable result."""

    workspace = _resolve_path(home)
    resources = (
        _resolve_path(resource_root)
        if resource_root is not None
        else workspace / "音源"
    )
    cache = (
        _resolve_path(cache_root)
        if cache_root is not None
        else resources / "下载缓存"
    )
    lock_path = _family_restore_lock_path(resources, family["id"])
    with _FamilyRestoreLock(lock_path):
        source_status, source_digest = _restore_source_tree(
            family,
            resource_root=resources,
            cache_root=cache,
            allow_file_urls=allow_file_urls,
            restart_download=restart_download,
        )
        derived_results: list[dict[str, Any]] = []
        derived_digests: list[tuple[str, TreeDigest]] = []
        for derived in family["install"]["derived"]:
            status, digest = _restore_derived_tree(
                derived,
                home=workspace,
                resource_root=resources,
            )
            derived_results.append(
                {
                    "target": derived["target"],
                    "status": status,
                    "tree": digest.to_dict(),
                }
            )
            derived_digests.append((derived["target"], digest))
        receipt = _write_receipt(
            family,
            resource_root=resources,
            source_digest=source_digest,
            derived_digests=derived_digests,
        )
    return {
        "family_id": family["id"],
        "status": source_status,
        "target": family["install"]["target"],
        "tree": source_digest.to_dict(),
        "derived": derived_results,
        "receipt": str(receipt),
    }


def build_restore_plan(
    families: Sequence[dict[str, Any]],
    *,
    resource_root: str | Path,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a quick, non-mutating plan; existing trees are not hashed here."""

    resources = _resolve_path(resource_root)
    cache = (
        _resolve_path(cache_root)
        if cache_root is not None
        else resources / "下载缓存"
    )
    items: list[dict[str, Any]] = []
    download_bytes = 0
    install_bytes = 0
    instrument_count = 0
    for family in families:
        target = _safe_join(
            resources,
            family["install"]["target"],
            label="resource target",
        )
        cache_path = _safe_join(
            cache,
            family["archive"]["filename"],
            label="archive filename",
        )
        source_state = (
            "present_unverified"
            if _path_is_plain_directory(target)
            else "conflict"
            if _path_exists(target)
            else "missing"
        )
        if source_state == "missing":
            install_bytes += int(family["install"]["tree"]["bytes"])
            if not _path_is_plain_file(cache_path):
                download_bytes += int(family["archive"]["estimated_bytes"])
        derived_items: list[dict[str, str]] = []
        for derived in family["install"]["derived"]:
            derived_target = _safe_join(
                resources,
                derived["target"],
                label="derived target",
            )
            state = (
                "present_unverified"
                if _path_is_plain_directory(derived_target)
                else "conflict"
                if _path_exists(derived_target)
                else "missing"
            )
            if state == "missing":
                install_bytes += int(derived["tree"]["bytes"])
            derived_items.append({"target": derived["target"], "state": state})
        instrument_count += len(family["instrument_ids"])
        items.append(
            {
                "family_id": family["id"],
                "group": family["group"],
                "display_name": family["display_name"],
                "instrument_count": len(family["instrument_ids"]),
                "target": family["install"]["target"],
                "source_state": source_state,
                "archive_cached": _path_is_plain_file(cache_path),
                "estimated_download_bytes": (
                    int(family["archive"]["estimated_bytes"])
                    if source_state == "missing"
                    and not _path_is_plain_file(cache_path)
                    else 0
                ),
                "installed_bytes": int(family["install"]["tree"]["bytes"]),
                "derived": derived_items,
                "license": family["license"]["expression"],
                "license_status": family["license"]["status"],
            }
        )
    return {
        "family_count": len(families),
        "instrument_count": instrument_count,
        "estimated_download_bytes": download_bytes,
        "additional_installed_bytes": install_bytes,
        "items": items,
    }


def _format_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{value} B"  # pragma: no cover


def _print_plan(plan: dict[str, Any]) -> None:
    print(
        f"资源族 {plan['family_count']} 组，覆盖 {plan['instrument_count']} 件乐器；"
        f"预计还需下载 {_format_bytes(plan['estimated_download_bytes'])}，"
        f"新增占用约 {_format_bytes(plan['additional_installed_bytes'])}。"
    )
    for item in plan["items"]:
        cached = "，归档已缓存" if item["archive_cached"] else ""
        print(
            f"  - {item['family_id']} [{item['source_state']}] "
            f"{item['instrument_count']} 件 / {item['license']}{cached}"
        )
        for derived in item["derived"]:
            print(f"      派生 {derived['target']} [{derived['state']}]")


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--family",
        action="append",
        default=[],
        help="只选择资源族 ID；可重复或使用逗号分隔",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="只选择组：vcsl、freepats、karoryfer、emilyguitar、mtg",
    )


def _common_paths(arguments: argparse.Namespace) -> tuple[Path, Path, Path]:
    home = _resolve_path(arguments.home)
    resources = (
        _resolve_path(arguments.resource_dir)
        if arguments.resource_dir is not None
        else home / "音源"
    )
    cache = (
        _resolve_path(arguments.cache_dir)
        if arguments.cache_dir is not None
        else resources / "下载缓存"
    )
    return home, resources, cache


def _confirm_install(plan: dict[str, Any]) -> None:
    if not sys.stdin.isatty():
        raise ResourceRestoreError(
            "interactive confirmation is unavailable; review `plan`, then rerun "
            "`install --yes` explicitly"
        )
    print(
        "\n即将从各上游直接下载并安装大型音源。不会覆盖不一致的已有目录；"
        "MTG Solo Sax 的输出使用需保留 CC-BY-4.0 署名。"
    )
    answer = input("确认后请输入 INSTALL（其他输入取消）：").strip()
    if answer != "INSTALL":
        raise ResourceRestoreError("installation cancelled")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="恢复清单；默认使用项目根 resource_restore_manifest.json",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.cwd(),
        help="天籁源码根目录（默认当前目录）",
    )
    parser.add_argument("--resource-dir", type=Path, help="音源目录覆盖")
    parser.add_argument("--cache-dir", type=Path, help="下载缓存目录覆盖")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="列出 10 个可恢复资源族")
    _add_selection_arguments(list_parser)
    plan_parser = commands.add_parser("plan", help="只读生成恢复计划")
    _add_selection_arguments(plan_parser)
    install_parser = commands.add_parser("install", help="下载、核验并原子安装")
    _add_selection_arguments(install_parser)
    install_parser.add_argument(
        "--yes",
        action="store_true",
        help="已审阅 plan，显式跳过交互确认",
    )
    install_parser.add_argument(
        "--restart-download",
        action="store_true",
        help="只删除所选族受控缓存中的 .part 后从零下载；不删除已发布缓存或目标",
    )
    verify_parser = commands.add_parser("verify", help="完整复核已安装资源树")
    _add_selection_arguments(verify_parser)
    arguments = parser.parse_args(argv)
    try:
        home, resources, cache = _common_paths(arguments)
        manifest_path = (
            _resolve_path(arguments.manifest)
            if arguments.manifest is not None
            else default_manifest_path(home)
        )
        manifest = load_restore_manifest(manifest_path)
        families = select_families(
            manifest,
            family_ids=arguments.family,
            groups=arguments.group,
        )
        if arguments.command == "list":
            payload = {
                "families": [
                    {
                        "id": family["id"],
                        "group": family["group"],
                        "display_name": family["display_name"],
                        "instrument_count": len(family["instrument_ids"]),
                        "estimated_download_bytes": family["archive"]["estimated_bytes"],
                        "installed_bytes": family["install"]["tree"]["bytes"]
                        + sum(
                            item["tree"]["bytes"]
                            for item in family["install"]["derived"]
                        ),
                        "license": family["license"]["expression"],
                        "license_status": family["license"]["status"],
                    }
                    for family in families
                ]
            }
            if arguments.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for item in payload["families"]:
                    print(
                        f"{item['id']:<28} {item['instrument_count']:>2} 件  "
                        f"{_format_bytes(item['estimated_download_bytes']):>10}  "
                        f"{item['license']}"
                    )
            return 0

        plan = build_restore_plan(
            families,
            resource_root=resources,
            cache_root=cache,
        )
        if arguments.command == "plan":
            if arguments.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                _print_plan(plan)
            return 0
        if arguments.command == "install":
            if arguments.json and not arguments.yes:
                raise ResourceRestoreError("--json install requires explicit --yes")
            if not arguments.json:
                _print_plan(plan)
            if not arguments.yes:
                _confirm_install(plan)
            results: list[dict[str, Any]] = []
            for family in families:
                if not arguments.json:
                    print(f"\n[{family['id']}] 开始恢复与完整核验……")
                result = restore_family(
                    family,
                    home=home,
                    resource_root=resources,
                    cache_root=cache,
                    restart_download=arguments.restart_download,
                )
                results.append(result)
                if not arguments.json:
                    print(f"[{family['id']}] {result['status']}")
            payload = {"status": "ready", "results": results}
            if arguments.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "verify":
            results = []
            for family in families:
                target = _safe_join(
                    resources,
                    family["install"]["target"],
                    label="resource target",
                )
                digest = verify_tree(target, family["install"]["tree"])
                derived_results = []
                for derived in family["install"]["derived"]:
                    derived_target = _safe_join(
                        resources,
                        derived["target"],
                        label="derived target",
                    )
                    derived_digest = verify_tree(derived_target, derived["tree"])
                    derived_results.append(
                        {
                            "target": derived["target"],
                            "tree": derived_digest.to_dict(),
                        }
                    )
                results.append(
                    {
                        "family_id": family["id"],
                        "tree": digest.to_dict(),
                        "derived": derived_results,
                    }
                )
            payload = {"status": "ready", "results": results}
            if arguments.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for item in results:
                    print(f"{item['family_id']}: verified")
            return 0
        raise AssertionError(f"unhandled command: {arguments.command}")
    except ResourceRestoreError as exc:
        if getattr(arguments, "json", False):
            print(
                json.dumps(
                    {"status": "error", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"音源恢复失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MANIFEST_KIND",
    "MANIFEST_SCHEMA_VERSION",
    "ResourceRestoreError",
    "RestoreManifestError",
    "TreeDigest",
    "build_restore_plan",
    "default_manifest_path",
    "download_archive",
    "family_for_instrument",
    "load_restore_manifest",
    "main",
    "restore_family",
    "safe_extract_archive",
    "select_families",
    "tree_digest",
    "validate_restore_manifest",
    "verify_tree",
]
