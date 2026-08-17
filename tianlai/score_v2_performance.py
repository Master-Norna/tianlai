"""Compile sealed Score-v2 plans into legacy performance transport JSON.

The bundle produced here is deliberately not render authority.  It binds the
exact score plan, capability-resolution plan and captured runtime generation,
then serializes the already-resolved values into the existing standalone
performance protocol.  A future v2 renderer must still define dispatch at the
exclusive final frame; this boundary records that fact instead of extending
the performance by a hidden sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
import math
from typing import Any, NamedTuple

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .events import parse_performance_document
from .resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    performance_event_limit,
)
from .score_v2_capability_adapter import (
    ScoreV2CapabilityAdapterError,
    ScoreV2CapabilityPlan,
)
from .score_v2_plan import ScoreV2Plan, ScoreV2PlanError
from .score_v2_runtime_source import (
    RUNTIME_FINGERPRINT_STATUS,
    ScoreV2RuntimeSourceError,
    ScoreV2RuntimeSourceSnapshot,
    _ScoreV2ExecutorExecutionInput,
)


SCORE_V2_PERFORMANCE_KIND = "tianlai.score_v2_performance_bundle"
SCORE_V2_PERFORMANCE_SCHEMA_VERSION = 1
SCORE_V2_PERFORMANCE_CONTRACT = (
    "score-v2-performance-transport-v1-not-render-authority"
)
ENDPOINT_DISPATCH_STATUS = "pending_v2_renderer"

_HEX = frozenset("0123456789abcdef")
_CAPABILITY_OCCURRENCE_FIELDS = {
    "occurrence_id",
    "part_id",
    "executor_id",
    "source_event_ids",
    "source_tie_ids",
    "start_sample",
    "end_sample",
    "articulation",
    "range",
    "pitch",
    "velocity",
    "capability_binding",
}


class ScoreV2PerformanceError(ValueError):
    """A stable, non-reflective performance-transport failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2Performance.{code.replace('.', '_')}"
        super().__init__(code)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
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


def _json_limits(maximum_bytes: int, *, maximum_items: int) -> AuthoringJsonLimits:
    return AuthoringJsonLimits(
        max_document_bytes=maximum_bytes,
        max_depth=128,
        max_nodes=max(1_000, maximum_bytes * 2),
        max_string_bytes=min(maximum_bytes, 1024 * 1024),
        max_array_items=max(1, maximum_items),
        max_object_members=65_536,
    )


def _bounded_document(
    document: dict[str, object],
    *,
    maximum_bytes: int,
    maximum_items: int,
    resource_code: str,
) -> tuple[dict[str, Any], bytes]:
    limits = _json_limits(maximum_bytes, maximum_items=maximum_items)
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
        if exc.code == "document_too_large":
            raise ResourceLimitError(
                resource_code,
                "Score-v2 performance transport exceeds its JSON budget",
                actual=exc.actual,
                limit=maximum_bytes,
            ) from exc
        raise ScoreV2PerformanceError("performance.nonportable_json") from exc
    if type(detached) is not dict or canonical_json_bytes(detached) != payload:
        raise ScoreV2PerformanceError("performance.canonical_roundtrip_mismatch")
    return detached, payload


def _fraction(value: object) -> Fraction:
    try:
        result = value.as_fraction()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        ) from exc
    if type(result) is not Fraction:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        )
    return result


def _document_fraction(value: object) -> Fraction:
    if type(value) is not dict or set(value) != {"numerator", "denominator"}:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        )
    numerator = value["numerator"]
    denominator = value["denominator"]
    if (
        type(numerator) is not str
        or type(denominator) is not str
        or not numerator
        or not denominator
        or len(numerator) > 256
        or len(denominator) > 256
    ):
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        )
    try:
        result = Fraction(int(numerator), int(denominator))
    except (ValueError, ZeroDivisionError) as exc:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        ) from exc
    return result


def _sample_time(sample: int, sample_rate: int) -> float:
    if type(sample) is not int or sample < 0:
        raise ScoreV2PerformanceError("performance.sample_invalid")
    value = sample / sample_rate
    if not math.isfinite(value) or round(value * sample_rate) != sample:
        raise ScoreV2PerformanceError(
            "performance.sample_time_not_representable"
        )
    return value


class ScoreV2PerformanceEventSidecar(NamedTuple):
    sequence: int
    occurrence_id: str
    role: str
    note_id: int
    expected_sample: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "occurrence_id": self.occurrence_id,
            "role": self.role,
            "note_id": self.note_id,
            "expected_sample": self.expected_sample,
        }


class _ScoreV2ExecutorLocalExecutionInput(NamedTuple):
    """Package-local bridge from sealed transport to local execution."""

    executor_order: int
    executor_id: str
    part_id: str
    performance_canonical_bytes: bytes
    event_sidecar_canonical_bytes: bytes
    performance_sha256: str
    event_sidecar_sha256: str
    runtime: _ScoreV2ExecutorExecutionInput


class _PendingEvent(NamedTuple):
    sample: int
    exact_time: Fraction
    phase: int
    source_order: int
    occurrence_id: str
    within_occurrence: int
    role: str
    note_id: int
    document: dict[str, object]


def _resolved_articulation(value: object) -> str | None:
    if type(value) is not dict:
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    if set(value) == {"status", "requested_value"}:
        if value.get("status") != "not_applicable" or value.get(
            "requested_value"
        ) is not None:
            raise ScoreV2PerformanceError("performance.resolution_invalid")
        return None
    resolved = value.get("resolved_value")
    if (
        type(resolved) is not str
        or not resolved
        or value.get("application") != "note_on_latched"
        or value.get("fidelity") == "ignored"
        or value.get("semantic_fidelity") == "ignored"
    ):
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    return resolved


def _resolved_pitch(value: object) -> tuple[str, float]:
    if type(value) is not dict or type(value.get("capability_resolution")) is not dict:
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    resolution = value["capability_resolution"]
    resolved = resolution.get("resolved_value")
    protocol = resolution.get("protocol_input")
    if (
        type(resolved) not in (int, float)
        or isinstance(resolved, bool)
        or not math.isfinite(float(resolved))
        or resolution.get("application") != "note_on_latched"
        or resolution.get("value_unit") != "midi_note_at_a4_440"
        or resolution.get("mode") not in {"continuous", "quantized"}
        or resolution.get("fidelity") == "ignored"
        or resolution.get("semantic_fidelity") == "ignored"
        or protocol not in {"midi_note", "pitch_hz", "midi_note_or_pitch_hz"}
    ):
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    midi = float(resolved)
    if protocol == "pitch_hz":
        try:
            pitch_hz = 440.0 * (2.0 ** ((midi - 69.0) / 12.0))
        except OverflowError as exc:
            raise ScoreV2PerformanceError("performance.resolution_invalid") from exc
        if not math.isfinite(pitch_hz) or pitch_hz <= 0.0:
            raise ScoreV2PerformanceError("performance.resolution_invalid")
        return "pitch_hz", pitch_hz
    return "midi_note", midi


def _resolved_velocity(value: object) -> float:
    if type(value) is not dict or type(value.get("capability_resolution")) is not dict:
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    resolution = value["capability_resolution"]
    resolved = resolution.get("resolved_value")
    if (
        type(resolved) not in (int, float)
        or isinstance(resolved, bool)
        or not math.isfinite(float(resolved))
        or not 0.0 <= float(resolved) <= 1.0
        or resolution.get("fidelity") == "ignored"
        or resolution.get("semantic_fidelity") == "ignored"
    ):
        raise ScoreV2PerformanceError("performance.resolution_invalid")
    return float(resolved)


