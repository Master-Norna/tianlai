"""Bounded-memory window batches shared by audio diagnostics.

The public analyzers use at most ``WINDOW_BATCH_SIZE`` windows per NumPy/FFT
call.  The float64 input batch is additionally capped at
``WINDOW_BATCH_BUFFER_BYTES``; a single unusually large window is the only
possible floor above that cap.  With the accompanying complex FFT and real
power buffers, normal peak scratch memory remains in the tens of MiB rather
than growing with track duration.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import math

import numpy as np


WINDOW_BATCH_SIZE = 16
WINDOW_BATCH_BUFFER_BYTES = 8 * 1024 * 1024
_CHANNEL_COUNT = 2
_FLOAT64_BYTES = np.dtype(np.float64).itemsize


def _batch_capacity(window_frames: int) -> int:
    if window_frames < 1:
        raise ValueError("window_frames must be positive")
    bytes_per_window = window_frames * _CHANNEL_COUNT * _FLOAT64_BYTES
    memory_limited = max(1, WINDOW_BATCH_BUFFER_BYTES // bytes_per_window)
    return min(WINDOW_BATCH_SIZE, memory_limited)


def iter_window_batches(
    audio: np.ndarray,
    starts: Sequence[int],
    window_frames: int,
    *,
    indices: np.ndarray | None = None,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield ``(window_indices, float64_windows)`` with zero-padded tails.

    Only the current batch is copied from ``audio``.  Consequently a float32
    array or memory map is never converted into a track-sized float64 buffer.
    """

    if indices is None:
        selected = np.arange(len(starts), dtype=np.intp)
    else:
        selected = np.asarray(indices, dtype=np.intp)
        if selected.ndim != 1:
            raise ValueError("window indices must be one-dimensional")
    capacity = _batch_capacity(window_frames)
    frame_count = int(audio.shape[0])
    starts_array = np.asarray(starts, dtype=np.int64)
    frame_offsets = np.arange(window_frames, dtype=np.int64)
    for offset in range(0, int(selected.size), capacity):
        batch_indices = selected[offset : offset + capacity]
        frame_indices = (
            starts_array[batch_indices, np.newaxis]
            + frame_offsets[np.newaxis, :]
        )
        valid = frame_indices < frame_count
        np.minimum(frame_indices, frame_count - 1, out=frame_indices)
        windows = np.asarray(audio[frame_indices], dtype=np.float64)
        windows[~valid] = 0.0
        if not bool(np.all(np.isfinite(windows))):
            raise ValueError("frames contain non-finite samples")
        yield batch_indices, windows


def window_rms(
    audio: np.ndarray,
    starts: Sequence[int],
    window_frames: int,
) -> np.ndarray:
    """Return overflow-resistant stereo RMS for every zero-padded window."""

    levels = np.empty(len(starts), dtype=np.float64)
    for indices, windows in iter_window_batches(
        audio,
        starts,
        window_frames,
    ):
        peaks = np.max(np.abs(windows), axis=(1, 2))
        nonzero = peaks > 0.0
        if bool(np.any(nonzero)):
            windows[nonzero] /= peaks[nonzero, np.newaxis, np.newaxis]
        np.square(windows, out=windows)
        mean_squares = np.mean(
            windows,
            axis=(1, 2),
            dtype=np.float64,
        )
        batch_levels = peaks * np.sqrt(mean_squares)
        levels[indices] = batch_levels
    if not bool(np.all(np.isfinite(levels))):
        raise ValueError("audio energy exceeds the finite analysis range")
    return levels


def selected_window_peak(
    audio: np.ndarray,
    starts: Sequence[int],
    indices: np.ndarray,
    window_frames: int,
) -> float:
    """Find one peak scale without retaining any selected windows."""

    peak = 0.0
    for _batch_indices, windows in iter_window_batches(
        audio,
        starts,
        window_frames,
        indices=indices,
    ):
        peak = max(peak, float(np.max(np.abs(windows))))
    return peak


def accumulated_stereo_power_spectrum(
    audio: np.ndarray,
    starts: Sequence[int],
    indices: np.ndarray,
    window_frames: int,
    scale: float,
) -> np.ndarray:
    """Accumulate centered, Hann-windowed stereo power in FFT batches."""

    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("spectral scale must be finite and positive")
    window = np.hanning(window_frames)[np.newaxis, :, np.newaxis]
    accumulated = np.zeros(window_frames // 2 + 1, dtype=np.float64)
    for _batch_indices, windows in iter_window_batches(
        audio,
        starts,
        window_frames,
        indices=indices,
    ):
        windows /= scale
        windows -= np.mean(
            windows,
            axis=1,
            keepdims=True,
            dtype=np.float64,
        )
        windows *= window
        spectra = np.fft.rfft(windows, axis=1)
        magnitudes = np.abs(spectra)
        np.square(magnitudes, out=magnitudes)
        accumulated += (
            np.sum(magnitudes, axis=(0, 2), dtype=np.float64) * 0.5
        )
    if not bool(np.all(np.isfinite(accumulated))):
        raise ValueError("spectral energy exceeds the finite analysis range")
    return accumulated


__all__ = (
    "WINDOW_BATCH_BUFFER_BYTES",
    "WINDOW_BATCH_SIZE",
    "accumulated_stereo_power_spectrum",
    "iter_window_batches",
    "selected_window_peak",
    "window_rms",
)
