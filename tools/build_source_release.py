#!/usr/bin/env python3
"""Build an auditable Tianlai source release from Git-tracked files only."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Final, Iterable, Mapping, Sequence
import unicodedata
from urllib.parse import unquote
import zipfile


MANIFEST_NAME: Final = "release-manifest.json"
MANIFEST_FORMAT: Final = "tianlai.source_release_manifest"
MANIFEST_FORMAT_VERSION: Final = 2
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_REQUIRED_ROOT_FILES: Final = frozenset(
    {"pyproject.toml", "LICENSE", "NOTICE", "OUTPUT_RIGHTS.md"}
)
_REGULAR_GIT_MODES: Final = frozenset({"100644", "100755"})
_PUBLIC_MARKDOWN_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("README.md", "README.en.md"),
    ("CONTRIBUTING.md", "CONTRIBUTING.en.md"),
    ("SECURITY.md", "SECURITY.en.md"),
    ("TRADEMARKS.md", "TRADEMARKS.en.md"),
    ("OUTPUT_RIGHTS.md", "OUTPUT_RIGHTS.en.md"),
    ("docs/README.md", "docs/README.en.md"),
    ("docs/Linux快速开始.md", "docs/Linux快速开始.en.md"),
    ("docs/macOS快速开始.md", "docs/macOS快速开始.en.md"),
    ("docs/MCP.md", "docs/MCP.en.md"),
    ("docs/创作工作流.md", "docs/创作工作流.en.md"),
    (
        "docs/VPO音源许可与安装说明.md",
        "docs/VPO音源许可与安装说明.en.md",
    ),
    ("docs/Windows安装与巡检.md", "docs/Windows安装与巡检.en.md"),
    ("docs/Windows最小启动.md", "docs/Windows最小启动.en.md"),
    (
        "docs/从乐谱到第二次渲染.md",
        "docs/从乐谱到第二次渲染.en.md",
    ),
    ("docs/渲染后自检.md", "docs/渲染后自检.en.md"),
    ("docs/当前状态.md", "docs/当前状态.en.md"),
    ("docs/音源许可政策.md", "docs/音源许可政策.en.md"),
    (
        "docs/音乐创作参考笔记/README.md",
        "docs/音乐创作参考笔记/README.en.md",
    ),
    (
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.md",
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.en.md",
    ),
    ("output/README.md", "output/README.en.md"),
    ("音源/README.md", "音源/README.en.md"),
    ("乐谱/README.md", "乐谱/README.en.md"),
)
_PUBLIC_DOCUMENT_PATHS: Final = frozenset(
    {
        # ``pyproject.toml`` names this file as the distribution README.  The
        # minimal source release must retain every local packaging input so a
        # checkout-free extraction remains valid build metadata.
        "README.pypi.md",
        "docs/音源许可例外.json",
        *(
            path
            for pair in _PUBLIC_MARKDOWN_PAIRS
            for path in pair
        ),
    }
)
_PUBLIC_DOCUMENT_KEYS: Final = frozenset(
    path.casefold() for path in _PUBLIC_DOCUMENT_PATHS
)
_PACKAGED_MARKDOWN_SOURCE_PATHS: Final[Mapping[str, str]] = {
    (
        "tianlai/_resources/constitutions/"
        "天籁音乐宪法-v0.1.md"
    ): "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.md",
    (
        "tianlai/_resources/constitutions/"
        "天籁音乐宪法-v0.1.en.md"
    ): "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.en.md",
}
_REPOSITORY_ONLY_ROOT_DOCUMENT_PATHS: Final = frozenset(
    {
        # Keep the historical ledger in Git without publishing its links to
        # repository-only engineering records in the minimal source archive.
        "CHANGELOG.md",
        "CHANGELOG.en.md",
    }
)
_REPOSITORY_ONLY_ROOT_DOCUMENT_KEYS: Final = frozenset(
    path.casefold() for path in _REPOSITORY_ONLY_ROOT_DOCUMENT_PATHS
)
_REPOSITORY_ONLY_DOCUMENT_REASON: Final = (
    "excluded repository-only documentation"
)
_PUBLIC_INSTRUMENT_DOCUMENT_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("README.md", "README.en.md"),
    ("来源.md", "来源.en.md"),
)
_PUBLIC_INSTRUMENT_DOCUMENT_NAMES: Final = frozenset(
    name
    for pair in _PUBLIC_INSTRUMENT_DOCUMENT_PAIRS
    for name in pair
)
_PUBLIC_INSTRUMENT_DOCUMENT_KEYS: Final = frozenset(
    name.casefold() for name in _PUBLIC_INSTRUMENT_DOCUMENT_NAMES
)
_MARKDOWN_LINK: Final = re.compile(
    r"!?\[[^\]\r\n]*\]\(\s*(?P<target><[^>\r\n]+>|[^\s)\r\n]+)"
)
_URI_SCHEME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
        *(f"COM{number}" for number in "¹²³"),
        *(f"LPT{number}" for number in "¹²³"),
    }
)
_ROOT_EXCLUDED_DIRECTORY_NAMES: Final = frozenset(
    {
        ".git",
        "build",
        "dist",
        "output",
        "发布包",
        "音源",
        # These are user-state lifecycles in the existing repository contract.
        "乐谱",
        "人工听审",
    }
)
_ROOT_LIFECYCLE_ANCHORS: Final = frozenset(
    {
        "output/README.md",
        "output/README.en.md",
        "音源/README.md",
        "音源/README.en.md",
        "乐谱/README.md",
        "乐谱/README.en.md",
    }
)
_EXCLUDED_DIRECTORY_NAMES: Final = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        "cache",
        "缓存",
        "tmp",
        "temp",
        "临时",
        "临时文件",
    }
)
_ROOT_EXCLUDED_DIRECTORY_KEYS: Final = frozenset(
    name.casefold() for name in _ROOT_EXCLUDED_DIRECTORY_NAMES
)
_ROOT_LIFECYCLE_ANCHOR_KEYS: Final = frozenset(
    path.casefold() for path in _ROOT_LIFECYCLE_ANCHORS
)
_EXCLUDED_DIRECTORY_KEYS: Final = frozenset(
    name.casefold() for name in _EXCLUDED_DIRECTORY_NAMES
)
_TEMPORARY_SUFFIXES: Final = (
    ".pyc",
    ".pyo",
    ".tmp",
    ".temp",
    ".swp",
    ".swo",
    ".bak",
    ".orig",
    ".rej",
)
_AUDIO_ASSET_SUFFIXES: Final = (
    ".wav",
    ".wave",
    ".flac",
    ".mp3",
    ".ogg",
    ".oga",
    ".aif",
    ".aiff",
    ".sf2",
    ".sf3",
)
_WINDOWS_ILLEGAL_CHARACTERS: Final = frozenset('<>:"\\|?*')
_COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40,64}$")
_BYTE_ORDER_MARKS: Final = (
    b"\xef\xbb\xbf",  # UTF-8
    b"\xff\xfe\x00\x00",  # UTF-32 little-endian
    b"\x00\x00\xfe\xff",  # UTF-32 big-endian
    b"\xff\xfe",  # UTF-16 little-endian
    b"\xfe\xff",  # UTF-16 big-endian
)


class ReleaseBuildError(RuntimeError):
    """Raised when a source snapshot cannot be published safely."""


@dataclass(frozen=True)
class TrackedFile:
    path: str
    mode: str
    object_id: str


@dataclass(frozen=True)
class SourcePayload:
    path: str
    mode: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _run_git(repo: Path, arguments: Sequence[str]) -> bytes:
    command = ["git", "-C", str(repo), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ReleaseBuildError(f"could not run Git: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"Git exited with status {completed.returncode}"
        raise ReleaseBuildError(detail)
    return completed.stdout


def _repository_root(repo: str | Path) -> Path:
    requested = Path(repo).expanduser().resolve()
    raw_root = _run_git(requested, ["rev-parse", "--show-toplevel"])
    try:
        discovered = Path(raw_root.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("Git repository path is not valid UTF-8") from exc
    if os.path.normcase(str(requested)) != os.path.normcase(str(discovered)):
        raise ReleaseBuildError(
            f"repository must be its Git worktree root: {discovered}"
        )
    return discovered


def _head_commit(repo: Path) -> str:
    raw = _run_git(repo, ["rev-parse", "--verify", "HEAD"]).strip()
    try:
        commit = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("Git returned a non-ASCII commit identifier") from exc
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseBuildError(f"invalid Git commit identifier: {commit!r}")
    return commit


def _worktree_status(repo: Path) -> bytes:
    return _run_git(
        repo,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    )


def _tracked_files(repo: Path) -> list[TrackedFile]:
    output = _run_git(repo, ["ls-files", "--stage", "-z"])
    tracked: list[TrackedFile] = []
    seen: set[str] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ReleaseBuildError("could not parse `git ls-files --stage`")
        raw_mode, raw_object_id, raw_stage = fields
        try:
            mode = raw_mode.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            stage = int(raw_stage)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseBuildError(
                "tracked paths and index metadata must be valid UTF-8/ASCII"
            ) from exc
        if stage != 0:
            raise ReleaseBuildError(
                f"unmerged index entry cannot enter a release: {path!r}"
            )
        if path in seen:
            raise ReleaseBuildError(f"duplicate tracked path: {path!r}")
        if _COMMIT_RE.fullmatch(object_id) is None:
            raise ReleaseBuildError(
                f"invalid Git object identifier for {path!r}"
            )
        seen.add(path)
        tracked.append(TrackedFile(path=path, mode=mode, object_id=object_id))
    return tracked


def _head_tracked_paths(repo: Path, commit: str) -> set[str]:
    output = _run_git(
        repo,
        ["ls-tree", "-r", "--name-only", "-z", commit],
    )
    paths: set[str] = set()
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError(
                "committed tracked paths must be valid UTF-8"
            ) from exc
        if path in paths:
            raise ReleaseBuildError(f"duplicate committed path: {path!r}")
        paths.add(path)
    return paths


def _excluded_reason(path: str) -> str | None:
    parts = path.split("/")
    if path.casefold() in _REPOSITORY_ONLY_ROOT_DOCUMENT_KEYS:
        return _REPOSITORY_ONLY_DOCUMENT_REASON
    if (
        len(parts) == 1
        and path.casefold().endswith(".md")
        and path.casefold() not in _PUBLIC_DOCUMENT_KEYS
    ):
        return _REPOSITORY_ONLY_DOCUMENT_REASON
    if (
        parts[0].casefold() == "docs"
        and path.casefold() not in _PUBLIC_DOCUMENT_KEYS
    ):
        return _REPOSITORY_ONLY_DOCUMENT_REASON
    if (
        parts[0].casefold() == "乐器"
        and path.casefold().endswith(".md")
        and parts[-1].casefold() not in _PUBLIC_INSTRUMENT_DOCUMENT_KEYS
    ):
        return _REPOSITORY_ONLY_DOCUMENT_REASON
    lifecycle_anchor = path.casefold() in _ROOT_LIFECYCLE_ANCHOR_KEYS
    for index, part in enumerate(parts):
        folded = part.casefold()
        if (
            index == 0
            and folded in _ROOT_EXCLUDED_DIRECTORY_KEYS
            and not lifecycle_anchor
        ):
            return f"excluded root directory: {part}"
        if folded in _EXCLUDED_DIRECTORY_KEYS:
            return f"excluded directory component: {part}"
        if folded.endswith(".egg-info"):
            return f"excluded package-build directory: {part}"

    filename = parts[-1]
    folded_filename = filename.casefold()
    if folded_filename in {
        ".ds_store",
        "thumbs.db",
        "desktop.ini",
        ".coverage",
    }:
        return f"excluded cache or operating-system file: {filename}"
    if (
        folded_filename.endswith(_TEMPORARY_SUFFIXES)
        or filename.endswith("~")
        or (filename.startswith("#") and filename.endswith("#"))
        or filename.startswith(".~")
    ):
        return f"excluded temporary file: {filename}"
    if folded_filename.endswith(_AUDIO_ASSET_SUFFIXES):
        return f"excluded audio asset: {filename}"
    return None


def _validate_portable_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path:
        raise ReleaseBuildError(f"non-portable tracked path: {path!r}")
    if path == MANIFEST_NAME or path.casefold() == MANIFEST_NAME.casefold():
        raise ReleaseBuildError(
            f"tracked path conflicts with generated {MANIFEST_NAME}: {path!r}"
        )
    if len(path.encode("utf-16-le")) // 2 > 240:
        raise ReleaseBuildError(
            f"tracked relative path exceeds 240 UTF-16 code units: {path!r}"
        )

    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseBuildError(f"unsafe tracked path: {path!r}")
    for part in parts:
        if unicodedata.normalize("NFC", part) != part:
            raise ReleaseBuildError(
                f"tracked path is not Unicode NFC and is not portable: {path!r}"
            )
        if part[-1] in {" ", "."}:
            raise ReleaseBuildError(
                f"tracked path has a Windows-unsafe suffix: {path!r}"
            )
        if any(
            ord(character) < 32
            or ord(character) == 127
            or character in _WINDOWS_ILLEGAL_CHARACTERS
            for character in part
        ):
            raise ReleaseBuildError(
                f"tracked path contains a non-portable character: {path!r}"
            )
        device_candidate = part.rstrip(" .").split(".", 1)[0].upper()
        if device_candidate in _WINDOWS_RESERVED_NAMES:
            raise ReleaseBuildError(
                f"tracked path uses a reserved Windows name: {path!r}"
            )
        if len(part.encode("utf-8")) > 255:
            raise ReleaseBuildError(
                f"tracked path component exceeds 255 UTF-8 bytes: {path!r}"
            )


def _portable_collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _select_release_entries(
    tracked: Iterable[TrackedFile],
) -> tuple[list[TrackedFile], list[dict[str, str]]]:
    included: list[TrackedFile] = []
    excluded: list[dict[str, str]] = []
    portable_paths: dict[str, str] = {}
    for entry in sorted(tracked, key=lambda item: item.path):
        reason = _excluded_reason(entry.path)
        if reason is not None:
            excluded.append({"path": entry.path, "reason": reason})
            continue
        _validate_portable_path(entry.path)
        key = _portable_collision_key(entry.path)
        previous = portable_paths.get(key)
        if previous is not None:
            raise ReleaseBuildError(
                "tracked paths collide on a case-insensitive or "
                f"Unicode-normalizing filesystem: {previous!r}, {entry.path!r}"
            )
        portable_paths[key] = entry.path
        if entry.mode == "120000":
            raise ReleaseBuildError(
                f"tracked symbolic links are not allowed: {entry.path!r}"
            )
        if entry.mode not in _REGULAR_GIT_MODES:
            raise ReleaseBuildError(
                f"unsupported tracked file mode {entry.mode}: {entry.path!r}"
            )
        included.append(entry)

    included_names = {entry.path for entry in included}
    missing = sorted(_REQUIRED_ROOT_FILES - included_names)
    if missing:
        raise ReleaseBuildError(
            "required release files are missing or excluded: "
            + ", ".join(missing)
        )
    return included, excluded


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ReleaseBuildError("unexpected end of `git cat-file` output")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_committed_payloads(
    repo: Path,
    entries: Sequence[TrackedFile],
) -> list[SourcePayload]:
    command = ["git", "-C", str(repo), "cat-file", "--batch"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ReleaseBuildError(f"could not run Git: {exc}") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    payloads: list[SourcePayload] = []
    cache: dict[str, bytes] = {}
    try:
        for entry in entries:
            content = cache.get(entry.object_id)
            if content is None:
                process.stdin.write(entry.object_id.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().rstrip(b"\n")
                fields = header.split()
                if (
                    len(fields) != 3
                    or fields[0].decode("ascii", errors="replace")
                    != entry.object_id
                    or fields[1] != b"blob"
                ):
                    raise ReleaseBuildError(
                        f"could not read committed blob for {entry.path!r}"
                    )
                try:
                    size = int(fields[2])
                except ValueError as exc:
                    raise ReleaseBuildError(
                        f"invalid Git blob size for {entry.path!r}"
                    ) from exc
                content = _read_exact(process.stdout, size)
                if process.stdout.read(1) != b"\n":
                    raise ReleaseBuildError(
                        f"invalid Git blob terminator for {entry.path!r}"
                    )
                cache[entry.object_id] = content
            payloads.append(
                SourcePayload(
                    path=entry.path,
                    mode=entry.mode,
                    content=content,
                )
            )
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        return_code = process.wait()
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        process.stdout.close()
        process.stderr.close()
        if return_code != 0 and sys.exc_info()[0] is None:
            raise ReleaseBuildError(
                stderr or f"`git cat-file` exited with status {return_code}"
            )
    return payloads


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _canonicalize_trusted_darwin_root_alias(path: Path) -> Path:
    """Map only verified macOS system aliases without resolving descendants.

    macOS exposes its private temporary roots through root-owned links such as
    ``/var -> /private/var``.  Resolving the whole output path would hide an
    untrusted link farther down the tree, so only the verified system prefix is
    rewritten and the normal ancestor walk still checks every descendant.
    """

    if sys.platform != "darwin" or not path.is_absolute():
        return path
    for declared_name, canonical_name in (
        ("/var", "/private/var"),
        ("/tmp", "/private/tmp"),
    ):
        declared = Path(declared_name)
        canonical = Path(canonical_name)
        try:
            relative = path.relative_to(declared)
        except ValueError:
            continue
        try:
            declared_metadata = declared.lstat()
            followed_metadata = declared.stat()
            canonical_metadata = canonical.lstat()
        except OSError:
            return path
        if (
            not stat.S_ISLNK(declared_metadata.st_mode)
            or getattr(declared_metadata, "st_uid", -1) != 0
            or not stat.S_ISDIR(canonical_metadata.st_mode)
            or stat.S_ISLNK(canonical_metadata.st_mode)
            or _is_reparse_point(canonical_metadata)
            or getattr(canonical_metadata, "st_uid", -1) != 0
            or not os.path.samestat(followed_metadata, canonical_metadata)
        ):
            return path
        return canonical / relative
    return path


def _validate_output_ancestors(path: Path) -> None:
    current = _canonicalize_trusted_darwin_root_alias(path)
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReleaseBuildError(
                f"could not inspect source release output path: {current}: {exc}"
            ) from exc
        else:
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                raise ReleaseBuildError(
                    "source release output may not traverse a symbolic link "
                    f"or reparse point: {current}"
                )
        parent = current.parent
        if parent == current:
            break
        current = parent


def _canonical_path_identity(path: Path, *, purpose: str) -> Path:
    """Return one fail-closed filesystem identity for security comparisons.

    On Windows, ``Path.resolve`` expands 8.3 aliases such as ``RUNNER~1``.
    Callers that also reject symlinks or reparse points must validate the
    original path spelling before calling this helper; resolving first would
    hide those redirecting ancestors.
    """

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReleaseBuildError(
            f"could not resolve {purpose} for a safe identity check: {path}"
        ) from exc


def _read_dirty_payload(
    repo: Path,
    entry: TrackedFile,
) -> SourcePayload | None:
    current = repo
    for component in entry.path.split("/"):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # An unstaged deletion/rename is a valid dirty snapshot operation.
            # The replacement side of an unstaged rename remains untracked and
            # therefore deliberately stays outside the archive.
            return None
        except OSError as exc:
            raise ReleaseBuildError(
                f"tracked file is unavailable: {entry.path!r}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise ReleaseBuildError(
                f"symbolic links/reparse points are not followed: {entry.path!r}"
            )
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBuildError(
            f"tracked release entry is not a regular file: {entry.path!r}"
        )
    try:
        content = current.read_bytes()
    except OSError as exc:
        raise ReleaseBuildError(
            f"could not read tracked file {entry.path!r}: {exc}"
        ) from exc
    return SourcePayload(path=entry.path, mode=entry.mode, content=content)


def _read_dirty_payloads(
    repo: Path,
    entries: Sequence[TrackedFile],
) -> tuple[list[SourcePayload], list[dict[str, str]]]:
    payloads: list[SourcePayload] = []
    absent: list[dict[str, str]] = []
    for entry in entries:
        payload = _read_dirty_payload(repo, entry)
        if payload is None:
            absent.append(
                {
                    "path": entry.path,
                    "reason": "tracked path absent from dirty working tree",
                }
            )
        else:
            payloads.append(payload)
    return payloads, absent


def _project_metadata(payloads: Sequence[SourcePayload]) -> tuple[str, str]:
    payload_by_path = {payload.path: payload.content for payload in payloads}
    for required in sorted(_REQUIRED_ROOT_FILES):
        if not payload_by_path.get(required):
            raise ReleaseBuildError(
                f"required release file must be non-empty: {required}"
            )
    try:
        pyproject_text = payload_by_path["pyproject.toml"].decode("utf-8")
        pyproject = tomllib.loads(pyproject_text)
        project = pyproject["project"]
        name = project["name"]
        version = project["version"]
        license_files = project["license-files"]
    except (
        KeyError,
        TypeError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        raise ReleaseBuildError(
            "pyproject.toml must define UTF-8 [project] name and version"
        ) from exc
    if not isinstance(name, str) or not name.strip():
        raise ReleaseBuildError("pyproject.toml project.name must be non-empty")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseBuildError("pyproject.toml project.version must be non-empty")
    if any(character in version for character in "/\\\0\r\n"):
        raise ReleaseBuildError("pyproject.toml project.version is not portable")
    if (
        not isinstance(license_files, list)
        or not all(isinstance(item, str) for item in license_files)
        or not {"LICENSE", "NOTICE", "OUTPUT_RIGHTS.md"}.issubset(license_files)
    ):
        raise ReleaseBuildError(
            "pyproject.toml project.license-files must include LICENSE, "
            "NOTICE, and OUTPUT_RIGHTS.md"
        )
    for license_path in license_files:
        _validate_portable_path(license_path)
        if not payload_by_path.get(license_path):
            raise ReleaseBuildError(
                "pyproject.toml declares a missing, excluded, or empty "
                f"license file: {license_path}"
            )

    runtime_init = payload_by_path.get("tianlai/__init__.py")
    if runtime_init is not None:
        try:
            tree = ast.parse(runtime_init.decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise ReleaseBuildError(
                "tianlai/__init__.py is not valid UTF-8 Python"
            ) from exc
        declared_versions: list[str] = []
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            value = statement.value
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in targets
            ):
                if not isinstance(value, ast.Constant) or not isinstance(
                    value.value, str
                ):
                    raise ReleaseBuildError(
                        "tianlai.__version__ must be a string literal"
                    )
                declared_versions.append(value.value)
        if declared_versions and (
            len(declared_versions) != 1 or declared_versions[0] != version
        ):
            raise ReleaseBuildError(
                "tianlai.__version__ does not match pyproject.toml "
                f"project.version ({version})"
            )
    return name, version


def _validate_public_documents(
    payloads: Sequence[SourcePayload],
) -> None:
    payload_by_path = {payload.path: payload.content for payload in payloads}
    missing = sorted(
        path
        for path in _PUBLIC_DOCUMENT_PATHS
        if not payload_by_path.get(path, b"").strip()
    )
    if missing:
        raise ReleaseBuildError(
            "public release documents must exist and be non-empty: "
            + ", ".join(missing)
        )

    instrument_documents = {
        path: content
        for path, content in payload_by_path.items()
        if path.split("/", 1)[0].casefold() == "乐器"
        and PurePosixPath(path).name.casefold()
        in _PUBLIC_INSTRUMENT_DOCUMENT_KEYS
    }
    empty_instrument_documents = sorted(
        path
        for path, content in instrument_documents.items()
        if not content.strip()
    )
    if empty_instrument_documents:
        raise ReleaseBuildError(
            "public instrument documents must be non-empty: "
            + ", ".join(empty_instrument_documents)
        )

    counterpart_by_key = {
        source.casefold(): counterpart
        for chinese, english in _PUBLIC_INSTRUMENT_DOCUMENT_PAIRS
        for source, counterpart in ((chinese, english), (english, chinese))
    }
    missing_instrument_pairs: list[str] = []
    for path in sorted(instrument_documents):
        parsed = PurePosixPath(path)
        counterpart = str(
            parsed.with_name(counterpart_by_key[parsed.name.casefold()])
        )
        if not payload_by_path.get(counterpart, b"").strip():
            missing_instrument_pairs.append(f"{path} -> {counterpart}")
    if missing_instrument_pairs:
        raise ReleaseBuildError(
            "public instrument documentation must be bilingual: "
            + ", ".join(missing_instrument_pairs)
        )


def _markdown_without_fenced_code(text: str) -> str:
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        marker = re.match(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})", line)
        if marker is not None:
            run = marker.group("marker")
            kind = run[0]
            if fence is None:
                fence = (kind, len(run))
            elif kind == fence[0] and len(run) >= fence[1]:
                fence = None
            continue
        if fence is None:
            kept.append(line)
    return "\n".join(kept)


def _resolve_markdown_target(source: str, raw_target: str) -> str | None:
    target = raw_target[1:-1] if raw_target.startswith("<") else raw_target
    target = target.strip()
    if (
        not target
        or target.startswith("#")
        or target.startswith("//")
        or _URI_SCHEME.match(target)
    ):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    target = unquote(target)
    if not target:
        return None
    if "\\" in target or target.startswith("/"):
        raise ReleaseBuildError(
            "local Markdown link must use a relative POSIX path: "
            f"{source!r} -> {raw_target!r}"
        )
    resolved = posixpath.normpath(
        str(PurePosixPath(source).parent / target)
    )
    if resolved == ".." or resolved.startswith("../"):
        raise ReleaseBuildError(
            "local Markdown link escapes the release root: "
            f"{source!r} -> {raw_target!r}"
        )
    return resolved


def _validate_markdown_links(
    payloads: Sequence[SourcePayload],
) -> None:
    included = {payload.path for payload in payloads}
    payload_by_path = {payload.path: payload for payload in payloads}
    for payload in payloads:
        if not payload.path.casefold().endswith(".md"):
            continue
        link_source = _PACKAGED_MARKDOWN_SOURCE_PATHS.get(
            payload.path, payload.path
        )
        if link_source != payload.path:
            canonical = payload_by_path.get(link_source)
            if canonical is None or canonical.content != payload.content:
                raise ReleaseBuildError(
                    "packaged constitution resource must be an exact copy "
                    f"of its public source: {payload.path!r} -> "
                    f"{link_source!r}"
                )
        try:
            text = payload.content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError(
                f"public Markdown must be UTF-8: {payload.path!r}"
            ) from exc
        visible_text = _markdown_without_fenced_code(text)
        for match in _MARKDOWN_LINK.finditer(visible_text):
            raw_target = match.group("target")
            # Package resources are byte-for-byte copies of public source
            # documents.  Their relative links deliberately retain the
            # source document's directory as their base; the runtime copy is
            # a fixed law text, not a second navigable documentation tree.
            target = _resolve_markdown_target(link_source, raw_target)
            if target is not None and target not in included:
                raise ReleaseBuildError(
                    "local Markdown link target is not included in the "
                    f"source release: {payload.path!r} -> {raw_target!r}"
                )


def _validate_windows_batch_payloads(
    payloads: Sequence[SourcePayload],
) -> None:
    """Keep committed ``.cmd`` bytes directly executable after ZIP extraction.

    Formal releases are serialized from Git blobs, not from a checked-out
    worktree.  Git's normal ``text eol=crlf`` rule stores LF-normalized blobs,
    so Windows batch files must instead be tracked byte-for-byte and verified
    here.  UTF-8 without a BOM plus explicit CRLF is the repository contract.
    """

    for payload in payloads:
        if not payload.path.casefold().endswith(".cmd"):
            continue
        content = payload.content
        if any(content.startswith(mark) for mark in _BYTE_ORDER_MARKS):
            raise ReleaseBuildError(
                "Windows batch files in a source release must not contain "
                f"a byte-order mark: {payload.path!r}"
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError(
                "Windows batch files in a source release must be valid "
                f"UTF-8: {payload.path!r}"
            ) from exc
        remainder = content.replace(b"\r\n", b"")
        if b"\r" in remainder or b"\n" in remainder:
            raise ReleaseBuildError(
                "Windows batch files in a source release must use CRLF "
                f"line endings only: {payload.path!r}"
            )


def _manifest_document(
    *,
    project_name: str,
    project_version: str,
    commit: str,
    dirty: bool,
    allow_dirty_requested: bool,
    payloads: Sequence[SourcePayload],
    excluded: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    repository_only_document_count = sum(
        row.get("reason") == _REPOSITORY_ONLY_DOCUMENT_REASON
        or (
            str(row.get("path", "")).casefold().endswith(".md")
            and _excluded_reason(str(row.get("path", ""))) is not None
        )
        for row in excluded
    )
    files = [
        {
            "path": payload.path,
            "sha256": payload.sha256,
            "size": len(payload.content),
            "git_mode": payload.mode,
        }
        for payload in payloads
    ]
    return {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "project_name": project_name,
        "project_version": project_version,
        "project_version_source": "pyproject.toml:[project].version",
        "commit": commit,
        "dirty": dirty,
        "allow_dirty_requested": allow_dirty_requested,
        "local_test_only": allow_dirty_requested,
        "source_scope": (
            "Git-tracked regular files selected by the public release policy"
        ),
        "file_count": len(files),
        "files": files,
        "exclusions": {
            "policy": (
                "Untracked files are never inspected or included. Tracked "
                "symbolic links and non-regular entries are rejected, never "
                "followed. Generated release-manifest.json is intentionally "
                "not self-hashed. Documentation not on the explicit public "
                "allowlist, including non-public instrument notes, remains "
                "repository-only; its paths are not exposed in this public "
                "manifest. Other excluded tracked paths are likewise reported "
                "only as an aggregate count."
            ),
            "excluded_root_directory_count": len(
                _ROOT_EXCLUDED_DIRECTORY_NAMES
            ),
            "included_lifecycle_anchors": sorted(_ROOT_LIFECYCLE_ANCHORS),
            "public_document_allowlist": sorted(_PUBLIC_DOCUMENT_PATHS),
            "public_instrument_document_names": sorted(
                _PUBLIC_INSTRUMENT_DOCUMENT_NAMES
            ),
            "repository_only_document_count": (
                repository_only_document_count
            ),
            "excluded_directory_component_count": len(
                _EXCLUDED_DIRECTORY_NAMES
            ),
            "temporary_suffixes": list(_TEMPORARY_SUFFIXES),
            "audio_asset_suffixes": list(_AUDIO_ASSET_SUFFIXES),
            "excluded_tracked_path_count": len(excluded),
        },
    }


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_info(path: str, mode: str = "100644") -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=path, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    permissions = 0o755 if mode == "100755" else 0o644
    info.external_attr = (stat.S_IFREG | permissions) << 16
    return info


def _write_staged_archive(
    temporary: Path,
    payloads: Sequence[SourcePayload],
    manifest_bytes: bytes,
) -> None:
    with zipfile.ZipFile(
        temporary,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
        strict_timestamps=True,
    ) as archive:
        for payload in payloads:
            archive.writestr(
                _zip_info(payload.path, payload.mode),
                payload.content,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
        archive.writestr(
            _zip_info(MANIFEST_NAME),
            manifest_bytes,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        )
        archive.comment = b"Tianlai auditable source release"

    expected_names = [payload.path for payload in payloads] + [MANIFEST_NAME]
    try:
        with zipfile.ZipFile(temporary, mode="r") as archive:
            if archive.namelist() != expected_names:
                raise ReleaseBuildError(
                    "staged source release member list is inconsistent"
                )
            damaged = archive.testzip()
            if damaged is not None:
                raise ReleaseBuildError(
                    f"staged source release failed CRC validation: {damaged}"
                )
            if archive.read(MANIFEST_NAME) != manifest_bytes:
                raise ReleaseBuildError(
                    "staged source release manifest is inconsistent"
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ReleaseBuildError):
            raise
        raise ReleaseBuildError(
            f"could not verify staged source release: {exc}"
        ) from exc

    # Windows' CRT rejects fsync() on a read-only descriptor. Opening the
    # completed staging file read/write does not modify its bytes.
    with temporary.open("r+b") as archive_file:
        os.fsync(archive_file.fileno())


def build_source_release(
    repo: str | Path,
    output: str | Path,
    *,
    allow_dirty: bool = False,
    overwrite: bool = False,
    expected_version: str | None = None,
) -> dict[str, object]:
    """Build and atomically publish one audited source-release ZIP.

    Clean worktrees are serialized from committed Git blobs, which avoids
    checkout line-ending differences. ``allow_dirty`` switches to the tracked
    working-tree bytes and marks the result as local-test-only. Untracked files
    are never enumerated as release inputs. When ``expected_version`` is set,
    the archive is not published unless committed package metadata matches it.
    """

    if not isinstance(allow_dirty, bool) or not isinstance(overwrite, bool):
        raise TypeError("allow_dirty and overwrite must be bool")
    if expected_version is not None and (
        not isinstance(expected_version, str) or not expected_version
    ):
        raise TypeError("expected_version must be a non-empty string or None")

    target = Path(output).expanduser().absolute()
    if target.suffix.casefold() != ".zip":
        raise ReleaseBuildError("source release output must use the .zip suffix")

    root = _repository_root(repo)
    _validate_output_ancestors(target)
    target_identity = _canonical_path_identity(
        target,
        purpose="source release output",
    )
    if target.exists() and not overwrite:
        raise FileExistsError(f"source release already exists: {target}")
    if target.exists() and not target.is_file():
        raise ReleaseBuildError(f"source release output is not a file: {target}")

    commit = _head_commit(root)
    initial_status = _worktree_status(root)
    dirty = bool(initial_status)
    if dirty and not allow_dirty:
        raise ReleaseBuildError(
            "Git worktree is dirty; commit/stash changes or use "
            "--allow-dirty for a local-test-only archive"
        )

    tracked = _tracked_files(root)
    head_paths = _head_tracked_paths(root, commit)
    try:
        target_relative = target_identity.relative_to(root).as_posix()
    except ValueError:
        target_relative = None
    if target_relative is not None:
        if target_relative.split("/", 1)[0].casefold() == ".git":
            raise ReleaseBuildError(
                "source release output may not be written inside .git"
            )
        tracked_output_keys = {
            _portable_collision_key(entry.path) for entry in tracked
        }
        tracked_output_keys.update(
            _portable_collision_key(path) for path in head_paths
        )
        if _portable_collision_key(target_relative) in tracked_output_keys:
            raise ReleaseBuildError(
                "source release output may not overwrite a Git-tracked path: "
                f"{target_relative}"
            )
    entries, excluded = _select_release_entries(tracked)
    index_paths = {entry.path for entry in tracked}
    for path in sorted(head_paths - index_paths):
        reason = _excluded_reason(path)
        excluded.append(
            {
                "path": path,
                "reason": (
                    reason
                    if reason == _REPOSITORY_ONLY_DOCUMENT_REASON
                    else "tracked path deleted from Git index relative to commit"
                ),
            }
        )
    if dirty:
        payloads, absent = _read_dirty_payloads(root, entries)
        excluded.extend(absent)
    else:
        payloads = _read_committed_payloads(root, entries)
    _validate_windows_batch_payloads(payloads)
    _validate_public_documents(payloads)
    _validate_markdown_links(payloads)
    project_name, project_version = _project_metadata(payloads)
    if (
        expected_version is not None
        and project_version != expected_version
    ):
        raise ReleaseBuildError(
            "committed project version does not match the expected release "
            f"version: {project_version!r} != {expected_version!r}"
        )

    if _head_commit(root) != commit or _worktree_status(root) != initial_status:
        raise ReleaseBuildError(
            "Git commit or worktree status changed while collecting the release"
        )

    manifest = _manifest_document(
        project_name=project_name,
        project_version=project_version,
        commit=commit,
        dirty=dirty,
        allow_dirty_requested=allow_dirty,
        payloads=payloads,
        excluded=excluded,
    )
    manifest_bytes = _json_bytes(manifest)

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=".tianlai-source-release.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published = False
    archive_sha256: str | None = None
    try:
        _write_staged_archive(temporary, payloads, manifest_bytes)
        archive_sha256 = _sha256_file(temporary)
        try:
            temporary.chmod(0o644)
        except OSError:
            # ZIP member modes are explicit. A host that cannot adjust the
            # outer file mode may still publish the complete archive safely.
            pass
        _validate_output_ancestors(target)
        if (
            _canonical_path_identity(
                target,
                purpose="source release output",
            )
            != target_identity
        ):
            raise ReleaseBuildError(
                "source release output identity changed during the build"
            )
        if overwrite:
            os.replace(temporary, target)
        else:
            # A same-filesystem hard link is an atomic create-if-absent
            # operation. It closes the race left by exists()+os.replace().
            os.link(temporary, target)
        published = True
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                # Publication is the commit point. Once the complete archive
                # is visible, failure to remove its private hard-link name
                # must not turn success into an ambiguous exception.
                if not published and sys.exc_info()[0] is None:
                    raise

    if not published or archive_sha256 is None:
        raise ReleaseBuildError("source release was not published")
    return {
        "output": str(target),
        "archive_sha256": archive_sha256,
        "project_version": project_version,
        "commit": commit,
        "dirty": dirty,
        "file_count": len(payloads),
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an auditable ZIP from Git-tracked Tianlai source files."
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Git worktree root (default: the project containing this tool)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="destination .zip file",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "allow tracked working-tree changes for local testing; the "
            "manifest will mark the archive local-test-only"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="atomically replace an existing ZIP",
    )
    parser.add_argument(
        "--expected-version",
        help=(
            "fail before publication unless committed package metadata "
            "matches this version exactly"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = build_source_release(
            arguments.repo,
            arguments.output,
            allow_dirty=arguments.allow_dirty,
            overwrite=arguments.overwrite,
            expected_version=arguments.expected_version,
        )
    except (FileExistsError, ReleaseBuildError, OSError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "output": result["output"],
                "archive_sha256": result["archive_sha256"],
                "project_version": result["project_version"],
                "commit": result["commit"],
                "dirty": result["dirty"],
                "file_count": result["file_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
