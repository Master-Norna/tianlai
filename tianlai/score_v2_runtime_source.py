"""Seal legacy runtime fingerprints for one Score-v2 capability generation.

This boundary fills the runtime-fingerprint hole deliberately left by
``score_v2_capability_source`` and ``score_v2_capability_adapter``.  It does
not create renderer events and its artifact is explicitly *not* render
authority.  The current legacy fingerprint API exposes a complete Python
render closure, runtime dependency identities, optional calibration files and
an aggregate constructed asset graph.  It does not expose descriptor-bound
identities for each asset in that graph; the limitation is recorded in every
artifact rather than silently overstating the evidence.

Capability manifest sources are descriptor-revalidated before and after
capture.  Revalidation sequentially recomputes every legacy fingerprint with
the exact effective manifest and sample rate that were captured here.  It
rejects replacements observed during those individual checks, but cannot make
unrelated executor files one atomic generation.  It also makes no claim about
ABA replacement of runtime files that the legacy API does not
descriptor-capture.
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
from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .instrument import factory_manifest_sha256
from .onset_evidence import (
    FINGERPRINT_ALGORITHM,
    OnsetEvidenceError,
    compute_runtime_fingerprint,
    validate_runtime_fingerprint,
)
from .resource_limits import ProjectLimits, ResourceLimitError
from .score_v2_capability_adapter import (
    ScoreV2CapabilityAdapterError,
    ScoreV2CapabilityPlan,
)
from .score_v2_capability_source import (
    ScoreV2CapabilitySourceError,
    ScoreV2CapabilitySourceSnapshot,
    ScoreV2ExecutorCapabilityBinding,
    ScoreV2ManifestGeneration,
)


SCORE_V2_RUNTIME_SOURCE_KIND = "tianlai.score_v2_runtime_source"
SCORE_V2_RUNTIME_SOURCE_SCHEMA_VERSION = 1
SCORE_V2_RUNTIME_SOURCE_CONTRACT = (
    "score-v2-runtime-source-v1-not-render-authority"
)
RUNTIME_FINGERPRINT_STATUS = "captured_legacy_runtime_fingerprint"
ASSET_DESCRIPTOR_STATUS = (
    "aggregate_only_per_asset_descriptors_unavailable_from_legacy_api"
)
NONEMPTY_ASSET_INVENTORY_STATUS = "captured_nonempty_runtime_asset_graph"
NO_EXTERNAL_ASSET_INVENTORY_STATUS = (
    "declared_no_external_audio_assets"
)

_HEX_DIGITS = frozenset("0123456789abcdef")
_LIMITATIONS = {
    "asset_descriptor_status": ASSET_DESCRIPTOR_STATUS,
    "onset_evidence_status": "not_captured",
    "lazy_asset_generation": "legacy_path_reopen",
    "factory_instance_generation": "not_captured",
    "factory_provenance_status": "pending_render_transaction",
    "runtime_generation_revalidation": "legacy_fingerprint_recomputation",
    "runtime_generation_set_atomicity": "sequential_observations_not_atomic",
    "ordinary_generation_replacement": "rejected_when_observed",
    "malicious_aba_resistance": "not_claimed",
}


class ScoreV2RuntimeSourceError(ValueError):
    """A stable, non-reflective runtime-source boundary failure."""

    def __init__(
        self,
        code: str,
        *,
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.message_key = f"scoreV2RuntimeSource.{code.replace('.', '_')}"
        self.actual = actual
        self.limit = limit
        super().__init__(code)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _active_limits(limits: ProjectLimits | None) -> ProjectLimits:
    if limits is None:
        return ProjectLimits.from_environment()
    if type(limits) is not ProjectLimits:
        raise TypeError("limits must be ProjectLimits or None")
    values = {
        name: getattr(limits, name)
        for name in ProjectLimits.__dataclass_fields__
    }
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise ValueError("ProjectLimits fields must retain positive integers")
    return ProjectLimits(**values)


def _json_limits(maximum_bytes: int) -> AuthoringJsonLimits:
    return AuthoringJsonLimits(
        max_document_bytes=maximum_bytes,
        max_depth=128,
        max_nodes=1_000_000,
        max_string_bytes=min(maximum_bytes, 1024 * 1024),
        max_array_items=250_000,
        max_object_members=65_536,
    )


def _bounded_object(
    value: object,
    *,
    maximum_bytes: int,
    error_code: str,
) -> tuple[dict[str, Any], bytes]:
    if type(value) is not dict:
        raise ScoreV2RuntimeSourceError(error_code)
    try:
        payload = bounded_canonical_json_bytes(
            value,
            limits=_json_limits(maximum_bytes),
            require_object=True,
            require_js_safe_integers=True,
        )
        detached = strict_json_loads(
            payload,
            limits=_json_limits(maximum_bytes),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        if exc.code == "document_too_large":
            raise ResourceLimitError(
                "runtime_source.document_too_large",
                "Score-v2 runtime source exceeds the plan JSON byte budget",
                actual=exc.actual,
                limit=maximum_bytes,
            ) from exc
        raise ScoreV2RuntimeSourceError(error_code) from exc
    except (
        AttributeError,
        IndexError,
        KeyError,
        RecursionError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise ScoreV2RuntimeSourceError(error_code) from exc
    if type(detached) is not dict:
        raise ScoreV2RuntimeSourceError(error_code)
    return detached, payload


def _optional_file(value: object) -> dict[str, str | None]:
    if type(value) is not dict or set(value) != {"path", "sha256"}:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    path = value["path"]
    digest = value["sha256"]
    if (
        (path is not None and (type(path) is not str or not path))
        or (digest is not None and not _is_sha256(digest))
        or (path is None and digest is not None)
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    return {"path": path, "sha256": digest}


def _fingerprint_evidence(
    fingerprint: dict[str, Any],
    *,
    sample_rate: int,
    manifest_raw_sha256: str,
    asset_inventory_status: str,
) -> dict[str, object]:
    expected_keys = {
        "algorithm",
        "manifest",
        "render_python_closure",
        "runtime_dependencies",
        "local_implementation",
        "resource_verification",
        "pitch_calibration",
        "runtime_asset_graph",
    }
    if set(fingerprint) != expected_keys:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    manifest = fingerprint["manifest"]
    closure = fingerprint["render_python_closure"]
    dependencies = fingerprint["runtime_dependencies"]
    graph = fingerprint["runtime_asset_graph"]
    if (
        fingerprint["algorithm"] != FINGERPRINT_ALGORITHM
        or type(manifest) is not dict
        or set(manifest) != {"path", "sha256"}
        or type(manifest["path"]) is not str
        or not manifest["path"]
        or not _is_sha256(manifest["sha256"])
        or manifest["sha256"] != manifest_raw_sha256
        or type(closure) is not dict
        or not closure
        or type(dependencies) is not dict
        or not dependencies
        or type(graph) is not dict
        or set(graph)
        != {
            "algorithm",
            "sample_rate_hz",
            "file_count",
            "total_bytes",
            "region_count",
            "sha256",
        }
        or graph["algorithm"] != "constructed-runtime-asset-graph-v1"
        or type(graph["sample_rate_hz"]) is not int
        or graph["sample_rate_hz"] != sample_rate
        or type(graph["file_count"]) is not int
        or graph["file_count"] < 0
        or type(graph["total_bytes"]) is not int
        or graph["total_bytes"] < 0
        or type(graph["region_count"]) is not int
        or graph["region_count"] < 0
        or not _is_sha256(graph["sha256"])
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    if (
        graph["file_count"] == 0
        and (
            graph["total_bytes"] != 0
            or asset_inventory_status
            != NO_EXTERNAL_ASSET_INVENTORY_STATUS
        )
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.empty_runtime_asset_graph"
        )
    if (
        graph["file_count"] > 0
        and (
            graph["total_bytes"] < 1
            or asset_inventory_status
            != NONEMPTY_ASSET_INVENTORY_STATUS
        )
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    local_implementation = _optional_file(fingerprint["local_implementation"])
    if local_implementation != {"path": None, "sha256": None}:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.custom_implementation_blocked"
        )
    resource_verification = _optional_file(
        fingerprint["resource_verification"]
    )
    pitch_calibration = _optional_file(fingerprint["pitch_calibration"])
    return {
        "render_python_closure_sha256": canonical_json_sha256(closure),
        "runtime_dependencies_sha256": canonical_json_sha256(dependencies),
        "local_implementation": local_implementation,
        "resource_verification": resource_verification,
        "pitch_calibration": pitch_calibration,
        "runtime_asset_graph": graph,
        "asset_inventory_status": asset_inventory_status,
        "asset_descriptor_status": ASSET_DESCRIPTOR_STATUS,
    }


class ScoreV2ExecutorRuntimeBinding(NamedTuple):
    """Immutable runtime evidence for one roster executor."""

    executor_order: int
    executor_id: str
    part_id: str
    capability_plan_sha256: str
    capability_source_sha256: str
    roster_projection_sha256: str
    manifest_source_sha256: str
    manifest_raw_sha256: str
    manifest_canonical_sha256: str
    capability_projection_sha256: str
    effective_manifest_canonical_sha256: str
    effective_manifest_sha256: str
    sample_rate: int
    legacy_runtime_fingerprint_canonical_bytes: bytes
    legacy_runtime_fingerprint_sha256: str
    render_python_closure_sha256: str
    runtime_dependencies_sha256: str
    asset_inventory_status: str

    def fingerprint_copy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.legacy_runtime_fingerprint_canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        return value

    def to_dict(self) -> dict[str, object]:
        fingerprint = self.fingerprint_copy()
        evidence = _fingerprint_evidence(
            fingerprint,
            sample_rate=self.sample_rate,
            manifest_raw_sha256=self.manifest_raw_sha256,
            asset_inventory_status=self.asset_inventory_status,
        )
        if (
            evidence["render_python_closure_sha256"]
            != self.render_python_closure_sha256
            or evidence["runtime_dependencies_sha256"]
            != self.runtime_dependencies_sha256
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        return {
            "executor_order": self.executor_order,
            "executor_id": self.executor_id,
            "part_id": self.part_id,
            "capability_plan_sha256": self.capability_plan_sha256,
            "capability_source_sha256": self.capability_source_sha256,
            "roster_projection_sha256": self.roster_projection_sha256,
            "manifest_source_sha256": self.manifest_source_sha256,
            "manifest_raw_sha256": self.manifest_raw_sha256,
            "manifest_canonical_sha256": self.manifest_canonical_sha256,
            "capability_projection_sha256": (
                self.capability_projection_sha256
            ),
            "effective_manifest_canonical_sha256": (
                self.effective_manifest_canonical_sha256
            ),
            "effective_manifest_sha256": self.effective_manifest_sha256,
            "sample_rate": self.sample_rate,
            "runtime_fingerprint_status": RUNTIME_FINGERPRINT_STATUS,
            "asset_inventory_status": self.asset_inventory_status,
            "legacy_runtime_fingerprint_bytes_size": len(
                self.legacy_runtime_fingerprint_canonical_bytes
            ),
            "legacy_runtime_fingerprint_sha256": (
                self.legacy_runtime_fingerprint_sha256
            ),
            "runtime_evidence": evidence,
            "legacy_runtime_fingerprint": fingerprint,
        }


class _ScoreV2ExecutorExecutionInput(NamedTuple):
    """Package-local factory input rebuilt from captured generations."""

    executor_order: int
    executor_id: str
    part_id: str
    manifest_path: str
    effective_manifest_canonical_bytes: bytes
    effective_manifest_canonical_sha256: str
    effective_manifest_sha256: str
    runtime_binding: ScoreV2ExecutorRuntimeBinding

    def manifest_copy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.effective_manifest_canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        return value


def _artifact_document(
    *,
    project_root: str,
    capability_plan_sha256: str,
    capability_source_sha256: str,
    roster_projection_sha256: str,
    sample_rate: int,
    executor_bindings: tuple[ScoreV2ExecutorRuntimeBinding, ...],
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_RUNTIME_SOURCE_KIND,
        "schema_version": SCORE_V2_RUNTIME_SOURCE_SCHEMA_VERSION,
        "contract": SCORE_V2_RUNTIME_SOURCE_CONTRACT,
        "render_authority": False,
        "project_root": project_root,
        "limitations": dict(_LIMITATIONS),
        "bindings": {
            "capability_plan_sha256": capability_plan_sha256,
            "capability_source_sha256": capability_source_sha256,
            "roster_projection_sha256": roster_projection_sha256,
            "sample_rate": sample_rate,
        },
        "executor_count": len(executor_bindings),
        "executors": [binding.to_dict() for binding in executor_bindings],
    }


def _validate_executor_runtime_binding(
    value: object,
    *,
    expected_order: int,
    capability_plan_sha256: str,
    capability_source_sha256: str,
    roster_projection_sha256: str,
    sample_rate: int,
) -> ScoreV2ExecutorRuntimeBinding:
    if type(value) is not ScoreV2ExecutorRuntimeBinding:
        raise ScoreV2RuntimeSourceError("runtime_source.integrity_mismatch")
    fields = (
        value.capability_plan_sha256,
        value.capability_source_sha256,
        value.roster_projection_sha256,
        value.manifest_source_sha256,
        value.manifest_raw_sha256,
        value.manifest_canonical_sha256,
        value.capability_projection_sha256,
        value.effective_manifest_canonical_sha256,
        value.effective_manifest_sha256,
        value.legacy_runtime_fingerprint_sha256,
        value.render_python_closure_sha256,
        value.runtime_dependencies_sha256,
    )
    if (
        type(value.executor_order) is not int
        or value.executor_order != expected_order
        or type(value.executor_id) is not str
        or not value.executor_id
        or type(value.part_id) is not str
        or not value.part_id
        or any(not _is_sha256(item) for item in fields)
        or value.capability_plan_sha256 != capability_plan_sha256
        or value.capability_source_sha256 != capability_source_sha256
        or value.roster_projection_sha256 != roster_projection_sha256
        or type(value.sample_rate) is not int
        or value.sample_rate != sample_rate
        or type(value.asset_inventory_status) is not str
        or value.asset_inventory_status
        not in {
            NONEMPTY_ASSET_INVENTORY_STATUS,
            NO_EXTERNAL_ASSET_INVENTORY_STATUS,
        }
        or type(value.legacy_runtime_fingerprint_canonical_bytes) is not bytes
        or not value.legacy_runtime_fingerprint_canonical_bytes
        or hashlib.sha256(
            value.legacy_runtime_fingerprint_canonical_bytes
        ).hexdigest()
        != value.legacy_runtime_fingerprint_sha256
    ):
        raise ScoreV2RuntimeSourceError("runtime_source.integrity_mismatch")
    try:
        fingerprint = strict_json_loads(
            value.legacy_runtime_fingerprint_canonical_bytes,
            limits=_json_limits(
                max(1, len(value.legacy_runtime_fingerprint_canonical_bytes))
            ),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.integrity_mismatch"
        ) from exc
    if (
        type(fingerprint) is not dict
        or canonical_json_bytes(fingerprint)
        != value.legacy_runtime_fingerprint_canonical_bytes
    ):
        raise ScoreV2RuntimeSourceError("runtime_source.integrity_mismatch")
    evidence = _fingerprint_evidence(
        fingerprint,
        sample_rate=sample_rate,
        manifest_raw_sha256=value.manifest_raw_sha256,
        asset_inventory_status=value.asset_inventory_status,
    )
    if (
        evidence["render_python_closure_sha256"]
        != value.render_python_closure_sha256
        or evidence["runtime_dependencies_sha256"]
        != value.runtime_dependencies_sha256
    ):
        raise ScoreV2RuntimeSourceError("runtime_source.integrity_mismatch")
    return value


def _validate_snapshot_values(
    *,
    project_root: object,
    capability_plan_sha256: object,
    capability_source_sha256: object,
    roster_projection_sha256: object,
    sample_rate: object,
    executor_bindings: object,
) -> None:
    if (
        type(project_root) is not str
        or not project_root
        or not Path(project_root).is_absolute()
        or not _is_sha256(capability_plan_sha256)
        or not _is_sha256(capability_source_sha256)
        or not _is_sha256(roster_projection_sha256)
        or type(sample_rate) is not int
        or not 8_000 <= sample_rate <= 384_000
        or type(executor_bindings) is not tuple
        or not executor_bindings
    ):
        raise ScoreV2RuntimeSourceError("runtime_source.integrity_mismatch")
    executor_ids: set[str] = set()
    part_ids: set[str] = set()
    for order, raw in enumerate(executor_bindings):
        binding = _validate_executor_runtime_binding(
            raw,
            expected_order=order,
            capability_plan_sha256=capability_plan_sha256,
            capability_source_sha256=capability_source_sha256,
            roster_projection_sha256=roster_projection_sha256,
            sample_rate=sample_rate,
        )
        if binding.executor_id in executor_ids or binding.part_id in part_ids:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        executor_ids.add(binding.executor_id)
        part_ids.add(binding.part_id)


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2RuntimeSourceSnapshot:
    """A sealed runtime-input generation that remains non-render-authority."""

    project_root: str
    capability_plan_sha256: str
    capability_source_sha256: str
    roster_projection_sha256: str
    sample_rate: int
    executor_bindings: tuple[ScoreV2ExecutorRuntimeBinding, ...]
    _capability_plan: ScoreV2CapabilityPlan = field(
        repr=False, compare=False
    )
    _capability_sources: ScoreV2CapabilitySourceSnapshot = field(
        repr=False, compare=False
    )
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2RuntimeSourceSnapshot cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2RuntimeSourceSnapshot must be created by "
            "capture_score_v2_runtime_sources"
        )

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            seal = self._identity_seal
            current_project_root = self.project_root
            current_plan_hash = self.capability_plan_sha256
            current_source_hash = self.capability_source_sha256
            current_roster_hash = self.roster_projection_sha256
            current_sample_rate = self.sample_rate
            current_bindings = self.executor_bindings
            current_capability_plan = self._capability_plan
            current_capability_sources = self._capability_sources
            current_payload = self._canonical_bytes
            current_artifact_hash = self._artifact_sha256
        except AttributeError as exc:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        if type(seal) is not tuple or len(seal) != 11:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        (
            project_root,
            plan_hash,
            source_hash,
            roster_hash,
            sample_rate,
            bindings,
            capability_plan,
            capability_sources,
            payload,
            artifact_hash,
            contract,
        ) = seal
        if (
            type(project_root) is not str
            or not _is_sha256(plan_hash)
            or not _is_sha256(source_hash)
            or not _is_sha256(roster_hash)
            or type(sample_rate) is not int
            or type(bindings) is not tuple
            or type(capability_plan) is not ScoreV2CapabilityPlan
            or type(capability_sources)
            is not ScoreV2CapabilitySourceSnapshot
            or type(artifact_hash) is not str
            or type(contract) is not str
            or
            type(current_project_root) is not str
            or current_project_root != project_root
            or type(current_plan_hash) is not str
            or current_plan_hash != plan_hash
            or type(current_source_hash) is not str
            or current_source_hash != source_hash
            or type(current_roster_hash) is not str
            or current_roster_hash != roster_hash
            or type(current_sample_rate) is not int
            or current_sample_rate != sample_rate
            or current_bindings is not bindings
            or current_capability_plan is not capability_plan
            or current_capability_sources is not capability_sources
            or current_payload is not payload
            or type(current_artifact_hash) is not str
            or current_artifact_hash != artifact_hash
            or contract != SCORE_V2_RUNTIME_SOURCE_CONTRACT
            or type(payload) is not bytes
            or not _is_sha256(artifact_hash)
            or hashlib.sha256(payload).hexdigest() != artifact_hash
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        try:
            _validate_snapshot_values(
                project_root=project_root,
                capability_plan_sha256=plan_hash,
                capability_source_sha256=source_hash,
                roster_projection_sha256=roster_hash,
                sample_rate=sample_rate,
                executor_bindings=bindings,
            )
            rebuilt = canonical_json_bytes(
                _artifact_document(
                    project_root=project_root,
                    capability_plan_sha256=plan_hash,
                    capability_source_sha256=source_hash,
                    roster_projection_sha256=roster_hash,
                    sample_rate=sample_rate,
                    executor_bindings=bindings,
                )
            )
        except ScoreV2RuntimeSourceError as exc:
            if exc.code == "runtime_source.integrity_mismatch":
                raise
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        if rebuilt != payload:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        return payload

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

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_artifact_bytes())
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.integrity_mismatch"
            )
        return value

    def _execution_input_for_executor(
        self,
        executor_id: str,
    ) -> _ScoreV2ExecutorExecutionInput:
        """Rebuild one factory input from retained, sealed input objects.

        The Score-v2 renderer must call :meth:`revalidate_runtime_sources`
        immediately before this method and again after factory construction.
        This method supplies no durable authority; it only prevents the
        renderer from reopening a manifest and inventing a second generation.
        """

        self._trusted_artifact_bytes()
        if type(executor_id) is not str or not executor_id:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.executor_not_found"
            )
        inputs = _input_generations(
            self._capability_plan,
            self._capability_sources,
            maximum_executors=len(self.executor_bindings),
        )
        _validate_cross_bindings(
            inputs.plan_document,
            capability_source_sha256=inputs.capability_source_sha256,
            roster_projection_sha256=inputs.roster_projection_sha256,
            sample_rate=inputs.sample_rate,
            source_bindings=inputs.source_bindings,
        )
        if (
            inputs.capability_plan_sha256 != self.capability_plan_sha256
            or inputs.capability_source_sha256
            != self.capability_source_sha256
            or inputs.roster_projection_sha256
            != self.roster_projection_sha256
            or inputs.sample_rate != self.sample_rate
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_generation_changed"
            )
        matches = tuple(
            binding
            for binding in self.executor_bindings
            if binding.executor_id == executor_id
        )
        if len(matches) != 1:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.executor_not_found"
            )
        runtime_binding = matches[0]
        source_binding = inputs.source_bindings.get(executor_id)
        source = inputs.manifest_sources.get(
            runtime_binding.manifest_source_sha256
        )
        if source_binding is None or source is None:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_generation_changed"
            )
        effective_manifest = _effective_manifest(source, source_binding)
        _validate_runtime_binding_source(
            runtime_binding,
            source_binding=source_binding,
            source=source,
            effective_manifest=effective_manifest,
        )
        effective_bytes = canonical_json_bytes(effective_manifest)
        effective_hash = hashlib.sha256(effective_bytes).hexdigest()
        if (
            effective_hash
            != runtime_binding.effective_manifest_canonical_sha256
            or factory_manifest_sha256(effective_manifest)
            != runtime_binding.effective_manifest_sha256
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_generation_changed"
            )
        _require_input_artifact_identities(
            self._capability_plan,
            self._capability_sources,
            capability_plan_sha256=self.capability_plan_sha256,
            capability_source_sha256=self.capability_source_sha256,
        )
        return _ScoreV2ExecutorExecutionInput(
            executor_order=runtime_binding.executor_order,
            executor_id=runtime_binding.executor_id,
            part_id=runtime_binding.part_id,
            manifest_path=source.manifest_path,
            effective_manifest_canonical_bytes=effective_bytes,
            effective_manifest_canonical_sha256=effective_hash,
            effective_manifest_sha256=(
                runtime_binding.effective_manifest_sha256
            ),
            runtime_binding=runtime_binding,
        )

    def revalidate_runtime_sources(self) -> None:
        """Sequentially recompute every captured runtime input.

        This is a non-authoritative observation pass: the legacy fingerprint
        API cannot make a set of unrelated runtime files atomically immutable.
        A render transaction must therefore revalidate and consume its runtime
        generation under the stronger execution boundary recorded here.
        """

        self._trusted_artifact_bytes()
        _revalidate_capability_sources(self._capability_sources)
        inputs = _input_generations(
            self._capability_plan,
            self._capability_sources,
            maximum_executors=len(self.executor_bindings),
        )
        _validate_cross_bindings(
            inputs.plan_document,
            capability_source_sha256=inputs.capability_source_sha256,
            roster_projection_sha256=inputs.roster_projection_sha256,
            sample_rate=inputs.sample_rate,
            source_bindings=inputs.source_bindings,
        )
        if (
            inputs.capability_plan_sha256 != self.capability_plan_sha256
            or inputs.capability_source_sha256
            != self.capability_source_sha256
            or inputs.roster_projection_sha256
            != self.roster_projection_sha256
            or inputs.sample_rate != self.sample_rate
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_generation_changed"
            )
        for runtime_binding in self.executor_bindings:
            source_binding = inputs.source_bindings.get(
                runtime_binding.executor_id
            )
            source = inputs.manifest_sources.get(
                runtime_binding.manifest_source_sha256
            )
            if source_binding is None or source is None:
                raise ScoreV2RuntimeSourceError(
                    "runtime_source.input_generation_changed"
                )
            effective_manifest = _effective_manifest(
                source, source_binding
            )
            _validate_runtime_binding_source(
                runtime_binding,
                source_binding=source_binding,
                source=source,
                effective_manifest=effective_manifest,
            )
            try:
                validated = validate_runtime_fingerprint(
                    runtime_binding.fingerprint_copy(),
                    project_root=self.project_root,
                    manifest_path=source.manifest_path,
                    effective_manifest=effective_manifest,
                    sample_rate_hz=self.sample_rate,
                )
                _current_document, current = _bounded_object(
                    validated,
                    maximum_bytes=max(
                        1,
                        len(
                            runtime_binding.legacy_runtime_fingerprint_canonical_bytes
                        ),
                    ),
                    error_code="runtime_source.runtime_generation_changed",
                )
            except ResourceLimitError as exc:
                raise ScoreV2RuntimeSourceError(
                    "runtime_source.runtime_generation_changed"
                ) from exc
            except (
                OnsetEvidenceError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                raise ScoreV2RuntimeSourceError(
                    "runtime_source.runtime_generation_changed"
                ) from exc
            if current != runtime_binding.legacy_runtime_fingerprint_canonical_bytes:
                raise ScoreV2RuntimeSourceError(
                    "runtime_source.runtime_generation_changed"
                )
        _revalidate_capability_sources(self._capability_sources)
        _require_input_artifact_identities(
            self._capability_plan,
            self._capability_sources,
            capability_plan_sha256=self.capability_plan_sha256,
            capability_source_sha256=self.capability_source_sha256,
        )


def _revalidate_capability_sources(
    sources: ScoreV2CapabilitySourceSnapshot,
) -> None:
    try:
        sources.revalidate_sources()
    except (ScoreV2CapabilitySourceError, OSError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.capability_source_changed"
        ) from exc


def _require_input_artifact_identities(
    plan: ScoreV2CapabilityPlan,
    sources: ScoreV2CapabilitySourceSnapshot,
    *,
    capability_plan_sha256: str,
    capability_source_sha256: str,
) -> None:
    try:
        current_plan = plan.artifact_sha256
        current_sources = sources.artifact_sha256
    except (
        ScoreV2CapabilityAdapterError,
        ScoreV2CapabilitySourceError,
        AttributeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_generation_changed"
        ) from exc
    if (
        current_plan != capability_plan_sha256
        or current_sources != capability_source_sha256
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_generation_changed"
        )


class _InputGenerations(NamedTuple):
    plan_document: dict[str, Any]
    source_document: dict[str, Any]
    capability_plan_sha256: str
    capability_source_sha256: str
    roster_projection_sha256: str
    sample_rate: int
    ordered_source_bindings: tuple[ScoreV2ExecutorCapabilityBinding, ...]
    source_bindings: dict[str, ScoreV2ExecutorCapabilityBinding]
    manifest_sources: dict[str, ScoreV2ManifestGeneration]


def _preflight_input_counts(
    plan: ScoreV2CapabilityPlan,
    sources: ScoreV2CapabilitySourceSnapshot,
    *,
    limits: ProjectLimits,
) -> None:
    """Apply cheap structural ceilings before any external-generation I/O."""

    if type(plan) is not ScoreV2CapabilityPlan:
        raise TypeError("capability_plan must be ScoreV2CapabilityPlan")
    if type(sources) is not ScoreV2CapabilitySourceSnapshot:
        raise TypeError(
            "capability_sources must be ScoreV2CapabilitySourceSnapshot"
        )
    try:
        occurrence_count = plan.occurrence_count
        executor_bindings = sources.executor_bindings
    except AttributeError as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        ) from exc
    if type(occurrence_count) is not int or occurrence_count < 0:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        )
    if type(executor_bindings) is not tuple:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        )
    if occurrence_count > limits.max_notes:
        raise ResourceLimitError(
            "runtime_source.too_many_occurrences",
            "Score-v2 runtime source exceeds the occurrence budget",
            actual=occurrence_count,
            limit=limits.max_notes,
        )
    if len(executor_bindings) > limits.max_executors:
        raise ResourceLimitError(
            "runtime_source.too_many_executors",
            "Score-v2 runtime source exceeds the executor budget",
            actual=len(executor_bindings),
            limit=limits.max_executors,
        )


def _input_generations(
    plan: ScoreV2CapabilityPlan,
    sources: ScoreV2CapabilitySourceSnapshot,
    *,
    maximum_executors: int,
) -> _InputGenerations:
    if type(plan) is not ScoreV2CapabilityPlan:
        raise TypeError("capability_plan must be ScoreV2CapabilityPlan")
    if type(sources) is not ScoreV2CapabilitySourceSnapshot:
        raise TypeError(
            "capability_sources must be ScoreV2CapabilitySourceSnapshot"
        )
    try:
        plan_bytes = plan.canonical_bytes
        source_bytes = sources.canonical_bytes
        plan_document = plan.to_dict()
        source_document = sources.to_dict()
        executor_bindings = sources.executor_bindings
        manifest_generations = sources.manifest_generations
    except (
        ScoreV2CapabilityAdapterError,
        ScoreV2CapabilitySourceError,
        AttributeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        ) from exc
    if (
        type(plan_document) is not dict
        or type(source_document) is not dict
        or type(plan_bytes) is not bytes
        or type(source_bytes) is not bytes
        or canonical_json_bytes(plan_document) != plan_bytes
        or canonical_json_bytes(source_document) != source_bytes
        or type(executor_bindings) is not tuple
        or type(manifest_generations) is not tuple
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        )
    if len(executor_bindings) > maximum_executors:
        raise ResourceLimitError(
            "runtime_source.too_many_executors",
            "Score-v2 runtime source exceeds the executor budget",
            actual=len(executor_bindings),
            limit=maximum_executors,
        )
    if not executor_bindings:
        raise ScoreV2RuntimeSourceError("runtime_source.empty_executor_set")
    bindings: dict[str, ScoreV2ExecutorCapabilityBinding] = {}
    for binding in executor_bindings:
        if (
            type(binding) is not ScoreV2ExecutorCapabilityBinding
            or binding.executor_id in bindings
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_artifact_integrity_mismatch"
            )
        bindings[binding.executor_id] = binding
    manifests: dict[str, ScoreV2ManifestGeneration] = {}
    for source in manifest_generations:
        if (
            type(source) is not ScoreV2ManifestGeneration
            or source.source_sha256 in manifests
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_artifact_integrity_mismatch"
            )
        manifests[source.source_sha256] = source
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    roster_hash = source_document.get("roster_projection_sha256")
    sample_rate = plan_document.get("sample_rate")
    if (
        not _is_sha256(plan_hash)
        or not _is_sha256(source_hash)
        or not _is_sha256(roster_hash)
        or type(sample_rate) is not int
        or not 8_000 <= sample_rate <= 384_000
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        )
    return _InputGenerations(
        plan_document=plan_document,
        source_document=source_document,
        capability_plan_sha256=plan_hash,
        capability_source_sha256=source_hash,
        roster_projection_sha256=roster_hash,
        sample_rate=sample_rate,
        ordered_source_bindings=executor_bindings,
        source_bindings=bindings,
        manifest_sources=manifests,
    )


def _validate_cross_bindings(
    plan_document: dict[str, Any],
    *,
    capability_source_sha256: str,
    roster_projection_sha256: str,
    sample_rate: int,
    source_bindings: dict[str, ScoreV2ExecutorCapabilityBinding],
) -> None:
    bindings = plan_document.get("bindings")
    occurrences = plan_document.get("occurrences")
    if (
        type(bindings) is not dict
        or bindings.get("capability_source_sha256")
        != capability_source_sha256
        or bindings.get("roster_projection_sha256")
        != roster_projection_sha256
        or type(plan_document.get("sample_rate")) is not int
        or plan_document.get("sample_rate") != sample_rate
        or plan_document.get("runtime_fingerprint_status") != "not_captured"
        or type(occurrences) is not list
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.capability_binding_mismatch"
        )
    seen_parts: dict[str, str] = {}
    for occurrence in occurrences:
        if type(occurrence) is not dict:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.capability_binding_mismatch"
            )
        executor_id = occurrence.get("executor_id")
        part_id = occurrence.get("part_id")
        occurrence_binding = occurrence.get("capability_binding")
        if (
            type(executor_id) is not str
            or type(part_id) is not str
            or type(occurrence_binding) is not dict
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.capability_binding_mismatch"
            )
        source_binding = source_bindings.get(executor_id)
        if (
            source_binding is None
            or source_binding.part_id != part_id
            or occurrence_binding.get("manifest_source_sha256")
            != source_binding.manifest_source_sha256
            or occurrence_binding.get("capability_projection_sha256")
            != source_binding.capability_projection_sha256
            or occurrence_binding.get("effective_manifest_sha256")
            != source_binding.effective_manifest_sha256
            or occurrence_binding.get("runtime_fingerprint_status")
            != "not_captured"
        ):
            raise ScoreV2RuntimeSourceError(
                "runtime_source.capability_binding_mismatch"
            )
        previous = seen_parts.setdefault(part_id, executor_id)
        if previous != executor_id:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.capability_binding_mismatch"
            )


def _effective_manifest(
    source: ScoreV2ManifestGeneration,
    binding: ScoreV2ExecutorCapabilityBinding,
) -> dict[str, Any]:
    if (
        binding.manifest_source_sha256 != source.source_sha256
        or binding.custom_implementation_blocked is not False
        or binding.execution_eligibility != "pending_runtime_fingerprint"
        or binding.runtime_fingerprint_status != "not_captured"
        or binding.runtime_fingerprint_sha256 is not None
    ):
        code = (
            "runtime_source.custom_implementation_blocked"
            if binding.custom_implementation_blocked
            else "runtime_source.execution_eligibility_mismatch"
        )
        raise ScoreV2RuntimeSourceError(code)
    try:
        manifest = source.manifest_copy()
        effective = {**manifest, **dict(binding.overrides)}
        effective_bytes = canonical_json_bytes(effective)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        ) from exc
    if (
        source.custom_implementation_blocked is not False
        or hashlib.sha256(effective_bytes).hexdigest()
        != binding.effective_manifest_canonical_sha256
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.custom_implementation_blocked"
        )
    return effective


def _asset_inventory_status(
    effective_manifest: dict[str, Any],
    fingerprint: dict[str, Any],
) -> str:
    graph = fingerprint.get("runtime_asset_graph")
    if type(graph) is not dict or type(graph.get("file_count")) is not int:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_invalid"
        )
    file_count = graph["file_count"]
    external_assets = effective_manifest.get("external_audio_assets")
    malformed_asset_fields = (
        (
            "external_audio_assets" in effective_manifest
            and type(external_assets) is not list
        )
        or (
            "asset_root" in effective_manifest
            and (
                type(effective_manifest["asset_root"]) is not str
                or not effective_manifest["asset_root"].strip()
            )
        )
        or (
            "soundfont" in effective_manifest
            and (
                type(effective_manifest["soundfont"]) is not str
                or not effective_manifest["soundfont"].strip()
            )
        )
        or (
            "sample" in effective_manifest
            and (
                type(effective_manifest["sample"]) is not str
                or not effective_manifest["sample"].strip()
            )
        )
        or (
            "regions" in effective_manifest
            and type(effective_manifest["regions"]) is not list
        )
    )
    if malformed_asset_fields:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.asset_inventory_declaration_mismatch"
        )
    declared_project_dsp = (
        effective_manifest.get("provenance_kind")
        == "project_authored_dsp"
        and type(external_assets) is list
        and external_assets == []
    )
    declared_asset_free_runtime = (
        effective_manifest.get("runtime_asset_policy")
        == "no_external_audio_assets"
    )
    explicit_asset_fields = (
        effective_manifest.get("type") in {"sample", "soundfont"}
        or (
            type(external_assets) is list
            and bool(external_assets)
        )
        or (
            type(effective_manifest.get("asset_root")) is str
            and bool(effective_manifest["asset_root"].strip())
        )
        or bool(effective_manifest.get("soundfont"))
        or bool(effective_manifest.get("sample"))
        or bool(effective_manifest.get("regions"))
    )
    declared_asset_free = (
        declared_project_dsp or declared_asset_free_runtime
    ) and not explicit_asset_fields
    if file_count == 0:
        if declared_asset_free:
            return NO_EXTERNAL_ASSET_INVENTORY_STATUS
        raise ScoreV2RuntimeSourceError(
            "runtime_source.empty_runtime_asset_graph"
        )
    if file_count > 0:
        if declared_asset_free_runtime or declared_project_dsp:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.asset_inventory_declaration_mismatch"
            )
        return NONEMPTY_ASSET_INVENTORY_STATUS
    raise ScoreV2RuntimeSourceError(
        "runtime_source.runtime_fingerprint_invalid"
    )


def _validate_runtime_binding_source(
    runtime_binding: ScoreV2ExecutorRuntimeBinding,
    *,
    source_binding: ScoreV2ExecutorCapabilityBinding,
    source: ScoreV2ManifestGeneration,
    effective_manifest: dict[str, Any],
) -> None:
    if (
        runtime_binding.executor_order != source_binding.executor_order
        or runtime_binding.executor_id != source_binding.executor_id
        or runtime_binding.part_id != source_binding.part_id
        or runtime_binding.manifest_source_sha256
        != source_binding.manifest_source_sha256
        or runtime_binding.manifest_source_sha256 != source.source_sha256
        or runtime_binding.manifest_raw_sha256 != source.raw_sha256
        or runtime_binding.manifest_canonical_sha256
        != source.manifest_canonical_sha256
        or runtime_binding.capability_projection_sha256
        != source_binding.capability_projection_sha256
        or runtime_binding.effective_manifest_canonical_sha256
        != source_binding.effective_manifest_canonical_sha256
        or runtime_binding.effective_manifest_sha256
        != source_binding.effective_manifest_sha256
    ):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_generation_changed"
        )
    current_status = _asset_inventory_status(
        effective_manifest,
        runtime_binding.fingerprint_copy(),
    )
    if current_status != runtime_binding.asset_inventory_status:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_generation_changed"
        )


def _capture_runtime_binding(
    *,
    source_binding: ScoreV2ExecutorCapabilityBinding,
    source: ScoreV2ManifestGeneration,
    project_root: Path,
    capability_plan_sha256: str,
    capability_source_sha256: str,
    roster_projection_sha256: str,
    sample_rate: int,
    maximum_bytes: int,
) -> ScoreV2ExecutorRuntimeBinding:
    effective_manifest = _effective_manifest(source, source_binding)
    try:
        computed = compute_runtime_fingerprint(
            project_root,
            source.manifest_path,
            effective_manifest=effective_manifest,
            sample_rate_hz=sample_rate,
        )
    except (OnsetEvidenceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_failed"
        ) from exc
    detached, fingerprint_bytes = _bounded_object(
        computed,
        maximum_bytes=maximum_bytes,
        error_code="runtime_source.runtime_fingerprint_invalid",
    )
    asset_inventory_status = _asset_inventory_status(
        effective_manifest,
        detached,
    )
    evidence = _fingerprint_evidence(
        detached,
        sample_rate=sample_rate,
        manifest_raw_sha256=source.raw_sha256,
        asset_inventory_status=asset_inventory_status,
    )
    try:
        validated = validate_runtime_fingerprint(
            detached,
            project_root=project_root,
            manifest_path=source.manifest_path,
            effective_manifest=effective_manifest,
            sample_rate_hz=sample_rate,
        )
        _validated_document, validated_bytes = _bounded_object(
            validated,
            maximum_bytes=maximum_bytes,
            error_code="runtime_source.runtime_fingerprint_invalid",
        )
    except ResourceLimitError:
        raise
    except ScoreV2RuntimeSourceError:
        raise
    except (OnsetEvidenceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_fingerprint_failed"
        ) from exc
    if validated_bytes != fingerprint_bytes:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.runtime_generation_changed"
        )
    return ScoreV2ExecutorRuntimeBinding(
        executor_order=source_binding.executor_order,
        executor_id=source_binding.executor_id,
        part_id=source_binding.part_id,
        capability_plan_sha256=capability_plan_sha256,
        capability_source_sha256=capability_source_sha256,
        roster_projection_sha256=roster_projection_sha256,
        manifest_source_sha256=source_binding.manifest_source_sha256,
        manifest_raw_sha256=source.raw_sha256,
        manifest_canonical_sha256=source.manifest_canonical_sha256,
        capability_projection_sha256=(
            source_binding.capability_projection_sha256
        ),
        effective_manifest_canonical_sha256=(
            source_binding.effective_manifest_canonical_sha256
        ),
        effective_manifest_sha256=source_binding.effective_manifest_sha256,
        sample_rate=sample_rate,
        legacy_runtime_fingerprint_canonical_bytes=fingerprint_bytes,
        legacy_runtime_fingerprint_sha256=hashlib.sha256(
            fingerprint_bytes
        ).hexdigest(),
        render_python_closure_sha256=str(
            evidence["render_python_closure_sha256"]
        ),
        runtime_dependencies_sha256=str(
            evidence["runtime_dependencies_sha256"]
        ),
        asset_inventory_status=asset_inventory_status,
    )


def capture_score_v2_runtime_sources(
    capability_plan: ScoreV2CapabilityPlan,
    capability_sources: ScoreV2CapabilitySourceSnapshot,
    *,
    project_root: str | os.PathLike[str],
    limits: ProjectLimits | None = None,
) -> ScoreV2RuntimeSourceSnapshot:
    """Capture one bounded legacy runtime generation for every executor."""

    active_limits = _active_limits(limits)
    _preflight_input_counts(
        capability_plan,
        capability_sources,
        limits=active_limits,
    )
    # This is intentionally the first external-generation operation.
    _revalidate_capability_sources(capability_sources)
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.project_root_unavailable"
        ) from exc
    if not root.is_dir():
        raise ScoreV2RuntimeSourceError(
            "runtime_source.project_root_unavailable"
        )
    try:
        catalogue_root = Path(capability_sources.catalogue_root.path).resolve()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeSourceError(
            "runtime_source.input_artifact_integrity_mismatch"
        ) from exc
    if not catalogue_root.is_relative_to(root):
        raise ScoreV2RuntimeSourceError(
            "runtime_source.catalogue_outside_project_root"
        )
    inputs = _input_generations(
        capability_plan,
        capability_sources,
        maximum_executors=active_limits.max_executors,
    )
    _validate_cross_bindings(
        inputs.plan_document,
        capability_source_sha256=inputs.capability_source_sha256,
        roster_projection_sha256=inputs.roster_projection_sha256,
        sample_rate=inputs.sample_rate,
        source_bindings=inputs.source_bindings,
    )
    plan_hash = inputs.capability_plan_sha256
    source_hash = inputs.capability_source_sha256
    roster_hash = inputs.roster_projection_sha256
    sample_rate = inputs.sample_rate

    runtime_bindings: list[ScoreV2ExecutorRuntimeBinding] = []
    empty_document = _artifact_document(
        project_root=str(root),
        capability_plan_sha256=plan_hash,
        capability_source_sha256=source_hash,
        roster_projection_sha256=roster_hash,
        sample_rate=sample_rate,
        executor_bindings=(),
    )
    charged_bytes = len(canonical_json_bytes(empty_document))
    if charged_bytes > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "runtime_source.document_too_large",
            "Score-v2 runtime source exceeds the plan JSON byte budget",
            actual=charged_bytes,
            limit=active_limits.max_plan_json_bytes,
        )
    for order, source_binding in enumerate(inputs.ordered_source_bindings):
        if source_binding.executor_order != order:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_artifact_integrity_mismatch"
            )
        source = inputs.manifest_sources.get(
            source_binding.manifest_source_sha256
        )
        if source is None:
            raise ScoreV2RuntimeSourceError(
                "runtime_source.input_artifact_integrity_mismatch"
            )
        runtime_binding = _capture_runtime_binding(
            source_binding=source_binding,
            source=source,
            project_root=root,
            capability_plan_sha256=plan_hash,
            capability_source_sha256=source_hash,
            roster_projection_sha256=roster_hash,
            sample_rate=sample_rate,
            maximum_bytes=active_limits.max_plan_json_bytes,
        )
        # Incrementally reject gross amplification before retaining the next
        # binding.  The final bounded encoding below remains authoritative for
        # exact structural bytes (including the executor_count digits).
        prospective_bytes = (
            charged_bytes
            + len(canonical_json_bytes(runtime_binding.to_dict()))
            + (1 if runtime_bindings else 0)
        )
        if prospective_bytes > active_limits.max_plan_json_bytes:
            raise ResourceLimitError(
                "runtime_source.document_too_large",
                "Score-v2 runtime source exceeds the plan JSON byte budget",
                actual=prospective_bytes,
                limit=active_limits.max_plan_json_bytes,
            )
        runtime_bindings.append(runtime_binding)
        charged_bytes = prospective_bytes
    executor_bindings = tuple(runtime_bindings)

    # Reject callbacks that changed either sealed input while fingerprints were
    # being computed, then descriptor-revalidate the manifest generation last.
    _require_input_artifact_identities(
        capability_plan,
        capability_sources,
        capability_plan_sha256=plan_hash,
        capability_source_sha256=source_hash,
    )
    _revalidate_capability_sources(capability_sources)

    document = _artifact_document(
        project_root=str(root),
        capability_plan_sha256=plan_hash,
        capability_source_sha256=source_hash,
        roster_projection_sha256=roster_hash,
        sample_rate=sample_rate,
        executor_bindings=executor_bindings,
    )
    try:
        payload = bounded_canonical_json_bytes(
            document,
            limits=_json_limits(active_limits.max_plan_json_bytes),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise ResourceLimitError(
            "runtime_source.document_too_large",
            "Score-v2 runtime source exceeds the plan JSON byte budget",
            actual=exc.actual,
            limit=active_limits.max_plan_json_bytes,
        ) from exc
    artifact_hash = hashlib.sha256(payload).hexdigest()
    result = object.__new__(ScoreV2RuntimeSourceSnapshot)
    for name, value in (
        ("project_root", str(root)),
        ("capability_plan_sha256", plan_hash),
        ("capability_source_sha256", source_hash),
        ("roster_projection_sha256", roster_hash),
        ("sample_rate", sample_rate),
        ("executor_bindings", executor_bindings),
        ("_capability_plan", capability_plan),
        ("_capability_sources", capability_sources),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", artifact_hash),
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(
        result,
        "_identity_seal",
        (
            str(root),
            plan_hash,
            source_hash,
            roster_hash,
            sample_rate,
            executor_bindings,
            capability_plan,
            capability_sources,
            payload,
            artifact_hash,
            SCORE_V2_RUNTIME_SOURCE_CONTRACT,
        ),
    )
    result._trusted_artifact_bytes()
    # A final whole-set pass recomputes closure, dependency, calibration and
    # aggregate asset identities for every executor.  It is intentionally a
    # sequential observation, not a claim that unrelated files were frozen at
    # one instant; the artifact remains explicitly non-render-authoritative.
    result.revalidate_runtime_sources()
    return result


__all__ = [
    "ASSET_DESCRIPTOR_STATUS",
    "NONEMPTY_ASSET_INVENTORY_STATUS",
    "NO_EXTERNAL_ASSET_INVENTORY_STATUS",
    "RUNTIME_FINGERPRINT_STATUS",
    "SCORE_V2_RUNTIME_SOURCE_CONTRACT",
    "SCORE_V2_RUNTIME_SOURCE_KIND",
    "SCORE_V2_RUNTIME_SOURCE_SCHEMA_VERSION",
    "ScoreV2ExecutorRuntimeBinding",
    "ScoreV2RuntimeSourceError",
    "ScoreV2RuntimeSourceSnapshot",
    "capture_score_v2_runtime_sources",
]
