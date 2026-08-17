"""Private cross-process admission for workers and session scratch.

The pool is intentionally automatic and has no environment or command-line
configuration.  Four permanent per-user slots coordinate independent Tianlai
processes.  A coordinator holds ``reservation.lock`` until the corresponding
result scratch is closed (or a warm child is retired), while the child holds
the independent ``active.lock`` for its whole lifetime.  The two-lock overlap
removes the parent-to-child hand-off gap without relying on inherited handles.

Managed-worker incremental memory and CPU capacity remain worker-only.  Scratch
claims, however, share one allocator and one per-volume ledger with independent
render-session leases.  A long coordinator mapping can therefore never race a
worker reservation by observing the same free bytes in an unrelated ledger.
Session leases do not consume worker slots and expose no user-facing setting.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import threading
import time
from typing import Any, Iterable


_POOL_FORMAT = "tianlai.managed_worker_slots"
_POOL_VERSION = 1
_SESSION_POOL_FORMAT = "tianlai.session_scratch_lease"
_SESSION_POOL_VERSION = 1
_PHYSICAL_SLOT_COUNT = 4
_SESSION_SLOT_COUNT = 32
_SCRATCH_FREE_RESERVE_BYTES = 512 * 1024 * 1024
_MAX_METADATA_BYTES = 16_384
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNER_RE = re.compile(r"^[0-9a-f]{32}$")
_LOCK_RETRY_SECONDS = 0.01
_CHILD_CLAIM_TIMEOUT_SECONDS = 5.0
_LOCAL_ALLOCATOR_LOCK = threading.Lock()
_LIVE_LOCKS_LOCK = threading.Lock()
_LIVE_LOCKS: set[_FileLock] = set()
_LIVE_LOCKS_PID = os.getpid()


class WorkerSlotError(RuntimeError):
    """The private global worker ledger is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class WorkerResourceClaim:
    """One worker plus its render-session and exact raw-stem scratch claim."""

    owner_id: str
    owner_cpu_capacity: int
    worker_memory_bytes: int
    coordinator_memory_bytes: int
    memory_budget_bytes: int
    scratch_bytes: int
    scratch_directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or _OWNER_RE.fullmatch(
            self.owner_id
        ) is None:
            raise ValueError("worker owner_id must be 32 lowercase hex characters")
        _positive_int(
            self.owner_cpu_capacity,
            "worker owner CPU capacity",
            maximum=_PHYSICAL_SLOT_COUNT,
        )
        _positive_int(self.worker_memory_bytes, "worker memory claim")
        _positive_int(self.coordinator_memory_bytes, "coordinator memory claim")
        _positive_int(self.memory_budget_bytes, "worker memory budget")
        if self.worker_memory_bytes + self.coordinator_memory_bytes > (
            self.memory_budget_bytes
        ):
            raise ValueError("worker memory claim exceeds its owner budget")
        _non_negative_int(self.scratch_bytes, "worker scratch claim")
        if not isinstance(self.scratch_directory, Path):
            raise TypeError("worker scratch directory must be a Path")


@dataclass(frozen=True, slots=True)
class SessionScratchClaim:
    """One automatic render-session claim on a local scratch volume."""

    scratch_bytes: int
    scratch_directory: Path

    def __post_init__(self) -> None:
        _non_negative_int(self.scratch_bytes, "session scratch claim")
        if not isinstance(self.scratch_directory, Path):
            raise TypeError("session scratch directory must be a Path")


@dataclass(frozen=True, slots=True)
class _FinalClaim:
    owner_id: str
    owner_cpu_capacity: int
    worker_memory_bytes: int
    coordinator_memory_bytes: int
    memory_budget_bytes: int
    scratch_bytes: int
    scratch_volume_id: str
    scratch_budget_bytes: int

    def document(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "owner_cpu_capacity": self.owner_cpu_capacity,
            "worker_memory_bytes": self.worker_memory_bytes,
            "coordinator_memory_bytes": self.coordinator_memory_bytes,
            "memory_budget_bytes": self.memory_budget_bytes,
            "scratch_bytes": self.scratch_bytes,
            "scratch_volume_id": self.scratch_volume_id,
            "scratch_budget_bytes": self.scratch_budget_bytes,
        }


@dataclass(frozen=True, slots=True)
class _FinalSessionScratchClaim:
    scratch_bytes: int
    scratch_volume_id: str
    scratch_budget_bytes: int

    def document(self) -> dict[str, Any]:
        return {
            "scratch_bytes": self.scratch_bytes,
            "scratch_volume_id": self.scratch_volume_id,
            "scratch_budget_bytes": self.scratch_budget_bytes,
        }


@dataclass(frozen=True, slots=True)
class _ScratchVolumeFacts:
    directory: Path
    volume_id: str
    usable_bytes: int


