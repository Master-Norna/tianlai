"""Unit tests for the read-only collaboration-layer audio diagnostics."""

from __future__ import annotations

import json
import math
import unittest

import numpy as np

from tianlai.mix_analysis import (
    FREQUENCY_BANDS_HZ,
    MixAnalysisConfig,
    analyze_track,
    overlap_active_rms_difference,
)


SAMPLE_RATE = 48_000
HALF_AMPLITUDE_DB = -6.020599913279624


def _stereo_tone(
    frequency_hz: float,
    amplitude: float,
    seconds: float = 1.0,
    *,
    sample_rate: int = SAMPLE_RATE,
    antiphase: bool = False,
) -> np.ndarray:
    time = np.arange(round(seconds * sample_rate), dtype=np.float64) / sample_rate
    left = amplitude * np.sin(2.0 * np.pi * frequency_hz * time)
    right = -left if antiphase else left.copy()
    return np.column_stack((left, right))


class TrackAnalysisTests(unittest.TestCase):
    def test_defaults_are_400ms_windows_with_100ms_hop(self) -> None:
        config = MixAnalysisConfig()
        self.assertEqual(config.window_seconds, 0.4)
        self.assertEqual(config.hop_seconds, 0.1)
        self.assertEqual(config.frame_lengths(SAMPLE_RATE), (19_200, 4_800))

    def test_half_amplitude_is_minus_6_0206_db_in_every_level_metric(self) -> None:
        full = analyze_track(_stereo_tone(1_000.0, 0.8), SAMPLE_RATE)
        half = analyze_track(_stereo_tone(1_000.0, 0.4), SAMPLE_RATE)

        self.assertAlmostEqual(
            half.peak_dbfs - full.peak_dbfs,
            HALF_AMPLITUDE_DB,
            places=10,
        )
        self.assertAlmostEqual(
            half.rms_dbfs - full.rms_dbfs,
            HALF_AMPLITUDE_DB,
            places=10,
        )
        self.assertAlmostEqual(
            half.active_rms_dbfs - full.active_rms_dbfs,
            HALF_AMPLITUDE_DB,
            places=10,
        )
        self.assertAlmostEqual(
            half.crest_factor_db,
            full.crest_factor_db,
            places=12,
        )

    def test_sparse_active_rms_is_not_diluted_by_appended_silence(self) -> None:
        sample_rate = 8_000
        short = np.zeros((sample_rate, 2), dtype=np.float64)
        tone = _stereo_tone(
            500.0,
            0.3,
            0.5,
            sample_rate=sample_rate,
        )
        short[: tone.shape[0]] = tone
        long = np.concatenate(
            (short, np.zeros((sample_rate * 9, 2), dtype=np.float64)),
            axis=0,
        )

        short_result = analyze_track(short, sample_rate)
        long_result = analyze_track(long, sample_rate)

        self.assertAlmostEqual(
            short_result.active_rms_dbfs,
            long_result.active_rms_dbfs,
            places=12,
        )
        self.assertLess(long_result.rms_dbfs, short_result.rms_dbfs - 9.0)
        self.assertLess(long_result.active_ratio, short_result.active_ratio)

    def test_antiphase_stereo_is_active_and_has_full_normalized_width(self) -> None:
        result = analyze_track(
            _stereo_tone(1_000.0, 0.4, antiphase=True),
            SAMPLE_RATE,
        )

        self.assertIsNotNone(result.active_rms_dbfs)
        self.assertGreater(result.active_window_count, 0)
        self.assertAlmostEqual(result.stereo_correlation, -1.0, places=12)
        self.assertAlmostEqual(result.stereo_width, 1.0, places=12)
        self.assertGreater(
            dict(result.band_energy_ratios)["mid"],
            0.99,
        )

    def test_in_phase_stereo_is_zero_width(self) -> None:
        result = analyze_track(_stereo_tone(1_000.0, 0.4), SAMPLE_RATE)
        self.assertAlmostEqual(result.stereo_correlation, 1.0, places=12)
        self.assertAlmostEqual(result.stereo_width, 0.0, places=12)

    def test_six_frequency_bands_are_non_overlapping_and_cover_energy(self) -> None:
        cases = (
            ("sub_bass", 30.0),
            ("bass", 100.0),
            ("low_mid", 350.0),
            ("mid", 1_000.0),
            ("presence", 3_000.0),
            ("brilliance", 10_000.0),
        )
        self.assertEqual(
            [name for name, _low, _high in FREQUENCY_BANDS_HZ],
            [name for name, _frequency in cases],
        )
        for expected_band, frequency in cases:
            with self.subTest(band=expected_band):
                result = analyze_track(
                    _stereo_tone(frequency, 0.3),
                    SAMPLE_RATE,
                )
                ratios = dict(result.band_energy_ratios)
                self.assertEqual(len(ratios), 6)
                self.assertAlmostEqual(sum(ratios.values()), 1.0, places=12)
                self.assertGreater(ratios[expected_band], 0.98)
                self.assertAlmostEqual(
                    result.spectral_centroid_hz,
                    frequency,
                    delta=8.0,
                )

    def test_silence_uses_none_not_infinity_or_nan(self) -> None:
        result = analyze_track(np.zeros((8_000, 2)), 8_000)
        document = result.to_dict()

        self.assertEqual(result.sample_peak, 0.0)
        self.assertIsNone(result.peak_dbfs)
        self.assertIsNone(result.rms_dbfs)
        self.assertIsNone(result.active_rms_dbfs)
        self.assertIsNone(result.crest_factor_db)
        self.assertEqual(result.active_ratio, 0.0)
        self.assertIsNone(result.spectral_centroid_hz)
        self.assertIsNone(result.stereo_correlation)
        self.assertIsNone(result.stereo_width)
        self.assertTrue(
            all(value is None for value in document["band_energy_ratios"].values())
        )
        json.dumps(document, allow_nan=False)

    def test_analysis_is_deterministic_and_does_not_modify_input(self) -> None:
        random = np.random.default_rng(20260727)
        audio = random.standard_normal((12_000, 2)).astype(np.float64) * 0.05
        before = audio.copy()

        first = analyze_track(audio, SAMPLE_RATE)
        second = analyze_track(audio, SAMPLE_RATE)

        np.testing.assert_array_equal(audio, before)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first.to_dict(), sort_keys=True, allow_nan=False),
            json.dumps(second.to_dict(), sort_keys=True, allow_nan=False),
        )

    def test_to_dict_has_a_stable_explicit_contract(self) -> None:
        result = analyze_track(_stereo_tone(1_000.0, 0.25), SAMPLE_RATE)
        document = result.to_dict()
        self.assertEqual(
            list(document),
            [
                "analysis_version",
                "sample_rate_hz",
                "frame_count",
                "gate",
                "sample_peak",
                "peak_dbfs",
                "rms_dbfs",
                "active_rms_dbfs",
                "crest_factor_db",
                "active_ratio",
                "active_window_count",
                "window_count",
                "band_energy_ratios",
                "spectral_centroid_hz",
                "stereo_correlation",
                "stereo_width",
            ],
        )
        self.assertEqual(
            list(document["band_energy_ratios"]),
            [name for name, _low, _high in FREQUENCY_BANDS_HZ],
        )
        json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)


