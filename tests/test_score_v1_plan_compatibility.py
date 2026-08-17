from __future__ import annotations

import hashlib

from tianlai.canonical_json import canonical_json_bytes
from tianlai.capability import InstrumentCapability
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


CAPABILITY = InstrumentCapability(
    name="score-v1 compatibility oscillator",
    relative_path="score-v1-compatibility-oscillator",
    manifest_path="score-v1-compatibility-oscillator/instrument.json",
    implementation_type="oscillator",
    pitched=True,
    note_min=0.0,
    note_max=127.0,
    articulations=("sustain", "staccato"),
    default_articulation="sustain",
    articulation_source="test",
    onset_seconds=None,
    quality_tier="formal",
    license_status="approved",
)


def _score_document() -> dict:
    return {
        "schema_version": 1,
        "title": "frozen score-v1 plan contract",
        "sample_rate": 8_000,
        "tail_seconds": 0.25,
        "tuning": {"temperament": "equal", "a4_hz": 442.0},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 120,
                "beats_per_bar": 4,
                "beat_unit": 4,
            },
            {
                "bar": 2,
                "beat": 1,
                "bpm": 90,
                "beats_per_bar": 3,
                "beat_unit": 8,
            },
            {"bar": 2, "beat": 2, "bpm": 110},
        ],
        "parts": [
            {
                "id": "melody",
                "name": "Melody",
                "default_dynamic": "mp",
                "default_articulation": "sustain",
                "phrases": [
                    {
                        "start_bar": 1,
                        "start_beat": 1,
                        "end_bar": 2,
                        "end_beat": 3,
                    }
                ],
                "notes": [
                    {
                        "event_id": "n-tie-start",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "dynamic": "p",
                        "tie": True,
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "n-tie-stop",
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "n-microtone",
                        "bar": 1,
                        "beat": 3,
                        "duration_beats": 0.5,
                        "pitch": 60.25,
                        "velocity": 0.61,
                        "articulation": "staccato",
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "n-meter-change",
                        "bar": 2,
                        "beat": 1,
                        "duration_beats": 1.5,
                        "pitch": "G4",
                        "dynamic": "f",
                        "staff": 1,
                        "voice": "upper",
                    },
                ],
            }
        ],
    }


def _roster():
    return parse_roster_document(
        {
            "name": "frozen score-v1 roster",
            "assignments": [
                {
                    "part": "melody",
                    "instrument": "score-v1-compatibility-oscillator",
                }
            ],
        },
        {"score-v1-compatibility-oscillator": CAPABILITY},
    )


def _plan_digest(mode: str) -> str:
    settings = ExpressionSettings.from_dict(
        {
            "mode": mode,
            "physical": False,
            "humanize": {
                "depth": 0.0 if mode == "strict" else 1.0,
                "timing_ms": 8.0,
                "velocity": 0.03,
                "seed": 20260816,
            },
        }
    )
    plan = build_plan(
        parse_score_document(_score_document()),
        _roster(),
        settings,
    )
    return hashlib.sha256(canonical_json_bytes(plan.to_dict())).hexdigest()


def test_score_v1_performance_plan_hashes_are_frozen_across_v2_work() -> None:
    assert {
        "strict": _plan_digest("strict"),
        "ensemble": _plan_digest("ensemble"),
    } == {
        "strict": (
            "5c356fb7cb062345d238dfb855f12361"
            "ac53d0c454de8d48e0b20dc530f5559d"
        ),
        "ensemble": (
            "92440dbd7e6afcda555cc3baee83f573"
            "7c02997564487097a40b231ff4c94ab2"
        ),
    }
