from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import tianlai.realization as realization_module
from tianlai.canonical_json import canonical_json_sha256
from tianlai.realization import (
    ControlLane,
    ControlPoint,
    ControlTarget,
    DEFAULTS_PROFILE_V1,
    NoteRealizationOverride,
    NumericOverride,
    REALIZATION_KIND,
    RealizationDocument,
    empty_realization,
    parse_realization_document,
)
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "realization.schema.json"


def _score_document() -> dict:
    return {
        "schema_version": 1,
        "title": "Realization contract",
        "tempo_map": [
            {
                "bar": 1,
                "bpm": 120,
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
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "note-2",
                        "bar": 1,
                        "beat": 3,
                        "duration_beats": 1,
                        "pitch": "C3",
                        "staff": 2,
                        "voice": "lower",
                    },
                ],
            },
            {
                "id": "Empty",
                "notes": [],
            },
        ],
    }


def _score_context() -> tuple[dict, object, str]:
    raw = _score_document()
    return raw, parse_score_document(raw), canonical_json_sha256(raw)


def _empty_document(score_sha256: str | None = None) -> dict:
    return {
        "kind": REALIZATION_KIND,
        "schema_version": 1,
        "score_sha256": score_sha256 or ("a" * 64),
        "defaults_profile": DEFAULTS_PROFILE_V1,
        "mode": "interpreted",
        "note_overrides": [],
        "control_lanes": [],
    }


def _full_document(score_sha256: str) -> dict:
    document = _empty_document(score_sha256)
    document["mode"] = "captured"
    document["note_overrides"] = [
        {
            "event_id": "note-1",
            "timing_offset_ms": {
                "strategy": "add", "value": -12.5,
                "value_policy": "adapt", "semantic_policy": "exact"
            },
            "gate_ratio": {
                "strategy": "scale", "value": 0.84,
                "value_policy": "adapt", "semantic_policy": "exact"
            },
            "velocity": {
                "strategy": "lock", "value": 0.61,
                "value_policy": "adapt", "semantic_policy": "exact"
            },
            "release_velocity": {
                "strategy": "replace", "value": 0.22,
                "value_policy": "adapt", "semantic_policy": "exact"
            },
        },
        {
            "event_id": "note-2",
            "velocity": {"strategy": "auto"},
        },
    ]
    document["control_lanes"] = [
        {
            "lane_id": "piano-expression",
            "target": {"part_id": "Piano"},
            "control": "expression",
            "interpolation": "linear",
            "time_policy": "adapt",
            "value_policy": "adapt",
            "semantic_policy": "exact",
            "points": [
                {"bar": 1, "beat": 2, "value": 0.75},
                {"bar": 1, "beat": 4, "value": 1.0},
            ],
        },
        {
            "lane_id": "upper-pedal",
            "target": {"part_id": "Piano", "voice": "upper"},
            "control": "sustain_pedal",
            "interpolation": "step",
            "time_policy": "adapt",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [
                {"bar": 1, "beat": 1.5, "value": 1.0},
                {"bar": 1, "beat": 3.5, "value": 0.0},
            ],
        },
    ]
    return document


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_empty_realization_is_a_frozen_round_trippable_noop() -> None:
    digest = "b" * 64
    realization = empty_realization(digest)

    assert isinstance(realization, RealizationDocument)
    assert realization.is_noop
    assert realization.to_dict() == _empty_document(digest)
    assert realization.note_overrides == ()
    assert realization.control_lanes == ()
    with pytest.raises(FrozenInstanceError):
        realization.mode = "captured"  # type: ignore[misc]

    captured = empty_realization(digest, mode="captured")
    assert captured.is_noop
    assert captured.mode == "captured"