class OverlapComparisonTests(unittest.TestCase):
    def test_overlap_difference_is_positive_6_0206_db_for_half_level_second(self) -> None:
        first = _stereo_tone(800.0, 0.8)
        second = _stereo_tone(800.0, 0.4)
        result = overlap_active_rms_difference(
            first,
            second,
            SAMPLE_RATE,
        )

        self.assertGreater(result.shared_active_window_count, 0)
        self.assertAlmostEqual(
            result.first_minus_second_db,
            -HALF_AMPLITUDE_DB,
            places=10,
        )
        json.dumps(result.to_dict(), sort_keys=True, allow_nan=False)

    def test_non_overlapping_tracks_return_none_levels(self) -> None:
        sample_rate = 8_000
        first = np.zeros((sample_rate * 2, 2), dtype=np.float64)
        second = first.copy()
        first_tone = _stereo_tone(
            500.0,
            0.4,
            0.4,
            sample_rate=sample_rate,
        )
        second_tone = _stereo_tone(
            700.0,
            0.4,
            0.4,
            sample_rate=sample_rate,
        )
        first[: first_tone.shape[0]] = first_tone
        second[sample_rate : sample_rate + second_tone.shape[0]] = second_tone

        result = overlap_active_rms_difference(first, second, sample_rate)

        self.assertEqual(result.shared_active_window_count, 0)
        self.assertEqual(result.overlap_ratio, 0.0)
        self.assertIsNone(result.first_active_rms_dbfs)
        self.assertIsNone(result.second_active_rms_dbfs)
        self.assertIsNone(result.first_minus_second_db)

    def test_overlap_analysis_does_not_modify_either_input(self) -> None:
        first = _stereo_tone(440.0, 0.3)
        second = _stereo_tone(660.0, 0.2)
        first_before = first.copy()
        second_before = second.copy()

        overlap_active_rms_difference(first, second, SAMPLE_RATE)

        np.testing.assert_array_equal(first, first_before)
        np.testing.assert_array_equal(second, second_before)


