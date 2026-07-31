"""Strict logical-time validation and reverse-coordinate regression tests."""

from __future__ import annotations

from dataclasses import replace
import math
import unittest

from tianlai.score import Phrase, TempoEntry, TempoMap, parse_score_document
from tianlai.score_time import (
    ScoreTimeError,
    coordinate_at_position,
    coordinate_at_seconds,
    seconds_range_for_positions,
    seconds_window_around,
    validate_bar_beat,
    validate_optional_bar_beat,
    validate_score_time_coordinates,
    validate_tempo_map_coordinates,
)


def _document():
    return parse_score_document(
        {
            "title": "score time",
            "tempo_map": [
                {
                    "bar": 1,
                    "beat": 1,
                    "bpm": 60,
                    "beats_per_bar": 4,
                    "beat_unit": 4,
                },
                {"bar": 1, "beat": 3, "bpm": 120},
                {
                    "bar": 2,
                    "beat": 1,
                    "bpm": 90,
                    "beats_per_bar": 3,
                    "beat_unit": 4,
                },
                {
                    "bar": 3,
                    "beat": 1,
                    "bpm": 60,
                    "beats_per_bar": 6,
                    "beat_unit": 8,
                },
                {"bar": 3, "beat": 4, "bpm": 120},
            ],
            "parts": [
                {
                    "id": "part",
                    "notes": [
                        {
                            "bar": 1,
                            "beat": 4.5,
                            "duration_beats": 1,
                            "pitch": "C4",
                        },
                        {
                            "bar": 2,
                            "beat": 3.5,
                            "duration_beats": 1,
                            "pitch": "D4",
                        },
                        {
                            "bar": 3,
                            "beat": 6.5,
                            "duration_beats": 1,
                            "pitch": "E4",
                        },
                    ],
                    "phrases": [
                        {
                            "start_bar": 1,
                            "start_beat": 1,
                            "end_bar": 4,
                            "end_beat": 1,
                        }
                    ],
                }
            ],
        }
    )


class ScoreTimeValidationTests(unittest.TestCase):
    def test_mixed_meters_accept_only_their_own_half_open_beat_ranges(self):
        score = _document()
        validate_score_time_coordinates(score)
        validate_bar_beat(score.tempo_map, 1, 4.999)
        validate_bar_beat(score.tempo_map, 2, 3.999)
        validate_bar_beat(score.tempo_map, 3, 6.999)

        for bar, excluded_end in ((1, 5), (2, 4), (3, 7)):
            with self.subTest(bar=bar):
                with self.assertRaises(ScoreTimeError):
                    validate_bar_beat(score.tempo_map, bar, excluded_end)

    def test_invalid_note_reports_original_json_array_path(self):
        score = _document()
        original = score.parts[0].notes[0]
        invalid = replace(original, beat=5.0)
        part = replace(score.parts[0], notes=(invalid,))
        score = replace(score, parts=(part,))

        with self.assertRaisesRegex(
            ScoreTimeError,
            r"score\.parts\[0\]\.notes\[0\]\.beat=5.*4/4",
        ):
            validate_score_time_coordinates(score)

    def test_invalid_phrase_endpoint_reports_its_json_path(self):
        score = _document()
        phrase = Phrase(
            start_bar=1,
            start_beat=1,
            end_bar=2,
            end_beat=4,
        )
        part = replace(score.parts[0], phrases=(phrase,))
        score = replace(score, parts=(part,))

        with self.assertRaisesRegex(
            ScoreTimeError,
            r"score\.parts\[0\]\.phrases\[0\]\.end\.beat=4.*3/4",
        ):
            validate_score_time_coordinates(score)

    def test_invalid_mid_bar_tempo_reports_tempo_json_path(self):
        score = _document()
        first = score.tempo_map.entries[0]
        invalid_map = TempoMap(
            entries=(
                first,
                TempoEntry(
                    bar=1,
                    beat=5,
                    bpm=80,
                    beats_per_bar=4,
                    beat_unit=4,
                ),
            )
        )
        with self.assertRaisesRegex(
            ScoreTimeError,
            r"score\.tempo_map\[1\]\.beat=5.*4/4",
        ):
            validate_tempo_map_coordinates(invalid_map)

    def test_optional_position_distinguishes_absent_from_ambiguous(self):
        tempo_map = _document().tempo_map
        validate_optional_bar_beat(tempo_map, None, None, path="marker")
        validate_optional_bar_beat(tempo_map, 2, None, path="marker")
        with self.assertRaisesRegex(
            ScoreTimeError,
            r"marker\.beat is present but marker\.bar is absent",
        ):
            validate_optional_bar_beat(tempo_map, None, 2, path="marker")

    def test_non_finite_position_is_rejected(self):
        tempo_map = _document().tempo_map
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ScoreTimeError, "finite"):
                    validate_bar_beat(tempo_map, 1, value)


