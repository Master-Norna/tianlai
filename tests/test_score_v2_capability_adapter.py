from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import tianlai.score_v2_capability_adapter as capability_adapter_module
from tianlai.canonical_json import canonical_json_bytes
from tianlai.capability import read_capability
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.roster import Roster, parse_roster_document
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2 import Rational
from tianlai.score_v2_capability_adapter import (
    SCORE_V2_CAPABILITY_PLAN_CONTRACT,
    ScoreV2CapabilityAdapterError,
    ScoreV2CapabilityPlan,
    compile_score_v2_capability_plan,
)
from tianlai.score_v2_capability_source import (
    capture_score_v2_capability_sources,
)
from tianlai.score_v2_execution_profile import (
    parse_score_v2_execution_profile,
)
from tianlai.score_v2_plan import compile_score_v2_plan
from tianlai.score_v2_time import ExactFraction


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score(
    *,
    pitch: tuple[int, int] = (60, 1),
    articulation: str | None = None,
    reference_midi: tuple[int, int] = (69, 1),
    reference_hz: tuple[int, int] = (440, 1),
) -> dict[str, object]:
    note: dict[str, object] = {
        "event_id": "n1",
        "position": {
            "measure_id": "m1",
            "offset_quarters": _r(0),
        },
        "duration_quarters": _r(1),
        "written_pitch": {
            "step": "C",
            "alter": _r(0),
            "octave": 4,
        },
        "sounding_pitch": {"midi_note": _r(*pitch)},
    }
    if articulation is not None:
        note["articulations"] = [articulation]
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "capability adapter fixture",
        "timeline": {
            "measures": [
                {
                    "measure_id": "m1",
                    "actual_duration_quarters": _r(4),
                }
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-60",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(60),
                }
            ],
        },
        "tuning": {
            "tuning_id": "fixture-tuning",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(*reference_midi),
            "reference_frequency_hz": _r(*reference_hz),
        },
        "parts": [
            {
                "part_id": "lead",
                "default_dynamic": "mf",
                "notes": [note],
            }
        ],
        "form": {"mode": "linear"},
    }


def _profile_document(
    *,
    sample_time: str = "exact",
    tuning_value: str = "exact",
    tuning_semantic: str = "exact",
    velocity_value: str = "adapt",
    velocity_semantic: str = "approximate",
    pitch_value: str = "exact",
    pitch_semantic: str = "exact",
    range_policy: str = "declared_hard",
    articulation_mapping: str = "direct_only",
    articulation_semantic: str = "exact",
) -> dict[str, object]:
    return {
        "kind": "tianlai.score_v2_execution_profile",
        "schema_version": 1,
        "sample_time_policy": sample_time,
        "dynamic_profile": {
            "mf": {"numerator": 3, "denominator": 5},
        },
        "note_velocity": {
            "value_policy": velocity_value,
            "semantic_policy": velocity_semantic,
        },
        "tuning": {
            "value_policy": tuning_value,
            "semantic_policy": tuning_semantic,
        },
        "pitch": {
            "value_policy": pitch_value,
            "semantic_policy": pitch_semantic,
            "range_policy": range_policy,
        },
        "articulation": {
            "mapping_policy": articulation_mapping,
            "semantic_policy": articulation_semantic,
        },
        "phrase_policy": "reject",
    }


def _write_manifest(
    root: Path,
    *,
    instrument_type: str = "oscillator",
    note_min: int = 0,
    note_max: int = 127,
    articulations: tuple[str, ...] = (),
    default_articulation: str | None = None,
) -> Path:
    directory = root / "instrument"
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "name": "adapter instrument",
        "type": instrument_type,
        "note_min": note_min,
        "note_max": note_max,
        "articulation_auto_default": False,
    }
    if articulations:
        document["allowed_articulations"] = list(articulations)
    if default_articulation is not None:
        document["default_articulation"] = default_articulation
    path = directory / "instrument.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _compile_context(
    tmp_path: Path,
    *,
    score_document: dict[str, object] | None = None,
    profile_document: dict[str, object] | None = None,
    manifest_options: dict[str, object] | None = None,
    assignment_options: dict[str, object] | None = None,
):
    score_document = score_document or _score()
    profile_document = profile_document or _profile_document()
    manifest_path = _write_manifest(tmp_path, **(manifest_options or {}))
    capability = read_capability(manifest_path, root=tmp_path)
    assignment: dict[str, object] = {
        "part": "lead",
        "instrument": capability.relative_path,
        "articulation_auto": False,
    }
    assignment.update(assignment_options or {})
    roster = parse_roster_document(
        {"name": "adapter roster", "assignments": [assignment]},
        {capability.relative_path: capability},
    )
    source = snapshot_score_document(score_document)
    profile = parse_score_v2_execution_profile(profile_document)
    dynamic_profile = {
        level.mark: Rational(level.value.numerator, level.value.denominator)
        for level in profile.dynamic_profile
    }
    plan = compile_score_v2_plan(
        source,
        sample_rate=8_000,
        sample_time_policy=profile.sample_time_policy,  # type: ignore[arg-type]
        dynamic_profile=dynamic_profile,
    )
    capability_sources = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )
    return (
        source,
        plan,
        profile,
        roster,
        capability_sources,
        manifest_path,
    )


