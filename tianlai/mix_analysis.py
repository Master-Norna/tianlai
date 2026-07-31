"""Deterministic, read-only diagnostics for one rendered stereo stem.

This module measures audio; it never changes gain, equalisation, dynamics or
the caller's input buffer.  The active-level metric is deliberately named
``active_rms_dbfs`` rather than LUFS: it is a transparent windowed RMS gate,
not an implementation of ITU-R BS.1770.

Activity is measured in overlapping windows (400 ms with a 100 ms hop by
default).  A window is active when its stereo RMS is above both an absolute
gate and a gate relative to the loudest window in that track.  Stereo energy
is calculated from the two channels independently, never from ``L + R``;
therefore a valid antiphase signal cannot disappear during analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any, Sequence

import numpy as np

from ._window_batches import (
    accumulated_stereo_power_spectrum,
    selected_window_peak,
    window_rms,
)


ANALYSIS_VERSION = 1
_AUDIO_CHUNK_FRAMES = 262_144
MIN_ANALYSIS_WINDOW_SECONDS = 0.020
MAX_ANALYSIS_WINDOW_SECONDS = 2.000
MIN_ANALYSIS_HOP_SECONDS = 0.010

# Six fixed, non-overlapping bands.  ``None`` means the current sample-rate
# Nyquist frequency.  Names and order are part of the serialized contract.
FREQUENCY_BANDS_HZ: tuple[tuple[str, float, float | None], ...] = (
    ("sub_bass", 0.0, 60.0),
    ("bass", 60.0, 250.0),
    ("low_mid", 250.0, 500.0),
    ("mid", 500.0, 2_000.0),
    ("presence", 2_000.0, 6_000.0),
    ("brilliance", 6_000.0, None),
)


def _finite_real(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _dbfs(amplitude: float) -> float | None:
    """Return amplitude dBFS, using ``None`` for exact digital silence."""

    if amplitude <= 0.0:
        return None
    return float(20.0 * math.log10(amplitude))


def _serialized_float(
    value: float | None,
    digits: int,
) -> float | None:
    """Quantize diagnostic JSON so insignificant FFT tails do not churn hashes."""

    if value is None:
        return None
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0.0 else rounded


def _sample_peak(audio: np.ndarray) -> float:
    peak = 0.0
    for start in range(0, int(audio.shape[0]), _AUDIO_CHUNK_FRAMES):
        chunk = audio[start : start + _AUDIO_CHUNK_FRAMES]
        peak = max(peak, float(np.max(np.abs(chunk))))
    return peak


def _rms(audio: np.ndarray, peak: float | None = None) -> float:
    """Overflow-resistant, bounded-memory RMS over every sample."""

    if peak is None:
        peak = _sample_peak(audio)
    if peak == 0.0:
        return 0.0
    square_sums: list[float] = []
    for start in range(0, int(audio.shape[0]), _AUDIO_CHUNK_FRAMES):
        chunk = np.asarray(
            audio[start : start + _AUDIO_CHUNK_FRAMES],
            dtype=np.float64,
        )
        normalized = chunk / peak
        square_sums.append(
            float(np.sum(normalized * normalized, dtype=np.float64))
        )
    mean_square = math.fsum(square_sums) / int(audio.size)
    return float(peak * math.sqrt(mean_square))


@dataclass(frozen=True, slots=True)
class MixAnalysisConfig:
    """Window and gate parameters for active-RMS diagnostics."""

    window_seconds: float = 0.400
    hop_seconds: float = 0.100
    absolute_gate_dbfs: float = -70.0
    relative_gate_db: float = -40.0

    def __post_init__(self) -> None:
        for field_name in (
            "window_seconds",
            "hop_seconds",
            "absolute_gate_dbfs",
            "relative_gate_db",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_real(getattr(self, field_name), field_name),
            )
        if not (
            MIN_ANALYSIS_WINDOW_SECONDS
            <= self.window_seconds
            <= MAX_ANALYSIS_WINDOW_SECONDS
        ):
            raise ValueError(
                "window_seconds must be between "
                f"{MIN_ANALYSIS_WINDOW_SECONDS:g} and "
                f"{MAX_ANALYSIS_WINDOW_SECONDS:g}"
            )
        if self.hop_seconds < MIN_ANALYSIS_HOP_SECONDS:
            raise ValueError(
                "hop_seconds must be at least "
                f"{MIN_ANALYSIS_HOP_SECONDS:g}"
            )
        if self.hop_seconds > self.window_seconds:
            raise ValueError("hop_seconds must not exceed window_seconds")
        if not -300.0 <= self.absolute_gate_dbfs <= 0.0:
            raise ValueError("absolute_gate_dbfs must be between -300 and 0")
        if not -300.0 <= self.relative_gate_db <= 0.0:
            raise ValueError("relative_gate_db must be between -300 and 0")

    def frame_lengths(self, sample_rate: int) -> tuple[int, int]:
        sample_rate = _validate_sample_rate(sample_rate)
        window_frames = round(self.window_seconds * sample_rate)
        hop_frames = round(self.hop_seconds * sample_rate)
        if window_frames < 8:
            raise ValueError(
                "window_seconds resolves to fewer than 8 frames at this sample rate"
            )
        if hop_frames < 1:
            raise ValueError(
                "hop_seconds resolves to fewer than 1 frame at this sample rate"
            )
        if hop_frames > window_frames:
            raise ValueError(
                "rounded hop length must not exceed rounded window length"
            )
        return window_frames, hop_frames

    def to_dict(self) -> dict[str, float]:
        return {
            "window_seconds": self.window_seconds,
            "hop_seconds": self.hop_seconds,
            "absolute_gate_dbfs": self.absolute_gate_dbfs,
            "relative_gate_db": self.relative_gate_db,
        }


@dataclass(frozen=True, slots=True)
class TrackMixAnalysis:
    """Stable, JSON-compatible measurements for one exact stereo buffer."""

    sample_rate_hz: int
    frame_count: int
    config: MixAnalysisConfig
    sample_peak: float
    peak_dbfs: float | None
    rms_dbfs: float | None
    active_rms_dbfs: float | None
    crest_factor_db: float | None
    active_ratio: float
    active_window_count: int
    window_count: int
    band_energy_ratios: tuple[tuple[str, float | None], ...]
    spectral_centroid_hz: float | None
    stereo_correlation: float | None
    stereo_width: float | None

    def to_dict(self) -> dict[str, Any]:
        """Return fields in a fixed order with only finite JSON numbers."""

        return {
            "analysis_version": ANALYSIS_VERSION,
            "sample_rate_hz": self.sample_rate_hz,
            "frame_count": self.frame_count,
            "gate": self.config.to_dict(),
            "sample_peak": _serialized_float(self.sample_peak, 9),
            "peak_dbfs": _serialized_float(self.peak_dbfs, 6),
            "rms_dbfs": _serialized_float(self.rms_dbfs, 6),
            "active_rms_dbfs": _serialized_float(
                self.active_rms_dbfs,
                6,
            ),
            "crest_factor_db": _serialized_float(
                self.crest_factor_db,
                6,
            ),
            "active_ratio": _serialized_float(self.active_ratio, 9),
            "active_window_count": self.active_window_count,
            "window_count": self.window_count,
            "band_energy_ratios": {
                name: _serialized_float(value, 9)
                for name, value in self.band_energy_ratios
            },
            "spectral_centroid_hz": _serialized_float(
                self.spectral_centroid_hz,
                3,
            ),
            "stereo_correlation": _serialized_float(
                self.stereo_correlation,
                9,
            ),
            # Normalized M/S width: 0 is perfectly mono/in-phase and 1 is
            # pure side/antiphase.  Unlike S/M it cannot become infinite.
            "stereo_width": _serialized_float(self.stereo_width, 9),
        }


@dataclass(frozen=True, slots=True)
class OverlapActiveRmsDifference:
    """Level comparison restricted to windows active in both tracks."""

    sample_rate_hz: int
    frame_count: int
    config: MixAnalysisConfig
    shared_active_window_count: int
    window_count: int
    overlap_ratio: float
    first_active_rms_dbfs: float | None
    second_active_rms_dbfs: float | None
    first_minus_second_db: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_version": ANALYSIS_VERSION,
            "sample_rate_hz": self.sample_rate_hz,
            "frame_count": self.frame_count,
            "gate": self.config.to_dict(),
            "shared_active_window_count": self.shared_active_window_count,
            "window_count": self.window_count,
            "overlap_ratio": _serialized_float(self.overlap_ratio, 9),
            "first_active_rms_dbfs": _serialized_float(
                self.first_active_rms_dbfs,
                6,
            ),
            "second_active_rms_dbfs": _serialized_float(
                self.second_active_rms_dbfs,
                6,
            ),
            "first_minus_second_db": _serialized_float(
                self.first_minus_second_db,
                6,
            ),
        }


@dataclass(frozen=True, slots=True)
class _WindowMeasurements:
    starts: tuple[int, ...]
    rms: np.ndarray
    active: np.ndarray
    window_frames: int
    hop_frames: int


@dataclass(slots=True)
class _StereoMoments:
    count: int = 0
    left_mean: float = 0.0
    right_mean: float = 0.0
    left_m2: float = 0.0
    right_m2: float = 0.0
    cross_m2: float = 0.0

    def add(self, left: np.ndarray, right: np.ndarray) -> None:
        chunk_count = int(left.size)
        if chunk_count == 0:
            return
        chunk_left_mean = float(np.mean(left, dtype=np.float64))
        chunk_right_mean = float(np.mean(right, dtype=np.float64))
        left_centered = left - chunk_left_mean
        right_centered = right - chunk_right_mean
        chunk_left_m2 = float(
            np.sum(left_centered * left_centered, dtype=np.float64)
        )
        chunk_right_m2 = float(
            np.sum(right_centered * right_centered, dtype=np.float64)
        )
        chunk_cross_m2 = float(
            np.sum(left_centered * right_centered, dtype=np.float64)
        )
        if self.count == 0:
            self.count = chunk_count
            self.left_mean = chunk_left_mean
            self.right_mean = chunk_right_mean
            self.left_m2 = chunk_left_m2
            self.right_m2 = chunk_right_m2
            self.cross_m2 = chunk_cross_m2
            return

        combined_count = self.count + chunk_count
        left_delta = chunk_left_mean - self.left_mean
        right_delta = chunk_right_mean - self.right_mean
        merge_weight = (self.count / combined_count) * chunk_count
        self.left_m2 += (
            chunk_left_m2 + left_delta * left_delta * merge_weight
        )
        self.right_m2 += (
            chunk_right_m2 + right_delta * right_delta * merge_weight
        )
        self.cross_m2 += (
            chunk_cross_m2 + left_delta * right_delta * merge_weight
        )
        self.left_mean += left_delta * (chunk_count / combined_count)
        self.right_mean += right_delta * (chunk_count / combined_count)
        self.count = combined_count

    def correlation(self) -> float | None:
        if self.left_m2 <= 0.0 or self.right_m2 <= 0.0:
            return None
        denominator = math.sqrt(self.left_m2) * math.sqrt(self.right_m2)
        if denominator <= 0.0 or not math.isfinite(denominator):
            return None
        value = self.cross_m2 / denominator
        if not math.isfinite(value):
            return None
        return max(-1.0, min(1.0, value))


def _validate_sample_rate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("sample_rate must be an integer between 8000 and 384000")
    sample_rate = int(value)
    if not 8_000 <= sample_rate <= 384_000:
        raise ValueError("sample_rate must be an integer between 8000 and 384000")
    return sample_rate


def _stereo_audio(frames: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    raw = np.asarray(frames)
    if np.iscomplexobj(raw):
        raise ValueError("frames must contain real samples")
    try:
        audio = (
            raw
            if raw.dtype.kind == "f"
            else np.asarray(frames, dtype=np.float64)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("frames must contain real numeric samples") from exc
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("frames must have shape (frame_count, 2)")
    if audio.shape[0] < 1:
        raise ValueError("frames must contain at least one stereo frame")
    for start in range(0, int(audio.shape[0]), _AUDIO_CHUNK_FRAMES):
        if not np.all(
            np.isfinite(audio[start : start + _AUDIO_CHUNK_FRAMES])
        ):
            raise ValueError("frames contain non-finite samples")
    return audio


def _window_measurements(
    audio: np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig,
) -> _WindowMeasurements:
    window_frames, hop_frames = config.frame_lengths(sample_rate)
    starts = tuple(range(0, audio.shape[0], hop_frames))
    levels = window_rms(audio, starts, window_frames)

    loudest = float(np.max(levels))
    if loudest <= 0.0:
        active = np.zeros(len(starts), dtype=bool)
    else:
        absolute = 10.0 ** (config.absolute_gate_dbfs / 20.0)
        relative = loudest * 10.0 ** (config.relative_gate_db / 20.0)
        threshold = max(absolute, relative)
        active = (levels >= threshold) & (levels > 0.0)
    return _WindowMeasurements(
        starts=starts,
        rms=levels,
        active=active,
        window_frames=window_frames,
        hop_frames=hop_frames,
    )


def _active_rms(measurements: _WindowMeasurements) -> float:
    selected = measurements.rms[measurements.active]
    if selected.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(selected), dtype=np.float64)))


def _spectral_metrics(
    audio: np.ndarray,
    measurements: _WindowMeasurements,
    sample_rate: int,
) -> tuple[tuple[tuple[str, float | None], ...], float | None]:
    active_indices = np.flatnonzero(measurements.active)
    if active_indices.size == 0:
        return (
            tuple((name, None) for name, _low, _high in FREQUENCY_BANDS_HZ),
            None,
        )

    scale = selected_window_peak(
        audio,
        measurements.starts,
        active_indices,
        measurements.window_frames,
    )
    if scale <= 0.0:
        return (
            tuple((name, None) for name, _low, _high in FREQUENCY_BANDS_HZ),
            None,
        )
    accumulated = accumulated_stereo_power_spectrum(
        audio,
        measurements.starts,
        active_indices,
        measurements.window_frames,
        scale,
    )
    accumulated[0] = 0.0
    total = float(np.sum(accumulated))
    if not math.isfinite(total):
        raise ValueError("spectral energy exceeds the finite analysis range")
    if total <= 0.0:
        return (
            tuple((name, None) for name, _low, _high in FREQUENCY_BANDS_HZ),
            None,
        )

    frequencies = np.fft.rfftfreq(
        measurements.window_frames,
        1.0 / sample_rate,
    )
    ratios: list[tuple[str, float | None]] = []
    for name, low, high in FREQUENCY_BANDS_HZ:
        if high is None:
            mask = frequencies >= low
        else:
            mask = (frequencies >= low) & (frequencies < high)
        energy = float(np.sum(accumulated[mask]))
        ratios.append((name, energy / total))
    centroid = float(np.sum(frequencies * accumulated) / total)
    return tuple(ratios), centroid


def _stereo_metrics(
    audio: np.ndarray,
    measurements: _WindowMeasurements,
) -> tuple[float | None, float | None]:
    active_indices = np.flatnonzero(measurements.active)
    if active_indices.size == 0:
        return None, None

    ranges: list[tuple[int, int]] = []
    range_start = -1
    range_end = -1
    for index in active_indices:
        start = measurements.starts[int(index)]
        end = min(
            int(audio.shape[0]),
            start + measurements.window_frames,
        )
        if range_start < 0:
            range_start, range_end = start, end
        elif start <= range_end:
            range_end = max(range_end, end)
        else:
            ranges.append((range_start, range_end))
            range_start, range_end = start, end
    ranges.append((range_start, range_end))

    left_peak = 0.0
    right_peak = 0.0
    mid_peak = 0.0
    side_peak = 0.0
    selected_frame_count = 0
    for range_start, range_end in ranges:
        selected_frame_count += range_end - range_start
        for start in range(
            range_start,
            range_end,
            _AUDIO_CHUNK_FRAMES,
        ):
            chunk = np.asarray(
                audio[start : min(range_end, start + _AUDIO_CHUNK_FRAMES)],
                dtype=np.float64,
            )
            left = chunk[:, 0]
            right = chunk[:, 1]
            left_peak = max(left_peak, float(np.max(np.abs(left))))
            right_peak = max(right_peak, float(np.max(np.abs(right))))
            left_half = left * 0.5
            right_half = right * 0.5
            mid_peak = max(
                mid_peak,
                float(np.max(np.abs(left_half + right_half))),
            )
            side_peak = max(
                side_peak,
                float(np.max(np.abs(left_half - right_half))),
            )
    if max(left_peak, right_peak) <= 0.0:
        return None, None

    mid_square_sums: list[float] = []
    side_square_sums: list[float] = []
    moments = _StereoMoments()
    for range_start, range_end in ranges:
        for start in range(
            range_start,
            range_end,
            _AUDIO_CHUNK_FRAMES,
        ):
            chunk = np.asarray(
                audio[start : min(range_end, start + _AUDIO_CHUNK_FRAMES)],
                dtype=np.float64,
            )
            left = chunk[:, 0]
            right = chunk[:, 1]
            left_half = left * 0.5
            right_half = right * 0.5
            mid = left_half + right_half
            side = left_half - right_half
            if mid_peak > 0.0:
                normalized_mid = mid / mid_peak
                mid_square_sums.append(
                    float(
                        np.sum(
                            normalized_mid * normalized_mid,
                            dtype=np.float64,
                        )
                    )
                )
            if side_peak > 0.0:
                normalized_side = side / side_peak
                side_square_sums.append(
                    float(
                        np.sum(
                            normalized_side * normalized_side,
                            dtype=np.float64,
                        )
                    )
                )
            if left_peak > 0.0 and right_peak > 0.0:
                moments.add(
                    left / left_peak,
                    right / right_peak,
                )

    mid_rms = (
        mid_peak
        * math.sqrt(
            math.fsum(mid_square_sums) / selected_frame_count
        )
        if mid_peak > 0.0
        else 0.0
    )
    side_rms = (
        side_peak
        * math.sqrt(
            math.fsum(side_square_sums) / selected_frame_count
        )
        if side_peak > 0.0
        else 0.0
    )
    combined = math.hypot(mid_rms, side_rms)
    width = side_rms / combined if combined > 0.0 else None
    return moments.correlation(), width


def analyze_track(
    frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig | None = None,
) -> TrackMixAnalysis:
    """Measure one stem without mutating it or applying any DSP."""

    sample_rate = _validate_sample_rate(sample_rate)
    if config is None:
        config = MixAnalysisConfig()
    elif not isinstance(config, MixAnalysisConfig):
        raise ValueError("config must be a MixAnalysisConfig")
    audio = _stereo_audio(frames)
    measurements = _window_measurements(audio, sample_rate, config)

    sample_peak = _sample_peak(audio)
    full_rms = _rms(audio, sample_peak)
    active_rms = _active_rms(measurements)
    active_count = int(np.count_nonzero(measurements.active))
    window_count = len(measurements.starts)
    band_ratios, centroid = _spectral_metrics(
        audio,
        measurements,
        sample_rate,
    )
    correlation, width = _stereo_metrics(audio, measurements)
    crest = (
        float(20.0 * math.log10(sample_peak / full_rms))
        if sample_peak > 0.0 and full_rms > 0.0
        else None
    )
    return TrackMixAnalysis(
        sample_rate_hz=sample_rate,
        frame_count=int(audio.shape[0]),
        config=config,
        sample_peak=sample_peak,
        peak_dbfs=_dbfs(sample_peak),
        rms_dbfs=_dbfs(full_rms),
        active_rms_dbfs=_dbfs(active_rms),
        crest_factor_db=crest,
        active_ratio=active_count / window_count,
        active_window_count=active_count,
        window_count=window_count,
        band_energy_ratios=band_ratios,
        spectral_centroid_hz=centroid,
        stereo_correlation=correlation,
        stereo_width=width,
    )


def overlap_active_rms_difference(
    first_frames: Sequence[Sequence[float]] | np.ndarray,
    second_frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig | None = None,
) -> OverlapActiveRmsDifference:
    """Compare levels only where both tracks pass their own activity gates.

    ``first_minus_second_db`` is positive when the first track is louder.
    Tracks must share a timeline and therefore have the same frame count.
    """

    sample_rate = _validate_sample_rate(sample_rate)
    if config is None:
        config = MixAnalysisConfig()
    elif not isinstance(config, MixAnalysisConfig):
        raise ValueError("config must be a MixAnalysisConfig")
    first = _stereo_audio(first_frames)
    second = _stereo_audio(second_frames)
    if first.shape[0] != second.shape[0]:
        raise ValueError("tracks must have the same frame count")

    first_windows = _window_measurements(first, sample_rate, config)
    second_windows = _window_measurements(second, sample_rate, config)
    shared = first_windows.active & second_windows.active
    shared_count = int(np.count_nonzero(shared))
    window_count = len(first_windows.starts)
    if shared_count:
        first_rms = float(
            np.sqrt(
                np.mean(
                    np.square(first_windows.rms[shared]),
                    dtype=np.float64,
                )
            )
        )
        second_rms = float(
            np.sqrt(
                np.mean(
                    np.square(second_windows.rms[shared]),
                    dtype=np.float64,
                )
            )
        )
        first_db = _dbfs(first_rms)
        second_db = _dbfs(second_rms)
        difference = (
            first_db - second_db
            if first_db is not None and second_db is not None
            else None
        )
    else:
        first_db = None
        second_db = None
        difference = None
    return OverlapActiveRmsDifference(
        sample_rate_hz=sample_rate,
        frame_count=int(first.shape[0]),
        config=config,
        shared_active_window_count=shared_count,
        window_count=window_count,
        overlap_ratio=shared_count / window_count,
        first_active_rms_dbfs=first_db,
        second_active_rms_dbfs=second_db,
        first_minus_second_db=difference,
    )
