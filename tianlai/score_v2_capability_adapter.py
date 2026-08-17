"""Bind Score-v2 intent to audited executor capability and creator consent.

This is deliberately a *capability plan*, not a renderer plan.  It proves that
one sealed score/plan/profile/roster/manifest generation can resolve the first
linear Score-v2 subset without silently exceeding the creator's numeric or
semantic permissions.  Runtime sample fingerprints and renderer-generation
revalidation are intentionally still absent, so the contract must not be used
as render authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
import math
from typing import Any

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .capability import InstrumentCapability
from .resource_limits import ProjectLimits, ResourceLimitError
from .roster import Executor, Roster
from .score_source import ScoreSourceSnapshot, snapshot_score_bytes
from .score_v2 import (
    SCORE_V2_IDENTITY_CONTRACT,
    SCORE_V2_TIME_CONTRACT,
    ScoreV2Document,
    score_render_projection_sha256,
)
from .score_v2_capability_source import (
    ScoreV2CapabilitySourceError,
    ScoreV2CapabilityProjection,
    ScoreV2CapabilitySourceSnapshot,
    ScoreV2ExecutorCapabilityBinding,
)
from .score_v2_execution_profile import (
    ScoreV2ExecutionProfile,
    parse_score_v2_execution_profile,
)
from .score_v2_plan import ScoreV2Occurrence, ScoreV2Plan


SCORE_V2_CAPABILITY_PLAN_KIND = "tianlai.score_v2_capability_plan"
SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION = 1
SCORE_V2_CAPABILITY_PLAN_CONTRACT = (
    "score-v2-capability-adapter-v1-not-render-authority"
)


class ScoreV2CapabilityAdapterError(ValueError):
    """A stable, non-reflective capability-adapter failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2CapabilityAdapter.{code.replace('.', '_')}"
        super().__init__(code)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
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


def _fraction_document(value: Fraction) -> dict[str, str]:
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


def _exact_float(value: Fraction) -> tuple[float, bool]:
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.numeric_transport_overflow"
        ) from exc
    if not math.isfinite(result):
        raise ScoreV2CapabilityAdapterError(
            "adapter.numeric_transport_overflow"
        )
    return result, Fraction.from_float(result) == value


def _capture_source(
    snapshot: ScoreSourceSnapshot,
    *,
    limits: ProjectLimits,
) -> ScoreSourceSnapshot:
    if type(snapshot) is not ScoreSourceSnapshot:
        raise TypeError("source must be a ScoreSourceSnapshot")
    try:
        payload = snapshot.canonical_bytes
        source_hash = snapshot.document_sha256
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.source_snapshot_mismatch"
        ) from exc
    if type(payload) is not bytes or type(source_hash) is not str:
        raise ScoreV2CapabilityAdapterError(
            "adapter.source_snapshot_mismatch"
        )
    if len(payload) > limits.max_score_json_bytes:
        raise ResourceLimitError(
            "score.document_too_large",
            "Score-v2 source exceeds the configured JSON byte budget",
            actual=len(payload),
            limit=limits.max_score_json_bytes,
        )
    if hashlib.sha256(payload).hexdigest() != source_hash:
        raise ScoreV2CapabilityAdapterError(
            "adapter.source_snapshot_mismatch"
        )
    try:
        trusted = snapshot_score_bytes(payload, limits=limits)
    except ResourceLimitError:
        raise
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.source_snapshot_mismatch"
        ) from exc
    if (
        trusted.document_sha256 != source_hash
        or trusted.identity_contract != SCORE_V2_IDENTITY_CONTRACT
        or trusted.time_contract != SCORE_V2_TIME_CONTRACT
        or type(trusted.score) is not ScoreV2Document
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.unsupported_score_contract"
        )
    return trusted


def _capture_profile(
    profile: ScoreV2ExecutionProfile,
) -> ScoreV2ExecutionProfile:
    """Detach one consent generation before any capability callbacks run."""

    if type(profile) is not ScoreV2ExecutionProfile:
        raise TypeError("profile must be a ScoreV2ExecutionProfile")
    try:
        payload = profile.canonical_bytes
        profile_hash = profile.artifact_sha256
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        ) from exc
    if (
        type(payload) is not bytes
        or not _is_sha256(profile_hash)
        or hashlib.sha256(payload).hexdigest() != profile_hash
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        )
    try:
        trusted = parse_score_v2_execution_profile(
            payload,
            max_document_bytes=max(1, len(payload)),
        )
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        ) from exc
    if trusted.artifact_sha256 != profile_hash:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        )
    return trusted


