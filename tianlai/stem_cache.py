"""Safe on-disk cache for rendered stereo stems.

The cache deliberately stores only little-endian float32 stereo frames.  It is
not a general serialisation format: a cache entry is independently verified
before it can be handed back to a renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, BinaryIO, Iterator, Mapping

import numpy as np

from .canonical_json import canonical_json_bytes
from .render_lock import (
    PlainDirectoryIdentity,
    RenderLockError,
    acquire_render_lock,
    capture_plain_directory,
    revalidate_plain_directory,
)
from .worker_slots import (
    SessionScratchClaim,
    SessionScratchLease,
    WorkerSlotError,
    WorkerSlotPool,
    scratch_volume_identity,
)


CACHE_FORMAT = "tianlai.stem_cache"
CACHE_VERSION = 1
_AUDIO_DTYPE = np.dtype("<f4")
_CHANNELS = 2
_KEY_LENGTH = 64
_MAX_METADATA_BYTES = 64 * 1024
_FINITE_CHECK_CHUNK_SAMPLES = 65_536 * _CHANNELS
_STREAM_VERIFY_CHUNK_BYTES = _FINITE_CHECK_CHUNK_SAMPLES * _AUDIO_DTYPE.itemsize
_VERIFIED_SOURCE_BLOCK_FRAMES = 65_536
_METADATA_KEYS = frozenset(
    {
        "format",
        "version",
        "key",
        "stage",
        "dtype",
        "channels",
        "sample_rate",
        "frame_count",
        "byte_length",
        "audio_sha256",
        "peak_voices",
        "manifest_sha256",
    }
)


class _InvalidDocument(ValueError):
    """An untrusted cache sidecar failed its exact schema."""


class _UnsafePath(ValueError):
    """A cache path contains an unexpected symbolic link."""


def _verified_snapshot_pool_factory() -> WorkerSlotPool:
    """Return the private cross-process ledger used by verified snapshots.

    Keeping construction behind a module-private factory lets tests bind the
    ledger to their own temporary directory instead of touching the real
    per-user pool.  Production callers intentionally receive the shared
    default pool.
    """

    return WorkerSlotPool()


def _same_plain_directory_identity(
    left: PlainDirectoryIdentity,
    right: PlainDirectoryIdentity,
) -> bool:
    return (
        left.path == right.path
        and left.device == right.device
        and left.inode == right.inode
    )


def _capture_verified_snapshot_storage(
    requested: Path,
) -> tuple[PlainDirectoryIdentity, str]:
    """Bind snapshot storage to one plain directory and local volume."""

    try:
        identity = capture_plain_directory(requested)
        directory = revalidate_plain_directory(identity)
        volume_id = scratch_volume_identity(directory)
    except MemoryError:
        raise
    except OSError as exc:
        if exc.errno in {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            errno.ESTALE,
            errno.EXDEV,
            errno.ENOTSUP,
        }:
            raise WorkerSlotError(
                "verified stem snapshot directory identity is unavailable"
            ) from exc
        raise
    except (TypeError, ValueError, WorkerSlotError) as exc:
        raise WorkerSlotError(
            "verified stem snapshot volume identity is unavailable"
        ) from exc
    return identity, volume_id


def _revalidate_verified_snapshot_storage(
    identity: PlainDirectoryIdentity,
    expected_volume_id: str,
    *,
    lease: SessionScratchLease | None = None,
    byte_length: int | None = None,
) -> Path:
    """Return the admitted canonical directory or fail closed on drift."""

    try:
        directory = revalidate_plain_directory(identity)
        current_volume_id = scratch_volume_identity(directory)
    except MemoryError:
        raise
    except (OSError, TypeError, ValueError, WorkerSlotError) as exc:
        raise WorkerSlotError(
            "verified stem snapshot directory or volume identity changed"
        ) from exc
    if current_volume_id != expected_volume_id:
        raise WorkerSlotError(
            "verified stem snapshot volume identity changed"
        )
    if lease is None:
        return directory

    if lease.closed:
        raise WorkerSlotError("verified stem snapshot lease is unavailable")
    try:
        lease_identity = capture_plain_directory(lease.scratch_directory)
        lease_directory = revalidate_plain_directory(lease_identity)
    except MemoryError:
        raise
    except (OSError, TypeError, ValueError, WorkerSlotError) as exc:
        raise WorkerSlotError(
            "verified stem snapshot lease directory is unavailable"
        ) from exc
    if not _same_plain_directory_identity(identity, lease_identity):
        raise WorkerSlotError(
            "verified stem snapshot lease directory identity changed"
        )
    claim = lease.claim
    if (
        claim.scratch_volume_id != expected_volume_id
        or byte_length is None
        or claim.scratch_bytes != byte_length
    ):
        raise WorkerSlotError("verified stem snapshot lease has the wrong claim")
    return lease_directory


def _close_snapshot_resources(
    snapshot: BinaryIO | None,
    lease: SessionScratchLease | None,
) -> BaseException | None:
    """Close the snapshot before its ledger lease, retaining the first error."""

    first_error: BaseException | None = None
    for resource in (snapshot, lease):
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _annotate_snapshot_cleanup_error(
    primary: BaseException,
    cleanup_error: BaseException | None,
) -> None:
    if cleanup_error is None:
        return
    try:
        primary.add_note(
            "verified stem snapshot cleanup also failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
    except BaseException:
        pass


def _all_finite_audio(audio: np.ndarray) -> bool:
    """Validate a track without allocating a track-sized boolean array."""

    flat = audio.reshape(-1)
    for start in range(0, flat.size, _FINITE_CHECK_CHUNK_SAMPLES):
        if not bool(
            np.isfinite(
                flat[start : start + _FINITE_CHECK_CHUNK_SAMPLES]
            ).all()
        ):
            return False
    return True


def _stream_audio_evidence(
    path: Path,
    expected_status: os.stat_result,
    *,
    byte_length: int,
) -> str:
    """Verify one cache payload with fixed-size memory and return its digest.

    Cache publication may run while a newly rendered, track-sized ndarray is
    still live.  Materialising an existing entry just to decide whether it is
    identical would double coordinator stem memory.  Keep the same digest,
    finite-sample and exact-length gates while scanning through one reusable
    block instead.
    """

    digest = hashlib.sha256()
    scratch = bytearray(_STREAM_VERIFY_CHUNK_BYTES)
    with path.open("rb", buffering=0) as source:
        opened_status = os.fstat(source.fileno())
        if not _same_file_identity(expected_status, opened_status):
            raise _InvalidDocument("audio file changed during lookup")
        if opened_status.st_size != byte_length:
            raise _InvalidDocument("audio byte length differs from metadata")

        remaining = byte_length
        while remaining:
            requested = min(remaining, len(scratch))
            view = memoryview(scratch)[:requested]
            offset = 0
            while offset < requested:
                count = source.readinto(view[offset:])
                if not count:
                    raise _InvalidDocument(
                        "audio frame count differs from metadata"
                    )
                offset += count
            digest.update(view)
            samples = np.frombuffer(view, dtype=_AUDIO_DTYPE)
            if not bool(np.isfinite(samples).all()):
                raise _InvalidDocument("audio contains non-finite samples")
            remaining -= requested

        if source.read(1):
            raise _InvalidDocument("audio frame count differs from metadata")
        final_status = os.fstat(source.fileno())
        if not _same_file_identity(opened_status, final_status):
            raise _InvalidDocument("audio file changed during lookup")
        if final_status.st_size != byte_length:
            raise _InvalidDocument("audio byte length differs from metadata")
    return digest.hexdigest()


def _stream_open_audio_evidence(
    source: BinaryIO,
    expected_status: os.stat_result,
    *,
    byte_length: int,
    snapshot: BinaryIO | None = None,
) -> str:
    """Verify an open payload, optionally copying those exact bytes privately."""

    digest = hashlib.sha256()
    scratch = bytearray(_STREAM_VERIFY_CHUNK_BYTES)
    source.seek(0)
    opened_status = os.fstat(source.fileno())
    if not _same_file_identity(expected_status, opened_status):
        raise _InvalidDocument("audio file changed during lookup")
    if opened_status.st_size != byte_length:
        raise _InvalidDocument("audio byte length differs from metadata")

    remaining = byte_length
    while remaining:
        requested = min(remaining, len(scratch))
        view = memoryview(scratch)[:requested]
        offset = 0
        while offset < requested:
            count = source.readinto(view[offset:])
            if not count:
                raise _InvalidDocument(
                    "audio frame count differs from metadata"
                )
            offset += count
        digest.update(view)
        samples = np.frombuffer(view, dtype=_AUDIO_DTYPE)
        if not bool(np.isfinite(samples).all()):
            raise _InvalidDocument("audio contains non-finite samples")
        if snapshot is not None:
            written = 0
            while written < requested:
                count = snapshot.write(view[written:])
                if not count:
                    raise OSError("verified stem snapshot write was incomplete")
                written += count
        remaining -= requested

    if source.read(1):
        raise _InvalidDocument("audio frame count differs from metadata")
    final_status = os.fstat(source.fileno())
    if not _same_file_identity(opened_status, final_status):
        raise _InvalidDocument("audio file changed during lookup")
    if final_status.st_size != byte_length:
        raise _InvalidDocument("audio byte length differs from metadata")
    source.seek(0)
    if snapshot is not None:
        snapshot.flush()
        snapshot.seek(0)
    return digest.hexdigest()


def _read_exact_bytes(source: BinaryIO, count: int) -> bytes:
    """Read at most ``count`` bytes, tolerating legal short raw reads."""

    payload = bytearray(count)
    view = memoryview(payload)
    offset = 0
    while offset < count:
        read = source.readinto(view[offset:])
        if not read:
            break
        offset += read
    if offset != count:
        del view
        del payload[offset:]
    return bytes(payload)


def _read_verified_metadata_bytes(
    path: Path,
    expected_status: os.stat_result,
) -> bytes:
    """Read one bounded sidecar from the exact file already inspected."""

    if (
        not stat.S_ISREG(expected_status.st_mode)
        or expected_status.st_size > _MAX_METADATA_BYTES
    ):
        raise _InvalidDocument(
            "metadata must be a regular file within the sidecar size limit"
        )
    with path.open("rb", buffering=0) as source:
        opened_status = os.fstat(source.fileno())
        if not _same_file_identity(expected_status, opened_status):
            raise _InvalidDocument("metadata changed during lookup")
        if (
            opened_status.st_size != expected_status.st_size
            or opened_status.st_size > _MAX_METADATA_BYTES
        ):
            raise _InvalidDocument("metadata changed during lookup")
        payload = _read_exact_bytes(source, int(opened_status.st_size))
        trailing = source.read(1)
        final_status = os.fstat(source.fileno())
        if not _same_file_identity(opened_status, final_status):
            raise _InvalidDocument("metadata changed during lookup")
        if (
            len(payload) != opened_status.st_size
            or trailing
            or final_status.st_size != opened_status.st_size
        ):
            raise _InvalidDocument("metadata changed during lookup")
    return payload


@dataclass(frozen=True, slots=True)
class StemCacheRecord:
    """Verified cache entry information, including its on-disk locations."""

    key: str
    audio_path: Path
    metadata_path: Path
    metadata: Mapping[str, Any]


class VerifiedStemSource:
    """A fully verified private snapshot with verified consumption.

    Opening performs complete length, SHA-256 and finite-sample passes while
    copying the exact bytes from the bound cache descriptor into an anonymous
    private snapshot and then verifying that snapshot before it is exposed.
    Consumption independently repeats those gates against the bound snapshot
    descriptor.  Cache replacement, in-place modification and snapshot write
    damage cannot expose unverified audio, and no track-sized ndarray is
    required.  Its exact snapshot claim remains live until the descriptor is
    closed; post-fork children may only abandon their duplicate resources.
    """

    __slots__ = (
        "record",
        "_source",
        "_opened_status",
        "_lease",
        "_owner_pid",
        "_closed",
        "_consumed",
        "_iterator_active",
        "__weakref__",
    )

    def __init__(
        self,
        record: StemCacheRecord,
        source: BinaryIO,
        opened_status: os.stat_result,
        lease: SessionScratchLease | None = None,
    ) -> None:
        self.record = record
        self._source = source
        self._opened_status = opened_status
        self._lease = lease
        self._owner_pid = os.getpid()
        self._closed = False
        self._consumed = False
        self._iterator_active = False

    @property
    def frame_count(self) -> int:
        return int(self.record.metadata["frame_count"])

    @property
    def shape(self) -> tuple[int, int]:
        return (self.frame_count, _CHANNELS)

    @property
    def audio_sha256(self) -> str:
        """Digest revalidated while the private snapshot is consumed."""

        return str(self.record.metadata["audio_sha256"])

    @property
    def closed(self) -> bool:
        return self._closed or self._owner_pid != os.getpid()

    def iter_blocks(
        self,
        block_frames: int = _VERIFIED_SOURCE_BLOCK_FRAMES,
    ) -> Iterator[np.ndarray]:
        """Yield immutable float32 stereo blocks from the private snapshot."""

        if (
            isinstance(block_frames, bool)
            or not isinstance(block_frames, int)
            or block_frames <= 0
            or block_frames > _VERIFIED_SOURCE_BLOCK_FRAMES
        ):
            raise ValueError(
                "block_frames must be between 1 and 65536"
            )
        if self.closed:
            raise ValueError("verified stem source is closed")
        if self._consumed or self._iterator_active:
            raise ValueError("verified stem source can only be consumed once")

        self._iterator_active = True
        self._consumed = True
        expected_bytes = int(self.record.metadata["byte_length"])
        expected_digest = str(self.record.metadata["audio_sha256"])
        digest = hashlib.sha256()
        chunk_bytes = block_frames * _CHANNELS * _AUDIO_DTYPE.itemsize
        remaining = expected_bytes
        source = self._source
        source.seek(0)
        try:
            while remaining:
                if self._owner_pid != os.getpid():
                    raise ValueError(
                        "verified stem source is unavailable after fork"
                    )
                requested = min(remaining, chunk_bytes)
                payload = _read_exact_bytes(source, requested)
                if len(payload) != requested:
                    raise _InvalidDocument(
                        "audio frame count differs from metadata during consumption"
                    )
                digest.update(payload)
                block = np.frombuffer(payload, dtype=_AUDIO_DTYPE).reshape(-1, 2)
                if not bool(np.isfinite(block).all()):
                    raise _InvalidDocument(
                        "audio contains non-finite samples during consumption"
                    )
                remaining -= requested
                yield block

            if self._owner_pid != os.getpid():
                raise ValueError(
                    "verified stem source is unavailable after fork"
                )
            if source.read(1):
                raise _InvalidDocument(
                    "audio frame count differs from metadata during consumption"
                )
            if digest.hexdigest() != expected_digest:
                raise _InvalidDocument(
                    "audio digest changed during consumption"
                )
            final_status = os.fstat(source.fileno())
            if not _same_file_identity(self._opened_status, final_status):
                raise _InvalidDocument("audio file changed during consumption")
            if final_status.st_size != expected_bytes:
                raise _InvalidDocument(
                    "audio byte length changed during consumption"
                )
        finally:
            self._iterator_active = False

    def materialise(self) -> np.ndarray:
        """Return one owned ndarray, preserving the verified descriptor path."""

        audio = np.empty((self.frame_count, _CHANNELS), dtype=_AUDIO_DTYPE)
        offset = 0
        for block in self.iter_blocks():
            stop = offset + int(block.shape[0])
            audio[offset:stop] = block
            offset = stop
        if offset != self.frame_count:
            raise _InvalidDocument("audio frame count differs from metadata")
        return audio

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_error = _close_snapshot_resources(self._source, self._lease)
        if cleanup_error is not None:
            raise cleanup_error

    def __enter__(self) -> "VerifiedStemSource":
        if self.closed:
            raise ValueError("verified stem source is closed")
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        _traceback: object,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except BaseException as cleanup_error:
            # A context body failure is earlier and therefore remains primary;
            # descriptor/ledger cleanup is still attempted in full by close().
            if isinstance(exc, BaseException):
                _annotate_snapshot_cleanup_error(exc, cleanup_error)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


@dataclass(frozen=True, slots=True)
class StemCacheLookup:
    """A non-throwing cache lookup result.

    ``status`` is one of ``hit``, ``missing``, ``incomplete``, ``corrupt``,
    ``unavailable``, ``too_large``, ``invalid_limit`` or ``invalid_key``.
    Only ``hit`` carries audio.  Ordinary cache and optional-ledger failures
    are structured; memory exhaustion and an inability to prove snapshot
    directory/volume identity intentionally remain hard failures.
    """

    status: str
    record: StemCacheRecord | None = None
    reason: str | None = None
    audio: np.ndarray | None = None
    source: VerifiedStemSource | None = None

    @property
    def hit(self) -> bool:
        return self.status == "hit"


@dataclass(frozen=True, slots=True)
class StemCacheStoreResult:
    """A non-throwing cache publication result."""

    status: str
    record: StemCacheRecord | None = None
    reason: str | None = None

    @property
    def stored(self) -> bool:
        return self.status in {"stored", "repaired"}


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidDocument(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _InvalidDocument(f"non-finite JSON value: {value}")


def _strict_json_load(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _InvalidDocument) as exc:
        raise _InvalidDocument("metadata is not strict JSON") from exc
    if not isinstance(document, dict):
        raise _InvalidDocument("metadata root is not an object")
    return document


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _KEY_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_int(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _validate_metadata(document: Mapping[str, Any], key: str) -> None:
    if set(document) != _METADATA_KEYS:
        raise _InvalidDocument("metadata keys do not exactly match the schema")
    if document["format"] != CACHE_FORMAT or document["version"] != CACHE_VERSION:
        raise _InvalidDocument("unsupported cache metadata format")
    if document["key"] != key or not _is_sha256(document["key"]):
        raise _InvalidDocument("metadata key is invalid")
    if not isinstance(document["stage"], str) or not document["stage"]:
        raise _InvalidDocument("metadata stage is invalid")
    if document["dtype"] != "<f4" or document["channels"] != _CHANNELS:
        raise _InvalidDocument("metadata audio shape is not float32 stereo")
    if not _is_int(document["sample_rate"], minimum=1):
        raise _InvalidDocument("metadata sample rate is invalid")
    if not _is_int(document["frame_count"]):
        raise _InvalidDocument("metadata frame count is invalid")
    if not _is_int(document["byte_length"]):
        raise _InvalidDocument("metadata byte length is invalid")
    if document["byte_length"] != document["frame_count"] * _CHANNELS * 4:
        raise _InvalidDocument("metadata byte length does not match frame count")
    if not _is_sha256(document["audio_sha256"]):
        raise _InvalidDocument("metadata audio digest is invalid")
    if not _is_int(document["peak_voices"]):
        raise _InvalidDocument("metadata peak voices is invalid")
    if not _is_sha256(document["manifest_sha256"]):
        raise _InvalidDocument("metadata manifest digest is invalid")


def _canonical_identity(value: Any) -> Any:
    """Convert a JSON-like identity into the one accepted by canonical JSON."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cache identity cannot contain non-finite floats")
        return value
    if isinstance(value, np.generic):
        return _canonical_identity(value.item())
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("cache identity object keys must be strings")
            result[key] = _canonical_identity(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_identity(item) for item in value]
    raise TypeError(f"cache identity cannot contain {type(value).__name__}")