def _runtime_binding_document(
    binding: dict[str, Any],
) -> dict[str, object]:
    evidence = binding.get("runtime_evidence")
    if type(evidence) is not dict or set(evidence) != {
        "render_python_closure_sha256",
        "runtime_dependencies_sha256",
        "local_implementation",
        "resource_verification",
        "pitch_calibration",
        "runtime_asset_graph",
        "asset_inventory_status",
        "asset_descriptor_status",
    }:
        raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
    graph = evidence.get("runtime_asset_graph")
    if (
        type(graph) is not dict
        or not _is_sha256(graph.get("sha256"))
        or type(binding.get("asset_inventory_status")) is not str
        or evidence.get("asset_inventory_status")
        != binding.get("asset_inventory_status")
    ):
        raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
    result = {
        "manifest_source_sha256": binding.get("manifest_source_sha256"),
        "manifest_raw_sha256": binding.get("manifest_raw_sha256"),
        "manifest_canonical_sha256": binding.get("manifest_canonical_sha256"),
        "capability_projection_sha256": binding.get(
            "capability_projection_sha256"
        ),
        "effective_manifest_canonical_sha256": (
            binding.get("effective_manifest_canonical_sha256")
        ),
        "effective_manifest_sha256": binding.get("effective_manifest_sha256"),
        "runtime_fingerprint_status": RUNTIME_FINGERPRINT_STATUS,
        "legacy_runtime_fingerprint_sha256": (
            binding.get("legacy_runtime_fingerprint_sha256")
        ),
        "render_python_closure_sha256": evidence.get(
            "render_python_closure_sha256"
        ),
        "runtime_dependencies_sha256": evidence.get(
            "runtime_dependencies_sha256"
        ),
        "asset_inventory_status": binding.get("asset_inventory_status"),
        "runtime_asset_graph_sha256": graph.get("sha256"),
    }
    if any(
        not _is_sha256(value)
        for key, value in result.items()
        if key
        not in {"runtime_fingerprint_status", "asset_inventory_status"}
    ) or binding.get("runtime_fingerprint_status") != RUNTIME_FINGERPRINT_STATUS:
        raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
    return result


def _verify_performance(
    document: dict[str, Any],
    sidecars: list[dict[str, object]],
    *,
    sample_rate: int,
    frame_count: int,
) -> int:
    if (
        set(document)
        != {
            "sample_rate",
            "channels",
            "duration_seconds",
            "tail_seconds",
            "tuning",
            "events",
        }
        or document.get("sample_rate") != sample_rate
        or document.get("channels") != 2
        or document.get("tail_seconds") != 0.0
        or document.get("tuning")
        != {"temperament": "equal", "a4_hz": 440.0}
        or type(document.get("events")) is not list
        or len(document["events"]) != len(sidecars)
    ):
        raise ScoreV2PerformanceError("performance.performance_roundtrip_mismatch")
    try:
        parsed = parse_performance_document(document)
    except (TypeError, ValueError) as exc:
        raise ScoreV2PerformanceError(
            "performance.performance_roundtrip_mismatch"
        ) from exc
    if parsed.sample_rate != sample_rate or parsed.total_samples != frame_count:
        raise ScoreV2PerformanceError("performance.performance_roundtrip_mismatch")
    active: dict[int, str] = {}
    endpoint_count = 0
    for sequence, (raw, event, sidecar) in enumerate(
        zip(document["events"], parsed.events, sidecars, strict=True)
    ):
        if (
            type(raw) is not dict
            or sidecar.get("sequence") != sequence
            or event.sequence != sequence
            or event.sample != sidecar.get("expected_sample")
            or event.type != sidecar.get("role")
            or raw.get("type") != event.type
            or round(float(raw.get("time", -1.0)) * sample_rate) != event.sample
        ):
            raise ScoreV2PerformanceError(
                "performance.performance_roundtrip_mismatch"
            )
        note_id = sidecar.get("note_id")
        occurrence_id = sidecar.get("occurrence_id")
        if type(note_id) is not int or type(occurrence_id) is not str:
            raise ScoreV2PerformanceError("performance.event_pairing_mismatch")
        if event.type == "articulation":
            if sequence + 1 >= len(sidecars) or (
                sidecars[sequence + 1].get("role") != "note_on"
                or sidecars[sequence + 1].get("note_id") != note_id
                or sidecars[sequence + 1].get("occurrence_id") != occurrence_id
            ):
                raise ScoreV2PerformanceError("performance.event_pairing_mismatch")
        elif event.type == "note_on":
            if note_id in active or event.payload.get("source_event_id") != occurrence_id:
                raise ScoreV2PerformanceError("performance.event_pairing_mismatch")
            active[note_id] = occurrence_id
        elif event.type == "note_off":
            if (
                active.pop(note_id, None) != occurrence_id
                or event.payload.get("source_event_id") != occurrence_id
            ):
                raise ScoreV2PerformanceError("performance.event_pairing_mismatch")
        if event.sample == frame_count:
            endpoint_count += 1
        elif event.sample > frame_count:
            raise ScoreV2PerformanceError("performance.endpoint_out_of_range")
    if active:
        raise ScoreV2PerformanceError("performance.event_pairing_mismatch")
    return endpoint_count


