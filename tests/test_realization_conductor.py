from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tianlai.canonical_json import (
    canonical_json_bytes,
    canonical_json_sha256,
)
from tianlai.capability import (
    InstrumentCapability,
    NoteVelocityCapability,
    load_capabilities,
)
from tianlai.conductor import ExpressionSettings, PerformancePlan, build_plan
from tianlai.realization import (
    DEFAULTS_PROFILE_V1,
    REALIZATION_KIND,
    RealizationDocument,
    empty_realization,
    parse_realization_document,
)
from tianlai.roster import Roster, parse_roster_document
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.score import ScoreDocument, parse_score_document


ROOT = Path(__file__).resolve().parents[1]
PIANO = "键盘乐器/钢琴"


@pytest.fixture(scope="module")
def catalog() -> dict[str, InstrumentCapability]:
    return load_capabilities(ROOT / "乐器")


def _score_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": "realization to conductor contract",
        "sample_rate": 8_000,
        "tail_seconds": 0.0,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 60,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "Piano",
                "notes": [
                    {
                        "event_id": "note-1",
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "velocity": 0.5,
                        "staff": 1,
                        "voice": "upper",
                    }
                ],
            }
        ],
    }


def _roster_document(
    *,
    assigned_part: str = "Piano",
    dropped_parts: tuple[str, ...] = (),
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": "realization conductor roster",
        "assignments": [
            {
                "part": assigned_part,
                "instrument": PIANO,
            }
        ],
    }
    if dropped_parts:
        document["drop_parts"] = list(dropped_parts)
    return document


def _strict_expression() -> ExpressionSettings:
    return ExpressionSettings(
        mode="strict",
        structural=False,
        physical=False,
        humanize_depth=0.0,
        timing_ms=0.0,
        velocity_spread=0.0,
        seed=0,
    )


def _realization_document(
    raw_score: dict[str, Any],
    *,
    note_overrides: list[dict[str, Any]] | None = None,
    control_lanes: list[dict[str, Any]] | None = None,
    mode: str = "captured",
) -> dict[str, Any]:
    return {
        "kind": REALIZATION_KIND,
        "schema_version": 1,
        "score_sha256": canonical_json_sha256(raw_score),
        "defaults_profile": DEFAULTS_PROFILE_V1,
        "mode": mode,
        "note_overrides": note_overrides or [],
        "control_lanes": control_lanes or [],
    }


def _parse_context(
    raw_score: dict[str, Any],
    catalog: dict[str, InstrumentCapability],
    *,
    roster_document: dict[str, Any] | None = None,
) -> tuple[ScoreDocument, Roster]:
    score = parse_score_document(raw_score)
    roster = parse_roster_document(
        roster_document or _roster_document(),
        catalog,
    )
    return score, roster


def _parse_realization(
    document: dict[str, Any],
    raw_score: dict[str, Any],
    score: ScoreDocument,
) -> RealizationDocument:
    return parse_realization_document(
        document,
        score_document=raw_score,
        score=score,
    )


def _build_with_realization(
    raw_score: dict[str, Any],
    score: ScoreDocument,
    roster: Roster,
    document: dict[str, Any],
) -> PerformancePlan:
    realization = _parse_realization(document, raw_score, score)
    return build_plan(
        score,
        roster,
        _strict_expression(),
        realization,
        score_document=raw_score,
        realization_document=document,
    )


def _lane(
    *,
    control: str,
    value: float,
    beat: float = 1.0,
    interpolation: str = "step",
    time_policy: str = "adapt",
    value_policy: str = "exact",
    semantic_policy: str = "exact",
    target: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "lane_id": f"{control}-lane",
        "target": target or {"part_id": "Piano"},
        "control": control,
        "interpolation": interpolation,
        "time_policy": time_policy,
        "value_policy": value_policy,
        "semantic_policy": semantic_policy,
        "points": [{"bar": 1, "beat": beat, "value": value}],
    }


def _events(plan: PerformancePlan, event_type: str) -> list[dict[str, Any]]:
    return [
        event
        for event in plan.parts[0].performance["events"]
        if event["type"] == event_type
    ]


