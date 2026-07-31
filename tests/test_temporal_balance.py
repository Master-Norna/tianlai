from __future__ import annotations

import json
import math
import unittest

import numpy as np

from tianlai.mix_analysis import MixAnalysisConfig
from tianlai.temporal_balance import (
    DERIVED_DECIMAL_DIGITS,
    TEMPORAL_BALANCE_FORMAT,
    TEMPORAL_BALANCE_VERSION,
    analyze_temporal_balance,
)


SAMPLE_RATE = 8_000
CONFIG = MixAnalysisConfig(
    window_seconds=0.100,
    hop_seconds=0.100,
    absolute_gate_dbfs=-100.0,
    relative_gate_db=-80.0,
)


def _tone(
    *,
    amplitude: float = 0.2,
    seconds: float = 2.0,
    antiphase: bool = False,
) -> np.ndarray:
    time = np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 200.0 * time)
    right = -mono if antiphase else mono
    return np.column_stack((mono, right)).astype(np.float64)


def _analyze(
    first: np.ndarray,
    second: np.ndarray,
    *,
    target: float,
    tolerance: float,
    minimum: int = 5,
):
    return analyze_temporal_balance(
        first,
        second,
        SAMPLE_RATE,
        CONFIG,
        target_offset_db=target,
        tolerance_db=tolerance,
        minimum_shared_window_count=minimum,
    )


