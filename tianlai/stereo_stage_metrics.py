"""Read-only, deterministic stereo-stage measurements.

The analyzer scans a stereo buffer in fixed-size chunks.  It reports sample
peaks, ordinary unweighted RMS, mono-fold compatibility, normalized M/S width
and centered left/right correlation.  It does not alter the audio and does not
implement true-peak, LUFS or ITU-R BS.1770 loudness measurement.

Energy is accumulated from the left and right channels independently.  A
valid antiphase stereo signal therefore remains visible in the stereo RMS even
when its mono fold cancels exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any, Sequence

import numpy as np


STEREO_STAGE_METRICS_FORMAT = "tianlai.stereo_stage_metrics"
STEREO_STAGE_METRICS_VERSION = 1

_CHUNK_FRAMES = 65_536
_DB_PER_NEPER = 20.0 / math.log(10.0)


def _serialized_float(
    value: float | None,
    digits: int,
) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric cannot be serialized as a finite number")
    rounded = round(number, digits)
    return 0.0 if rounded == 0.0 else rounded


def _linear_float(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("linear metric must be finite and non-negative")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True, slots=True)
class TailWindowMetrics:
    """Measurements restricted to the requested final section of the audio."""

    requested_seconds: float
    effective_frame_count: int
    effective_seconds: float
    sample_peak: float
    sample_peak_dbfs: float | None
    stereo_rms_dbfs: float | None
    peak_relative_to_full_track_db: float | None
    silent: bool

    @property
    def peak_dbfs(self) -> float | None:
        """Compatibility alias for the explicitly named sample-peak field."""

        return self.sample_peak_dbfs

    @property
    def rms_dbfs(self) -> float | None:
        """Compatibility alias; the measured RMS is stereo RMS."""

        return self.stereo_rms_dbfs

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_seconds": _serialized_float(
                self.requested_seconds,
                12,
            ),
            "effective_frame_count": self.effective_frame_count,
            "effective_seconds": _serialized_float(
                self.effective_seconds,
                12,
            ),
            "sample_peak": _linear_float(self.sample_peak),
            "sample_peak_dbfs": _serialized_float(
                self.sample_peak_dbfs,
                6,
            ),
            "stereo_rms_dbfs": _serialized_float(
                self.stereo_rms_dbfs,
                6,
            ),
            "peak_relative_to_full_track_db": _serialized_float(
                self.peak_relative_to_full_track_db,
                6,
            ),
            "silent": self.silent,
        }


@dataclass(frozen=True, slots=True)
class StereoStageMetrics:
    """Stable measurements for one complete stereo buffer."""

    sample_rate_hz: int
    frame_count: int
    left_peak: float
    left_peak_dbfs: float | None
    right_peak: float
    right_peak_dbfs: float | None
    sample_peak: float
    sample_peak_dbfs: float | None
    stereo_rms_dbfs: float | None
    mono_fold_rms_dbfs: float | None
    mono_fold_delta_db: float | None
    mono_fold_silent: bool
    normalized_ms_width: float | None
    left_right_correlation: float | None
    tail_window: TailWindowMetrics | None

    @property
    def sample_rate(self) -> int:
        return self.sample_rate_hz

    @property
    def left_sample_peak(self) -> float:
        return self.left_peak

    @property
    def right_sample_peak(self) -> float:
        return self.right_peak

    @property
    def stereo_width(self) -> float | None:
        return self.normalized_ms_width

    @property
    def stereo_correlation(self) -> float | None:
        return self.left_right_correlation

    def to_dict(self) -> dict[str, Any]:
        """Return a fixed-order document containing no NaN or infinity."""

        return {
            "format": STEREO_STAGE_METRICS_FORMAT,
            "version": STEREO_STAGE_METRICS_VERSION,
            "metric": "full_track_stereo_sample_peak_and_rms",
            "sample_rate_hz": self.sample_rate_hz,
            "frame_count": self.frame_count,
            "left_peak": _linear_float(self.left_peak),
            "left_peak_dbfs": _serialized_float(
                self.left_peak_dbfs,
                6,
            ),
            "right_peak": _linear_float(self.right_peak),
            "right_peak_dbfs": _serialized_float(
                self.right_peak_dbfs,
                6,
            ),
            "sample_peak": _linear_float(self.sample_peak),
            "sample_peak_dbfs": _serialized_float(
                self.sample_peak_dbfs,
                6,
            ),
            "stereo_rms_dbfs": _serialized_float(
                self.stereo_rms_dbfs,
                6,
            ),
            "mono_fold_rms_dbfs": _serialized_float(
                self.mono_fold_rms_dbfs,
                6,
            ),
            "mono_fold_delta_db": _serialized_float(
                self.mono_fold_delta_db,
                6,
            ),
            "mono_fold_delta_definition": "mono_fold_minus_stereo_db",
            "mono_fold_silent": self.mono_fold_silent,
            "normalized_ms_width": _serialized_float(
                self.normalized_ms_width,
                12,
            ),
            "left_right_correlation": _serialized_float(
                self.left_right_correlation,
                12,
            ),
            "tail_window": (
                self.tail_window.to_dict()
                if self.tail_window is not None
                else None
            ),
            "audio_modified": False,
            "notice": (
                "These are sample-peak and ordinary unweighted RMS "
                "diagnostics; they are not true-peak, LUFS, or ITU-R "
                "BS.1770 loudness measurements."
            ),
        }


@dataclass(frozen=True, slots=True)
class _Peaks:
    left: float
    right: float
    sample: float
    mid: float
    side: float
    tail: float


@dataclass(slots=True)
class _CompensatedSum:
    total: float = 0.0
    correction: float = 0.0

    def add(self, value: float) -> None:
        # Neumaier summation is stable even when the next positive chunk is
        # larger than the running total.
        updated = self.total + value
        if abs(self.total) >= abs(value):
            self.correction += (self.total - updated) + value
        else:
            self.correction += (value - updated) + self.total
        self.total = updated

    def value(self) -> float:
        result = self.total + self.correction
        if not math.isfinite(result) or result < 0.0:
            raise ValueError("audio energy exceeds the finite analysis range")
        return result


@dataclass(slots=True)
class _CorrelationAccumulator:
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
        if (
            self.count < 2
            or self.left_m2 <= 0.0
            or self.right_m2 <= 0.0
        ):
            return None
        denominator = math.sqrt(self.left_m2) * math.sqrt(self.right_m2)
        if denominator <= 0.0 or not math.isfinite(denominator):
            return None
        correlation = self.cross_m2 / denominator
        if not math.isfinite(correlation):
            return None
        return max(-1.0, min(1.0, correlation))


def _validate_sample_rate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(
            "sample_rate must be an integer between 8000 and 384000"
        )
    sample_rate = int(value)
    if not 8_000 <= sample_rate <= 384_000:
        raise ValueError(
            "sample_rate must be an integer between 8000 and 384000"
        )
    return sample_rate


def _stereo_audio(
    frames: Sequence[Sequence[float]] | np.ndarray,
) -> np.ndarray:
    try:
        audio = np.asanyarray(frames)
    except (TypeError, ValueError) as exc:
        raise ValueError("frames must contain real numeric samples") from exc
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("frames must have shape (frame_count, 2)")
    if audio.shape[0] < 1:
        raise ValueError("frames must contain at least one stereo frame")
    if audio.dtype.kind not in "fiu":
        if np.iscomplexobj(audio):
            raise ValueError("frames must contain real samples")
        raise ValueError("frames must contain real numeric samples")
    return audio


def _tail_frame_count(
    tail_window_seconds: float | None,
    sample_rate: int,
    frame_count: int,
) -> tuple[float | None, int]:
    if tail_window_seconds is None:
        return None, 0
    if (
        isinstance(tail_window_seconds, bool)
        or not isinstance(tail_window_seconds, Real)
    ):
        raise ValueError("tail_window_seconds must be a finite positive number")
    seconds = float(tail_window_seconds)
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise ValueError("tail_window_seconds must be a finite positive number")
    requested_frames = seconds * sample_rate
    if not math.isfinite(requested_frames) or requested_frames >= frame_count:
        return seconds, frame_count
    tail_frames = round(requested_frames)
    if tail_frames < 1:
        raise ValueError(
            "tail_window_seconds resolves to fewer than one frame"
        )
    return seconds, min(frame_count, tail_frames)


def _as_float64_chunk(audio: np.ndarray, start: int, end: int) -> np.ndarray:
    try:
        chunk = np.asarray(audio[start:end], dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("frames must contain finite real samples") from exc
    if not bool(np.all(np.isfinite(chunk))):
        raise ValueError("frames contain non-finite samples")
    return chunk


def _mid_and_side(chunk: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    left_half = chunk[:, 0] * 0.5
    right_half = chunk[:, 1] * 0.5
    return left_half + right_half, left_half - right_half


def _maximum_absolute(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    return float(np.max(np.abs(values)))


def _scan_peaks(
    audio: np.ndarray,
    tail_start: int | None,
) -> _Peaks:
    left_peak = 0.0
    right_peak = 0.0
    mid_peak = 0.0
    side_peak = 0.0
    tail_peak = 0.0
    frame_count = int(audio.shape[0])
    for start in range(0, frame_count, _CHUNK_FRAMES):
        end = min(frame_count, start + _CHUNK_FRAMES)
        chunk = _as_float64_chunk(audio, start, end)
        chunk_left_peak = _maximum_absolute(chunk[:, 0])
        chunk_right_peak = _maximum_absolute(chunk[:, 1])
        left_peak = max(left_peak, chunk_left_peak)
        right_peak = max(right_peak, chunk_right_peak)
        mid, side = _mid_and_side(chunk)
        mid_peak = max(mid_peak, _maximum_absolute(mid))
        side_peak = max(side_peak, _maximum_absolute(side))
        if tail_start is not None and end > tail_start:
            local_start = max(0, tail_start - start)
            tail_peak = max(
                tail_peak,
                _maximum_absolute(chunk[local_start:]),
            )
    return _Peaks(
        left=left_peak,
        right=right_peak,
        sample=max(left_peak, right_peak),
        mid=mid_peak,
        side=side_peak,
        tail=tail_peak,
    )


def _normalized_square_sum(values: np.ndarray, scale: float) -> float:
    if scale <= 0.0 or values.size == 0:
        return 0.0
    normalized = values / scale
    return float(np.sum(normalized * normalized, dtype=np.float64))


def _level_dbfs_from_energy(
    scale: float,
    normalized_square_sum: float,
    sample_count: int,
) -> float | None:
    if scale <= 0.0 or normalized_square_sum <= 0.0:
        return None
    log_rms = (
        math.log(scale)
        + 0.5
        * (
            math.log(normalized_square_sum)
            - math.log(sample_count)
        )
    )
    result = _DB_PER_NEPER * log_rms
    if not math.isfinite(result):
        raise ValueError("audio level exceeds the finite analysis range")
    return result


def _peak_dbfs(peak: float) -> float | None:
    if peak <= 0.0:
        return None
    result = _DB_PER_NEPER * math.log(peak)
    if not math.isfinite(result):
        raise ValueError("sample peak exceeds the finite analysis range")
    return result


def _width_from_levels(
    mid_scale: float,
    mid_square_sum: float,
    side_scale: float,
    side_square_sum: float,
    frame_count: int,
) -> float | None:
    if mid_scale <= 0.0 and side_scale <= 0.0:
        return None
    if side_scale <= 0.0:
        return 0.0
    if mid_scale <= 0.0:
        return 1.0
    mid_log_rms = (
        math.log(mid_scale)
        + 0.5
        * (
            math.log(mid_square_sum)
            - math.log(frame_count)
        )
    )
    side_log_rms = (
        math.log(side_scale)
        + 0.5
        * (
            math.log(side_square_sum)
            - math.log(frame_count)
        )
    )
    reference = max(mid_log_rms, side_log_rms)
    normalized_mid = math.exp(mid_log_rms - reference)
    normalized_side = math.exp(side_log_rms - reference)
    width = normalized_side / math.hypot(
        normalized_mid,
        normalized_side,
    )
    return max(0.0, min(1.0, width))


def _relative_peak_db(
    selected_peak: float,
    full_peak: float,
) -> float | None:
    if selected_peak <= 0.0 or full_peak <= 0.0:
        return None
    difference = _DB_PER_NEPER * (
        math.log(selected_peak) - math.log(full_peak)
    )
    if not math.isfinite(difference):
        raise ValueError("relative sample peak is not finite")
    # A selected window is a subset of the full track.  Suppress a tiny
    # positive residue caused only by subtracting logarithms.
    return min(0.0, difference)


def analyze_stereo_stage(
    frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    *,
    tail_window_seconds: float | None = None,
) -> StereoStageMetrics:
    """Measure stereo stage and optional end-window behavior.

    ``mono_fold_delta_db`` is mono-fold RMS minus full stereo RMS.  It is
    ``None`` when the mono fold is exactly silent, with
    ``mono_fold_silent=True`` making that cancellation explicit.
    """

    sample_rate = _validate_sample_rate(sample_rate)
    audio = _stereo_audio(frames)
    frame_count = int(audio.shape[0])
    requested_tail_seconds, tail_frames = _tail_frame_count(
        tail_window_seconds,
        sample_rate,
        frame_count,
    )
    tail_start = frame_count - tail_frames if tail_frames else None
    peaks = _scan_peaks(audio, tail_start)

    stereo_energy = _CompensatedSum()
    mid_energy = _CompensatedSum()
    side_energy = _CompensatedSum()
    tail_energy = _CompensatedSum()
    correlation = _CorrelationAccumulator()
    for start in range(0, frame_count, _CHUNK_FRAMES):
        end = min(frame_count, start + _CHUNK_FRAMES)
        chunk = _as_float64_chunk(audio, start, end)
        left = chunk[:, 0]
        right = chunk[:, 1]
        if peaks.sample > 0.0:
            stereo_energy.add(
                _normalized_square_sum(left, peaks.sample)
                + _normalized_square_sum(right, peaks.sample)
            )
        mid, side = _mid_and_side(chunk)
        mid_energy.add(_normalized_square_sum(mid, peaks.mid))
        side_energy.add(_normalized_square_sum(side, peaks.side))
        if peaks.left > 0.0 and peaks.right > 0.0:
            correlation.add(
                left / peaks.left,
                right / peaks.right,
            )
        if tail_start is not None and end > tail_start:
            local_start = max(0, tail_start - start)
            tail_chunk = chunk[local_start:]
            tail_energy.add(
                _normalized_square_sum(tail_chunk[:, 0], peaks.tail)
                + _normalized_square_sum(tail_chunk[:, 1], peaks.tail)
            )

    stereo_rms_dbfs = _level_dbfs_from_energy(
        peaks.sample,
        stereo_energy.value(),
        frame_count * 2,
    )
    mono_rms_dbfs = _level_dbfs_from_energy(
        peaks.mid,
        mid_energy.value(),
        frame_count,
    )
    mono_fold_silent = peaks.mid <= 0.0
    if mono_rms_dbfs is None or stereo_rms_dbfs is None:
        mono_fold_delta_db = None
    else:
        mono_fold_delta_db = mono_rms_dbfs - stereo_rms_dbfs
        if not math.isfinite(mono_fold_delta_db):
            raise ValueError("mono-fold level difference is not finite")

    tail_window: TailWindowMetrics | None
    if requested_tail_seconds is None:
        tail_window = None
    else:
        tail_rms_dbfs = _level_dbfs_from_energy(
            peaks.tail,
            tail_energy.value(),
            tail_frames * 2,
        )
        tail_window = TailWindowMetrics(
            requested_seconds=requested_tail_seconds,
            effective_frame_count=tail_frames,
            effective_seconds=tail_frames / sample_rate,
            sample_peak=peaks.tail,
            sample_peak_dbfs=_peak_dbfs(peaks.tail),
            stereo_rms_dbfs=tail_rms_dbfs,
            peak_relative_to_full_track_db=_relative_peak_db(
                peaks.tail,
                peaks.sample,
            ),
            silent=peaks.tail <= 0.0,
        )

    return StereoStageMetrics(
        sample_rate_hz=sample_rate,
        frame_count=frame_count,
        left_peak=peaks.left,
        left_peak_dbfs=_peak_dbfs(peaks.left),
        right_peak=peaks.right,
        right_peak_dbfs=_peak_dbfs(peaks.right),
        sample_peak=peaks.sample,
        sample_peak_dbfs=_peak_dbfs(peaks.sample),
        stereo_rms_dbfs=stereo_rms_dbfs,
        mono_fold_rms_dbfs=mono_rms_dbfs,
        mono_fold_delta_db=mono_fold_delta_db,
        mono_fold_silent=mono_fold_silent,
        normalized_ms_width=_width_from_levels(
            peaks.mid,
            mid_energy.value(),
            peaks.side,
            side_energy.value(),
            frame_count,
        ),
        left_right_correlation=correlation.correlation(),
        tail_window=tail_window,
    )


__all__ = (
    "STEREO_STAGE_METRICS_FORMAT",
    "STEREO_STAGE_METRICS_VERSION",
    "StereoStageMetrics",
    "TailWindowMetrics",
    "analyze_stereo_stage",
)
