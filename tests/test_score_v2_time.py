from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import os
import pickle
import subprocess
import sys

import pytest

import tianlai.score_v2_time as score_v2_time_module
from tianlai.canonical_json import canonical_json_bytes
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2 import Rational, ScorePosition
from tianlai.score_v2_time import (
    DEFAULT_MAX_TIME_INDEX_JSON_BYTES,
    MAX_TIME_INDEX_JSON_BYTES,
    MAX_SAMPLE_RATE,
    MIN_SAMPLE_RATE,
    SAMPLE_ROUNDING_MODE,
    ExactFraction,
    ScoreV2TimeCompileError,
    ScoreV2TimeLimits,
    compile_score_v2_time,
)


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _note(
    event_id: str,
    *,
    measure_id: str,
    offset: tuple[int, int],
    duration: tuple[int, int],
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "position": {
            "measure_id": measure_id,
            "offset_quarters": _r(*offset),
        },
        "duration_quarters": _r(*duration),
        "written_pitch": {
            "step": "C",
            "alter": _r(0),
            "octave": 4,
        },
        "sounding_pitch": {"midi_note": _r(60)},
    }


def _score() -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "exact time",
        "timeline": {
            "measures": [
                {
                    "measure_id": "m1",
                    "actual_duration_quarters": _r(4),
                },
                {
                    "measure_id": "m2",
                    "actual_duration_quarters": _r(4),
                },
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
                },
                {
                    "meter_id": "meter-2",
                    "at": {
                        "measure_id": "m2",
                        "offset_quarters": _r(0),
                    },
                    "groups": [2, 2],
                    "beat_unit": 4,
                },
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-120",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(120),
                },
                {
                    "tempo_id": "tempo-60",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(1, 7),
                    },
                    "quarter_bpm": _r(60),
                },
            ],
        },
        "tuning": {
            "tuning_id": "a440",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(69),
            "reference_frequency_hz": _r(440),
        },
        "parts": [
            {
                "part_id": "part-1",
                "notes": [
                    _note(
                        "one-seventh",
                        measure_id="m1",
                        offset=(0, 1),
                        duration=(1, 7),
                    ),
                    _note(
                        "cross-measure",
                        measure_id="m1",
                        offset=(7, 2),
                        duration=(1, 1),
                    ),
                ],
            }
        ],
        "form": {"mode": "linear"},
    }


def _snapshot(document: dict[str, object] | None = None):
    return snapshot_score_document(document or _score())


def test_exact_mid_measure_tempo_integration_and_cross_measure_note() -> None:
    snapshot = _snapshot()
    index = compile_score_v2_time(snapshot, sample_rate=8_000)

    assert index.source_document_sha256 == snapshot.document_sha256
    serialized = canonical_json_bytes(index.to_dict())
    assert index.artifact_sha256 == hashlib.sha256(serialized).hexdigest()
    assert index.canonical_json_bytes_size == len(serialized)
    assert index.to_dict()["source_identity_contract"] == "stable-event-v2"
    assert index.to_dict()["source_time_contract"] == (
        "rational-measure-offset-v2"
    )
    assert index.tempo_segments[0].start.quarter == 0
    assert index.tempo_segments[0].end.quarter == Fraction(1, 7)
    assert index.tempo_segments[0].end.seconds == Fraction(1, 14)
    assert index.tempo_segments[1].seconds_per_quarter == 1
    assert index.tempo_events[1].at.seconds == Fraction(1, 14)
    assert index.meter_events[1].at.quarter == 4
    assert index.meter_events[1].at.seconds == Fraction(55, 14)
    assert index.score_duration.quarter == 8
    assert index.score_duration.seconds == Fraction(111, 14)

    first, crossing = index.notes
    assert first.end.quarter == Fraction(1, 7)
    assert first.end.seconds == Fraction(1, 14)
    assert crossing.start.quarter == Fraction(7, 2)
    assert crossing.start.seconds == Fraction(24, 7)
    assert crossing.end.quarter == Fraction(9, 2)
    assert crossing.end.seconds == Fraction(31, 7)

    m2 = index.resolve_position(ScorePosition("m2", Rational(0)))
    assert m2.quarter == 4
    assert m2.seconds == Fraction(55, 14)
    score_end = index.resolve_position(ScorePosition("m2", Rational(4)))
    assert score_end == index.score_duration