def _profile_dynamic_document(
    profile: ScoreV2ExecutionProfile,
) -> dict[str, object]:
    return {
        "kind": "tianlai.score_v2_dynamic_profile",
        "schema_version": 1,
        "velocities": {
            level.mark: {
                "numerator": level.value.numerator,
                "denominator": level.value.denominator,
            }
            for level in profile.dynamic_profile
        },
    }


def _validate_plan_profile_bindings(
    *,
    source: ScoreSourceSnapshot,
    score: ScoreV2Document,
    plan: ScoreV2Plan,
    profile: ScoreV2ExecutionProfile,
) -> tuple[dict[str, object], dict[str, object], bytes, str]:
    if type(plan) is not ScoreV2Plan:
        raise TypeError("plan must be a ScoreV2Plan")
    if type(profile) is not ScoreV2ExecutionProfile:
        raise TypeError("profile must be a ScoreV2ExecutionProfile")
    try:
        plan_payload = plan.canonical_bytes
        plan_hash = hashlib.sha256(plan_payload).hexdigest()
        plan_document = json.loads(plan_payload)
        profile_payload = profile.canonical_bytes
        profile_hash = hashlib.sha256(profile_payload).hexdigest()
        profile_document = json.loads(profile_payload)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        ) from exc
    if (
        type(plan_document) is not dict
        or type(profile_document) is not dict
        or not _is_sha256(plan_hash)
        or not _is_sha256(profile_hash)
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        )
    dynamic_document = _profile_dynamic_document(profile)
    dynamic_hash = canonical_json_sha256(dynamic_document)
    bindings = plan_document.get("bindings")
    if type(bindings) is not dict or (
        bindings.get("source_document_sha256") != source.document_sha256
        or bindings.get("score_render_projection_sha256")
        != score_render_projection_sha256(score)
    ):
        raise ScoreV2CapabilityAdapterError("adapter.plan_binding_mismatch")
    if (
        plan_document.get("sample_time_policy")
        != profile.sample_time_policy
        or bindings.get("dynamic_profile_sha256") != dynamic_hash
        or plan_document.get("dynamic_profile") != dynamic_document
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.execution_profile_binding_mismatch"
        )
    return plan_document, profile_document, plan_payload, plan_hash


def _validate_roster_generation(
    roster: Roster,
    sources: ScoreV2CapabilitySourceSnapshot,
    *,
    limits: ProjectLimits,
) -> tuple[
    dict[str, object],
    dict[str, ScoreV2ExecutorCapabilityBinding],
]:
    if type(roster) is not Roster:
        raise TypeError("roster must be a Roster")
    if type(sources) is not ScoreV2CapabilitySourceSnapshot:
        raise TypeError(
            "capability_sources must be a ScoreV2CapabilitySourceSnapshot"
        )
    try:
        executors = roster.executors
    except AttributeError as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.roster_binding_mismatch"
        ) from exc
    if type(executors) is not tuple:
        raise ScoreV2CapabilityAdapterError(
            "adapter.roster_binding_mismatch"
        )
    if len(executors) > limits.max_executors:
        raise ResourceLimitError(
            "adapter.too_many_executors",
            "Score-v2 executor count exceeds the configured budget",
            actual=len(executors),
            limit=limits.max_executors,
        )
    try:
        sources.artifact_sha256
        sources.to_dict()
        sources.revalidate_sources()
        roster_document = Roster.to_dict(roster)
        roster_hash = canonical_json_sha256(roster_document)
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.capability_source_mismatch"
        ) from exc
    if roster_hash != sources.roster_projection_sha256:
        raise ScoreV2CapabilityAdapterError("adapter.roster_binding_mismatch")
    bindings: dict[str, ScoreV2ExecutorCapabilityBinding] = {}
    for binding in sources.executor_bindings:
        if binding.executor_id in bindings:
            raise ScoreV2CapabilityAdapterError(
                "adapter.duplicate_executor_binding"
            )
        bindings[binding.executor_id] = binding
    if len(bindings) != len(executors):
        raise ScoreV2CapabilityAdapterError("adapter.roster_binding_mismatch")
    return roster_document, bindings