class TemporalBalanceTests(unittest.TestCase):
    def test_balanced_median_can_still_expose_front_back_drift(self) -> None:
        second = _tone(amplitude=0.2)
        first = _tone(amplitude=1.0)
        halfway = first.shape[0] // 2
        first[:halfway] *= 0.1
        first[halfway:] *= 0.4

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=1.0,
        )

        self.assertEqual(result.shared_active_window_count, 20)
        self.assertEqual(result.window_count, 20)
        self.assertEqual(result.overlap_ratio, 1.0)
        self.assertAlmostEqual(result.median_db, 0.0, places=6)
        self.assertLess(result.p10_db, -5.9)
        self.assertGreater(result.p90_db, 5.9)
        self.assertGreater(result.robust_span_db, 11.8)
        self.assertEqual(result.within_tolerance_window_ratio, 0.0)
        self.assertEqual(result.below_tolerance_window_count, 10)
        self.assertEqual(result.above_tolerance_window_count, 10)
        self.assertEqual(result.status, "varies_outside_tolerance")
        self.assertEqual(result.candidate_segment_count, 2)
        self.assertFalse(result.candidate_segments_truncated)
        self.assertEqual(
            [segment.to_dict() for segment in result.candidate_segments],
            [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "shared_active_window_count": 10,
                    "shared_active_coverage_seconds": 1.0,
                    "median_offset_db": -6.0206,
                    "deviation_db": -6.0206,
                    "direction": "subject_too_quiet",
                },
                {
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "shared_active_window_count": 10,
                    "shared_active_coverage_seconds": 1.0,
                    "median_offset_db": 6.0206,
                    "deviation_db": 6.0206,
                    "direction": "subject_too_loud",
                },
            ],
        )

    def test_constant_offset_is_stable_within_tolerance(self) -> None:
        first = _tone(amplitude=0.1)
        second = _tone(amplitude=0.2)

        result = _analyze(
            first,
            second,
            target=-6.0206,
            tolerance=0.01,
        )

        self.assertAlmostEqual(result.p10_db, -6.0206, places=6)
        self.assertAlmostEqual(result.median_db, -6.0206, places=6)
        self.assertAlmostEqual(result.p90_db, -6.0206, places=6)
        self.assertEqual(result.robust_span_db, 0.0)
        self.assertEqual(result.within_tolerance_window_ratio, 1.0)
        self.assertEqual(result.below_tolerance_window_count, 0)
        self.assertEqual(result.above_tolerance_window_count, 0)
        self.assertEqual(result.status, "stable_within_tolerance")
        self.assertEqual(result.candidate_segments, ())
        self.assertEqual(result.candidate_segment_count, 0)

    def test_robust_status_keeps_sparse_outlier_visible_but_not_dominant(
        self,
    ) -> None:
        first = _tone(amplitude=0.2)
        second = _tone(amplitude=0.2)
        first[-SAMPLE_RATE // 10 :] *= 2.0

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=0.1,
        )

        self.assertEqual(result.p10_db, 0.0)
        self.assertEqual(result.p90_db, 0.0)
        self.assertEqual(result.above_tolerance_window_count, 1)
        self.assertEqual(result.within_tolerance_window_ratio, 0.95)
        self.assertEqual(result.status, "stable_within_tolerance")
        self.assertEqual(result.candidate_segments, ())

    def test_adjacent_same_direction_buckets_are_merged(self) -> None:
        first = _tone(amplitude=0.1, seconds=3.0)
        second = _tone(amplitude=0.2, seconds=3.0)
        first[2 * SAMPLE_RATE :] *= 4.0

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=1.0,
        )

        self.assertEqual(result.candidate_segment_count, 2)
        self.assertEqual(
            [
                (
                    segment.start_seconds,
                    segment.end_seconds,
                    segment.direction,
                    segment.shared_active_window_count,
                )
                for segment in result.candidate_segments
            ],
            [
                (0.0, 2.0, "subject_too_quiet", 20),
                (2.0, 3.0, "subject_too_loud", 10),
            ],
        )

    def test_short_overlap_nulls_every_estimate(self) -> None:
        first = np.zeros((SAMPLE_RATE, 2), dtype=np.float64)
        second = np.zeros_like(first)
        first[: SAMPLE_RATE // 5] = _tone(seconds=0.2)
        second[: SAMPLE_RATE // 5] = _tone(seconds=0.2)

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=1.0,
            minimum=3,
        )

        self.assertEqual(result.shared_active_window_count, 2)
        self.assertEqual(result.overlap_ratio, 0.2)
        self.assertEqual(result.status, "insufficient_overlap")
        self.assertEqual(result.candidate_segment_count, 0)
        self.assertEqual(result.candidate_segments, ())
        for value in (
            result.p10_db,
            result.median_db,
            result.p90_db,
            result.robust_span_db,
            result.within_tolerance_window_ratio,
            result.below_tolerance_window_count,
            result.above_tolerance_window_count,
        ):
            self.assertIsNone(value)

    def test_short_overlapping_windows_cannot_expand_a_transient_to_one_second(
        self,
    ) -> None:
        config = MixAnalysisConfig(
            window_seconds=0.020,
            hop_seconds=0.010,
            absolute_gate_dbfs=-100.0,
            relative_gate_db=-80.0,
        )
        first = _tone(amplitude=0.2)
        second = _tone(amplitude=0.2)
        first[: round(0.040 * SAMPLE_RATE)] *= 2.0

        result = analyze_temporal_balance(
            first,
            second,
            SAMPLE_RATE,
            config,
            target_offset_db=0.0,
            tolerance_db=1.0,
            minimum_shared_window_count=3,
        )

        self.assertGreaterEqual(result.above_tolerance_window_count, 3)
        self.assertEqual(result.candidate_segment_count, 0)
        self.assertEqual(result.candidate_segments, ())

    def test_candidate_requires_three_direction_supporting_windows(
        self,
    ) -> None:
        config = MixAnalysisConfig(
            window_seconds=0.400,
            hop_seconds=0.300,
            absolute_gate_dbfs=-100.0,
            relative_gate_db=-80.0,
        )
        first = _tone(amplitude=0.2, seconds=1.0)
        second = _tone(amplitude=0.2, seconds=1.0)
        first[: round(0.600 * SAMPLE_RATE)] *= 2.0

        result = analyze_temporal_balance(
            first,
            second,
            SAMPLE_RATE,
            config,
            target_offset_db=0.0,
            tolerance_db=1.0,
            minimum_shared_window_count=3,
        )

        self.assertEqual(result.shared_active_window_count, 4)
        self.assertGreaterEqual(result.above_tolerance_window_count, 2)
        self.assertEqual(result.candidate_segment_count, 0)

    def test_segment_boundaries_and_coverage_follow_evidence_not_full_bucket(
        self,
    ) -> None:
        config = MixAnalysisConfig(
            window_seconds=0.020,
            hop_seconds=0.010,
            absolute_gate_dbfs=-100.0,
            relative_gate_db=-80.0,
        )
        first = _tone(amplitude=0.2)
        second = _tone(amplitude=0.2)
        first[: round(0.600 * SAMPLE_RATE)] *= 2.0

        result = analyze_temporal_balance(
            first,
            second,
            SAMPLE_RATE,
            config,
            target_offset_db=0.0,
            tolerance_db=1.0,
            minimum_shared_window_count=3,
        )

        self.assertEqual(result.candidate_segment_count, 1)
        segment = result.candidate_segments[0]
        self.assertEqual(segment.start_seconds, 0.0)
        self.assertLess(segment.end_seconds, 0.7)
        self.assertGreaterEqual(
            segment.shared_active_coverage_seconds,
            0.5,
        )

    def test_antiphase_stereo_does_not_disappear(self) -> None:
        first = _tone(antiphase=True)
        second = first.copy()

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=0.001,
        )

        self.assertEqual(result.shared_active_window_count, 20)
        self.assertEqual(result.median_db, 0.0)
        self.assertEqual(result.status, "stable_within_tolerance")

    def test_silence_is_insufficient_and_all_estimates_are_null(self) -> None:
        silence = np.zeros((SAMPLE_RATE, 2), dtype=np.float64)

        result = _analyze(
            silence,
            silence,
            target=0.0,
            tolerance=1.0,
            minimum=1,
        )

        self.assertEqual(result.shared_active_window_count, 0)
        self.assertEqual(result.status, "insufficient_overlap")
        self.assertIsNone(result.p10_db)
        self.assertIsNone(result.median_db)
        self.assertIsNone(result.p90_db)
        self.assertIsNone(result.robust_span_db)
        self.assertIsNone(result.within_tolerance_window_ratio)
        self.assertIsNone(result.below_tolerance_window_count)
        self.assertIsNone(result.above_tolerance_window_count)

    def test_huge_finite_input_remains_finite_and_json_safe(self) -> None:
        first = _tone() * 1.0e300
        second = first.copy()

        result = _analyze(
            first,
            second,
            target=0.0,
            tolerance=0.001,
        )
        document = result.to_dict()

        self.assertEqual(result.status, "stable_within_tolerance")
        self.assertEqual(result.median_db, 0.0)
        json.dumps(document, ensure_ascii=False, allow_nan=False)

    def test_input_is_unchanged_and_result_is_deterministic(self) -> None:
        first = _tone(amplitude=0.15)
        second = _tone(amplitude=0.2)
        first_before = first.copy()
        second_before = second.copy()

        first_result = _analyze(
            first,
            second,
            target=-2.5,
            tolerance=0.1,
        )
        second_result = _analyze(
            first,
            second,
            target=-2.5,
            tolerance=0.1,
        )

        np.testing.assert_array_equal(first, first_before)
        np.testing.assert_array_equal(second, second_before)
        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result.to_dict(), second_result.to_dict())

    def test_json_contract_is_quantized_and_omits_window_sequence(self) -> None:
        result = _analyze(
            _tone(amplitude=0.15),
            _tone(amplitude=0.2),
            target=-2.5,
            tolerance=0.2,
        )
        document = result.to_dict()

        self.assertEqual(document["format"], TEMPORAL_BALANCE_FORMAT)
        self.assertEqual(document["version"], TEMPORAL_BALANCE_VERSION)
        self.assertEqual(
            document["offset_definition"],
            "first_minus_second_db",
        )
        self.assertEqual(document["gate"], CONFIG.to_dict())
        self.assertEqual(
            document["quantization"]["derived_decimal_digits"],
            DERIVED_DECIMAL_DIGITS,
        )
        self.assertFalse(document["window_sequence_included"])
        self.assertNotIn("window_offsets_db", document)
        self.assertFalse(
            document["candidate_segment_policy"][
                "raw_window_sequence_included"
            ]
        )
        self.assertEqual(
            document["candidate_segment_policy"]["bucket_seconds"],
            1.0,
        )
        self.assertEqual(
            document["candidate_segment_policy"][
                "minimum_shared_window_coverage_seconds_per_bucket"
            ],
            0.5,
        )
        self.assertNotIn(
            "window_offsets_db",
            json.dumps(document, ensure_ascii=False),
        )
        self.assertFalse(document["audio_modified"])
        self.assertIn("不会自动调整", document["notice"])
        self.assertIn("masking", document["notice"])
        for key in (
            "overlap_ratio",
            "p10_db",
            "median_db",
            "p90_db",
            "robust_span_db",
            "within_tolerance_window_ratio",
        ):
            value = document[key]
            self.assertTrue(math.isfinite(value))
            self.assertEqual(value, round(value, DERIVED_DECIMAL_DIGITS))

    def test_same_timeline_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "same frame count"):
            _analyze(
                _tone(seconds=1.0),
                _tone(seconds=0.5),
                target=0.0,
                tolerance=1.0,
            )

    def test_invalid_parameters_and_audio_are_rejected(self) -> None:
        valid = _tone()
        operations = (
            lambda: analyze_temporal_balance(
                valid,
                valid,
                SAMPLE_RATE,
                object(),
                target_offset_db=0.0,
                tolerance_db=1.0,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                valid,
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=float("nan"),
                tolerance_db=1.0,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                valid,
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=0.0,
                tolerance_db=-0.1,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                valid,
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=1.0e308,
                tolerance_db=1.0e308,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                valid,
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=0.0,
                tolerance_db=1.0,
                minimum_shared_window_count=0,
            ),
            lambda: analyze_temporal_balance(
                valid,
                valid,
                7_999,
                CONFIG,
                target_offset_db=0.0,
                tolerance_db=1.0,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                np.zeros(32),
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=0.0,
                tolerance_db=1.0,
                minimum_shared_window_count=1,
            ),
            lambda: analyze_temporal_balance(
                np.full_like(valid, np.inf),
                valid,
                SAMPLE_RATE,
                CONFIG,
                target_offset_db=0.0,
                tolerance_db=1.0,
                minimum_shared_window_count=1,
            ),
        )
        for index, operation in enumerate(operations):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    operation()


if __name__ == "__main__":
    unittest.main()
