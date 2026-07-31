from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from tianlai._window_batches import (
    WINDOW_BATCH_BUFFER_BYTES,
    WINDOW_BATCH_SIZE,
    _batch_capacity,
    accumulated_stereo_power_spectrum,
    selected_window_peak,
    window_rms,
)
from tianlai.mix_analysis import MixAnalysisConfig, analyze_track
from tianlai.spectral_overlap import analyze_spectral_overlap


SAMPLE_RATE = 8_000
WINDOW_FRAMES = 160
HOP_FRAMES = 80


def _padded_window(
    audio: np.ndarray,
    start: int,
) -> np.ndarray:
    result = np.zeros((WINDOW_FRAMES, 2), dtype=np.float64)
    available = audio[start : start + WINDOW_FRAMES]
    result[: available.shape[0]] = available
    return result


def _scalar_rms(audio: np.ndarray, starts: tuple[int, ...]) -> np.ndarray:
    result = np.empty(len(starts), dtype=np.float64)
    for index, start in enumerate(starts):
        window = _padded_window(audio, start)
        peak = float(np.max(np.abs(window)))
        if peak <= 0.0:
            result[index] = 0.0
        else:
            normalized = window / peak
            result[index] = peak * math.sqrt(
                float(
                    np.mean(
                        normalized * normalized,
                        dtype=np.float64,
                    )
                )
            )
    return result


def _scalar_spectrum(
    audio: np.ndarray,
    starts: tuple[int, ...],
    scale: float,
) -> np.ndarray:
    hann = np.hanning(WINDOW_FRAMES)[:, np.newaxis]
    accumulated = np.zeros(WINDOW_FRAMES // 2 + 1, dtype=np.float64)
    for start in starts:
        normalized = _padded_window(audio, start) / scale
        centered = normalized - np.mean(
            normalized,
            axis=0,
            keepdims=True,
        )
        spectra = np.fft.rfft(centered * hann, axis=0)
        accumulated += np.mean(
            np.square(np.abs(spectra)),
            axis=1,
        )
    return accumulated


class WindowBatchTests(unittest.TestCase):
    def test_cross_batch_tail_padding_matches_scalar_reference(
        self,
    ) -> None:
        frame_count = HOP_FRAMES * (WINDOW_BATCH_SIZE * 2 + 3) + 37
        starts = tuple(range(0, frame_count, HOP_FRAMES))
        self.assertGreater(len(starts), WINDOW_BATCH_SIZE * 2)
        self.assertLess(
            frame_count - starts[-1],
            WINDOW_FRAMES,
        )
        random = np.random.default_rng(20260727)
        source = (
            random.standard_normal((frame_count, 2)).astype(np.float32)
            * np.float32(0.05)
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long-track.float32"
            writable = np.memmap(
                path,
                dtype=np.float32,
                mode="w+",
                shape=source.shape,
            )
            writable[:] = source
            writable.flush()
            del writable
            audio = np.memmap(
                path,
                dtype=np.float32,
                mode="r",
                shape=source.shape,
            )
            expected_rms = _scalar_rms(audio, starts)
            selected = np.arange(len(starts), dtype=np.intp)
            expected_scale = max(
                float(np.max(np.abs(_padded_window(audio, start))))
                for start in starts
            )
            expected_spectrum = _scalar_spectrum(
                audio,
                starts,
                expected_scale,
            )

            first_rms = window_rms(audio, starts, WINDOW_FRAMES)
            second_rms = window_rms(audio, starts, WINDOW_FRAMES)
            scale = selected_window_peak(
                audio,
                starts,
                selected,
                WINDOW_FRAMES,
            )
            first_spectrum = accumulated_stereo_power_spectrum(
                audio,
                starts,
                selected,
                WINDOW_FRAMES,
                scale,
            )
            second_spectrum = accumulated_stereo_power_spectrum(
                audio,
                starts,
                selected,
                WINDOW_FRAMES,
                scale,
            )

            np.testing.assert_allclose(
                first_rms,
                expected_rms,
                rtol=1.0e-15,
                atol=0.0,
            )
            np.testing.assert_array_equal(first_rms, second_rms)
            self.assertEqual(scale, expected_scale)
            np.testing.assert_allclose(
                first_spectrum,
                expected_spectrum,
                rtol=2.0e-15,
                atol=1.0e-15,
            )
            np.testing.assert_array_equal(
                first_spectrum,
                second_spectrum,
            )
            np.testing.assert_array_equal(audio, source)

            config = MixAnalysisConfig(
                window_seconds=WINDOW_FRAMES / SAMPLE_RATE,
                hop_seconds=HOP_FRAMES / SAMPLE_RATE,
                absolute_gate_dbfs=-100.0,
                relative_gate_db=-80.0,
            )
            first_result = analyze_spectral_overlap(
                audio,
                audio,
                SAMPLE_RATE,
                config,
            )
            second_result = analyze_spectral_overlap(
                audio,
                audio,
                SAMPLE_RATE,
                config,
            )
            self.assertEqual(first_result, second_result)
            self.assertEqual(
                first_result.to_dict(),
                second_result.to_dict(),
            )
            first_track = analyze_track(audio, SAMPLE_RATE, config)
            second_track = analyze_track(audio, SAMPLE_RATE, config)
            self.assertEqual(first_track, second_track)
            self.assertEqual(
                first_track.to_dict(),
                second_track.to_dict(),
            )
            np.testing.assert_array_equal(audio, source)
            del audio

    def test_fft_calls_are_per_batch_and_buffer_cap_is_explicit(
        self,
    ) -> None:
        window_count = WINDOW_BATCH_SIZE * 2 + 1
        frame_count = HOP_FRAMES * (window_count - 1) + 1
        audio = np.ones((frame_count, 2), dtype=np.float32)
        starts = tuple(range(0, frame_count, HOP_FRAMES))
        indices = np.arange(len(starts), dtype=np.intp)
        original_rfft = np.fft.rfft

        with mock.patch(
            "tianlai._window_batches.np.fft.rfft",
            wraps=original_rfft,
        ) as batched_rfft:
            accumulated_stereo_power_spectrum(
                audio,
                starts,
                indices,
                WINDOW_FRAMES,
                1.0,
            )

        expected_calls = math.ceil(
            len(starts) / _batch_capacity(WINDOW_FRAMES)
        )
        self.assertEqual(batched_rfft.call_count, expected_calls)
        self.assertLess(batched_rfft.call_count, len(starts))

        large_window_frames = WINDOW_BATCH_BUFFER_BYTES // 16
        capacity = _batch_capacity(large_window_frames)
        allocated_bytes = capacity * large_window_frames * 2 * 8
        self.assertLessEqual(
            allocated_bytes,
            WINDOW_BATCH_BUFFER_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
