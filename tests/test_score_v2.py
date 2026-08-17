from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import tianlai.score_v2 as score_v2_module
from tianlai.canonical_json import canonical_json_bytes
from tianlai.score_v2 import (
    MAX_RATIONAL_DENOMINATOR,
    MAX_SAFE_INTEGER,
    Rational,
    ScorePosition,
    ScoreV2Document,
    parse_score_v2_document,
    score_render_projection,
    score_render_projection_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score() -> dict:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "精确七连音",
        "timeline": {
            "measures": [
                {
                    "measure_id": "measure-1",
                    "actual_duration_quarters": _r(4),
                },
                {
                    "measure_id": "measure-2",
                    "actual_duration_quarters": _r(7, 2),
                },
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {
                        "measure_id": "measure-1",
                        "offset_quarters": _r(0),
                    },
                    "groups": [2, 2],
                    "beat_unit": 4,
                },
                {
                    "meter_id": "meter-2",
                    "at": {
                        "measure_id": "measure-2",
                        "offset_quarters": _r(0),
                    },
                    "groups": [2, 2, 3],
                    "beat_unit": 8,
                },
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-1",
                    "at": {
                        "measure_id": "measure-1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(120),
                },
                {
                    "tempo_id": "tempo-2",
                    "at": {
                        "measure_id": "measure-2",
                        "offset_quarters": _r(1, 7),
                    },
                    "quarter_bpm": _r(617, 5),
                },
            ],
        },
        "tuning": {
            "tuning_id": "tuning-concert-a",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(69),
            "reference_frequency_hz": _r(442),
        },
        "parts": [
            {
                "part_id": "clarinet-b-flat",
                "name": "B-flat Clarinet",
                "default_dynamic": "mp",
                "notes": [
                    {
                        "event_id": "note-1",
                        "position": {
                            "measure_id": "measure-1",
                            "offset_quarters": _r(0),
                        },
                        "duration_quarters": _r(1, 7),
                        "written_pitch": {
                            "step": "C",
                            "alter": _r(1, 2),
                            "octave": 4,
                            "accidental": "quarter-sharp",
                        },
                        "sounding_pitch": {"midi_note": _r(117, 2)},
                        "dynamic": "mf",
                        "articulations": ["tenuto"],
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "note-2",
                        "position": {
                            "measure_id": "measure-1",
                            "offset_quarters": _r(1, 7),
                        },
                        "duration_quarters": _r(1, 7),
                        "written_pitch": {
                            "step": "C",
                            "alter": _r(1, 2),
                            "octave": 4,
                        },
                        "sounding_pitch": {"midi_note": _r(117, 2)},
                        "staff": 1,
                        "voice": "upper",
                    },
                ],
            }
        ],
        "ties": [
            {
                "tie_id": "tie-1",
                "from_event_id": "note-1",
                "to_event_id": "note-2",
            }
        ],
        "phrases": [
            {
                "phrase_id": "phrase-1",
                "part_id": "clarinet-b-flat",
                "start": {
                    "measure_id": "measure-1",
                    "offset_quarters": _r(0),
                },
                "end": {
                    "measure_id": "measure-1",
                    "offset_quarters": _r(2, 7),
                },
            }
        ],
        "form": {"mode": "linear"},
        "extensions": [
            {
                "namespace": "https://example.invalid/engraving/v1",
                "version": 1,
                "required": False,
                "audible": False,
                "payload": {"page_break": True},
            }
        ],
    }