def _positive_int(value: object, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} exceeds its fixed maximum")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def default_worker_slot_directory() -> Path:
    """Return one stable, per-user pool directory without accepting overrides."""

    if os.name == "nt":
        raw_base = os.environ.get("LOCALAPPDATA")
        if not raw_base:
            # TEMP is not guaranteed to be isolated by Windows user/SID.  A
            # shared fallback would violate the pool's per-user namespace and
            # could let unrelated accounts suppress each other's workers.
            raise WorkerSlotError(
                "per-user Windows worker slot storage is unavailable"
            )
        base = Path(raw_base)
        if not base.is_absolute():
            raise WorkerSlotError(
                "per-user Windows worker slot storage is not absolute"
            )
        return base / "Tianlai" / "managed-worker-slots"
    if not hasattr(os, "getuid"):
        raise WorkerSlotError("per-user POSIX worker slot identity is unavailable")
    uid = os.getuid()
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise WorkerSlotError("per-user POSIX worker slot identity is invalid")

    # XDG_RUNTIME_DIR and tempfile.gettempdir() are process-environment
    # choices.  Two Tianlai processes launched by different shells, services
    # or desktop applications can therefore receive different values while
    # still running as the same user.  Selecting either value would split the
    # admission ledger and allow both processes to believe the same global
    # worker and scratch capacity was free.  /tmp is the fixed local runtime
    # namespace on both Linux and macOS; resolving it also canonicalises the
    # conventional /tmp -> /private/tmp link on macOS.  The UID keeps accounts
    # separate, while _ensure_private_directory retains owner and 0700 checks
    # and fails closed if another account pre-created the predictable name.
    try:
        runtime = Path("/tmp").resolve(strict=True)
        runtime_status = runtime.stat()
    except OSError as exc:
        raise WorkerSlotError(
            "canonical POSIX worker slot storage is unavailable"
        ) from exc
    if not runtime.is_absolute() or not stat.S_ISDIR(runtime_status.st_mode):
        raise WorkerSlotError(
            "canonical POSIX worker slot storage is not a directory"
        )
    return runtime / f"tianlai-managed-worker-slots-{uid}"


def _is_link_or_reparse(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _same_opened_directory(
    opened: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(observed.st_mode)
        and not _is_link_or_reparse(observed)
        and opened.st_dev == observed.st_dev
        and opened.st_ino == observed.st_ino
        and opened.st_ino != 0
    )


def _posix_directory_observations(
    path: Path,
) -> tuple[os.stat_result, Path, os.stat_result, os.stat_result]:
    """Capture lexical/canonical/lexical observations for one opened leaf."""

    lexical_before = os.lstat(path)
    resolved = path.resolve(strict=True)
    canonical = os.lstat(resolved)
    lexical_after = os.lstat(path)
    return lexical_before, resolved, canonical, lexical_after


def _ensure_posix_private_directory(path: Path) -> Path:
    """Bind one private leaf directory to a no-follow descriptor.

    The default pool has a predictable name below the sticky ``/tmp`` root.
    Merely resolving that name would accept an attacker-created leaf symlink.
    Open the lexical leaf itself with ``O_NOFOLLOW | O_DIRECTORY``, apply any
    permission repair through that descriptor, and require both the lexical
    and canonical names to retain the opened identity before returning it.
    Canonicalising after the no-follow open intentionally permits macOS's
    system ``/tmp`` to ``/private/tmp`` alias without permitting a linked leaf.
    """

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise WorkerSlotError(
            "POSIX worker slot no-follow directory access is unavailable"
        )
    flags = os.O_RDONLY | nofollow | directory_flag
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkerSlotError(
            "worker slot directory is not a safe ordinary directory"
        ) from exc
    active_error: BaseException | None = None
    try:
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        uid = os.getuid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_ino == 0
            or opened.st_uid != uid
        ):
            raise WorkerSlotError(
                "worker slot directory has an unsafe identity"
            )
        if opened.st_mode & 0o077:
            try:
                os.fchmod(descriptor, 0o700)
            except OSError as exc:
                raise WorkerSlotError(
                    "worker slot directory is not private"
                ) from exc
            secured = os.fstat(descriptor)
            if (
                not _same_opened_directory(opened, secured)
                or secured.st_uid != uid
                or secured.st_mode & 0o077
            ):
                raise WorkerSlotError(
                    "worker slot directory is not private"
                )
            opened = secured

        try:
            (
                lexical_before,
                resolved,
                canonical,
                lexical_after,
            ) = _posix_directory_observations(path)
        except (OSError, RuntimeError) as exc:
            raise WorkerSlotError(
                "worker slot directory identity changed during validation"
            ) from exc
        if (
            not _same_opened_directory(opened, lexical_before)
            or not _same_opened_directory(opened, canonical)
            or not _same_opened_directory(opened, lexical_after)
        ):
            raise WorkerSlotError(
                "worker slot directory identity changed during validation"
            )
        return resolved
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException as cleanup_error:
            if active_error is None:
                raise
            try:
                active_error.add_note(
                    "worker slot directory descriptor cleanup also failed: "
                    f"{cleanup_error!r}"
                )
            except BaseException:
                pass


def _ensure_private_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(os.fspath(path)))
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        return _ensure_posix_private_directory(requested)

    # Windows has no portable directory-descriptor equivalent.  Reject a
    # linked/reparse leaf before and after canonicalisation, then bind both
    # spellings to the same directory identity.  This preserves ordinary 8.3
    # aliases while preventing a junction leaf from redirecting the pool.
    try:
        lexical_before = requested.lstat()
        resolved = requested.resolve(strict=True)
        canonical = resolved.lstat()
        lexical_after = requested.lstat()
    except (OSError, RuntimeError) as exc:
        raise WorkerSlotError(
            "worker slot directory identity changed during validation"
        ) from exc
    if (
        not _same_opened_directory(lexical_before, lexical_before)
        or not _same_opened_directory(lexical_before, canonical)
        or not _same_opened_directory(lexical_before, lexical_after)
    ):
        raise WorkerSlotError(
            "worker slot directory is not a safe ordinary directory"
        )
    return resolved


