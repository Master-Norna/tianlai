from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from tianlai.capability import InstrumentCapability
from tianlai.conductor import ExpressionSettings, _unit_random, build_plan
from tianlai.events import parse_performance_document
from tianlai.roster import parse_roster_document
from tianlai.score import (
    parse_score_document,
    upgrade_legacy_score_to_v1,
)


ROOT = Path(__file__).resolve().parents[1]


def _legacy_score() -> dict:
    return {
        "title": "event identity",
        "sample_rate": 48_000,
        "tail_seconds": 0,
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
                "id": "melody",
                "notes": [
                    {
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 0.5,
                        "pitch": "C4",
                        "dynamic": "mf",
                    },
                    {
                        "bar": 1,
                        "beat": 3,
                        "duration_beats": 0.5,
                        "pitch": "E4",
                        "dynamic": "mf",
                    },
                ],
            }
        ],
    }


CAPABILITY = InstrumentCapability(
    name="event identity instrument",
    relative_path="event-identity-instrument",
    manifest_path="event-identity-instrument/乐器.json",
    implementation_type="oscillator",
    pitched=True,
    note_min=0.0,
    note_max=127.0,
    articulations=("sustain",),
    default_articulation="sustain",
    articulation_source="test",
    onset_seconds=None,
    quality_tier="formal",
    license_status="approved",
)


def _roster():
    return parse_roster_document(
        {
            "name": "event identity roster",
            "assignments": [
                {
                    "part": "melody",
                    "instrument": "event-identity-instrument",
                }
            ],
        },
        {"event-identity-instrument": CAPABILITY},
    )


def _expression() -> ExpressionSettings:
    return ExpressionSettings(
        mode="ensemble",
        structural=False,
        physical=False,
        humanize_depth=1.0,
        timing_ms=8.0,
        velocity_spread=0.03,
        seed=20260727,
    )


def _note_on_by_source(plan) -> dict[str, dict]:
    return {
        event["source_event_id"]: event
        for event in plan.parts[0].performance["events"]
        if event["type"] == "note_on"
    }


class LegacyScoreIdentityTests(unittest.TestCase):
    def test_legacy_parse_and_serialization_do_not_grow_identity_fields(self) -> None:
        score = parse_score_document(_legacy_score())

        self.assertIsNone(score.schema_version)
        self.assertEqual([note.index for note in score.parts[0].notes], [0, 1])
        self.assertTrue(
            all(note.source_event_id is None for note in score.parts[0].notes)
        )
        self.assertTrue(
            all("event_id" not in note.to_dict() for note in score.parts[0].notes)
        )

        plan = build_plan(score, _roster(), _expression())
        self.assertTrue(
            all("source_event_id" not in trace for trace in plan.parts[0].trace)
        )
        self.assertTrue(
            all(
                "source_event_id" not in event
                for event in plan.parts[0].performance["events"]
            )
        )

    def test_legacy_humanisation_keeps_the_exact_index_hash_contract(self) -> None:
        self.assertEqual(
            _unit_random(17, "melody", 0, "t"),
            _unit_random(17, "melody", int("0"), "t"),
        )
        self.assertNotEqual(
            _unit_random(17, "melody", 0, "t"),
            _unit_random(17, "melody", "event-000001", "t"),
        )