class ValidationTests(unittest.TestCase):
    def test_invalid_gate_configurations_are_rejected(self) -> None:
        invalid = (
            {"window_seconds": 0.0},
            {"window_seconds": 0.019999},
            {"window_seconds": 2.000001},
            {"hop_seconds": 0.0},
            {"hop_seconds": 0.009999},
            {"hop_seconds": 0.5, "window_seconds": 0.4},
            {"absolute_gate_dbfs": 1.0},
            {"absolute_gate_dbfs": -301.0},
            {"relative_gate_db": 0.1},
            {"relative_gate_db": -301.0},
            {"window_seconds": float("nan")},
            {"hop_seconds": float("inf")},
            {"window_seconds": True},
            {"window_seconds": "0.4"},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    MixAnalysisConfig(**values)

    def test_window_workload_boundaries_are_accepted(self) -> None:
        minimum = MixAnalysisConfig(
            window_seconds=0.020,
            hop_seconds=0.010,
        )
        maximum = MixAnalysisConfig(
            window_seconds=2.000,
            hop_seconds=2.000,
        )

        self.assertEqual(minimum.frame_lengths(8_000), (160, 80))
        self.assertEqual(maximum.frame_lengths(48_000), (96_000, 96_000))

    def test_effective_window_and_input_contracts_are_rejected(self) -> None:
        audio = np.zeros((8_000, 2))
        cases = (
            (lambda: analyze_track(audio, 7_999), "sample_rate"),
            (lambda: analyze_track(audio, 8_000.0), "sample_rate"),
            (lambda: analyze_track(audio, True), "sample_rate"),
            (lambda: analyze_track(np.zeros(8_000), 8_000), "shape"),
            (lambda: analyze_track(np.zeros((8_000, 1)), 8_000), "shape"),
            (lambda: analyze_track(np.zeros((0, 2)), 8_000), "at least one"),
            (
                lambda: analyze_track(
                    np.array([[0.0, math.nan]]),
                    8_000,
                ),
                "non-finite",
            ),
            (lambda: analyze_track(audio, 8_000, config={}), "MixAnalysisConfig"),
        )
        for call, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    call()

    def test_overlap_requires_equal_timelines(self) -> None:
        with self.assertRaisesRegex(ValueError, "same frame count"):
            overlap_active_rms_difference(
                np.zeros((8_000, 2)),
                np.zeros((8_001, 2)),
                8_000,
            )


if __name__ == "__main__":
    unittest.main()
