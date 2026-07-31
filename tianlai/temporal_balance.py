"""Deterministic temporal-balance triage for two rendered stereo stems.

The diagnostic looks for a relationship whose whole-piece average appears
reasonable while its local level offset drifts between passages.  Each stem is
gated independently, and only windows active in both stems contribute.
Stereo energy is measured channel by channel, never from ``L + R``.

This module measures and reports.  It never changes gain, and its windowed RMS
statistics are not a psychoacoustic loudness or masking model.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any, Sequence

import numpy as np

from ._window_batches import window_rms
from .mix_analysis import MixAnalysisConfig


TEMPORAL_BALANCE_FORMAT = "tianlai.temporal_balance"
TEMPORAL_BALANCE_VERSION = 2
DERIVED_DECIMAL_DIGITS = 6
LOCALIZATION_BUCKET_SECONDS = 1.0
MINIMUM_BUCKET_SHARED_WINDOW_COUNT = 3
MINIMUM_BUCKET_SHARED_COVERAGE_SECONDS = 0.5
MAX_REPORTED_CANDIDATE_SEGMENTS = 128

_STABLE = "stable_within_tolerance"
_VARIES = "varies_outside_tolerance"
_INSUFFICIENT = "insufficient_overlap"
_TOO_QUIET = "subject_too_quiet"
_TOO_LOUD = "subject_too_loud"
_AUDIO_CHUNK_FRAMES = 262_144


@dataclass(frozen=True, slots=True)
class TemporalBalanceSegment:
    """A coarse, creator-reviewable passage; never a raw window sequence."""

    start_seconds: float
    end_seconds: float
    shared_active_window_count: int
    shared_active_coverage_seconds: float
    median_offset_db: float
    deviation_db: float
    direction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "shared_active_window_count": self.shared_active_window_count,
            "shared_active_coverage_seconds": (
                self.shared_active_coverage_seconds
            ),
            "median_offset_db": self.median_offset_db,
            "deviation_db": self.deviation_db,
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class TemporalBalanceAnalysis:
    """Stable aggregate statistics; the per-window sequence is not retained."""

    sample_rate_hz: int
    frame_count: int
    config: MixAnalysisConfig
    target_offset_db: float
    tolerance_db: float
    minimum_shared_window_count: int
    shared_active_window_count: int
    window_count: int
    overlap_ratio: float
    p10_db: float | None
    median_db: float | None
    p90_db: float | None
    robust_span_db: float | None
    within_tolerance_window_ratio: float | None
    below_tolerance_window_count: int | None
    above_tolerance_window_count: int | None
    status: str
    candidate_segments: tuple[TemporalBalanceSegment, ...] = ()
    candidate_segment_count: int = 0
    candidate_segments_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a quantized JSON contract without the window-level series."""

        return {
            "format": TEMPORAL_BALANCE_FORMAT,
            "version": TEMPORAL_BALANCE_VERSION,
            "metric": "shared_active_window_rms_offset_distribution",
            "offset_definition": "first_minus_second_db",
            "sample_rate_hz": self.sample_rate_hz,
            "frame_count": self.frame_count,
            "gate": self.config.to_dict(),
            "target_offset_db": self.target_offset_db,
            "tolerance_db": self.tolerance_db,
            "minimum_shared_window_count": (
                self.minimum_shared_window_count
            ),
            "shared_active_window_count": self.shared_active_window_count,
            "window_count": self.window_count,
            "overlap_ratio": self.overlap_ratio,
            "p10_db": self.p10_db,
            "median_db": self.median_db,
            "p90_db": self.p90_db,
            "robust_span_db": self.robust_span_db,
            "within_tolerance_window_ratio": (
                self.within_tolerance_window_ratio
            ),
            "below_tolerance_window_count": (
                self.below_tolerance_window_count
            ),
            "above_tolerance_window_count": (
                self.above_tolerance_window_count
            ),
            "status": self.status,
            "status_rule": (
                "p10_and_p90_must_both_be_within_target_tolerance"
            ),
            "candidate_segment_policy": {
                "time_basis": "seconds",
                "bucket_seconds": LOCALIZATION_BUCKET_SECONDS,
                "minimum_shared_window_count_per_bucket": (
                    MINIMUM_BUCKET_SHARED_WINDOW_COUNT
                ),
                "minimum_shared_window_coverage_seconds_per_bucket": (
                    MINIMUM_BUCKET_SHARED_COVERAGE_SECONDS
                ),
                "bucket_statistic": (
                    "median_of_quantized_shared_active_window_offsets"
                ),
                "segment_boundaries": (
                    "union_evidence_bounds_not_full_bucket_bounds"
                ),
                "merge_rule": (
                    "adjacent_candidate_buckets_with_same_direction"
                ),
                "raw_window_sequence_included": False,
            },
            "candidate_segment_count": self.candidate_segment_count,
            "candidate_segments_truncated": (
                self.candidate_segments_truncated
            ),
            "candidate_segments": [
                segment.to_dict() for segment in self.candidate_segments
            ],
            "quantization": {
                "derived_decimal_digits": DERIVED_DECIMAL_DIGITS,
                "percentile_method": "linear_(n-1)_interpolation",
            },
            "window_sequence_included": False,
            "audio_modified": False,
            "notice": (
                "该确定性窗口 RMS 指标只用于段落平衡排查；它不会自动调整"
                "增益，也不是心理声学响度或 masking 结论。"
            ),
        }