def _validate_live_capability_projection(
    capability: InstrumentCapability,
    *,
    binding: ScoreV2ExecutorCapabilityBinding,
    projection: ScoreV2CapabilityProjection | None,
) -> None:
    if type(capability) is not InstrumentCapability or projection is None:
        raise ScoreV2CapabilityAdapterError(
            "adapter.capability_projection_mismatch"
        )
    maximum = max(1, len(projection.canonical_bytes))
    try:
        InstrumentCapability.__post_init__(capability)
        capability_document = InstrumentCapability.to_dict(capability)
        capability_bytes = bounded_canonical_json_bytes(
            capability_document,
            limits=AuthoringJsonLimits(max_document_bytes=maximum),
            require_object=True,
            require_js_safe_integers=True,
        )
    except (AuthoringJsonError, AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.capability_projection_mismatch"
        ) from exc
    if (
        not _is_sha256(binding.capability_projection_sha256)
        or not _is_sha256(projection.canonical_sha256)
        or projection.canonical_sha256
        != binding.capability_projection_sha256
        or hashlib.sha256(capability_bytes).hexdigest()
        != binding.capability_projection_sha256
        or capability_bytes != projection.canonical_bytes
        or projection.instrument_relative_path != capability.relative_path
        or projection.manifest_source_sha256
        != binding.manifest_source_sha256
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.capability_projection_mismatch"
        )


def _validate_executor_subset(
    score: ScoreV2Document,
    roster: Roster,
    bindings: dict[str, ScoreV2ExecutorCapabilityBinding],
    sources: ScoreV2CapabilitySourceSnapshot,
) -> dict[str, Executor]:
    if roster.dropped_parts:
        raise ScoreV2CapabilityAdapterError(
            "adapter.dropped_parts_unsupported"
        )
    score_parts = {part.part_id for part in score.parts}
    by_part: dict[str, list[Executor]] = {}
    projections = {
        item.canonical_sha256: item
        for item in sources.capability_projections
    }
    for order, executor in enumerate(roster.executors):
        if type(executor) is not Executor:
            raise ScoreV2CapabilityAdapterError("adapter.invalid_executor")
        by_part.setdefault(executor.part_id, []).append(executor)
        binding = bindings.get(executor.executor_id)
        if (
            binding is None
            or binding.executor_order != order
            or binding.part_id != executor.part_id
            or binding.instrument_relative_path
            != executor.capability.relative_path
        ):
            raise ScoreV2CapabilityAdapterError(
                "adapter.executor_binding_mismatch"
            )
        if binding.custom_implementation_blocked:
            raise ScoreV2CapabilityAdapterError(
                "adapter.custom_implementation_blocked"
            )
        if (
            binding.runtime_fingerprint_status != "not_captured"
            or binding.runtime_fingerprint_sha256 is not None
            or binding.execution_eligibility
            != "pending_runtime_fingerprint"
        ):
            raise ScoreV2CapabilityAdapterError(
                "adapter.runtime_fingerprint_contract_mismatch"
            )
        if (
            executor.kit_pitch is not None
            or executor.transpose != 0
            or executor.duration_scale != 1.0
            or executor.dynamic_compression != 0.0
            or executor.articulation_auto is not False
            or executor.gain_automation
            or executor.overrides
        ):
            raise ScoreV2CapabilityAdapterError(
                "adapter.executor_transform_unsupported"
            )
        projection = projections.get(binding.capability_projection_sha256)
        _validate_live_capability_projection(
            executor.capability,
            binding=binding,
            projection=projection,
        )
    if set(by_part) != score_parts or any(
        len(executors) != 1 for executors in by_part.values()
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.part_assignment_not_one_to_one"
        )
    return {part_id: executors[0] for part_id, executors in by_part.items()}


def _resolve_tuning(
    score: ScoreV2Document,
    profile: ScoreV2ExecutionProfile,
) -> tuple[dict[str, object], float, bool]:
    tuning = score.tuning
    reference_midi = tuning.reference_midi_note.as_fraction()
    reference_hz = tuning.reference_frequency_hz.as_fraction()
    exponent = (Fraction(69) - reference_midi) / 12
    # Reject outside binary64's physical transport range before constructing
    # ``2 ** power``.  Score-v2 Rational numerators are JS-safe but an authored
    # reference MIDI value can still make ``power`` enormous; materializing
    # that bigint would consume unbounded CPU/memory even though the eventual
    # renderer float cannot represent the result.
    try:
        log2_a4 = (
            math.log2(reference_hz.numerator)
            - math.log2(reference_hz.denominator)
            + float(exponent)
        )
    except (OverflowError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.tuning_transport_overflow"
        ) from exc
    if not math.isfinite(log2_a4) or not -1074.0 <= log2_a4 < 1024.0:
        raise ScoreV2CapabilityAdapterError(
            "adapter.tuning_transport_overflow"
        )
    exact_fraction: Fraction | None = None
    if exponent.denominator == 1:
        power = exponent.numerator
        exact_fraction = (
            reference_hz * (2**power)
            if power >= 0
            else reference_hz / (2 ** (-power))
        )
        a4_hz, exact = _exact_float(exact_fraction)
    else:
        try:
            a4_hz = float(reference_hz) * (2.0 ** float(exponent))
        except (OverflowError, ValueError) as exc:
            raise ScoreV2CapabilityAdapterError(
                "adapter.tuning_transport_overflow"
            ) from exc
        exact = False
    if not math.isfinite(a4_hz) or a4_hz <= 0.0:
        raise ScoreV2CapabilityAdapterError(
            "adapter.tuning_transport_overflow"
        )
    if not exact and profile.tuning.value_policy != "adapt":
        raise ScoreV2CapabilityAdapterError(
            "adapter.tuning_adaptation_not_authorized"
        )
    # Score v2 currently accepts only 12-division equal temperament, so no
    # semantic substitution is required at this boundary.
    evidence: dict[str, object] = {
        "requested": {
            "tuning_id": tuning.tuning_id,
            "system": tuning.system,
            "divisions_per_octave": tuning.divisions_per_octave,
            "reference_midi_note": _fraction_document(reference_midi),
            "reference_frequency_hz": _fraction_document(reference_hz),
        },
        "resolved_legacy_equal_temperament": {"a4_hz": a4_hz},
        "value_policy": profile.tuning.value_policy,
        "semantic_policy": profile.tuning.semantic_policy,
        "numeric_fidelity": "exact" if exact else "adapted",
        "semantic_fidelity": "native",
        "adapted": not exact,
        "source": "score-v2 equal-temperament reference -> legacy a4_hz",
    }
    return evidence, a4_hz, exact