class ReverseCoordinateTests(unittest.TestCase):
    def test_mid_bar_tempo_and_meter_changes_are_inverted_exactly(self):
        tempo_map = _document().tempo_map
        expected = (
            (0.0, 1, 1.0, 0.0),
            (2.0, 1, 3.0, 2.0),
            (2.5, 1, 4.0, 3.0),
            (3.0, 2, 1.0, 4.0),
            (5.0, 3, 1.0, 7.0),
            (6.0, 3, 3.0, 8.0),
            (6.5, 3, 4.0, 8.5),
            (7.25, 4, 1.0, 10.0),
        )
        for seconds, bar, beat, quarter in expected:
            with self.subTest(seconds=seconds):
                coordinate = coordinate_at_seconds(tempo_map, seconds)
                self.assertEqual(coordinate.bar, bar)
                self.assertAlmostEqual(coordinate.beat, beat)
                self.assertAlmostEqual(coordinate.absolute_quarter, quarter)
                self.assertEqual(coordinate.seconds, seconds)

    def test_position_to_seconds_round_trips_across_all_segments(self):
        tempo_map = _document().tempo_map
        for bar, beat in (
            (1, 1.0),
            (1, 2.5),
            (1, 3.0),
            (2, 1.0),
            (2, 2.25),
            (3, 1.0),
            (3, 4.0),
            (3, 5.75),
            (8, 1.0),
        ):
            with self.subTest(bar=bar, beat=beat):
                forward = coordinate_at_position(tempo_map, bar, beat)
                reverse = coordinate_at_seconds(tempo_map, forward.seconds)
                self.assertEqual(reverse.bar, bar)
                self.assertAlmostEqual(reverse.beat, beat, places=9)
                self.assertAlmostEqual(
                    reverse.absolute_quarter,
                    forward.absolute_quarter,
                    places=9,
                )

    def test_exact_barline_belongs_to_next_bar_beat_one(self):
        tempo_map = _document().tempo_map
        boundary = coordinate_at_position(tempo_map, 4, 1)
        reverse = coordinate_at_seconds(tempo_map, boundary.seconds)
        self.assertEqual((reverse.bar, reverse.beat), (4, 1.0))

    def test_negative_and_non_finite_seconds_are_rejected(self):
        tempo_map = _document().tempo_map
        for value in (-0.001, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ScoreTimeError):
                    coordinate_at_seconds(tempo_map, value)

    def test_coordinate_at_position_enforces_excluded_bar_end(self):
        with self.assertRaisesRegex(ScoreTimeError, r"outside \[1, 5\)"):
            coordinate_at_position(_document().tempo_map, 1, 5)


class SecondsWindowTests(unittest.TestCase):
    def test_clock_window_clips_at_zero_and_known_media_end(self):
        window = seconds_window_around(
            2.0,
            before_seconds=5.0,
            after_seconds=5.0,
            maximum_seconds=6.0,
        )
        self.assertEqual(window.start_seconds, 0.0)
        self.assertEqual(window.end_seconds, 6.0)
        self.assertEqual(window.duration_seconds, 6.0)
        self.assertEqual(
            window.to_dict(),
            {
                "start_seconds": 0.0,
                "end_seconds": 6.0,
                "duration_seconds": 6.0,
            },
        )

    def test_musical_window_uses_tempo_map_and_padding(self):
        tempo_map = _document().tempo_map
        window = seconds_range_for_positions(
            tempo_map,
            start_bar=1,
            start_beat=2,
            end_bar=2,
            end_beat=2,
            pre_roll_seconds=2.0,
            post_roll_seconds=0.5,
        )
        self.assertEqual(window.start_seconds, 0.0)
        # bar 2 beat 1 is 3s; at 90 BPM, beat 2 is 3 + 2/3s.
        self.assertAlmostEqual(window.end_seconds, 3.0 + 2.0 / 3.0 + 0.5)

    def test_reversed_musical_window_is_rejected(self):
        with self.assertRaisesRegex(ScoreTimeError, "must not precede"):
            seconds_range_for_positions(
                _document().tempo_map,
                start_bar=3,
                end_bar=2,
            )

    def test_clock_window_rejects_center_beyond_known_media(self):
        with self.assertRaisesRegex(ScoreTimeError, "must not exceed"):
            seconds_window_around(7, maximum_seconds=6)


if __name__ == "__main__":
    unittest.main()