def _compile_adapter(context, *, limits: ProjectLimits | None = None):
    source, plan, profile, roster, capability_sources, _manifest_path = context
    return compile_score_v2_capability_plan(
        source,
        plan,
        profile,
        roster,
        capability_sources,
        limits=limits,
    )


def _error_code(callable_: object) -> str:
    with pytest.raises(ScoreV2CapabilityAdapterError) as caught:
        callable_()  # type: ignore[operator]
    assert str(caught.value) == caught.value.code
    return caught.value.code


def test_native_subset_produces_sealed_non_render_authority(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    capability_plan = _compile_adapter(context)
    raw = capability_plan.to_dict()

    assert type(capability_plan) is ScoreV2CapabilityPlan
    assert raw["contract"] == SCORE_V2_CAPABILITY_PLAN_CONTRACT
    assert raw["render_authority"] is False
    assert raw["runtime_fingerprint_status"] == "not_captured"
    assert raw["occurrence_count"] == 1
    assert raw["occurrences"][0]["executor_id"] == "lead"
    assert raw["occurrences"][0]["pitch"]["adapted"] is False
    assert raw["occurrences"][0]["velocity"]["adapted"] is True
    assert raw["occurrences"][0]["range"]["status"] == (
        "inside_declared_hard_range"
    )
    assert raw["tuning_resolution"]["numeric_fidelity"] == "exact"
    assert len(raw["bindings"]["capability_source_sha256"]) == 64
    assert capability_plan.canonical_bytes == canonical_json_bytes(raw)
    assert capability_plan.artifact_sha256 == hashlib.sha256(
        capability_plan.canonical_bytes
    ).hexdigest()


def test_plan_must_have_been_compiled_from_same_profile_subset(
    tmp_path: Path,
) -> None:
    context = list(_compile_context(tmp_path))
    other_profile = parse_score_v2_execution_profile(
        {
            **_profile_document(),
            "dynamic_profile": {
                "mf": {"numerator": 4, "denominator": 5}
            },
        }
    )
    context[2] = other_profile
    assert _error_code(lambda: _compile_adapter(tuple(context))) == (
        "adapter.execution_profile_binding_mismatch"
    )


def test_semantic_approximation_requires_creator_consent(tmp_path: Path) -> None:
    profile = _profile_document(velocity_semantic="exact")
    context = _compile_context(tmp_path, profile_document=profile)
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.velocity_not_representable"
    )


def test_hidden_roster_transform_is_rejected(tmp_path: Path) -> None:
    context = _compile_context(
        tmp_path,
        assignment_options={"transpose": 1},
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.executor_transform_unsupported"
    )


def test_declared_range_is_enforced_with_exact_score_coordinate(
    tmp_path: Path,
) -> None:
    context = _compile_context(
        tmp_path,
        manifest_options={"note_min": 0, "note_max": 59},
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.pitch_outside_declared_range"
    )


def test_verified_high_quality_policy_fails_without_approved_evidence(
    tmp_path: Path,
) -> None:
    context = _compile_context(
        tmp_path,
        profile_document=_profile_document(
            range_policy="verified_high_quality"
        ),
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.high_quality_range_unverified"
    )


def test_non_a69_tuning_requires_separate_tuning_and_pitch_adaptation(
    tmp_path: Path,
) -> None:
    score = _score(reference_midi=(60, 1), reference_hz=(262, 1))
    strict_context = _compile_context(tmp_path / "strict", score_document=score)
    assert _error_code(lambda: _compile_adapter(strict_context)) == (
        "adapter.tuning_adaptation_not_authorized"
    )

    adapted_context = _compile_context(
        tmp_path / "adapted",
        score_document=score,
        profile_document=_profile_document(
            tuning_value="adapt",
            pitch_value="adapt",
        ),
    )
    result = _compile_adapter(adapted_context).to_dict()
    assert result["tuning_resolution"]["adapted"] is True
    assert result["occurrences"][0]["pitch"]["adapted"] is True