def _resolve_articulation(
    occurrence: ScoreV2Occurrence,
    executor: Executor,
    profile: ScoreV2ExecutionProfile,
) -> tuple[dict[str, object], str | None]:
    requested = occurrence.articulation
    capability = executor.capability
    if requested is None:
        if capability.default_articulation is not None:
            raise ScoreV2CapabilityAdapterError(
                "adapter.articulation_unresolved"
            )
        return {"status": "not_applicable", "requested_value": None}, None
    mapped = requested
    mapping_used = False
    for source, target in executor.articulation_map:
        if source == requested:
            mapped = target
            mapping_used = target != requested
            break
    if mapping_used and profile.articulation.mapping_policy != (
        "allow_roster_mapping"
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.articulation_mapping_not_authorized"
        )
    try:
        if mapping_used:
            resolution = capability.adapt_articulation_execution(
                requested,
                mapped,
                mapping_source="roster.articulation_map",
                semantic_policy=profile.articulation.semantic_policy,
            )
        else:
            resolution = capability.require_articulation_execution(
                requested,
                semantic_policy=profile.articulation.semantic_policy,
            )
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.articulation_not_executable"
        ) from exc
    if resolution.semantic_fidelity == "ignored" or resolution.fidelity == (
        "ignored"
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.articulation_not_executable"
        )
    return resolution.to_dict(), resolution.resolved_value


def _resolve_range(
    value: float,
    *,
    articulation: str | None,
    capability: InstrumentCapability,
    profile: ScoreV2ExecutionProfile,
) -> dict[str, object]:
    if type(value) is not float or not math.isfinite(value):
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_transport_overflow"
        )
    if profile.pitch.range_policy == "verified_high_quality":
        evaluation = capability.evaluate_range_profile(
            value,
            articulation,
            overrides=None,
            mode="strict_hq",
        )
        if not evaluation.verified or not evaluation.high_quality_covered:
            raise ScoreV2CapabilityAdapterError(
                "adapter.high_quality_range_unverified"
            )
        return {
            "policy": profile.pitch.range_policy,
            "coordinate": "midi_note_at_a4_440",
            "requested_value": value,
            "status": evaluation.status,
            "evaluation": evaluation.to_dict(),
        }
    ranges = capability.ranges_for(articulation)
    if not ranges:
        raise ScoreV2CapabilityAdapterError(
            "adapter.hard_range_undeclared"
        )
    covered = any(float(low) <= value <= float(high) for low, high in ranges)
    if not covered:
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_outside_declared_range"
        )
    return {
        "policy": profile.pitch.range_policy,
        "coordinate": "midi_note_at_a4_440",
        "requested_value": value,
        "status": "inside_declared_hard_range",
        "ranges": [[low, high] for low, high in ranges],
    }