def _schema() -> dict:
    return json.loads(
        (ROOT / "schemas" / "score-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )


def test_public_schema_is_valid_and_accepts_the_normalized_fixture() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_score())


def test_exact_rational_normalizes_without_losing_a_seventh() -> None:
    assert Rational(2, 14) == Rational(1, 7)
    assert Rational(2.0, 14.0).to_dict() == _r(1, 7)

    parsed = parse_score_v2_document(_score())
    note = parsed.parts[0].notes[0]
    assert note.duration_quarters == Rational(1, 7)
    assert parsed.to_dict() == _score()
    assert parse_score_v2_document(parsed.to_dict()) == parsed


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (True, 1),
        (1.5, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (1, 0),
        (1, -1),
        (MAX_SAFE_INTEGER + 1, 2),
        (MAX_SAFE_INTEGER + 1, MAX_SAFE_INTEGER + 1),
        (0, MAX_RATIONAL_DENOMINATOR + 1),
        (2, MAX_RATIONAL_DENOMINATOR + 1),
    ],
)
def test_rational_rejects_invalid_components_before_reduction(
    numerator: object,
    denominator: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        Rational(numerator, denominator)  # type: ignore[arg-type]


def test_integral_float_schema_values_normalize_to_json_integers() -> None:
    document = _score()
    document["schema_version"] = 2.0
    document["timeline"]["measures"][0]["actual_duration_quarters"] = {
        "numerator": 4.0,
        "denominator": 1.0,
    }
    Draft202012Validator(_schema()).validate(document)

    normalized = parse_score_v2_document(document).to_dict()
    assert normalized["schema_version"] == 2
    assert normalized["timeline"]["measures"][0][
        "actual_duration_quarters"
    ] == _r(4)


def test_written_spelling_and_exact_sounding_pitch_are_independent() -> None:
    note = parse_score_v2_document(_score()).parts[0].notes[0]
    assert note.written_pitch.step == "C"
    assert note.written_pitch.alter == Rational(1, 2)
    assert note.written_pitch.accidental == "quarter-sharp"
    assert note.sounding_pitch.midi_note == Rational(117, 2)


def test_identity_and_time_contracts_are_explicit_capabilities() -> None:
    score = parse_score_v2_document(_score())
    assert score.identity_contract == "stable-event-v2"
    assert score.has_stable_event_identity
    assert score.time_contract == "rational-measure-offset-v2"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["parts"][0]["notes"][1].__setitem__(
            "event_id", "note-1"
        ),
        lambda document: document["ties"][0].__setitem__(
            "to_event_id", "missing-note"
        ),
        lambda document: document["parts"][0]["notes"][0][
            "position"
        ].__setitem__("measure_id", "missing-measure"),
        lambda document: document["timeline"]["tempo_events"][0].__setitem__(
            "tempo_id", document["timeline"]["tempo_events"][1]["tempo_id"]
        ),
    ],
)
def test_duplicate_id_and_dangling_reference_boundaries(mutation) -> None:
    document = _score()
    mutation(document)
    with pytest.raises(ValueError):
        parse_score_v2_document(document)


def test_positions_and_ties_are_validated_on_the_exact_timeline() -> None:
    outside = _score()
    outside["parts"][0]["notes"][0]["position"][
        "offset_quarters"
    ] = _r(29, 7)
    with pytest.raises(ValueError, match="measure|offset|position"):
        parse_score_v2_document(outside)

    gap = _score()
    gap["parts"][0]["notes"][1]["position"][
        "offset_quarters"
    ] = _r(2, 7)
    with pytest.raises(ValueError, match="tie|contiguous"):
        parse_score_v2_document(gap)


def test_optional_inaudible_extension_round_trips_but_is_detached() -> None:
    document = _score()
    parsed = parse_score_v2_document(document)
    document["extensions"][0]["payload"]["page_break"] = False
    assert parsed.to_dict()["extensions"][0]["payload"] == {
        "page_break": True
    }


@pytest.mark.parametrize(("field", "value"), [("required", True), ("audible", True)])
def test_unknown_required_or_audible_extensions_fail_closed(
    field: str,
    value: bool,
) -> None:
    document = _score()
    document["extensions"][0][field] = value
    with pytest.raises(ValueError, match="extension|required|audible"):
        parse_score_v2_document(document)