class ScoreV1ParsingTests(unittest.TestCase):
    def test_v1_requires_non_empty_event_id_on_every_note(self) -> None:
        document = _legacy_score()
        document["schema_version"] = 1

        with self.assertRaisesRegex(ValueError, "event_id"):
            parse_score_document(document)

        for note in document["parts"][0]["notes"]:
            note["event_id"] = ""
        with self.assertRaisesRegex(ValueError, "non-empty"):
            parse_score_document(document)

    def test_event_ids_are_preserved_and_must_be_unique_across_parts(self) -> None:
        document = upgrade_legacy_score_to_v1(_legacy_score())
        document["parts"].append(
            {
                "id": "second",
                "notes": [
                    {
                        "event_id": document["parts"][0]["notes"][0]["event_id"],
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "G4",
                    }
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "duplicate event_id"):
            parse_score_document(document)

        document["parts"][1]["notes"][0]["event_id"] = "user-stable-id"
        score = parse_score_document(document)
        note = score.parts[1].notes[0]
        self.assertEqual(note.source_event_id, "user-stable-id")
        self.assertEqual(note.to_dict()["event_id"], "user-stable-id")

    def test_event_id_without_schema_version_is_rejected_as_ambiguous(self) -> None:
        document = _legacy_score()
        document["parts"][0]["notes"][0]["event_id"] = "orphan-id"

        with self.assertRaisesRegex(ValueError, "requires score.schema_version 1"):
            parse_score_document(document)


class ScoreV1MigrationTests(unittest.TestCase):
    def test_migration_is_pure_deterministic_and_idempotent(self) -> None:
        legacy = _legacy_score()
        before = copy.deepcopy(legacy)

        first = upgrade_legacy_score_to_v1(legacy)
        second = upgrade_legacy_score_to_v1(legacy)

        self.assertEqual(legacy, before)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(
            [note["event_id"] for note in first["parts"][0]["notes"]],
            ["event-000001", "event-000002"],
        )

        detached = upgrade_legacy_score_to_v1(first)
        self.assertEqual(detached, first)
        self.assertIsNot(detached, first)
        detached["parts"][0]["notes"][0]["pitch"] = "A5"
        self.assertEqual(first["parts"][0]["notes"][0]["pitch"], "C4")

    def test_migration_ids_do_not_depend_on_editable_note_content(self) -> None:
        original = _legacy_score()
        edited = _legacy_score()
        edited["parts"][0]["notes"][0].update(
            {
                "bar": 2,
                "beat": 4,
                "duration_beats": 2,
                "pitch": "Bb5",
                "dynamic": "ff",
                "articulation": "accent",
            }
        )

        original_ids = [
            note["event_id"]
            for note in upgrade_legacy_score_to_v1(original)["parts"][0]["notes"]
        ]
        edited_ids = [
            note["event_id"]
            for note in upgrade_legacy_score_to_v1(edited)["parts"][0]["notes"]
        ]
        self.assertEqual(edited_ids, original_ids)


class ScoreV1ConductorTests(unittest.TestCase):
    def test_source_identity_reaches_trace_and_renderable_performance(self) -> None:
        document = upgrade_legacy_score_to_v1(_legacy_score())
        score = parse_score_document(document)
        plan = build_plan(score, _roster(), _expression())

        expected = {"event-000001", "event-000002"}
        self.assertEqual(
            {trace["source_event_id"] for trace in plan.parts[0].trace},
            expected,
        )
        note_events = [
            event
            for event in plan.parts[0].performance["events"]
            if event["type"] in {"note_on", "note_off"}
        ]
        self.assertEqual(
            {event["source_event_id"] for event in note_events},
            expected,
        )

        parsed = parse_performance_document(plan.parts[0].performance)
        self.assertEqual(
            {
                event.payload["source_event_id"]
                for event in parsed.events
                if event.type in {"note_on", "note_off"}
            },
            expected,
        )

    def test_tied_sounding_note_uses_the_tie_chain_anchor_identity(self) -> None:
        document = upgrade_legacy_score_to_v1(_legacy_score())
        first, second = document["parts"][0]["notes"]
        first["duration_beats"] = 1
        first["tie"] = True
        second["pitch"] = first["pitch"]

        plan = build_plan(
            parse_score_document(document),
            _roster(),
            _expression(),
        )
        note_ons = [
            event
            for event in plan.parts[0].performance["events"]
            if event["type"] == "note_on"
        ]
        self.assertEqual(len(note_ons), 1)
        self.assertEqual(note_ons[0]["source_event_id"], first["event_id"])
        self.assertEqual(
            plan.parts[0].trace[0]["source_event_id"],
            first["event_id"],
        )

    def test_existing_v1_note_humanisation_survives_an_insert(self) -> None:
        baseline_document = upgrade_legacy_score_to_v1(_legacy_score())
        baseline = build_plan(
            parse_score_document(baseline_document),
            _roster(),
            _expression(),
        )

        edited_document = copy.deepcopy(baseline_document)
        edited_document["parts"][0]["notes"].insert(
            0,
            {
                "event_id": "new-event",
                "bar": 1,
                "beat": 1,
                "duration_beats": 0.5,
                "pitch": "G3",
                "dynamic": "mf",
            },
        )
        edited = build_plan(
            parse_score_document(edited_document),
            _roster(),
            _expression(),
        )

        before = _note_on_by_source(baseline)
        after = _note_on_by_source(edited)
        for event_id in ("event-000001", "event-000002"):
            with self.subTest(event_id=event_id):
                self.assertEqual(after[event_id]["time"], before[event_id]["time"])
                self.assertEqual(
                    after[event_id]["velocity"],
                    before[event_id]["velocity"],
                )


class ScoreIdentitySchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.score_schema = json.loads(
            (ROOT / "schemas" / "score.schema.json").read_text(encoding="utf-8")
        )
        cls.performance_schema = json.loads(
            (ROOT / "schemas" / "performance.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_score_schema_accepts_legacy_and_v1_but_requires_v1_ids(self) -> None:
        validator = Draft202012Validator(self.score_schema)
        self.assertEqual(list(validator.iter_errors(_legacy_score())), [])

        v1 = upgrade_legacy_score_to_v1(_legacy_score())
        self.assertEqual(list(validator.iter_errors(v1)), [])

        del v1["parts"][0]["notes"][0]["event_id"]
        self.assertTrue(list(validator.iter_errors(v1)))

        legacy_with_id = _legacy_score()
        legacy_with_id["parts"][0]["notes"][0]["event_id"] = "legacy-id"
        self.assertTrue(list(validator.iter_errors(legacy_with_id)))

        whitespace_id = upgrade_legacy_score_to_v1(_legacy_score())
        whitespace_id["parts"][0]["notes"][0]["event_id"] = " \t "
        self.assertTrue(list(validator.iter_errors(whitespace_id)))

    def test_performance_schema_accepts_generated_source_event_ids(self) -> None:
        plan = build_plan(
            parse_score_document(upgrade_legacy_score_to_v1(_legacy_score())),
            _roster(),
            _expression(),
        )
        errors = list(
            Draft202012Validator(self.performance_schema).iter_errors(
                plan.parts[0].performance
            )
        )
        self.assertEqual(errors, [])

    def test_performance_parser_rejects_an_empty_source_event_id(self) -> None:
        performance = {
            "events": [
                {
                    "time": 0,
                    "type": "note_on",
                    "note_id": 1,
                    "source_event_id": "",
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {
                    "time": 1,
                    "type": "note_off",
                    "note_id": 1,
                    "source_event_id": "",
                },
            ]
        }
        with self.assertRaisesRegex(ValueError, "source_event_id"):
            parse_performance_document(performance)

        performance["events"][0]["source_event_id"] = " \t "
        performance["events"][1]["source_event_id"] = " \t "
        with self.assertRaisesRegex(ValueError, "source_event_id"):
            parse_performance_document(performance)

        schema_errors = list(
            Draft202012Validator(self.performance_schema).iter_errors(
                performance
            )
        )
        self.assertTrue(schema_errors)

    def test_performance_parser_requires_matching_source_event_ids(self) -> None:
        performance = {
            "events": [
                {
                    "time": 0,
                    "type": "note_on",
                    "note_id": 1,
                    "source_event_id": "score-event-a",
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {
                    "time": 1,
                    "type": "note_off",
                    "note_id": 1,
                    "source_event_id": "score-event-b",
                },
            ]
        }
        with self.assertRaisesRegex(ValueError, "must match"):
            parse_performance_document(performance)


if __name__ == "__main__":
    unittest.main()