def test_sample_resolution_carries_requested_resolved_and_fidelity_evidence() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)
    evidence = index.notes[0].end.sample

    assert evidence.requested_seconds == Fraction(1, 14)
    assert evidence.requested_sample == Fraction(4_000, 7)
    assert evidence.resolved_sample == 571
    assert evidence.resolved_seconds == Fraction(571, 8_000)
    assert evidence.error_seconds == Fraction(-3, 56_000)
    assert evidence.fidelity == "rounded"
    assert evidence.rounding_mode == SAMPLE_ROUNDING_MODE

    origin = index.tempo_events[0].at.sample
    assert origin.requested_sample == 0
    assert origin.resolved_sample == 0
    assert origin.error_seconds == 0
    assert origin.fidelity == "exact"

    serialized = evidence.to_dict()
    assert serialized["requested_seconds"] == {
        "numerator": "1",
        "denominator": "14",
    }


def test_nearest_ties_to_even_is_exact_and_does_not_use_float() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)

    half = index.resolve_position(ScorePosition("m1", Rational(1, 8_000)))
    one_and_half = index.resolve_position(
        ScorePosition("m1", Rational(3, 8_000))
    )
    assert half.seconds == Fraction(1, 16_000)
    assert half.sample.requested_sample == Fraction(1, 2)
    assert half.sample.resolved_sample == 0
    assert one_and_half.sample.requested_sample == Fraction(3, 2)
    assert one_and_half.sample.resolved_sample == 2

    two_and_half = index.resolve_position(
        ScorePosition("m1", Rational(1, 1_600))
    )
    three_and_half = index.resolve_position(
        ScorePosition("m1", Rational(7, 8_000))
    )
    assert two_and_half.sample.requested_sample == Fraction(5, 2)
    assert two_and_half.sample.resolved_sample == 2
    assert two_and_half.sample.error_seconds == Fraction(-1, 16_000)
    assert three_and_half.sample.requested_sample == Fraction(7, 2)
    assert three_and_half.sample.resolved_sample == 4
    assert three_and_half.sample.error_seconds == Fraction(1, 16_000)


def test_same_sample_uses_exact_time_before_event_type_priority() -> None:
    document = _score()
    part = document["parts"][0]  # type: ignore[index]
    part["notes"] = [  # type: ignore[index]
        _note(
            "subsample-note",
            measure_id="m1",
            offset=(0, 1),
            duration=(1, 16_000),
        )
    ]
    index = compile_score_v2_time(_snapshot(document), sample_rate=8_000)

    boundaries = [
        event
        for event in index.events
        if event.subject_id == "subsample-note"
    ]
    assert [event.kind for event in boundaries] == ["note_start", "note_end"]
    assert boundaries[0].at.sample.resolved_sample == 0
    assert boundaries[1].at.sample.resolved_sample == 0
    assert boundaries[0].at.seconds < boundaries[1].at.seconds
    origin = [event.kind for event in index.events if event.at.seconds == 0]
    assert origin == ["meter", "tempo", "note_start"]


def test_exact_same_instant_orders_end_before_start_and_streams_stably() -> None:
    document = _score()
    part = document["parts"][0]  # type: ignore[index]
    part["notes"] = [  # type: ignore[index]
        _note("a", measure_id="m1", offset=(0, 1), duration=(1, 7)),
        _note("b", measure_id="m1", offset=(1, 7), duration=(1, 7)),
        _note("c", measure_id="m1", offset=(1, 7), duration=(1, 7)),
    ]
    index = compile_score_v2_time(_snapshot(document), sample_rate=8_000)
    collision = [
        event
        for event in index.events
        if event.at.seconds == Fraction(1, 14)
        and event.kind in ("note_end", "note_start")
    ]
    assert [(event.kind, event.subject_id) for event in collision] == [
        ("note_end", "a"),
        ("note_start", "b"),
        ("note_start", "c"),
    ]


