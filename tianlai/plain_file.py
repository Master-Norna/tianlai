"""Descriptor-bound reads for immutable, single-link local artifacts.

Path validation followed by ``Path.read_bytes()`` leaves a race in which the
name can be replaced between the check and the open.  Workflow manifests and
their evidence bindings need a slightly stronger primitive: open once, compare
the descriptor with the path, read through that descriptor, then compare both
again before accepting the bytes or digest.

This module deliberately does not create, replace, or remove files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat


_REPARSE_POINT = 0x400
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PlainFileIdentity:
    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def _is_reparse(value: os.stat_result) -> bool:
    return bool(
        getattr(value, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _require_plain_status(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
        or value.st_nlink != 1
    ):
        raise OSError("path is not a plain single-link file")


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
        and int(left.st_size) == int(right.st_size)
        and int(left.st_mtime_ns) == int(right.st_mtime_ns)
        and int(left.st_ctime_ns) == int(right.st_ctime_ns)
    )


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(os.path.abspath(os.fspath(value)))
    except (OSError, TypeError, ValueError) as exc:
        raise OSError("invalid file path") from exc
    if not path.is_absolute():
        raise OSError("file path must be absolute")
    return path


def _open_plain_file(
    value: str | os.PathLike[str],
) -> tuple[Path, int, os.stat_result]:
    path = _absolute_path(value)
    try:
        before = os.lstat(path)
        _require_plain_status(before)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError:
        raise
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        _require_plain_status(opened)
        _require_plain_status(after_open)
        if not _same_object(before, opened) or not _same_object(
            opened, after_open
        ):
            raise OSError("file identity changed while opening")
        return path, descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _identity(path: Path, value: os.stat_result) -> PlainFileIdentity:
    return PlainFileIdentity(
        path=path,
        device=int(value.st_dev),
        inode=int(value.st_ino),
        size=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
    )


def _finish_read(
    path: Path,
    descriptor: int,
    opened: os.stat_result,
) -> PlainFileIdentity:
    finished = os.fstat(descriptor)
    current = os.lstat(path)
    _require_plain_status(finished)
    _require_plain_status(current)
    if not _same_object(opened, finished) or not _same_object(
        finished, current
    ):
        raise OSError("file identity changed while reading")
    return _identity(path, finished)


def read_plain_file_bytes(
    value: str | os.PathLike[str],
    *,
    maximum_bytes: int,
) -> tuple[PlainFileIdentity, bytes]:
    """Read a bounded plain file without reopening its path."""

    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
    ):
        raise ValueError("maximum_bytes must be a positive integer")
    path, descriptor, opened = _open_plain_file(value)
    try:
        if opened.st_size > maximum_bytes:
            raise OSError("file exceeds the configured byte limit")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_BYTES, maximum_bytes + 1 - observed),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > maximum_bytes:
                raise OSError("file exceeds the configured byte limit")
        identity = _finish_read(path, descriptor, opened)
        payload = b"".join(chunks)
        if len(payload) != identity.size:
            raise OSError("file size changed while reading")
        return identity, payload
    finally:
        os.close(descriptor)


def sha256_plain_file(
    value: str | os.PathLike[str],
    *,
    maximum_bytes: int | None = None,
) -> tuple[PlainFileIdentity, str]:
    """Hash one plain file through the descriptor whose identity was checked."""

    if maximum_bytes is not None and (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
    ):
        raise ValueError("maximum_bytes must be a positive integer or None")
    path, descriptor, opened = _open_plain_file(value)
    try:
        if maximum_bytes is not None and opened.st_size > maximum_bytes:
            raise OSError("file exceeds the configured byte limit")
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            observed += len(chunk)
            if maximum_bytes is not None and observed > maximum_bytes:
                raise OSError("file exceeds the configured byte limit")
            digest.update(chunk)
        identity = _finish_read(path, descriptor, opened)
        if observed != identity.size:
            raise OSError("file size changed while hashing")
        return identity, digest.hexdigest()
    finally:
        os.close(descriptor)


def revalidate_plain_file(identity: PlainFileIdentity) -> Path:
    """Fail if a previously captured path no longer names the same file."""

    if not isinstance(identity, PlainFileIdentity):
        raise TypeError("PlainFileIdentity is required")
    current = os.lstat(identity.path)
    _require_plain_status(current)
    if (
        int(current.st_dev) != identity.device
        or int(current.st_ino) != identity.inode
        or int(current.st_size) != identity.size
        or int(current.st_mtime_ns) != identity.modified_ns
        or int(current.st_ctime_ns) != identity.changed_ns
    ):
        raise OSError("file identity changed after capture")
    return identity.path


__all__ = [
    "PlainFileIdentity",
    "read_plain_file_bytes",
    "revalidate_plain_file",
    "sha256_plain_file",
]