def test_nonlinear_form_and_unknown_fields_fail_closed() -> None:
    repeated = _score()
    repeated["form"] = {"mode": "repeat", "times": 2}
    with pytest.raises(ValueError, match="form|linear"):
        parse_score_v2_document(repeated)

    unknown = _score()
    unknown["parts"][0]["notes"][0]["raw_cc"] = 74
    with pytest.raises(ValueError, match="unknown|field|raw_cc"):
        parse_score_v2_document(unknown)


def test_render_projection_is_domain_specific_not_document_identity() -> None:
    first_document = _score()
    second_document = copy.deepcopy(first_document)
    second_document["title"] = "不同标题与谱面拼写"
    written = second_document["parts"][0]["notes"][0]["written_pitch"]
    written.update(
        {
            "step": "D",
            "alter": _r(-3, 2),
            "accidental": "other-spelling",
        }
    )
    second_document["extensions"][0]["payload"] = {"systems": 3}

    first = parse_score_v2_document(first_document)
    second = parse_score_v2_document(second_document)
    assert first.to_dict() != second.to_dict()
    assert score_render_projection(first) == score_render_projection(second)
    assert score_render_projection_sha256(first) == score_render_projection_sha256(
        second
    )

    audible = copy.deepcopy(first_document)
    audible["parts"][0]["notes"][0]["dynamic"] = "ff"
    assert score_render_projection_sha256(
        parse_score_v2_document(audible)
    ) != score_render_projection_sha256(first)


def test_public_dataclass_construction_cannot_bypass_nested_validation() -> None:
    with pytest.raises((TypeError, ValueError)):
        ScorePosition("measure-1", object())  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ScoreV2Document(
            kind="tianlai.score",
            schema_version=2,
            title="forged",
            timeline={},
            tuning={},
            parts=(),
        )


def test_non_finite_numbers_cannot_enter_extension_payload() -> None:
    document = _score()
    document["extensions"][0]["payload"] = {"bad": math.nan}
    with pytest.raises(ValueError, match="finite|JSON|payload"):
        parse_score_v2_document(document)


@pytest.mark.parametrize("stream", ["meter_events", "tempo_events"])
def test_meter_and_tempo_streams_must_cover_timeline_origin(stream: str) -> None:
    document = _score()
    document["timeline"][stream][0]["at"]["offset_quarters"] = _r(1)
    with pytest.raises(ValueError, match="begin|first measure|zero offset"):
        parse_score_v2_document(document)


def test_meter_changes_are_barline_only_but_tempo_changes_are_not() -> None:
    meter = _score()
    meter["timeline"]["meter_events"][1]["at"][
        "offset_quarters"
    ] = _r(1, 7)
    with pytest.raises(ValueError, match="meter|boundary|zero offset"):
        parse_score_v2_document(meter)

    # The fixture's second tempo event is deliberately inside measure 2.
    assert parse_score_v2_document(_score()).timeline.tempo_events[1].at == (
        ScorePosition("measure-2", Rational(1, 7))
    )


def test_exactly_equal_event_positions_do_not_bypass_strict_ordering() -> None:
    document = _score()
    document["timeline"]["tempo_events"] = [
        document["timeline"]["tempo_events"][0],
        {
            "tempo_id": "tempo-equivalent-1",
            "at": {
                "measure_id": "measure-1",
                "offset_quarters": _r(1, 7),
            },
            "quarter_bpm": _r(120),
        },
        {
            "tempo_id": "tempo-equivalent-2",
            "at": {
                "measure_id": "measure-1",
                "offset_quarters": _r(2, 14),
            },
            "quarter_bpm": _r(121),
        },
    ]
    with pytest.raises(ValueError, match="increasing position order"):
        parse_score_v2_document(document)


