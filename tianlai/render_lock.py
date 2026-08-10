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
import stat
import sys
import unicodedata
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


@dataclass(frozen=True, slots=True)
class PlainDirectoryIdentity:
    """Stable identity for one ordinary, non-reparse directory.

    Candidate publication keeps this identity from the authorised output root
    through lock acquisition and publication.  Revalidating it at every
    filesystem boundary prevents a path that was approved as a directory from
    later being followed through a symlink or Windows junction.
    """

    path: Path
    device: int
    inode: int


class _LockBusy(Exception):
    pass


def _resolved_output_directory(
    output_directory: str | os.PathLike[str],
) -> Path:
    return Path(output_directory).resolve(strict=False)


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _is_macos_runtime() -> bool:
    return sys.platform == "darwin"


def _macos_system_alias_path(path: Path) -> Path:
    """Canonicalise only the two aliases installed by macOS itself.

    Darwin exposes ``/var`` and ``/tmp`` as links into ``/private``.  Treating
    every resolved spelling as trusted would also accept caller-created
    symlinks, so keep the exception deliberately limited to those two literal
    prefixes.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not _is_macos_runtime():
        return absolute
    value = absolute.as_posix()
    for alias, target in (("/var", "/private/var"), ("/tmp", "/private/tmp")):
        if value == alias or value.startswith(f"{alias}/"):
            return Path(f"{target}{value[len(alias):]}")
    return absolute


def _path_comparison_key(path: Path) -> str:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if _is_macos_runtime():
        absolute = _macos_system_alias_path(absolute)
    value = unicodedata.normalize("NFC", os.fspath(absolute))
    if _is_windows_runtime():
        return os.path.normcase(value)
    if _is_macos_runtime():
        return value.casefold()
    return value


def _is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _plain_directory_status(path: Path) -> os.stat_result:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise OSError(
            exc.errno or errno.ENOENT,
            "render directory is unavailable",
            str(path),
        ) from exc
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or _is_reparse_point(status)
    ):
        raise OSError(
            errno.ELOOP,
            "render directory must be an ordinary non-reparse directory",
            str(path),
        )
    return status


def _capture_plain_directory_ancestry(
    directory: Path,
) -> tuple[tuple[Path, os.stat_result], ...]:
    """Capture every lexical ancestor without following a reparse point.

    Windows may canonicalise an ordinary 8.3 component such as
    ``RUNNER~1`` to its long spelling when ``Path.resolve`` is called.  A
    spelling comparison therefore cannot distinguish that harmless alias
    from a junction.  Walking the lexical path with ``lstat`` retains the
    security property we actually need: every component selected by the
    caller must be an ordinary directory rather than a symlink or reparse
    point.
    """

    absolute = Path(os.path.abspath(os.fspath(directory)))
    ancestry = tuple(reversed((absolute, *absolute.parents)))
    return tuple(
        (path, _plain_directory_status(path)) for path in ancestry
    )


def _revalidate_plain_directory_ancestry(
    ancestry: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    """Fail closed if a captured lexical ancestor changed or became linked."""

    for path, captured_status in ancestry:
        current_status = _plain_directory_status(path)
        if not os.path.samestat(captured_status, current_status):
            raise OSError(
                errno.ESTALE,
                "render directory ancestry changed while it was captured",
                str(path),
            )


def _windows_long_path_name(path: Path) -> Path:
    """Expand Windows 8.3 components without resolving filesystem links.

    ``GetLongPathNameW`` requires traversal permission for every parent and
    can fail on otherwise usable sandboxed temp paths.  ``FindFirstFileW``
    exposes the on-disk long name of one lexical entry, so query only prefixes
    containing the DOS short-name marker.  An unreadable marked component
    keeps its caller spelling; that is fail-closed because a real expansion
    will then disagree with ``Path.resolve`` below.
    """

    requested = Path(os.path.abspath(os.fspath(path)))
    parts = requested.parts
    if not parts or not requested.anchor:
        raise OSError(errno.EINVAL, "Windows directory path has no anchor")
    if not any("~" in component for component in parts[1:]):
        return requested

    import ctypes
    from ctypes import wintypes

    class _WindowsFindData(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("dwReserved0", wintypes.DWORD),
            ("dwReserved1", wintypes.DWORD),
            ("cFileName", wintypes.WCHAR * 260),
            ("cAlternateFileName", wintypes.WCHAR * 14),
        )

    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )
    find_first_file = kernel32.FindFirstFileW
    find_first_file.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(_WindowsFindData),
    )
    find_first_file.restype = wintypes.HANDLE
    find_close = kernel32.FindClose
    find_close.argtypes = (wintypes.HANDLE,)
    find_close.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value

    lexical = Path(parts[0])
    expanded = Path(parts[0])
    for component in parts[1:]:
        lexical /= component
        if "~" not in component:
            expanded /= component
            continue
        data = _WindowsFindData()
        handle = find_first_file(os.fspath(lexical), ctypes.byref(data))
        if handle == invalid_handle:
            long_component = component
        else:
            try:
                long_component = data.cFileName
            finally:
                find_close(handle)
        expanded /= long_component
    return expanded


def capture_plain_directory(
    directory: str | os.PathLike[str],
) -> PlainDirectoryIdentity:
    """Capture an existing directory without accepting linked path aliases."""

    requested = Path(directory)
    if not requested.is_absolute():
        requested = requested.absolute()
    windows_runtime = _is_windows_runtime()
    requested_long_path = (
        _windows_long_path_name(requested) if windows_runtime else None
    )
    requested_ancestry = (
        _capture_plain_directory_ancestry(requested)
        if windows_runtime
        else ()
    )
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OSError(
            errno.ESTALE,
            "render directory could not be resolved safely",
            str(requested),
        ) from exc
    requested_value = Path(os.path.abspath(os.fspath(requested))).as_posix()
    exact_macos_system_alias = (
        _is_macos_runtime()
        and requested_value in {"/var", "/tmp"}
        and _path_comparison_key(requested)
        == _path_comparison_key(resolved)
    )
    status = _plain_directory_status(
        resolved if exact_macos_system_alias else requested
    )
    spelling_changed = (
        _path_comparison_key(requested) != _path_comparison_key(resolved)
    )
    if spelling_changed and not windows_runtime:
        raise OSError(
            errno.ELOOP,
            "render directory path contains a symlink or reparse point",
            str(requested),
        )
    if windows_runtime:
        # The component metadata expansion above does not resolve symlinks or
        # junctions.  Requiring Path.resolve() to agree with that narrower
        # canonicalisation means the Windows exception cannot admit an
        # arbitrary linked spelling, including one briefly swapped in only
        # during the resolve/status window.
        assert requested_long_path is not None
        current_long_path = _windows_long_path_name(requested)
        if _path_comparison_key(current_long_path) != _path_comparison_key(
            requested_long_path
        ):
            raise OSError(
                errno.ESTALE,
                "render directory long-name identity changed while captured",
                str(requested),
            )
        if _path_comparison_key(resolved) != _path_comparison_key(
            current_long_path
        ):
            raise OSError(
                errno.ELOOP,
                "render directory path contains a symlink or reparse point",
                str(requested),
            )
        # Validate both spellings.  The first walk proves that no caller-chosen
        # component is a link/junction; the second proves that the canonical
        # target is itself reached entirely through ordinary directories.
        resolved_ancestry = _capture_plain_directory_ancestry(resolved)
        _revalidate_plain_directory_ancestry(requested_ancestry)
        _revalidate_plain_directory_ancestry(resolved_ancestry)
    resolved_status = _plain_directory_status(resolved)
    if int(resolved_status.st_ino) == 0:
        raise OSError(
            errno.ENOTSUP,
            "render directory has no stable filesystem identity",
            str(requested),
        )
    if not os.path.samestat(status, resolved_status):
        raise OSError(
            errno.ESTALE,
            "render directory changed while its identity was captured",
            str(requested),
        )
    return PlainDirectoryIdentity(
        path=resolved,
        device=int(resolved_status.st_dev),
        inode=int(resolved_status.st_ino),
    )


def ensure_plain_directory_tree(
    directory: str | os.PathLike[str],
) -> PlainDirectoryIdentity:
    """Safely create every missing component below an existing plain root."""

    requested = Path(directory)
    if not requested.is_absolute():
        requested = requested.absolute()
    missing: list[str] = []
    cursor = requested
    while not os.path.lexists(cursor):
        parent = cursor.parent
        if parent == cursor:
            raise OSError(
                errno.ENOENT,
                "render directory has no existing filesystem root",
                str(requested),
            )
        if cursor.name in {"", ".", ".."}:
            raise OSError(
                errno.EINVAL,
                "render directory contains an unsafe path component",
                str(requested),
            )
        missing.append(cursor.name)
        cursor = parent

    identity = capture_plain_directory(cursor)
    for child_name in reversed(missing):
        identity = ensure_authorized_child_directory(identity, child_name)
    return identity


def revalidate_plain_directory(identity: PlainDirectoryIdentity) -> Path:
    """Fail closed unless ``identity.path`` still names the captured object."""

    if not isinstance(identity, PlainDirectoryIdentity):
        raise TypeError("directory identity is required")
    status = _plain_directory_status(identity.path)
    if int(status.st_dev) != identity.device or int(status.st_ino) != identity.inode:
        raise OSError(
            errno.ESTALE,
            "render directory identity changed",
            str(identity.path),
        )
    try:
        resolved = identity.path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OSError(
            errno.ESTALE,
            "render directory identity is no longer resolvable",
            str(identity.path),
        ) from exc
    if _path_comparison_key(resolved) != _path_comparison_key(identity.path):
        raise OSError(
            errno.ELOOP,
            "render directory was replaced by a linked path",
            str(identity.path),
        )
    return identity.path


def _bind_plain_directory_path(
    directory: Path,
    identity: PlainDirectoryIdentity,
    *,
    message: str,
) -> Path:
    """Bind a caller spelling to an already authorised directory identity.

    On Windows the caller may name an ordinary ancestor through its 8.3 alias
    while ``identity.path`` uses the long spelling.  Comparing those strings
    would reject the same directory; recapturing the caller spelling retains
    all symlink/reparse checks and lets the filesystem identity decide.
    """

    authorised = revalidate_plain_directory(identity)
    observed = capture_plain_directory(directory)
    if (
        _path_comparison_key(observed.path)
        != _path_comparison_key(identity.path)
        or observed.device != identity.device
        or observed.inode != identity.inode
    ):
        raise OSError(errno.EPERM, message, str(directory))
    revalidate_plain_directory(identity)
    revalidate_plain_directory(observed)
    return authorised


def ensure_authorized_child_directory(
    authorized_root: PlainDirectoryIdentity,
    child_name: str,
) -> PlainDirectoryIdentity:
    """Create/reopen one direct child anchored to an authorised root.

    On POSIX the mkdir/open operations are performed relative to an open root
    descriptor with ``O_NOFOLLOW``.  Other platforms use the same strict
    lstat/reparse/identity checks before and after the single-component mkdir.
    """

    root = revalidate_plain_directory(authorized_root)
    if (
        not isinstance(child_name, str)
        or not child_name
        or child_name in {".", ".."}
        or Path(child_name).name != child_name
        or any(character in child_name for character in "/\\")
    ):
        raise ValueError("render work directory must be one portable segment")

    child = root / child_name
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        directory_flags |= nofollow

    root_descriptor: int | None = None
    child_descriptor: int | None = None
    try:
        if os.name != "nt" and os.open in os.supports_dir_fd:
            root_descriptor = os.open(root, directory_flags)
            opened_root = os.fstat(root_descriptor)
            if (
                int(opened_root.st_dev) != authorized_root.device
                or int(opened_root.st_ino) != authorized_root.inode
            ):
                raise OSError(
                    errno.ESTALE,
                    "authorised output root changed before work creation",
                    str(root),
                )
            try:
                os.mkdir(child_name, 0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            child_descriptor = os.open(
                child_name,
                directory_flags,
                dir_fd=root_descriptor,
            )
            opened_child = os.fstat(child_descriptor)
            if not stat.S_ISDIR(opened_child.st_mode):
                raise OSError(
                    errno.ENOTDIR,
                    "render work path is not a directory",
                    str(child),
                )
        else:
            try:
                os.mkdir(child, 0o700)
            except FileExistsError:
                pass

        revalidate_plain_directory(authorized_root)
        identity = capture_plain_directory(child)
        if child_descriptor is not None and (
            int(os.fstat(child_descriptor).st_dev) != identity.device
            or int(os.fstat(child_descriptor).st_ino) != identity.inode
        ):
            raise OSError(
                errno.ESTALE,
                "render work directory changed during creation",
                str(child),
            )
        if _path_comparison_key(identity.path.parent) != _path_comparison_key(root):
            raise OSError(
                errno.EPERM,
                "render work directory escaped the authorised output root",
                str(child),
            )
        return identity
    finally:
        if child_descriptor is not None:
            os.close(child_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _lock_identity(output_directory: Path) -> str:
    # APFS is Unicode-normalisation-insensitive in both its case-sensitive and
    # case-insensitive variants, and its default macOS format is also
    # case-insensitive.  Fold those aliases to one conservative lock identity.
    # A case-sensitive APFS volume may consequently serialize two distinct
    # case-only paths, which is safe; allowing two writers to one directory is
    # not.  Windows retains its native normcase semantics, while other POSIX
    # filesystems continue to distinguish case.
    identity = unicodedata.normalize("NFC", str(output_directory))
    if _is_windows_runtime():
        return os.path.normcase(identity)
    if _is_macos_runtime():
        return identity.casefold()
    return identity


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


def _unsafe_lock_file(path: Path, reason: str, code: int) -> OSError:
    return OSError(
        code,
        f"拒绝使用不安全的渲染锁文件（{reason}）",
        str(path),
    )


def _validate_open_lock_file(path: Path, descriptor: int) -> None:
    """Prove that ``descriptor`` still names one private regular file.

    Lock sidecars live beside user-selected output directories, so an existing
    path must be treated as hostile until proven otherwise.  In particular,
    following a symlink and then truncating owner metadata could damage an
    unrelated file.  ``O_NOFOLLOW`` closes the normal POSIX race at open time;
    the descriptor/path identity and link-count checks also fail closed on
    platforms where that flag is unavailable and before every metadata write.
    """

    descriptor_status = os.fstat(descriptor)
    try:
        path_status = os.lstat(path)
    except FileNotFoundError as exc:
        raise _unsafe_lock_file(
            path,
            "路径在打开后被替换或删除",
            errno.ESTALE,
        ) from exc

    if not stat.S_ISREG(descriptor_status.st_mode):
        raise _unsafe_lock_file(path, "打开的对象不是普通文件", errno.EINVAL)
    if not stat.S_ISREG(path_status.st_mode):
        reason = "路径是符号链接" if stat.S_ISLNK(path_status.st_mode) else (
            "路径不是普通文件"
        )
        raise _unsafe_lock_file(path, reason, errno.ELOOP)
    if int(descriptor_status.st_ino) == 0 or int(path_status.st_ino) == 0:
        raise _unsafe_lock_file(
            path,
            "stable file identity is unavailable",
            errno.ENOTSUP,
        )
    if not os.path.samestat(descriptor_status, path_status):
        raise _unsafe_lock_file(
            path,
            "路径与已打开文件不再指向同一对象",
            errno.ESTALE,
        )
    if descriptor_status.st_nlink != 1:
        raise _unsafe_lock_file(
            path,
            "锁文件存在硬链接",
            errno.EMLINK,
        )
    if os.name != "nt" and descriptor_status.st_uid != os.geteuid():
        raise _unsafe_lock_file(
            path,
            "锁文件不属于当前用户",
            errno.EACCES,
        )


def _open_lock_file(
    path: Path,
    *,
    parent_identity: PlainDirectoryIdentity | None = None,
):
    if parent_identity is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_identity = capture_plain_directory(path.parent)
    parent = _bind_plain_directory_path(
        path.parent,
        parent_identity,
        message="render lock escaped its verified parent directory",
    )
    path = parent / path.name
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    parent_descriptor: int | None = None
    try:
        if os.name != "nt" and os.open in os.supports_dir_fd:
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            if nofollow:
                directory_flags |= nofollow
            parent_descriptor = os.open(parent, directory_flags)
            parent_status = os.fstat(parent_descriptor)
            if (
                int(parent_status.st_dev) != parent_identity.device
                or int(parent_status.st_ino) != parent_identity.inode
            ):
                raise OSError(
                    errno.ESTALE,
                    "render lock parent changed before open",
                    str(parent),
                )
            try:
                descriptor = os.open(
                    path.name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError as first_error:
                # Darwin has been observed to return one transient ENOENT when
                # two processes concurrently O_CREAT the same new sidecar,
                # even though this verified parent descriptor remains valid.
                # Retry only after proving both the path and descriptor still
                # name the authorised directory.  The descriptor anchors the
                # second open, while O_NOFOLLOW and the descriptor/path checks
                # below continue to reject replacement attacks.
                revalidate_plain_directory(parent_identity)
                retry_parent_status = os.fstat(parent_descriptor)
                if (
                    int(retry_parent_status.st_dev) != parent_identity.device
                    or int(retry_parent_status.st_ino) != parent_identity.inode
                ):
                    raise OSError(
                        errno.ESTALE,
                        "render lock parent changed before retry",
                        str(parent),
                    ) from first_error
                descriptor = os.open(
                    path.name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
        else:
            descriptor = os.open(path, flags, 0o600)
    except BaseException:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise
    try:
        revalidate_plain_directory(parent_identity)
        _validate_open_lock_file(path, descriptor)
    except BaseException:
        os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        # Do not materialize byte zero until this handle owns the lock.
        # Windows byte-range locks may extend beyond EOF.  Writing the byte
        # here would race when two first-time callers both observe an empty
        # sidecar: one can acquire byte zero before the other's write, making
        # that otherwise harmless initialization fail with EACCES.  The owner
        # metadata writer extends the file while the lock is held instead.
        handle.seek(0)
        return handle, parent_descriptor, parent_identity
    except BaseException:
        handle.close()
        if parent_descriptor is not None:
            os.close(parent_descriptor)
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
    *,
    parent_identity: PlainDirectoryIdentity | None = None,
) -> Iterator[RenderLock]:
    """Own one render target until the context exits.

    Acquisition is non-blocking.  A concurrent owner raises
    :class:`RenderLockError` immediately; normal exit, an exception in the
    context, or process termination releases the operating-system lock.
    """

    requested = Path(output_directory)
    if not requested.is_absolute():
        requested = requested.absolute()
    if requested.parent == requested:
        raise ValueError("render output directory cannot be a filesystem root")
    if parent_identity is None:
        requested_parent = requested.parent
        parent_identity = ensure_plain_directory_tree(requested_parent)
    if requested.name in {"", ".", ".."}:
        raise OSError(
            errno.EPERM,
            "render target is not inside its verified parent directory",
            str(requested),
        )
    parent = _bind_plain_directory_path(
        requested.parent,
        parent_identity,
        message="render target is not inside its verified parent directory",
    )
    resolved = parent / requested.name
    if os.path.lexists(requested):
        target_identity = capture_plain_directory(requested)
        if _path_comparison_key(target_identity.path.parent) != (
            _path_comparison_key(parent)
        ):
            raise OSError(
                errno.EPERM,
                "render target is not inside its verified parent directory",
                str(requested),
            )
        resolved = target_identity.path
    identity = _lock_identity(resolved).encode(
        "utf-8", errors="surrogatepass"
    )
    digest = hashlib.sha256(identity).hexdigest()[:_LOCK_DIGEST_HEX]
    lock_path = parent / f"{_LOCK_FILE_PREFIX}{digest}{_LOCK_FILE_SUFFIX}"
    handle, parent_descriptor, verified_parent = _open_lock_file(
        lock_path,
        parent_identity=parent_identity,
    )
    locked = False
    try:
        try:
            _try_lock(handle)
        except _LockBusy as exc:
            raise RenderLockError(resolved, lock_path) from exc
        locked = True
        revalidate_plain_directory(verified_parent)
        _validate_open_lock_file(lock_path, handle.fileno())
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
            if parent_descriptor is not None:
                os.close(parent_descriptor)


__all__ = (
    "RenderLock",
    "RenderLockError",
    "PlainDirectoryIdentity",
    "acquire_render_lock",
    "capture_plain_directory",
    "ensure_authorized_child_directory",
    "ensure_plain_directory_tree",
    "revalidate_plain_directory",
    "render_lock_path",
)