def test_schema_integer_values_are_normalized_by_parser_and_dataclasses() -> None:
    document = _full_document("a" * 64)
    document["schema_version"] = 1.0
    document["control_lanes"][0]["points"][0]["bar"] = 1.0

    Draft202012Validator(_schema()).validate(document)
    parsed = parse_realization_document(document)

    assert parsed.schema_version == 1
    assert type(parsed.schema_version) is int
    assert parsed.control_lanes[0].points[0].bar == 1
    assert type(parsed.control_lanes[0].points[0].bar) is int
    assert type(parsed.to_dict()["schema_version"]) is int
    assert type(parsed.to_dict()["control_lanes"][0]["points"][0]["bar"]) is int

    point = ControlPoint(bar=1.0, beat=1, value=0.5)  # type: ignore[arg-type]
    realization = RealizationDocument(
        score_sha256="a" * 64,
        defaults_profile=DEFAULTS_PROFILE_V1,
        mode="interpreted",
        schema_version=1.0,  # type: ignore[arg-type]
    )
    assert type(point.bar) is int
    assert type(realization.schema_version) is int


@pytest.mark.parametrize("value", [True, math.nan, math.inf, 1.5])
@pytest.mark.parametrize("field", ["schema_version", "bar"])
def test_schema_parser_and_dataclasses_reject_non_integer_values(
    field: str,
    value,
) -> None:
    document = _full_document("a" * 64)
    if field == "schema_version":
        document[field] = value
    else:
        document["control_lanes"][0]["points"][0][field] = value

    assert list(Draft202012Validator(_schema()).iter_errors(document))
    with pytest.raises(ValueError):
        parse_realization_document(document)

    if field == "schema_version":
        with pytest.raises(ValueError, match="schema_version"):
            RealizationDocument(
                score_sha256="a" * 64,
                defaults_profile=DEFAULTS_PROFILE_V1,
                mode="interpreted",
                schema_version=value,
            )
    else:
        with pytest.raises(ValueError, match="integer starting at 1"):
            ControlPoint(bar=value, beat=1, value=0.5)


def test_public_frozen_dataclasses_cannot_bypass_structural_validation() -> None:
    with pytest.raises(ValueError, match="absent when strategy is auto"):
        NumericOverride("auto", math.nan)
    with pytest.raises(ValueError, match="<= 1"):
        NoteRealizationOverride(
            event_id="note-1",
            velocity=NumericOverride("replace", 2, "exact", "exact"),
        )
    with pytest.raises(ValueError, match="finite"):
        ControlPoint(bar=1, beat=1, value=math.nan)

    target = ControlTarget(part_id="Piano")
    first = ControlPoint(bar=1, beat=2, value=0.5)
    second = ControlPoint(bar=1, beat=1, value=0.75)
    with pytest.raises(ValueError, match="must follow"):
        ControlLane(
            lane_id="lane",
            target=target,
            control="expression",
            interpolation="linear",
            time_policy="adapt",
            value_policy="exact",
            semantic_policy="exact",
            points=(first, second),
        )

    with pytest.raises(ValueError, match="mode"):
        RealizationDocument(
            score_sha256="a" * 64,
            defaults_profile=DEFAULTS_PROFILE_V1,
            mode="unvalidated",
        )
    with pytest.raises(TypeError, match="must be a tuple"):
        RealizationDocument(
            score_sha256="a" * 64,
            defaults_profile=DEFAULTS_PROFILE_V1,
            mode="interpreted",
            note_overrides=[],  # type: ignore[arg-type]
        )


