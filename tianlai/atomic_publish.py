"""Private primitives for publishing small local artifacts.

The helpers in this module are deliberately conservative.  A destination
that appears during a no-replace publication is not overwritten.  Temporary
and backup entries are bound to filesystem identities before cleanup, and an
observed mismatch is preserved.  Random private names protect ordinary local
writer races; a same-user adversary that can discover and replace such a name
inside the final pathname operation is outside this cooperative contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import threading
from typing import BinaryIO
import warnings

from .plain_file import PlainFileIdentity, sha256_plain_file
from .render_lock import (
    PlainDirectoryIdentity,
    bind_plain_sibling_path,
    ensure_plain_directory_tree,
    revalidate_plain_directory,
)


_PRIVATE_PATH_ATTEMPTS = 32
_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class _PrivateFileClaim:
    """Filesystem identity captured when one private file was created."""

    path: Path
    file_key: tuple[int, int]
    parent_identity: PlainDirectoryIdentity
    _state: _PrivateFileClaimState = field(
        default_factory=lambda: _PrivateFileClaimState(),
        repr=False,
        compare=False,
    )
    _generation: int = 0


@dataclass(slots=True)
class _PrivateFileClaimState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    generation: int = 0
    writer_active: bool = False
    sealed: bool = False
    closed: bool = False


@dataclass(frozen=True, slots=True)
class _SealedPrivateFileClaim:
    claim: _PrivateFileClaim
    identity: PlainFileIdentity
    sha256: str


def _rename_noreplace(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> None:
    """Atomically rename one same-volume entry only if the target is absent."""

    source_path = os.fspath(source)
    destination_path = os.fspath(destination)
    if os.name == "nt":
        # Windows MoveFile semantics, exposed by os.rename(), reject an
        # existing file or directory instead of replacing it.
        os.rename(source_path, destination_path)
        return

    source_bytes = os.fsencode(source_path)
    destination_bytes = os.fsencode(destination_path)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(
                errno.ENOTSUP,
                "renameat2 is unavailable; refusing a clobbering rename",
                destination_path,
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
            raise OSError(
                errno.ENOTSUP,
                "renamex_np is unavailable; refusing a clobbering rename",
                destination_path,
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
        raise OSError(
            errno.ENOTSUP,
            f"{sys.platform} has no configured no-replace rename",
            destination_path,
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_path)


def _pretty_json_bytes(value: object) -> bytes:
    """Return the repository's deterministic human-readable JSON encoding."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _same_file(
    left: PlainFileIdentity,
    right: PlainFileIdentity,
    *,
    left_sha256: str,
    right_sha256: str,
) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.modified_ns == right.modified_ns
        and left_sha256 == right_sha256
    )


def _capture_file(path: Path) -> tuple[PlainFileIdentity, str]:
    return sha256_plain_file(path)


def _file_key(identity: PlainFileIdentity) -> tuple[int, int]:
    return identity.device, identity.inode


def _descriptor_file_key(descriptor: int) -> tuple[int, int]:
    value = os.fstat(descriptor)
    if (
        not stat.S_ISREG(value.st_mode)
        or int(value.st_ino) == 0
        or int(value.st_nlink) != 1
    ):
        raise OSError("publication staging descriptor is not a plain file")
    return int(value.st_dev), int(value.st_ino)


def _path_file_key(
    path: Path,
    *,
    allow_additional_links: bool = False,
) -> tuple[int, int]:
    """Return the key of one already-isolated plain private pathname."""

    value = os.lstat(path)
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)
        or int(value.st_ino) == 0
        or (
            int(value.st_nlink) < 1
            or (not allow_additional_links and int(value.st_nlink) != 1)
        )
    ):
        raise OSError("private pathname is not a plain file")
    return int(value.st_dev), int(value.st_ino)