def test_phrase_is_part_owned_and_rejects_ambiguous_barline_spelling() -> None:
    missing_owner = _score()
    del missing_owner["phrases"][0]["part_id"]
    assert not Draft202012Validator(_schema()).is_valid(missing_owner)
    with pytest.raises(ValueError, match="part_id|required"):
        parse_score_v2_document(missing_owner)

    dangling_owner = _score()
    dangling_owner["phrases"][0]["part_id"] = "missing-part"
    with pytest.raises(ValueError, match="part_id|missing"):
        parse_score_v2_document(dangling_owner)

    ambiguous = _score()
    ambiguous["phrases"][0]["end"] = {
        "measure_id": "measure-1",
        "offset_quarters": _r(4),
    }
    with pytest.raises(ValueError, match="ambiguous|measure-end"):
        parse_score_v2_document(ambiguous)

    canonical = _score()
    canonical["phrases"][0]["end"] = {
        "measure_id": "measure-2",
        "offset_quarters": _r(0),
    }
    assert parse_score_v2_document(canonical).phrases[0].part_id == (
        "clarinet-b-flat"
    )

    score_end = _score()
    score_end["phrases"][0]["end"] = {
        "measure_id": "measure-2",
        "offset_quarters": _r(7, 2),
    }
    parse_score_v2_document(score_end)


def test_explicit_v2_ties_may_cross_staff_and_voice() -> None:
    document = _score()
    target = document["parts"][0]["notes"][1]
    target["staff"] = 2
    target["voice"] = "lower"
    parsed = parse_score_v2_document(document)
    assert parsed.ties[0].to_event_id == "note-2"


def test_tie_cycles_cannot_satisfy_exact_forward_contiguity() -> None:
    document = _score()
    document["ties"] = [
        {
            "tie_id": "reverse-tie",
            "from_event_id": "note-2",
            "to_event_id": "note-1",
        }
    ]
    with pytest.raises(ValueError, match="tie|contiguous"):
        parse_score_v2_document(document)


def test_cross_measure_logical_note_is_explicitly_supported() -> None:
    document = _score()
    note = document["parts"][0]["notes"][0]
    note["position"]["offset_quarters"] = _r(7, 2)
    note["duration_quarters"] = _r(1)
    document["parts"][0]["notes"] = [note]
    document["ties"] = []
    parsed = parse_score_v2_document(document)
    assert parsed.parts[0].notes[0].duration_quarters == Rational(1)


def test_extension_payload_budget_is_aggregate_across_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_v2_module, "MAX_EXTENSION_PAYLOAD_NODES", 3)
    document = _score()
    document["extensions"] = [
        {
            "namespace": "https://example.invalid/one",
            "version": 1,
            "required": False,
            "audible": False,
            "payload": [0],
        },
        {
            "namespace": "https://example.invalid/two",
            "version": 1,
            "required": False,
            "audible": False,
            "payload": [0],
        },
    ]
    with pytest.raises(ValueError, match="node bound"):
        parse_score_v2_document(document)


def test_extension_metadata_and_duplicates_fail_before_ambiguous_payloads() -> None:
    required = _score()
    required["extensions"][0]["required"] = True
    required["extensions"][0]["payload"] = object()
    with pytest.raises(ValueError, match="unknown|required|audible"):
        parse_score_v2_document(required)

    duplicate = _score()
    second = copy.deepcopy(duplicate["extensions"][0])
    second["payload"] = {"different": True}
    duplicate["extensions"].append(second)
    with pytest.raises(ValueError, match="duplicate.*extension"):
        parse_score_v2_document(duplicate)


def test_extension_payload_rejects_cycles_depth_and_container_subclasses() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    document = _score()
    document["extensions"][0]["payload"] = cyclic
    with pytest.raises(ValueError, match="cycle"):
        parse_score_v2_document(document)

    too_deep: object = None
    for _ in range(score_v2_module.MAX_EXTENSION_PAYLOAD_DEPTH + 1):
        too_deep = [too_deep]
    document = _score()
    document["extensions"][0]["payload"] = too_deep
    with pytest.raises(ValueError, match="depth"):
        parse_score_v2_document(document)

    class DictSubclass(dict):
        pass

    document = _score()
    document["extensions"][0]["payload"] = DictSubclass(ok=True)
    with pytest.raises(ValueError, match="JSON|payload"):
        parse_score_v2_document(document)