def test_absent_and_empty_realization_are_byte_equivalent(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    baseline = build_plan(score, roster, _strict_expression())
    empty = empty_realization(canonical_json_sha256(raw_score))
    explicit_empty = build_plan(
        score,
        roster,
        _strict_expression(),
        empty,
        score_document=raw_score,
    )

    assert canonical_json_bytes(explicit_empty.to_dict()) == canonical_json_bytes(
        baseline.to_dict()
    )
    assert "realization" not in baseline.to_dict()
    assert "realization" not in explicit_empty.to_dict()


def test_plan_binds_realization_to_score_hash_and_rejects_another_revision(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "velocity": {
                    "strategy": "lock",
                    "value": 0.6,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )
    realization = _parse_realization(document, raw_score, score)

    plan = build_plan(
        score,
        roster,
        _strict_expression(),
        realization,
        score_document=raw_score,
        realization_document=document,
    )

    assert plan.realization == {
        "kind": REALIZATION_KIND,
        "schema_version": 1,
        "score_sha256": canonical_json_sha256(raw_score),
        "canonical_sha256": canonical_json_sha256(document),
        "defaults_profile": DEFAULTS_PROFILE_V1,
        "mode": "captured",
    }

    revised_raw = copy.deepcopy(raw_score)
    revised_raw["title"] = "a different score revision"
    revised_score = parse_score_document(revised_raw)
    with pytest.raises(ValueError, match="score_sha256 does not match score_document"):
        build_plan(
            revised_score,
            roster,
            _strict_expression(),
            realization,
            score_document=revised_raw,
            realization_document=document,
        )


def test_note_overrides_merge_then_freeze_on_the_sample_grid_with_evidence(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "timing_offset_ms": {
                    "strategy": "add",
                    "value": 0.2,
                    "value_policy": "adapt",
                    "semantic_policy": "exact",
                },
                "gate_ratio": {
                    "strategy": "scale",
                    "value": 0.5,
                    "value_policy": "adapt",
                    "semantic_policy": "exact",
                },
                "velocity": {
                    "strategy": "add",
                    "value": 0.1,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    plan = _build_with_realization(raw_score, score, roster, document)

    note_on = _events(plan, "note_on")[0]
    note_off = _events(plan, "note_off")[0]
    assert note_on["time"] == pytest.approx(8_002 / 8_000)
    assert note_off["time"] == pytest.approx(11_802 / 8_000)
    assert note_on["velocity"] == pytest.approx(0.6)

    trace = plan.parts[0].trace[0]
    evidence = trace["推导"]["realization"]
    assert evidence["timing_offset_ms"] == {
        "strategy": "add",
        "automatic_value": 0.0,
        "operand": 0.2,
        "resolved_value": 0.2,
        "value_policy": "adapt",
        "semantic_policy": "exact",
        "locked": False,
        "contract_scope": "performance_plan",
    }
    assert evidence["gate_ratio"]["strategy"] == "scale"
    assert evidence["gate_ratio"]["automatic_value"] == pytest.approx(0.95)
    assert evidence["gate_ratio"]["resolved_value"] == pytest.approx(0.475)
    assert evidence["velocity"]["strategy"] == "add"
    assert evidence["velocity"]["automatic_value"] == pytest.approx(0.5)
    assert evidence["velocity"]["resolved_value"] == pytest.approx(0.6)
    assert evidence["scheduler"] == {
        "sample_rate": 8_000,
        "requested_start_seconds": pytest.approx(1.0002),
        "resolved_start_sample": 8_002,
        "resolved_start_seconds": pytest.approx(8_002 / 8_000),
        "timing_resolution": {
            "value_policy": "adapt",
            "requested_seconds": pytest.approx(1.0002),
            "boundary_resolved_seconds": pytest.approx(1.0002),
            "scheduled_before_grid_seconds": pytest.approx(1.0002),
            "resolved_seconds": pytest.approx(8_002 / 8_000),
            "boundary_adapted": False,
            "grid_adapted": True,
            "adapted": True,
        },
        "requested_release_seconds": pytest.approx(1.4752),
        "resolved_release_sample": 11_802,
        "resolved_release_seconds": pytest.approx(11_802 / 8_000),
        "resolved_logical_gate_start_sample": 8_002,
        "resolved_logical_gate_start_seconds": pytest.approx(8_002 / 8_000),
        "requested_gate_seconds": pytest.approx(0.475),
        "resolved_gate_seconds": pytest.approx(0.475),
        "effective_gate_ratio": pytest.approx(0.475),
        "gate_resolution": {
            "value_policy": "adapt",
            "requested_seconds": pytest.approx(0.475),
            "resolved_seconds": pytest.approx(0.475),
            "adapted": False,
        },
    }


def test_merged_numeric_overflow_is_rejected_instead_of_clamped(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "velocity": {
                    "strategy": "add",
                    "value": 0.75,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="must resolve between 0 and 1"):
        _build_with_realization(raw_score, score, roster, document)


def test_release_velocity_fails_closed_for_an_unsupported_instrument(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "release_velocity": {
                    "strategy": "replace",
                    "value": 0.25,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(
        ValueError,
        match="does not declare an audible release-velocity implementation",
    ):
        _build_with_realization(raw_score, score, roster, document)


def test_late_part_step_lane_materializes_default_and_precedes_same_sample_note(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(control="sustain_pedal", value=1.0, beat=2.0),
        ],
    )

    plan = _build_with_realization(raw_score, score, roster, document)

    controls = _events(plan, "control")
    assert controls == [
        {"time": 0.0, "type": "control", "name": "sustain_pedal", "value": 0.0},
        {"time": 1.0, "type": "control", "name": "sustain_pedal", "value": 1.0},
    ]
    same_sample = [
        event["type"]
        for event in plan.parts[0].performance["events"]
        if event["time"] == 1.0
    ]
    assert same_sample[:2] == ["control", "note_on"]

    trace = plan.parts[0].control_trace
    assert len(trace) == 2
    assert trace[0]["materialized_default"] is True
    assert "requested_value" not in trace[0]
    assert trace[0]["resolved_sample"] == 0
    assert trace[0]["resolved_value"] == 0.0
    assert trace[1]["materialized_default"] is False
    assert trace[1]["requested_value"] == 1.0
    assert trace[1]["adapted"] is False
    assert trace[1]["resolved_sample"] == 8_000


def test_discrete_control_requires_exact_value_or_explicit_adaptation(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    exact = _realization_document(
        raw_score,
        control_lanes=[
            _lane(control="sustain_pedal", value=0.4, value_policy="exact"),
        ],
    )

    with pytest.raises(ValueError, match="not exactly representable"):
        _build_with_realization(raw_score, score, roster, exact)

    adapted = copy.deepcopy(exact)
    adapted["control_lanes"][0]["value_policy"] = "adapt"
    plan = _build_with_realization(raw_score, score, roster, adapted)
    assert _events(plan, "control") == [
        {"time": 0.0, "type": "control", "name": "sustain_pedal", "value": 0.0}
    ]
    trace = plan.parts[0].control_trace[0]
    assert trace["requested_value"] == 0.4
    assert trace["resolved_value"] == 0.0
    assert trace["adapted"] is True
    assert trace["fidelity"] == "adapted"
    assert trace["steps"] == 2


def test_una_corda_requires_explicit_semantic_approximation_consent(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    exact = _realization_document(
        raw_score,
        control_lanes=[
            _lane(control="una_corda", value=0.4, semantic_policy="exact"),
        ],
    )

    with pytest.raises(ValueError, match="semantic_policy='approximate'"):
        _build_with_realization(raw_score, score, roster, exact)

    approximate = copy.deepcopy(exact)
    approximate["control_lanes"][0]["semantic_policy"] = "approximate"
    plan = _build_with_realization(raw_score, score, roster, approximate)
    trace = plan.parts[0].control_trace[0]
    assert trace["semantic_policy"] == "approximate"
    assert trace["semantic_fidelity"] == "approximated"
    assert trace["resolved_value"] == 0.4
    assert "reducing note-on velocity and brightness" in trace[
        "approximation_reason"
    ]


def test_linear_control_lane_fails_closed_at_capability_preflight(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                interpolation="linear",
            )
        ],
    )

    with pytest.raises(ValueError, match="does not allow 'linear' interpolation"):
        _build_with_realization(raw_score, score, roster, document)


def test_voice_target_fails_closed_while_runtime_controls_are_part_wide(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                target={"part_id": "Piano", "voice": "upper"},
            )
        ],
    )

    with pytest.raises(ValueError, match="scope 'per_note'"):
        _build_with_realization(raw_score, score, roster, document)


def test_control_lane_targeting_a_dropped_part_fails_closed(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    raw_score["parts"].append(
        {
            "id": "Anchor",
            "notes": [
                {
                    "event_id": "anchor-1",
                    "bar": 1,
                    "beat": 1,
                    "duration_beats": 1,
                    "pitch": "C4",
                }
            ],
        }
    )
    score, roster = _parse_context(
        raw_score,
        catalog,
        roster_document=_roster_document(
            assigned_part="Anchor",
            dropped_parts=("Piano",),
        ),
    )
    document = _realization_document(
        raw_score,
        control_lanes=[_lane(control="sustain_pedal", value=1.0)],
    )

    with pytest.raises(ValueError, match="targets dropped part 'Piano'"):
        _build_with_realization(raw_score, score, roster, document)


def test_control_lane_checks_every_final_routed_articulation(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    roster_document = {
        "name": "conditional control roster",
        "assignments": [
            {
                "part": "Piano",
                "instrument": "管弦乐/打击乐组/三角铁",
                "articulation_auto": False,
            }
        ],
    }
    score, roster = _parse_context(
        raw_score,
        catalog,
        roster_document=roster_document,
    )
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                semantic_policy="approximate",
            )
        ],
    )

    with pytest.raises(
        ValueError,
        match="not applicable to articulation 'open'",
    ):
        _build_with_realization(raw_score, score, roster, document)

    raw_score["parts"][0]["notes"][0]["articulation"] = "roll"
    score, roster = _parse_context(
        raw_score,
        catalog,
        roster_document=roster_document,
    )
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                semantic_policy="approximate",
            )
        ],
    )
    plan = _build_with_realization(raw_score, score, roster, document)
    assert _events(plan, "control")


