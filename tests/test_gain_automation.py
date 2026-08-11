"""Collaboration-layer mix automation and override-boundary regression tests."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import unittest

import numpy as np

from tianlai.capability import load_capabilities
from tianlai.conductor import (
    ExpressionSettings,
    GainEnvelopePoint,
    build_plan,
)
from tianlai.ensemble import apply_gain_envelope
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]


def _score() -> dict:
    return {
        "title": "自动化测试",
        "sample_rate": 48_000,
        "tail_seconds": 0.0,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 120,
                "beats_per_bar": 4,
                "beat_unit": 4,
            },
            {"bar": 2, "beat": 1, "bpm": 60},
        ],
        "parts": [
            {
                "id": "piano",
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
        "name": "自动化测试编制",
        "assignments": [
            {
                "part": "piano",
                "instrument": "键盘乐器/钢琴",
                "gain_db": -6.0,
                "gain_automation": [
                    {"bar": 1, "beat": 1, "offset_db": 0.0},
                    {"bar": 2, "beat": 3, "offset_db": 6.0},
                ],
            }
        ],
    }


class GainAutomationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_musical_points_compile_through_tempo_changes(self) -> None:
        score = parse_score_document(_score())
        roster = parse_roster_document(_roster(), self.capabilities)
        plan = build_plan(
            score,
            roster,
            ExpressionSettings.from_dict({"mode": "strict"}),
        )
        envelope = plan.parts[0].gain_envelope
        self.assertEqual(len(envelope), 2)
        self.assertAlmostEqual(envelope[0].time_seconds, 0.0)
        # bar 1 = 2s at 120 BPM; two more quarters at 60 BPM = 2s.
        self.assertAlmostEqual(envelope[1].time_seconds, 4.0)
        serialized = plan.parts[0].to_dict()["gain_envelope"]
        self.assertEqual(serialized[1]["effective_gain_db"], 0.0)

    def test_first_point_must_remove_pre_roll_ambiguity(self) -> None:
        roster = _roster()
        roster["assignments"][0]["gain_automation"][0]["beat"] = 2
        with self.assertRaisesRegex(ValueError, "bar 1 beat 1"):
            parse_roster_document(roster, self.capabilities)

    def test_points_must_be_strictly_ordered(self) -> None:
        roster = _roster()
        roster["assignments"][0]["gain_automation"].append(
            {"bar": 2, "beat": 3, "offset_db": -2}
        )
        with self.assertRaisesRegex(ValueError, "ordered without duplicates"):
            parse_roster_document(roster, self.capabilities)

    def test_point_must_fall_inside_its_bar(self) -> None:
        roster = _roster()
        roster["assignments"][0]["gain_automation"][1]["beat"] = 5
        parsed = parse_roster_document(roster, self.capabilities)
        with self.assertRaisesRegex(ValueError, "4/4"):
            build_plan(parse_score_document(_score()), parsed)

    def test_plan_is_unchanged_when_automation_is_absent(self) -> None:
        roster_data = _roster()
        del roster_data["assignments"][0]["gain_automation"]
        plan = build_plan(
            parse_score_document(_score()),
            parse_roster_document(roster_data, self.capabilities),
        )
        self.assertEqual(plan.parts[0].gain_envelope, ())
        self.assertNotIn("gain_envelope", plan.parts[0].to_dict())


class GainEnvelopeDspTests(unittest.TestCase):
    def test_dsp_interpolates_in_db_and_includes_static_gain(self) -> None:
        plus_six = 20.0 * math.log10(2.0)
        points = (
            GainEnvelopePoint(1, 1.0, 0.0, 0.0),
            GainEnvelopePoint(2, 1.0, 4.0, plus_six),
        )
        buffer = np.ones((5, 2), dtype=np.float64)
        apply_gain_envelope(buffer, 1, -plus_six, points)
        self.assertAlmostEqual(buffer[0, 0], 0.5)
        self.assertAlmostEqual(buffer[2, 0], math.sqrt(0.5))
        self.assertAlmostEqual(buffer[4, 0], 1.0)
        np.testing.assert_array_equal(buffer[:, 0], buffer[:, 1])

    def test_no_envelope_is_exact_static_gain(self) -> None:
        buffer = np.ones((3, 2), dtype=np.float64)
        apply_gain_envelope(buffer, 48_000, -6.0, ())
        np.testing.assert_allclose(buffer, 10.0 ** (-6.0 / 20.0))

    def test_in_place_curve_scratch_is_bit_identical_across_chunks(self) -> None:
        sample_rate = 48_000
        frames = 150_123
        points = (
            GainEnvelopePoint(1, 1.0, 0.0, 0.0),
            GainEnvelopePoint(1, 2.0, 1.25, -3.0),
            GainEnvelopePoint(1, 3.0, 3.0, 2.0),
        )
        rng = np.random.default_rng(20260811)
        buffer = rng.uniform(-0.5, 0.5, size=(frames, 2)).astype(np.float64)
        expected = buffer.copy()
        times = np.asarray([point.time_seconds for point in points])
        offsets = np.asarray([point.offset_db for point in points])
        for start in range(0, frames, 65_536):
            end = min(frames, start + 65_536)
            frame_times = np.arange(start, end, dtype=np.float64) / sample_rate
            db = -1.5 + np.interp(
                frame_times,
                times,
                offsets,
                left=offsets[0],
                right=offsets[-1],
            )
            expected[start:end] *= np.power(10.0, db / 20.0)[:, np.newaxis]

        apply_gain_envelope(buffer, sample_rate, -1.5, points)

        np.testing.assert_array_equal(buffer, expected)


class OverrideBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_documented_acoustic_overrides_remain_available(self) -> None:
        for key, value in (
            ("release_seconds", 0.25),
            ("release_tail_gain", 0.0),
            ("sample_variant", "SEC"),
        ):
            roster = _roster()
            del roster["assignments"][0]["gain_automation"]
            roster["assignments"][0]["overrides"] = {key: value}
            parsed = parse_roster_document(roster, self.capabilities)
            self.assertEqual(parsed.executors[0].override_map, {key: value})

    def test_release_tail_gain_override_is_range_checked(self) -> None:
        for value in (-0.01, 1.01, float("nan"), True, "0"):
            with self.subTest(value=value):
                roster = _roster()
                del roster["assignments"][0]["gain_automation"]
                roster["assignments"][0]["overrides"] = {
                    "release_tail_gain": value
                }
                with self.assertRaisesRegex(ValueError, "release_tail_gain"):
                    parse_roster_document(roster, self.capabilities)

    def test_identity_and_license_fields_cannot_be_overridden(self) -> None:
        for key in ("type", "implementation", "asset_root", "license_status"):
            with self.subTest(key=key):
                roster = copy.deepcopy(_roster())
                del roster["assignments"][0]["gain_automation"]
                roster["assignments"][0]["overrides"] = {key: "forged"}
                with self.assertRaisesRegex(ValueError, "禁止覆盖"):
                    parse_roster_document(roster, self.capabilities)


if __name__ == "__main__":
    unittest.main()