def test_direct_frozen_payload_checks_text_budget_before_canonical_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_v2_module, "MAX_EXTENSION_PAYLOAD_UTF8_BYTES", 10)
    frozen = score_v2_module.FrozenJSONArray(("123456", "abcdef"))

    def unexpected_canonicalization(_value: object) -> bytes:
        raise AssertionError("oversized payload reached canonical materialization")

    monkeypatch.setattr(
        score_v2_module,
        "canonical_json_bytes",
        unexpected_canonicalization,
    )
    with pytest.raises(ValueError, match="UTF-8 size bound"):
        score_v2_module.ScoreExtension(
            namespace="https://example.invalid/direct",
            version=1,
            required=False,
            audible=False,
            payload=frozen,
        )


def test_timeline_denominator_and_cumulative_fraction_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denominator_heavy = _score()
    denominator_heavy["timeline"]["measures"][0][
        "actual_duration_quarters"
    ] = _r(1, 2)
    denominator_heavy["timeline"]["measures"][1][
        "actual_duration_quarters"
    ] = _r(1, 3)
    monkeypatch.setattr(
        score_v2_module,
        "MAX_TIMELINE_COMMON_DENOMINATOR_BITS",
        2,
    )
    with pytest.raises(ValueError, match="rational complexity"):
        parse_score_v2_document(denominator_heavy)

    monkeypatch.setattr(
        score_v2_module,
        "MAX_TIMELINE_COMMON_DENOMINATOR_BITS",
        4_096,
    )
    monkeypatch.setattr(
        score_v2_module,
        "MAX_TIMELINE_CUMULATIVE_POSITION_BITS",
        2,
    )
    with pytest.raises(ValueError, match="rational complexity"):
        parse_score_v2_document(_score())


def test_aggregate_articulation_limit_is_preflighted_before_domain_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_v2_module, "MAX_ARTICULATIONS", 1)
    document = _score()
    document["parts"][0]["notes"][1]["articulations"] = ["legato"]

    def timeline_must_not_be_materialized(*_args, **_kwargs):
        raise AssertionError("timeline parsed before articulation preflight")

    monkeypatch.setattr(
        score_v2_module,
        "_parse_timeline",
        timeline_must_not_be_materialized,
    )
    with pytest.raises(ValueError, match="aggregate supported bound"):
        parse_score_v2_document(document)


def test_aggregate_meter_group_limit_is_preflighted_before_measures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(score_v2_module, "MAX_METER_GROUPS", 4)
    document = _score()

    def measure_must_not_be_materialized(*_args, **_kwargs):
        raise AssertionError("measure parsed before meter-group preflight")

    monkeypatch.setattr(
        score_v2_module,
        "_parse_measure",
        measure_must_not_be_materialized,
    )
    with pytest.raises(ValueError, match="meter groups.*aggregate"):
        parse_score_v2_document(document)


