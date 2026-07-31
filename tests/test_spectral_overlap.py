from __future__ import annotations

import json
import math
import unittest

import numpy as np

from tianlai.mix_analysis import (
    FREQUENCY_BANDS_HZ,
    MixAnalysisConfig,
)
from tianlai.spectral_overlap import (
    SPECTRAL_OVERLAP_FORMAT,
    SPECTRAL_OVERLAP_VERSION,
    analyze_spectral_overlap,
)


SAMPLE_RATE = 8_000


def _config(sample_rate: int = SAMPLE_RATE) -> MixAnalysisConfig:
    del sample_rate
    return MixAnalysisConfig(
        window_seconds=0.200,
        hop_seconds=0.100,
        absolute_gate_dbfs=-100.0,
        relative_gate_db=-80.0,
    )


def _tone(
    frequency_hz: float,
    *,
    amplitude: float = 0.25,
    seconds: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    antiphase: bool = False,
) -> np.ndarray:
    time = np.arange(round(seconds * sample_rate)) / sample_rate
    mono = amplitude * np.sin(2.0 * np.pi * frequency_hz * time)
    right = -mono if antiphase else mono
    return np.column_stack((mono, right)).astype(np.float64)


def _all_null(mapping: tuple[tuple[str, float | None], ...]) -> bool:
    return all(value is None for _name, value in mapping)