def test_latched_control_is_scoped_to_shifted_note_on_and_then_restored(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "timing_offset_ms": {
                    "strategy": "lock",
                    "value": 1.0,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
        control_lanes=[
            {
                **_lane(
                    control="una_corda",
                    value=1.0,
                    beat=2.0,
                    semantic_policy="approximate",
                ),
                "points": [
                    {"bar": 1, "beat": 2.0, "value": 1.0},
                    {"bar": 1, "beat": 2.0005, "value": 0.0},
                ],
            }
        ],
    )

    plan = _build_with_realization(raw_score, score, roster, document)
    boundary = [
        event
        for event in plan.parts[0].performance["events"]
        if event["time"] == pytest.approx(1.001)
    ]
    assert [event["type"] for event in boundary] == [
        "control",
        "note_on",
        "control",
    ]
    assert [boundary[0]["value"], boundary[2]["value"]] == [1.0, 0.0]
    assert boundary[1]["midi_note"] == 60.0
    assert boundary[1]["velocity"] == 0.5
    assert boundary[1]["source_event_id"] == "note-1"
    trace = [
        item
        for item in plan.parts[0].control_trace
        if item["resolved_sample"] == 8_008
    ]
    assert trace[0]["materialized_for_note_boundary"] == "note_on_latched"
    assert trace[1]["restored_after_note_boundary"] == "note_on_latched"


def test_release_gate_is_scoped_to_shifted_note_off_and_then_restored(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note["beat"] = 1.0
    note["duration_beats"] = 2.0
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "gate_ratio": {
                    "strategy": "lock",
                    "value": 1.50004,
                    "value_policy": "adapt",
                    "semantic_policy": "exact",
                },
            }
        ],
        control_lanes=[
            {
                **_lane(
                    control="sustain_pedal",
                    value=1.0,
                    beat=4.0,
                ),
                "points": [
                    {"bar": 1, "beat": 4.0, "value": 1.0},
                    {"bar": 1, "beat": 4.0001, "value": 0.0},
                ],
            }
        ],
    )

    plan = _build_with_realization(raw_score, score, roster, document)
    boundary = [
        event
        for event in plan.parts[0].performance["events"]
        if event["time"] == pytest.approx(3.000125)
    ]
    assert [event["type"] for event in boundary] == [
        "control",
        "control",
        "note_off",
        "control",
    ]
    assert [
        boundary[0]["value"],
        boundary[1]["value"],
        boundary[3]["value"],
    ] == [0.0, 1.0, 0.0]
    assert boundary[2]["source_event_id"] == "note-1"
    trace = [
        item
        for item in plan.parts[0].control_trace
        if item["resolved_sample"] == 24_001
    ]
    materialized = [
        item
        for item in trace
        if "materialized_for_note_boundary" in item
    ]
    restored = [
        item for item in trace if "restored_after_note_boundary" in item
    ]
    assert materialized[0]["materialized_for_note_boundary"] == "release_gate"
    assert restored[0]["restored_after_note_boundary"] == "release_gate"