def test_schema_and_parser_agree_on_string_and_integer_boundaries() -> None:
    validator = Draft202012Validator(_schema())
    astral = chr(0x1F600)

    accepted_title = _score()
    accepted_title["title"] = astral * score_v2_module.MAX_TEXT_CHARACTERS
    assert validator.is_valid(accepted_title)
    validator.validate(parse_score_v2_document(accepted_title).to_dict())

    rejected_title = _score()
    rejected_title["title"] = "x" * (
        score_v2_module.MAX_TEXT_CHARACTERS + 1
    )
    assert not validator.is_valid(rejected_title)
    with pytest.raises(ValueError, match="character bound"):
        parse_score_v2_document(rejected_title)

    accepted_id = _score()
    long_id = astral * score_v2_module.MAX_ID_CHARACTERS
    accepted_id["parts"][0]["part_id"] = long_id
    accepted_id["phrases"][0]["part_id"] = long_id
    assert validator.is_valid(accepted_id)
    validator.validate(parse_score_v2_document(accepted_id).to_dict())

    surrogate_id = _score()
    surrogate_id["parts"][0]["part_id"] = "bad\ud800id"
    surrogate_id["phrases"][0]["part_id"] = "bad\ud800id"
    assert not validator.is_valid(surrogate_id)
    with pytest.raises(ValueError, match="Unicode"):
        parse_score_v2_document(surrogate_id)

    oversized_staff = _score()
    oversized_staff["parts"][0]["notes"][0]["staff"] = MAX_SAFE_INTEGER + 1
    oversized_staff["parts"][0]["notes"][1]["staff"] = MAX_SAFE_INTEGER + 1
    assert not validator.is_valid(oversized_staff)
    with pytest.raises(ValueError, match="safe-integer"):
        parse_score_v2_document(oversized_staff)


def test_schema_and_parser_agree_on_extension_json_container_bounds() -> None:
    validator = Draft202012Validator(_schema())

    invalid_object = _score()
    invalid_object["extensions"][0]["payload"] = object()
    assert not validator.is_valid(invalid_object)
    with pytest.raises(ValueError, match="JSON|payload"):
        parse_score_v2_document(invalid_object)

    invalid_integer = _score()
    invalid_integer["extensions"][0]["payload"] = MAX_SAFE_INTEGER + 1
    assert not validator.is_valid(invalid_integer)
    with pytest.raises(ValueError, match="safe bound"):
        parse_score_v2_document(invalid_integer)

    invalid_integral_float = _score()
    invalid_integral_float["extensions"][0]["payload"] = float(
        MAX_SAFE_INTEGER + 1
    )
    assert not validator.is_valid(invalid_integral_float)
    with pytest.raises(ValueError, match="safe bound"):
        parse_score_v2_document(invalid_integral_float)

    oversized_array = _score()
    oversized_array["extensions"][0]["payload"] = [None] * (
        score_v2_module.MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS + 1
    )
    assert not validator.is_valid(oversized_array)
    with pytest.raises(ValueError, match="array is too large"):
        parse_score_v2_document(oversized_array)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.pop("title"),
        lambda d: d["parts"][0].__setitem__("default_dynamic", "forte"),
        lambda d: d["parts"][0]["notes"][0][
            "duration_quarters"
        ].__setitem__("numerator", 1.5),
        lambda d: d.__setitem__("form", {"mode": "repeat"}),
        lambda d: d["tuning"].__setitem__("ignored", True),
    ],
)
def test_schema_and_parser_agree_on_required_enum_and_object_shape(
    mutation,
) -> None:
    document = _score()
    mutation(document)
    assert not Draft202012Validator(_schema()).is_valid(document)
    with pytest.raises((TypeError, ValueError)):
        parse_score_v2_document(document)


def test_typed_to_dict_canonicalizes_explicit_empty_optional_arrays() -> None:
    document = _score()
    document["ties"] = []
    document["phrases"] = []
    document["extensions"] = []
    document["parts"][0]["notes"][0]["articulations"] = []
    parsed = parse_score_v2_document(document)
    normalized = parsed.to_dict()
    assert "ties" not in normalized
    assert "phrases" not in normalized
    assert "extensions" not in normalized
    assert "articulations" not in normalized["parts"][0]["notes"][0]
    Draft202012Validator(_schema()).validate(normalized)
    assert parse_score_v2_document(normalized) == parsed