def _validate_sha256(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _require_live_claim_locked(
    claim: _PrivateFileClaim,
    *,
    allow_sealed: bool,
) -> _PrivateFileClaimState:
    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    state = claim._state
    if claim._generation != state.generation:
        raise RuntimeError("private file claim generation is stale")
    if state.closed:
        raise RuntimeError("private file claim is closed")
    if state.writer_active:
        raise RuntimeError("private file claim already has an active writer")
    if state.sealed and not allow_sealed:
        raise RuntimeError("sealed private file claim cannot be written")
    return state


def _advance_claim_locked(
    claim: _PrivateFileClaim,
    *,
    path: Path,
    file_key: tuple[int, int],
) -> _PrivateFileClaim:
    state = claim._state
    state.generation += 1
    return _PrivateFileClaim(
        path=path,
        file_key=file_key,
        parent_identity=claim.parent_identity,
        _state=state,
        _generation=state.generation,
    )


def _private_sibling(parent: Path, stem: str) -> Path:
    for _ in range(_PRIVATE_PATH_ATTEMPTS):
        candidate = parent / f".{stem}.{secrets.token_hex(16)}"
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError("could not allocate a private publication path")


def _add_cleanup_note(primary: BaseException, message: str) -> None:
    try:
        primary.add_note(message)
    except BaseException:
        pass


def _retire_owned_file(
    path: Path,
    identity: PlainFileIdentity,
    digest: str,
    *,
    parent_identity: PlainDirectoryIdentity,
    require_present: bool = False,
) -> None:
    """Remove one identity-bound private file without trusting its old name.

    The unpredictable quarantine name and post-move identity check close the
    ordinary cleanup race that previously deleted a writer installed at the
    mutable temporary pathname.  A same-user adversary able to discover and
    replace that random private name in the final revalidate/unlink boundary
    is outside this local cooperative-writer contract; an observed mismatch
    is always retained.
    """

    if not os.path.lexists(path):
        if require_present:
            raise FileNotFoundError(
                errno.ENOENT,
                "identity-bound private file disappeared before cleanup",
                str(path),
            )
        return
    parent = revalidate_plain_directory(parent_identity)
    if path.parent != parent:
        raise OSError(errno.EPERM, "publication file escaped its parent", str(path))
    quarantine = _private_sibling(parent, f"{path.name.lstrip('.')}.retired")
    _rename_noreplace(path, quarantine)
    moved_identity, moved_digest = _capture_file(quarantine)
    if not _same_file(
        identity,
        moved_identity,
        left_sha256=digest,
        right_sha256=moved_digest,
    ):
        raise OSError(
            errno.ESTALE,
            "publication file changed before cleanup; replacement preserved",
            str(quarantine),
        )
    revalidate_plain_directory(parent_identity)
    # See the threat-boundary note above.  The path is random, same-parent,
    # and was just rebound to the captured inode before this final removal.
    quarantine.unlink()


def _retire_file_key(
    path: Path,
    expected_key: tuple[int, int],
    *,
    parent_identity: PlainDirectoryIdentity,
) -> None:
    """Retire a private file whose inode was bound at exclusive creation."""

    if not os.path.lexists(path):
        return
    parent = revalidate_plain_directory(parent_identity)
    if path.parent != parent:
        raise OSError(errno.EPERM, "publication file escaped its parent", str(path))
    quarantine = _private_sibling(parent, f"{path.name.lstrip('.')}.retired")
    _rename_noreplace(path, quarantine)
    if _path_file_key(quarantine) != expected_key:
        raise OSError(
            errno.ESTALE,
            "publication file changed before cleanup; replacement preserved",
            str(quarantine),
        )
    revalidate_plain_directory(parent_identity)
    quarantine.unlink()


def _reserve_private_file(
    directory: str | os.PathLike[str],
    *,
    prefix: str,
    suffix: str,
) -> _PrivateFileClaim:
    """Create an empty private file and retain its exclusive identity claim.

    The returned path may be reopened by a caller that needs a pathname-based
    writer.  Cleanup must use :func:`_retire_private_file`, which distinguishes
    that originally created inode from a later entry installed at the same
    mutable name.
    """

    if not isinstance(prefix, str) or not isinstance(suffix, str):
        raise TypeError("private file prefix and suffix must be strings")
    parent_identity = ensure_plain_directory_tree(Path(directory))
    parent = revalidate_plain_directory(parent_identity)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=prefix,
        suffix=suffix,
    )
    temporary = Path(temporary_name)
    owned_key: tuple[int, int] | None = None
    descriptor_open = True
    try:
        owned_key = _descriptor_file_key(descriptor)
        # close(2) failure leaves descriptor ownership ambiguous (notably
        # after EINTR), and a wrapper can report failure after the OS already
        # closed it.  Relinquish ownership before the only close attempt so an
        # exception path never closes a recycled descriptor number.
        descriptor_open = False
        os.close(descriptor)
        identity, digest = _capture_file(temporary)
        if (
            _file_key(identity) != owned_key
            or identity.size != 0
            or digest != hashlib.sha256(b"").hexdigest()
        ):
            raise OSError("private file changed during reservation")
        revalidate_plain_directory(parent_identity)
        return _PrivateFileClaim(
            path=temporary,
            file_key=owned_key,
            parent_identity=parent_identity,
        )
    except BaseException as primary_error:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError as close_error:
                _add_cleanup_note(
                    primary_error,
                    "private file descriptor cleanup was not completed: "
                    f"{close_error}",
                )
        if owned_key is not None and os.path.lexists(temporary):
            try:
                _retire_file_key(
                    temporary,
                    owned_key,
                    parent_identity=parent_identity,
                )
            except BaseException as cleanup_error:
                _add_cleanup_note(
                    primary_error,
                    "private file reservation cleanup was not completed: "
                    f"{cleanup_error}",
                )
        raise