def _verify_against_retained_inputs(
    document: dict[str, Any],
    *,
    score_plan: ScoreV2Plan,
    capability_plan: ScoreV2CapabilityPlan,
    runtime_sources: ScoreV2RuntimeSourceSnapshot,
) -> None:
    """Rebuild every executor transport from the three retained artifacts."""

    score_document = score_plan.to_dict()
    capability_document = capability_plan.to_dict()
    runtime_document = runtime_sources.to_dict()
    score_bytes = score_plan.canonical_bytes
    capability_bytes = capability_plan.canonical_bytes
    runtime_bytes = runtime_sources.canonical_bytes
    score_hash = hashlib.sha256(score_bytes).hexdigest()
    capability_hash = hashlib.sha256(capability_bytes).hexdigest()
    runtime_hash = hashlib.sha256(runtime_bytes).hexdigest()
    bindings = document["bindings"]
    score_bindings = score_document.get("bindings")
    capability_bindings = capability_document.get("bindings")
    runtime_bindings = runtime_document.get("bindings")
    score_occurrences = score_document.get("occurrences")
    capability_occurrences = capability_document.get("occurrences")
    runtime_executors = runtime_document.get("executors")
    output_executors = document.get("executors")
    sample_rate = document.get("sample_rate")
    frame_count = document.get("frame_count")
    if (
        type(bindings) is not dict
        or type(score_bindings) is not dict
        or type(capability_bindings) is not dict
        or type(runtime_bindings) is not dict
        or type(score_occurrences) is not list
        or type(capability_occurrences) is not list
        or type(runtime_executors) is not list
        or type(output_executors) is not list
        or type(sample_rate) is not int
        or type(frame_count) is not int
        or canonical_json_bytes(score_document) != score_bytes
        or canonical_json_bytes(capability_document) != capability_bytes
        or canonical_json_bytes(runtime_document) != runtime_bytes
        or bindings.get("score_v2_plan_sha256") != score_hash
        or bindings.get("capability_plan_sha256") != capability_hash
        or bindings.get("runtime_source_sha256") != runtime_hash
        or bindings.get("source_document_sha256")
        != score_bindings.get("source_document_sha256")
        or capability_bindings.get("source_document_sha256")
        != score_bindings.get("source_document_sha256")
        or bindings.get("score_render_projection_sha256")
        != score_bindings.get("score_render_projection_sha256")
        or capability_bindings.get("score_render_projection_sha256")
        != score_bindings.get("score_render_projection_sha256")
        or bindings.get("execution_profile_sha256")
        != capability_bindings.get("execution_profile_sha256")
        or bindings.get("capability_source_sha256")
        != capability_bindings.get("capability_source_sha256")
        or runtime_bindings.get("capability_source_sha256")
        != capability_bindings.get("capability_source_sha256")
        or bindings.get("roster_projection_sha256")
        != capability_bindings.get("roster_projection_sha256")
        or runtime_bindings.get("roster_projection_sha256")
        != capability_bindings.get("roster_projection_sha256")
        or capability_bindings.get("score_v2_plan_sha256") != score_hash
        or runtime_bindings.get("capability_plan_sha256") != capability_hash
        or score_document.get("sample_rate") != sample_rate
        or capability_document.get("sample_rate") != sample_rate
        or runtime_bindings.get("sample_rate") != sample_rate
        or len(score_occurrences) != len(capability_occurrences)
        or len(runtime_executors) != len(output_executors)
    ):
        raise ScoreV2PerformanceError("performance.integrity_mismatch")

    records: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    runtime_by_id: dict[str, dict[str, Any]] = {}
    for order, runtime_binding in enumerate(runtime_executors):
        if type(runtime_binding) is not dict:
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        executor_id = runtime_binding.get("executor_id")
        if (
            runtime_binding.get("executor_order") != order
            or type(executor_id) is not str
            or not executor_id
            or executor_id in runtime_by_id
        ):
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        runtime_by_id[executor_id] = runtime_binding
        records[executor_id] = []

    seen_occurrences: set[str] = set()
    for score_occurrence, capability_occurrence in zip(
        score_occurrences,
        capability_occurrences,
        strict=True,
    ):
        if (
            type(score_occurrence) is not dict
            or type(capability_occurrence) is not dict
        ):
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        occurrence_id = score_occurrence.get("occurrence_id")
        executor_id = capability_occurrence.get("executor_id")
        runtime_binding = runtime_by_id.get(executor_id)
        score_start = score_occurrence.get("start")
        score_end = score_occurrence.get("end")
        capability_binding = capability_occurrence.get("capability_binding")
        if (
            type(occurrence_id) is not str
            or not occurrence_id
            or occurrence_id in seen_occurrences
            or type(executor_id) is not str
            or runtime_binding is None
            or type(score_start) is not dict
            or type(score_end) is not dict
            or type(capability_binding) is not dict
            or capability_occurrence.get("occurrence_id") != occurrence_id
            or capability_occurrence.get("part_id")
            != score_occurrence.get("part_id")
            or runtime_binding.get("part_id") != score_occurrence.get("part_id")
            or capability_occurrence.get("source_event_ids")
            != score_occurrence.get("source_event_ids")
            or capability_occurrence.get("source_tie_ids")
            != score_occurrence.get("source_tie_ids")
            or capability_occurrence.get("start_sample")
            != score_start.get("resolved_sample")
            or capability_occurrence.get("end_sample")
            != score_end.get("resolved_sample")
            or capability_binding.get("manifest_source_sha256")
            != runtime_binding.get("manifest_source_sha256")
            or capability_binding.get("capability_projection_sha256")
            != runtime_binding.get("capability_projection_sha256")
            or capability_binding.get("effective_manifest_sha256")
            != runtime_binding.get("effective_manifest_sha256")
        ):
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        seen_occurrences.add(occurrence_id)
        records[executor_id].append((score_occurrence, capability_occurrence))

    for runtime_binding, actual_executor in zip(
        runtime_executors,
        output_executors,
        strict=True,
    ):
        if type(actual_executor) is not dict:
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        executor_id = runtime_binding["executor_id"]
        pending: list[_PendingEvent] = []
        for note_id, (score_occurrence, capability_occurrence) in enumerate(
            records[executor_id],
            start=1,
        ):
            score_start = score_occurrence["start"]
            score_end = score_occurrence["end"]
            start_sample = score_start["resolved_sample"]
            end_sample = score_end["resolved_sample"]
            start_exact = _document_fraction(score_start["requested_seconds"])
            end_exact = _document_fraction(score_end["requested_seconds"])
            source_order = score_occurrence["source_order"]
            occurrence_id = score_occurrence["occurrence_id"]
            if (
                type(start_sample) is not int
                or type(end_sample) is not int
                or type(source_order) is not int
            ):
                raise ScoreV2PerformanceError("performance.integrity_mismatch")
            start_time = _sample_time(start_sample, sample_rate)
            end_time = _sample_time(end_sample, sample_rate)
            articulation = _resolved_articulation(
                capability_occurrence["articulation"]
            )
            if articulation is not None:
                pending.append(
                    _PendingEvent(
                        start_sample,
                        start_exact,
                        1,
                        source_order,
                        occurrence_id,
                        0,
                        "articulation",
                        note_id,
                        {
                            "time": start_time,
                            "type": "articulation",
                            "name": articulation,
                        },
                    )
                )
            pitch_name, pitch_value = _resolved_pitch(
                capability_occurrence["pitch"]
            )
            pending.append(
                _PendingEvent(
                    start_sample,
                    start_exact,
                    1,
                    source_order,
                    occurrence_id,
                    1,
                    "note_on",
                    note_id,
                    {
                        "time": start_time,
                        "type": "note_on",
                        "note_id": note_id,
                        pitch_name: pitch_value,
                        "velocity": _resolved_velocity(
                            capability_occurrence["velocity"]
                        ),
                        "source_event_id": occurrence_id,
                    },
                )
            )
            pending.append(
                _PendingEvent(
                    end_sample,
                    end_exact,
                    0,
                    source_order,
                    occurrence_id,
                    0,
                    "note_off",
                    note_id,
                    {
                        "time": end_time,
                        "type": "note_off",
                        "note_id": note_id,
                        "source_event_id": occurrence_id,
                    },
                )
            )
        pending.sort(
            key=lambda item: (
                item.sample,
                item.exact_time,
                item.phase,
                item.source_order,
                item.occurrence_id,
                item.within_occurrence,
            )
        )
        expected_events = [item.document for item in pending]
        expected_sidecars = [
            ScoreV2PerformanceEventSidecar(
                sequence=sequence,
                occurrence_id=item.occurrence_id,
                role=item.role,
                note_id=item.note_id,
                expected_sample=item.sample,
            ).to_dict()
            for sequence, item in enumerate(pending)
        ]
        expected_performance = {
            "sample_rate": sample_rate,
            "channels": 2,
            "duration_seconds": frame_count / sample_rate,
            "tail_seconds": 0.0,
            "tuning": {"temperament": "equal", "a4_hz": 440.0},
            "events": expected_events,
        }
        expected_performance_bytes = canonical_json_bytes(expected_performance)
        expected_executor = {
            "executor_order": runtime_binding["executor_order"],
            "executor_id": executor_id,
            "part_id": runtime_binding["part_id"],
            "endpoint_dispatch_status": ENDPOINT_DISPATCH_STATUS,
            "runtime_binding": _runtime_binding_document(runtime_binding),
            "performance_canonical_json_bytes": len(expected_performance_bytes),
            "performance_sha256": hashlib.sha256(
                expected_performance_bytes
            ).hexdigest(),
            "event_count": len(expected_events),
            "event_sidecar_sha256": canonical_json_sha256(expected_sidecars),
            "performance": expected_performance,
            "event_sidecar": expected_sidecars,
        }
        if actual_executor != expected_executor:
            raise ScoreV2PerformanceError("performance.integrity_mismatch")