class SpectralOverlapTests(unittest.TestCase):
    def test_same_spectrum_ignores_overall_level_for_overlap_coefficient(
        self,
    ) -> None:
        first = _tone(440.0, amplitude=0.25)
        second = _tone(440.0, amplitude=0.5)

        result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )

        self.assertEqual(result.shared_active_window_count, 10)
        self.assertEqual(result.window_count, 10)
        self.assertEqual(result.overlap_ratio, 1.0)
        self.assertAlmostEqual(result.spectral_overlap_coefficient, 1.0)
        first_ratios = dict(result.first_band_energy_ratios)
        second_ratios = dict(result.second_band_energy_ratios)
        self.assertAlmostEqual(
            math.fsum(float(value) for value in first_ratios.values()),
            1.0,
        )
        for name, _low, _high in FREQUENCY_BANDS_HZ:
            self.assertAlmostEqual(
                first_ratios[name],
                second_ratios[name],
            )

    def test_half_amplitude_is_minus_six_db_in_occupied_band(self) -> None:
        first = _tone(440.0, amplitude=0.25)
        second = _tone(440.0, amplitude=0.5)

        result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )
        differences = dict(result.band_first_minus_second_db)

        self.assertAlmostEqual(
            differences["low_mid"],
            -6.020599913279624,
            places=9,
        )
        # At 8 kHz the brilliance band starts above Nyquist.  Its ratio is
        # exactly zero, while a level difference is explicitly inconclusive.
        self.assertEqual(
            dict(result.first_band_energy_ratios)["brilliance"],
            0.0,
        )
        self.assertIsNone(differences["brilliance"])

    def test_antiphase_stereo_remains_active_and_spectral(self) -> None:
        first = _tone(440.0, antiphase=True)
        second = first.copy()

        result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )

        self.assertGreater(result.shared_active_window_count, 0)
        self.assertGreater(
            dict(result.first_band_energy_ratios)["low_mid"],
            0.99,
        )
        self.assertAlmostEqual(result.spectral_overlap_coefficient, 1.0)

    def test_different_frequency_regions_have_low_overlap(self) -> None:
        sample_rate = 16_000
        first = _tone(
            100.0,
            sample_rate=sample_rate,
        )
        second = _tone(
            3_000.0,
            sample_rate=sample_rate,
        )

        result = analyze_spectral_overlap(
            first,
            second,
            sample_rate,
            _config(sample_rate),
        )

        self.assertIsNotNone(result.spectral_overlap_coefficient)
        self.assertLess(result.spectral_overlap_coefficient, 0.01)
        self.assertGreater(
            dict(result.first_band_energy_ratios)["bass"],
            0.99,
        )
        self.assertGreater(
            dict(result.second_band_energy_ratios)["presence"],
            0.99,
        )

    def test_silence_returns_null_spectral_results(self) -> None:
        silence = np.zeros((SAMPLE_RATE, 2), dtype=np.float64)

        result = analyze_spectral_overlap(
            silence,
            silence,
            SAMPLE_RATE,
            _config(),
        )

        self.assertEqual(result.shared_active_window_count, 0)
        self.assertEqual(result.overlap_ratio, 0.0)
        self.assertTrue(_all_null(result.first_band_energy_ratios))
        self.assertTrue(_all_null(result.second_band_energy_ratios))
        self.assertTrue(_all_null(result.band_first_minus_second_db))
        self.assertIsNone(result.spectral_overlap_coefficient)

    def test_nonoverlapping_activity_returns_all_null_metrics(self) -> None:
        frame_count = SAMPLE_RATE * 3
        first = np.zeros((frame_count, 2), dtype=np.float64)
        second = np.zeros_like(first)
        first[: SAMPLE_RATE // 2] = _tone(220.0, seconds=0.5)
        second[-SAMPLE_RATE // 2 :] = _tone(880.0, seconds=0.5)

        result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )

        self.assertEqual(result.shared_active_window_count, 0)
        self.assertTrue(_all_null(result.first_band_energy_ratios))
        self.assertTrue(_all_null(result.second_band_energy_ratios))
        self.assertTrue(_all_null(result.band_first_minus_second_db))
        self.assertIsNone(result.spectral_overlap_coefficient)

    def test_track_with_no_ac_band_energy_keeps_differences_null(self) -> None:
        constant = np.full((SAMPLE_RATE, 2), 0.25, dtype=np.float64)
        config = MixAnalysisConfig(
            window_seconds=0.200,
            hop_seconds=0.200,
            absolute_gate_dbfs=-100.0,
            relative_gate_db=-80.0,
        )

        result = analyze_spectral_overlap(
            constant,
            _tone(440.0),
            SAMPLE_RATE,
            config,
        )

        self.assertGreater(result.shared_active_window_count, 0)
        self.assertTrue(_all_null(result.first_band_energy_ratios))
        self.assertTrue(_all_null(result.band_first_minus_second_db))
        self.assertIsNone(result.spectral_overlap_coefficient)

    def test_input_is_unchanged_and_result_is_deterministic(self) -> None:
        first = _tone(220.0)
        second = _tone(880.0)
        first_before = first.copy()
        second_before = second.copy()

        first_result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )
        second_result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )

        np.testing.assert_array_equal(first, first_before)
        np.testing.assert_array_equal(second, second_before)
        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result.to_dict(), second_result.to_dict())

    def test_large_finite_samples_still_produce_finite_json(self) -> None:
        first = _tone(440.0) * 1.0e300
        second = first.copy()

        result = analyze_spectral_overlap(
            first,
            second,
            SAMPLE_RATE,
            _config(),
        )
        encoded = result.to_dict()

        self.assertAlmostEqual(result.spectral_overlap_coefficient, 1.0)
        self.assertAlmostEqual(
            dict(result.band_first_minus_second_db)["low_mid"],
            0.0,
        )
        json.dumps(encoded, ensure_ascii=False, allow_nan=False)

    def test_to_dict_has_stable_identity_config_and_notice(self) -> None:
        config = _config()
        result = analyze_spectral_overlap(
            _tone(220.0),
            _tone(440.0),
            SAMPLE_RATE,
            config,
        )
        document = result.to_dict()

        self.assertEqual(document["format"], SPECTRAL_OVERLAP_FORMAT)
        self.assertEqual(document["version"], SPECTRAL_OVERLAP_VERSION)
        self.assertEqual(document["gate"], config.to_dict())
        self.assertFalse(document["audio_modified"])
        self.assertIn("masking", document["notice"])
        self.assertIn("LUFS", document["notice"])
        self.assertEqual(
            list(document["first_band_energy_ratios"]),
            [name for name, _low, _high in FREQUENCY_BANDS_HZ],
        )
        self.assertEqual(
            list(document),
            [
                "format",
                "version",
                "metric",
                "sample_rate_hz",
                "frame_count",
                "gate",
                "shared_active_window_count",
                "window_count",
                "overlap_ratio",
                "first_band_energy_ratios",
                "second_band_energy_ratios",
                "band_first_minus_second_db",
                "spectral_overlap_coefficient",
                "audio_modified",
                "notice",
            ],
        )

    def test_same_timeline_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "same frame count"):
            analyze_spectral_overlap(
                _tone(220.0, seconds=1.0),
                _tone(220.0, seconds=0.5),
                SAMPLE_RATE,
                _config(),
            )

    def test_invalid_inputs_and_configuration_are_rejected(self) -> None:
        valid = _tone(440.0)
        cases = (
            (
                "shape",
                lambda: analyze_spectral_overlap(
                    np.zeros(32),
                    valid,
                    SAMPLE_RATE,
                    _config(),
                ),
            ),
            (
                "nonfinite",
                lambda: analyze_spectral_overlap(
                    np.full_like(valid, np.nan),
                    valid,
                    SAMPLE_RATE,
                    _config(),
                ),
            ),
            (
                "config",
                lambda: analyze_spectral_overlap(
                    valid,
                    valid,
                    SAMPLE_RATE,
                    object(),
                ),
            ),
            (
                "sample_rate",
                lambda: analyze_spectral_overlap(
                    valid,
                    valid,
                    7_999,
                    _config(),
                ),
            ),
        )
        for label, operation in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    operation()


if __name__ == "__main__":
    unittest.main()