def test_extreme_rational_tuning_is_rejected_before_bigint_materialization(
    tmp_path: Path,
) -> None:
    context = _compile_context(
        tmp_path,
        score_document=_score(
            reference_midi=(9_007_199_254_740_991, 1),
            reference_hz=(440, 1),
        ),
        profile_document=_profile_document(
            tuning_value="adapt",
            pitch_value="adapt",
        ),
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.tuning_transport_overflow"
    )


@pytest.mark.parametrize(
    ("pitch", "reference_midi"),
    (
        ((127, 1), (-12_207, 1)),
        ((0, 1), (12_957, 1)),
    ),
)
def test_tuned_physical_pitch_must_be_finite_for_legacy_transport(
    tmp_path: Path,
    pitch: tuple[int, int],
    reference_midi: tuple[int, int],
) -> None:
    context = _compile_context(
        tmp_path,
        score_document=_score(
            pitch=pitch,
            reference_midi=reference_midi,
            reference_hz=(1, 1),
        ),
        profile_document=_profile_document(pitch_value="adapt"),
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.pitch_transport_overflow"
    )


def test_declared_range_uses_tuned_physical_pitch_coordinate(
    tmp_path: Path,
) -> None:
    context = _compile_context(
        tmp_path,
        score_document=_score(
            pitch=(60, 1),
            reference_midi=(69, 1),
            reference_hz=(880, 1),
        ),
        profile_document=_profile_document(pitch_value="adapt"),
        manifest_options={"note_min": 59, "note_max": 61},
    )
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.pitch_outside_declared_range"
    )


def test_distinct_articulation_mapping_needs_mapping_and_semantic_consent(
    tmp_path: Path,
) -> None:
    score = _score(articulation="staccato")
    manifest = {
        "instrument_type": "dedicated_sfz",
        "articulations": ("short", "sustain"),
        "default_articulation": "sustain",
    }
    assignment = {"articulation_map": {"staccato": "short"}}
    strict = _compile_context(
        tmp_path / "strict",
        score_document=score,
        manifest_options=manifest,
        assignment_options=assignment,
    )
    assert _error_code(lambda: _compile_adapter(strict)) == (
        "adapter.articulation_mapping_not_authorized"
    )

    allowed = _compile_context(
        tmp_path / "allowed",
        score_document=score,
        profile_document=_profile_document(
            articulation_mapping="allow_roster_mapping",
            articulation_semantic="approximate",
        ),
        manifest_options=manifest,
        assignment_options=assignment,
    )
    resolution = _compile_adapter(allowed).to_dict()["occurrences"][0][
        "articulation"
    ]
    assert resolution["requested_value"] == "staccato"
    assert resolution["resolved_value"] == "short"
    assert resolution["semantic_fidelity"] == "approximated"


def test_manifest_generation_change_before_completion_is_rejected(
    tmp_path: Path,
) -> None:
    context = _compile_context(tmp_path)
    manifest_path = context[-1]
    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed["note_max"] = 126
    manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.capability_source_mismatch"
    )


