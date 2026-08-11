"""Fail-closed content-addressed cache for collaboration analysis payloads.

The cache contains diagnostic JSON only.  It is never authoritative audio
provenance: every lookup recomputes the canonical identity key, verifies the
payload digest and rejects unexpected files or paths before returning data.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Any, Mapping
from weakref import WeakValueDictionary

from .canonical_json import canonical_json_bytes
from .render_lock import RenderLockError, acquire_render_lock
from .stem_cache import _canonical_cache_identity_bytes


CACHE_FORMAT = "tianlai.collaboration_analysis_cache"
CACHE_VERSION = 1
_KEY_LENGTH = 64
_MAX_ENTRY_BYTES = 32 * 1024 * 1024
_ENTRY_KEYS = frozenset(
    {
        "format",
        "version",
        "key",
        "kind",
        "identity",
        "payload",
        "payload_canonical_sha256",
    }
)
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: WeakValueDictionary[str, threading.Lock] = (
    WeakValueDictionary()
)


def _process_key_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


class _InvalidDocument(ValueError):
    """An untrusted analysis-cache document failed verification."""


class _UnsafePath(ValueError):
    """An analysis-cache path contains an unexpected symbolic link."""


@dataclass(frozen=True, slots=True)
class AnalysisCacheLookup:
    status: str
    payload: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def hit(self) -> bool:
        return self.status == "hit"


@dataclass(frozen=True, slots=True)
class AnalysisCacheStoreResult:
    status: str
    reason: str | None = None


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _KEY_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _InvalidDocument(f"duplicate JSON key: {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise _InvalidDocument(f"non-finite JSON value: {value}")


def _strict_json(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _InvalidDocument,
        RecursionError,
    ) as exc:
        raise _InvalidDocument("entry is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise _InvalidDocument("entry root is not an object")
    return document


def _validate_entry(
    document: Mapping[str, Any],
    *,
    key: str,
    kind: str,
    identity_canonical: bytes,
) -> tuple[dict[str, Any], bytes]:
    if set(document) != _ENTRY_KEYS:
        raise _InvalidDocument("entry keys do not exactly match the schema")
    if (
        document["format"] != CACHE_FORMAT
        or document["version"] != CACHE_VERSION
    ):
        raise _InvalidDocument("unsupported cache format")
    if document["key"] != key or not _is_sha256(document["key"]):
        raise _InvalidDocument("entry key is invalid")
    if document["kind"] != kind:
        raise _InvalidDocument("entry kind differs from the requested kind")
    stored_identity = document["identity"]
    if not isinstance(stored_identity, dict):
        raise _InvalidDocument("entry identity is not an object")
    stored_identity_canonical = _canonical_cache_identity_bytes(
        stored_identity
    )
    if hashlib.sha256(stored_identity_canonical).hexdigest() != key:
        raise _InvalidDocument("stored identity does not reproduce the key")
    if stored_identity_canonical != identity_canonical:
        raise _InvalidDocument("stored identity differs from the live identity")
    payload = document["payload"]
    if not isinstance(payload, dict):
        raise _InvalidDocument("entry payload is not an object")
    expected_payload_hash = document["payload_canonical_sha256"]
    payload_canonical = canonical_json_bytes(payload)
    actual_payload_hash = hashlib.sha256(payload_canonical).hexdigest()
    if (
        not _is_sha256(expected_payload_hash)
        or expected_payload_hash != actual_payload_hash
    ):
        raise _InvalidDocument("payload digest does not match")
    return dict(payload), payload_canonical


def _entry_fragments(
    *,
    key: str,
    kind: str,
    identity_canonical: bytes,
    payload_canonical: bytes,
    payload_hash: str,
) -> tuple[bytes, ...]:
    """Return exact canonical entry pieces without encoding payload twice."""

    return (
        b'{"format":',
        canonical_json_bytes(CACHE_FORMAT),
        b',"identity":',
        identity_canonical,
        b',"key":"',
        key.encode("ascii"),
        b'","kind":',
        canonical_json_bytes(kind),
        b',"payload":',
        payload_canonical,
        b',"payload_canonical_sha256":"',
        payload_hash.encode("ascii"),
        b'","version":',
        str(CACHE_VERSION).encode("ascii"),
        b"}",
    )


class CollaborationAnalysisCache:
    """JSON cache rooted at ``root/v1/<prefix>/<key>.json``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _root(self, *, create: bool) -> Path:
        if self.root.is_symlink():
            raise _UnsafePath("cache root must not be a symbolic link")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.exists() and (
            self.root.is_symlink() or not self.root.is_dir()
        ):
            raise _UnsafePath("cache root must be a regular directory")
        return self.root.resolve(strict=False)

    def _path(self, key: str, *, create: bool) -> Path:
        root = self._root(create=create)
        version = root / "v1"
        directory = version / key[:2]
        for ancestor in (version, directory):
            if ancestor.exists() and ancestor.is_symlink():
                raise _UnsafePath(
                    "cache entry directory must not be a symbolic link"
                )
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise _UnsafePath("cache entry directory must be regular")
        path = directory / f"{key}.json"
        if not path.is_relative_to(root):
            raise _UnsafePath("cache entry escaped cache root")
        return path

    def load(
        self,
        identity: Mapping[str, Any],
        *,
        kind: str,
    ) -> AnalysisCacheLookup:
        """Load one fully verified payload without propagating cache errors."""

        try:
            if not isinstance(kind, str) or not kind:
                raise ValueError("kind must be a non-empty string")
            identity_canonical = _canonical_cache_identity_bytes(identity)
            key = hashlib.sha256(identity_canonical).hexdigest()
            return self._load_canonical(
                key=key,
                identity_canonical=identity_canonical,
                kind=kind,
            )
        except (
            TypeError,
            ValueError,
            _InvalidDocument,
            _UnsafePath,
            RecursionError,
        ) as exc:
            return AnalysisCacheLookup("corrupt", reason=str(exc))

    def _load_canonical(
        self,
        *,
        key: str,
        identity_canonical: bytes,
        kind: str,
    ) -> AnalysisCacheLookup:
        """Load using an identity already canonicalised by the caller."""

        lookup, _payload_canonical = self._load_canonical_entry(
            key=key,
            identity_canonical=identity_canonical,
            kind=kind,
        )
        return lookup

    def _load_canonical_entry(
        self,
        *,
        key: str,
        identity_canonical: bytes,
        kind: str,
    ) -> tuple[AnalysisCacheLookup, bytes | None]:
        """Load and retain verified payload bytes for store comparisons."""

        try:
            path = self._path(key, create=False)
            try:
                status = path.lstat()
            except FileNotFoundError:
                return (
                    AnalysisCacheLookup("missing", reason="entry absent"),
                    None,
                )
            if not stat.S_ISREG(status.st_mode):
                return (
                    AnalysisCacheLookup(
                        "corrupt",
                        reason="entry must be a regular non-symlink file",
                    ),
                    None,
                )
            if status.st_size > _MAX_ENTRY_BYTES:
                raise _InvalidDocument("entry exceeds the size limit")
            payload, payload_canonical = _validate_entry(
                _strict_json(path.read_bytes()),
                key=key,
                kind=kind,
                identity_canonical=identity_canonical,
            )
            return (
                AnalysisCacheLookup("hit", payload=payload),
                payload_canonical,
            )
        except (OSError, PermissionError) as exc:
            return (
                AnalysisCacheLookup(
                    "unavailable",
                    reason=f"cache read failed: {exc}",
                ),
                None,
            )
        except (
            TypeError,
            ValueError,
            _InvalidDocument,
            _UnsafePath,
            RecursionError,
        ) as exc:
            return AnalysisCacheLookup("corrupt", reason=str(exc)), None

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        # The temporary name disappears on success.  Preserve it on failure;
        # an unlink-by-name cleanup could target a racing replacement rather
        # than the file descriptor created above.
        os.replace(temporary, path)

    def store(
        self,
        identity: Mapping[str, Any],
        *,
        kind: str,
        payload: Mapping[str, Any],
    ) -> AnalysisCacheStoreResult:
        """Atomically publish JSON; preserve a conflicting valid entry."""

        try:
            if not isinstance(kind, str) or not kind:
                raise ValueError("kind must be a non-empty string")
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be an object")
            identity_document = dict(identity)
            payload_document = dict(payload)
            # Store documents must already be JSON-compatible.  This retains
            # the previous input contract while allowing all later identity
            # checks to reuse the same exact bytes.
            identity_canonical = _canonical_cache_identity_bytes(
                identity_document
            )
            if canonical_json_bytes(identity_document) != identity_canonical:
                raise ValueError("identity is not canonical JSON")
            key = hashlib.sha256(identity_canonical).hexdigest()
            payload_canonical = canonical_json_bytes(payload_document)
            payload_hash = hashlib.sha256(payload_canonical).hexdigest()
            fragments = _entry_fragments(
                key=key,
                kind=kind,
                identity_canonical=identity_canonical,
                payload_canonical=payload_canonical,
                payload_hash=payload_hash,
            )
            if sum(map(len, fragments)) > _MAX_ENTRY_BYTES:
                raise ValueError("entry exceeds the size limit")
        except (TypeError, ValueError, RecursionError) as exc:
            return AnalysisCacheStoreResult("invalid_input", str(exc))

        try:
            root = self._root(create=True)
            process_lock = _process_key_lock(
                f"{os.path.normcase(str(root))}:{key}"
            )
            if not process_lock.acquire(blocking=False):
                return AnalysisCacheStoreResult(
                    "busy",
                    "this process is already publishing the entry",
                )
            try:
                with acquire_render_lock(root / ".locks" / key):
                    path = self._path(key, create=True)
                    existing, existing_payload_canonical = (
                        self._load_canonical_entry(
                            key=key,
                            identity_canonical=identity_canonical,
                            kind=kind,
                        )
                    )
                    if existing.hit:
                        assert existing.payload is not None
                        if existing_payload_canonical != payload_canonical:
                            return AnalysisCacheStoreResult(
                                "conflict",
                                "a valid entry for this identity has a "
                                "different payload",
                            )
                        return AnalysisCacheStoreResult(
                            "exists",
                            "entry already published",
                        )
                    encoded = b"".join(fragments)
                    self._write_atomic(path, encoded)
                    verified = self._load_canonical(
                        key=key,
                        identity_canonical=identity_canonical,
                        kind=kind,
                    )
                    if not verified.hit:
                        return AnalysisCacheStoreResult(
                            "write_error",
                            "published entry did not verify",
                        )
                    return AnalysisCacheStoreResult(
                        "repaired"
                        if existing.status == "corrupt"
                        else "stored"
                    )
            finally:
                process_lock.release()
        except RenderLockError as exc:
            return AnalysisCacheStoreResult("busy", str(exc))
        except (
            OSError,
            PermissionError,
            ValueError,
            _UnsafePath,
        ) as exc:
            return AnalysisCacheStoreResult(
                "write_error",
                f"cache write failed: {exc}",
            )


__all__ = (
    "CACHE_FORMAT",
    "CACHE_VERSION",
    "AnalysisCacheLookup",
    "AnalysisCacheStoreResult",
    "CollaborationAnalysisCache",
)
