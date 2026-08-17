"""Sealed roster capability and manifest-generation snapshots for Score v2.

This module captures the local inputs that a future Score-v2 capability
adapter must bind before it can reason about execution.  It deliberately does
not emit renderer events and it does not compute the comparatively expensive
runtime/sample fingerprint.  In particular, a manifest that names a local
``implementation`` is retained for audit but is marked fail-closed.

The boundary keeps three identities separate:

* a descriptor-bound raw manifest generation (bytes, file identity and hash),
* the canonical :class:`InstrumentCapability` projection resolved from it,
* the effective factory manifest for each roster executor after overrides.

All caller-owned containers are canonicalized before they are retained.  The
returned artifact is tuple/bytes-backed, content addressed, and revalidates
its complete seal on every public projection.  Files can later be revalidated
against the exact captured generation without trusting a pathname-only check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes
from .capability import InstrumentCapability, read_capability
from .instrument import factory_manifest_sha256
from .plain_file import (
    PlainFileIdentity,
    read_plain_file_bytes,
    revalidate_plain_file,
)
from .render_lock import (
    PlainDirectoryIdentity,
    capture_plain_directory,
    revalidate_plain_directory,
)
from .roster import (
    Executor,
    Roster,
    _OVERRIDE_ALLOWED_FIELDS,
    _parse_overrides,
)


SCORE_V2_CAPABILITY_SOURCE_KIND = "tianlai.score_v2_capability_source"
SCORE_V2_CAPABILITY_SOURCE_SCHEMA_VERSION = 1
SCORE_V2_CAPABILITY_SOURCE_CONTRACT = (
    "score-v2-capability-source-not-render-authority"
)

DEFAULT_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HARD_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_EXECUTORS = 512
HARD_MAX_EXECUTORS = 4096
MAX_CAPABILITY_ARTIFACT_BYTES = 32 * 1024 * 1024

RUNTIME_FINGERPRINT_STATUS = "not_captured"
_HEX_DIGITS = frozenset("0123456789abcdef")


class ScoreV2CapabilitySourceError(ValueError):
    """A stable, non-reflective capability-source boundary failure."""

    def __init__(
        self,
        code: str,
        *,
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.message_key = f"scoreV2CapabilitySource.{code.replace('.', '_')}"
        self.actual = actual
        self.limit = limit
        super().__init__(code)


class ScoreV2DirectoryGeneration(NamedTuple):
    """JSON-safe projection of one captured plain-directory identity."""

    path: str
    device: int
    inode: int

    @classmethod
    def from_plain(
        cls,
        identity: PlainDirectoryIdentity,
    ) -> "ScoreV2DirectoryGeneration":
        return cls(
            path=str(identity.path),
            device=identity.device,
            inode=identity.inode,
        )

    def as_plain(self) -> PlainDirectoryIdentity:
        return PlainDirectoryIdentity(
            path=Path(self.path),
            device=self.device,
            inode=self.inode,
        )

    def to_dict(self) -> dict[str, str]:
        # Filesystem IDs routinely exceed JavaScript's exact integer range.
        return {
            "path": self.path,
            "device": str(self.device),
            "inode": str(self.inode),
        }


class ScoreV2FileGeneration(NamedTuple):
    """JSON-safe projection of one captured plain-file identity."""

    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    parent: ScoreV2DirectoryGeneration

    @classmethod
    def from_plain(
        cls,
        identity: PlainFileIdentity,
    ) -> "ScoreV2FileGeneration":
        return cls(
            path=str(identity.path),
            device=identity.device,
            inode=identity.inode,
            size=identity.size,
            modified_ns=identity.modified_ns,
            changed_ns=identity.changed_ns,
            parent=ScoreV2DirectoryGeneration.from_plain(
                identity.parent_identity
            ),
        )

    def as_plain(self) -> PlainFileIdentity:
        return PlainFileIdentity(
            path=Path(self.path),
            device=self.device,
            inode=self.inode,
            size=self.size,
            modified_ns=self.modified_ns,
            changed_ns=self.changed_ns,
            parent_identity=self.parent.as_plain(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": str(self.device),
            "inode": str(self.inode),
            "size": self.size,
            "modified_ns": str(self.modified_ns),
            "changed_ns": str(self.changed_ns),
            "parent": self.parent.to_dict(),
        }


class ScoreV2ManifestGeneration(NamedTuple):
    """One immutable descriptor-bound manifest source generation."""

    source_sha256: str
    manifest_path: str
    file_identity: ScoreV2FileGeneration
    raw_bytes: bytes
    raw_sha256: str
    manifest_canonical_bytes: bytes
    manifest_canonical_sha256: str
    custom_implementation_blocked: bool

    def manifest_copy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "manifest_path": self.manifest_path,
            "file_identity": self.file_identity.to_dict(),
            "raw_bytes_size": len(self.raw_bytes),
            "raw_sha256": self.raw_sha256,
            "manifest_canonical_sha256": self.manifest_canonical_sha256,
            "custom_implementation_blocked": (
                self.custom_implementation_blocked
            ),
        }


class ScoreV2CapabilityProjection(NamedTuple):
    """One detached canonical projection of InstrumentCapability."""

    manifest_source_sha256: str
    instrument_relative_path: str
    canonical_bytes: bytes
    canonical_sha256: str

    def projection_copy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_source_sha256": self.manifest_source_sha256,
            "instrument_relative_path": self.instrument_relative_path,
            "canonical_sha256": self.canonical_sha256,
            "projection": self.projection_copy(),
        }


class ScoreV2ExecutorCapabilityBinding(NamedTuple):
    """Effective manifest and capability identity for one roster executor."""

    executor_order: int
    executor_id: str
    part_id: str
    instrument_relative_path: str
    manifest_source_sha256: str
    capability_projection_sha256: str
    overrides: tuple[tuple[str, int | float | str | bool], ...]
    effective_manifest_canonical_sha256: str
    effective_manifest_sha256: str
    custom_implementation_blocked: bool
    execution_eligibility: str
    runtime_fingerprint_status: str
    runtime_fingerprint_sha256: None

    def to_dict(self) -> dict[str, object]:
        return {
            "executor_order": self.executor_order,
            "executor_id": self.executor_id,
            "part_id": self.part_id,
            "instrument_relative_path": self.instrument_relative_path,
            "manifest_source_sha256": self.manifest_source_sha256,
            "capability_projection_sha256": (
                self.capability_projection_sha256
            ),
            "overrides": dict(self.overrides),
            "effective_manifest_canonical_sha256": (
                self.effective_manifest_canonical_sha256
            ),
            # This is the domain-separated identity already consumed by the
            # instrument factory and raw-stem cache.
            "effective_manifest_sha256": self.effective_manifest_sha256,
            "custom_implementation_blocked": (
                self.custom_implementation_blocked
            ),
            "execution_eligibility": self.execution_eligibility,
            "runtime_fingerprint_status": self.runtime_fingerprint_status,
            "runtime_fingerprint_sha256": self.runtime_fingerprint_sha256,
        }


def _artifact_document(
    *,
    catalogue_root: ScoreV2DirectoryGeneration,
    roster_projection_bytes: bytes,
    roster_projection_sha256: str,
    manifest_generations: tuple[ScoreV2ManifestGeneration, ...],
    capability_projections: tuple[ScoreV2CapabilityProjection, ...],
    executor_bindings: tuple[ScoreV2ExecutorCapabilityBinding, ...],
) -> dict[str, object]:
    try:
        roster_projection = json.loads(roster_projection_bytes)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    if type(roster_projection) is not dict:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return {
        "kind": SCORE_V2_CAPABILITY_SOURCE_KIND,
        "schema_version": SCORE_V2_CAPABILITY_SOURCE_SCHEMA_VERSION,
        "contract": SCORE_V2_CAPABILITY_SOURCE_CONTRACT,
        "catalogue_root": catalogue_root.to_dict(),
        "roster_projection_sha256": roster_projection_sha256,
        "roster_projection": roster_projection,
        "runtime_fingerprint_policy": RUNTIME_FINGERPRINT_STATUS,
        "manifest_generations": [
            source.to_dict() for source in manifest_generations
        ],
        "capability_projections": [
            projection.to_dict() for projection in capability_projections
        ],
        "executor_bindings": [
            binding.to_dict() for binding in executor_bindings
        ],
    }


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2CapabilitySourceSnapshot:
    """One sealed generation of all capability inputs selected by a roster."""

    catalogue_root: ScoreV2DirectoryGeneration
    roster_projection_sha256: str
    manifest_generations: tuple[ScoreV2ManifestGeneration, ...]
    capability_projections: tuple[ScoreV2CapabilityProjection, ...]
    executor_bindings: tuple[ScoreV2ExecutorCapabilityBinding, ...]
    _roster_projection_bytes: bytes = field(repr=False, compare=False)
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2CapabilitySourceSnapshot cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2CapabilitySourceSnapshot must be created by "
            "capture_score_v2_capability_sources"
        )

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            seal = self._identity_seal
        except AttributeError as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if type(seal) is not tuple or len(seal) != 9:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        try:
            (
                catalogue_root,
                roster_projection_sha256,
                manifest_generations,
                capability_projections,
                executor_bindings,
                roster_projection_bytes,
                canonical_bytes_value,
                artifact_sha256,
                contract,
            ) = seal
        except (TypeError, ValueError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if (
            self.catalogue_root is not catalogue_root
            or type(self.roster_projection_sha256) is not str
            or self.roster_projection_sha256 != roster_projection_sha256
            or self.manifest_generations is not manifest_generations
            or self.capability_projections is not capability_projections
            or self.executor_bindings is not executor_bindings
            or self._roster_projection_bytes is not roster_projection_bytes
            or self._canonical_bytes is not canonical_bytes_value
            or type(self._artifact_sha256) is not str
            or self._artifact_sha256 != artifact_sha256
            or contract != SCORE_V2_CAPABILITY_SOURCE_CONTRACT
            or type(roster_projection_bytes) is not bytes
            or type(canonical_bytes_value) is not bytes
            or not _is_sha256(artifact_sha256)
            or hashlib.sha256(canonical_bytes_value).hexdigest()
            != artifact_sha256
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        try:
            _validate_snapshot_values(
                catalogue_root=catalogue_root,
                roster_projection_bytes=roster_projection_bytes,
                roster_projection_sha256=roster_projection_sha256,
                manifest_generations=manifest_generations,
                capability_projections=capability_projections,
                executor_bindings=executor_bindings,
            )
            rebuilt = canonical_json_bytes(
                _artifact_document(
                    catalogue_root=catalogue_root,
                    roster_projection_bytes=roster_projection_bytes,
                    roster_projection_sha256=roster_projection_sha256,
                    manifest_generations=manifest_generations,
                    capability_projections=capability_projections,
                    executor_bindings=executor_bindings,
                )
            )
        except ScoreV2CapabilitySourceError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if rebuilt != canonical_bytes_value:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        return canonical_bytes_value

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_artifact_bytes()

    @property
    def canonical_json_bytes(self) -> bytes:
        return self._trusted_artifact_bytes()

    @property
    def canonical_json_bytes_size(self) -> int:
        return len(self._trusted_artifact_bytes())

    @property
    def artifact_sha256(self) -> str:
        self._trusted_artifact_bytes()
        return self._artifact_sha256

    def roster_projection_copy(self) -> dict[str, Any]:
        self._trusted_artifact_bytes()
        try:
            value = json.loads(self._roster_projection_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        return value

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_artifact_bytes())
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        return value

    def revalidate_sources(self) -> None:
        """Fail unless every path still names the exact captured generation."""

        self._trusted_artifact_bytes()
        try:
            revalidate_plain_directory(self.catalogue_root.as_plain())
            for source in self.manifest_generations:
                # First use the inexpensive identity-only boundary, then make
                # a fresh descriptor-bound read and compare the bytes as well.
                revalidate_plain_file(source.file_identity.as_plain())
                current_identity, current_bytes = read_plain_file_bytes(
                    source.manifest_path,
                    maximum_bytes=max(1, len(source.raw_bytes)),
                )
                if (
                    ScoreV2FileGeneration.from_plain(current_identity)
                    != source.file_identity
                    or current_bytes != source.raw_bytes
                    or hashlib.sha256(current_bytes).hexdigest()
                    != source.raw_sha256
                ):
                    raise ScoreV2CapabilitySourceError(
                        "capability_source.source_generation_changed"
                    )
            revalidate_plain_directory(self.catalogue_root.as_plain())
        except ScoreV2CapabilitySourceError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.source_generation_changed"
            ) from exc


class _ExecutorFact(NamedTuple):
    order: int
    executor_id: str
    part_id: str
    capability: InstrumentCapability
    manifest_lookup_key: str
    manifest_requested_path: str
    instrument_relative_path: str
    capability_projection_bytes: bytes
    overrides: tuple[tuple[str, int | float | str | bool], ...]
    defer_onset_evidence: bool


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _active_limits(maximum_bytes: int) -> AuthoringJsonLimits:
    return AuthoringJsonLimits(
        max_document_bytes=maximum_bytes,
        max_depth=128,
        max_nodes=1_000_000,
        max_string_bytes=min(maximum_bytes, 1024 * 1024),
        max_array_items=250_000,
        max_object_members=65_536,
    )


def _require_resource_limits(
    maximum_manifest_bytes: object,
    maximum_executors: object,
) -> tuple[int, int]:
    if (
        type(maximum_manifest_bytes) is not int
        or not 1 <= maximum_manifest_bytes <= HARD_MAX_MANIFEST_BYTES
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_manifest_byte_limit",
            limit=HARD_MAX_MANIFEST_BYTES,
        )
    if (
        type(maximum_executors) is not int
        or not 1 <= maximum_executors <= HARD_MAX_EXECUTORS
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_executor_limit",
            limit=HARD_MAX_EXECUTORS,
        )
    return maximum_manifest_bytes, maximum_executors


def _translate_json_error(
    prefix: str,
    error: AuthoringJsonError,
) -> ScoreV2CapabilitySourceError:
    return ScoreV2CapabilitySourceError(
        f"capability_source.{prefix}.{error.code}",
        actual=error.actual,
        limit=error.limit,
    )


def _capture_json_object(
    document: object,
    *,
    limits: AuthoringJsonLimits,
    error_prefix: str,
) -> tuple[dict[str, Any], bytes]:
    if type(document) is not dict:
        raise ScoreV2CapabilitySourceError(
            f"capability_source.{error_prefix}.object_required"
        )
    try:
        payload = bounded_canonical_json_bytes(
            document,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
        detached = strict_json_loads(
            payload,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _translate_json_error(error_prefix, exc) from exc
    except (
        IndexError,
        KeyError,
        RecursionError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise ScoreV2CapabilitySourceError(
            f"capability_source.{error_prefix}.capture_failed"
        ) from exc
    if type(detached) is not dict:
        raise ScoreV2CapabilitySourceError(
            f"capability_source.{error_prefix}.object_required"
        )
    return detached, payload


def _strict_manifest_document(
    payload: bytes,
    *,
    limits: AuthoringJsonLimits,
) -> tuple[dict[str, Any], bytes]:
    try:
        detached = strict_json_loads(
            payload,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
        if type(detached) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.manifest.object_required"
            )
        canonical = bounded_canonical_json_bytes(
            detached,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _translate_json_error("manifest", exc) from exc
    return detached, canonical


def _normal_path_key(value: str) -> str:
    try:
        absolute = os.path.abspath(os.fspath(value))
    except (OSError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_manifest_path"
        ) from exc
    return os.path.normcase(os.path.normpath(absolute))


def _capture_overrides(
    executor: Executor,
    *,
    limits: AuthoringJsonLimits,
) -> tuple[tuple[str, int | float | str | bool], ...]:
    raw = executor.overrides
    if type(raw) is not tuple:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_executor_overrides"
        )
    document: dict[str, int | float | str | bool] = {}
    for item in raw:
        if type(item) is not tuple or len(item) != 2:
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_executor_overrides"
            )
        key, value = item
        if (
            type(key) is not str
            or not key
            or key not in _OVERRIDE_ALLOWED_FIELDS
            or key in document
            or type(value) not in (int, float, str, bool)
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_executor_overrides"
            )
        document[key] = value
    try:
        normalized = _parse_overrides(
            document,
            "score-v2 capability source",
        )
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_executor_overrides"
        ) from exc
    detached, _payload = _capture_json_object(
        dict(normalized),
        limits=limits,
        error_prefix="executor_overrides",
    )
    return tuple(
        (key, detached[key])
        for key in sorted(detached)
    )  # type: ignore[misc]


def _capability_projection_bytes(
    capability: InstrumentCapability,
    *,
    limits: AuthoringJsonLimits,
) -> bytes:
    try:
        InstrumentCapability.__post_init__(capability)
        document = InstrumentCapability.to_dict(capability)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_capability"
        ) from exc
    _detached, payload = _capture_json_object(
        document,
        limits=limits,
        error_prefix="capability_projection",
    )
    return payload


def _capture_executor_facts(
    roster: Roster,
    *,
    limits: AuthoringJsonLimits,
    maximum_executors: int,
) -> tuple[tuple[_ExecutorFact, ...], bytes]:
    if type(roster) is not Roster:
        raise ScoreV2CapabilitySourceError(
            "capability_source.roster_required"
        )
    executors = roster.executors
    if type(executors) is not tuple or not executors:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_roster"
        )
    if len(executors) > maximum_executors:
        raise ScoreV2CapabilitySourceError(
            "capability_source.too_many_executors",
            actual=len(executors),
            limit=maximum_executors,
        )
    try:
        roster_document = Roster.to_dict(roster)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.invalid_roster"
        ) from exc
    _roster_detached, roster_bytes = _capture_json_object(
        roster_document,
        limits=limits,
        error_prefix="roster_projection",
    )

    facts: list[_ExecutorFact] = []
    seen_executor_ids: set[str] = set()
    for order, executor in enumerate(executors):
        if type(executor) is not Executor:
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_executor"
            )
        capability = executor.capability
        if type(capability) is not InstrumentCapability:
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_capability"
            )
        if (
            type(executor.executor_id) is not str
            or not executor.executor_id
            or executor.executor_id in seen_executor_ids
            or type(executor.part_id) is not str
            or not executor.part_id
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_executor"
            )
        seen_executor_ids.add(executor.executor_id)
        if (
            type(capability.manifest_path) is not str
            or not capability.manifest_path
            or type(capability.relative_path) is not str
            or not capability.relative_path
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_capability"
            )
        facts.append(
            _ExecutorFact(
                order=order,
                executor_id=executor.executor_id,
                part_id=executor.part_id,
                capability=capability,
                manifest_lookup_key=_normal_path_key(
                    capability.manifest_path
                ),
                manifest_requested_path=capability.manifest_path,
                instrument_relative_path=capability.relative_path,
                capability_projection_bytes=_capability_projection_bytes(
                    capability,
                    limits=limits,
                ),
                overrides=_capture_overrides(executor, limits=limits),
                defer_onset_evidence=(
                    capability.onset_evidence_path is not None
                    and not capability.articulation_onsets
                ),
            )
        )
    # Detect an ordinary concurrent frozen-dataclass bypass.  The detached
    # facts above remain authoritative even if the caller mutates afterwards.
    if roster.executors is not executors:
        raise ScoreV2CapabilitySourceError(
            "capability_source.roster_changed_during_capture"
        )
    return tuple(facts), roster_bytes


def _manifest_source_document(
    source: ScoreV2ManifestGeneration,
    *,
    include_source_sha256: bool,
) -> dict[str, object]:
    document = source.to_dict()
    if not include_source_sha256:
        document.pop("source_sha256")
    return document


def _new_manifest_generation(
    identity: PlainFileIdentity,
    raw_bytes: bytes,
    manifest_document: dict[str, Any],
    manifest_canonical_bytes: bytes,
) -> ScoreV2ManifestGeneration:
    file_identity = ScoreV2FileGeneration.from_plain(identity)
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    manifest_canonical_sha256 = hashlib.sha256(
        manifest_canonical_bytes
    ).hexdigest()
    custom_blocked = manifest_document.get("implementation") is not None
    provisional = ScoreV2ManifestGeneration(
        source_sha256="0" * 64,
        manifest_path=str(identity.path),
        file_identity=file_identity,
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        manifest_canonical_bytes=manifest_canonical_bytes,
        manifest_canonical_sha256=manifest_canonical_sha256,
        custom_implementation_blocked=custom_blocked,
    )
    source_sha256 = hashlib.sha256(
        canonical_json_bytes(
            _manifest_source_document(
                provisional,
                include_source_sha256=False,
            )
        )
    ).hexdigest()
    return provisional._replace(source_sha256=source_sha256)


def _same_plain_file_identity(
    left: PlainFileIdentity,
    right: PlainFileIdentity,
) -> bool:
    return ScoreV2FileGeneration.from_plain(
        left
    ) == ScoreV2FileGeneration.from_plain(right)


def _read_fresh_capability_projection(
    source: ScoreV2ManifestGeneration,
    facts: tuple[_ExecutorFact, ...],
    *,
    catalogue_root: Path,
    limits: AuthoringJsonLimits,
) -> bytes:
    expected = {fact.capability_projection_bytes for fact in facts}
    if len(expected) != 1:
        raise ScoreV2CapabilitySourceError(
            "capability_source.capability_projection_conflict"
        )
    defer_modes = {fact.defer_onset_evidence for fact in facts}
    if len(defer_modes) != 1:
        raise ScoreV2CapabilitySourceError(
            "capability_source.capability_projection_conflict"
        )
    try:
        capability = read_capability(
            source.manifest_path,
            root=catalogue_root,
            defer_onset_evidence=defer_modes.pop(),
        )
        fresh = _capability_projection_bytes(capability, limits=limits)
    except ScoreV2CapabilitySourceError:
        raise
    except (AuthoringJsonError, OSError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.capability_resolution_failed"
        ) from exc
    if (
        fresh not in expected
        or _normal_path_key(capability.manifest_path)
        != _normal_path_key(source.manifest_path)
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.capability_projection_mismatch"
        )
    for fact in facts:
        if capability.relative_path != fact.instrument_relative_path:
            raise ScoreV2CapabilitySourceError(
                "capability_source.capability_projection_mismatch"
            )
    return fresh


def _capture_manifest_generation(
    facts: tuple[_ExecutorFact, ...],
    *,
    catalogue_root: Path,
    manifest_limits: AuthoringJsonLimits,
    projection_limits: AuthoringJsonLimits,
    maximum_manifest_bytes: int,
) -> tuple[ScoreV2ManifestGeneration, bytes]:
    requested = facts[0].manifest_requested_path
    try:
        first_identity, first_bytes = read_plain_file_bytes(
            requested,
            maximum_bytes=maximum_manifest_bytes,
        )
    except OSError as exc:
        code = (
            "capability_source.manifest_too_large"
            if "byte limit" in str(exc)
            else "capability_source.manifest_source_unavailable"
        )
        raise ScoreV2CapabilitySourceError(
            code,
            limit=(maximum_manifest_bytes if "byte limit" in str(exc) else None),
        ) from exc
    manifest, canonical = _strict_manifest_document(
        first_bytes,
        limits=manifest_limits,
    )
    source = _new_manifest_generation(
        first_identity,
        first_bytes,
        manifest,
        canonical,
    )
    projection = _read_fresh_capability_projection(
        source,
        facts,
        catalogue_root=catalogue_root,
        limits=projection_limits,
    )
    try:
        second_identity, second_bytes = read_plain_file_bytes(
            source.manifest_path,
            maximum_bytes=maximum_manifest_bytes,
        )
    except OSError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.source_changed_during_capture"
        ) from exc
    if (
        not _same_plain_file_identity(first_identity, second_identity)
        or first_bytes != second_bytes
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.source_changed_during_capture"
        )
    return source, projection


def _validate_directory_generation(
    identity: object,
    *,
    code: str,
) -> ScoreV2DirectoryGeneration:
    if (
        type(identity) is not ScoreV2DirectoryGeneration
        or type(identity.path) is not str
        or not identity.path
        or not Path(identity.path).is_absolute()
        or type(identity.device) is not int
        or identity.device < 0
        or type(identity.inode) is not int
        or identity.inode <= 0
    ):
        raise ScoreV2CapabilitySourceError(code)
    return identity


def _validate_file_generation(identity: object) -> ScoreV2FileGeneration:
    if (
        type(identity) is not ScoreV2FileGeneration
        or type(identity.path) is not str
        or not identity.path
        or not Path(identity.path).is_absolute()
        or type(identity.device) is not int
        or identity.device < 0
        or type(identity.inode) is not int
        or identity.inode <= 0
        or type(identity.size) is not int
        or identity.size < 0
        or type(identity.modified_ns) is not int
        or type(identity.changed_ns) is not int
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    parent = _validate_directory_generation(
        identity.parent,
        code="capability_source.integrity_mismatch",
    )
    if Path(identity.path).parent != Path(parent.path):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return identity


def _validated_manifest_document(
    source: object,
) -> tuple[ScoreV2ManifestGeneration, dict[str, Any]]:
    if type(source) is not ScoreV2ManifestGeneration:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    identity = _validate_file_generation(source.file_identity)
    if (
        type(source.manifest_path) is not str
        or source.manifest_path != identity.path
        or type(source.raw_bytes) is not bytes
        or not 1 <= len(source.raw_bytes) <= HARD_MAX_MANIFEST_BYTES
        or identity.size != len(source.raw_bytes)
        or not _is_sha256(source.raw_sha256)
        or hashlib.sha256(source.raw_bytes).hexdigest() != source.raw_sha256
        or type(source.manifest_canonical_bytes) is not bytes
        or not _is_sha256(source.manifest_canonical_sha256)
        or hashlib.sha256(source.manifest_canonical_bytes).hexdigest()
        != source.manifest_canonical_sha256
        or type(source.custom_implementation_blocked) is not bool
        or not _is_sha256(source.source_sha256)
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    limits = _active_limits(HARD_MAX_MANIFEST_BYTES)
    try:
        manifest, canonical = _strict_manifest_document(
            source.raw_bytes,
            limits=limits,
        )
    except ScoreV2CapabilitySourceError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    expected_source_sha256 = hashlib.sha256(
        canonical_json_bytes(
            _manifest_source_document(
                source,
                include_source_sha256=False,
            )
        )
    ).hexdigest()
    if (
        canonical != source.manifest_canonical_bytes
        or (manifest.get("implementation") is not None)
        is not source.custom_implementation_blocked
        or expected_source_sha256 != source.source_sha256
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return source, manifest


def _validated_capability_projection(
    projection: object,
    *,
    sources: dict[str, tuple[ScoreV2ManifestGeneration, dict[str, Any]]],
) -> ScoreV2CapabilityProjection:
    if (
        type(projection) is not ScoreV2CapabilityProjection
        or not _is_sha256(projection.manifest_source_sha256)
        or projection.manifest_source_sha256 not in sources
        or type(projection.instrument_relative_path) is not str
        or not projection.instrument_relative_path
        or type(projection.canonical_bytes) is not bytes
        or not _is_sha256(projection.canonical_sha256)
        or hashlib.sha256(projection.canonical_bytes).hexdigest()
        != projection.canonical_sha256
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    limits = _active_limits(HARD_MAX_MANIFEST_BYTES)
    try:
        document = strict_json_loads(
            projection.canonical_bytes,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    source, manifest = sources[projection.manifest_source_sha256]
    if (
        type(document) is not dict
        or canonical_json_bytes(document) != projection.canonical_bytes
        or document.get("relative_path") != projection.instrument_relative_path
        or document.get("implementation_type") != manifest.get("type")
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return projection


def _validated_overrides(
    value: object,
) -> tuple[tuple[str, int | float | str | bool], ...]:
    if type(value) is not tuple:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    result: list[tuple[str, int | float | str | bool]] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        key, scalar = item
        if (
            type(key) is not str
            or not key
            or key not in _OVERRIDE_ALLOWED_FIELDS
            or key in seen
            or type(scalar) not in (int, float, str, bool)
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        seen.add(key)
        result.append((key, scalar))
    if tuple(sorted(result)) != value:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    # Reuse the strict value gate for finite numbers and safe integers.
    try:
        normalized = _parse_overrides(
            dict(result),
            "score-v2 capability source",
        )
        bounded_canonical_json_bytes(
            dict(normalized),
            limits=_active_limits(HARD_MAX_MANIFEST_BYTES),
            require_object=True,
            require_js_safe_integers=True,
        )
    except (AuthoringJsonError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    if normalized != value:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return value


def _validate_binding(
    binding: object,
    *,
    sources: dict[str, tuple[ScoreV2ManifestGeneration, dict[str, Any]]],
    projections: dict[str, ScoreV2CapabilityProjection],
) -> ScoreV2ExecutorCapabilityBinding:
    if (
        type(binding) is not ScoreV2ExecutorCapabilityBinding
        or type(binding.executor_order) is not int
        or binding.executor_order < 0
        or type(binding.executor_id) is not str
        or not binding.executor_id
        or type(binding.part_id) is not str
        or not binding.part_id
        or type(binding.instrument_relative_path) is not str
        or not binding.instrument_relative_path
        or not _is_sha256(binding.manifest_source_sha256)
        or binding.manifest_source_sha256 not in sources
        or not _is_sha256(binding.capability_projection_sha256)
        or binding.capability_projection_sha256 not in projections
        or not _is_sha256(binding.effective_manifest_canonical_sha256)
        or not _is_sha256(binding.effective_manifest_sha256)
        or type(binding.custom_implementation_blocked) is not bool
        or type(binding.execution_eligibility) is not str
        or type(binding.runtime_fingerprint_status) is not str
        or binding.runtime_fingerprint_status != RUNTIME_FINGERPRINT_STATUS
        or binding.runtime_fingerprint_sha256 is not None
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    source, manifest = sources[binding.manifest_source_sha256]
    projection = projections[binding.capability_projection_sha256]
    overrides = _validated_overrides(binding.overrides)
    effective = {**manifest, **dict(overrides)}
    expected_canonical_sha256 = hashlib.sha256(
        canonical_json_bytes(effective)
    ).hexdigest()
    try:
        expected_factory_sha256 = factory_manifest_sha256(effective)
    except ValueError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    expected_eligibility = (
        "blocked_custom_implementation"
        if source.custom_implementation_blocked
        else "pending_runtime_fingerprint"
    )
    if (
        projection.manifest_source_sha256 != source.source_sha256
        or projection.instrument_relative_path
        != binding.instrument_relative_path
        or expected_canonical_sha256
        != binding.effective_manifest_canonical_sha256
        or expected_factory_sha256 != binding.effective_manifest_sha256
        or source.custom_implementation_blocked
        is not binding.custom_implementation_blocked
        or binding.execution_eligibility != expected_eligibility
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    return binding


def _validate_roster_binding_projection(
    roster_document: dict[str, Any],
    executor_bindings: tuple[ScoreV2ExecutorCapabilityBinding, ...],
    projections: dict[str, ScoreV2CapabilityProjection],
) -> None:
    raw_executors = roster_document.get("executors")
    if type(raw_executors) is not list or len(raw_executors) != len(
        executor_bindings
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    for binding, raw_executor in zip(executor_bindings, raw_executors):
        if type(raw_executor) is not dict:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        projection = projections[binding.capability_projection_sha256]
        projection_document = projection.projection_copy()
        raw_overrides = raw_executor.get("overrides", {})
        if (
            type(raw_overrides) is not dict
            or raw_executor.get("executor_id") != binding.executor_id
            or raw_executor.get("part_id") != binding.part_id
            or raw_executor.get("instrument")
            != binding.instrument_relative_path
            or raw_executor.get("instrument_name")
            != projection_document.get("name")
            or raw_overrides != dict(binding.overrides)
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )


def _validate_snapshot_values(
    *,
    catalogue_root: object,
    roster_projection_bytes: object,
    roster_projection_sha256: object,
    manifest_generations: object,
    capability_projections: object,
    executor_bindings: object,
) -> None:
    root = _validate_directory_generation(
        catalogue_root,
        code="capability_source.integrity_mismatch",
    )
    if (
        type(roster_projection_bytes) is not bytes
        or not _is_sha256(roster_projection_sha256)
        or hashlib.sha256(roster_projection_bytes).hexdigest()
        != roster_projection_sha256
        or type(manifest_generations) is not tuple
        or not manifest_generations
        or type(capability_projections) is not tuple
        or not capability_projections
        or type(executor_bindings) is not tuple
        or not executor_bindings
        or len(executor_bindings) > HARD_MAX_EXECUTORS
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    try:
        roster_document = strict_json_loads(
            roster_projection_bytes,
            limits=_active_limits(MAX_CAPABILITY_ARTIFACT_BYTES),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        ) from exc
    if (
        type(roster_document) is not dict
        or canonical_json_bytes(roster_document) != roster_projection_bytes
    ):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )

    sources: dict[
        str,
        tuple[ScoreV2ManifestGeneration, dict[str, Any]],
    ] = {}
    source_relative_paths: dict[str, str] = {}
    aggregate_captured_bytes = len(roster_projection_bytes)
    previous_path_key: tuple[str, str] | None = None
    for raw_source in manifest_generations:
        source, manifest = _validated_manifest_document(raw_source)
        if source.source_sha256 in sources:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        try:
            relative_directory = Path(source.manifest_path).parent.relative_to(
                Path(root.path)
            )
        except ValueError as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            ) from exc
        path_key = (
            os.path.normcase(source.manifest_path),
            source.manifest_path,
        )
        if previous_path_key is not None and path_key <= previous_path_key:
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        previous_path_key = path_key
        sources[source.source_sha256] = (source, manifest)
        source_relative_paths[source.source_sha256] = (
            relative_directory.as_posix()
        )
        aggregate_captured_bytes += len(source.raw_bytes)

    projections: dict[str, ScoreV2CapabilityProjection] = {}
    projection_sources: set[str] = set()
    for raw_projection in capability_projections:
        projection = _validated_capability_projection(
            raw_projection,
            sources=sources,
        )
        if (
            projection.canonical_sha256 in projections
            or projection.manifest_source_sha256 in projection_sources
            or projection.instrument_relative_path
            != source_relative_paths[projection.manifest_source_sha256]
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        projections[projection.canonical_sha256] = projection
        projection_sources.add(projection.manifest_source_sha256)
        aggregate_captured_bytes += len(projection.canonical_bytes)
    if projection_sources != set(sources):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    if aggregate_captured_bytes > MAX_CAPABILITY_ARTIFACT_BYTES:
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )

    used_sources: set[str] = set()
    used_projections: set[str] = set()
    executor_ids: set[str] = set()
    for expected_order, raw_binding in enumerate(executor_bindings):
        binding = _validate_binding(
            raw_binding,
            sources=sources,
            projections=projections,
        )
        if (
            binding.executor_order != expected_order
            or binding.executor_id in executor_ids
        ):
            raise ScoreV2CapabilitySourceError(
                "capability_source.integrity_mismatch"
            )
        executor_ids.add(binding.executor_id)
        used_sources.add(binding.manifest_source_sha256)
        used_projections.add(binding.capability_projection_sha256)
    if used_sources != set(sources) or used_projections != set(projections):
        raise ScoreV2CapabilitySourceError(
            "capability_source.integrity_mismatch"
        )
    _validate_roster_binding_projection(
        roster_document,
        executor_bindings,
        projections,
    )


def capture_score_v2_capability_sources(
    roster: Roster,
    *,
    catalogue_root: str | os.PathLike[str],
    maximum_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    maximum_executors: int = DEFAULT_MAX_EXECUTORS,
) -> ScoreV2CapabilitySourceSnapshot:
    """Capture every manifest generation referenced by a resolved roster.

    The current ``read_capability`` API is pathname-oriented.  To avoid
    accepting an ordinary check/use race without changing that established
    module, this boundary captures the manifest before resolution, resolves a
    fresh capability, then descriptor-captures the same path again and
    requires identical file identity and bytes.  The retained raw generation
    remains the authority for every effective-manifest hash.
    """

    maximum_manifest_bytes, maximum_executors = _require_resource_limits(
        maximum_manifest_bytes,
        maximum_executors,
    )
    manifest_limits = _active_limits(maximum_manifest_bytes)
    # The manifest byte ceiling is intentionally independent from the already
    # resolved roster/capability projection.  A caller may tighten one tiny
    # manifest to eight bytes without redefining the roster's JSON budget.
    projection_limits = _active_limits(DEFAULT_MAX_MANIFEST_BYTES)
    try:
        root_identity = capture_plain_directory(catalogue_root)
        root_path = revalidate_plain_directory(root_identity)
    except (OSError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.catalogue_root_unavailable"
        ) from exc

    facts, roster_projection_bytes = _capture_executor_facts(
        roster,
        limits=projection_limits,
        maximum_executors=maximum_executors,
    )
    grouped: dict[str, list[_ExecutorFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.manifest_lookup_key, []).append(fact)

    captured: list[
        tuple[ScoreV2ManifestGeneration, bytes, tuple[_ExecutorFact, ...]]
    ] = []
    aggregate_captured_bytes = len(roster_projection_bytes)
    for key in sorted(grouped):
        group = tuple(grouped[key])
        source, projection_bytes = _capture_manifest_generation(
            group,
            catalogue_root=root_path,
            manifest_limits=manifest_limits,
            projection_limits=projection_limits,
            maximum_manifest_bytes=maximum_manifest_bytes,
        )
        aggregate_captured_bytes += len(source.raw_bytes) + len(
            projection_bytes
        )
        if aggregate_captured_bytes > MAX_CAPABILITY_ARTIFACT_BYTES:
            raise ScoreV2CapabilitySourceError(
                "capability_source.aggregate_too_large",
                actual=aggregate_captured_bytes,
                limit=MAX_CAPABILITY_ARTIFACT_BYTES,
            )
        captured.append((source, projection_bytes, group))
    try:
        revalidate_plain_directory(root_identity)
    except OSError as exc:
        raise ScoreV2CapabilitySourceError(
            "capability_source.catalogue_changed_during_capture"
        ) from exc

    captured.sort(
        key=lambda item: (
            os.path.normcase(item[0].manifest_path),
            item[0].manifest_path,
        )
    )
    # Different input spellings must not produce two records for one resolved
    # manifest.  This is especially important on case-insensitive filesystems.
    resolved_keys = [
        os.path.normcase(item[0].manifest_path) for item in captured
    ]
    if len(resolved_keys) != len(set(resolved_keys)):
        raise ScoreV2CapabilitySourceError(
            "capability_source.duplicate_manifest_identity"
        )

    manifest_generations = tuple(item[0] for item in captured)
    projections_list: list[ScoreV2CapabilityProjection] = []
    facts_by_order: dict[
        int,
        tuple[
            _ExecutorFact,
            ScoreV2ManifestGeneration,
            ScoreV2CapabilityProjection,
        ],
    ] = {}
    for source, projection_bytes, group in captured:
        projection_sha256 = hashlib.sha256(projection_bytes).hexdigest()
        projection = ScoreV2CapabilityProjection(
            manifest_source_sha256=source.source_sha256,
            instrument_relative_path=group[0].instrument_relative_path,
            canonical_bytes=projection_bytes,
            canonical_sha256=projection_sha256,
        )
        projections_list.append(projection)
        for fact in group:
            facts_by_order[fact.order] = (fact, source, projection)
    capability_projections = tuple(projections_list)

    bindings: list[ScoreV2ExecutorCapabilityBinding] = []
    for order in range(len(facts)):
        fact, source, projection = facts_by_order[order]
        manifest = source.manifest_copy()
        effective_manifest = {**manifest, **dict(fact.overrides)}
        effective_canonical_sha256 = hashlib.sha256(
            canonical_json_bytes(effective_manifest)
        ).hexdigest()
        try:
            effective_factory_sha256 = factory_manifest_sha256(
                effective_manifest
            )
        except ValueError as exc:
            raise ScoreV2CapabilitySourceError(
                "capability_source.invalid_effective_manifest"
            ) from exc
        custom_blocked = source.custom_implementation_blocked
        bindings.append(
            ScoreV2ExecutorCapabilityBinding(
                executor_order=order,
                executor_id=fact.executor_id,
                part_id=fact.part_id,
                instrument_relative_path=fact.instrument_relative_path,
                manifest_source_sha256=source.source_sha256,
                capability_projection_sha256=projection.canonical_sha256,
                overrides=fact.overrides,
                effective_manifest_canonical_sha256=(
                    effective_canonical_sha256
                ),
                effective_manifest_sha256=effective_factory_sha256,
                custom_implementation_blocked=custom_blocked,
                execution_eligibility=(
                    "blocked_custom_implementation"
                    if custom_blocked
                    else "pending_runtime_fingerprint"
                ),
                runtime_fingerprint_status=RUNTIME_FINGERPRINT_STATUS,
                runtime_fingerprint_sha256=None,
            )
        )
    executor_bindings = tuple(bindings)

    catalogue_generation = ScoreV2DirectoryGeneration.from_plain(
        root_identity
    )
    roster_projection_sha256 = hashlib.sha256(
        roster_projection_bytes
    ).hexdigest()
    _validate_snapshot_values(
        catalogue_root=catalogue_generation,
        roster_projection_bytes=roster_projection_bytes,
        roster_projection_sha256=roster_projection_sha256,
        manifest_generations=manifest_generations,
        capability_projections=capability_projections,
        executor_bindings=executor_bindings,
    )
    artifact_document = _artifact_document(
        catalogue_root=catalogue_generation,
        roster_projection_bytes=roster_projection_bytes,
        roster_projection_sha256=roster_projection_sha256,
        manifest_generations=manifest_generations,
        capability_projections=capability_projections,
        executor_bindings=executor_bindings,
    )
    try:
        artifact_bytes = bounded_canonical_json_bytes(
            artifact_document,
            limits=_active_limits(MAX_CAPABILITY_ARTIFACT_BYTES),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _translate_json_error("artifact", exc) from exc
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

    snapshot = object.__new__(ScoreV2CapabilitySourceSnapshot)
    object.__setattr__(snapshot, "catalogue_root", catalogue_generation)
    object.__setattr__(
        snapshot,
        "roster_projection_sha256",
        roster_projection_sha256,
    )
    object.__setattr__(
        snapshot,
        "manifest_generations",
        manifest_generations,
    )
    object.__setattr__(
        snapshot,
        "capability_projections",
        capability_projections,
    )
    object.__setattr__(snapshot, "executor_bindings", executor_bindings)
    object.__setattr__(
        snapshot,
        "_roster_projection_bytes",
        roster_projection_bytes,
    )
    object.__setattr__(snapshot, "_canonical_bytes", artifact_bytes)
    object.__setattr__(snapshot, "_artifact_sha256", artifact_sha256)
    object.__setattr__(
        snapshot,
        "_identity_seal",
        (
            catalogue_generation,
            roster_projection_sha256,
            manifest_generations,
            capability_projections,
            executor_bindings,
            roster_projection_bytes,
            artifact_bytes,
            artifact_sha256,
            SCORE_V2_CAPABILITY_SOURCE_CONTRACT,
        ),
    )
    snapshot._trusted_artifact_bytes()
    return snapshot


__all__ = [
    "DEFAULT_MAX_EXECUTORS",
    "DEFAULT_MAX_MANIFEST_BYTES",
    "HARD_MAX_EXECUTORS",
    "HARD_MAX_MANIFEST_BYTES",
    "MAX_CAPABILITY_ARTIFACT_BYTES",
    "RUNTIME_FINGERPRINT_STATUS",
    "SCORE_V2_CAPABILITY_SOURCE_CONTRACT",
    "SCORE_V2_CAPABILITY_SOURCE_KIND",
    "SCORE_V2_CAPABILITY_SOURCE_SCHEMA_VERSION",
    "ScoreV2CapabilityProjection",
    "ScoreV2CapabilitySourceError",
    "ScoreV2CapabilitySourceSnapshot",
    "ScoreV2DirectoryGeneration",
    "ScoreV2ExecutorCapabilityBinding",
    "ScoreV2FileGeneration",
    "ScoreV2ManifestGeneration",
    "capture_score_v2_capability_sources",
]