@contextmanager
def _open_private_file_claim(
    claim: _PrivateFileClaim,
    *,
    truncate: bool = True,
) -> Iterator[BinaryIO]:
    """Open the claimed inode for streaming writes without path truncation.

    The mutable pathname is opened without ``O_TRUNC`` and checked against the
    creation-time key before truncation is allowed.  One process-local claim
    generation permits at most one active writer.  Normal context exit flushes
    and fsyncs the descriptor, then checks the descriptor, pathname and parent
    again.  A caller exception closes the descriptor without claiming that the
    partial payload is complete; the same claim may still be retired.
    """

    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=False)
        state.writer_active = True

    descriptor = -1
    output: BinaryIO | None = None
    primary_error: BaseException | None = None
    try:
        parent = revalidate_plain_directory(claim.parent_identity)
        if claim.path.parent != parent:
            raise OSError(
                errno.EPERM,
                "private file escaped its captured parent",
                str(claim.path),
            )
        if _path_file_key(claim.path) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private file changed before writer open",
                str(claim.path),
            )
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(claim.path, flags)
        if _descriptor_file_key(descriptor) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private file changed while opening writer",
                str(claim.path),
            )
        if _path_file_key(claim.path) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private pathname changed while opening writer",
                str(claim.path),
            )
        revalidate_plain_directory(claim.parent_identity)
        transferred_descriptor = descriptor
        # Ownership passes to the file object at this call boundary.  Clear
        # our raw-fd claim first: if a wrapper reports failure after accepting
        # or closing it, retrying close could hit a recycled descriptor.
        descriptor = -1
        output = os.fdopen(transferred_descriptor, "r+b")
        if truncate:
            output.seek(0)
            output.truncate(0)
        yield output
        output.flush()
        os.fsync(output.fileno())
        if _descriptor_file_key(output.fileno()) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private file descriptor changed while writing",
                str(claim.path),
            )
        if _path_file_key(claim.path) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private pathname changed while writing",
                str(claim.path),
            )
        revalidate_plain_directory(claim.parent_identity)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if output is not None:
                try:
                    output.close()
                except BaseException as close_error:
                    if primary_error is None:
                        raise
                    _add_cleanup_note(
                        primary_error,
                        "private file writer close was not completed: "
                        f"{close_error}",
                    )
            elif descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    if primary_error is None:
                        raise
                    _add_cleanup_note(
                        primary_error,
                        "private file descriptor close was not completed: "
                        f"{close_error}",
                    )
        finally:
            with state.lock:
                state.writer_active = False


def _write_private_file_bytes(
    claim: _PrivateFileClaim,
    payload: bytes,
) -> None:
    """Write one exact byte payload through the claim-bound streaming API."""

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    with _open_private_file_claim(claim, truncate=True) as output:
        written = output.write(payload)
        if written != len(payload):
            raise OSError("private file write was incomplete")


def _rebind_private_file_claim(
    claim: _PrivateFileClaim,
    *,
    expected_sha256: str,
) -> _PrivateFileClaim:
    """Bind a claim to a verified generation atomically installed at its path.

    Path-based writers may replace an initially reserved empty inode.  A caller
    that already knows the exact expected payload digest can adopt that new
    generation without making later cleanup trust the mutable pathname alone.
    """

    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    expected = _validate_sha256(expected_sha256, label="expected_sha256")
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=False)
        revalidate_plain_directory(claim.parent_identity)
        identity, digest = _capture_file(claim.path)
        if digest != expected:
            raise OSError(
                errno.ESTALE,
                "private file digest differs from the expected replacement",
                str(claim.path),
            )
        if identity.path != claim.path:
            raise OSError(
                errno.EPERM,
                "private file escaped its claimed pathname",
                str(identity.path),
            )
        revalidate_plain_directory(claim.parent_identity)
        return _advance_claim_locked(
            claim,
            path=identity.path,
            file_key=_file_key(identity),
        )


def _seal_private_file_claim(
    claim: _PrivateFileClaim,
    *,
    expected_sha256: str,
) -> _SealedPrivateFileClaim:
    """Freeze one completed claim against an exact expected payload digest."""

    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    expected = _validate_sha256(expected_sha256, label="expected_sha256")
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=False)
        identity, digest = _capture_file(claim.path)
        if (
            _file_key(identity) != claim.file_key
            or identity.path != claim.path
            or digest != expected
        ):
            raise OSError(
                errno.ESTALE,
                "private file differs from its expected sealed generation",
                str(claim.path),
            )
        revalidate_plain_directory(claim.parent_identity)
        state.sealed = True
        return _SealedPrivateFileClaim(
            claim=claim,
            identity=identity,
            sha256=digest,
        )