def _resolve_pitch(
    value: Fraction,
    *,
    a4_hz: float,
    capability: InstrumentCapability,
    profile: ScoreV2ExecutionProfile,
) -> tuple[dict[str, object], float]:
    note_pitch = capability.note_pitch
    if (
        note_pitch is None
        or note_pitch.mode in {"fixed", "selector"}
        or note_pitch.semantic_fidelity == "ignored"
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_semantics_unavailable"
        )
    value_float, rational_exact = _exact_float(value)
    try:
        event_pitch_hz = a4_hz * (
            2.0 ** ((value_float - 69.0) / 12.0)
        )
        shift = 12.0 * math.log2(a4_hz / 440.0)
        requested_backend = value_float + shift
    except (ValueError, OverflowError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_transport_overflow"
        ) from exc
    if (
        not math.isfinite(event_pitch_hz)
        or event_pitch_hz <= 0.0
        or not math.isfinite(requested_backend)
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_transport_overflow"
        )
    # The common A4=440 path has an exact score-coordinate transport whenever
    # the authored Rational itself is binary64-exact.  Other tuning shifts use
    # log2 and therefore require explicit adaptation in this first contract.
    transport_exact = rational_exact and a4_hz == 440.0 and shift == 0.0
    if not transport_exact and profile.pitch.value_policy != "adapt":
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_adaptation_not_authorized"
        )
    try:
        resolution = (
            capability.require_note_pitch(
                requested_backend,
                semantic_policy=profile.pitch.semantic_policy,
            )
            if profile.pitch.value_policy == "exact"
            else capability.adapt_note_pitch(
                requested_backend,
                semantic_policy=profile.pitch.semantic_policy,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_not_representable"
        ) from exc
    if resolution.semantic_fidelity == "ignored" or resolution.fidelity == (
        "ignored"
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.pitch_semantics_unavailable"
        )
    return (
        {
            "requested_score_midi_note": _fraction_document(value),
            "requested_event_pitch_hz": event_pitch_hz,
            "requested_backend_midi_note_at_a4_440": requested_backend,
            "rational_to_binary64_fidelity": (
                "exact" if transport_exact else "adapted"
            ),
            "value_policy": profile.pitch.value_policy,
            "semantic_policy": profile.pitch.semantic_policy,
            "capability_resolution": resolution.to_dict(),
            "adapted": (not transport_exact) or resolution.adapted,
        },
        requested_backend,
    )


def _resolve_velocity(
    value: Fraction,
    *,
    capability: InstrumentCapability,
    profile: ScoreV2ExecutionProfile,
) -> dict[str, object]:
    note_velocity = capability.note_velocity
    if note_velocity is None or note_velocity.semantic_fidelity == "ignored":
        raise ScoreV2CapabilityAdapterError(
            "adapter.velocity_semantics_unavailable"
        )
    value_float, transport_exact = _exact_float(value)
    if not transport_exact and profile.note_velocity.value_policy != "adapt":
        raise ScoreV2CapabilityAdapterError(
            "adapter.velocity_adaptation_not_authorized"
        )
    try:
        resolution = (
            capability.require_note_velocity(
                value_float,
                semantic_policy=profile.note_velocity.semantic_policy,
            )
            if profile.note_velocity.value_policy == "exact"
            else capability.adapt_note_velocity(
                value_float,
                semantic_policy=profile.note_velocity.semantic_policy,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.velocity_not_representable"
        ) from exc
    if resolution.semantic_fidelity == "ignored":
        raise ScoreV2CapabilityAdapterError(
            "adapter.velocity_semantics_unavailable"
        )
    return {
        "requested_value": _fraction_document(value),
        "rational_to_binary64_fidelity": (
            "exact" if transport_exact else "adapted"
        ),
        "value_policy": profile.note_velocity.value_policy,
        "semantic_policy": profile.note_velocity.semantic_policy,
        "capability_resolution": resolution.to_dict(),
        "adapted": (not transport_exact) or resolution.adapted,
    }


def _occurrence_document(
    occurrence: ScoreV2Occurrence,
    *,
    executor: Executor,
    binding: ScoreV2ExecutorCapabilityBinding,
    a4_hz: float,
    profile: ScoreV2ExecutionProfile,
) -> dict[str, object]:
    articulation, final_articulation = _resolve_articulation(
        occurrence,
        executor,
        profile,
    )
    score_pitch = occurrence.sounding_midi_note.as_fraction()
    pitch, requested_backend_pitch = _resolve_pitch(
        score_pitch,
        a4_hz=a4_hz,
        capability=executor.capability,
        profile=profile,
    )
    range_evidence = _resolve_range(
        requested_backend_pitch,
        articulation=final_articulation,
        capability=executor.capability,
        profile=profile,
    )
    velocity = _resolve_velocity(
        occurrence.velocity.as_fraction(),
        capability=executor.capability,
        profile=profile,
    )
    return {
        "occurrence_id": occurrence.occurrence_id,
        "part_id": occurrence.part_id,
        "executor_id": executor.executor_id,
        "source_event_ids": list(occurrence.source_event_ids),
        "source_tie_ids": list(occurrence.source_tie_ids),
        "start_sample": occurrence.start.resolved_sample,
        "end_sample": occurrence.end.resolved_sample,
        "articulation": articulation,
        "range": range_evidence,
        "pitch": pitch,
        "velocity": velocity,
        "capability_binding": {
            "manifest_source_sha256": binding.manifest_source_sha256,
            "capability_projection_sha256": (
                binding.capability_projection_sha256
            ),
            "effective_manifest_sha256": binding.effective_manifest_sha256,
            "runtime_fingerprint_status": binding.runtime_fingerprint_status,
        },
    }


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2CapabilityPlan:
    """One sealed, non-render-authoritative capability-resolution artifact."""

    source_document_sha256: str
    score_render_projection_sha256: str
    score_v2_plan_sha256: str
    execution_profile_sha256: str
    capability_source_sha256: str
    roster_projection_sha256: str
    sample_rate: int
    occurrence_count: int
    occurrences_sha256: str
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2CapabilityPlan cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2CapabilityPlan must be created by "
            "compile_score_v2_capability_plan"
        )

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            values = self._identity_seal
        except AttributeError as exc:
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            ) from exc
        if type(values) is not tuple or len(values) != 11:
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            )
        (
            source_hash,
            projection_hash,
            plan_hash,
            profile_hash,
            capability_hash,
            roster_hash,
            sample_rate,
            occurrence_count,
            occurrences_hash,
            payload,
            artifact_hash,
        ) = values
        if (
            not _is_sha256(source_hash)
            or not _is_sha256(projection_hash)
            or not _is_sha256(plan_hash)
            or not _is_sha256(profile_hash)
            or not _is_sha256(capability_hash)
            or not _is_sha256(roster_hash)
            or type(sample_rate) is not int
            or sample_rate < 1
            or type(occurrence_count) is not int
            or occurrence_count < 0
            or not _is_sha256(occurrences_hash)
            or not _is_sha256(self.source_document_sha256)
            or not _is_sha256(self.score_render_projection_sha256)
            or not _is_sha256(self.score_v2_plan_sha256)
            or not _is_sha256(self.execution_profile_sha256)
            or not _is_sha256(self.capability_source_sha256)
            or not _is_sha256(self.roster_projection_sha256)
            or type(self.sample_rate) is not int
            or type(self.occurrence_count) is not int
            or not _is_sha256(self.occurrences_sha256)
            or self.source_document_sha256 != source_hash
            or self.score_render_projection_sha256 != projection_hash
            or self.score_v2_plan_sha256 != plan_hash
            or self.execution_profile_sha256 != profile_hash
            or self.capability_source_sha256 != capability_hash
            or self.roster_projection_sha256 != roster_hash
            or self.sample_rate != sample_rate
            or self.occurrence_count != occurrence_count
            or self.occurrences_sha256 != occurrences_hash
            or self._canonical_bytes is not payload
            or type(payload) is not bytes
            or not _is_sha256(self._artifact_sha256)
            or not _is_sha256(artifact_hash)
            or self._artifact_sha256 != artifact_hash
            or hashlib.sha256(payload).hexdigest() != artifact_hash
        ):
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            )
        try:
            document = strict_json_loads(
                payload,
                limits=AuthoringJsonLimits(
                    max_document_bytes=max(1, len(payload))
                ),
                require_object=True,
                require_js_safe_integers=True,
            )
        except (AuthoringJsonError, TypeError, ValueError) as exc:
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            ) from exc
        expected_root_keys = {
            "kind",
            "schema_version",
            "contract",
            "render_authority",
            "bindings",
            "sample_rate",
            "runtime_fingerprint_status",
            "tuning_resolution",
            "occurrence_count",
            "occurrences_sha256",
            "occurrences",
        }
        if type(document) is not dict or set(document) != expected_root_keys:
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            )
        bindings = document["bindings"]
        occurrences = document["occurrences"]
        if (
            document["kind"] != SCORE_V2_CAPABILITY_PLAN_KIND
            or type(document["schema_version"]) is not int
            or document["schema_version"]
            != SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION
            or document["contract"] != SCORE_V2_CAPABILITY_PLAN_CONTRACT
            or document["render_authority"] is not False
            or type(bindings) is not dict
            or set(bindings)
            != {
                "source_document_sha256",
                "score_render_projection_sha256",
                "score_v2_plan_sha256",
                "execution_profile_sha256",
                "capability_source_sha256",
                "roster_projection_sha256",
            }
            or bindings["source_document_sha256"] != source_hash
            or bindings["score_render_projection_sha256"]
            != projection_hash
            or bindings["score_v2_plan_sha256"] != plan_hash
            or bindings["execution_profile_sha256"] != profile_hash
            or bindings["capability_source_sha256"] != capability_hash
            or bindings["roster_projection_sha256"] != roster_hash
            or type(document["sample_rate"]) is not int
            or document["sample_rate"] != sample_rate
            or document["runtime_fingerprint_status"] != "not_captured"
            or type(document["tuning_resolution"]) is not dict
            or type(document["occurrence_count"]) is not int
            or document["occurrence_count"] != occurrence_count
            or document["occurrences_sha256"] != occurrences_hash
            or type(occurrences) is not list
            or len(occurrences) != occurrence_count
            or canonical_json_sha256(occurrences) != occurrences_hash
            or canonical_json_bytes(document) != payload
        ):
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            )
        for occurrence in occurrences:
            if type(occurrence) is not dict:
                raise ScoreV2CapabilityAdapterError(
                    "adapter.plan_integrity_mismatch"
                )
            capability_binding = occurrence.get("capability_binding")
            if (
                type(capability_binding) is not dict
                or set(capability_binding)
                != {
                    "manifest_source_sha256",
                    "capability_projection_sha256",
                    "effective_manifest_sha256",
                    "runtime_fingerprint_status",
                }
                or not _is_sha256(
                    capability_binding["manifest_source_sha256"]
                )
                or not _is_sha256(
                    capability_binding["capability_projection_sha256"]
                )
                or not _is_sha256(
                    capability_binding["effective_manifest_sha256"]
                )
                or capability_binding["runtime_fingerprint_status"]
                != "not_captured"
            ):
                raise ScoreV2CapabilityAdapterError(
                    "adapter.plan_integrity_mismatch"
                )
        return payload

    @property
    def canonical_bytes(self) -> bytes:
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
            raise ScoreV2CapabilityAdapterError(
                "adapter.plan_integrity_mismatch"
            )
        return value