def _artifact_document(
    *,
    score_plan_hash: str,
    capability_plan_hash: str,
    runtime_source_hash: str,
    source_hash: str,
    projection_hash: str,
    profile_hash: str,
    capability_source_hash: str,
    roster_hash: str,
    sample_rate: int,
    frame_count: int,
    occurrence_count: int,
    event_count: int,
    endpoint_count: int,
    executors_hash: str,
    executors: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_PERFORMANCE_KIND,
        "schema_version": SCORE_V2_PERFORMANCE_SCHEMA_VERSION,
        "contract": SCORE_V2_PERFORMANCE_CONTRACT,
        "render_authority": False,
        "endpoint_dispatch_status": ENDPOINT_DISPATCH_STATUS,
        "bindings": {
            "score_v2_plan_sha256": score_plan_hash,
            "capability_plan_sha256": capability_plan_hash,
            "runtime_source_sha256": runtime_source_hash,
            "source_document_sha256": source_hash,
            "score_render_projection_sha256": projection_hash,
            "execution_profile_sha256": profile_hash,
            "capability_source_sha256": capability_source_hash,
            "roster_projection_sha256": roster_hash,
        },
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate,
        "occurrence_count": occurrence_count,
        "executor_count": len(executors),
        "event_count": event_count,
        "frame_count_endpoint_event_count": endpoint_count,
        "executors_sha256": executors_hash,
        "executors": executors,
    }


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2PerformanceBundle:
    """A sealed legacy transport bundle awaiting a v2 renderer."""

    score_v2_plan_sha256: str
    capability_plan_sha256: str
    runtime_source_sha256: str
    sample_rate: int
    frame_count: int
    occurrence_count: int
    executor_count: int
    event_count: int
    _score_plan: ScoreV2Plan = field(repr=False, compare=False)
    _capability_plan: ScoreV2CapabilityPlan = field(repr=False, compare=False)
    _runtime_sources: ScoreV2RuntimeSourceSnapshot = field(repr=False, compare=False)
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2PerformanceBundle cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2PerformanceBundle must be created by "
            "compile_score_v2_performance_bundle"
        )

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            seal = self._identity_seal
            if type(seal) is not tuple or len(seal) != 20:
                raise ValueError
            (
                score_hash,
                capability_hash,
                runtime_hash,
                source_hash,
                projection_hash,
                profile_hash,
                capability_source_hash,
                roster_hash,
                sample_rate,
                frame_count,
                occurrence_count,
                executor_count,
                event_count,
                endpoint_count,
                executors_hash,
                score_plan,
                capability_plan,
                runtime_sources,
                payload,
                artifact_hash,
            ) = seal
            hashes = (
                score_hash,
                capability_hash,
                runtime_hash,
                source_hash,
                projection_hash,
                profile_hash,
                capability_source_hash,
                roster_hash,
                executors_hash,
                artifact_hash,
            )
            if (
                any(not _is_sha256(value) for value in hashes)
                or type(sample_rate) is not int
                or type(frame_count) is not int
                or type(occurrence_count) is not int
                or type(executor_count) is not int
                or type(event_count) is not int
                or type(endpoint_count) is not int
                or self.score_v2_plan_sha256 != score_hash
                or self.capability_plan_sha256 != capability_hash
                or self.runtime_source_sha256 != runtime_hash
                or self.sample_rate != sample_rate
                or self.frame_count != frame_count
                or self.occurrence_count != occurrence_count
                or self.executor_count != executor_count
                or self.event_count != event_count
                or self._score_plan is not score_plan
                or self._capability_plan is not capability_plan
                or self._runtime_sources is not runtime_sources
                or self._canonical_bytes is not payload
                or self._artifact_sha256 != artifact_hash
                or type(payload) is not bytes
                or hashlib.sha256(payload).hexdigest() != artifact_hash
            ):
                raise ValueError
            document = json.loads(payload)
            root_fields = {
                "kind", "schema_version", "contract", "render_authority",
                "endpoint_dispatch_status", "bindings", "sample_rate",
                "frame_count", "duration_seconds", "occurrence_count",
                "executor_count", "event_count",
                "frame_count_endpoint_event_count", "executors_sha256",
                "executors",
            }
            bindings = document.get("bindings")
            executors = document.get("executors")
            if (
                type(document) is not dict
                or set(document) != root_fields
                or document.get("kind") != SCORE_V2_PERFORMANCE_KIND
                or document.get("schema_version")
                != SCORE_V2_PERFORMANCE_SCHEMA_VERSION
                or document.get("contract") != SCORE_V2_PERFORMANCE_CONTRACT
                or document.get("render_authority") is not False
                or document.get("endpoint_dispatch_status")
                != ENDPOINT_DISPATCH_STATUS
                or type(bindings) is not dict
                or set(bindings)
                != {
                    "score_v2_plan_sha256", "capability_plan_sha256",
                    "runtime_source_sha256", "source_document_sha256",
                    "score_render_projection_sha256",
                    "execution_profile_sha256", "capability_source_sha256",
                    "roster_projection_sha256",
                }
                or bindings["score_v2_plan_sha256"] != score_hash
                or bindings["capability_plan_sha256"] != capability_hash
                or bindings["runtime_source_sha256"] != runtime_hash
                or bindings["source_document_sha256"] != source_hash
                or bindings["score_render_projection_sha256"]
                != projection_hash
                or bindings["execution_profile_sha256"] != profile_hash
                or bindings["capability_source_sha256"]
                != capability_source_hash
                or bindings["roster_projection_sha256"] != roster_hash
                or document.get("sample_rate") != sample_rate
                or document.get("frame_count") != frame_count
                or document.get("duration_seconds") != frame_count / sample_rate
                or document.get("occurrence_count") != occurrence_count
                or document.get("executor_count") != executor_count
                or document.get("event_count") != event_count
                or document.get("frame_count_endpoint_event_count")
                != endpoint_count
                or document.get("executors_sha256") != executors_hash
                or type(executors) is not list
                or len(executors) != executor_count
                or canonical_json_sha256(executors) != executors_hash
                or canonical_json_bytes(document) != payload
            ):
                raise ValueError
            seen_executors: set[str] = set()
            seen_parts: set[str] = set()
            seen_occurrences: set[str] = set()
            checked_events = 0
            checked_endpoints = 0
            for order, executor in enumerate(executors):
                if type(executor) is not dict or set(executor) != {
                    "executor_order", "executor_id", "part_id",
                    "endpoint_dispatch_status", "runtime_binding",
                    "performance_canonical_json_bytes", "performance_sha256",
                    "event_count", "event_sidecar_sha256", "performance",
                    "event_sidecar",
                }:
                    raise ValueError
                executor_id = executor["executor_id"]
                part_id = executor["part_id"]
                runtime_binding = executor["runtime_binding"]
                performance = executor["performance"]
                sidecars = executor["event_sidecar"]
                if (
                    executor["executor_order"] != order
                    or type(executor_id) is not str
                    or not executor_id
                    or executor_id in seen_executors
                    or type(part_id) is not str
                    or not part_id
                    or part_id in seen_parts
                    or executor["endpoint_dispatch_status"]
                    != ENDPOINT_DISPATCH_STATUS
                    or type(runtime_binding) is not dict
                    or set(runtime_binding)
                    != {
                        "manifest_source_sha256", "manifest_raw_sha256",
                        "manifest_canonical_sha256",
                        "capability_projection_sha256",
                        "effective_manifest_canonical_sha256",
                        "effective_manifest_sha256", "runtime_fingerprint_status",
                        "legacy_runtime_fingerprint_sha256",
                        "render_python_closure_sha256",
                        "runtime_dependencies_sha256",
                        "asset_inventory_status",
                        "runtime_asset_graph_sha256",
                    }
                    or runtime_binding["runtime_fingerprint_status"]
                    != RUNTIME_FINGERPRINT_STATUS
                    or any(
                        not _is_sha256(value)
                        for key, value in runtime_binding.items()
                        if key not in {
                            "runtime_fingerprint_status",
                            "asset_inventory_status",
                        }
                    )
                    or type(performance) is not dict
                    or type(sidecars) is not list
                    or executor["event_count"] != len(sidecars)
                    or executor["event_sidecar_sha256"]
                    != canonical_json_sha256(sidecars)
                ):
                    raise ValueError
                performance_bytes = canonical_json_bytes(performance)
                if (
                    executor["performance_canonical_json_bytes"]
                    != len(performance_bytes)
                    or executor["performance_sha256"]
                    != hashlib.sha256(performance_bytes).hexdigest()
                ):
                    raise ValueError
                local_endpoints = _verify_performance(
                    performance,
                    sidecars,
                    sample_rate=sample_rate,
                    frame_count=frame_count,
                )
                for sidecar in sidecars:
                    if sidecar.get("role") == "note_on":
                        seen_occurrences.add(sidecar["occurrence_id"])
                seen_executors.add(executor_id)
                seen_parts.add(part_id)
                checked_events += len(sidecars)
                checked_endpoints += local_endpoints
            if (
                checked_events != event_count
                or checked_endpoints != endpoint_count
                or len(seen_occurrences) != occurrence_count
            ):
                raise ValueError
            _verify_against_retained_inputs(
                document,
                score_plan=score_plan,
                capability_plan=capability_plan,
                runtime_sources=runtime_sources,
            )
            return payload
        except (
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ScoreV2PerformanceError("performance.integrity_mismatch") from exc

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
        value = json.loads(self._trusted_artifact_bytes())
        if type(value) is not dict:
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        return value

    def revalidate_runtime_sources(self) -> None:
        self._trusted_artifact_bytes()
        try:
            self._runtime_sources.revalidate_runtime_sources()
            if (
                self._score_plan.artifact_sha256 != self.score_v2_plan_sha256
                or self._capability_plan.artifact_sha256
                != self.capability_plan_sha256
                or self._runtime_sources.artifact_sha256
                != self.runtime_source_sha256
            ):
                raise ScoreV2PerformanceError(
                    "performance.input_generation_changed"
                )
        except ScoreV2PerformanceError:
            raise
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ScoreV2PerformanceError(
                "performance.runtime_generation_changed"
            ) from exc

    def _local_execution_input_for_executor(
        self,
        executor_id: str,
    ) -> _ScoreV2ExecutorLocalExecutionInput:
        """Return one retained execution input after full cross-validation.

        This package-local bridge deliberately starts with the bundle's public
        runtime revalidation.  A renderer must still repeat that revalidation
        after factory construction and after rendering; the returned value is
        an immutable input snapshot, not a lease over external files.
        """

        self.revalidate_runtime_sources()
        if type(executor_id) is not str or not executor_id:
            raise ScoreV2PerformanceError(
                "performance.executor_not_found"
            )
        document = self.to_dict()
        executors = document.get("executors")
        if type(executors) is not list:
            raise ScoreV2PerformanceError("performance.integrity_mismatch")
        matches = tuple(
            executor
            for executor in executors
            if type(executor) is dict
            and executor.get("executor_id") == executor_id
        )
        if len(matches) != 1:
            raise ScoreV2PerformanceError(
                "performance.executor_not_found"
            )
        executor = matches[0]
        try:
            runtime = self._runtime_sources._execution_input_for_executor(
                executor_id
            )
            runtime_document = executor["runtime_binding"]
            performance = executor["performance"]
            sidecar = executor["event_sidecar"]
            fingerprint = runtime.runtime_binding.fingerprint_copy()
            asset_graph = fingerprint["runtime_asset_graph"]
        except (
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ScoreV2PerformanceError(
                "performance.execution_input_mismatch"
            ) from exc
        expected_runtime = {
            "manifest_source_sha256": (
                runtime.runtime_binding.manifest_source_sha256
            ),
            "manifest_raw_sha256": runtime.runtime_binding.manifest_raw_sha256,
            "manifest_canonical_sha256": (
                runtime.runtime_binding.manifest_canonical_sha256
            ),
            "capability_projection_sha256": (
                runtime.runtime_binding.capability_projection_sha256
            ),
            "effective_manifest_canonical_sha256": (
                runtime.effective_manifest_canonical_sha256
            ),
            "effective_manifest_sha256": runtime.effective_manifest_sha256,
            "runtime_fingerprint_status": RUNTIME_FINGERPRINT_STATUS,
            "legacy_runtime_fingerprint_sha256": (
                runtime.runtime_binding.legacy_runtime_fingerprint_sha256
            ),
            "render_python_closure_sha256": (
                runtime.runtime_binding.render_python_closure_sha256
            ),
            "runtime_dependencies_sha256": (
                runtime.runtime_binding.runtime_dependencies_sha256
            ),
            "asset_inventory_status": (
                runtime.runtime_binding.asset_inventory_status
            ),
            "runtime_asset_graph_sha256": asset_graph.get("sha256"),
        }
        if (
            executor.get("executor_order") != runtime.executor_order
            or executor.get("executor_id") != runtime.executor_id
            or executor.get("part_id") != runtime.part_id
            or runtime_document != expected_runtime
            or type(performance) is not dict
            or type(sidecar) is not list
        ):
            raise ScoreV2PerformanceError(
                "performance.execution_input_mismatch"
            )
        performance_bytes = canonical_json_bytes(performance)
        sidecar_bytes = canonical_json_bytes(sidecar)
        performance_hash = hashlib.sha256(performance_bytes).hexdigest()
        sidecar_hash = hashlib.sha256(sidecar_bytes).hexdigest()
        if (
            executor.get("performance_sha256") != performance_hash
            or executor.get("event_sidecar_sha256") != sidecar_hash
        ):
            raise ScoreV2PerformanceError(
                "performance.execution_input_mismatch"
            )
        return _ScoreV2ExecutorLocalExecutionInput(
            executor_order=runtime.executor_order,
            executor_id=runtime.executor_id,
            part_id=runtime.part_id,
            performance_canonical_bytes=performance_bytes,
            event_sidecar_canonical_bytes=sidecar_bytes,
            performance_sha256=performance_hash,
            event_sidecar_sha256=sidecar_hash,
            runtime=runtime,
        )


def _capture_inputs(
    score_plan: ScoreV2Plan,
    capability_plan: ScoreV2CapabilityPlan,
    runtime_sources: ScoreV2RuntimeSourceSnapshot,
    *,
    limits: ProjectLimits,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes, bytes]:
    if type(score_plan) is not ScoreV2Plan:
        raise TypeError("score_plan must be ScoreV2Plan")
    if type(capability_plan) is not ScoreV2CapabilityPlan:
        raise TypeError("capability_plan must be ScoreV2CapabilityPlan")
    if type(runtime_sources) is not ScoreV2RuntimeSourceSnapshot:
        raise TypeError("runtime_sources must be ScoreV2RuntimeSourceSnapshot")
    try:
        score_bytes = score_plan.canonical_bytes
        capability_bytes = capability_plan.canonical_bytes
        runtime_bytes = runtime_sources.canonical_bytes
        documents = (
            score_plan.to_dict(),
            capability_plan.to_dict(),
            runtime_sources.to_dict(),
        )
    except (
        ScoreV2PlanError,
        ScoreV2CapabilityAdapterError,
        ScoreV2RuntimeSourceError,
        AttributeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        ) from exc
    for payload in (score_bytes, capability_bytes, runtime_bytes):
        if len(payload) > limits.max_plan_json_bytes:
            raise ResourceLimitError(
                "performance.input_document_too_large",
                "Score-v2 performance input exceeds the plan JSON budget",
                actual=len(payload),
                limit=limits.max_plan_json_bytes,
            )
    return (*documents, score_bytes, capability_bytes, runtime_bytes)


def compile_score_v2_performance_bundle(
    score_plan: ScoreV2Plan,
    capability_plan: ScoreV2CapabilityPlan,
    runtime_sources: ScoreV2RuntimeSourceSnapshot,
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2PerformanceBundle:
    """Compile a sealed, bounded legacy-performance transport bundle."""

    active_limits = _active_limits(limits)
    if type(score_plan) is not ScoreV2Plan:
        raise TypeError("score_plan must be ScoreV2Plan")
    if type(capability_plan) is not ScoreV2CapabilityPlan:
        raise TypeError("capability_plan must be ScoreV2CapabilityPlan")
    if type(runtime_sources) is not ScoreV2RuntimeSourceSnapshot:
        raise TypeError("runtime_sources must be ScoreV2RuntimeSourceSnapshot")
    try:
        cheap_occurrence_count = capability_plan.occurrence_count
        cheap_runtime_bindings = runtime_sources.executor_bindings
    except AttributeError as exc:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        ) from exc
    if type(cheap_occurrence_count) is not int or cheap_occurrence_count < 0:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        )
    if cheap_occurrence_count > active_limits.max_notes:
        raise ResourceLimitError(
            "performance.too_many_occurrences",
            "Score-v2 performance occurrence count exceeds the note budget",
            actual=cheap_occurrence_count,
            limit=active_limits.max_notes,
        )
    if type(cheap_runtime_bindings) is not tuple:
        raise ScoreV2PerformanceError(
            "performance.input_artifact_integrity_mismatch"
        )
    if len(cheap_runtime_bindings) > active_limits.max_executors:
        raise ResourceLimitError(
            "performance.too_many_executors",
            "Score-v2 performance executor count exceeds the executor budget",
            actual=len(cheap_runtime_bindings),
            limit=active_limits.max_executors,
        )
    minimum_framing = len(
        canonical_json_bytes(
            {
                "kind": SCORE_V2_PERFORMANCE_KIND,
                "schema_version": SCORE_V2_PERFORMANCE_SCHEMA_VERSION,
                "contract": SCORE_V2_PERFORMANCE_CONTRACT,
                "render_authority": False,
                "endpoint_dispatch_status": ENDPOINT_DISPATCH_STATUS,
                "bindings": {},
                "executors": [],
            }
        )
    )
    if minimum_framing > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "performance.document_too_large",
            "Score-v2 performance bundle exceeds the plan JSON budget",
            actual=minimum_framing,
            limit=active_limits.max_plan_json_bytes,
        )
    minimum_performance_framing = len(
        canonical_json_bytes(
            {
                "sample_rate": 8_000,
                "channels": 2,
                "duration_seconds": 0.0,
                "tail_seconds": 0.0,
                "tuning": {"temperament": "equal", "a4_hz": 440.0},
                "events": [],
            }
        )
    )
    if minimum_performance_framing > active_limits.max_score_json_bytes:
        raise ResourceLimitError(
            "performance.performance_document_too_large",
            "legacy performance JSON exceeds the score JSON budget",
            actual=minimum_performance_framing,
            limit=active_limits.max_score_json_bytes,
        )
    (
        score_document,
        capability_document,
        runtime_document,
        score_bytes,
        capability_bytes,
        runtime_bytes,
    ) = _capture_inputs(
        score_plan,
        capability_plan,
        runtime_sources,
        limits=active_limits,
    )
    score_hash = hashlib.sha256(score_bytes).hexdigest()
    capability_hash = hashlib.sha256(capability_bytes).hexdigest()
    runtime_hash = hashlib.sha256(runtime_bytes).hexdigest()
    score_bindings = score_document.get("bindings")
    capability_bindings = capability_document.get("bindings")
    runtime_bindings = runtime_document.get("bindings")
    score_occurrences = score_document.get("occurrences")
    capability_occurrences = capability_document.get("occurrences")
    runtime_executor_values = runtime_document.get("executors")
    sample_rate = score_document.get("sample_rate")
    if (
        type(score_bindings) is not dict
        or type(capability_bindings) is not dict
        or type(runtime_bindings) is not dict
        or type(score_occurrences) is not list
        or type(capability_occurrences) is not list
        or type(runtime_executor_values) is not list
        or type(sample_rate) is not int
        or not 8_000 <= sample_rate <= 384_000
        or capability_bindings.get("score_v2_plan_sha256") != score_hash
        or capability_document.get("sample_rate") != sample_rate
        or runtime_bindings.get("capability_plan_sha256") != capability_hash
        or runtime_bindings.get("sample_rate") != sample_rate
        or capability_bindings.get("source_document_sha256")
        != score_bindings.get("source_document_sha256")
        or capability_bindings.get("score_render_projection_sha256")
        != score_bindings.get("score_render_projection_sha256")
        or runtime_bindings.get("capability_plan_sha256") != capability_hash
        or runtime_bindings.get("capability_source_sha256")
        != capability_bindings.get("capability_source_sha256")
        or runtime_bindings.get("roster_projection_sha256")
        != capability_bindings.get("roster_projection_sha256")
        or len(score_occurrences) != len(capability_occurrences)
        or capability_document.get("occurrence_count") != len(score_occurrences)
    ):
        raise ScoreV2PerformanceError("performance.binding_mismatch")
    if len(score_occurrences) > active_limits.max_notes:
        raise ResourceLimitError(
            "performance.too_many_occurrences",
            "Score-v2 performance occurrence count exceeds the note budget",
            actual=len(score_occurrences),
            limit=active_limits.max_notes,
        )
    if not runtime_executor_values:
        raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
    score_duration = score_document.get("score_duration")
    duration_sample = (
        score_duration.get("sample")
        if type(score_duration) is dict
        else None
    )
    frame_count = (
        duration_sample.get("resolved_sample")
        if type(duration_sample) is dict
        else None
    )
    if (
        type(frame_count) is not int
        or frame_count < 1
        or duration_sample.get("sample_rate") != sample_rate
        or round((frame_count / sample_rate) * sample_rate) != frame_count
    ):
        raise ScoreV2PerformanceError("performance.duration_not_representable")

    runtime_by_executor: dict[str, dict[str, Any]] = {}
    records_by_executor: dict[
        str, list[tuple[dict[str, Any], dict[str, Any]]]
    ] = {}
    for order, binding in enumerate(runtime_executor_values):
        if type(binding) is not dict:
            raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
        executor_id = binding.get("executor_id")
        if (
            binding.get("executor_order") != order
            or type(executor_id) is not str
            or not executor_id
            or executor_id in runtime_by_executor
            or binding.get("sample_rate") != sample_rate
            or binding.get("capability_plan_sha256") != capability_hash
            or binding.get("capability_source_sha256")
            != capability_bindings.get("capability_source_sha256")
            or binding.get("roster_projection_sha256")
            != capability_bindings.get("roster_projection_sha256")
        ):
            raise ScoreV2PerformanceError("performance.runtime_binding_mismatch")
        _runtime_binding_document(binding)
        runtime_by_executor[executor_id] = binding
        records_by_executor[executor_id] = []

    seen_occurrences: set[str] = set()
    for score_occurrence, capability_occurrence in zip(
        score_occurrences, capability_occurrences, strict=True
    ):
        if (
            type(score_occurrence) is not dict
            or type(capability_occurrence) is not dict
            or set(capability_occurrence) != _CAPABILITY_OCCURRENCE_FIELDS
        ):
            raise ScoreV2PerformanceError("performance.occurrence_binding_mismatch")
        occurrence_id = score_occurrence.get("occurrence_id")
        executor_id = capability_occurrence.get("executor_id")
        binding = runtime_by_executor.get(executor_id)
        capability_binding = capability_occurrence.get("capability_binding")
        score_start = score_occurrence.get("start")
        score_end = score_occurrence.get("end")
        if type(score_start) is not dict or type(score_end) is not dict:
            raise ScoreV2PerformanceError("performance.occurrence_binding_mismatch")
        start_exact = _document_fraction(score_start.get("requested_seconds"))
        end_exact = _document_fraction(score_end.get("requested_seconds"))
        start_sample = score_start.get("resolved_sample")
        end_sample = score_end.get("resolved_sample")
        if (
            type(occurrence_id) is not str
            or not occurrence_id
            or occurrence_id in seen_occurrences
            or type(executor_id) is not str
            or binding is None
            or type(capability_binding) is not dict
            or capability_occurrence.get("occurrence_id") != occurrence_id
            or capability_occurrence.get("part_id") != score_occurrence.get("part_id")
            or binding.get("part_id") != score_occurrence.get("part_id")
            or capability_occurrence.get("source_event_ids")
            != score_occurrence.get("source_event_ids")
            or capability_occurrence.get("source_tie_ids")
            != score_occurrence.get("source_tie_ids")
            or capability_occurrence.get("start_sample") != start_sample
            or capability_occurrence.get("end_sample") != end_sample
            or not Fraction(0) <= start_exact < end_exact
            or type(start_sample) is not int
            or type(end_sample) is not int
            or not 0 <= start_sample <= frame_count
            or not 0 <= end_sample <= frame_count
            or capability_binding.get("manifest_source_sha256")
            != binding.get("manifest_source_sha256")
            or capability_binding.get("capability_projection_sha256")
            != binding.get("capability_projection_sha256")
            or capability_binding.get("effective_manifest_sha256")
            != binding.get("effective_manifest_sha256")
            or capability_binding.get("runtime_fingerprint_status") != "not_captured"
        ):
            raise ScoreV2PerformanceError("performance.occurrence_binding_mismatch")
        seen_occurrences.add(occurrence_id)
        records_by_executor[executor_id].append(
            (score_occurrence, capability_occurrence)
        )

    projected_events = sum(
        2 + (_resolved_articulation(capability["articulation"]) is not None)
        for values in records_by_executor.values()
        for _score, capability in values
    )
    event_limit = performance_event_limit(active_limits)
    if projected_events > event_limit:
        raise ResourceLimitError(
            "performance.too_many_events",
            "Score-v2 performance event count exceeds the event budget",
            actual=projected_events,
            limit=event_limit,
        )

    exact_performance_framing = len(
        canonical_json_bytes(
            {
                "sample_rate": sample_rate,
                "channels": 2,
                "duration_seconds": frame_count / sample_rate,
                "tail_seconds": 0.0,
                "tuning": {"temperament": "equal", "a4_hz": 440.0},
                "events": [],
            }
        )
    )
    if exact_performance_framing > active_limits.max_score_json_bytes:
        raise ResourceLimitError(
            "performance.performance_document_too_large",
            "legacy performance JSON exceeds the score JSON budget",
            actual=exact_performance_framing,
            limit=active_limits.max_score_json_bytes,
        )
    try:
        runtime_sources.revalidate_runtime_sources()
    except (
        ScoreV2RuntimeSourceError,
        AttributeError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ScoreV2PerformanceError(
            "performance.runtime_generation_changed"
        ) from exc

    executor_documents: list[dict[str, object]] = []
    total_events = 0
    endpoint_events = 0
    approximate_bundle_bytes = len(canonical_json_bytes({
        "bindings": {
            "score_v2_plan_sha256": score_hash,
            "capability_plan_sha256": capability_hash,
            "runtime_source_sha256": runtime_hash,
        },
        "executors": [],
    }))
    for binding in runtime_executor_values:
        executor_id = binding["executor_id"]
        pending: list[_PendingEvent] = []
        for note_index, (score_occurrence, capability_occurrence) in enumerate(
            records_by_executor[executor_id], start=1
        ):
            pitch_name, pitch_value = _resolved_pitch(
                capability_occurrence["pitch"]
            )
            velocity = _resolved_velocity(capability_occurrence["velocity"])
            articulation = _resolved_articulation(
                capability_occurrence["articulation"]
            )
            occurrence_id = score_occurrence["occurrence_id"]
            score_start = score_occurrence["start"]
            score_end = score_occurrence["end"]
            start_sample = score_start["resolved_sample"]
            end_sample = score_end["resolved_sample"]
            start_exact = _document_fraction(score_start["requested_seconds"])
            end_exact = _document_fraction(score_end["requested_seconds"])
            source_order = score_occurrence["source_order"]
            if type(source_order) is not int:
                raise ScoreV2PerformanceError(
                    "performance.occurrence_binding_mismatch"
                )
            start_time = _sample_time(start_sample, sample_rate)
            end_time = _sample_time(end_sample, sample_rate)
            if articulation is not None:
                event = {
                    "time": start_time,
                    "type": "articulation",
                    "name": articulation,
                }
                approximate_bundle_bytes += len(canonical_json_bytes(event)) + 128
                if approximate_bundle_bytes > active_limits.max_plan_json_bytes:
                    raise ResourceLimitError(
                        "performance.document_too_large",
                        "Score-v2 performance bundle exceeds the plan JSON budget",
                        actual=approximate_bundle_bytes,
                        limit=active_limits.max_plan_json_bytes,
                    )
                pending.append(
                    _PendingEvent(
                        start_sample, start_exact, 1, source_order,
                        occurrence_id, 0, "articulation", note_index, event,
                    )
                )
            note_on = {
                "time": start_time,
                "type": "note_on",
                "note_id": note_index,
                pitch_name: pitch_value,
                "velocity": velocity,
                "source_event_id": occurrence_id,
            }
            approximate_bundle_bytes += len(canonical_json_bytes(note_on)) + 128
            if approximate_bundle_bytes > active_limits.max_plan_json_bytes:
                raise ResourceLimitError(
                    "performance.document_too_large",
                    "Score-v2 performance bundle exceeds the plan JSON budget",
                    actual=approximate_bundle_bytes,
                    limit=active_limits.max_plan_json_bytes,
                )
            pending.append(
                _PendingEvent(
                    start_sample, start_exact, 1, source_order,
                    occurrence_id, 1, "note_on", note_index, note_on,
                )
            )
            note_off = {
                "time": end_time,
                "type": "note_off",
                "note_id": note_index,
                "source_event_id": occurrence_id,
            }
            approximate_bundle_bytes += len(canonical_json_bytes(note_off)) + 128
            if approximate_bundle_bytes > active_limits.max_plan_json_bytes:
                raise ResourceLimitError(
                    "performance.document_too_large",
                    "Score-v2 performance bundle exceeds the plan JSON budget",
                    actual=approximate_bundle_bytes,
                    limit=active_limits.max_plan_json_bytes,
                )
            pending.append(
                _PendingEvent(
                    end_sample, end_exact, 0, source_order,
                    occurrence_id, 0, "note_off", note_index, note_off,
                )
            )
        pending.sort(
            key=lambda item: (
                item.sample,
                item.exact_time,
                item.phase,
                item.source_order,
                item.occurrence_id,
                item.within_occurrence,
            )
        )
        events: list[dict[str, object]] = []
        sidecars: list[dict[str, object]] = []
        performance_base = {
            "sample_rate": sample_rate,
            "channels": 2,
            "duration_seconds": frame_count / sample_rate,
            "tail_seconds": 0.0,
            "tuning": {"temperament": "equal", "a4_hz": 440.0},
            "events": [],
        }
        performance_bytes_used = len(canonical_json_bytes(performance_base))
        for item in pending:
            sidecar = ScoreV2PerformanceEventSidecar(
                sequence=len(events),
                occurrence_id=item.occurrence_id,
                role=item.role,
                note_id=item.note_id,
                expected_sample=item.sample,
            ).to_dict()
            prospective = (
                performance_bytes_used
                + len(canonical_json_bytes(item.document))
                + (1 if events else 0)
            )
            if prospective > active_limits.max_score_json_bytes:
                raise ResourceLimitError(
                    "performance.performance_document_too_large",
                    "legacy performance JSON exceeds the score JSON budget",
                    actual=prospective,
                    limit=active_limits.max_score_json_bytes,
                )
            events.append(item.document)
            sidecars.append(sidecar)
            performance_bytes_used = prospective
        performance_document = {**performance_base, "events": events}
        detached, performance_payload = _bounded_document(
            performance_document,
            maximum_bytes=active_limits.max_score_json_bytes,
            maximum_items=max(event_limit, 1),
            resource_code="performance.performance_document_too_large",
        )
        if len(performance_payload) != performance_bytes_used:
            raise ScoreV2PerformanceError(
                "performance.performance_roundtrip_mismatch"
            )
        local_endpoint_count = _verify_performance(
            detached,
            sidecars,
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
        executor_documents.append(
            {
                "executor_order": binding["executor_order"],
                "executor_id": executor_id,
                "part_id": binding["part_id"],
                "endpoint_dispatch_status": ENDPOINT_DISPATCH_STATUS,
                "runtime_binding": _runtime_binding_document(binding),
                "performance_canonical_json_bytes": len(performance_payload),
                "performance_sha256": hashlib.sha256(
                    performance_payload
                ).hexdigest(),
                "event_count": len(events),
                "event_sidecar_sha256": canonical_json_sha256(sidecars),
                "performance": detached,
                "event_sidecar": sidecars,
            }
        )
        total_events += len(events)
        endpoint_events += local_endpoint_count

    if total_events != projected_events:
        raise ScoreV2PerformanceError("performance.event_count_mismatch")
    source_hash = capability_bindings["source_document_sha256"]
    projection_hash = capability_bindings["score_render_projection_sha256"]
    profile_hash = capability_bindings["execution_profile_sha256"]
    capability_source_hash = capability_bindings["capability_source_sha256"]
    roster_hash = capability_bindings["roster_projection_sha256"]
    if any(
        not _is_sha256(value)
        for value in (
            source_hash,
            projection_hash,
            profile_hash,
            capability_source_hash,
            roster_hash,
        )
    ):
        raise ScoreV2PerformanceError("performance.binding_mismatch")
    executors_hash = canonical_json_sha256(executor_documents)
    document = _artifact_document(
        score_plan_hash=score_hash,
        capability_plan_hash=capability_hash,
        runtime_source_hash=runtime_hash,
        source_hash=source_hash,
        projection_hash=projection_hash,
        profile_hash=profile_hash,
        capability_source_hash=capability_source_hash,
        roster_hash=roster_hash,
        sample_rate=sample_rate,
        frame_count=frame_count,
        occurrence_count=len(score_occurrences),
        event_count=total_events,
        endpoint_count=endpoint_events,
        executors_hash=executors_hash,
        executors=executor_documents,
    )
    _detached_bundle, payload = _bounded_document(
        document,
        maximum_bytes=active_limits.max_plan_json_bytes,
        maximum_items=max(event_limit, active_limits.max_executors, 1),
        resource_code="performance.document_too_large",
    )

    try:
        runtime_sources.revalidate_runtime_sources()
        if (
            score_plan.canonical_bytes != score_bytes
            or capability_plan.canonical_bytes != capability_bytes
            or runtime_sources.canonical_bytes != runtime_bytes
        ):
            raise ScoreV2PerformanceError(
                "performance.input_generation_changed"
            )
    except ScoreV2PerformanceError:
        raise
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2PerformanceError(
            "performance.runtime_generation_changed"
        ) from exc

    artifact_hash = hashlib.sha256(payload).hexdigest()
    result = object.__new__(ScoreV2PerformanceBundle)
    for name, value in (
        ("score_v2_plan_sha256", score_hash),
        ("capability_plan_sha256", capability_hash),
        ("runtime_source_sha256", runtime_hash),
        ("sample_rate", sample_rate),
        ("frame_count", frame_count),
        ("occurrence_count", len(score_occurrences)),
        ("executor_count", len(executor_documents)),
        ("event_count", total_events),
        ("_score_plan", score_plan),
        ("_capability_plan", capability_plan),
        ("_runtime_sources", runtime_sources),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", artifact_hash),
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(
        result,
        "_identity_seal",
        (
            score_hash,
            capability_hash,
            runtime_hash,
            source_hash,
            projection_hash,
            profile_hash,
            capability_source_hash,
            roster_hash,
            sample_rate,
            frame_count,
            len(score_occurrences),
            len(executor_documents),
            total_events,
            endpoint_events,
            executors_hash,
            score_plan,
            capability_plan,
            runtime_sources,
            payload,
            artifact_hash,
        ),
    )
    result._trusted_artifact_bytes()
    return result


__all__ = [
    "ENDPOINT_DISPATCH_STATUS",
    "SCORE_V2_PERFORMANCE_CONTRACT",
    "SCORE_V2_PERFORMANCE_KIND",
    "SCORE_V2_PERFORMANCE_SCHEMA_VERSION",
    "ScoreV2PerformanceBundle",
    "ScoreV2PerformanceError",
    "ScoreV2PerformanceEventSidecar",
    "compile_score_v2_performance_bundle",
]