def test_capability_plan_budget_is_exact_at_boundary(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    baseline = _compile_adapter(context)
    exact = ProjectLimits(
        max_plan_json_bytes=baseline.canonical_json_bytes_size
    )
    assert _compile_adapter(context, limits=exact).artifact_sha256 == (
        baseline.artifact_sha256
    )
    one_short = ProjectLimits(
        max_plan_json_bytes=baseline.canonical_json_bytes_size - 1
    )
    with pytest.raises(ResourceLimitError) as caught:
        _compile_adapter(context, limits=one_short)
    assert caught.value.code == "adapter.document_too_large"


def test_capability_plan_public_state_mutation_fails_closed(tmp_path: Path) -> None:
    capability_plan = _compile_adapter(_compile_context(tmp_path))
    object.__setattr__(capability_plan, "sample_rate", 48_000)
    assert _error_code(capability_plan.to_dict) == (
        "adapter.plan_integrity_mismatch"
    )
    with pytest.raises(TypeError, match="must be created"):
        ScoreV2CapabilityPlan()


def test_source_and_roster_generation_mismatch_fail_closed(tmp_path: Path) -> None:
    context = list(_compile_context(tmp_path))
    newer_score = copy.deepcopy(_score())
    newer_score["title"] = "another generation"
    context[0] = snapshot_score_document(newer_score)
    assert _error_code(lambda: _compile_adapter(tuple(context))) == (
        "adapter.plan_binding_mismatch"
    )

    context = list(_compile_context(tmp_path / "roster"))
    object.__setattr__(context[3], "name", "changed roster")
    assert _error_code(lambda: _compile_adapter(tuple(context))) == (
        "adapter.roster_binding_mismatch"
    )


def test_resealed_input_plan_occurrence_cannot_diverge_from_artifact(
    tmp_path: Path,
) -> None:
    context = list(_compile_context(tmp_path))
    plan = context[1]
    forged_occurrences = (
        plan.occurrences[0]._replace(velocity=ExactFraction(1, 10)),
    )
    forged_seal = list(plan._identity_seal)
    forged_seal[7] = forged_occurrences
    object.__setattr__(plan, "occurrences", forged_occurrences)
    object.__setattr__(plan, "_identity_seal", tuple(forged_seal))

    assert _error_code(lambda: _compile_adapter(tuple(context))) == (
        "adapter.input_artifact_integrity_mismatch"
    )


def test_resealed_input_plan_sample_rate_cannot_diverge_from_artifact(
    tmp_path: Path,
) -> None:
    context = list(_compile_context(tmp_path))
    plan = context[1]
    forged_seal = list(plan._identity_seal)
    forged_seal[4] = 16_000
    object.__setattr__(plan, "sample_rate", 16_000)
    object.__setattr__(plan, "_identity_seal", tuple(forged_seal))

    assert _error_code(lambda: _compile_adapter(tuple(context))) == (
        "adapter.input_artifact_integrity_mismatch"
    )


def test_live_capability_change_during_occurrence_compilation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _compile_context(tmp_path)
    original = capability_adapter_module._resolve_articulation

    def mutate_after_initial_projection_check(*args, **kwargs):
        resolution = original(*args, **kwargs)
        executor = args[1]
        note_pitch = executor.capability.note_pitch
        assert note_pitch is not None
        object.__setattr__(
            note_pitch,
            "source",
            "tampered after the captured projection was validated",
        )
        return resolution

    monkeypatch.setattr(
        capability_adapter_module,
        "_resolve_articulation",
        mutate_after_initial_projection_check,
    )

    assert _error_code(lambda: _compile_adapter(context)) == (
        "adapter.capability_projection_mismatch"
    )


def test_resealed_occurrence_tampering_without_occurrence_binding_is_rejected(
    tmp_path: Path,
) -> None:
    capability_plan = _compile_adapter(_compile_context(tmp_path))
    forged_document = capability_plan.to_dict()
    occurrence = forged_document["occurrences"][0]
    occurrence["capability_binding"]["manifest_source_sha256"] = "0" * 64
    forged_payload = canonical_json_bytes(forged_document)
    forged_artifact_hash = hashlib.sha256(forged_payload).hexdigest()

    object.__setattr__(capability_plan, "_canonical_bytes", forged_payload)
    object.__setattr__(
        capability_plan,
        "_artifact_sha256",
        forged_artifact_hash,
    )
    object.__setattr__(
        capability_plan,
        "_identity_seal",
        (
            *capability_plan._identity_seal[:-2],
            forged_payload,
            forged_artifact_hash,
        ),
    )

    assert _error_code(capability_plan.to_dict) == (
        "adapter.plan_integrity_mismatch"
    )
    assert _error_code(lambda: capability_plan.artifact_sha256) == (
        "adapter.plan_integrity_mismatch"
    )


def test_hostile_non_exact_plan_field_type_fails_without_comparison_leak(
    tmp_path: Path,
) -> None:
    class HostileComparison:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("hostile __eq__ escaped")

        def __ne__(self, _other: object) -> bool:
            raise RuntimeError("hostile __ne__ escaped")

    capability_plan = _compile_adapter(_compile_context(tmp_path))
    object.__setattr__(
        capability_plan,
        "source_document_sha256",
        HostileComparison(),
    )

    assert _error_code(capability_plan.to_dict) == (
        "adapter.plan_integrity_mismatch"
    )


def test_executor_limit_precedes_roster_canonical_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = list(_compile_context(tmp_path))
    roster = context[3]
    first = roster.executors[0]
    second = replace(first, executor_id="pad", part_id="pad")
    two_executor_roster = replace(
        roster,
        executors=(first, second),
    )
    context[3] = two_executor_roster
    context[4] = capture_score_v2_capability_sources(
        two_executor_roster,
        catalogue_root=tmp_path,
    )

    def fail_if_materialized(_self: Roster) -> dict[str, object]:
        raise AssertionError("Roster.to_dict ran before max_executors check")

    monkeypatch.setattr(Roster, "to_dict", fail_if_materialized)
    with pytest.raises(ResourceLimitError) as caught:
        _compile_adapter(
            tuple(context),
            limits=ProjectLimits(max_executors=1),
        )
    assert caught.value.code == "adapter.too_many_executors"