def test_release_gate_samples_the_interpreted_key_up_not_notated_end(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note["beat"] = 1.0
    note["duration_beats"] = 2.0
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "gate_ratio": {
                    "strategy": "lock",
                    "value": 0.5,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                beat=2.5,
            )
        ],
    )

    plan = _build_with_realization(raw_score, score, roster, document)
    at_key_up = [
        event
        for event in plan.parts[0].performance["events"]
        if event["time"] == pytest.approx(1.0)
    ]
    assert [event["type"] for event in at_key_up] == ["note_off"]
    assert not any(
        item.get("materialized_for_note_boundary") == "release_gate"
        for item in plan.parts[0].control_trace
    )


def test_note_velocity_requires_numeric_and_semantic_consent(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    score, roster = _parse_context(raw_score, catalog)
    quantized = NoteVelocityCapability(
        fidelity="adapted",
        semantic_fidelity="approximated",
        approximation_reason="test backend models only amplitude",
        steps=127,
        quantization_exponent=1.25,
        quantization_output_range=(1, 127),
        zero_behavior="minimum_nonzero",
        source="test:velocity-grid",
    )
    executor = replace(
        roster.executors[0],
        capability=replace(
            roster.executors[0].capability,
            note_velocity=quantized,
        ),
    )
    roster = replace(roster, executors=(executor,))
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "velocity": {
                    "strategy": "lock",
                    "value": 0.61,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="not exactly representable"):
        _build_with_realization(raw_score, score, roster, document)

    document["note_overrides"][0]["velocity"]["value_policy"] = "adapt"
    with pytest.raises(ValueError, match="semantic_policy='approximate'"):
        _build_with_realization(raw_score, score, roster, document)

    document["note_overrides"][0]["velocity"][
        "semantic_policy"
    ] = "approximate"
    plan = _build_with_realization(raw_score, score, roster, document)
    expected = (round(0.61**1.25 * 127) / 127) ** (1.0 / 1.25)
    note_on = _events(plan, "note_on")[0]
    assert note_on["velocity"] == pytest.approx(expected)
    execution = plan.parts[0].trace[0]["推导"]["realization"][
        "velocity"
    ]["execution_resolution"]
    assert execution["requested_value"] == 0.61
    assert execution["resolved_value"] == pytest.approx(expected)
    assert execution["adapted"] is True
    assert execution["fidelity"] == "adapted"
    assert execution["semantic_fidelity"] == "approximated"


def test_exact_gate_rejects_the_runtime_minimum_duration_clamp(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note["beat"] = 1.0
    note["duration_beats"] = 0.01
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "gate_ratio": {
                    "strategy": "lock",
                    "value": 0.5,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="requested exact sample timing"):
        _build_with_realization(raw_score, score, roster, document)

    document["note_overrides"][0]["gate_ratio"]["value_policy"] = "adapt"
    plan = _build_with_realization(raw_score, score, roster, document)
    note_off = _events(plan, "note_off")[0]
    assert note_off["time"] == pytest.approx(0.02)
    scheduler = plan.parts[0].trace[0]["推导"]["realization"]["scheduler"]
    assert scheduler["requested_gate_seconds"] == pytest.approx(0.005)
    assert scheduler["resolved_gate_seconds"] == pytest.approx(0.02)
    assert scheduler["effective_gate_ratio"] == pytest.approx(2.0)
    assert scheduler["gate_resolution"]["adapted"] is True


def test_exact_gate_compares_duration_after_both_endpoints_are_quantized(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note["beat"] = 1.0000625
    note["duration_beats"] = 0.0200625
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "gate_ratio": {
                    "strategy": "lock",
                    "value": 1.0,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="requested exact sample timing"):
        _build_with_realization(raw_score, score, roster, document)

    document["note_overrides"][0]["gate_ratio"]["value_policy"] = "adapt"
    plan = _build_with_realization(raw_score, score, roster, document)
    scheduler = plan.parts[0].trace[0]["推导"]["realization"]["scheduler"]
    assert scheduler["gate_resolution"]["adapted"] is True
    assert scheduler["effective_gate_ratio"] != pytest.approx(1.0)


def test_timing_boundary_requires_explicit_adaptation(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note["beat"] = 1.0
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                "timing_offset_ms": {
                    "strategy": "lock",
                    "value": -100.0,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="timeline boundary or sample grid"):
        _build_with_realization(raw_score, score, roster, document)

    document["note_overrides"][0]["timing_offset_ms"][
        "value_policy"
    ] = "adapt"
    plan = _build_with_realization(raw_score, score, roster, document)
    note_on = _events(plan, "note_on")[0]
    assert note_on["time"] == 0.0
    timing = plan.parts[0].trace[0]["推导"]["realization"]["scheduler"][
        "timing_resolution"
    ]
    assert timing == {
        "value_policy": "adapt",
        "requested_seconds": pytest.approx(-0.1),
        "boundary_resolved_seconds": 0.0,
        "scheduled_before_grid_seconds": 0.0,
        "resolved_seconds": 0.0,
        "boundary_adapted": True,
        "grid_adapted": False,
        "adapted": True,
    }


@pytest.mark.parametrize(
    ("parameter", "value"),
    (("timing_offset_ms", 60_000.0), ("gate_ratio", 16.0)),
)
def test_realization_cannot_extend_the_resolved_plan_past_its_time_budget(
    catalog: dict[str, InstrumentCapability],
    parameter: str,
    value: float,
) -> None:
    raw_score = _score_document()
    note = raw_score["parts"][0]["notes"][0]
    note.update({"bar": 1800, "beat": 4.0, "duration_beats": 0.125})
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        note_overrides=[
            {
                "event_id": "note-1",
                parameter: {
                    "strategy": "lock",
                    "value": value,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        ],
    )

    with pytest.raises(ResourceLimitError) as raised:
        _build_with_realization(raw_score, score, roster, document)

    assert raised.value.code == "plan.note_time_too_late"
    assert raised.value.actual > raised.value.limit


def test_control_lane_and_tail_cannot_extend_the_final_plan_past_its_budget(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    raw_score["tail_seconds"] = 10.0
    score, roster = _parse_context(raw_score, catalog)
    document = _realization_document(
        raw_score,
        control_lanes=[
            _lane(
                control="sustain_pedal",
                value=1.0,
                beat=1.0,
            )
        ],
    )
    document["control_lanes"][0]["points"][0]["bar"] = 1801

    with pytest.raises(ResourceLimitError) as raised:
        _build_with_realization(raw_score, score, roster, document)

    assert raised.value.code == "plan.duration_too_long"
    assert raised.value.actual == pytest.approx(7210.0)
    assert raised.value.limit == 7200


def test_plan_budget_rejects_trace_growth_before_whole_plan_serialization(
    catalog: dict[str, InstrumentCapability],
) -> None:
    raw_score = _score_document()
    template = raw_score["parts"][0]["notes"][0]
    raw_score["parts"][0]["notes"] = [
        {
            **template,
            "event_id": f"note-{index:04d}",
            "pitch": 48 + (index % 24),
        }
        for index in range(100)
    ]
    score, roster = _parse_context(raw_score, catalog)
    tiny_limits = ProjectLimits(max_plan_json_bytes=8 * 1024)

    with (
        patch(
            "tianlai.conductor.ProjectLimits.from_environment",
            return_value=tiny_limits,
        ),
        patch.object(
            PerformancePlan,
            "to_dict",
            side_effect=AssertionError("whole plan was serialized first"),
        ),
        pytest.raises(ResourceLimitError) as raised,
    ):
        build_plan(score, roster, _strict_expression())

    assert raised.value.code == "plan.document_too_large"
    assert raised.value.actual > raised.value.limit
