"""Cross-process ownership lock for one render output directory.

The lock is a retained sidecar file in the output directory's parent, never
inside the output directory itself.  File existence is not ownership: the
operating-system byte-range lock is authoritative, so a process crash releases
ownership automatically and the harmless sidecar may remain on disk.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
from typing import Iterator

from .canonical_json import canonical_json_bytes


_LOCK_FORMAT = "tianlai.render_lock"
_LOCK_VERSION = 1
_LOCK_FILE_PREFIX = ".tianlai-render-"
_LOCK_FILE_SUFFIX = ".lock"
_LOCK_DIGEST_HEX = 24
_LOCKED_BYTE_COUNT = 1


class RenderLockError(RuntimeError):
    """Raised when another process already owns a render directory."""

    def __init__(self, output_directory: Path, lock_path: Path) -> None:
        self.output_directory = output_directory
        self.lock_path = lock_path
        super().__init__(
            "渲染目录正在被另一个进程使用: "
            f"{output_directory}。请等待现有渲染完成后重试。"
            f"锁文件 {lock_path} 可以保留；渲染进行时请勿删除它。"
        )


@dataclass(frozen=True, slots=True)
class RenderLock:
    """Information about the lock currently owned by this context."""

    output_directory: Path
    lock_path: Path
    owner_pid: int


class _LockBusy(Exception):
    pass


def _resolved_output_directory(
    output_directory: str | os.PathLike[str],
) -> Path:
    return Path(output_directory).resolve(strict=False)


def _lock_identity(output_directory: Path) -> str:
    # On Windows normcase folds case and slash variants which name the same
    # directory.  It is deliberately a no-op on case-sensitive POSIX systems.
    return os.path.normcase(str(output_directory))


def render_lock_path(
    output_directory: str | os.PathLike[str],
) -> Path:
    """Return the stable sidecar path used to lock ``output_directory``."""

    resolved = _resolved_output_directory(output_directory)
    identity = _lock_identity(resolved).encode("utf-8", errors="surrogatepass")
    # Ninety-six bits keeps accidental collisions negligible without making
    # already-deep Windows output paths needlessly approach MAX_PATH.
    digest = hashlib.sha256(identity).hexdigest()[:_LOCK_DIGEST_HEX]
    parent = resolved.parent
    if parent == resolved:
        # A filesystem root has no location outside itself for a sibling lock,
        # and publishing render artifacts directly into a root is unsafe.
        raise ValueError("渲染输出目录不能是文件系统根目录")
    return parent / f"{_LOCK_FILE_PREFIX}{digest}{_LOCK_FILE_SUFFIX}"


def _open_lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            # msvcrt.locking operates on a byte range starting at the current
            # file position.  Materialize byte zero before trying to lock it.
            handle.write(b"\n")
            handle.flush()
        handle.seek(0)
        return handle
    except BaseException:
        handle.close()
        raise


def _try_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(
                handle.fileno(),
                msvcrt.LK_NBLCK,
                _LOCKED_BYTE_COUNT,
            )
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise _LockBusy from exc
            raise
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise _LockBusy from exc
        raise


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(
            handle.fileno(),
            msvcrt.LK_UNLCK,
            _LOCKED_BYTE_COUNT,
        )
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_owner_metadata(
    handle,
    output_directory: Path,
) -> None:
    payload = canonical_json_bytes(
        {
            "format": _LOCK_FORMAT,
            "version": _LOCK_VERSION,
            "pid": os.getpid(),
            "output_directory": str(output_directory),
        }
    ) + b"\n"
    # Byte zero remains the stable locked range; human-readable owner metadata
    # starts after it and is replaced only while this process owns the lock.
    handle.seek(_LOCKED_BYTE_COUNT)
    handle.truncate(_LOCKED_BYTE_COUNT)
    handle.write(payload)
    handle.flush()


@contextmanager
def acquire_render_lock(
    output_directory: str | os.PathLike[str],
) -> Iterator[RenderLock]:
    """Own one render target until the context exits.

    Acquisition is non-blocking.  A concurrent owner raises
    :class:`RenderLockError` immediately; normal exit, an exception in the
    context, or process termination releases the operating-system lock.
    """

    resolved = _resolved_output_directory(output_directory)
    lock_path = render_lock_path(resolved)
    handle = _open_lock_file(lock_path)
    locked = False
    try:
        try:
            _try_lock(handle)
        except _LockBusy as exc:
            raise RenderLockError(resolved, lock_path) from exc
        locked = True
        _write_owner_metadata(handle, resolved)
        yield RenderLock(
            output_directory=resolved,
            lock_path=lock_path,
            owner_pid=os.getpid(),
        )
    finally:
        try:
            if locked:
                _unlock(handle)
        finally:
            handle.close()


__all__ = (
    "RenderLock",
    "RenderLockError",
    "acquire_render_lock",
    "render_lock_path",
)
