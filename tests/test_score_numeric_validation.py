from __future__ import annotations

import copy
import math
import unittest

from tianlai.score import parse_score_document
from tianlai.score_time import validate_score_time_coordinates


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "numeric validation",
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
                "name": "part",
                "notes": [
                    {
                        "event_id": "note-1",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 1.0,
                        "pitch": "C4",
                        "velocity": 0.5,
                    }
                ],
            }
        ],
    }


class ScoreNumericValidationTests(unittest.TestCase):
    def test_nonfinite_note_coordinates_and_duration_are_rejected(self) -> None:
        for field in ("beat", "duration_beats"):
            for value in (math.inf, -math.inf, math.nan):
                with self.subTest(field=field, value=value):
                    document = _score()
                    document["parts"][0]["notes"][0][field] = value
                    with self.assertRaisesRegex(ValueError, "finite"):
                        parse_score_document(document)

    def test_nonfinite_tail_tempo_and_tuning_are_rejected(self) -> None:
        mutations = (
            (("tail_seconds",), math.inf),
            (("tempo_map", 0, "beat"), math.nan),
            (("tempo_map", 0, "bpm"), math.inf),
            (("tuning", "a4_hz"), math.inf),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                document = _score()
                target = document
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    parse_score_document(document)

    def test_boolean_and_fractional_integer_fields_are_rejected(self) -> None:
        for path, value in (
            (("sample_rate",), True),
            (("tempo_map", 0, "bar"), True),
            (("tempo_map", 0, "bar"), 1.5),
            (("tempo_map", 0, "beats_per_bar"), 4.5),
            (("tempo_map", 0, "beat_unit"), False),
            (("parts", 0, "notes", 0, "bar"), 1.5),
            (("parts", 0, "notes", 0, "staff"), True),
            (("parts", 0, "notes", 0, "staff"), 1.5),
        ):
            with self.subTest(path=path):
                document = copy.deepcopy(_score())
                target = document
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    parse_score_document(document)

    def test_tie_and_voice_types_are_not_coerced(self) -> None:
        for field, value in (
            ("tie", "false"),
            ("tie", 1),
            ("voice", 2),
            ("voice", "   "),
            ("articulation", 123),
        ):
            with self.subTest(field=field, value=value):
                document = copy.deepcopy(_score())
                document["parts"][0]["notes"][0][field] = value
                with self.assertRaises(ValueError):
                    parse_score_document(document)

    def test_parsed_score_passes_strict_time_validation(self) -> None:
        validate_score_time_coordinates(parse_score_document(_score()))


if __name__ == "__main__":
    unittest.main()