def _open_plain_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkerSlotError("worker slot file is not regular")
        if os.name != "nt" and (
            info.st_uid != os.getuid() or info.st_nlink != 1
        ):
            raise WorkerSlotError("worker slot file identity is unsafe")
        if info.st_size < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _try_lock_fd(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class _FileLock:
    __slots__ = ("fd", "path", "_owner_pid", "_released")

    def __init__(self, path: Path, *, blocking: bool) -> None:
        self.path = path
        self.fd = _open_plain_file(path)
        self._owner_pid = os.getpid()
        self._released = False
        try:
            while not _try_lock_fd(self.fd):
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, "worker slot lock is busy")
                time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException:
            os.close(self.fd)
            self._released = True
            raise
        with _LIVE_LOCKS_LOCK:
            _LIVE_LOCKS.add(self)

    def close(self) -> None:
        if self._released:
            return
        if self._owner_pid != os.getpid():
            os.close(self.fd)
        else:
            try:
                _unlock_fd(self.fd)
            finally:
                os.close(self.fd)
        self._released = True
        with _LIVE_LOCKS_LOCK:
            _LIVE_LOCKS.discard(self)

    def _abandon_after_fork(self) -> None:
        if not self._released:
            os.close(self.fd)
            self._released = True

    def __enter__(self) -> _FileLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _reset_locks_after_fork() -> None:
    global _LIVE_LOCKS
    global _LIVE_LOCKS_LOCK
    global _LIVE_LOCKS_PID
    global _LOCAL_ALLOCATOR_LOCK

    locks = tuple(_LIVE_LOCKS)
    for lock in locks:
        lock._abandon_after_fork()
    # A different parent thread may have owned either Python lock at fork.
    # Such ownership can never be released in the single-threaded child, so
    # inherited lock objects must not be reused there.  Closing inherited OS
    # descriptors (without unlocking their shared open-file descriptions)
    # preserves the parent's leases while giving the child fresh bookkeeping.
    _LIVE_LOCKS = set()
    _LIVE_LOCKS_LOCK = threading.Lock()
    _LOCAL_ALLOCATOR_LOCK = threading.Lock()
    _LIVE_LOCKS_PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_after_fork)


def _canonical_metadata(document: dict[str, Any]) -> bytes:
    payload = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not payload or len(payload) > _MAX_METADATA_BYTES:
        raise WorkerSlotError("worker slot metadata exceeds its bound")
    digest = hashlib.sha256(payload).digest()
    return struct.pack(">I", len(payload)) + payload + digest


def _write_metadata(path: Path, document: dict[str, Any]) -> None:
    record = _canonical_metadata(document)
    fd = _open_plain_file(path)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(record)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("worker slot metadata write made no progress")
            view = view[written:]
        os.ftruncate(fd, len(record))
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_metadata(path: Path) -> dict[str, Any]:
    fd = _open_plain_file(path)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        header = os.read(fd, 4)
        if len(header) != 4:
            raise WorkerSlotError("worker slot metadata header is truncated")
        size = struct.unpack(">I", header)[0]
        if size <= 0 or size > _MAX_METADATA_BYTES:
            raise WorkerSlotError("worker slot metadata length is invalid")
        payload = bytearray()
        while len(payload) < size:
            chunk = os.read(fd, size - len(payload))
            if not chunk:
                raise WorkerSlotError("worker slot metadata is truncated")
            payload.extend(chunk)
        digest = os.read(fd, 32)
        if len(digest) != 32 or hashlib.sha256(payload).digest() != digest:
            raise WorkerSlotError("worker slot metadata checksum is invalid")
        if os.read(fd, 1):
            raise WorkerSlotError("worker slot metadata has trailing bytes")
        document = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerSlotError("worker slot metadata is invalid JSON") from exc
    finally:
        os.close(fd)
    if not isinstance(document, dict):
        raise WorkerSlotError("worker slot metadata root is invalid")
    return document


def scratch_volume_identity(directory: Path) -> str:
    """Return a stable local volume identity or fail closed."""

    resolved = directory.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("worker scratch path must be a directory")
    if os.name != "nt":
        return f"posix-dev:{resolved.stat().st_dev}"

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    volume_path = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(
        wintypes.LPCWSTR(str(resolved)), volume_path, len(volume_path)
    ):
        raise WorkerSlotError("worker scratch volume root is unavailable")
    volume_name = ctypes.create_unicode_buffer(32768)
    if kernel32.GetVolumeNameForVolumeMountPointW(
        wintypes.LPCWSTR(volume_path.value), volume_name, len(volume_name)
    ):
        return "windows-guid:" + volume_name.value.casefold()
    # Drive roots and serials can alias the same mounted volume through
    # different paths.  Without the canonical Volume GUID, cross-process
    # scratch claims cannot be placed in a trustworthy bucket.
    raise WorkerSlotError("worker scratch volume GUID is unavailable")


