from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from tianlai.capability import InstrumentCapability
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = InstrumentCapability(
    name="测试乐器",
    relative_path="测试乐器",
    manifest_path="测试乐器/乐器.json",
    implementation_type="oscillator",
    pitched=True,
    note_min=0.0,
    note_max=127.0,
    articulations=("sustain",),
    default_articulation="sustain",
    articulation_source="test",
    onset_seconds=None,
    quality_tier="candidate",
    license_status="approved",
)
CAPABILITIES = {"测试乐器": CAPABILITY}


def _score() -> dict:
    return {
        "title": "unknown-key test",
        "tempo_map": [
            {
                "bar": 1,
                "bpm": 60,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "part",
                "phrases": [{"start_bar": 1, "end_bar": 1}],
                "notes": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    }
                ],
            }
        ],
    }


def _roster() -> dict:
    return {
        "name": "unknown-key test",
        "assignments": [
            {
                "part": "part",
                "instrument": "测试乐器",
                "gain_db": -3,
                "seat": {"azimuth_deg": 0, "distance_m": 3},
                "gain_automation": [
                    {"bar": 1, "beat": 1, "offset_db": 0}
                ],
            }
        ],
    }


class UnknownKeyTestCase(unittest.TestCase):
    def assert_unknown(self, function, path: str, key: str) -> None:
        with self.assertRaisesRegex(
            ValueError,
            re.escape(f"{path}.{key}"),
        ):
            function()


class ScoreUnknownKeyTests(UnknownKeyTestCase):
    def test_score_top_level_path(self) -> None:
        document = _score()
        document["titel"] = "typo"
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score",
            "titel",
        )

    def test_tempo_path(self) -> None:
        document = _score()
        document["tempo_map"][0]["bpmm"] = 90
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score.tempo_map[0]",
            "bpmm",
        )

    def test_part_path(self) -> None:
        document = _score()
        document["parts"][0]["nam"] = "typo"
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score.parts[0]",
            "nam",
        )

    def test_note_articulation_typo_is_not_silently_dropped(self) -> None:
        document = _score()
        document["parts"][0]["notes"][0]["articluation"] = "staccato"
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score.parts[0].notes[0]",
            "articluation",
        )

    def test_phrase_path(self) -> None:
        document = _score()
        document["parts"][0]["phrases"][0]["end_beet"] = 4
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score.parts[0].phrases[0]",
            "end_beet",
        )

    def test_tuning_path(self) -> None:
        document = _score()
        document["tuning"] = {
            "temperament": "equal",
            "a4_hz": 440,
            "a4": 442,
        }
        self.assert_unknown(
            lambda: parse_score_document(document),
            "score.tuning",
            "a4",
        )


class RosterUnknownKeyTests(UnknownKeyTestCase):
    def test_roster_top_level_path(self) -> None:
        document = _roster()
        document["drop_part"] = []
        self.assert_unknown(
            lambda: parse_roster_document(document, CAPABILITIES),
            "roster",
            "drop_part",
        )

    def test_assignment_gain_typo_is_not_silently_defaulted(self) -> None:
        document = _roster()
        document["assignments"][0]["gian_db"] = -20
        self.assert_unknown(
            lambda: parse_roster_document(document, CAPABILITIES),
            "roster.assignments[0]",
            "gian_db",
        )

    def test_seat_path(self) -> None:
        document = _roster()
        document["assignments"][0]["seat"]["distance"] = 4
        self.assert_unknown(
            lambda: parse_roster_document(document, CAPABILITIES),
            "roster.assignments[0].seat",
            "distance",
        )

    def test_kit_entry_object_path_while_notehead_keys_remain_open(self) -> None:
        document = _roster()
        assignment = document["assignments"][0]
        assignment.pop("instrument")
        assignment["kit"] = {
            "C2": {
                "instrument": "测试乐器",
                "transopse": 12,
            },
            "F#2": "测试乐器",
        }
        self.assert_unknown(
            lambda: parse_roster_document(document, CAPABILITIES),
            "roster.assignments[0].kit['C2']",
            "transopse",
        )

    def test_gain_automation_point_path(self) -> None:
        document = _roster()
        document["assignments"][0]["gain_automation"][0]["offset_dB"] = 2
        self.assert_unknown(
            lambda: parse_roster_document(document, CAPABILITIES),
            "roster.assignments[0].gain_automation[0]",
            "offset_dB",
        )

    def test_assignment_note_string_is_an_intentional_comment(self) -> None:
        document = _roster()
        document["assignments"][0]["_note"] = "这里只给人看"
        parsed = parse_roster_document(document, CAPABILITIES)
        self.assertEqual(len(parsed.executors), 1)

    def test_assignment_note_must_be_a_string(self) -> None:
        document = _roster()
        document["assignments"][0]["_note"] = {"not": "text"}
        with self.assertRaisesRegex(
            ValueError,
            re.escape("roster.assignments[0]._note"),
        ):
            parse_roster_document(document, CAPABILITIES)


class SchemaSynchronizationTests(unittest.TestCase):
    def test_roster_schema_declares_assignment_note_comment(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "roster.schema.json").read_text(encoding="utf-8")
        )
        assignment = schema["properties"]["assignments"]["items"]
        self.assertEqual(assignment["properties"]["_note"]["type"], "string")
        self.assertFalse(assignment["additionalProperties"])

    def test_score_schema_remains_closed_at_all_checked_object_layers(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "score.schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        tempo = schema["properties"]["tempo_map"]["items"]
        part = schema["properties"]["parts"]["items"]
        note = part["properties"]["notes"]["items"]
        phrase = part["properties"]["phrases"]["items"]
        for layer in (tempo, part, note, phrase):
            self.assertFalse(layer["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