def test_measure_boundary_orders_meter_tempo_end_start_then_source() -> None:
    document = _score()
    timeline = document["timeline"]  # type: ignore[index]
    timeline["tempo_events"].append(  # type: ignore[union-attr]
        {
            "tempo_id": "tempo-at-m2",
            "at": {"measure_id": "m2", "offset_quarters": _r(0)},
            "quarter_bpm": _r(90),
        }
    )
    part = document["parts"][0]  # type: ignore[index]
    part["notes"] = [  # type: ignore[index]
        _note("ending", measure_id="m1", offset=(3, 1), duration=(1, 1)),
        _note("starting-a", measure_id="m2", offset=(0, 1), duration=(1, 1)),
        _note("starting-b", measure_id="m2", offset=(0, 1), duration=(1, 1)),
    ]
    index = compile_score_v2_time(_snapshot(document), sample_rate=8_000)
    boundary = [event for event in index.events if event.at.quarter == 4]
    assert [(event.kind, event.subject_id) for event in boundary] == [
        ("meter", "meter-2"),
        ("tempo", "tempo-at-m2"),
        ("note_end", "ending"),
        ("note_start", "starting-a"),
        ("note_start", "starting-b"),
    ]


def test_snapshot_boundary_ignores_mutated_detached_typed_graph() -> None:
    snapshot = _snapshot()
    detached = snapshot.score
    note = detached.parts[0].notes[0]  # type: ignore[union-attr]
    object.__setattr__(note, "duration_quarters", Rational(2, 7))

    index = compile_score_v2_time(snapshot, sample_rate=8_000)
    assert index.notes[0].end.quarter == Fraction(1, 7)

    with pytest.raises(ScoreV2TimeCompileError, match="untrusted_source"):
        compile_score_v2_time(detached, sample_rate=8_000)  # type: ignore[arg-type]


def test_snapshot_source_hash_is_rechecked_and_bound_to_output() -> None:
    snapshot = _snapshot()
    object.__setattr__(snapshot, "document_sha256", "0" * 64)
    with pytest.raises(ScoreV2TimeCompileError, match="identity_mismatch"):
        compile_score_v2_time(snapshot, sample_rate=8_000)

    snapshot = _snapshot()
    object.__setattr__(snapshot, "document", ())
    index = compile_score_v2_time(snapshot, sample_rate=8_000)
    assert index.source_document_sha256 == snapshot.document_sha256


def test_snapshot_generation_is_independent_of_later_environment_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    monkeypatch.setenv("TIANLAI_MAX_NOTES", "1")

    index = compile_score_v2_time(snapshot, sample_rate=8_000)

    assert len(index.notes) == 2


def test_exact_fraction_rejects_tuple_fallback_in_both_directions() -> None:
    value = ExactFraction(1, 2)

    for operation in (
        lambda: value < (2, 3),
        lambda: (0, 1) < value,
        lambda: value > (0, 1),
        lambda: (2, 3) > value,
        lambda: value + (3, 4),
        lambda: (3, 4) + value,
    ):
        with pytest.raises(TypeError):
            operation()

    snapshot = _snapshot()
    object.__setattr__(snapshot, "canonical_bytes", b"{}")
    with pytest.raises(ScoreV2TimeCompileError, match="identity_mismatch"):
        compile_score_v2_time(snapshot, sample_rate=8_000)


def test_compiled_evidence_resists_frozen_dataclass_bypass() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)

    detached = index.to_dict()
    detached["notes"] = []
    assert len(index.to_dict()["notes"]) == 2  # type: ignore[arg-type]

    with pytest.raises(AttributeError):
        object.__setattr__(
            index.notes[0].end.sample,
            "resolved_sample",
            999,
        )
    with pytest.raises(AttributeError):
        object.__setattr__(index._tempo_spans[0], "start_seconds", Fraction(9))

    original_notes = index.notes
    object.__setattr__(index, "notes", ())
    with pytest.raises(ScoreV2TimeCompileError, match="index_integrity_mismatch"):
        index.to_dict()
    with pytest.raises(ScoreV2TimeCompileError, match="index_integrity_mismatch"):
        index.resolve_position(ScorePosition("m1", Rational(0)))
    object.__setattr__(index, "notes", original_notes)

    object.__setattr__(index._limits, "max_fraction_bits", 1)
    with pytest.raises(ScoreV2TimeCompileError, match="index_integrity_mismatch"):
        index.to_dict()
    with pytest.raises(ScoreV2TimeCompileError, match="index_integrity_mismatch"):
        index.resolve_position(ScorePosition("m1", Rational(0)))