def test_parser_resource_caps_fail_closed_before_unbounded_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realization_module, "MAX_NOTE_OVERRIDES", 1)
    document = _empty_document()
    document["note_overrides"] = [
        {"event_id": "one", "velocity": {"strategy": "auto"}},
        {"event_id": "two", "velocity": {"strategy": "auto"}},
    ]
    with pytest.raises(ValueError, match="note_overrides exceeds 1"):
        parse_realization_document(document)

    monkeypatch.setattr(realization_module, "MAX_NOTE_OVERRIDES", 250_000)
    monkeypatch.setattr(realization_module, "MAX_CONTROL_POINTS_PER_LANE", 1)
    lane = {
        "lane_id": "lane-a",
        "target": {"part_id": "Piano"},
        "control": "expression",
        "interpolation": "step",
        "time_policy": "exact",
        "value_policy": "exact",
        "semantic_policy": "exact",
        "points": [
            {"bar": 1, "beat": 1, "value": 0.5},
            {"bar": 1, "beat": 2, "value": 0.6},
        ],
    }
    document = _empty_document()
    document["control_lanes"] = [lane]
    with pytest.raises(ValueError, match="points exceeds 1"):
        parse_realization_document(document)

    monkeypatch.setattr(
        realization_module,
        "MAX_CONTROL_POINTS_PER_LANE",
        65_536,
    )
    monkeypatch.setattr(realization_module, "MAX_TOTAL_CONTROL_POINTS", 1)
    lane["points"] = [{"bar": 1, "beat": 1, "value": 0.5}]
    second_lane = copy.deepcopy(lane)
    second_lane["lane_id"] = "lane-b"
    second_lane["control"] = "breath"
    document["control_lanes"] = [lane, second_lane]

    monkeypatch.setattr(realization_module, "MAX_CONTROL_LANES", 1)
    with pytest.raises(ValueError, match="control_lanes exceeds 1"):
        parse_realization_document(document)

    monkeypatch.setattr(realization_module, "MAX_CONTROL_LANES", 4_096)
    monkeypatch.setattr(realization_module, "MAX_TOTAL_CONTROL_POINTS", 1)
    with pytest.raises(ValueError, match="point count exceeds 1"):
        parse_realization_document(document)


def test_full_document_parses_with_hash_and_score_reference_validation() -> None:
    raw_score, score, digest = _score_context()
    source = _full_document(digest)

    realization = parse_realization_document(
        source,
        score_document=raw_score,
        score=score,
        expected_score_sha256=digest,
    )

    assert realization.to_dict() == source
    assert not realization.is_noop
    assert realization.note_overrides[0].velocity is not None
    assert realization.note_overrides[0].velocity.strategy == "lock"
    assert realization.note_overrides[1].is_noop
    assert realization.control_lanes[0].value_policy == "adapt"
    assert realization.control_lanes[1].value_policy == "exact"
    assert realization.control_lanes[0].semantic_policy == "exact"
    assert realization.control_lanes[0].points[0].bar == 1
    assert realization.control_lanes[0].points[0].beat == 2.0

    detached = realization.to_dict()
    detached["note_overrides"][0]["velocity"]["value"] = 0.01
    assert realization.note_overrides[0].velocity.value == 0.61


def test_parsed_score_reference_validation_requires_raw_document() -> None:
    _raw_score, score, digest = _score_context()
    with pytest.raises(ValueError, match="requires score_document"):
        parse_realization_document(_empty_document(digest), score=score)