def build_cache_key(identity: Any) -> str:
    """Return the SHA-256 key for a canonical JSON cache identity."""

    canonical = _canonical_cache_identity_bytes(identity)
    return hashlib.sha256(canonical).hexdigest()


def _canonical_cache_identity_bytes(identity: Any) -> bytes:
    """Canonicalise a cache identity once for hashing and verification."""

    return canonical_json_bytes(_canonical_identity(identity))


# A few clear aliases make this helper convenient for callers that describe
# the same operation as cache-key construction or canonical identity hashing.
canonical_cache_key = build_cache_key
cache_key_for_identity = build_cache_key


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def source_tree_digest(source_root: str | os.PathLike[str] | None = None) -> str:
    """Hash all ``tianlai/**/*.py`` source files with their relative names."""

    root = Path(source_root) if source_root is not None else _project_root()
    package = root / "tianlai"
    if not package.is_dir() or package.is_symlink():
        raise ValueError("source root does not contain a regular tianlai package")
    digest = hashlib.sha256()
    # ``Path.rglob`` followed by ``is_symlink`` and ``is_file`` performs
    # several directory/stat operations per source.  This check runs at
    # multiple safety boundaries during a render, so enumerate with scandir.
    # File type is checked immediately before opening, and file contents are
    # still read and hashed on every call: source-drift detection is not
    # weakened into a metadata-only cache.
    entries: list[tuple[str, str]] = []
    directories = [(os.fspath(package), "tianlai")]
    windows = os.name == "nt"
    while directories:
        directory, relative_directory = directories.pop()
        with os.scandir(directory) as children:
            for child in children:
                relative = f"{relative_directory}/{child.name}"
                if child.is_dir(follow_symlinks=False):
                    directories.append((child.path, relative))
                name_matches = (
                    child.name.lower().endswith(".py")
                    if windows
                    else child.name.endswith(".py")
                )
                if name_matches:
                    entries.append((relative, child.path))
    entries.sort(key=lambda item: item[0])
    for relative_text, path_text in entries:
        path = Path(path_text)
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"source tree contains a non-regular Python file: {path}")
        relative = relative_text.encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with open(path_text, "rb") as source:
            data = source.read()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