def test_time_index_canonical_output_has_an_exact_incremental_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary = compile_score_v2_time(_snapshot(), sample_rate=8_000)
    exact_size = len(canonical_json_bytes(ordinary.to_dict()))
    assert exact_size < DEFAULT_MAX_TIME_INDEX_JSON_BYTES
    assert len(
        canonical_json_bytes(
            compile_score_v2_time(
                _snapshot(),
                sample_rate=8_000,
                limits=ScoreV2TimeLimits(max_index_json_bytes=exact_size),
            ).to_dict()
        )
    ) == exact_size
    with pytest.raises(ScoreV2TimeCompileError, match="index_too_large"):
        compile_score_v2_time(
            _snapshot(),
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(max_index_json_bytes=exact_size - 1),
        )

    many = _score()
    part = many["parts"][0]  # type: ignore[index]
    part["notes"] = [  # type: ignore[index]
        _note(f"n-{index}", measure_id="m1", offset=(0, 1), duration=(1, 7))
        for index in range(100)
    ]
    real_encoder = score_v2_time_module.canonical_json_bytes

    def reject_full_materialization(value: object) -> bytes:
        if (
            type(value) is dict
            and value.get("kind") == "tianlai.score-v2-time-index"
            and value.get("notes")
        ):
            raise AssertionError("full oversized index was materialized")
        return real_encoder(value)

    monkeypatch.setattr(
        score_v2_time_module,
        "canonical_json_bytes",
        reject_full_materialization,
    )
    with pytest.raises(ScoreV2TimeCompileError, match="index_too_large"):
        compile_score_v2_time(
            _snapshot(many),
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(max_index_json_bytes=4_096),
        )


def test_v1_snapshot_fails_closed_instead_of_using_float_time() -> None:
    document = {
        "schema_version": 1,
        "title": "v1",
        "sample_rate": 48_000,
        "tail_seconds": 1.0,
        "tuning": {"temperament": "equal", "a4_hz": 440.0},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 120.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "part",
                "name": "Piano",
                "notes": [
                    {
                        "event_id": "note",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 1.0,
                        "pitch": "C4",
                    }
                ],
            }
        ],
    }
    snapshot = snapshot_score_document(document)
    with pytest.raises(ScoreV2TimeCompileError, match="invalid_score_v2_source"):
        compile_score_v2_time(snapshot, sample_rate=48_000)


def _prime_tempo_score() -> dict[str, object]:
    document = _score()
    timeline = document["timeline"]  # type: ignore[index]
    timeline["measures"] = [  # type: ignore[index]
        {"measure_id": f"m{index}", "actual_duration_quarters": _r(1)}
        for index in range(1, 4)
    ]
    timeline["meter_events"] = [  # type: ignore[index]
        {
            "meter_id": "meter",
            "at": {"measure_id": "m1", "offset_quarters": _r(0)},
            "groups": [1],
            "beat_unit": 4,
        }
    ]
    timeline["tempo_events"] = [  # type: ignore[index]
        {
            "tempo_id": f"tempo-{bpm}",
            "at": {
                "measure_id": f"m{index}",
                "offset_quarters": _r(0),
            },
            "quarter_bpm": _r(bpm),
        }
        for index, bpm in enumerate((101, 103, 107), start=1)
    ]
    document["parts"] = [  # type: ignore[index]
        {"part_id": "part", "notes": []}
    ]
    return document


def test_prime_bpm_accumulation_is_bounded_before_fraction_explosion() -> None:
    limits = ScoreV2TimeLimits(max_fraction_bits=16)
    with pytest.raises(ScoreV2TimeCompileError, match="rational_complexity"):
        compile_score_v2_time(
            _snapshot(_prime_tempo_score()),
            sample_rate=8_000,
            limits=limits,
        )


def test_tempo_segment_duration_sample_rate_and_sample_index_limits() -> None:
    snapshot = _snapshot(_prime_tempo_score())
    with pytest.raises(ScoreV2TimeCompileError, match="too_many_tempo"):
        compile_score_v2_time(
            snapshot,
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(max_tempo_segments=2),
        )

    slow = _score()
    timeline = slow["timeline"]  # type: ignore[index]
    timeline["tempo_events"] = [  # type: ignore[index]
        {
            "tempo_id": "extremely-slow",
            "at": {"measure_id": "m1", "offset_quarters": _r(0)},
            "quarter_bpm": _r(1, 1_000_000),
        }
    ]
    with pytest.raises(ScoreV2TimeCompileError, match="duration_too_long"):
        compile_score_v2_time(_snapshot(slow), sample_rate=8_000)

    ordinary = _snapshot()
    for invalid in (MIN_SAMPLE_RATE - 1, MAX_SAMPLE_RATE + 1, 48_000.0, True):
        with pytest.raises(ScoreV2TimeCompileError, match="invalid_sample_rate"):
            compile_score_v2_time(
                ordinary,
                sample_rate=invalid,  # type: ignore[arg-type]
            )

    assert compile_score_v2_time(
        ordinary,
        sample_rate=MAX_SAMPLE_RATE,
    ).sample_rate == MAX_SAMPLE_RATE
    with pytest.raises(ScoreV2TimeCompileError, match="sample_index_overflow"):
        compile_score_v2_time(
            ordinary,
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(max_sample_index=10),
        )