def _same_directory_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    """Compare one resolved directory across an admission-time probe."""

    return (
        stat.S_ISDIR(first.st_mode)
        and stat.S_ISDIR(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_ino != 0
    )


def _scratch_volume_facts(directory: Path) -> _ScratchVolumeFacts:
    """Bind free-space evidence to one stable resolved directory and volume."""

    resolved = directory.resolve(strict=True)
    opened = resolved.lstat()
    if not stat.S_ISDIR(opened.st_mode):
        raise WorkerSlotError("scratch path must resolve to a plain directory")
    volume_id = scratch_volume_identity(resolved)
    usable = max(
        0,
        shutil.disk_usage(resolved).free - _SCRATCH_FREE_RESERVE_BYTES,
    )
    final = resolved.lstat()
    final_volume_id = scratch_volume_identity(resolved)
    if (
        not _same_directory_identity(opened, final)
        or final_volume_id != volume_id
    ):
        raise WorkerSlotError("scratch directory identity changed during admission")
    return _ScratchVolumeFacts(resolved, volume_id, usable)


def _metadata_document(
    *, token: str, parent_pid: int, claim: _FinalClaim, active_token: str
) -> dict[str, Any]:
    return {
        "format": _POOL_FORMAT,
        "version": _POOL_VERSION,
        "token": token,
        "active_token": active_token,
        "parent_pid": parent_pid,
        "claim": claim.document(),
    }


def _parse_metadata(document: dict[str, Any]) -> tuple[str, int, _FinalClaim, str]:
    if set(document) != {
        "format", "version", "token", "active_token", "parent_pid", "claim"
    }:
        raise WorkerSlotError("worker slot metadata fields are invalid")
    token = document["token"]
    active_token = document["active_token"]
    parent_pid = document["parent_pid"]
    raw = document["claim"]
    if (
        document["format"] != _POOL_FORMAT
        or document["version"] != _POOL_VERSION
        or not isinstance(token, str)
        or _TOKEN_RE.fullmatch(token) is None
        or not isinstance(active_token, str)
        or (active_token and active_token != token)
        or isinstance(parent_pid, bool)
        or not isinstance(parent_pid, int)
        or parent_pid <= 0
        or not isinstance(raw, dict)
    ):
        raise WorkerSlotError("worker slot metadata identity is invalid")
    expected = {
        "owner_id", "owner_cpu_capacity", "worker_memory_bytes",
        "coordinator_memory_bytes", "memory_budget_bytes", "scratch_bytes",
        "scratch_volume_id", "scratch_budget_bytes",
    }
    if set(raw) != expected:
        raise WorkerSlotError("worker slot claim fields are invalid")
    raw_owner_id = raw["owner_id"]
    raw_volume_id = raw["scratch_volume_id"]
    if type(raw_owner_id) is not str or type(raw_volume_id) is not str:
        raise WorkerSlotError("worker slot claim strings are invalid")
    try:
        claim = _FinalClaim(
            owner_id=raw_owner_id,
            owner_cpu_capacity=_positive_int(
                raw["owner_cpu_capacity"], "ledger CPU capacity",
                maximum=_PHYSICAL_SLOT_COUNT,
            ),
            worker_memory_bytes=_positive_int(
                raw["worker_memory_bytes"], "ledger worker memory"
            ),
            coordinator_memory_bytes=_positive_int(
                raw["coordinator_memory_bytes"], "ledger coordinator memory"
            ),
            memory_budget_bytes=_positive_int(
                raw["memory_budget_bytes"], "ledger memory budget"
            ),
            scratch_bytes=_non_negative_int(
                raw["scratch_bytes"], "ledger scratch"
            ),
            scratch_volume_id=raw_volume_id,
            scratch_budget_bytes=_positive_int(
                raw["scratch_budget_bytes"], "ledger scratch budget"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise WorkerSlotError("worker slot claim values are invalid") from exc
    if (
        _OWNER_RE.fullmatch(claim.owner_id) is None
        or not claim.scratch_volume_id
        or len(claim.scratch_volume_id) > 1024
        or claim.worker_memory_bytes + claim.coordinator_memory_bytes
        > claim.memory_budget_bytes
        or claim.scratch_bytes > claim.scratch_budget_bytes
    ):
        raise WorkerSlotError("worker slot claim bounds are invalid")
    return token, parent_pid, claim, active_token


def _session_metadata_document(
    *,
    token: str,
    owner_pid: int,
    claim: _FinalSessionScratchClaim,
) -> dict[str, Any]:
    return {
        "format": _SESSION_POOL_FORMAT,
        "version": _SESSION_POOL_VERSION,
        "token": token,
        "owner_pid": owner_pid,
        "claim": claim.document(),
    }


def _parse_session_metadata(
    document: dict[str, Any],
) -> tuple[str, int, _FinalSessionScratchClaim]:
    if set(document) != {"format", "version", "token", "owner_pid", "claim"}:
        raise WorkerSlotError("session scratch metadata fields are invalid")
    token = document["token"]
    owner_pid = document["owner_pid"]
    raw = document["claim"]
    if (
        document["format"] != _SESSION_POOL_FORMAT
        or document["version"] != _SESSION_POOL_VERSION
        or type(token) is not str
        or _TOKEN_RE.fullmatch(token) is None
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not isinstance(raw, dict)
        or set(raw)
        != {"scratch_bytes", "scratch_volume_id", "scratch_budget_bytes"}
    ):
        raise WorkerSlotError("session scratch metadata identity is invalid")
    raw_volume_id = raw["scratch_volume_id"]
    if type(raw_volume_id) is not str:
        raise WorkerSlotError("session scratch volume identity is invalid")
    try:
        claim = _FinalSessionScratchClaim(
            scratch_bytes=_non_negative_int(
                raw["scratch_bytes"], "session ledger scratch"
            ),
            scratch_volume_id=raw_volume_id,
            scratch_budget_bytes=_non_negative_int(
                raw["scratch_budget_bytes"], "session ledger scratch budget"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise WorkerSlotError("session scratch claim values are invalid") from exc
    if (
        not claim.scratch_volume_id
        or len(claim.scratch_volume_id) > 1024
        or claim.scratch_bytes > claim.scratch_budget_bytes
    ):
        raise WorkerSlotError("session scratch claim bounds are invalid")
    return token, owner_pid, claim


@dataclass(frozen=True, slots=True)
class ChildSlotSpec:
    directory: Path
    slot_index: int
    token: str
    parent_pid: int


class ReservedWorkerSlot:
    """One parent reservation retained through result or warm-child lifetime."""

    __slots__ = ("_pool", "index", "token", "claim", "_lock", "_closed")

    def __init__(
        self, pool: WorkerSlotPool, index: int, token: str,
        claim: _FinalClaim, lock: _FileLock,
    ) -> None:
        self._pool = pool
        self.index = index
        self.token = token
        self.claim = claim
        self._lock = lock
        self._closed = False

    @property
    def child_spec(self) -> ChildSlotSpec:
        if self._closed:
            raise WorkerSlotError("worker slot reservation is closed")
        return ChildSlotSpec(
            self._pool.directory, self.index, self.token, os.getpid()
        )

    def wait_for_active(self, process: Any, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        paths = self._pool._paths(self.index)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkerSlotError("managed worker exited before slot ACK")
            probe: _FileLock | None = None
            try:
                probe = _FileLock(paths.active, blocking=False)
            except BlockingIOError:
                try:
                    token, parent_pid, claim, active = _parse_metadata(
                        _read_metadata(paths.metadata)
                    )
                    if (
                        token == self.token
                        and active == token
                        and parent_pid == os.getpid()
                        and claim == self.claim
                    ):
                        return
                except WorkerSlotError:
                    pass
            finally:
                if probe is not None:
                    probe.close()
            time.sleep(_LOCK_RETRY_SECONDS)
        raise WorkerSlotError("managed worker slot ACK timed out")

    def close(self) -> None:
        if not self._closed:
            self._lock.close()
            self._closed = True

    def __enter__(self) -> ReservedWorkerSlot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class WorkerSlotReservation:
    """All-or-nothing batch reservation, consumed one slot at a time."""

    __slots__ = ("_slots", "_owner_pid", "_closed")

    def __init__(self, slots: list[ReservedWorkerSlot]) -> None:
        self._slots = slots
        self._owner_pid = os.getpid()
        self._closed = False

    def take(self) -> ReservedWorkerSlot:
        if self._closed or self._owner_pid != os.getpid() or not self._slots:
            raise WorkerSlotError("worker batch reservation is unavailable")
        return self._slots.pop(0)

    def close(self) -> None:
        if self._closed:
            return
        for slot in self._slots:
            slot.close()
        self._slots.clear()
        self._closed = True

    def __enter__(self) -> WorkerSlotReservation:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SessionScratchLease:
    """One process-owned scratch lease that consumes no worker capacity.

    Callers must create scratch through ``scratch_directory`` (the resolved
    directory whose identity was admitted) and retain the lease until every
    claimed file and mapping has been closed.
    """

    __slots__ = (
        "_pool",
        "index",
        "token",
        "claim",
        "scratch_directory",
        "_lock",
        "_owner_pid",
        "_closed",
    )

    def __init__(
        self,
        pool: WorkerSlotPool,
        index: int,
        token: str,
        claim: _FinalSessionScratchClaim,
        scratch_directory: Path,
        lock: _FileLock,
    ) -> None:
        self._pool = pool
        self.index = index
        self.token = token
        self.claim = claim
        self.scratch_directory = scratch_directory
        self._lock = lock
        self._owner_pid = os.getpid()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._owner_pid != os.getpid()

    def close(self) -> None:
        if self._closed:
            return
        # _FileLock distinguishes the post-fork child from the owner.  Closing
        # an inherited lease there only abandons the duplicate descriptor and
        # never unlocks the parent's shared open-file description.
        self._lock.close()
        self._closed = True

    def __enter__(self) -> SessionScratchLease:
        if self.closed:
            raise WorkerSlotError("session scratch lease is unavailable")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


@dataclass(frozen=True, slots=True)
class _SlotPaths:
    reservation: Path
    active: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class _SessionSlotPaths:
    lease: Path
    metadata: Path


class WorkerSlotPool:
    """One per-user, fail-closed worker and scratch admission ledger."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = _ensure_private_directory(
            default_worker_slot_directory() if directory is None else directory
        )
        self._allocator_path = self.directory / "allocator.lock"
        self._session_directory = _ensure_private_directory(
            self.directory / "session-scratch"
        )
        _open_and_close(self._allocator_path)
        for index in range(_PHYSICAL_SLOT_COUNT):
            paths = self._paths(index)
            paths.reservation.parent.mkdir(mode=0o700, exist_ok=True)
            for path in (paths.reservation, paths.active, paths.metadata):
                _open_and_close(path)
        # Session files are deliberately lazy.  Every worker child constructs
        # this pool during its early slot ACK, while only coordinators probe
        # session scratch.  Pre-creating 64 files here made an unrelated child
        # pay that filesystem cost.  _FileLock and _write_metadata both create
        # their exact fixed names under the already-private directory while
        # the allocator is held, so admission remains race-safe and retains
        # all 32 session slots without constructor I/O.

    def _paths(self, index: int) -> _SlotPaths:
        if isinstance(index, bool) or not isinstance(index, int) or not (
            0 <= index < _PHYSICAL_SLOT_COUNT
        ):
            raise ValueError("worker slot index is invalid")
        root = self.directory / f"slot-{index}"
        return _SlotPaths(
            root / "reservation.lock", root / "active.lock", root / "metadata"
        )

    def _session_paths(self, index: int) -> _SessionSlotPaths:
        if isinstance(index, bool) or not isinstance(index, int) or not (
            0 <= index < _SESSION_SLOT_COUNT
        ):
            raise ValueError("session scratch slot index is invalid")
        return _SessionSlotPaths(
            self._session_directory / f"lease-{index}.lock",
            self._session_directory / f"lease-{index}.metadata",
        )

    def _probe_session_slots(
        self,
    ) -> tuple[
        list[_FinalSessionScratchClaim],
        list[tuple[int, _FileLock]],
    ]:
        """Return active claims and retain locks for every free session slot."""

        active: list[_FinalSessionScratchClaim] = []
        free: list[tuple[int, _FileLock]] = []
        try:
            for index in range(_SESSION_SLOT_COUNT):
                paths = self._session_paths(index)
                try:
                    probe = _FileLock(paths.lease, blocking=False)
                except BlockingIOError:
                    _token, _owner_pid, claim = _parse_session_metadata(
                        _read_metadata(paths.metadata)
                    )
                    active.append(claim)
                else:
                    try:
                        free.append((index, probe))
                    except BaseException:
                        probe.close()
                        raise
            return active, free
        except BaseException:
            for _index, probe in free:
                probe.close()
            raise

    def _probe_active_worker_claims(self) -> list[_FinalClaim]:
        """Read every busy worker slot while the shared allocator is held."""

        active: list[_FinalClaim] = []
        for index in range(_PHYSICAL_SLOT_COUNT):
            paths = self._paths(index)
            reservation_probe: _FileLock | None = None
            active_probe: _FileLock | None = None
            reservation_busy = active_busy = False
            try:
                reservation_probe = _FileLock(
                    paths.reservation,
                    blocking=False,
                )
            except BlockingIOError:
                reservation_busy = True
            try:
                active_probe = _FileLock(paths.active, blocking=False)
            except BlockingIOError:
                active_busy = True
            except BaseException:
                if reservation_probe is not None:
                    reservation_probe.close()
                raise
            if not reservation_busy and not active_busy:
                assert reservation_probe is not None and active_probe is not None
                active_probe.close()
                reservation_probe.close()
                continue
            if reservation_probe is not None:
                reservation_probe.close()
            if active_probe is not None:
                active_probe.close()
            _token, _pid, claim, _active = _parse_metadata(
                _read_metadata(paths.metadata)
            )
            active.append(claim)
        return active

    def reserve_exact(
        self, claims: Iterable[WorkerResourceClaim]
    ) -> WorkerSlotReservation | None:
        requested = tuple(claims)
        if not requested:
            raise ValueError("worker slot batch must not be empty")
        if len(requested) > _PHYSICAL_SLOT_COUNT:
            return None
        if any(type(claim) is not WorkerResourceClaim for claim in requested):
            raise TypeError("worker slot claims must be exact WorkerResourceClaim values")

        if not _LOCAL_ALLOCATOR_LOCK.acquire(blocking=False):
            return None
        allocator: _FileLock | None = None
        free: list[tuple[int, _FileLock, _FileLock]] = []
        free_sessions: list[tuple[int, _FileLock]] = []
        try:
            try:
                allocator = _FileLock(self._allocator_path, blocking=False)
            except BlockingIOError:
                return None
            active_claims: list[_FinalClaim] = []
            for index in range(_PHYSICAL_SLOT_COUNT):
                paths = self._paths(index)
                reservation_probe: _FileLock | None = None
                active_probe: _FileLock | None = None
                reservation_busy = active_busy = False
                try:
                    reservation_probe = _FileLock(paths.reservation, blocking=False)
                except BlockingIOError:
                    reservation_busy = True
                try:
                    active_probe = _FileLock(paths.active, blocking=False)
                except BlockingIOError:
                    active_busy = True
                except BaseException:
                    if reservation_probe is not None:
                        reservation_probe.close()
                    raise
                if not reservation_busy and not active_busy:
                    assert reservation_probe is not None and active_probe is not None
                    try:
                        free.append((index, reservation_probe, active_probe))
                    except BaseException:
                        active_probe.close()
                        reservation_probe.close()
                        raise
                    continue
                if reservation_probe is not None:
                    reservation_probe.close()
                if active_probe is not None:
                    active_probe.close()
                try:
                    _token, _pid, claim, _active = _parse_metadata(
                        _read_metadata(paths.metadata)
                    )
                except WorkerSlotError:
                    return None
                active_claims.append(claim)

            if len(free) < len(requested):
                return None
            finalized = self._finalize_claims(requested)
            active_sessions, free_sessions = self._probe_session_slots()
            if not _claims_fit(
                (*active_claims, *finalized),
                active_sessions,
            ):
                return None
            selected = free[: len(finalized)]
            unselected = free[len(finalized) :]
            for _index, reservation_probe, active_probe in unselected:
                active_probe.close()
                reservation_probe.close()
            free = selected
            slots: list[ReservedWorkerSlot] = []
            for (index, reservation_lock, active_probe), claim in zip(
                selected, finalized, strict=True
            ):
                token = os.urandom(16).hex()
                _write_metadata(
                    self._paths(index).metadata,
                    _metadata_document(
                        token=token,
                        parent_pid=os.getpid(),
                        claim=claim,
                        active_token="",
                    ),
                )
                # Keep the active probe locked until the new token is durable:
                # a late child from a crashed former parent can only run after
                # it observes (and rejects) this generation.
                active_probe.close()
                slots.append(
                    ReservedWorkerSlot(self, index, token, claim, reservation_lock)
                )
            free.clear()
            return WorkerSlotReservation(slots)
        except (OSError, ValueError, WorkerSlotError):
            return None
        finally:
            for _index, reservation_probe, active_probe in free:
                active_probe.close()
                reservation_probe.close()
            for _index, lease_probe in free_sessions:
                lease_probe.close()
            if allocator is not None:
                allocator.close()
            _LOCAL_ALLOCATOR_LOCK.release()

    def reserve_session_scratch(
        self,
        claim: SessionScratchClaim,
    ) -> SessionScratchLease | None:
        """Non-blockingly reserve scratch without consuming a worker slot.

        ``None`` is the complete fail-closed fallback for busy or unverifiable
        host state.  A successful caller must use the lease's resolved scratch
        directory, not re-resolve the original request path independently.
        """

        if type(claim) is not SessionScratchClaim:
            raise TypeError("session scratch claim must be an exact value")
        if not _LOCAL_ALLOCATOR_LOCK.acquire(blocking=False):
            return None
        allocator: _FileLock | None = None
        free_sessions: list[tuple[int, _FileLock]] = []
        selected_lock: _FileLock | None = None
        try:
            try:
                allocator = _FileLock(self._allocator_path, blocking=False)
            except BlockingIOError:
                return None
            active_workers = self._probe_active_worker_claims()
            active_sessions, free_sessions = self._probe_session_slots()
            if not free_sessions:
                return None
            finalized, resolved_directory = self._finalize_session_claim(claim)
            if not _scratch_claims_fit(
                active_workers,
                (*active_sessions, finalized),
            ):
                return None

            selected_index, selected_lock = free_sessions.pop(0)
            token = os.urandom(16).hex()
            _write_metadata(
                self._session_paths(selected_index).metadata,
                _session_metadata_document(
                    token=token,
                    owner_pid=os.getpid(),
                    claim=finalized,
                ),
            )
            lease = SessionScratchLease(
                self,
                selected_index,
                token,
                finalized,
                resolved_directory,
                selected_lock,
            )
            selected_lock = None
            return lease
        except (OSError, ValueError, WorkerSlotError):
            return None
        finally:
            if selected_lock is not None:
                selected_lock.close()
            for _index, lease_probe in free_sessions:
                lease_probe.close()
            if allocator is not None:
                allocator.close()
            _LOCAL_ALLOCATOR_LOCK.release()

    def _finalize_claims(
        self, claims: tuple[WorkerResourceClaim, ...]
    ) -> tuple[_FinalClaim, ...]:
        volume_facts: dict[str, tuple[Path, int]] = {}
        identities: list[str] = []
        for claim in claims:
            facts = _scratch_volume_facts(claim.scratch_directory)
            directory = facts.directory
            identity = facts.volume_id
            usable = facts.usable_bytes
            previous = volume_facts.get(identity)
            if previous is None or usable < previous[1]:
                volume_facts[identity] = (directory, usable)
            identities.append(identity)
        finalized = tuple(
            _FinalClaim(
                owner_id=claim.owner_id,
                owner_cpu_capacity=claim.owner_cpu_capacity,
                worker_memory_bytes=claim.worker_memory_bytes,
                coordinator_memory_bytes=claim.coordinator_memory_bytes,
                memory_budget_bytes=claim.memory_budget_bytes,
                scratch_bytes=claim.scratch_bytes,
                scratch_volume_id=identity,
                scratch_budget_bytes=volume_facts[identity][1],
            )
            for claim, identity in zip(claims, identities, strict=True)
        )
        if any(item.scratch_budget_bytes <= 0 for item in finalized):
            raise WorkerSlotError("worker scratch usable budget is exhausted")
        return finalized

    def _finalize_session_claim(
        self,
        claim: SessionScratchClaim,
    ) -> tuple[_FinalSessionScratchClaim, Path]:
        facts = _scratch_volume_facts(claim.scratch_directory)
        finalized = _FinalSessionScratchClaim(
            scratch_bytes=claim.scratch_bytes,
            scratch_volume_id=facts.volume_id,
            scratch_budget_bytes=facts.usable_bytes,
        )
        if finalized.scratch_bytes > finalized.scratch_budget_bytes:
            raise WorkerSlotError("session scratch claim exceeds usable space")
        return finalized, facts.directory


def _open_and_close(path: Path) -> None:
    fd = _open_plain_file(path)
    os.close(fd)


def _scratch_claims_fit(
    worker_claims: Iterable[_FinalClaim],
    session_claims: Iterable[_FinalSessionScratchClaim],
) -> bool:
    scratch_by_volume: dict[str, int] = {}
    scratch_budget_by_volume: dict[str, int] = {}
    for claim in (*tuple(worker_claims), *tuple(session_claims)):
        scratch_by_volume[claim.scratch_volume_id] = (
            scratch_by_volume.get(claim.scratch_volume_id, 0)
            + claim.scratch_bytes
        )
        scratch_budget_by_volume[claim.scratch_volume_id] = min(
            scratch_budget_by_volume.get(
                claim.scratch_volume_id,
                claim.scratch_budget_bytes,
            ),
            claim.scratch_budget_bytes,
        )
    return all(
        scratch_bytes <= scratch_budget_by_volume[volume]
        for volume, scratch_bytes in scratch_by_volume.items()
    )


def _claims_fit(
    claims: tuple[_FinalClaim, ...],
    session_claims: Iterable[_FinalSessionScratchClaim] = (),
) -> bool:
    if not claims:
        return _scratch_claims_fit((), session_claims)
    capacity = min(claim.owner_cpu_capacity for claim in claims)
    if len(claims) > capacity:
        return False
    owners: dict[str, tuple[int, int]] = {}
    worker_memory = 0
    memory_budget = min(claim.memory_budget_bytes for claim in claims)
    for claim in claims:
        worker_memory += claim.worker_memory_bytes
        owner = owners.get(claim.owner_id)
        owner_facts = (claim.coordinator_memory_bytes, claim.memory_budget_bytes)
        if owner is not None and owner != owner_facts:
            return False
        owners[claim.owner_id] = owner_facts
    total_memory = worker_memory + sum(value[0] for value in owners.values())
    if total_memory > memory_budget:
        return False
    return _scratch_claims_fit(claims, session_claims)


class ActiveWorkerSlot:
    """Child-owned active lock; process exit also releases it automatically."""

    __slots__ = ("_lock", "_closed")

    def __init__(self, lock: _FileLock) -> None:
        self._lock = lock
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._lock.close()
            self._closed = True

    def __enter__(self) -> ActiveWorkerSlot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def claim_reserved_worker_slot(
    spec: ChildSlotSpec,
    *,
    timeout: float = _CHILD_CLAIM_TIMEOUT_SECONDS,
) -> ActiveWorkerSlot:
    """Claim a parent's reserved slot using an independent child descriptor."""

    if type(spec) is not ChildSlotSpec:
        raise TypeError("child worker slot spec must be exact")
    if _TOKEN_RE.fullmatch(spec.token) is None:
        raise ValueError("child worker slot token is invalid")
    if spec.parent_pid <= 0:
        raise ValueError("child worker parent PID is invalid")
    pool = WorkerSlotPool(spec.directory)
    paths = pool._paths(spec.slot_index)
    deadline = time.monotonic() + timeout
    lock: _FileLock | None = None
    while time.monotonic() < deadline:
        if not _parent_is_alive(spec.parent_pid):
            raise WorkerSlotError("managed worker parent exited before slot claim")
        try:
            lock = _FileLock(paths.active, blocking=False)
            break
        except BlockingIOError:
            # A former parent's late child may briefly own active.lock.  The
            # durable generation token makes it reject this reservation.
            token, parent_pid, _claim, _active = _parse_metadata(
                _read_metadata(paths.metadata)
            )
            if token != spec.token or parent_pid != spec.parent_pid:
                raise WorkerSlotError("managed worker slot generation changed")
            time.sleep(_LOCK_RETRY_SECONDS)
    if lock is None:
        raise WorkerSlotError("managed worker active slot timed out")
    try:
        # Best-effort reject a delayed child after its parent releases the
        # reservation.  This probe cannot identify which process owns a busy
        # lock (an allocator scan may briefly own it), so the durable token and
        # active lock remain the authoritative safety boundary: even in that
        # narrow race every scanner still accounts this child's full claim.
        reservation_probe: _FileLock | None = None
        try:
            reservation_probe = _FileLock(paths.reservation, blocking=False)
        except BlockingIOError:
            pass
        else:
            reservation_probe.close()
            raise WorkerSlotError(
                "managed worker parent reservation is no longer active"
            )
        token, parent_pid, claim, _active = _parse_metadata(
            _read_metadata(paths.metadata)
        )
        if token != spec.token or parent_pid != spec.parent_pid:
            raise WorkerSlotError("managed worker slot reservation changed")
        _write_metadata(
            paths.metadata,
            _metadata_document(
                token=token,
                parent_pid=parent_pid,
                claim=claim,
                active_token=token,
            ),
        )
        return ActiveWorkerSlot(lock)
    except BaseException:
        lock.close()
        raise


def _parent_is_alive(parent_pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    # A worker is always launched directly by its coordinator.  Requiring the
    # kernel-reported parent relationship also rejects PID reuse after an
    # old coordinator exits; mere ``kill(pid, 0)`` cannot prove identity.
    return os.getppid() == parent_pid


__all__ = [
    "ActiveWorkerSlot",
    "ChildSlotSpec",
    "ReservedWorkerSlot",
    "SessionScratchClaim",
    "SessionScratchLease",
    "WorkerResourceClaim",
    "WorkerSlotError",
    "WorkerSlotPool",
    "WorkerSlotReservation",
    "claim_reserved_worker_slot",
    "default_worker_slot_directory",
    "scratch_volume_identity",
]