def _relocate_sealed_private_file(
    sealed: _SealedPrivateFileClaim,
    *,
    stem: str,
) -> _SealedPrivateFileClaim:
    """Move a sealed private generation to a fresh no-replace pathname."""

    if not isinstance(sealed, _SealedPrivateFileClaim):
        raise TypeError("_SealedPrivateFileClaim is required")
    if (
        not isinstance(stem, str)
        or not stem
        or stem in {".", ".."}
        or Path(stem).name != stem
        or os.sep in stem
        or (os.altsep is not None and os.altsep in stem)
    ):
        raise ValueError("private relocation stem must be one path component")
    claim = sealed.claim
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=True)
        if not state.sealed:
            raise RuntimeError("private file claim must be sealed before relocation")
        current_identity, current_digest = _capture_file(claim.path)
        if not _same_file(
            sealed.identity,
            current_identity,
            left_sha256=sealed.sha256,
            right_sha256=current_digest,
        ):
            raise OSError(
                errno.ESTALE,
                "sealed private file changed before relocation",
                str(claim.path),
            )
        parent = revalidate_plain_directory(claim.parent_identity)
        if claim.path.parent != parent:
            raise OSError(
                errno.EPERM,
                "sealed private file escaped its parent",
                str(claim.path),
            )

        for _ in range(_PRIVATE_PATH_ATTEMPTS):
            destination = _private_sibling(parent, stem)
            reported_error: BaseException | None = None
            try:
                _rename_noreplace(claim.path, destination)
            except FileExistsError:
                # A wrapper may report FileExists after the native move
                # committed and another writer recreated the source name.
                # Prefer inspecting the fresh destination before retrying;
                # only a still-absent destination proves no move occurred.
                if not os.path.lexists(destination) and os.path.lexists(
                    claim.path
                ):
                    continue
                reported_error = FileExistsError(
                    errno.EEXIST,
                    "relocation destination appeared after a committed move",
                    str(destination),
                )
            except BaseException as exc:
                if os.path.lexists(claim.path) and not os.path.lexists(
                    destination
                ):
                    raise
                reported_error = exc

            try:
                moved_identity, moved_digest = _capture_file(destination)
            except BaseException as capture_error:
                warnings.warn(
                    "relocated private entry could not be verified and was "
                    f"retained for recovery at {destination}: {capture_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                if reported_error is not None:
                    raise reported_error
                raise
            if not _same_file(
                sealed.identity,
                moved_identity,
                left_sha256=sealed.sha256,
                right_sha256=moved_digest,
            ):
                mismatch = OSError(
                    errno.ESTALE,
                    "relocated private entry is not the sealed generation",
                    str(destination),
                )
                warnings.warn(
                    "relocated private entry no longer matches its sealed "
                    f"generation and was retained at {destination}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                raise mismatch from reported_error

            moved_claim = _advance_claim_locked(
                claim,
                path=destination,
                file_key=_file_key(moved_identity),
            )
            return _SealedPrivateFileClaim(
                claim=moved_claim,
                identity=moved_identity,
                sha256=moved_digest,
            )
    raise RuntimeError("could not allocate a private relocation path")


def _install_sealed_private_file(
    sealed: _SealedPrivateFileClaim,
    target: str | os.PathLike[str],
) -> _SealedPrivateFileClaim:
    """Install a sealed private generation at one exact no-replace name.

    This is the fixed-name counterpart to
    :func:`_relocate_sealed_private_file`.  It is intended for an already
    private transaction directory: the caller chooses the final generation
    filename, while this function keeps ownership bound to the same inode and
    digest across move-then-error wrappers and pathname races.
    """

    if not isinstance(sealed, _SealedPrivateFileClaim):
        raise TypeError("_SealedPrivateFileClaim is required")
    claim = sealed.claim
    requested = Path(os.path.abspath(os.fspath(target)))
    if (
        not requested.name
        or requested.name in {".", ".."}
        or requested.name != Path(requested.name).name
    ):
        raise ValueError("private installation target must be one filename")
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=True)
        if not state.sealed:
            raise RuntimeError("private file claim must be sealed before installation")
        parent = revalidate_plain_directory(claim.parent_identity)
        if claim.path.parent != parent:
            raise OSError(
                errno.EPERM,
                "sealed private installation escaped its captured parent",
                str(requested),
            )
        requested = bind_plain_sibling_path(
            requested,
            claim.parent_identity,
            message=(
                "sealed private installation escaped its captured parent"
            ),
        )
        current_identity, current_digest = _capture_file(claim.path)
        if not _same_file(
            sealed.identity,
            current_identity,
            left_sha256=sealed.sha256,
            right_sha256=current_digest,
        ):
            raise OSError(
                errno.ESTALE,
                "sealed private file changed before installation",
                str(claim.path),
            )

        reported_error: BaseException | None = None
        try:
            _rename_noreplace(claim.path, requested)
        except BaseException as exc:
            # A wrapper may raise after the native move committed.  Only the
            # exact target generation can prove success; a still-present
            # source still matching the sealed generation plus an absent or
            # unrelated target proves the move did not happen.  Preserve the
            # native FileExistsError in that ordinary no-replace case instead
            # of mislabelling the pre-existing target as a post-move race.
            if os.path.lexists(claim.path):
                source_still_owned = False
                try:
                    source_identity, source_digest = _capture_file(claim.path)
                    source_still_owned = _same_file(
                        sealed.identity,
                        source_identity,
                        left_sha256=sealed.sha256,
                        right_sha256=source_digest,
                    )
                except BaseException:
                    pass
                if source_still_owned:
                    if not os.path.lexists(requested):
                        raise
                    try:
                        target_identity, target_digest = _capture_file(requested)
                    except BaseException:
                        raise exc
                    if not _same_file(
                        sealed.identity,
                        target_identity,
                        left_sha256=sealed.sha256,
                        right_sha256=target_digest,
                    ):
                        raise exc
            reported_error = exc

        try:
            installed_identity, installed_digest = _capture_file(requested)
        except BaseException as capture_error:
            warnings.warn(
                "installed private entry could not be verified and was "
                f"retained for recovery at {requested}: {capture_error}",
                RuntimeWarning,
                stacklevel=2,
            )
            if reported_error is not None:
                raise reported_error
            raise
        if not _same_file(
            sealed.identity,
            installed_identity,
            left_sha256=sealed.sha256,
            right_sha256=installed_digest,
        ):
            mismatch = OSError(
                errno.ESTALE,
                "installed entry is not the sealed private generation",
                str(requested),
            )
            warnings.warn(
                "installed entry no longer matches its sealed generation "
                f"and was retained at {requested}",
                RuntimeWarning,
                stacklevel=2,
            )
            raise mismatch from reported_error
        revalidate_plain_directory(claim.parent_identity)
        installed_claim = _advance_claim_locked(
            claim,
            path=installed_identity.path,
            file_key=_file_key(installed_identity),
        )
        return _SealedPrivateFileClaim(
            claim=installed_claim,
            identity=installed_identity,
            sha256=installed_digest,
        )


def _close_private_file_claim(claim: _PrivateFileClaim) -> None:
    """Close one live claim without performing any pathname operation."""

    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=True)
        state.closed = True


