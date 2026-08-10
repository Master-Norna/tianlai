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

from .canonical_json import canonical_json_bytes
from .render_lock import RenderLockError, acquire_render_lock
from .stem_cache import build_cache_key


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
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


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
    identity: Mapping[str, Any],
) -> dict[str, Any]:
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
    if build_cache_key(stored_identity) != key:
        raise _InvalidDocument("stored identity does not reproduce the key")
    if canonical_json_bytes(stored_identity) != canonical_json_bytes(identity):
        raise _InvalidDocument("stored identity differs from the live identity")
    payload = document["payload"]
    if not isinstance(payload, dict):
        raise _InvalidDocument("entry payload is not an object")
    expected_payload_hash = document["payload_canonical_sha256"]
    actual_payload_hash = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    if (
        not _is_sha256(expected_payload_hash)
        or expected_payload_hash != actual_payload_hash
    ):
        raise _InvalidDocument("payload digest does not match")
    return dict(payload)


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

    @staticmethod
    def _regular_file(path: Path) -> bool:
        return not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)

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
            key = build_cache_key(identity)
            path = self._path(key, create=False)
            if not (path.exists() or path.is_symlink()):
                return AnalysisCacheLookup("missing", reason="entry absent")
            if not self._regular_file(path):
                return AnalysisCacheLookup(
                    "corrupt",
                    reason="entry must be a regular non-symlink file",
                )
            if path.stat().st_size > _MAX_ENTRY_BYTES:
                raise _InvalidDocument("entry exceeds the size limit")
            payload = _validate_entry(
                _strict_json(path.read_bytes()),
                key=key,
                kind=kind,
                identity=identity,
            )
            return AnalysisCacheLookup("hit", payload=payload)
        except (OSError, PermissionError) as exc:
            return AnalysisCacheLookup(
                "unavailable",
                reason=f"cache read failed: {exc}",
            )
        except (
            TypeError,
            ValueError,
            _InvalidDocument,
            _UnsafePath,
            RecursionError,
        ) as exc:
            return AnalysisCacheLookup("corrupt", reason=str(exc))

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
            key = build_cache_key(identity_document)
            payload_hash = hashlib.sha256(
                canonical_json_bytes(payload_document)
            ).hexdigest()
            document = {
                "format": CACHE_FORMAT,
                "version": CACHE_VERSION,
                "key": key,
                "kind": kind,
                "identity": identity_document,
                "payload": payload_document,
                "payload_canonical_sha256": payload_hash,
            }
            encoded = canonical_json_bytes(document)
            if len(encoded) > _MAX_ENTRY_BYTES:
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
                    existing = self.load(identity_document, kind=kind)
                    if existing.hit:
                        assert existing.payload is not None
                        if (
                            canonical_json_bytes(existing.payload)
                            != canonical_json_bytes(payload_document)
                        ):
                            return AnalysisCacheStoreResult(
                                "conflict",
                                "a valid entry for this identity has a "
                                "different payload",
                            )
                        return AnalysisCacheStoreResult(
                            "exists",
                            "entry already published",
                        )
                    self._write_atomic(path, encoded)
                    verified = self.load(identity_document, kind=kind)
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