def test_hash_binding_is_strict_and_lowercase() -> None:
    raw_score, score, digest = _score_context()
    with pytest.raises(ValueError, match="does not match"):
        parse_realization_document(
            _empty_document("a" * 64),
            score_document=raw_score,
            score=score,
            expected_score_sha256=digest,
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        parse_realization_document(
            _empty_document(digest.upper()),
            expected_score_sha256=digest,
        )
    with pytest.raises(ValueError, match="expected_score_sha256"):
        parse_realization_document(
            _empty_document(digest),
            expected_score_sha256="short",
        )


def test_raw_score_proves_binding_and_rejects_a_different_parsed_revision() -> None:
    raw_score, _score, digest = _score_context()
    different_raw = copy.deepcopy(raw_score)
    different_raw["title"] = "Different revision with overlapping IDs"
    different_score = parse_score_document(different_raw)

    with pytest.raises(ValueError, match="does not match the parsed score_document"):
        parse_realization_document(
            _empty_document(digest),
            score_document=raw_score,
            score=different_score,
        )

    with pytest.raises(ValueError, match="does not match score_document"):
        parse_realization_document(
            _empty_document(digest),
            score_document=different_raw,
        )

    parsed = parse_realization_document(
        _empty_document(digest),
        score_document=raw_score,
    )
    assert parsed.score_sha256 == digest


def test_binding_uses_one_canonical_snapshot_of_mutable_input() -> None:
    raw_score = _score_document()
    raw_score["parts"][0]["notes"][0]["velocity"] = 0.2
    parsed_before_mutation = parse_score_document(raw_score)
    mutated_score = copy.deepcopy(raw_score)
    mutated_score["parts"][0]["notes"][0]["velocity"] = 0.9
    mutated_digest = canonical_json_sha256(mutated_score)

    class MutatingScoreDocument(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "title":
                self["parts"][0]["notes"][0]["velocity"] = 0.9
            return value

    live_score = MutatingScoreDocument(copy.deepcopy(raw_score))
    with pytest.raises(ValueError, match="does not match score_document"):
        parse_realization_document(
            _empty_document(mutated_digest),
            score_document=live_score,
            score=parsed_before_mutation,
            expected_score_sha256=mutated_digest,
        )


@pytest.mark.parametrize(
    ("parameter", "override"),
    [
        ("timing_offset_ms", {"strategy": "auto"}),
        ("timing_offset_ms", {"strategy": "add", "value": -60_000, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("timing_offset_ms", {"strategy": "scale", "value": 16, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("timing_offset_ms", {"strategy": "replace", "value": 100, "value_policy": "exact", "semantic_policy": "exact"}),
        ("timing_offset_ms", {"strategy": "lock", "value": 0, "value_policy": "exact", "semantic_policy": "exact"}),
        ("gate_ratio", {"strategy": "auto"}),
        ("gate_ratio", {"strategy": "add", "value": -16, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("gate_ratio", {"strategy": "scale", "value": 0.01, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("gate_ratio", {"strategy": "replace", "value": 1, "value_policy": "exact", "semantic_policy": "exact"}),
        ("gate_ratio", {"strategy": "lock", "value": 16, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("velocity", {"strategy": "auto"}),
        ("velocity", {"strategy": "add", "value": -1, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("velocity", {"strategy": "scale", "value": 0, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("velocity", {"strategy": "replace", "value": 0, "value_policy": "exact", "semantic_policy": "exact"}),
        ("velocity", {"strategy": "lock", "value": 1, "value_policy": "exact", "semantic_policy": "exact"}),
        ("release_velocity", {"strategy": "add", "value": 1, "value_policy": "adapt", "semantic_policy": "exact"}),
        ("release_velocity", {"strategy": "scale", "value": 16, "value_policy": "adapt", "semantic_policy": "exact"}),
    ],
)
def test_supported_merge_strategies_and_bounds_are_accepted(
    parameter: str,
    override: dict,
) -> None:
    document = _empty_document()
    document["note_overrides"] = [
        {"event_id": "note-1", parameter: override}
    ]

    parsed = parse_realization_document(document)

    assert parsed.note_overrides[0].to_dict()[parameter] == override


@pytest.mark.parametrize(
    ("parameter", "override", "message"),
    [
        ("velocity", {"strategy": "auto", "value": 0.5}, "must be absent"),
        ("velocity", {"strategy": "lock"}, "value is required"),
        ("velocity", {"strategy": "guess", "value": 0.5}, "one of"),
        ("velocity", {"strategy": "replace", "value": 1.01, "value_policy": "exact", "semantic_policy": "exact"}, "<= 1"),
        ("velocity", {"strategy": "scale", "value": 16.01, "value_policy": "adapt", "semantic_policy": "exact"}, "<= 16"),
        ("gate_ratio", {"strategy": "replace", "value": 0, "value_policy": "exact", "semantic_policy": "exact"}, "> 0"),
        ("gate_ratio", {"strategy": "scale", "value": -0.1, "value_policy": "adapt", "semantic_policy": "exact"}, "> 0"),
        (
            "timing_offset_ms",
            {"strategy": "replace", "value": 60_001, "value_policy": "exact", "semantic_policy": "exact"},
            "<= 60000",
        ),
        ("release_velocity", {"strategy": "lock", "value": math.nan, "value_policy": "adapt", "semantic_policy": "exact"}, "finite"),
        ("release_velocity", {"strategy": "lock", "value": math.inf, "value_policy": "adapt", "semantic_policy": "exact"}, "finite"),
        ("release_velocity", {"strategy": "lock", "value": True, "value_policy": "adapt", "semantic_policy": "exact"}, "finite"),
        ("release_velocity", {"strategy": "lock", "value": "0.5", "value_policy": "adapt", "semantic_policy": "exact"}, "finite"),
    ],
)
def test_invalid_merge_instructions_are_rejected(
    parameter: str,
    override: dict,
    message: str,
) -> None:
    document = _empty_document()
    document["note_overrides"] = [
        {"event_id": "note-1", parameter: override}
    ]

    with pytest.raises(ValueError, match=message):
        parse_realization_document(document)


def test_note_override_requires_a_parameter_and_unique_event_id() -> None:
    document = _empty_document()
    document["note_overrides"] = [{"event_id": "note-1"}]
    with pytest.raises(ValueError, match="at least one override"):
        parse_realization_document(document)

    duplicate = _empty_document()
    duplicate["note_overrides"] = [
        {"event_id": "note-1", "velocity": {"strategy": "auto"}},
        {"event_id": "note-1", "gate_ratio": {"strategy": "auto"}},
    ]
    with pytest.raises(ValueError, match="duplicate event_id"):
        parse_realization_document(duplicate)


def test_event_part_voice_and_empty_part_references_fail_closed() -> None:
    raw_score, score, digest = _score_context()

    unknown_event = _empty_document(digest)
    unknown_event["note_overrides"] = [
        {"event_id": "missing", "velocity": {"strategy": "auto"}}
    ]
    with pytest.raises(ValueError, match="unknown score event"):
        parse_realization_document(
            unknown_event,
            score_document=raw_score,
            score=score,
            expected_score_sha256=digest,
        )

    for part_id, voice, message in (
        ("Missing", None, "unknown score part"),
        ("Piano", "missing", "references no note"),
        ("Empty", None, "no notes"),
    ):
        document = _empty_document(digest)
        target = {"part_id": part_id}
        if voice is not None:
            target["voice"] = voice
        document["control_lanes"] = [
            {
                "lane_id": "lane",
                "target": target,
                "control": "expression",
                "interpolation": "step",
                "time_policy": "exact",
                "value_policy": "exact",
                "semantic_policy": "exact",
                "points": [{"bar": 1, "beat": 1, "value": 1}],
            }
        ]
        with pytest.raises(ValueError, match=message):
            parse_realization_document(
                document,
                score_document=raw_score,
                score=score,
                expected_score_sha256=digest,
            )


def test_tie_continuation_override_is_rejected_but_chain_head_is_valid() -> None:
    raw_score = _score_document()
    raw_score["parts"][0]["notes"] = [
        {
            "event_id": "tie-head",
            "bar": 1,
            "beat": 1,
            "duration_beats": 1,
            "pitch": "C4",
            "staff": 1,
            "voice": "upper",
            "tie": True,
        },
        {
            "event_id": "tie-continuation",
            "bar": 1,
            "beat": 2,
            "duration_beats": 1,
            "pitch": "C4",
            "staff": 1,
            "voice": "upper",
        },
    ]
    digest = canonical_json_sha256(raw_score)

    head = _empty_document(digest)
    head["note_overrides"] = [
        {
            "event_id": "tie-head",
            "velocity": {
                "strategy": "replace",
                "value": 0.7,
                "value_policy": "exact",
                "semantic_policy": "exact",
            },
        }
    ]
    assert parse_realization_document(
        head,
        score_document=raw_score,
    ).note_overrides[0].event_id == "tie-head"

    continuation = copy.deepcopy(head)
    continuation["note_overrides"][0]["event_id"] = "tie-continuation"
    with pytest.raises(
        ValueError,
        match="tie continuation.*tie-chain head event_id 'tie-head'",
    ):
        parse_realization_document(
            continuation,
            score_document=raw_score,
        )


def test_noncontiguous_note_after_tie_marker_is_not_a_continuation() -> None:
    raw_score = _score_document()
    raw_score["parts"][0]["notes"] = [
        {
            "event_id": "tie-head",
            "bar": 1,
            "beat": 1,
            "duration_beats": 1,
            "pitch": "C4",
            "tie": True,
        },
        {
            "event_id": "later-note",
            "bar": 1,
            "beat": 3,
            "duration_beats": 1,
            "pitch": "C4",
        },
    ]
    digest = canonical_json_sha256(raw_score)
    document = _empty_document(digest)
    document["note_overrides"] = [
        {"event_id": "later-note", "velocity": {"strategy": "auto"}}
    ]

    assert parse_realization_document(
        document,
        score_document=raw_score,
    ).note_overrides[0].event_id == "later-note"


def test_realization_references_require_score_v1() -> None:
    raw = _score_document()
    raw.pop("schema_version")
    for part in raw["parts"]:
        for note in part["notes"]:
            note.pop("event_id")
    legacy = parse_score_document(raw)
    digest = canonical_json_sha256(raw)

    with pytest.raises(ValueError, match="score.schema_version 1"):
        parse_realization_document(
            _empty_document(digest),
            score_document=raw,
            score=legacy,
            expected_score_sha256=digest,
        )


def test_sparse_lane_may_start_late_but_points_must_be_ordered() -> None:
    raw_score, score, digest = _score_context()
    document = _empty_document(digest)
    document["control_lanes"] = [
        {
            "lane_id": "late-breath",
            "target": {"part_id": "Piano"},
            "control": "breath",
            "interpolation": "linear",
            "time_policy": "adapt",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [
                {"bar": 1, "beat": 2.5, "value": 0.4},
                {"bar": 1, "beat": 4.5, "value": 0.8},
            ],
        }
    ]
    parsed = parse_realization_document(
        document,
        score_document=raw_score,
        score=score,
        expected_score_sha256=digest,
    )
    assert parsed.control_lanes[0].points[0].beat == 2.5

    for points in (
        [
            {"bar": 1, "beat": 2, "value": 0.4},
            {"bar": 1, "beat": 2, "value": 0.8},
        ],
        [
            {"bar": 1, "beat": 3, "value": 0.4},
            {"bar": 1, "beat": 2, "value": 0.8},
        ],
    ):
        invalid = copy.deepcopy(document)
        invalid["control_lanes"][0]["points"] = points
        with pytest.raises(ValueError, match="must follow"):
            parse_realization_document(invalid)


def test_lane_points_are_checked_against_score_meter_when_bound() -> None:
    raw_score, score, digest = _score_context()
    document = _empty_document(digest)
    document["control_lanes"] = [
        {
            "lane_id": "invalid-position",
            "target": {"part_id": "Piano"},
            "control": "expression",
            "interpolation": "step",
            "time_policy": "exact",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [{"bar": 1, "beat": 5, "value": 0.5}],
        }
    ]

    # Shape-only parsing cannot know the score's meter.
    assert parse_realization_document(document).control_lanes
    with pytest.raises(ValueError, match="outside"):
        parse_realization_document(
            document,
            score_document=raw_score,
            score=score,
            expected_score_sha256=digest,
        )


def test_bound_lane_position_must_resolve_to_finite_score_time() -> None:
    raw_score, _score, digest = _score_context()
    document = _empty_document(digest)
    document["control_lanes"] = [
        {
            "lane_id": "unrepresentable-position",
            "target": {"part_id": "Piano"},
            "control": "expression",
            "interpolation": "step",
            "time_policy": "exact",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [{"bar": 10**400, "beat": 1, "value": 0.5}],
        }
    ]

    with pytest.raises(ValueError, match="finite score-time range"):
        parse_realization_document(
            document,
            score_document=raw_score,
        )


def test_lane_identity_and_target_control_are_unambiguous() -> None:
    base_lane = {
        "lane_id": "lane-a",
        "target": {"part_id": "Piano"},
        "control": "expression",
        "interpolation": "step",
        "time_policy": "exact",
        "value_policy": "exact",
        "semantic_policy": "exact",
        "points": [{"bar": 1, "beat": 1, "value": 1}],
    }
    duplicate_id = _empty_document()
    other = copy.deepcopy(base_lane)
    other["control"] = "breath"
    duplicate_id["control_lanes"] = [base_lane, other]
    with pytest.raises(ValueError, match="duplicate lane_id"):
        parse_realization_document(duplicate_id)

    competing = _empty_document()
    other = copy.deepcopy(base_lane)
    other["lane_id"] = "lane-b"
    competing["control_lanes"] = [base_lane, other]
    with pytest.raises(ValueError, match="competing lanes"):
        parse_realization_document(competing)


def test_lane_value_policy_requires_explicit_quantization_consent() -> None:
    lane = {
        "lane_id": "lane",
        "target": {"part_id": "Piano"},
        "control": "expression",
        "interpolation": "linear",
        "time_policy": "adapt",
        "value_policy": "exact",
        "semantic_policy": "exact",
        "points": [{"bar": 2, "beat": 1, "value": 0.501}],
    }
    for policy in ("exact", "adapt"):
        document = _empty_document()
        document["control_lanes"] = [{**lane, "value_policy": policy}]
        parsed = parse_realization_document(document)
        assert parsed.control_lanes[0].value_policy == policy

    missing = _empty_document()
    lane_without_policy = copy.deepcopy(lane)
    lane_without_policy.pop("value_policy")
    missing["control_lanes"] = [lane_without_policy]
    with pytest.raises(ValueError, match="value_policy"):
        parse_realization_document(missing)

    unknown = _empty_document()
    unknown["control_lanes"] = [{**lane, "value_policy": "nearest"}]
    with pytest.raises(ValueError, match="one of"):
        parse_realization_document(unknown)


@pytest.mark.parametrize("value_policy", ("exact", "adapt"))
@pytest.mark.parametrize("semantic_policy", ("exact", "approximate"))
def test_lane_semantic_and_value_fidelity_policies_are_orthogonal(
    value_policy: str,
    semantic_policy: str,
) -> None:
    document = _empty_document()
    document["control_lanes"] = [
        {
            "lane_id": "piano-soft-pedal",
            "target": {"part_id": "Piano"},
            "control": "una_corda",
            "interpolation": "step",
            "time_policy": "exact",
            "value_policy": value_policy,
            "semantic_policy": semantic_policy,
            "points": [{"bar": 1, "beat": 1, "value": 1}],
        }
    ]

    parsed = parse_realization_document(document)
    lane = parsed.control_lanes[0]
    assert lane.value_policy == value_policy
    assert lane.semantic_policy == semantic_policy


def test_lane_semantic_policy_requires_explicit_approximation_consent() -> None:
    document = _empty_document()
    lane = {
        "lane_id": "piano-soft-pedal",
        "target": {"part_id": "Piano"},
        "control": "una_corda",
        "interpolation": "step",
        "time_policy": "exact",
        "value_policy": "exact",
        "semantic_policy": "exact",
        "points": [{"bar": 1, "beat": 1, "value": 1}],
    }
    lane_without_policy = copy.deepcopy(lane)
    lane_without_policy.pop("semantic_policy")
    document["control_lanes"] = [lane_without_policy]
    with pytest.raises(ValueError, match="semantic_policy"):
        parse_realization_document(document)

    document["control_lanes"] = [{**lane, "semantic_policy": "emulate"}]
    with pytest.raises(ValueError, match="one of"):
        parse_realization_document(document)


def test_backend_ambiguous_generic_modulation_is_not_a_v1_control() -> None:
    document = _empty_document()
    document["control_lanes"] = [
        {
            "lane_id": "ambiguous-modulation",
            "target": {"part_id": "Piano"},
            "control": "modulation",
            "interpolation": "step",
            "time_policy": "exact",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [{"bar": 1, "beat": 1, "value": 0.5}],
        }
    ]

    with pytest.raises(ValueError, match="control must be one of"):
        parse_realization_document(document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bar", True, "integer starting at 1"),
        ("bar", 1.5, "integer starting at 1"),
        ("beat", math.nan, "finite"),
        ("beat", math.inf, "finite"),
        ("beat", 0.99, "at least 1"),
        ("value", math.nan, "finite"),
        ("value", -0.01, ">= 0"),
        ("value", 1.01, "<= 1"),
    ],
)
def test_control_point_numbers_are_finite_and_bounded(
    field: str,
    value,
    message: str,
) -> None:
    document = _empty_document()
    point = {"bar": 1, "beat": 1, "value": 0.5}
    point[field] = value
    document["control_lanes"] = [
        {
            "lane_id": "lane",
            "target": {"part_id": "Piano"},
            "control": "expression",
            "interpolation": "linear",
            "time_policy": "adapt",
            "value_policy": "exact",
            "semantic_policy": "exact",
            "points": [point],
        }
    ]
    with pytest.raises(ValueError, match=message):
        parse_realization_document(document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["note_overrides"][0].update({"unknown": True}),
        lambda value: value["note_overrides"][0]["velocity"].update(
            {"unknown": True}
        ),
        lambda value: value["control_lanes"][0].update({"unknown": True}),
        lambda value: value["control_lanes"][0]["target"].update(
            {"unknown": True}
        ),
        lambda value: value["control_lanes"][0]["points"][0].update(
            {"unknown": True}
        ),
    ],
)
def test_unknown_fields_are_rejected_at_every_level(mutation) -> None:
    document = _full_document("a" * 64)
    mutation(document)
    with pytest.raises(ValueError, match="unknown fields"):
        parse_realization_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "tianlai.other"),
        ("schema_version", True),
        ("schema_version", 2),
        ("defaults_profile", "latest"),
        ("mode", "automatic"),
    ],
)
def test_header_contract_is_versioned_and_fail_closed(field: str, value) -> None:
    document = _empty_document()
    document[field] = value
    with pytest.raises(ValueError):
        parse_realization_document(document)


def test_schema_matches_parser_shape_and_is_valid_draft_2020_12() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid = _full_document("a" * 64)

    validator.validate(valid)

    invalid_documents = []
    unknown = copy.deepcopy(valid)
    unknown["unknown"] = True
    invalid_documents.append(unknown)
    auto_value = copy.deepcopy(valid)
    auto_value["note_overrides"][1]["velocity"]["value"] = 0.5
    invalid_documents.append(auto_value)
    invalid_control = copy.deepcopy(valid)
    invalid_control["control_lanes"][0]["control"] = "modulation"
    invalid_documents.append(invalid_control)
    missing_value_policy = copy.deepcopy(valid)
    missing_value_policy["control_lanes"][0].pop("value_policy")
    invalid_documents.append(missing_value_policy)
    missing_semantic_policy = copy.deepcopy(valid)
    missing_semantic_policy["control_lanes"][0].pop("semantic_policy")
    invalid_documents.append(missing_semantic_policy)
    empty_lane = copy.deepcopy(valid)
    empty_lane["control_lanes"][0]["points"] = []
    invalid_documents.append(empty_lane)

    for invalid in invalid_documents:
        assert list(validator.iter_errors(invalid))