def _finite_real(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{label} must be a positive integer")
    number = int(value)
    if number < 1:
        raise ValueError(f"{label} must be a positive integer")
    return number


def _stereo_audio(
    frames: Sequence[Sequence[float]] | np.ndarray,
) -> np.ndarray:
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


def _window_levels(
    audio: np.ndarray,
    starts: tuple[int, ...],
    window_frames: int,
    config: MixAnalysisConfig,
) -> tuple[np.ndarray, np.ndarray]:
    levels = window_rms(audio, starts, window_frames)
    loudest = float(np.max(levels))
    if loudest <= 0.0:
        active = np.zeros(len(starts), dtype=bool)
    else:
        absolute = 10.0 ** (config.absolute_gate_dbfs / 20.0)
        relative = loudest * 10.0 ** (config.relative_gate_db / 20.0)
        threshold = max(absolute, relative)
        active = (levels >= threshold) & (levels > 0.0)
    return levels, active


def _quantize(value: float) -> float:
    quantized = round(float(value), DERIVED_DECIMAL_DIGITS)
    return 0.0 if quantized == 0.0 else quantized


def _linear_percentile(
    ordered_values: tuple[float, ...],
    percentile: float,
) -> float:
    if not ordered_values:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered_values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered_values[lower]
    fraction = position - lower
    return (
        ordered_values[lower] * (1.0 - fraction)
        + ordered_values[upper] * fraction
    )


def _median(values: Sequence[float]) -> float:
    return _linear_percentile(tuple(sorted(values)), 0.50)


def _interval_union(
    intervals: Sequence[tuple[float, float]],
) -> tuple[float, float, float]:
    """Return evidence bounds and union coverage for clipped time spans."""

    ordered = sorted(
        (float(start), float(end))
        for start, end in intervals
        if end > start
    )
    if not ordered:
        return 0.0, 0.0, 0.0
    evidence_start = ordered[0][0]
    current_start, current_end = ordered[0]
    coverage = 0.0
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        coverage += current_end - current_start
        current_start, current_end = start, end
    coverage += current_end - current_start
    return evidence_start, current_end, coverage


def _candidate_segments(
    *,
    offsets: Sequence[float],
    shared_window_indices: Sequence[int],
    starts: Sequence[int],
    window_frames: int,
    sample_rate: int,
    frame_count: int,
    target_offset_db: float,
    tolerance_db: float,
) -> tuple[tuple[TemporalBalanceSegment, ...], int, bool]:
    """Coarsen shared-window offsets into deterministic one-second passages.

    Raw window offsets remain local to this function.  A bucket must contain
    at least three shared-active windows and its median must be outside the
    declared tolerance.  Consequently one isolated abnormal analysis window
    cannot become a public passage.  Adjacent buckets on the same side of the
    target are merged before serialization.
    """

    if len(offsets) != len(shared_window_indices):
        raise ValueError("offsets and shared windows must have equal length")
    duration_seconds = frame_count / sample_rate
    if duration_seconds <= 0.0:
        return (), 0, False

    bucket_evidence: dict[
        int, list[tuple[float, float, float]]
    ] = {}
    bucket_frames_twice = max(
        1,
        round(2 * LOCALIZATION_BUCKET_SECONDS * sample_rate),
    )
    final_center_twice = (2 * frame_count) - 1
    for offset, window_index in zip(
        offsets,
        shared_window_indices,
        strict=True,
    ):
        center_frames_twice = min(
            (2 * starts[window_index]) + window_frames,
            final_center_twice,
        )
        bucket_index = center_frames_twice // bucket_frames_twice
        bucket_start = bucket_index * LOCALIZATION_BUCKET_SECONDS
        bucket_end = min(
            (bucket_index + 1) * LOCALIZATION_BUCKET_SECONDS,
            duration_seconds,
        )
        evidence_start = max(
            bucket_start,
            starts[window_index] / sample_rate,
        )
        evidence_end = min(
            bucket_end,
            (starts[window_index] + window_frames) / sample_rate,
            duration_seconds,
        )
        if evidence_end > evidence_start:
            bucket_evidence.setdefault(bucket_index, []).append(
                (offset, evidence_start, evidence_end)
            )

    lower_bound = target_offset_db - tolerance_db
    upper_bound = target_offset_db + tolerance_db
    candidates: list[
        tuple[int, str, tuple[float, ...], float, float, float]
    ] = []
    for bucket_index in sorted(bucket_evidence):
        evidence = tuple(bucket_evidence[bucket_index])
        values = tuple(item[0] for item in evidence)
        if len(values) < MINIMUM_BUCKET_SHARED_WINDOW_COUNT:
            continue
        bucket_median = _quantize(_median(values))
        if bucket_median < lower_bound:
            direction = _TOO_QUIET
            supporting = tuple(
                item for item in evidence if item[0] < lower_bound
            )
        elif bucket_median > upper_bound:
            direction = _TOO_LOUD
            supporting = tuple(
                item for item in evidence if item[0] > upper_bound
            )
        else:
            continue
        if len(supporting) < MINIMUM_BUCKET_SHARED_WINDOW_COUNT:
            continue
        evidence_start, evidence_end, coverage = _interval_union(
            tuple((item[1], item[2]) for item in supporting)
        )
        if coverage < MINIMUM_BUCKET_SHARED_COVERAGE_SECONDS:
            continue
        candidates.append(
            (
                bucket_index,
                direction,
                tuple(item[0] for item in supporting),
                evidence_start,
                evidence_end,
                coverage,
            )
        )

    merged: list[TemporalBalanceSegment] = []
    candidate_index = 0
    while candidate_index < len(candidates):
        (
            first_bucket,
            direction,
            first_values,
            evidence_start,
            evidence_end,
            evidence_coverage,
        ) = candidates[candidate_index]
        last_bucket = first_bucket
        merged_values = list(first_values)
        candidate_index += 1
        while candidate_index < len(candidates):
            (
                bucket_index,
                next_direction,
                values,
                next_evidence_start,
                next_evidence_end,
                next_evidence_coverage,
            ) = candidates[candidate_index]
            if (
                bucket_index != last_bucket + 1
                or next_direction != direction
            ):
                break
            last_bucket = bucket_index
            merged_values.extend(values)
            evidence_start = min(evidence_start, next_evidence_start)
            evidence_end = max(evidence_end, next_evidence_end)
            evidence_coverage += next_evidence_coverage
            candidate_index += 1
        median_offset = _quantize(_median(merged_values))
        merged.append(
            TemporalBalanceSegment(
                start_seconds=_quantize(evidence_start),
                end_seconds=_quantize(evidence_end),
                shared_active_window_count=len(merged_values),
                shared_active_coverage_seconds=_quantize(
                    evidence_coverage
                ),
                median_offset_db=median_offset,
                deviation_db=_quantize(
                    median_offset - target_offset_db
                ),
                direction=direction,
            )
        )

    segment_count = len(merged)
    truncated = segment_count > MAX_REPORTED_CANDIDATE_SEGMENTS
    return (
        tuple(merged[:MAX_REPORTED_CANDIDATE_SEGMENTS]),
        segment_count,
        truncated,
    )


def _insufficient_result(
    *,
    sample_rate: int,
    frame_count: int,
    config: MixAnalysisConfig,
    target_offset_db: float,
    tolerance_db: float,
    minimum_shared_window_count: int,
    shared_active_window_count: int,
    window_count: int,
) -> TemporalBalanceAnalysis:
    return TemporalBalanceAnalysis(
        sample_rate_hz=sample_rate,
        frame_count=frame_count,
        config=config,
        target_offset_db=target_offset_db,
        tolerance_db=tolerance_db,
        minimum_shared_window_count=minimum_shared_window_count,
        shared_active_window_count=shared_active_window_count,
        window_count=window_count,
        overlap_ratio=_quantize(
            shared_active_window_count / window_count
        ),
        p10_db=None,
        median_db=None,
        p90_db=None,
        robust_span_db=None,
        within_tolerance_window_ratio=None,
        below_tolerance_window_count=None,
        above_tolerance_window_count=None,
        status=_INSUFFICIENT,
    )


def analyze_temporal_balance(
    first_frames: Sequence[Sequence[float]] | np.ndarray,
    second_frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig,
    *,
    target_offset_db: float,
    tolerance_db: float,
    minimum_shared_window_count: int,
) -> TemporalBalanceAnalysis:
    """Summarize local first-minus-second dB offsets in shared windows.

    Window offsets are quantized before aggregation.  Once the explicit shared
    window minimum is met, the result is stable only when the robust central
    interval from p10 through p90 lies wholly inside
    ``target_offset_db ± tolerance_db``.
    """

    if not isinstance(config, MixAnalysisConfig):
        raise ValueError("config must be a MixAnalysisConfig")
    target_offset_db = _finite_real(
        target_offset_db,
        "target_offset_db",
    )
    tolerance_db = _finite_real(tolerance_db, "tolerance_db")
    if tolerance_db < 0.0:
        raise ValueError("tolerance_db must not be negative")
    if not (
        math.isfinite(target_offset_db - tolerance_db)
        and math.isfinite(target_offset_db + tolerance_db)
    ):
        raise ValueError(
            "target_offset_db ± tolerance_db must remain finite"
        )
    minimum_shared_window_count = _positive_integer(
        minimum_shared_window_count,
        "minimum_shared_window_count",
    )
    window_frames, hop_frames = config.frame_lengths(sample_rate)
    sample_rate = int(sample_rate)

    first = _stereo_audio(first_frames)
    second = _stereo_audio(second_frames)
    if first.shape[0] != second.shape[0]:
        raise ValueError("stems must have the same frame count")
    starts = tuple(range(0, first.shape[0], hop_frames))
    first_levels, first_active = _window_levels(
        first,
        starts,
        window_frames,
        config,
    )
    second_levels, second_active = _window_levels(
        second,
        starts,
        window_frames,
        config,
    )
    shared = first_active & second_active
    shared_count = int(np.count_nonzero(shared))
    window_count = len(starts)
    if shared_count < minimum_shared_window_count:
        return _insufficient_result(
            sample_rate=sample_rate,
            frame_count=int(first.shape[0]),
            config=config,
            target_offset_db=target_offset_db,
            tolerance_db=tolerance_db,
            minimum_shared_window_count=minimum_shared_window_count,
            shared_active_window_count=shared_count,
            window_count=window_count,
        )

    offsets: list[float] = []
    shared_window_indices = tuple(
        int(index) for index in np.flatnonzero(shared)
    )
    for first_level, second_level in zip(
        first_levels[shared],
        second_levels[shared],
        strict=True,
    ):
        if first_level <= 0.0 or second_level <= 0.0:
            raise ValueError("shared active window has zero energy")
        difference = 20.0 * (
            math.log10(float(first_level))
            - math.log10(float(second_level))
        )
        if not math.isfinite(difference):
            raise ValueError("window level difference is not finite")
        offsets.append(_quantize(difference))

    ordered = tuple(sorted(offsets))
    p10 = _quantize(_linear_percentile(ordered, 0.10))
    median = _quantize(_linear_percentile(ordered, 0.50))
    p90 = _quantize(_linear_percentile(ordered, 0.90))
    robust_span = _quantize(max(0.0, p90 - p10))
    lower_bound = target_offset_db - tolerance_db
    upper_bound = target_offset_db + tolerance_db
    below_count = sum(value < lower_bound for value in offsets)
    above_count = sum(value > upper_bound for value in offsets)
    within_count = shared_count - below_count - above_count
    within_ratio = _quantize(within_count / shared_count)
    status = (
        _STABLE
        if p10 >= lower_bound and p90 <= upper_bound
        else _VARIES
    )
    candidate_segments, candidate_segment_count, truncated = (
        _candidate_segments(
            offsets=offsets,
            shared_window_indices=shared_window_indices,
            starts=starts,
            window_frames=window_frames,
            sample_rate=sample_rate,
            frame_count=int(first.shape[0]),
            target_offset_db=target_offset_db,
            tolerance_db=tolerance_db,
        )
    )
    return TemporalBalanceAnalysis(
        sample_rate_hz=sample_rate,
        frame_count=int(first.shape[0]),
        config=config,
        target_offset_db=target_offset_db,
        tolerance_db=tolerance_db,
        minimum_shared_window_count=minimum_shared_window_count,
        shared_active_window_count=shared_count,
        window_count=window_count,
        overlap_ratio=_quantize(shared_count / window_count),
        p10_db=p10,
        median_db=median,
        p90_db=p90,
        robust_span_db=robust_span,
        within_tolerance_window_ratio=within_ratio,
        below_tolerance_window_count=below_count,
        above_tolerance_window_count=above_count,
        status=status,
        candidate_segments=candidate_segments,
        candidate_segment_count=candidate_segment_count,
        candidate_segments_truncated=truncated,
    )


__all__ = (
    "DERIVED_DECIMAL_DIGITS",
    "LOCALIZATION_BUCKET_SECONDS",
    "MAX_REPORTED_CANDIDATE_SEGMENTS",
    "MINIMUM_BUCKET_SHARED_COVERAGE_SECONDS",
    "MINIMUM_BUCKET_SHARED_WINDOW_COUNT",
    "TEMPORAL_BALANCE_FORMAT",
    "TEMPORAL_BALANCE_VERSION",
    "TemporalBalanceAnalysis",
    "TemporalBalanceSegment",
    "analyze_temporal_balance",
)