def _retire_sealed_private_file(
    sealed: _SealedPrivateFileClaim,
    *,
    require_present: bool = False,
) -> None:
    """Retire a sealed generation using its full identity and digest."""

    if not isinstance(sealed, _SealedPrivateFileClaim):
        raise TypeError("_SealedPrivateFileClaim is required")
    claim = sealed.claim
    state = claim._state
    with state.lock:
        _require_live_claim_locked(claim, allow_sealed=True)
        if not state.sealed:
            raise RuntimeError("private file claim must be sealed before retirement")
    primary: BaseException | None = None
    try:
        _retire_owned_file(
            claim.path,
            sealed.identity,
            sealed.sha256,
            parent_identity=claim.parent_identity,
            require_present=require_present,
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            _close_private_file_claim(claim)
        except BaseException as close_error:
            if primary is None:
                raise
            _add_cleanup_note(
                primary,
                "sealed private claim state could not be closed: "
                f"{close_error}",
            )


def _retire_private_file(
    claim: _PrivateFileClaim,
    *,
    allow_additional_links: bool = False,
    require_present: bool = False,
) -> Path | None:
    """Remove a claimed private file, preserving and warning on uncertainty.

    ``None`` means that the claimed pathname was already absent or that the
    identity-bound entry was removed.  A returned path names the entry retained
    for recovery.  In particular, a replacement installed at the original
    pathname is moved to an unpredictable quarantine name and is not treated
    as the claimed generation.
    ``allow_additional_links`` is reserved for a claimed rollback backup after
    it has deliberately restored the public target through a hard link.
    """

    if not isinstance(claim, _PrivateFileClaim):
        raise TypeError("_PrivateFileClaim is required")
    _close_private_file_claim(claim)
    path = claim.path
    if not os.path.lexists(path):
        if require_present:
            raise FileNotFoundError(
                errno.ENOENT,
                "claimed private file disappeared before cleanup",
                str(path),
            )
        return None

    quarantine: Path | None = None
    try:
        parent = revalidate_plain_directory(claim.parent_identity)
        if path.parent != parent:
            raise OSError(
                errno.EPERM,
                "private file escaped its captured parent",
                str(path),
            )
        quarantine = _private_sibling(
            parent,
            f"{path.name.lstrip('.')}.retired",
        )
        _rename_noreplace(path, quarantine)
        if _path_file_key(
            quarantine,
            allow_additional_links=allow_additional_links,
        ) != claim.file_key:
            raise OSError(
                errno.ESTALE,
                "private file changed before cleanup; replacement preserved",
                str(quarantine),
            )
        revalidate_plain_directory(claim.parent_identity)
        quarantine.unlink()
        return None
    except BaseException as cleanup_error:
        preserved = next(
            (
                candidate
                for candidate in (quarantine, path)
                if candidate is not None and os.path.lexists(candidate)
            ),
            None,
        )
        location = (
            f" at {preserved}"
            if preserved is not None
            else " at an unconfirmed pathname"
        )
        try:
            warnings.warn(
                "private file cleanup was not completed; entry preserved"
                f"{location}: {cleanup_error}",
                RuntimeWarning,
                stacklevel=2,
            )
        except BaseException:
            pass
        return preserved


def _withdraw_published_file(
    target: Path,
    expected_key: tuple[int, int],
    *,
    parent_identity: PlainDirectoryIdentity,
) -> tuple[Path | None, str | None]:
    """Withdraw our failed publication by inode, retaining its bad bytes.

    The pathname is deliberately moved *before* it is trusted again.  That
    closes the capture-to-rename exchange window: if a concurrent writer's
    entry was moved instead, it is restored to the public name with another
    no-replace rename.  A raced private destination is never inspected as if
    it were our recovery; the operation simply chooses another random name.
    """

    parent = revalidate_plain_directory(parent_identity)
    for _ in range(_PRIVATE_PATH_ATTEMPTS):
        if not os.path.lexists(target):
            return None, "failed publication disappeared before withdrawal"
        recovery = _private_sibling(parent, f"{target.name}.publish-recovery")
        rename_error: BaseException | None = None
        try:
            _rename_noreplace(target, recovery)
        except FileExistsError:
            # The random destination was occupied after selection.  Native
            # no-replace semantics guarantee that the source was not moved.
            if os.path.lexists(target):
                continue
            return None, (
                "publication target disappeared while a recovery pathname "
                "was concurrently occupied"
            )
        except BaseException as exc:
            rename_error = exc
            if os.path.lexists(target) and not os.path.lexists(recovery):
                return None, f"failed publication withdrawal failed: {exc}"

        try:
            moved_identity, moved_digest = _capture_file(recovery)
        except BaseException as inspect_error:
            # Without an identity proof this private entry must not remain
            # hidden: it could belong to the concurrent writer that exchanged
            # the public name immediately before our rename.
            if not os.path.lexists(target):
                try:
                    _rename_noreplace(recovery, target)
                except BaseException as restore_error:
                    return recovery, (
                        "withdrawal entry could not be identified or restored; "
                        f"it remains at {recovery}: {inspect_error}; "
                        f"restore error: {restore_error}"
                    )
                return None, (
                    "withdrawal entry could not be identified and was restored "
                    f"to the public name: {inspect_error}"
                )
            return recovery, (
                "withdrawal entry could not be identified while the public "
                f"name was occupied; it remains at {recovery}: {inspect_error}"
            )

        if _file_key(moved_identity) == expected_key:
            if rename_error is None:
                return recovery, None
            return recovery, (
                "withdrawal reported an error after moving the failed "
                f"publication: {rename_error}"
            )

        # We moved a concurrent writer's entry, not our failed publication.
        # Restore that exact entry whenever the public name is still vacant.
        if not os.path.lexists(target):
            try:
                _rename_noreplace(recovery, target)
                _capture_moved_file(
                    target,
                    moved_identity,
                    moved_digest,
                    message="concurrent publication changed while being restored",
                )
                detail = (
                    f"; withdrawal also reported: {rename_error}"
                    if rename_error is not None
                    else ""
                )
                return None, (
                    "publication target changed during withdrawal; the "
                    f"concurrent entry was restored{detail}"
                )
            except BaseException as restore_error:
                return recovery, (
                    "publication target changed during withdrawal; its entry "
                    f"could not be restored and remains at {recovery}: "
                    f"{restore_error}"
                )
        return recovery, (
            "publication target changed during withdrawal and its public name "
            f"was occupied; the moved concurrent entry remains at {recovery}"
        )
    return None, "could not allocate an exclusive failed-publication recovery path"


def _write_private_payload(
    parent_identity: PlainDirectoryIdentity,
    target_name: str,
    payload: bytes,
) -> tuple[Path, PlainFileIdentity, str]:
    parent = revalidate_plain_directory(parent_identity)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{target_name}.",
        suffix=".publish.tmp",
    )
    temporary = Path(temporary_name)
    owned_key: tuple[int, int] | None = None
    try:
        owned_key = _descriptor_file_key(descriptor)
        transferred_descriptor = descriptor
        # See the ownership rule in _open_private_file_claim: once fdopen is
        # invoked, this function must never close the raw number again.
        descriptor = -1
        with os.fdopen(transferred_descriptor, "wb") as output:
            written = output.write(payload)
            if written != len(payload):
                raise OSError("publication staging write was incomplete")
            output.flush()
            os.fsync(output.fileno())
            if _descriptor_file_key(output.fileno()) != owned_key:
                raise OSError("publication staging descriptor identity changed")
        identity, digest = _capture_file(temporary)
        if (
            _file_key(identity) != owned_key
            or identity.size != len(payload)
            or digest != hashlib.sha256(payload).hexdigest()
        ):
            raise OSError("publication staging bytes changed after writing")
        revalidate_plain_directory(parent_identity)
        return temporary, identity, digest
    except BaseException as primary_error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if owned_key is not None and os.path.lexists(temporary):
            try:
                _retire_file_key(
                    temporary,
                    owned_key,
                    parent_identity=parent_identity,
                )
            except BaseException as cleanup_error:
                _add_cleanup_note(
                    primary_error,
                    "publication staging cleanup was not completed: "
                    f"{cleanup_error}",
                )
        raise