def test_duration_limit_is_inclusive_but_sample_index_limit_is_independent() -> None:
    document = _score()
    timeline = document["timeline"]  # type: ignore[index]
    timeline["measures"] = [  # type: ignore[index]
        {"measure_id": "m1", "actual_duration_quarters": _r(4)}
    ]
    timeline["meter_events"] = [  # type: ignore[index]
        {
            "meter_id": "meter",
            "at": {"measure_id": "m1", "offset_quarters": _r(0)},
            "groups": [4],
            "beat_unit": 4,
        }
    ]
    timeline["tempo_events"] = [  # type: ignore[index]
        {
            "tempo_id": "slow",
            "at": {"measure_id": "m1", "offset_quarters": _r(0)},
            "quarter_bpm": _r(1, 30),
        }
    ]
    document["parts"] = [{"part_id": "part", "notes": []}]
    snapshot = _snapshot(document)
    exact_samples = 7_200 * 8_000
    index = compile_score_v2_time(
        snapshot,
        sample_rate=8_000,
        limits=ScoreV2TimeLimits(
            max_output_seconds=7_200,
            max_sample_index=exact_samples,
        ),
    )
    assert index.score_duration.seconds == 7_200
    assert index.score_duration.sample.resolved_sample == exact_samples
    with pytest.raises(ScoreV2TimeCompileError, match="sample_index_overflow"):
        compile_score_v2_time(
            snapshot,
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(
                max_output_seconds=7_200,
                max_sample_index=exact_samples - 1,
            ),
        )
    with pytest.raises(ScoreV2TimeCompileError, match="duration_too_long"):
        compile_score_v2_time(
            snapshot,
            sample_rate=8_000,
            limits=ScoreV2TimeLimits(max_output_seconds=7_199),
        )


def test_final_tempo_segment_and_score_end_use_logarithmic_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _score()
    timeline = document["timeline"]  # type: ignore[index]
    timeline["tempo_events"].append(  # type: ignore[union-attr]
        {
            "tempo_id": "tempo-final",
            "at": {"measure_id": "m2", "offset_quarters": _r(0)},
            "quarter_bpm": _r(90),
        }
    )
    index = compile_score_v2_time(_snapshot(document), sample_rate=8_000)
    real_bisect = score_v2_time_module.bisect_right
    calls: list[tuple[int, Fraction]] = []

    def recording_bisect(starts: tuple[Fraction, ...], value: Fraction) -> int:
        calls.append((len(starts), value))
        return real_bisect(starts, value)

    monkeypatch.setattr(score_v2_time_module, "bisect_right", recording_bisect)
    end = index.resolve_position(ScorePosition("m2", Rational(4)))
    assert end == index.score_duration
    assert calls == [(3, Fraction(8))]


def test_tempo_event_at_score_end_is_rejected_before_zero_length_segment() -> None:
    document = _score()
    timeline = document["timeline"]  # type: ignore[index]
    timeline["tempo_events"].append(  # type: ignore[union-attr]
        {
            "tempo_id": "tempo-at-end",
            "at": {"measure_id": "m2", "offset_quarters": _r(4)},
            "quarter_bpm": _r(90),
        }
    )
    with pytest.raises(ValueError, match="measure-end"):
        _snapshot(document)