def test_forged_dataclass_subclasses_cannot_cross_validated_boundaries() -> None:
    valid = parse_score_v2_document(_score())

    class ForgedTimeline(score_v2_module.ScoreTimeline):
        def __post_init__(self) -> None:
            pass

    forged_timeline = ForgedTimeline((), (), ())
    with pytest.raises(ValueError, match="ScoreTimeline"):
        ScoreV2Document(
            kind="tianlai.score",
            schema_version=2,
            title="forged",
            timeline=forged_timeline,
            tuning=valid.tuning,
            parts=valid.parts,
        )

    class ForgedDocument(ScoreV2Document):
        def __post_init__(self) -> None:
            pass

    forged_document = ForgedDocument(
        kind="wrong",
        schema_version=1,
        title="forged",
        timeline=valid.timeline,
        tuning=valid.tuning,
        parts=(),
    )
    with pytest.raises(TypeError, match="ScoreV2Document"):
        score_render_projection(forged_document)


def test_parser_rejects_container_subclasses_and_caps_unknown_diagnostics() -> None:
    class DictSubclass(dict):
        pass

    with pytest.raises(ValueError, match="object"):
        parse_score_v2_document(DictSubclass(_score()))

    document = _score()
    for index in range(10_000):
        document[f"unknown-{index}"] = index
    with pytest.raises(ValueError, match="unknown field") as caught:
        parse_score_v2_document(document)
    assert len(str(caught.value)) < 500

    one_huge_key = _score()
    one_huge_key["X" * 100_000] = None
    with pytest.raises(ValueError, match="unknown field") as huge_key_error:
        parse_score_v2_document(one_huge_key)
    assert len(str(huge_key_error.value)) < 500


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "wrong.kind"), ("schema_version", 3)],
)
def test_top_level_discriminators_fail_before_recursive_payloads(
    field: str,
    value: object,
) -> None:
    document = _score()
    cycle: list[object] = []
    cycle.append(cycle)
    document["extensions"][0]["payload"] = cycle
    document[field] = value
    with pytest.raises(ValueError, match=field):
        parse_score_v2_document(document)


def test_projection_hash_uses_exact_domain_formula_and_field_semantics() -> None:
    base_document = _score()
    base = parse_score_v2_document(base_document)
    projection = score_render_projection(base)
    projection_bytes = canonical_json_bytes(projection)
    expected = hashlib.sha256(
        score_v2_module.SCORE_RENDER_PROJECTION_DOMAIN + projection_bytes
    ).hexdigest()
    assert score_render_projection_sha256(base) == expected
    assert expected == (
        "c965404f080961537976f745740e01fb0cb7c064a7b7798977c8e32870ddb721"
    )
    assert expected != hashlib.sha256(projection_bytes).hexdigest()

    omitted_form = copy.deepcopy(base_document)
    del omitted_form["form"]
    assert score_render_projection_sha256(
        parse_score_v2_document(omitted_form)
    ) == score_render_projection_sha256(base)

    presentation = copy.deepcopy(base_document)
    presentation["parts"][0]["name"] = "Presentation only"
    assert score_render_projection_sha256(
        parse_score_v2_document(presentation)
    ) == score_render_projection_sha256(base)

    mutations = [
        lambda d: d["timeline"]["tempo_events"][1].__setitem__(
            "quarter_bpm", _r(618, 5)
        ),
        lambda d: d["tuning"].__setitem__(
            "reference_frequency_hz", _r(440)
        ),
        lambda d: d["parts"][0].__setitem__("default_dynamic", "mf"),
        lambda d: d["parts"][0].__setitem__(
            "default_articulation", "legato"
        ),
        lambda d: d["ties"][0].__setitem__("tie_id", "tie-renamed"),
        lambda d: d["phrases"][0].__setitem__(
            "phrase_id", "phrase-renamed"
        ),
    ]
    for mutation in mutations:
        changed = copy.deepcopy(base_document)
        mutation(changed)
        assert score_render_projection_sha256(
            parse_score_v2_document(changed)
        ) != score_render_projection_sha256(base)
