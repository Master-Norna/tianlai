from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tianlai.stereo_stage_metrics import (
    STEREO_STAGE_METRICS_FORMAT,
    STEREO_STAGE_METRICS_VERSION,
    StereoStageMetrics,
    TailWindowMetrics,
    analyze_stereo_stage,
)


SAMPLE_RATE = 8_000


def _tone(
    *,
    amplitude: float = 1.0,
    seconds: float = 1.0,
    antiphase: bool = False,
    dtype: np.dtype = np.dtype(np.float64),
) -> np.ndarray:
    time = np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 1_000.0 * time)
    right = -mono if antiphase else mono
    return np.column_stack((mono, right)).astype(dtype)


class StereoStageMetricsTests(unittest.TestCase):
    def test_in_phase_stereo_reports_sample_peak_rms_and_center(self) -> None:
        result = analyze_stereo_stage(_tone(), SAMPLE_RATE)

        self.assertIsInstance(result, StereoStageMetrics)
        self.assertEqual(result.sample_rate_hz, SAMPLE_RATE)
        self.assertEqual(result.sample_rate, SAMPLE_RATE)
        self.assertEqual(result.frame_count, SAMPLE_RATE)
        self.assertAlmostEqual(result.left_peak, 1.0, places=12)
        self.assertAlmostEqual(result.right_peak, 1.0, places=12)
        self.assertAlmostEqual(result.sample_peak, 1.0, places=12)
        self.assertAlmostEqual(result.left_peak_dbfs, 0.0, places=12)
        self.assertAlmostEqual(result.right_peak_dbfs, 0.0, places=12)
        self.assertAlmostEqual(result.sample_peak_dbfs, 0.0, places=12)
        self.assertAlmostEqual(
            result.stereo_rms_dbfs,
            -3.0102999566,
            places=9,
        )
        self.assertAlmostEqual(
            result.mono_fold_rms_dbfs,
            result.stereo_rms_dbfs,
            places=12,
        )
        self.assertAlmostEqual(result.mono_fold_delta_db, 0.0, places=12)
        self.assertFalse(result.mono_fold_silent)
        self.assertEqual(result.normalized_ms_width, 0.0)
        self.assertAlmostEqual(result.left_right_correlation, 1.0, places=12)
        self.assertIsNone(result.tail_window)

    def test_antiphase_stays_audible_but_mono_fold_is_explicit_null(
        self,
    ) -> None:
        result = analyze_stereo_stage(
            _tone(antiphase=True),
            SAMPLE_RATE,
        )

        self.assertAlmostEqual(
            result.stereo_rms_dbfs,
            -3.0102999566,
            places=9,
        )
        self.assertTrue(result.mono_fold_silent)
        self.assertIsNone(result.mono_fold_rms_dbfs)
        self.assertIsNone(result.mono_fold_delta_db)
        self.assertEqual(result.normalized_ms_width, 1.0)
        self.assertAlmostEqual(
            result.left_right_correlation,
            -1.0,
            places=12,
        )

    def test_one_sided_audio_counts_two_stereo_channels(self) -> None:
        stereo = _tone()
        stereo[:, 1] = 0.0

        result = analyze_stereo_stage(stereo, SAMPLE_RATE)

        self.assertAlmostEqual(result.left_peak, 1.0, places=12)
        self.assertEqual(result.right_peak, 0.0)
        self.assertIsNone(result.right_peak_dbfs)
        self.assertAlmostEqual(
            result.stereo_rms_dbfs,
            -6.0205999133,
            places=9,
        )
        self.assertAlmostEqual(
            result.mono_fold_rms_dbfs,
            -9.0308998699,
            places=9,
        )
        self.assertAlmostEqual(
            result.mono_fold_delta_db,
            -3.0102999566,
            places=9,
        )
        self.assertAlmostEqual(
            result.normalized_ms_width,
            1.0 / math.sqrt(2.0),
            places=12,
        )
        self.assertIsNone(result.left_right_correlation)

    def test_silence_uses_null_instead_of_non_finite_numbers(self) -> None:
        silence = np.zeros((SAMPLE_RATE, 2), dtype=np.float64)

        result = analyze_stereo_stage(
            silence,
            SAMPLE_RATE,
            tail_window_seconds=0.25,
        )
        document = result.to_dict()

        self.assertEqual(result.left_peak, 0.0)
        self.assertEqual(result.right_peak, 0.0)
        self.assertEqual(result.sample_peak, 0.0)
        self.assertIsNone(result.left_peak_dbfs)
        self.assertIsNone(result.right_peak_dbfs)
        self.assertIsNone(result.sample_peak_dbfs)
        self.assertIsNone(result.stereo_rms_dbfs)
        self.assertIsNone(result.mono_fold_rms_dbfs)
        self.assertIsNone(result.mono_fold_delta_db)
        self.assertTrue(result.mono_fold_silent)
        self.assertIsNone(result.normalized_ms_width)
        self.assertIsNone(result.left_right_correlation)
        self.assertIsNotNone(result.tail_window)
        assert result.tail_window is not None
        self.assertTrue(result.tail_window.silent)
        self.assertIsNone(result.tail_window.sample_peak_dbfs)
        self.assertIsNone(result.tail_window.stereo_rms_dbfs)
        self.assertIsNone(
            result.tail_window.peak_relative_to_full_track_db
        )
        json.dumps(document, ensure_ascii=False, allow_nan=False)

    def test_tail_window_reports_level_and_peak_relative_to_full_track(
        self,
    ) -> None:
        audio = _tone()
        tail_frames = SAMPLE_RATE // 4
        audio[-tail_frames:] *= 0.1

        result = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=0.25,
        )

        self.assertIsInstance(result.tail_window, TailWindowMetrics)
        assert result.tail_window is not None
        tail = result.tail_window
        self.assertEqual(tail.effective_frame_count, tail_frames)
        self.assertEqual(tail.effective_seconds, 0.25)
        self.assertAlmostEqual(tail.sample_peak, 0.1, places=12)
        self.assertAlmostEqual(tail.sample_peak_dbfs, -20.0, places=9)
        self.assertAlmostEqual(
            tail.stereo_rms_dbfs,
            -23.0102999566,
            places=9,
        )
        self.assertAlmostEqual(
            tail.peak_relative_to_full_track_db,
            -20.0,
            places=9,
        )
        self.assertFalse(tail.silent)

    def test_tail_longer_than_track_is_clamped_to_full_track(self) -> None:
        audio = _tone(seconds=0.25)

        result = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=10.0,
        )

        assert result.tail_window is not None
        self.assertEqual(
            result.tail_window.effective_frame_count,
            audio.shape[0],
        )
        self.assertEqual(result.tail_window.effective_seconds, 0.25)
        self.assertAlmostEqual(
            result.tail_window.stereo_rms_dbfs,
            result.stereo_rms_dbfs,
            places=12,
        )
        self.assertEqual(
            result.tail_window.peak_relative_to_full_track_db,
            0.0,
        )

    def test_tail_uses_its_own_scale_across_extreme_dynamic_range(
        self,
    ) -> None:
        audio = np.full((16, 2), 1.0e-300, dtype=np.float64)
        audio[0] = 1.0e300

        result = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=15.0 / SAMPLE_RATE,
        )

        assert result.tail_window is not None
        self.assertAlmostEqual(
            result.tail_window.sample_peak_dbfs,
            -6_000.0,
            places=6,
        )
        self.assertAlmostEqual(
            result.tail_window.stereo_rms_dbfs,
            -6_000.0,
            places=6,
        )
        self.assertAlmostEqual(
            result.tail_window.peak_relative_to_full_track_db,
            -12_000.0,
            places=6,
        )

    def test_huge_finite_samples_remain_finite_and_json_safe(self) -> None:
        unit = _tone(amplitude=0.5)
        audio = unit * 2.0e300

        result = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=0.1,
        )
        document = result.to_dict()

        self.assertAlmostEqual(result.sample_peak_dbfs, 6_000.0, places=6)
        self.assertTrue(math.isfinite(result.stereo_rms_dbfs))
        self.assertTrue(math.isfinite(result.mono_fold_rms_dbfs))
        self.assertTrue(math.isfinite(result.left_right_correlation))
        json.dumps(document, ensure_ascii=False, allow_nan=False)

    def test_float32_input_is_unchanged_and_deterministic(self) -> None:
        audio = _tone(amplitude=0.25, dtype=np.dtype(np.float32))
        before = audio.copy()
        audio.flags.writeable = False

        first = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=0.125,
        )
        second = analyze_stereo_stage(
            audio,
            SAMPLE_RATE,
            tail_window_seconds=0.125,
        )

        np.testing.assert_array_equal(audio, before)
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        with self.assertRaises(FrozenInstanceError):
            first.frame_count = 1  # type: ignore[misc]

    def test_read_only_memmap_is_supported_without_modification(self) -> None:
        source = _tone(
            amplitude=0.375,
            seconds=10.0,
            dtype=np.dtype(np.float32),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stereo.float32"
            writable = np.memmap(
                path,
                dtype=np.float32,
                mode="w+",
                shape=source.shape,
            )
            writable[:] = source
            writable.flush()
            del writable
            mapped = np.memmap(
                path,
                dtype=np.float32,
                mode="r",
                shape=source.shape,
            )

            result = analyze_stereo_stage(
                mapped,
                SAMPLE_RATE,
                tail_window_seconds=0.5,
            )

            self.assertEqual(result.frame_count, source.shape[0])
            self.assertAlmostEqual(
                result.sample_peak,
                0.375,
                places=7,
            )
            np.testing.assert_array_equal(mapped, source)
            del mapped

    def test_json_contract_is_fixed_order_and_names_measurement_limits(
        self,
    ) -> None:
        result = analyze_stereo_stage(_tone(), SAMPLE_RATE)
        first = result.to_dict()
        second = result.to_dict()

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(first),
            (
                "format",
                "version",
                "metric",
                "sample_rate_hz",
                "frame_count",
                "left_peak",
                "left_peak_dbfs",
                "right_peak",
                "right_peak_dbfs",
                "sample_peak",
                "sample_peak_dbfs",
                "stereo_rms_dbfs",
                "mono_fold_rms_dbfs",
                "mono_fold_delta_db",
                "mono_fold_delta_definition",
                "mono_fold_silent",
                "normalized_ms_width",
                "left_right_correlation",
                "tail_window",
                "audio_modified",
                "notice",
            ),
        )
        self.assertEqual(first["format"], STEREO_STAGE_METRICS_FORMAT)
        self.assertEqual(first["version"], STEREO_STAGE_METRICS_VERSION)
        self.assertEqual(
            first["mono_fold_delta_definition"],
            "mono_fold_minus_stereo_db",
        )
        self.assertFalse(first["audio_modified"])
        self.assertIn("sample-peak", first["notice"])
        self.assertIn("RMS", first["notice"])
        self.assertIn("true-peak", first["notice"])
        self.assertIn("LUFS", first["notice"])
        json.dumps(first, ensure_ascii=False, allow_nan=False)

    def test_invalid_audio_sample_rate_and_tail_are_rejected(self) -> None:
        valid = _tone()
        invalid_operations = (
            lambda: analyze_stereo_stage(np.zeros(16), SAMPLE_RATE),
            lambda: analyze_stereo_stage(np.zeros((16, 1)), SAMPLE_RATE),
            lambda: analyze_stereo_stage(np.zeros((0, 2)), SAMPLE_RATE),
            lambda: analyze_stereo_stage(
                np.zeros((16, 2), dtype=np.complex128),
                SAMPLE_RATE,
            ),
            lambda: analyze_stereo_stage(
                np.zeros((16, 2), dtype=np.bool_),
                SAMPLE_RATE,
            ),
            lambda: analyze_stereo_stage(
                np.full((16, 2), "0.1", dtype=object),
                SAMPLE_RATE,
            ),
            lambda: analyze_stereo_stage(
                np.full((16, 2), np.nan),
                SAMPLE_RATE,
            ),
            lambda: analyze_stereo_stage(
                np.full((16, 2), np.inf),
                SAMPLE_RATE,
            ),
            lambda: analyze_stereo_stage(valid, True),
            lambda: analyze_stereo_stage(valid, 7_999),
            lambda: analyze_stereo_stage(valid, 384_001),
            lambda: analyze_stereo_stage(
                valid,
                SAMPLE_RATE,
                tail_window_seconds=True,
            ),
            lambda: analyze_stereo_stage(
                valid,
                SAMPLE_RATE,
                tail_window_seconds=0.0,
            ),
            lambda: analyze_stereo_stage(
                valid,
                SAMPLE_RATE,
                tail_window_seconds=-1.0,
            ),
            lambda: analyze_stereo_stage(
                valid,
                SAMPLE_RATE,
                tail_window_seconds=float("nan"),
            ),
            lambda: analyze_stereo_stage(
                valid,
                SAMPLE_RATE,
                tail_window_seconds=0.1 / SAMPLE_RATE,
            ),
        )
        for index, operation in enumerate(invalid_operations):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    operation()


if __name__ == "__main__":
    unittest.main()