PROCESS_SOURCE_TREE_SHA256 = source_tree_digest()


def current_source_tree_matches(
    producer_digest: str = PROCESS_SOURCE_TREE_SHA256,
    source_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Safely compare a producer's captured source digest with this tree."""

    if not _is_sha256(producer_digest):
        return False
    try:
        return source_tree_digest(source_root) == producer_digest
    except (OSError, ValueError):
        return False


def _is_cache_key(key: object) -> bool:
    return isinstance(key, str) and _is_sha256(key)


def _same_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _write_stream_payload(
    target: BinaryIO,
    payload: bytes | bytearray | memoryview,
) -> None:
    """Write one bounded cache block without accepting a short write."""

    remaining = memoryview(payload)
    while remaining:
        written = target.write(remaining)
        if written is None or written <= 0:
            raise OSError("streaming cache write made no progress")
        remaining = remaining[written:]


def _discard_private_stream_handle(
    temporary: BinaryIO | None,
    descriptor: int | None = None,
) -> None:
    """Discard only bytes owned by an already-open private temp handle.

    There is no portable atomic "unlink this path only if it still names this
    inode" operation.  Closing the descriptor and then checking + unlinking
    the pathname would therefore be vulnerable to a same-directory rename
    race.  Truncating through the descriptor is identity-bound; the random
    name may remain as a zero-byte diagnostic after an aborted transaction,
    but cleanup can never delete a replacement installed by another actor.
    """

    try:
        if temporary is not None:
            try:
                temporary.seek(0)
                temporary.truncate(0)
            finally:
                temporary.close()
        elif descriptor is not None:
            try:
                os.ftruncate(descriptor, 0)
            finally:
                os.close(descriptor)
    except (OSError, PermissionError, ValueError):
        # Cache cleanup is non-authoritative.  The descriptor close attempted
        # above is the only safe cross-platform action available here.
        pass


class _StreamingStemCacheStore:
    """One non-authoritative raw-stem cache write transaction.

    Rendering tees already-verified raw blocks into this object.  No key lock
    is held while those blocks are produced.  Only ``finish`` may publish,
    and only after its caller supplies the independently verified complete
    frame count and digest.
    """

    __slots__ = (
        "_cache",
        "_key",
        "_stage",
        "_sample_rate",
        "_peak_voices",
        "_manifest_sha256",
        "_audio_path",
        "_metadata_path",
        "_directory_identity",
        "_temporary",
        "_temporary_path",
        "_temporary_status",
        "_digest",
        "_frame_count",
        "_terminal_result",
        "_result",
        "_aborted",
        "_publication_attempted",
        "_installed",
        "_owner_pid",
    )

    def __init__(
        self,
        cache: "StemCache",
        key: str,
        *,
        stage: str,
        sample_rate: int,
        peak_voices: int,
        manifest_sha256: str,
        audio_path: Path | None = None,
        metadata_path: Path | None = None,
        directory_identity: Any | None = None,
        temporary: BinaryIO | None = None,
        temporary_path: Path | None = None,
        temporary_status: os.stat_result | None = None,
        terminal_result: StemCacheStoreResult | None = None,
    ) -> None:
        self._cache = cache
        self._key = key
        self._stage = stage
        self._sample_rate = sample_rate
        self._peak_voices = peak_voices
        self._manifest_sha256 = manifest_sha256
        self._audio_path = audio_path
        self._metadata_path = metadata_path
        self._directory_identity = directory_identity
        self._temporary = temporary
        self._temporary_path = temporary_path
        self._temporary_status = temporary_status
        self._digest = hashlib.sha256()
        self._frame_count = 0
        self._terminal_result = terminal_result
        self._result: StemCacheStoreResult | None = None
        self._aborted = False
        self._publication_attempted = False
        self._installed = False
        self._owner_pid = os.getpid()

    @property
    def temporary_path(self) -> Path | None:
        """Private diagnostic hook; callers must never mutate this path."""

        return self._temporary_path

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def active(self) -> bool:
        return (
            not self._aborted
            and self._result is None
            and self._terminal_result is None
            and self._temporary is not None
        )

    def _ensure_live_call(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "streaming cache transaction belongs to another process"
            )
        if self._aborted:
            raise ValueError("streaming cache transaction was aborted")
        if self._result is not None:
            raise ValueError("streaming cache transaction is already finished")

    def _close_temporary(self) -> BaseException | None:
        temporary = self._temporary
        self._temporary = None
        if temporary is None:
            return None
        try:
            temporary.close()
        except BaseException as exc:
            return exc
        return None

    def _cleanup_temporary(self) -> None:
        temporary = self._temporary
        self._temporary = None
        if temporary is not None:
            if os.getpid() == self._owner_pid:
                _discard_private_stream_handle(temporary)
            else:
                # A fork child owns only its duplicate descriptor.  Truncating
                # it would mutate the parent's still-active cache transaction.
                try:
                    temporary.close()
                except (OSError, ValueError):
                    pass

    def _disable(self, result: StemCacheStoreResult) -> None:
        if self._terminal_result is None:
            self._terminal_result = result
        self._cleanup_temporary()

    def append(self, block: np.ndarray) -> None:
        """Append one exact, bounded raw float32-stereo block.

        Ordinary validation or filesystem failures disable only this cache
        transaction.  They are returned later by ``finish`` so the renderer
        can continue consuming the authoritative stem.  MemoryError remains
        a host-pressure signal and is re-raised immediately.
        """

        self._ensure_live_call()
        if self._terminal_result is not None:
            return
        if (
            not isinstance(block, np.ndarray)
            or block.ndim != 2
            or block.shape[1:] != (_CHANNELS,)
            or block.shape[0] <= 0
            or block.shape[0] > _VERIFIED_SOURCE_BLOCK_FRAMES
            or block.dtype != _AUDIO_DTYPE
            or not block.flags.c_contiguous
        ):
            self._disable(
                StemCacheStoreResult(
                    "invalid_input",
                    reason=(
                        "streaming stem blocks must be non-empty contiguous "
                        "<f4 stereo arrays of at most 65536 frames"
                    ),
                )
            )
            return
        try:
            if not _all_finite_audio(block):
                self._disable(
                    StemCacheStoreResult(
                        "invalid_input",
                        reason="streaming stem block contains NaN or infinity",
                    )
                )
                return
            temporary = self._temporary
            if temporary is None:
                self._disable(
                    StemCacheStoreResult(
                        "write_error",
                        reason="streaming cache temporary file is unavailable",
                    )
                )
                return
            payload = memoryview(block).cast("B")
            _write_stream_payload(temporary, payload)
            self._digest.update(payload)
            self._frame_count += int(block.shape[0])
        except MemoryError:
            self._cleanup_temporary()
            self._aborted = True
            raise
        except (OSError, PermissionError, TypeError, ValueError) as exc:
            self._disable(
                StemCacheStoreResult(
                    "write_error",
                    reason=f"streaming cache write failed: {exc}",
                )
            )

    def _validate_temporary(self, *, byte_length: int) -> None:
        temporary = self._temporary
        path = self._temporary_path
        expected_status = self._temporary_status
        directory_identity = self._directory_identity
        if (
            temporary is None
            or path is None
            or expected_status is None
            or directory_identity is None
        ):
            raise OSError("streaming cache temporary file is unavailable")
        directory = revalidate_plain_directory(directory_identity)
        if path.parent != directory or path != directory / path.name:
            raise OSError("streaming cache temporary path escaped its directory")
        opened_status = os.fstat(temporary.fileno())
        path_status = path.lstat()
        if (
            not _same_file_identity(expected_status, opened_status)
            or not _same_file_identity(expected_status, path_status)
            or opened_status.st_size != byte_length
            or path_status.st_size != byte_length
        ):
            raise OSError("streaming cache temporary identity or length changed")
        revalidate_plain_directory(directory_identity)

    def _metadata(self, *, frame_count: int, audio_sha256: str) -> dict[str, Any]:
        return {
            "format": CACHE_FORMAT,
            "version": CACHE_VERSION,
            "key": self._key,
            "stage": self._stage,
            "dtype": "<f4",
            "channels": _CHANNELS,
            "sample_rate": self._sample_rate,
            "frame_count": frame_count,
            "byte_length": frame_count * _CHANNELS * _AUDIO_DTYPE.itemsize,
            "audio_sha256": audio_sha256,
            "peak_voices": self._peak_voices,
            "manifest_sha256": self._manifest_sha256,
        }

    def _complete(
        self,
        result: StemCacheStoreResult,
        *,
        cleanup: bool = True,
    ) -> StemCacheStoreResult:
        if cleanup:
            self._cleanup_temporary()
        self._result = result
        return result

    def finish(
        self,
        expected_frame_count: int,
        expected_audio_sha256: str,
    ) -> StemCacheStoreResult:
        """Commit only an independently verified complete raw stem."""

        self._ensure_live_call()
        if self._terminal_result is not None:
            return self._complete(self._terminal_result)
        if (
            isinstance(expected_frame_count, bool)
            or not isinstance(expected_frame_count, int)
            or expected_frame_count < 0
            or not _is_sha256(expected_audio_sha256)
        ):
            return self._complete(
                StemCacheStoreResult(
                    "invalid_input",
                    reason=(
                        "streaming finish requires a non-negative frame count "
                        "and lowercase SHA-256 digest"
                    ),
                )
            )
        if self._frame_count != expected_frame_count:
            relation = (
                "short"
                if self._frame_count < expected_frame_count
                else "excess"
            )
            return self._complete(
                StemCacheStoreResult(
                    "invalid_input",
                    reason=f"streaming stem frame count is {relation}",
                )
            )
        actual_digest = self._digest.hexdigest()
        if actual_digest != expected_audio_sha256:
            return self._complete(
                StemCacheStoreResult(
                    "invalid_input",
                    reason="streaming stem SHA-256 does not match",
                )
            )

        byte_length = expected_frame_count * _CHANNELS * _AUDIO_DTYPE.itemsize
        metadata = self._metadata(
            frame_count=expected_frame_count,
            audio_sha256=expected_audio_sha256,
        )
        metadata_payload = canonical_json_bytes(metadata)
        try:
            temporary = self._temporary
            if temporary is None:
                raise OSError("streaming cache temporary file is unavailable")
            temporary.flush()
            os.fsync(temporary.fileno())
            self._validate_temporary(byte_length=byte_length)

            root = self._cache._root(create=True)
            with acquire_render_lock(root / ".locks" / self._key):
                assert self._audio_path is not None
                assert self._metadata_path is not None
                existing = self._cache._lookup(
                    self._key,
                    materialise_audio=False,
                )
                if existing.status == "hit":
                    assert existing.record is not None
                    old_digest = existing.record.metadata["audio_sha256"]
                    if old_digest != metadata["audio_sha256"]:
                        return self._complete(
                            StemCacheStoreResult(
                                "conflict",
                                existing.record,
                                "a valid entry for this key has different audio",
                            )
                        )
                    if dict(existing.record.metadata) != metadata:
                        self._cache._write_atomic(
                            self._metadata_path,
                            metadata_payload,
                        )
                        record = StemCacheRecord(
                            self._key,
                            self._audio_path,
                            self._metadata_path,
                            dict(metadata),
                        )
                        return self._complete(
                            StemCacheStoreResult(
                                "repaired",
                                record,
                                "semantic metadata repaired for identical audio",
                            )
                        )
                    return self._complete(
                        StemCacheStoreResult(
                            "exists",
                            existing.record,
                            "entry already published",
                        )
                    )

                # Revalidate the exact private file immediately before closing
                # its descriptor and moving that random name.  Once replace is
                # attempted, cleanup never unlinks the old name: a racing
                # writer may have installed a different entry there while the
                # failed call was unwinding.
                self._validate_temporary(byte_length=byte_length)
                close_error = self._close_temporary()
                if close_error is not None:
                    raise close_error
                directory = revalidate_plain_directory(
                    self._directory_identity
                )
                assert self._temporary_path is not None
                current = self._temporary_path.lstat()
                if (
                    self._temporary_path.parent != directory
                    or self._temporary_status is None
                    or not _same_file_identity(
                        self._temporary_status,
                        current,
                    )
                    or current.st_size != byte_length
                ):
                    raise OSError(
                        "streaming cache temporary changed before publication"
                    )
                self._publication_attempted = True
                os.replace(self._temporary_path, self._audio_path)
                self._installed = True

                installed_status = self._audio_path.lstat()
                if (
                    self._temporary_status is None
                    or not _same_file_identity(
                        self._temporary_status,
                        installed_status,
                    )
                    or installed_status.st_size != byte_length
                ):
                    raise OSError(
                        "streaming cache audio identity changed during publication"
                    )
                # Audio is durable and installed first.  Metadata remains the
                # commit marker and is replaced last, matching store(buffer).
                self._cache._write_atomic(
                    self._metadata_path,
                    metadata_payload,
                )
                record = StemCacheRecord(
                    self._key,
                    self._audio_path,
                    self._metadata_path,
                    dict(metadata),
                )
                return self._complete(
                    StemCacheStoreResult("stored", record),
                    cleanup=False,
                )
        except MemoryError:
            self._cleanup_temporary()
            self._aborted = True
            raise
        except RenderLockError as exc:
            return self._complete(
                StemCacheStoreResult("busy", reason=str(exc))
            )
        except (OSError, PermissionError, _UnsafePath, ValueError) as exc:
            return self._complete(
                StemCacheStoreResult(
                    "write_error",
                    reason=f"streaming cache write failed: {exc}",
                )
            )

    def abort(self) -> None:
        """Discard an uncommitted tee without touching cache entry paths."""

        if self._result is not None or self._aborted:
            return
        self._aborted = True
        self._cleanup_temporary()

    close = abort

    def __enter__(self) -> "_StreamingStemCacheStore":
        self._ensure_live_call()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.abort()

    def __del__(self) -> None:
        try:
            self.abort()
        except BaseException:
            pass


class StemCache:
    """Content-addressed stem cache rooted at ``root/v1/<prefix>/``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _root(self, *, create: bool) -> Path:
        if self.root.is_symlink():
            raise _UnsafePath("cache root must not be a symbolic link")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise _UnsafePath("cache root must be a regular directory")
        return self.root.resolve(strict=False)

    def _paths(self, key: str, *, create: bool) -> tuple[Path, Path]:
        root = self._root(create=create)
        directory = root / "v1" / key[:2]
        for ancestor in (root / "v1", directory):
            if ancestor.exists() and ancestor.is_symlink():
                raise _UnsafePath("cache entry directory must not be a symbolic link")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise _UnsafePath("cache entry directory must be regular")
        elif not directory.exists():
            return directory / f"{key}.f32le", directory / f"{key}.json"
        audio = directory / f"{key}.f32le"
        metadata = directory / f"{key}.json"
        if not audio.is_relative_to(root) or not metadata.is_relative_to(root):
            raise _UnsafePath("cache entry escaped cache root")
        return audio, metadata

    def begin_streaming_store(
        self,
        key: str,
        *,
        stage: str,
        sample_rate: int,
        peak_voices: int,
        manifest_sha256: str,
    ) -> _StreamingStemCacheStore:
        """Begin an internal bounded raw-block cache transaction.

        This is an integration primitive for the ensemble renderer, not a
        user-facing cache format.  Beginning never holds the per-key lock;
        ordinary setup failures are retained on the returned transaction and
        reported by ``finish`` without interrupting authoritative rendering.
        """

        invalid_reason: str | None = None
        if not _is_cache_key(key):
            invalid_reason = "key must be 64 lowercase hex characters"
        elif not isinstance(stage, str) or not stage:
            invalid_reason = "stage must be a non-empty string"
        elif not _is_int(sample_rate, minimum=1):
            invalid_reason = "sample_rate must be a positive integer"
        elif not _is_int(peak_voices):
            invalid_reason = "peak_voices must be a non-negative integer"
        elif not _is_sha256(manifest_sha256):
            invalid_reason = "manifest_sha256 must be lowercase SHA-256 hex"
        if invalid_reason is not None:
            return _StreamingStemCacheStore(
                self,
                key,
                stage=stage,
                sample_rate=sample_rate,
                peak_voices=peak_voices,
                manifest_sha256=manifest_sha256,
                terminal_result=StemCacheStoreResult(
                    "invalid_input",
                    reason=invalid_reason,
                ),
            )

        descriptor: int | None = None
        temporary: BinaryIO | None = None
        temporary_path: Path | None = None
        temporary_status: os.stat_result | None = None
        directory_identity: Any | None = None
        try:
            audio_path, metadata_path = self._paths(key, create=True)
            directory_identity = capture_plain_directory(audio_path.parent)
            directory = revalidate_plain_directory(directory_identity)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=directory,
                prefix=f".{key}.stream-",
                suffix=".f32le.tmp",
            )
            temporary_path = Path(temporary_name)
            temporary_status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(temporary_status.st_mode)
                or temporary_path.parent != directory
                or not _same_file_identity(
                    temporary_status,
                    temporary_path.lstat(),
                )
            ):
                raise OSError(
                    "streaming cache temporary file has an unsafe identity"
                )
            temporary = os.fdopen(descriptor, "w+b", buffering=0)
            descriptor = None
            transaction = _StreamingStemCacheStore(
                self,
                key,
                stage=stage,
                sample_rate=sample_rate,
                peak_voices=peak_voices,
                manifest_sha256=manifest_sha256,
                audio_path=audio_path,
                metadata_path=metadata_path,
                directory_identity=directory_identity,
                temporary=temporary,
                temporary_path=temporary_path,
                temporary_status=temporary_status,
            )
            revalidate_plain_directory(directory_identity)
            # The transaction takes ownership only after every setup
            # revalidation succeeds.  Until this assignment the exception
            # path below remains responsible for the descriptor-bound
            # truncate-and-close.  The random zero-byte name is intentionally
            # retained because portable path deletion has a replacement race.
            temporary = None
            return transaction
        except MemoryError:
            _discard_private_stream_handle(temporary, descriptor)
            raise
        except (OSError, PermissionError, _UnsafePath, ValueError) as exc:
            _discard_private_stream_handle(temporary, descriptor)
            return _StreamingStemCacheStore(
                self,
                key,
                stage=stage,
                sample_rate=sample_rate,
                peak_voices=peak_voices,
                manifest_sha256=manifest_sha256,
                terminal_result=StemCacheStoreResult(
                    "write_error",
                    reason=f"streaming cache setup failed: {exc}",
                ),
            )

    def load(
        self,
        key: str,
        *,
        maximum_audio_bytes: int | None = None,
    ) -> StemCacheLookup:
        """Load and fully verify a stem.  Failures are reported, never raised."""

        if maximum_audio_bytes is not None and (
            isinstance(maximum_audio_bytes, bool)
            or not isinstance(maximum_audio_bytes, int)
            or maximum_audio_bytes < 0
        ):
            return StemCacheLookup(
                "invalid_limit",
                reason="maximum_audio_bytes must be a non-negative integer",
            )
        return self._lookup(
            key,
            materialise_audio=True,
            maximum_audio_bytes=maximum_audio_bytes,
        )

    def open_verified(
        self,
        key: str,
        *,
        snapshot_directory: str | os.PathLike[str] | None = None,
    ) -> StemCacheLookup:
        """Open a verified stem for bounded, descriptor-bound consumption.

        Unlike :meth:`load`, a hit does not allocate a track-sized ndarray.
        The returned source owns a fully verified private descriptor and
        independently verifies it again as it is consumed.  It must be closed
        by the caller (normally with a ``with`` statement).
        """

        if not _is_cache_key(key):
            return StemCacheLookup(
                "invalid_key",
                reason="key must be 64 lowercase hex characters",
            )
        source: BinaryIO | None = None
        snapshot: BinaryIO | None = None
        lease: SessionScratchLease | None = None
        snapshot_identity: PlainDirectoryIdentity | None = None
        snapshot_volume_id: str | None = None
        structured_result: StemCacheLookup | None = None
        try:
            audio_path, metadata_path = self._paths(key, create=False)
            try:
                audio_status = audio_path.lstat()
            except FileNotFoundError:
                audio_status = None
            try:
                metadata_status = metadata_path.lstat()
            except FileNotFoundError:
                metadata_status = None
            if audio_status is None and metadata_status is None:
                return StemCacheLookup("missing", reason="entry does not exist")
            if audio_status is None or metadata_status is None:
                return StemCacheLookup(
                    "incomplete",
                    reason="audio or metadata is missing",
                )
            if not stat.S_ISREG(audio_status.st_mode) or not stat.S_ISREG(
                metadata_status.st_mode
            ):
                return StemCacheLookup(
                    "corrupt",
                    reason="entry files must be regular, non-symlink files",
                )
            # Bind metadata parsing to the exact descriptor checked above.
            # A concurrent sidecar replacement can no longer pair unrelated
            # metadata with the audio descriptor opened below.
            raw_metadata = _read_verified_metadata_bytes(
                metadata_path,
                metadata_status,
            )
            metadata = _strict_json_load(raw_metadata)
            _validate_metadata(metadata, key)

            source = audio_path.open("rb", buffering=0)
            opened_audio_status = os.fstat(source.fileno())
            if not _same_file_identity(audio_status, opened_audio_status):
                raise _InvalidDocument("audio file changed during lookup")
            if opened_audio_status.st_size != metadata["byte_length"]:
                raise _InvalidDocument(
                    "audio byte length differs from metadata"
                )
            requested_snapshot_root = (
                Path(tempfile.gettempdir())
                if snapshot_directory is None
                else Path(snapshot_directory)
            )
            snapshot_identity, snapshot_volume_id = (
                _capture_verified_snapshot_storage(requested_snapshot_root)
            )
            byte_length = int(metadata["byte_length"])
            snapshot_root = _revalidate_verified_snapshot_storage(
                snapshot_identity,
                snapshot_volume_id,
            )
            try:
                pool = _verified_snapshot_pool_factory()
                lease = pool.reserve_session_scratch(
                    SessionScratchClaim(
                        scratch_bytes=byte_length,
                        scratch_directory=snapshot_root,
                    )
                )
            except MemoryError:
                raise
            except (OSError, TypeError, ValueError, WorkerSlotError):
                # The optional ledger may itself be unavailable.  Prove that
                # the requested storage identity did not cause that failure
                # before retaining the established fail-soft cache fallback.
                _revalidate_verified_snapshot_storage(
                    snapshot_identity,
                    snapshot_volume_id,
                )
                lease = None
            if lease is None:
                _revalidate_verified_snapshot_storage(
                    snapshot_identity,
                    snapshot_volume_id,
                )
                raise OSError(
                    errno.ENOSPC,
                    "verified stem snapshot scratch admission is unavailable",
                )
            snapshot_root = _revalidate_verified_snapshot_storage(
                snapshot_identity,
                snapshot_volume_id,
                lease=lease,
                byte_length=byte_length,
            )
            snapshot = tempfile.TemporaryFile(
                mode="w+b",
                prefix=".tianlai-verified-stem.",
                suffix=".f32le",
                dir=snapshot_root,
            )
            # The anonymous file is now bound to an opened descriptor.  Make
            # sure the directory itself was not exchanged during creation
            # before copying any cache payload into it.
            _revalidate_verified_snapshot_storage(
                snapshot_identity,
                snapshot_volume_id,
                lease=lease,
                byte_length=byte_length,
            )
            if _stream_open_audio_evidence(
                source,
                opened_audio_status,
                byte_length=int(metadata["byte_length"]),
                snapshot=snapshot,
            ) != metadata["audio_sha256"]:
                raise _InvalidDocument("audio digest differs from metadata")
            snapshot_status = os.fstat(snapshot.fileno())
            if (
                not stat.S_ISREG(snapshot_status.st_mode)
                or snapshot_status.st_size != metadata["byte_length"]
            ):
                raise _InvalidDocument(
                    "verified stem snapshot has an invalid length"
                )
            if _stream_open_audio_evidence(
                snapshot,
                snapshot_status,
                byte_length=int(metadata["byte_length"]),
            ) != metadata["audio_sha256"]:
                raise _InvalidDocument(
                    "verified stem snapshot digest differs from metadata"
                )
            _revalidate_verified_snapshot_storage(
                snapshot_identity,
                snapshot_volume_id,
                lease=lease,
                byte_length=byte_length,
            )
            source.close()
            source = None

            record = StemCacheRecord(
                key,
                audio_path,
                metadata_path,
                dict(metadata),
            )
            verified = VerifiedStemSource(
                record,
                snapshot,
                snapshot_status,
                lease,
            )
            snapshot = None
            lease = None
            return StemCacheLookup(
                "hit",
                record=record,
                source=verified,
            )
        except MemoryError:
            raise
        except (OSError, PermissionError) as exc:
            if snapshot_identity is not None and snapshot_volume_id is not None:
                try:
                    _revalidate_verified_snapshot_storage(
                        snapshot_identity,
                        snapshot_volume_id,
                        lease=lease,
                        byte_length=(
                            int(metadata["byte_length"])
                            if lease is not None
                            else None
                        ),
                    )
                except BaseException as identity_error:
                    raise identity_error from exc
            structured_result = StemCacheLookup(
                "unavailable",
                reason=f"cache read failed: {exc}",
            )
        except (_InvalidDocument, _UnsafePath, ValueError) as exc:
            if snapshot_identity is not None and snapshot_volume_id is not None:
                _revalidate_verified_snapshot_storage(
                    snapshot_identity,
                    snapshot_volume_id,
                    lease=lease,
                    byte_length=(
                        int(metadata["byte_length"])
                        if lease is not None
                        else None
                    ),
                )
            structured_result = StemCacheLookup("corrupt", reason=str(exc))
        finally:
            cleanup_error: BaseException | None = None
            if source is not None:
                try:
                    source.close()
                except BaseException as exc:
                    cleanup_error = exc
            snapshot_cleanup_error = _close_snapshot_resources(snapshot, lease)
            if cleanup_error is None:
                cleanup_error = snapshot_cleanup_error
            active_error = sys.exception()
            if active_error is not None:
                _annotate_snapshot_cleanup_error(active_error, cleanup_error)
            elif cleanup_error is not None and structured_result is None:
                raise cleanup_error
            elif cleanup_error is not None and structured_result is not None:
                structured_result = StemCacheLookup(
                    structured_result.status,
                    record=structured_result.record,
                    reason=(
                        f"{structured_result.reason}; cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    ),
                    audio=structured_result.audio,
                    source=structured_result.source,
                )
        assert structured_result is not None
        return structured_result

    def _lookup(
        self,
        key: str,
        *,
        materialise_audio: bool,
        maximum_audio_bytes: int | None = None,
    ) -> StemCacheLookup:
        """Verify an entry, optionally returning its track-sized ndarray."""

        if not _is_cache_key(key):
            return StemCacheLookup("invalid_key", reason="key must be 64 lowercase hex characters")
        try:
            audio_path, metadata_path = self._paths(key, create=False)
            try:
                audio_status = audio_path.lstat()
            except FileNotFoundError:
                audio_status = None
            try:
                metadata_status = metadata_path.lstat()
            except FileNotFoundError:
                metadata_status = None
            if audio_status is None and metadata_status is None:
                return StemCacheLookup("missing", reason="entry does not exist")
            if audio_status is None or metadata_status is None:
                return StemCacheLookup("incomplete", reason="audio or metadata is missing")
            if not stat.S_ISREG(audio_status.st_mode) or not stat.S_ISREG(
                metadata_status.st_mode
            ):
                return StemCacheLookup("corrupt", reason="entry files must be regular, non-symlink files")
            raw_metadata = _read_verified_metadata_bytes(
                metadata_path,
                metadata_status,
            )
            metadata = _strict_json_load(raw_metadata)
            _validate_metadata(metadata, key)
            if (
                materialise_audio
                and maximum_audio_bytes is not None
                and metadata["byte_length"] > maximum_audio_bytes
            ):
                return StemCacheLookup(
                    "too_large",
                    reason="cache audio exceeds the bounded load threshold",
                )
            # Metadata parsing creates a race window.  Revalidate the audio
            # pathname immediately before the track-sized allocation so a
            # replacement cannot use the earlier size check to force an
            # unbounded ``fromfile`` read.
            latest_audio_status = audio_path.lstat()
            if not _same_file_identity(audio_status, latest_audio_status):
                raise _InvalidDocument("audio file changed during lookup")
            if latest_audio_status.st_size != metadata["byte_length"]:
                raise _InvalidDocument("audio byte length differs from metadata")
            expected_samples = metadata["frame_count"] * _CHANNELS
            if materialise_audio:
                # If the pathname changes after the final identity check, cap
                # the allocation at the declared payload plus one sentinel
                # sample.  The sentinel still detects trailing data without
                # admitting an unbounded replacement into memory.
                audio = np.fromfile(
                    audio_path,
                    dtype=_AUDIO_DTYPE,
                    count=expected_samples + 1,
                )
                if audio.size != expected_samples:
                    raise _InvalidDocument(
                        "audio frame count differs from metadata"
                    )
                audio.shape = (metadata["frame_count"], _CHANNELS)
                if (
                    hashlib.sha256(memoryview(audio).cast("B")).hexdigest()
                    != metadata["audio_sha256"]
                ):
                    raise _InvalidDocument(
                        "audio digest differs from metadata"
                    )
                # ``fromfile`` returns owned writable memory rather than a
                # mmap; assigning ``shape`` preserves ownership without a
                # second track-sized bytes allocation.
                if not _all_finite_audio(audio):
                    raise _InvalidDocument(
                        "audio contains non-finite samples"
                    )
            else:
                audio = None
                if _stream_audio_evidence(
                    audio_path,
                    latest_audio_status,
                    byte_length=metadata["byte_length"],
                ) != metadata["audio_sha256"]:
                    raise _InvalidDocument(
                        "audio digest differs from metadata"
                    )
            record = StemCacheRecord(key, audio_path, metadata_path, dict(metadata))
            return StemCacheLookup("hit", record=record, audio=audio)
        except (OSError, PermissionError) as exc:
            return StemCacheLookup("unavailable", reason=f"cache read failed: {exc}")
        except (_InvalidDocument, _UnsafePath, ValueError) as exc:
            return StemCacheLookup("corrupt", reason=str(exc))

    @staticmethod
    def _normalise_audio(buffer: Any) -> np.ndarray:
        array = np.asarray(buffer)
        if array.ndim != 2 or array.shape[1] != _CHANNELS:
            raise ValueError("stem buffer must have shape (frames, 2)")
        if array.dtype.kind != "f" or array.dtype.itemsize != 4:
            raise ValueError("stem buffer must use float32 samples")
        audio = np.ascontiguousarray(array, dtype=_AUDIO_DTYPE)
        if not _all_finite_audio(audio):
            raise ValueError("stem buffer must not contain NaN or infinity")
        return audio

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        # Success consumes the temporary name.  Failure preserves it: a
        # path-based cleanup could otherwise unlink a concurrently installed
        # entry at the same name.
        os.replace(temporary, path)

    @staticmethod
    def _write_audio_atomic(path: Path, audio: np.ndarray) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as target:
            audio.tofile(target)
            target.flush()
            os.fsync(target.fileno())
        # See ``_write_atomic``: never resolve a failed publication by
        # deleting a mutable pathname.
        os.replace(temporary, path)

    def store(
        self,
        key: str,
        buffer: Any,
        *,
        stage: str,
        sample_rate: int,
        peak_voices: int,
        manifest_sha256: str,
    ) -> StemCacheStoreResult:
        """Atomically publish a verified float32 stereo entry.

        The per-key lock is non-blocking.  A concurrent renderer gets ``busy``
        and can render without caching instead of waiting on a cache writer.
        """

        if not _is_cache_key(key):
            return StemCacheStoreResult("invalid_key", reason="key must be 64 lowercase hex characters")
        try:
            audio = self._normalise_audio(buffer)
            if not isinstance(stage, str) or not stage:
                raise ValueError("stage must be a non-empty string")
            if not _is_int(sample_rate, minimum=1):
                raise ValueError("sample_rate must be a positive integer")
            if not _is_int(peak_voices):
                raise ValueError("peak_voices must be a non-negative integer")
            if not _is_sha256(manifest_sha256):
                raise ValueError("manifest_sha256 must be lowercase SHA-256 hex")
        except (TypeError, ValueError) as exc:
            return StemCacheStoreResult("invalid_input", reason=str(exc))

        audio_sha256 = hashlib.sha256(
            memoryview(audio).cast("B")
        ).hexdigest()
        metadata: dict[str, Any] = {
            "format": CACHE_FORMAT,
            "version": CACHE_VERSION,
            "key": key,
            "stage": stage,
            "dtype": "<f4",
            "channels": _CHANNELS,
            "sample_rate": sample_rate,
            "frame_count": int(audio.shape[0]),
            "byte_length": int(audio.nbytes),
            "audio_sha256": audio_sha256,
            "peak_voices": peak_voices,
            "manifest_sha256": manifest_sha256,
        }
        metadata_payload = canonical_json_bytes(metadata)
        try:
            root = self._root(create=True)
            # Re-use the well-tested cross-process lock implementation, while
            # making the target identity this exact cache key.
            with acquire_render_lock(root / ".locks" / key):
                audio_path, metadata_path = self._paths(key, create=True)
                # The freshly rendered ndarray is still owned by the caller.
                # Verify an existing payload in bounded blocks instead of
                # loading a second full stem merely to compare its evidence.
                existing = self._lookup(
                    key,
                    materialise_audio=False,
                )
                if existing.status == "hit":
                    assert existing.record is not None
                    old_digest = existing.record.metadata["audio_sha256"]
                    if old_digest != metadata["audio_sha256"]:
                        return StemCacheStoreResult(
                            "conflict", existing.record, "a valid entry for this key has different audio"
                        )
                    if dict(existing.record.metadata) != metadata:
                        # The freshly rendered audio proves that the current
                        # semantic sidecar belongs to the same bytes.  Repair
                        # only the metadata commit marker; never replace a
                        # valid entry whose audio differs.
                        self._write_atomic(metadata_path, metadata_payload)
                        record = StemCacheRecord(
                            key,
                            audio_path,
                            metadata_path,
                            dict(metadata),
                        )
                        return StemCacheStoreResult(
                            "repaired",
                            record,
                            "semantic metadata repaired for identical audio",
                        )
                    return StemCacheStoreResult("exists", existing.record, "entry already published")
                # Audio is published first.  Metadata is the commit marker and
                # therefore must be replaced last.
                self._write_audio_atomic(audio_path, audio)
                self._write_atomic(metadata_path, metadata_payload)
                record = StemCacheRecord(key, audio_path, metadata_path, dict(metadata))
                return StemCacheStoreResult("stored", record)
        except RenderLockError as exc:
            return StemCacheStoreResult("busy", reason=str(exc))
        except (OSError, PermissionError, _UnsafePath, ValueError) as exc:
            return StemCacheStoreResult("write_error", reason=f"cache write failed: {exc}")


__all__ = (
    "CACHE_FORMAT",
    "CACHE_VERSION",
    "PROCESS_SOURCE_TREE_SHA256",
    "StemCache",
    "StemCacheLookup",
    "StemCacheRecord",
    "StemCacheStoreResult",
    "VerifiedStemSource",
    "build_cache_key",
    "cache_key_for_identity",
    "canonical_cache_key",
    "current_source_tree_matches",
    "source_tree_digest",
)