def test_position_queries_fail_closed_on_unknown_and_ambiguous_boundaries() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)
    with pytest.raises(ScoreV2TimeCompileError, match="unknown_measure"):
        index.resolve_position(ScorePosition("missing", Rational(0)))
    with pytest.raises(ScoreV2TimeCompileError, match="position_out_of_range"):
        index.resolve_position(ScorePosition("m1", Rational(5)))
    with pytest.raises(ScoreV2TimeCompileError, match="ambiguous_measure_end"):
        index.resolve_position(ScorePosition("m1", Rational(4)))

    forged = ScorePosition("m1", Rational(0))
    object.__setattr__(forged, "offset_quarters", object())
    with pytest.raises(ScoreV2TimeCompileError, match="invalid_position"):
        index.resolve_position(forged)

    forged = ScorePosition("m1", Rational(0))
    object.__setattr__(forged, "measure_id", "x" * 100_000)
    with pytest.raises(ScoreV2TimeCompileError) as captured:
        index.resolve_position(forged)
    assert captured.value.code == "time.invalid_position"
    assert len(str(captured.value)) < 200


def test_time_index_serialization_is_hash_seed_stable() -> None:
    document_json = json.dumps(_score(), ensure_ascii=True, separators=(",", ":"))
    script = "\n".join(
        (
            "import hashlib, json, os",
            "from tianlai.canonical_json import canonical_json_bytes",
            "from tianlai.score_source import snapshot_score_document",
            "from tianlai.score_v2_time import compile_score_v2_time",
            "raw = json.loads(os.environ['TIANLAI_TIME_TEST_SCORE'])",
            "snapshot = snapshot_score_document(raw)",
            "index = compile_score_v2_time(snapshot, sample_rate=8000)",
            "print(hashlib.sha256(canonical_json_bytes(index.to_dict())).hexdigest())",
        )
    )
    digests: list[str] = []
    for seed in ("1", "8675309"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["TIANLAI_TIME_TEST_SCORE"] = document_json
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1]
    assert digests[0] == (
        "0845390929451151f3d6c9e2887f8cf07714a913fc349690ef91c719376c3cee"
    )
    assert digests[0] == hashlib.sha256(
        canonical_json_bytes(
            compile_score_v2_time(_snapshot(), sample_rate=8_000).to_dict()
        )
    ).hexdigest()


def test_public_exact_values_are_tuple_backed_and_cannot_diverge_from_artifact() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)
    exact = index.notes[0].end.sample.requested_seconds
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(exact, "_numerator", 99)
    assert exact == Fraction(1, 14)
    assert index.to_dict()["notes"][0]["end"]["sample"][
        "requested_seconds"
    ] == {"numerator": "1", "denominator": "14"}


def test_exact_fraction_has_consistent_numeric_and_copy_semantics() -> None:
    half = ExactFraction(1, 2)
    zero = ExactFraction(0)
    assert half == Fraction(1, 2)
    assert not (half != Fraction(1, 2))
    assert half <= Fraction(1, 2)
    assert half >= Fraction(1, 2)
    assert half > 0
    assert not zero
    assert half + half == 1
    assert half * 2 == 1
    assert 2 * half == 1
    assert 1 - half == Fraction(1, 2)
    assert half / 2 == Fraction(1, 4)
    assert copy.copy(half) == half
    assert copy.deepcopy(half) == half
    assert pickle.loads(pickle.dumps(half)) == half
    assert half != (1, 2)
    assert (1, 2) != half
    assert len({half, (1, 2)}) == 2


def test_private_resolver_rationals_are_also_tuple_backed() -> None:
    index = compile_score_v2_time(_snapshot(), sample_rate=8_000)
    private_start = index._tempo_spans[0].start_quarter
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(private_start, "_numerator", 99)
    assert index.resolve_position(ScorePosition("m2", Rational(0))).quarter == 4


def test_limits_are_exact_positive_integer_contracts() -> None:
    for kwargs in (
        {"max_fraction_bits": 0},
        {"max_tempo_segments": True},
        {"max_output_seconds": -1},
        {"max_fraction_bits": 10**9},
        {"max_tempo_segments": 100_001},
        {"max_output_seconds": 10**30},
        {"max_sample_index": MAX_SAMPLE_RATE * MAX_SAMPLE_RATE * MAX_SAMPLE_RATE},
        {"max_index_json_bytes": 0},
        {"max_index_json_bytes": True},
        {"max_index_json_bytes": MAX_TIME_INDEX_JSON_BYTES + 1},
    ):
        with pytest.raises(ValueError):
            ScoreV2TimeLimits(**kwargs)

    forged = ScoreV2TimeLimits()
    object.__setattr__(forged, "max_fraction_bits", 0)
    with pytest.raises(ValueError, match="max_fraction_bits"):
        compile_score_v2_time(
            _snapshot(),
            sample_rate=8_000,
            limits=forged,
        )