def _make_capability_plan(
    document: dict[str, object],
    *,
    source_hash: str,
    projection_hash: str,
    plan_hash: str,
    profile_hash: str,
    capability_hash: str,
    roster_hash: str,
    sample_rate: int,
    occurrence_count: int,
    occurrences_hash: str,
) -> ScoreV2CapabilityPlan:
    payload = canonical_json_bytes(document)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    result = object.__new__(ScoreV2CapabilityPlan)
    for name, value in (
        ("source_document_sha256", source_hash),
        ("score_render_projection_sha256", projection_hash),
        ("score_v2_plan_sha256", plan_hash),
        ("execution_profile_sha256", profile_hash),
        ("capability_source_sha256", capability_hash),
        ("roster_projection_sha256", roster_hash),
        ("sample_rate", sample_rate),
        ("occurrence_count", occurrence_count),
        ("occurrences_sha256", occurrences_hash),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", artifact_hash),
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(
        result,
        "_identity_seal",
        (
            source_hash,
            projection_hash,
            plan_hash,
            profile_hash,
            capability_hash,
            roster_hash,
            sample_rate,
            occurrence_count,
            occurrences_hash,
            payload,
            artifact_hash,
        ),
    )
    result._trusted_artifact_bytes()
    return result


def compile_score_v2_capability_plan(
    source: ScoreSourceSnapshot,
    plan: ScoreV2Plan,
    profile: ScoreV2ExecutionProfile,
    roster: Roster,
    capability_sources: ScoreV2CapabilitySourceSnapshot,
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2CapabilityPlan:
    """Resolve the safe first Score-v2 subset without granting render authority."""

    active_limits = _active_limits(limits)
    trusted_source = _capture_source(source, limits=active_limits)
    trusted_profile = _capture_profile(profile)
    score = trusted_source.score
    assert type(score) is ScoreV2Document
    (
        plan_document,
        _profile_document,
        plan_payload,
        plan_hash,
    ) = _validate_plan_profile_bindings(
        source=trusted_source,
        score=score,
        plan=plan,
        profile=trusted_profile,
    )
    _roster_document, bindings = _validate_roster_generation(
        roster,
        capability_sources,
        limits=active_limits,
    )
    executors = _validate_executor_subset(
        score,
        roster,
        bindings,
        capability_sources,
    )
    try:
        plan_occurrences = plan.occurrences
        plan_sample_rate = plan_document["sample_rate"]
        plan_bindings = plan_document["bindings"]
        assert type(plan_bindings) is dict
        plan_projection_hash = plan_bindings[
            "score_render_projection_sha256"
        ]
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        ) from exc
    if (
        not _is_sha256(plan_hash)
        or not _is_sha256(plan_projection_hash)
        or type(plan_occurrences) is not tuple
        or type(plan_sample_rate) is not int
        or plan_sample_rate < 1
    ):
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        )
    if len(plan_occurrences) > active_limits.max_notes:
        raise ResourceLimitError(
            "adapter.too_many_occurrences",
            "Score-v2 capability occurrence count exceeds the note budget",
            actual=len(plan_occurrences),
            limit=active_limits.max_notes,
        )
    tuning, a4_hz, _tuning_exact = _resolve_tuning(
        score,
        trusted_profile,
    )
    profile_hash = trusted_profile.artifact_sha256
    capability_hash = capability_sources.artifact_sha256
    roster_hash = capability_sources.roster_projection_sha256
    base: dict[str, object] = {
        "kind": SCORE_V2_CAPABILITY_PLAN_KIND,
        "schema_version": SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION,
        "contract": SCORE_V2_CAPABILITY_PLAN_CONTRACT,
        "render_authority": False,
        "bindings": {
            "source_document_sha256": trusted_source.document_sha256,
            "score_render_projection_sha256": plan_projection_hash,
            "score_v2_plan_sha256": plan_hash,
            "execution_profile_sha256": profile_hash,
            "capability_source_sha256": capability_hash,
            "roster_projection_sha256": roster_hash,
        },
        "sample_rate": plan_sample_rate,
        "runtime_fingerprint_status": "not_captured",
        "tuning_resolution": tuning,
        "occurrence_count": len(plan_occurrences),
        # Fixed-width placeholder keeps incremental byte accounting exact.
        "occurrences_sha256": "0" * 64,
        "occurrences": [],
    }
    used_bytes = len(canonical_json_bytes(base))
    if used_bytes > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "adapter.document_too_large",
            "Score-v2 capability plan exceeds the plan JSON byte budget",
            actual=used_bytes,
            limit=active_limits.max_plan_json_bytes,
        )
    occurrence_documents: list[dict[str, object]] = []
    for occurrence in plan_occurrences:
        executor = executors.get(occurrence.part_id)
        if executor is None:
            raise ScoreV2CapabilityAdapterError(
                "adapter.part_assignment_not_one_to_one"
            )
        binding = bindings[executor.executor_id]
        document = _occurrence_document(
            occurrence,
            executor=executor,
            binding=binding,
            a4_hz=a4_hz,
            profile=trusted_profile,
        )
        prospective = (
            used_bytes
            + len(canonical_json_bytes(document))
            + (1 if occurrence_documents else 0)
        )
        if prospective > active_limits.max_plan_json_bytes:
            raise ResourceLimitError(
                "adapter.document_too_large",
                "Score-v2 capability plan exceeds the plan JSON byte budget",
                actual=prospective,
                limit=active_limits.max_plan_json_bytes,
            )
        occurrence_documents.append(document)
        used_bytes = prospective
    occurrences_hash = canonical_json_sha256(occurrence_documents)
    final_document = {
        **base,
        "occurrences_sha256": occurrences_hash,
        "occurrences": occurrence_documents,
    }
    final_bytes = canonical_json_bytes(final_document)
    if len(final_bytes) != used_bytes:
        raise RuntimeError(
            "Score-v2 capability plan byte accounting mismatch"
        )
    # The artifact is meaningful only while the descriptor-bound source
    # generation captured for this compilation is still present unchanged.
    try:
        capability_sources.revalidate_sources()
    except ScoreV2CapabilitySourceError as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.capability_source_changed"
        ) from exc
    # The renderer-independent result must not mix the generation validated at
    # entry with live roster/capability objects changed by a callback while
    # occurrences were being resolved.
    _final_roster_document, final_bindings = _validate_roster_generation(
        roster,
        capability_sources,
        limits=active_limits,
    )
    _validate_executor_subset(
        score,
        roster,
        final_bindings,
        capability_sources,
    )
    try:
        if plan.canonical_bytes != plan_payload:
            raise ScoreV2CapabilityAdapterError(
                "adapter.input_artifact_integrity_mismatch"
            )
    except ScoreV2CapabilityAdapterError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2CapabilityAdapterError(
            "adapter.input_artifact_integrity_mismatch"
        ) from exc
    return _make_capability_plan(
        final_document,
        source_hash=trusted_source.document_sha256,
        projection_hash=plan_projection_hash,
        plan_hash=plan_hash,
        profile_hash=profile_hash,
        capability_hash=capability_hash,
        roster_hash=roster_hash,
        sample_rate=plan_sample_rate,
        occurrence_count=len(occurrence_documents),
        occurrences_hash=occurrences_hash,
    )


__all__ = [
    "SCORE_V2_CAPABILITY_PLAN_CONTRACT",
    "SCORE_V2_CAPABILITY_PLAN_KIND",
    "SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION",
    "ScoreV2CapabilityAdapterError",
    "ScoreV2CapabilityPlan",
    "compile_score_v2_capability_plan",
]
