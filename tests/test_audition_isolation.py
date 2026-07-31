from __future__ import annotations

import math
import unittest

from tianlai.audition_protocol import (
    AuditionStrike,
    FullRangeAudition,
    isolate_full_range_audition,
    restrict_full_range_audition,
)


def _plan() -> FullRangeAudition:
    sequence = (
        AuditionStrike(60, "sustain", 0.48, 0.12),
        AuditionStrike(61, "sustain", 0.8, 0.9),
    )
    return FullRangeAudition(
        instrument="测试/长尾弦乐",
        articulation="sustain",
        pitch_semantics="pitched_chromatic",
        range_source="test",
        declared_ranges=((60, 61),),
        gaps=(),
        sequence=sequence,
        tail_seconds=1.5,
        exception=None,
        document={},
    )


class IsolatedAuditionTests(unittest.TestCase):
    def test_review_subset_is_explicit_and_keeps_only_selected_keys(self) -> None:
        source = FullRangeAudition(
            instrument="测试/扩展弦乐",
            articulation="sustain",
            pitch_semantics="pitched_chromatic",
            range_source="manifest.note_min_note_max",
            declared_ranges=((55, 105),),
            gaps=(),
            sequence=tuple(
                AuditionStrike(key, "sustain") for key in range(55, 106)
            ),
            tail_seconds=1.5,
            exception=None,
            document={},
        )

        core = restrict_full_range_audition(
            source,
            ranges=((55, 94),),
            reason="95–105 仅为升调兼容扩展，不进入高仿复验",
        )

        self.assertEqual(core.declared_ranges, ((55, 94),))
        self.assertEqual(core.keys, tuple(range(55, 95)))
        self.assertEqual(core.document["events"][-2]["midi_note"], 94)
        self.assertIn("升调兼容扩展", core.exception or "")
        self.assertIn("explicit_review_subset", core.range_source)

    def test_review_subset_must_be_inside_source_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the source audition"):
            restrict_full_range_audition(
                _plan(),
                ranges=((59, 60),),
                reason="invalid",
            )

    def test_isolation_preserves_coverage_and_reserves_release_tail(self) -> None:
        original = _plan()
        isolated = isolate_full_range_audition(
            original,
            gate_seconds=0.7,
            release_seconds=1.6,
            silence_seconds=0.2,
        )

        self.assertEqual(isolated.keys, original.keys)
        self.assertEqual(
            tuple(strike.articulation for strike in isolated.sequence),
            ("sustain", "sustain"),
        )
        self.assertEqual(
            tuple(strike.duration_seconds for strike in isolated.sequence),
            (0.7, 0.8),
        )
        self.assertEqual(
            tuple(strike.gap_seconds for strike in isolated.sequence),
            (1.8, 1.8),
        )
        self.assertEqual(isolated.tail_seconds, 1.8)
        self.assertIn("相邻音的声音尾部不得交叠", isolated.exception or "")

        note_ons = [
            event
            for event in isolated.document["events"]
            if event["type"] == "note_on"
        ]
        note_offs = [
            event
            for event in isolated.document["events"]
            if event["type"] == "note_off"
        ]
        self.assertEqual(
            [event["time"] for event in note_ons],
            [0.25, 2.75],
        )
        self.assertEqual(
            [event["time"] for event in note_offs],
            [0.95, 3.55],
        )
        self.assertEqual(
            original.sequence[0],
            AuditionStrike(60, "sustain", 0.48, 0.12),
        )

    def test_existing_longer_gap_and_tail_are_not_shortened(self) -> None:
        isolated = isolate_full_range_audition(
            _plan(),
            gate_seconds=0.5,
            release_seconds=0.3,
            silence_seconds=0.1,
        )

        self.assertEqual(
            tuple(strike.gap_seconds for strike in isolated.sequence),
            (0.4, 0.9),
        )
        self.assertEqual(isolated.tail_seconds, 1.5)

    def test_timing_inputs_must_be_finite_positive_numbers(self) -> None:
        for field, kwargs in (
            ("gate_seconds", {"gate_seconds": 0.0, "release_seconds": 1.0}),
            (
                "release_seconds",
                {"gate_seconds": 1.0, "release_seconds": math.nan},
            ),
            (
                "silence_seconds",
                {
                    "gate_seconds": 1.0,
                    "release_seconds": 1.0,
                    "silence_seconds": -0.1,
                },
            ),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    isolate_full_range_audition(_plan(), **kwargs)


if __name__ == "__main__":
    unittest.main()