def _capture_moved_file(
    path: Path,
    expected_identity: PlainFileIdentity,
    expected_digest: str,
    *,
    message: str,
) -> tuple[PlainFileIdentity, str]:
    observed_identity, observed_digest = _capture_file(path)
    if not _same_file(
        expected_identity,
        observed_identity,
        left_sha256=expected_digest,
        right_sha256=observed_digest,
    ):
        raise OSError(errno.ESTALE, message, str(path))
    return observed_identity, observed_digest


def _publish_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    overwrite: bool,
) -> None:
    """Publish bytes without clobbering a concurrent pathname.

    Handled failures restore the captured previous file when its pathname is
    still vacant.  If a racer occupies the destination, that entry is left
    untouched and the old generation remains under a recoverable private
    backup name.  This is handled-failure rollback and atomic namespace
    replacement, not a claim of power-loss durability or cross-path crash
    atomicity.
    """

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    requested = Path(path)
    if not requested.is_absolute():
        requested = requested.absolute()
    if requested.name in {"", ".", ".."}:
        raise ValueError("publication target must name a file")
    parent_identity = ensure_plain_directory_tree(requested.parent)
    parent = revalidate_plain_directory(parent_identity)
    target = parent / requested.name
    initial_target: tuple[PlainFileIdentity, str] | None = None
    if os.path.lexists(target):
        if not overwrite:
            raise FileExistsError(
                errno.EEXIST,
                "publication target already exists",
                str(target),
            )
        initial_target = _capture_file(target)

    temporary, temporary_identity, temporary_digest = _write_private_payload(
        parent_identity,
        target.name,
        payload,
    )
    backup: Path | None = None
    backup_identity: PlainFileIdentity | None = None
    backup_digest: str | None = None
    old_moved = False
    published = False
    primary: BaseException | None = None
    try:
        if initial_target is None:
            if os.path.lexists(target):
                raise FileExistsError(
                    errno.EEXIST,
                    "publication target appeared while staging",
                    str(target),
                )
        else:
            old_identity, old_digest = initial_target
            current_identity, current_digest = _capture_file(target)
            if not _same_file(
                old_identity,
                current_identity,
                left_sha256=old_digest,
                right_sha256=current_digest,
            ):
                raise OSError(
                    errno.ESTALE,
                    "publication target changed while staging",
                    str(target),
                )
            backup = _private_sibling(parent, f"{target.name}.publish-backup")
            revalidate_plain_directory(parent_identity)
            try:
                _rename_noreplace(target, backup)
            except BaseException as rename_error:
                # A filesystem wrapper can report an error after the native
                # rename already committed.  Detect that state by identity so
                # the outer rollback can still restore the captured old file.
                try:
                    moved_identity, moved_digest = _capture_file(backup)
                except BaseException as inspect_error:
                    _add_cleanup_note(
                        rename_error,
                        "publication backup state could not be inspected: "
                        f"{inspect_error}",
                    )
                    # If the source name vanished, conservatively put the
                    # unknown backup entry back.  No-replace restoration cannot
                    # overwrite a concurrent target, and the outer handler will
                    # retain anything that cannot be restored.
                    if not os.path.lexists(target) and os.path.lexists(backup):
                        try:
                            _rename_noreplace(backup, target)
                        except BaseException as restore_error:
                            _add_cleanup_note(
                                rename_error,
                                "publication backup could not be restored after "
                                f"inspection failure: {restore_error}",
                            )
                else:
                    if _same_file(
                        old_identity,
                        moved_identity,
                        left_sha256=old_digest,
                        right_sha256=moved_digest,
                    ):
                        old_moved = True
                        backup_identity = moved_identity
                        backup_digest = moved_digest
                    elif not os.path.lexists(target):
                        try:
                            _rename_noreplace(backup, target)
                            _capture_moved_file(
                                target,
                                moved_identity,
                                moved_digest,
                                message=(
                                    "concurrent target changed while being "
                                    "restored after isolation failure"
                                ),
                            )
                        except BaseException as restore_error:
                            _add_cleanup_note(
                                rename_error,
                                "concurrent target could not be restored after "
                                f"isolation failure: {restore_error}",
                            )
                    else:
                        _add_cleanup_note(
                            rename_error,
                            "an entry moved during failed isolation was retained "
                            f"at {backup}",
                        )
                raise
            old_moved = True
            try:
                backup_identity, backup_digest = _capture_moved_file(
                    backup,
                    old_identity,
                    old_digest,
                    message=(
                        "publication target changed before isolation; "
                        "moved entry preserved"
                    ),
                )
            except BaseException:
                if not os.path.lexists(target):
                    try:
                        _rename_noreplace(backup, target)
                        old_moved = False
                    except BaseException:
                        pass
                raise

        revalidate_plain_directory(parent_identity)
        try:
            _rename_noreplace(temporary, target)
        except BaseException as rename_error:
            # As above, treat a move-then-error as a failed publication that
            # must be withdrawn, not as an untracked success left public.
            try:
                installed_identity, _installed_digest = _capture_file(target)
            except BaseException as inspect_error:
                _add_cleanup_note(
                    rename_error,
                    "publication target state could not be inspected after "
                    f"rename failure: {inspect_error}",
                )
                # A missing source plus an occupied target is conservatively
                # treated as a committed move.  Withdrawal performs its own
                # inode check and leaves/restores any racer it encounters.
                published = (
                    not os.path.lexists(temporary)
                    and os.path.lexists(target)
                )
            else:
                published = (
                    _file_key(installed_identity) == _file_key(temporary_identity)
                )
            raise
        published = True
        _capture_moved_file(
            target,
            temporary_identity,
            temporary_digest,
            message="published file changed during installation",
        )
    except BaseException as exc:
        primary = exc
        if published:
            recovery, withdrawal_note = _withdraw_published_file(
                target,
                _file_key(temporary_identity),
                parent_identity=parent_identity,
            )
            if withdrawal_note is not None:
                _add_cleanup_note(exc, withdrawal_note)
            if recovery is not None:
                _add_cleanup_note(
                    exc,
                    f"failed publication retained for recovery at {recovery}",
                )
            published = False
        if old_moved and backup is not None and os.path.lexists(backup):
            if not os.path.lexists(target):
                try:
                    _rename_noreplace(backup, target)
                    old_moved = False
                except BaseException as rollback_error:
                    _add_cleanup_note(
                        exc,
                        f"previous publication could not be restored: {rollback_error}",
                    )
            else:
                _add_cleanup_note(
                    exc,
                    f"previous publication retained for recovery at {backup}",
                )
        raise
    finally:
        if os.path.lexists(temporary):
            try:
                _retire_owned_file(
                    temporary,
                    temporary_identity,
                    temporary_digest,
                    parent_identity=parent_identity,
                )
            except BaseException as cleanup_error:
                if primary is not None:
                    _add_cleanup_note(
                        primary,
                        f"publication staging cleanup was not completed: {cleanup_error}",
                    )
                else:
                    try:
                        warnings.warn(
                            "publication staging cleanup was not completed: "
                            f"{cleanup_error}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    except BaseException:
                        pass

    if (
        published
        and old_moved
        and backup is not None
        and backup_identity is not None
        and backup_digest is not None
    ):
        try:
            _retire_owned_file(
                backup,
                backup_identity,
                backup_digest,
                parent_identity=parent_identity,
            )
        except BaseException as cleanup_error:
            # Publication already committed; keep the recoverable old file
            # and do not turn cleanup policy into a false render failure.
            try:
                warnings.warn(
                    "previous publication cleanup was not completed: "
                    f"{cleanup_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            except BaseException:
                pass


__all__ = ()
