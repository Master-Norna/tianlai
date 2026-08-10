"""Safe on-disk cache for rendered stereo stems.

The cache deliberately stores only little-endian float32 stereo frames.  It is
not a general serialisation format: a cache entry is independently verified
before it can be handed back to a renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

import numpy as np

from .canonical_json import canonical_json_bytes
from .render_lock import RenderLockError, acquire_render_lock


CACHE_FORMAT = "tianlai.stem_cache"
CACHE_VERSION = 1
_AUDIO_DTYPE = np.dtype("<f4")
_CHANNELS = 2
_KEY_LENGTH = 64
_MAX_METADATA_BYTES = 64 * 1024
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


@dataclass(frozen=True, slots=True)
class StemCacheRecord:
    """Verified cache entry information, including its on-disk locations."""

    key: str
    audio_path: Path
    metadata_path: Path
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StemCacheLookup:
    """A non-throwing cache lookup result.

    ``status`` is one of ``hit``, ``missing``, ``incomplete``, ``corrupt``,
    ``unavailable`` or ``invalid_key``.  Only ``hit`` carries audio.
    """

    status: str
    record: StemCacheRecord | None = None
    reason: str | None = None
    audio: np.ndarray | None = None

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

    canonical = canonical_json_bytes(_canonical_identity(identity))
    return hashlib.sha256(canonical).hexdigest()


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
    paths = sorted(package.rglob("*.py"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"source tree contains a non-regular Python file: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
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

    @staticmethod
    def _regular_file(path: Path) -> bool:
        return not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)

    def load(self, key: str) -> StemCacheLookup:
        """Load and fully verify a stem.  Failures are reported, never raised."""

        if not _is_cache_key(key):
            return StemCacheLookup("invalid_key", reason="key must be 64 lowercase hex characters")
        try:
            audio_path, metadata_path = self._paths(key, create=False)
            audio_exists = audio_path.exists() or audio_path.is_symlink()
            metadata_exists = metadata_path.exists() or metadata_path.is_symlink()
            if not audio_exists and not metadata_exists:
                return StemCacheLookup("missing", reason="entry does not exist")
            if not audio_exists or not metadata_exists:
                return StemCacheLookup("incomplete", reason="audio or metadata is missing")
            if not self._regular_file(audio_path) or not self._regular_file(metadata_path):
                return StemCacheLookup("corrupt", reason="entry files must be regular, non-symlink files")
            if metadata_path.stat().st_size > _MAX_METADATA_BYTES:
                raise _InvalidDocument("metadata exceeds the cache sidecar size limit")
            metadata = _strict_json_load(metadata_path.read_bytes())
            _validate_metadata(metadata, key)
            if audio_path.stat().st_size != metadata["byte_length"]:
                raise _InvalidDocument("audio byte length differs from metadata")
            audio = np.fromfile(audio_path, dtype=_AUDIO_DTYPE)
            if audio.size != metadata["frame_count"] * _CHANNELS:
                raise _InvalidDocument("audio frame count differs from metadata")
            audio.shape = (metadata["frame_count"], _CHANNELS)
            if (
                hashlib.sha256(memoryview(audio).cast("B")).hexdigest()
                != metadata["audio_sha256"]
            ):
                raise _InvalidDocument("audio digest differs from metadata")
            # ``fromfile`` returns owned writable memory rather than a mmap;
            # reshaping by assigning ``shape`` preserves that ownership while
            # avoiding a second track-sized bytes allocation.
            if not bool(np.isfinite(audio).all()):
                raise _InvalidDocument("audio contains non-finite samples")
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
        if not bool(np.isfinite(audio).all()):
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
                existing = self.load(key)
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
    "build_cache_key",
    "cache_key_for_identity",
    "canonical_cache_key",
    "current_source_tree_matches",
    "source_tree_digest",
)
