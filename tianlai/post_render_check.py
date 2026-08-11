"""Deterministic, read-only quality evidence for one delivered PCM-24 WAV.

This module deliberately separates measurement from policy.  Structural
contract violations raise before a report can be published.  Measurements
that can also describe an intentional musical choice remain review findings;
they never rewrite, normalise, limit, or otherwise alter the audio.

The standards-based measurements are currently certified only for 48 kHz:

* ITU-R BS.1770-5 K-weighted programme loudness;
* EBU Tech 3341 momentary and short-term loudness;
* EBU Tech 3342 Loudness Range; and
* ITU-R BS.1770-5 Annex 2 four-phase, 48-tap true-peak estimation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePath, PurePosixPath
import tempfile
from typing import Any

import numpy as np

from .canonical_json import canonical_json_bytes
from .self_check import ISSUE_SCHEMA_VERSION, build_issue, summarize_issues


REPORT_SCHEMA_URI = "https://tianlai.local/schemas/post-render-check.schema.json"
REPORT_FORMAT = "tianlai.post_render_check"
REPORT_VERSION = 1
POST_RENDER_CHECK_NAME = "渲染后自检.json"
MEASUREMENT_VERSION = 1
POLICY_VERSION = 1

_CERTIFIED_SAMPLE_RATE = 48_000
_READ_BLOCK_FRAMES = 65_536
_PCM24_SCALE = 8_388_608.0
_PCM24_LSB = 1.0 / _PCM24_SCALE
_FULL_SCALE_FLOOR = 1.0 - _PCM24_LSB
_NEAR_SILENT_PEAK = 1.0e-4
_FULL_SCALE_RUN_WARNING = 3
_TAIL_SECONDS = 0.250
_DC_WINDOW_SECONDS = 0.400
_DC_REQUIRED_WINDOWS = 3

_K_STAGE_1_B = (1.53512485958697, -2.69169618940638, 1.19839281085285)
_K_STAGE_1_A = (1.0, -1.69065929318241, 0.73248077421585)
_K_STAGE_2_B = (1.0, -2.0, 1.0)
_K_STAGE_2_A = (1.0, -1.99004745483398, 0.99007225036621)

# ITU-R BS.1770-5 Annex 2: rows are input delays and columns are phases.
_TRUE_PEAK_POLYPHASE = np.asarray(
    (
        (0.0017089843750, -0.0291748046875, -0.0189208984375, -0.0083007812500),
        (0.0109863281250, 0.0292968750000, 0.0330810546875, 0.0148925781250),
        (-0.0196533203125, -0.0517578125000, -0.0582275390625, -0.0266113281250),
        (0.0332031250000, 0.0891113281250, 0.1015625000000, 0.0476074218750),
        (-0.0594482421875, -0.1665039062500, -0.2003173828125, -0.1022949218750),
        (0.1373291015625, 0.4650878906250, 0.7797851562500, 0.9721679687500),
        (0.9721679687500, 0.7797851562500, 0.4650878906250, 0.1373291015625),
        (-0.1022949218750, -0.2003173828125, -0.1665039062500, -0.0594482421875),
        (0.0476074218750, 0.1015625000000, 0.0891113281250, 0.0332031250000),
        (-0.0266113281250, -0.0582275390625, -0.0517578125000, -0.0196533203125),
        (0.0148925781250, 0.0330810546875, 0.0292968750000, 0.0109863281250),
        (-0.0083007812500, -0.0189208984375, -0.0291748046875, 0.0017089843750),
    ),
    dtype=np.float64,
)


class PostRenderCheckError(ValueError):
    """A report is invalid or contains a delivery-blocking finding."""


def _finite_float(value: float, *, digits: int = 12) -> float:
    result = round(float(value), digits)
    if not math.isfinite(result):
        raise ValueError("post-render measurement produced a non-finite value")
    return 0.0 if result == 0.0 else result


def _optional_float(value: float | None, *, digits: int = 12) -> float | None:
    return None if value is None else _finite_float(value, digits=digits)


def _db_amplitude(value: float) -> float | None:
    if value <= 0.0:
        return None
    return 20.0 * math.log10(value)


def _db_power(value: float) -> float | None:
    if value <= 0.0:
        return None
    return -0.691 + 10.0 * math.log10(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _portable_artifact_label(value: str | PurePath) -> str:
    if not isinstance(value, (str, PurePath)):
        raise ValueError("artifact_path must be a string or pure path")
    label = value.as_posix() if isinstance(value, PurePath) else value
    pure = PurePosixPath(label)
    if (
        not label
        or not pure.parts
        or "\\" in label
        or ":" in label
        or any(ord(character) < 32 or ord(character) == 127 for character in label)
        or pure.is_absolute()
        or pure.as_posix() != label
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise ValueError("artifact_path must be one canonical relative POSIX path")
    return label


def _validate_sha256(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


class _RunStats:
    def __init__(self) -> None:
        self.count = 0
        self.longest = 0
        self._carry = 0

    def update(self, mask: np.ndarray) -> None:
        values = np.asarray(mask, dtype=np.bool_)
        length = int(values.size)
        if length == 0:
            return
        self.count += int(np.count_nonzero(values))
        false_indices = np.flatnonzero(~values)
        if false_indices.size == 0:
            self._carry += length
            self.longest = max(self.longest, self._carry)
            return
        leading = int(false_indices[0])
        trailing = length - 1 - int(false_indices[-1])
        boundaries = np.concatenate(
            (np.asarray((-1,), dtype=np.int64), false_indices, np.asarray((length,), dtype=np.int64))
        )
        internal = int(np.max(np.diff(boundaries) - 1))
        self.longest = max(self.longest, self._carry + leading, internal)
        self._carry = trailing


class _SustainedDcEvidence:
    def __init__(self, sample_rate: int) -> None:
        self.window_frames = round(_DC_WINDOW_SECONDS * sample_rate)
        self._count = 0
        self._sum = np.zeros(2, dtype=np.float64)
        self._sum_squares = np.zeros(2, dtype=np.float64)
        self.qualifying_window_count = np.zeros(2, dtype=np.int64)
        self.longest_qualifying_run = np.zeros(2, dtype=np.int64)
        self._current_run = np.zeros(2, dtype=np.int64)
        self.max_abs_mean = np.zeros(2, dtype=np.float64)
        self.max_mean_to_rms = np.zeros(2, dtype=np.float64)

    def process(self, samples: np.ndarray) -> None:
        offset = 0
        while offset < len(samples):
            take = min(self.window_frames - self._count, len(samples) - offset)
            section = samples[offset : offset + take]
            self._sum += np.sum(section, axis=0, dtype=np.float64)
            self._sum_squares += np.sum(section * section, axis=0, dtype=np.float64)
            self._count += take
            offset += take
            if self._count == self.window_frames:
                mean = self._sum / self.window_frames
                rms = np.sqrt(self._sum_squares / self.window_frames)
                ratio = np.divide(
                    np.abs(mean),
                    rms,
                    out=np.zeros(2, dtype=np.float64),
                    where=rms > 0.0,
                )
                threshold = np.maximum(0.01, 0.25 * rms)
                qualifies = np.abs(mean) > threshold
                self.max_abs_mean = np.maximum(self.max_abs_mean, np.abs(mean))
                self.max_mean_to_rms = np.maximum(self.max_mean_to_rms, ratio)
                self.qualifying_window_count += qualifies.astype(np.int64)
                self._current_run = np.where(qualifies, self._current_run + 1, 0)
                self.longest_qualifying_run = np.maximum(
                    self.longest_qualifying_run, self._current_run
                )
                self._count = 0
                self._sum.fill(0.0)
                self._sum_squares.fill(0.0)

    def report(self) -> dict[str, Any]:
        return {
            "window_seconds": _DC_WINDOW_SECONDS,
            "window_frames": self.window_frames,
            "required_consecutive_windows": _DC_REQUIRED_WINDOWS,
            "criterion": "abs(mean) > max(0.01, 0.25 * window_rms)",
            "left": {
                "qualifying_window_count": int(self.qualifying_window_count[0]),
                "longest_qualifying_run": int(self.longest_qualifying_run[0]),
                "max_abs_mean": _finite_float(self.max_abs_mean[0]),
                "max_mean_to_rms_ratio": _finite_float(
                    self.max_mean_to_rms[0], digits=9
                ),
            },
            "right": {
                "qualifying_window_count": int(self.qualifying_window_count[1]),
                "longest_qualifying_run": int(self.longest_qualifying_run[1]),
                "max_abs_mean": _finite_float(self.max_abs_mean[1]),
                "max_mean_to_rms_ratio": _finite_float(
                    self.max_mean_to_rms[1], digits=9
                ),
            },
        }


class _KWeighting:
    """One streaming stereo implementation of the 48 kHz BS.1770 filter."""

    def __init__(self) -> None:
        self._state = np.zeros((2, 2, 2), dtype=np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        result = np.empty(len(samples), dtype=np.float64)
        b10, b11, b12 = _K_STAGE_1_B
        _, a11, a12 = _K_STAGE_1_A
        b20, b21, b22 = _K_STAGE_2_B
        _, a21, a22 = _K_STAGE_2_A
        state = self._state
        left_10 = float(state[0, 0, 0])
        left_11 = float(state[0, 0, 1])
        left_20 = float(state[0, 1, 0])
        left_21 = float(state[0, 1, 1])
        right_10 = float(state[1, 0, 0])
        right_11 = float(state[1, 0, 1])
        right_20 = float(state[1, 1, 0])
        right_21 = float(state[1, 1, 1])
        for index in range(len(samples)):
            left = float(samples[index, 0])
            right = float(samples[index, 1])

            left_stage_1 = b10 * left + left_10
            left_10 = b11 * left - a11 * left_stage_1 + left_11
            left_11 = b12 * left - a12 * left_stage_1
            left_stage_2 = b20 * left_stage_1 + left_20
            left_20 = b21 * left_stage_1 - a21 * left_stage_2 + left_21
            left_21 = b22 * left_stage_1 - a22 * left_stage_2

            right_stage_1 = b10 * right + right_10
            right_10 = b11 * right - a11 * right_stage_1 + right_11
            right_11 = b12 * right - a12 * right_stage_1
            right_stage_2 = b20 * right_stage_1 + right_20
            right_20 = b21 * right_stage_1 - a21 * right_stage_2 + right_21
            right_21 = b22 * right_stage_1 - a22 * right_stage_2

            result[index] = left_stage_2 * left_stage_2 + right_stage_2 * right_stage_2

        state[0, 0, 0] = left_10
        state[0, 0, 1] = left_11
        state[0, 1, 0] = left_20
        state[0, 1, 1] = left_21
        state[1, 0, 0] = right_10
        state[1, 0, 1] = right_11
        state[1, 1, 0] = right_20
        state[1, 1, 1] = right_21
        return result


class _LoudnessMeter:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.momentary_frames = round(0.400 * sample_rate)
        self.short_term_frames = round(3.000 * sample_rate)
        self.hop_frames = round(0.100 * sample_rate)
        self._filter = _KWeighting()
        self._history = np.empty(0, dtype=np.float64)
        self._frame_count = 0
        self._max_momentary_power: float | None = None
        self._max_short_term_power: float | None = None
        self._integrated_blocks: list[float] = []
        self._lra_short_term_levels: list[float] = []

    @staticmethod
    def _window_sums(
        cumulative: np.ndarray,
        *,
        extended_start: int,
        chunk_start: int,
        chunk_end: int,
        window_frames: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        first_end = max(chunk_start + 1, window_frames)
        if first_end > chunk_end:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        ends = np.arange(first_end, chunk_end + 1, dtype=np.int64)
        right = ends - extended_start
        left = right - window_frames
        return ends, cumulative[right] - cumulative[left]

    def process(self, samples: np.ndarray) -> None:
        powers = self._filter.process(samples)
        chunk_start = self._frame_count
        chunk_end = chunk_start + len(powers)
        extended = np.concatenate((self._history, powers))
        extended_start = chunk_start - len(self._history)
        cumulative = np.empty(len(extended) + 1, dtype=np.float64)
        cumulative[0] = 0.0
        np.cumsum(extended, dtype=np.float64, out=cumulative[1:])

        moment_ends, moment_sums = self._window_sums(
            cumulative,
            extended_start=extended_start,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            window_frames=self.momentary_frames,
        )
        if moment_sums.size:
            maximum = float(np.max(moment_sums) / self.momentary_frames)
            self._max_momentary_power = (
                maximum
                if self._max_momentary_power is None
                else max(self._max_momentary_power, maximum)
            )
            aligned = ((moment_ends - self.momentary_frames) % self.hop_frames) == 0
            self._integrated_blocks.extend(
                (moment_sums[aligned] / self.momentary_frames).tolist()
            )

        short_ends, short_sums = self._window_sums(
            cumulative,
            extended_start=extended_start,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            window_frames=self.short_term_frames,
        )
        if short_sums.size:
            maximum = float(np.max(short_sums) / self.short_term_frames)
            self._max_short_term_power = (
                maximum
                if self._max_short_term_power is None
                else max(self._max_short_term_power, maximum)
            )
            aligned = ((short_ends - self.short_term_frames) % self.hop_frames) == 0
            aligned_powers = short_sums[aligned] / self.short_term_frames
            self._lra_short_term_levels.extend(
                [
                    -math.inf if power <= 0.0 else -0.691 + 10.0 * math.log10(power)
                    for power in aligned_powers
                ]
            )

        keep = min(self.short_term_frames - 1, len(extended))
        self._history = extended[-keep:].copy() if keep else np.empty(0, dtype=np.float64)
        self._frame_count = chunk_end

    def report(self) -> dict[str, Any]:
        block_powers = np.asarray(self._integrated_blocks, dtype=np.float64)
        block_levels = np.full(block_powers.shape, -math.inf, dtype=np.float64)
        positive = block_powers > 0.0
        block_levels[positive] = -0.691 + 10.0 * np.log10(block_powers[positive])
        absolute_mask = block_levels > -70.0
        absolute_count = int(np.count_nonzero(absolute_mask))
        relative_threshold: float | None = None
        integrated_power: float | None = None
        final_count = 0
        if absolute_count:
            absolute_power = float(np.mean(block_powers[absolute_mask]))
            absolute_loudness = _db_power(absolute_power)
            assert absolute_loudness is not None
            relative_threshold = absolute_loudness - 10.0
            final_mask = absolute_mask & (block_levels > relative_threshold)
            final_count = int(np.count_nonzero(final_mask))
            if final_count:
                integrated_power = float(np.mean(block_powers[final_mask]))

        integrated = _db_power(integrated_power or 0.0)
        momentary = _db_power(self._max_momentary_power or 0.0)
        short_term = _db_power(self._max_short_term_power or 0.0)
        duration = self._frame_count / self.sample_rate
        if integrated is not None:
            reason: str | None = None
        elif not len(block_powers):
            reason = "insufficient duration for one 400 ms loudness block"
        else:
            reason = "no 400 ms loudness block passed the -70 LUFS absolute gate"
        return {
            "status": "available" if integrated is not None else "unavailable",
            "reason": reason,
            "integrated_lufs": _optional_float(integrated, digits=6),
            "max_momentary_lufs": _optional_float(momentary, digits=6),
            "max_short_term_lufs": _optional_float(short_term, digits=6),
            "block_seconds": 0.4,
            "block_overlap_ratio": 0.75,
            "block_count": int(len(block_powers)),
            "absolute_gate_lufs": -70.0,
            "absolute_gated_block_count": absolute_count,
            "relative_gate_lu": -10.0,
            "relative_gate_lufs": _optional_float(relative_threshold, digits=6),
            "final_gated_block_count": final_count,
            "lra": _calculate_lra(self._lra_short_term_levels, duration),
        }


def _calculate_lra(
    short_term_levels: list[float] | np.ndarray,
    duration_seconds: float,
) -> dict[str, Any]:
    values = np.asarray(short_term_levels, dtype=np.float64)
    finite = values[np.isfinite(values)]
    absolute = finite[finite > -70.0]
    stability = "stable" if duration_seconds >= 60.0 else "not_recommended"
    base: dict[str, Any] = {
        "status": "unavailable",
        "stability": stability,
        "value_lu": None,
        "short_term_window_seconds": 3.0,
        "measurement_rate_hz": 10.0,
        "absolute_gate_lufs": -70.0,
        "relative_gate_lu": -20.0,
        "relative_gate_lufs": None,
        "low_percentile": 10.0,
        "high_percentile": 95.0,
        "short_term_sample_count": int(values.size),
        "absolute_gated_sample_count": int(absolute.size),
        "relative_gated_sample_count": 0,
        "p10_lufs": None,
        "p95_lufs": None,
    }
    if absolute.size == 0:
        return base
    absolute_loudness = 10.0 * math.log10(float(np.mean(10.0 ** (absolute / 10.0))))
    relative_threshold = absolute_loudness - 20.0
    relative = np.sort(absolute[absolute > relative_threshold])
    base["relative_gate_lufs"] = _finite_float(relative_threshold, digits=6)
    base["relative_gated_sample_count"] = int(relative.size)
    if relative.size == 0:
        return base

    def percentile(percent: float) -> float:
        index = int(math.floor((relative.size - 1) * percent / 100.0 + 0.5))
        return float(relative[index])

    low = percentile(10.0)
    high = percentile(95.0)
    base.update(
        {
            "status": "available",
            "value_lu": _finite_float(high - low, digits=6),
            "p10_lufs": _finite_float(low, digits=6),
            "p95_lufs": _finite_float(high, digits=6),
        }
    )
    return base


class _TruePeakMeter:
    """Streaming Annex 2 polyphase FIR with silence on both boundaries."""

    def __init__(self) -> None:
        self._history = np.zeros((_TRUE_PEAK_POLYPHASE.shape[0] - 1, 2), dtype=np.float64)
        self._peaks = np.zeros(2, dtype=np.float64)
        self._finished = False

    def process(self, samples: np.ndarray) -> None:
        if self._finished:
            raise RuntimeError("true-peak meter has already been finalised")
        if not len(samples):
            return
        extended = np.concatenate((self._history, samples), axis=0)
        start = len(self._history)
        stop = start + len(samples)
        for channel in range(2):
            source = extended[:, channel]
            for phase in range(4):
                filtered = np.convolve(
                    source, _TRUE_PEAK_POLYPHASE[:, phase], mode="full"
                )
                peak = float(np.max(np.abs(filtered[start:stop])))
                self._peaks[channel] = max(self._peaks[channel], peak)
        self._history = extended[-(len(_TRUE_PEAK_POLYPHASE) - 1) :].copy()

    def finish(self) -> np.ndarray:
        if not self._finished:
            tail = np.zeros((len(_TRUE_PEAK_POLYPHASE) - 1, 2), dtype=np.float64)
            self.process(tail)
            self._finished = True
        return self._peaks.copy()


def _window_metrics(samples: np.ndarray) -> dict[str, Any]:
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
    return {
        "frame_count": int(len(samples)),
        "sample_peak": _finite_float(peak),
        "sample_peak_dbfs": _optional_float(_db_amplitude(peak), digits=6),
        "stereo_rms": _finite_float(rms),
        "stereo_rms_dbfs": _optional_float(_db_amplitude(rms), digits=6),
    }


class _Accumulator:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.input_frame_count = 0
        self.frame_count = 0
        self._pending = np.empty((0, 2), dtype=np.float64)
        self._finished = False
        self.sums = np.zeros(2, dtype=np.float64)
        self.sum_squares = np.zeros(2, dtype=np.float64)
        self.sum_products = 0.0
        self.mid_sum_squares = 0.0
        self.side_sum_squares = 0.0
        self.peaks = np.zeros(2, dtype=np.float64)
        self.peak_frames: list[int | None] = [None, None]
        self.sample_peak_frame: int | None = None
        self.sample_peak_channel: int | None = None
        self.full_scale = [_RunStats(), _RunStats()]
        self.zero_samples = [_RunStats(), _RunStats()]
        self.zero_frames = _RunStats()
        self.dc = _SustainedDcEvidence(sample_rate)
        self.leading = np.empty((0, 2), dtype=np.float64)
        self.trailing = np.empty((0, 2), dtype=np.float64)
        self.tail_frames = max(1, round(_TAIL_SECONDS * sample_rate))
        self.last_frame: np.ndarray | None = None
        self.previous_frame: np.ndarray | None = None
        self.loudness = _LoudnessMeter(sample_rate) if sample_rate == _CERTIFIED_SAMPLE_RATE else None
        self.true_peak = _TruePeakMeter() if sample_rate == _CERTIFIED_SAMPLE_RATE else None

    def process(self, samples: np.ndarray) -> None:
        if self._finished:
            raise RuntimeError("post-render accumulator has already been finalised")
        if samples.ndim != 2 or samples.shape[1] != 2:
            raise ValueError("decoded WAV block is not stereo")
        if not np.all(np.isfinite(samples)):
            raise ValueError("delivered PCM contains non-finite decoded samples")
        count = len(samples)
        if count == 0:
            return
        self.input_frame_count += count
        buffered = (
            samples
            if len(self._pending) == 0
            else np.concatenate((self._pending, samples), axis=0)
        )
        offset = 0
        while len(buffered) - offset >= _READ_BLOCK_FRAMES:
            self._process_canonical_block(
                buffered[offset : offset + _READ_BLOCK_FRAMES]
            )
            offset += _READ_BLOCK_FRAMES
        self._pending = buffered[offset:].copy()

    def _process_canonical_block(self, samples: np.ndarray) -> None:
        count = len(samples)
        absolute_samples = np.abs(samples)
        flat_index = int(np.argmax(absolute_samples))
        flat_peak = float(absolute_samples.reshape(-1)[flat_index])
        previous_global_peak = float(np.max(self.peaks))
        if self.sample_peak_frame is None or flat_peak > previous_global_peak:
            self.sample_peak_frame = self.frame_count + flat_index // 2
            self.sample_peak_channel = flat_index % 2
        self.sums += np.sum(samples, axis=0, dtype=np.float64)
        self.sum_squares += np.sum(samples * samples, axis=0, dtype=np.float64)
        self.sum_products += float(np.dot(samples[:, 0], samples[:, 1]))
        mid = 0.5 * (samples[:, 0] + samples[:, 1])
        side = 0.5 * (samples[:, 0] - samples[:, 1])
        self.mid_sum_squares += float(np.dot(mid, mid))
        self.side_sum_squares += float(np.dot(side, side))
        for channel in range(2):
            absolute = absolute_samples[:, channel]
            local_peak_frame = int(np.argmax(absolute))
            local_peak = float(absolute[local_peak_frame])
            if self.peak_frames[channel] is None or local_peak > self.peaks[channel]:
                self.peaks[channel] = local_peak
                self.peak_frames[channel] = self.frame_count + local_peak_frame
            self.full_scale[channel].update(absolute >= _FULL_SCALE_FLOOR)
            self.zero_samples[channel].update(samples[:, channel] == 0.0)
        self.zero_frames.update(np.all(samples == 0.0, axis=1))
        self.dc.process(samples)

        if len(self.leading) < self.tail_frames:
            needed = self.tail_frames - len(self.leading)
            self.leading = np.concatenate((self.leading, samples[:needed].copy()), axis=0)
        self.trailing = np.concatenate((self.trailing, samples), axis=0)[-self.tail_frames :].copy()
        if count == 1:
            self.previous_frame = None if self.last_frame is None else self.last_frame.copy()
            self.last_frame = samples[-1].copy()
        else:
            self.previous_frame = samples[-2].copy()
            self.last_frame = samples[-1].copy()

        if self.loudness is not None:
            self.loudness.process(samples)
        if self.true_peak is not None:
            self.true_peak.process(samples)
        self.frame_count += count

    def finish(self) -> None:
        if self._finished:
            return
        if len(self._pending):
            self._process_canonical_block(self._pending)
        self._pending = np.empty((0, 2), dtype=np.float64)
        self._finished = True

    def report(self) -> dict[str, Any]:
        self.finish()
        if self.frame_count <= 0 or self.last_frame is None:
            raise ValueError("delivered WAV contains no audio frames")
        channel_rms = np.sqrt(self.sum_squares / self.frame_count)
        channel_means = self.sums / self.frame_count
        stereo_power = float(np.sum(self.sum_squares) / (2.0 * self.frame_count))
        stereo_rms = math.sqrt(max(0.0, stereo_power))
        sample_peak = float(np.max(self.peaks))
        mono_power = self.mid_sum_squares / self.frame_count
        mono_rms = math.sqrt(max(0.0, mono_power))
        side_power = self.side_sum_squares / self.frame_count
        side_rms = math.sqrt(max(0.0, side_power))
        mono_delta = (
            None
            if stereo_rms <= 0.0 or mono_rms <= 0.0
            else 20.0 * math.log10(mono_rms / stereo_rms)
        )
        side_to_mid_ratio = None if mono_rms <= 0.0 else side_rms / mono_rms
        side_to_mid_db = (
            None
            if side_to_mid_ratio is None or side_to_mid_ratio <= 0.0
            else 20.0 * math.log10(side_to_mid_ratio)
        )
        centred_left = float(self.sum_squares[0] - self.sums[0] ** 2 / self.frame_count)
        centred_right = float(self.sum_squares[1] - self.sums[1] ** 2 / self.frame_count)
        covariance = float(self.sum_products - self.sums[0] * self.sums[1] / self.frame_count)
        correlation = (
            covariance / math.sqrt(centred_left * centred_right)
            if centred_left > 0.0 and centred_right > 0.0
            else None
        )
        if correlation is not None:
            correlation = max(-1.0, min(1.0, correlation))
        level_difference = (
            None
            if channel_rms[0] <= 0.0 or channel_rms[1] <= 0.0
            else abs(20.0 * math.log10(channel_rms[0] / channel_rms[1]))
        )
        crest = (
            None
            if sample_peak <= 0.0 or stereo_rms <= 0.0
            else 20.0 * math.log10(sample_peak / stereo_rms)
        )

        leading = _window_metrics(self.leading)
        trailing = _window_metrics(self.trailing)
        tail_peak = float(trailing["sample_peak"])
        tail_relative = (
            None
            if sample_peak <= 0.0 or tail_peak <= 0.0
            else 20.0 * math.log10(tail_peak / sample_peak)
        )
        trailing["peak_relative_to_full_track_db"] = _optional_float(
            tail_relative, digits=6
        )
        final_jump = (
            None
            if self.previous_frame is None
            else self.last_frame - self.previous_frame
        )

        if self.true_peak is None:
            true_peak_report = {
                "status": "unavailable",
                "reason": "standards measurement is currently certified only at 48000 Hz",
                "oversampling_ratio": None,
                "oversampled_rate_hz": None,
                "left": None,
                "right": None,
                "maximum": None,
                "left_dbtp": None,
                "right_dbtp": None,
                "maximum_dbtp": None,
            }
        else:
            true_peaks = self.true_peak.finish()
            maximum = float(np.max(true_peaks))
            true_peak_report = {
                "status": "available",
                "reason": None,
                "oversampling_ratio": 4,
                "oversampled_rate_hz": 192_000,
                "left": _finite_float(true_peaks[0]),
                "right": _finite_float(true_peaks[1]),
                "maximum": _finite_float(maximum),
                "left_dbtp": _optional_float(_db_amplitude(true_peaks[0]), digits=6),
                "right_dbtp": _optional_float(_db_amplitude(true_peaks[1]), digits=6),
                "maximum_dbtp": _optional_float(_db_amplitude(maximum), digits=6),
            }

        if self.loudness is None:
            loudness_report: dict[str, Any] = {
                "status": "unavailable",
                "reason": "standards measurement is currently certified only at 48000 Hz",
                "integrated_lufs": None,
                "max_momentary_lufs": None,
                "max_short_term_lufs": None,
                "block_seconds": 0.4,
                "block_overlap_ratio": 0.75,
                "block_count": 0,
                "absolute_gate_lufs": -70.0,
                "absolute_gated_block_count": 0,
                "relative_gate_lu": -10.0,
                "relative_gate_lufs": None,
                "final_gated_block_count": 0,
                "lra": _calculate_lra([], self.frame_count / self.sample_rate),
            }
        else:
            loudness_report = self.loudness.report()

        return {
            "sample": {
                "sample_peak": _finite_float(sample_peak),
                "sample_peak_dbfs": _optional_float(_db_amplitude(sample_peak), digits=6),
                "sample_peak_frame": self.sample_peak_frame,
                "sample_peak_channel": (
                    "left" if self.sample_peak_channel == 0 else "right"
                ),
                "stereo_rms": _finite_float(stereo_rms),
                "stereo_rms_dbfs": _optional_float(_db_amplitude(stereo_rms), digits=6),
                "crest_factor_db": _optional_float(crest, digits=6),
            },
            "channels": {
                "left": {
                    "sample_peak": _finite_float(self.peaks[0]),
                    "sample_peak_dbfs": _optional_float(_db_amplitude(self.peaks[0]), digits=6),
                    "sample_peak_frame": self.peak_frames[0],
                    "rms": _finite_float(channel_rms[0]),
                    "rms_dbfs": _optional_float(_db_amplitude(channel_rms[0]), digits=6),
                    "dc_mean": _finite_float(channel_means[0]),
                    "dc_level_dbfs": _optional_float(_db_amplitude(abs(channel_means[0])), digits=6),
                },
                "right": {
                    "sample_peak": _finite_float(self.peaks[1]),
                    "sample_peak_dbfs": _optional_float(_db_amplitude(self.peaks[1]), digits=6),
                    "sample_peak_frame": self.peak_frames[1],
                    "rms": _finite_float(channel_rms[1]),
                    "rms_dbfs": _optional_float(_db_amplitude(channel_rms[1]), digits=6),
                    "dc_mean": _finite_float(channel_means[1]),
                    "dc_level_dbfs": _optional_float(_db_amplitude(abs(channel_means[1])), digits=6),
                },
                "rms_level_difference_db": _optional_float(level_difference, digits=6),
            },
            "stereo": {
                "left_right_correlation": _optional_float(correlation, digits=9),
                "mono_fold_rms": _finite_float(mono_rms),
                "mono_fold_rms_dbfs": _optional_float(_db_amplitude(mono_rms), digits=6),
                "mono_fold_delta_db": _optional_float(mono_delta, digits=6),
                "mono_fold_silent": mono_rms == 0.0,
                "mid_rms": _finite_float(mono_rms),
                "mid_rms_dbfs": _optional_float(_db_amplitude(mono_rms), digits=6),
                "side_rms": _finite_float(side_rms),
                "side_rms_dbfs": _optional_float(_db_amplitude(side_rms), digits=6),
                "side_to_mid_ratio": _optional_float(side_to_mid_ratio, digits=9),
                "side_to_mid_db": _optional_float(side_to_mid_db, digits=6),
            },
            "extrema": {
                "full_scale_threshold": _finite_float(_FULL_SCALE_FLOOR),
                "full_scale_sample_count": self.full_scale[0].count + self.full_scale[1].count,
                "longest_full_scale_run": max(
                    self.full_scale[0].longest, self.full_scale[1].longest
                ),
                "left_full_scale_sample_count": self.full_scale[0].count,
                "right_full_scale_sample_count": self.full_scale[1].count,
                "left_longest_full_scale_run": self.full_scale[0].longest,
                "right_longest_full_scale_run": self.full_scale[1].longest,
                "exact_zero_sample_count": self.zero_samples[0].count + self.zero_samples[1].count,
                "left_exact_zero_sample_count": self.zero_samples[0].count,
                "right_exact_zero_sample_count": self.zero_samples[1].count,
                "longest_left_zero_run": self.zero_samples[0].longest,
                "longest_right_zero_run": self.zero_samples[1].longest,
                "exact_zero_frame_count": self.zero_frames.count,
                "longest_exact_zero_frame_run": self.zero_frames.longest,
            },
            "boundaries": {
                "window_seconds": _TAIL_SECONDS,
                "leading": leading,
                "trailing": trailing,
                "final_sample": {
                    "left": _finite_float(self.last_frame[0]),
                    "right": _finite_float(self.last_frame[1]),
                    "maximum_absolute": _finite_float(float(np.max(np.abs(self.last_frame)))),
                },
                "final_jump": {
                    "left": None if final_jump is None else _finite_float(final_jump[0]),
                    "right": None if final_jump is None else _finite_float(final_jump[1]),
                    "maximum_absolute": (
                        None
                        if final_jump is None
                        else _finite_float(float(np.max(np.abs(final_jump))))
                    ),
                },
            },
            "sustained_dc": self.dc.report(),
            "true_peak": true_peak_report,
            "loudness": loudness_report,
        }


def _quality_issues(
    *,
    artifact_label: str,
    measurements: dict[str, Any],
    expected_activity: bool,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    scope = {"kind": "artifact", "path": artifact_label}
    sample_peak = float(measurements["sample"]["sample_peak"])
    if expected_activity and sample_peak == 0.0:
        issues.append(
            build_issue(
                severity="error",
                code="render.expected_activity_silent",
                stage="post_render_delivery",
                message="乐谱声明存在活动内容，但交付 WAV 是精确数字静音。",
                basis="render_contract",
                confidence="high",
                scope=scope,
                evidence={"sample_peak": sample_peak, "blocking_threshold": 0.0},
                suggestions=["检查事件计划、乐器资源、增益与实际渲染输入。"],
            )
        )
    elif 0.0 < sample_peak <= _NEAR_SILENT_PEAK:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.near_silent_delivery",
                stage="post_render_delivery",
                message="交付 WAV 接近静音；这可能是有意的极弱奏，也可能表示渲染内容缺失。",
                basis="delivery_measurement",
                confidence="high",
                scope=scope,
                evidence={
                    "sample_peak": sample_peak,
                    "warning_threshold": _NEAR_SILENT_PEAK,
                },
                suggestions=["结合乐谱和试听确认极低电平是否符合创作意图。"],
            )
        )

    true_peak = measurements["true_peak"]
    maximum_dbtp = true_peak["maximum_dbtp"]
    if maximum_dbtp is not None and maximum_dbtp > 0.0:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.true_peak_overload_risk",
                stage="post_render_delivery",
                message="重建波形的 True Peak 超过 0 dBTP，后续转换或播放链可能过载。",
                basis="ITU-R BS.1770-5 Annex 2",
                confidence="high",
                scope=scope,
                evidence={"maximum_dbtp": maximum_dbtp, "threshold_dbtp": 0.0},
                suggestions=["试听并检查增益；若并非有意效果，可在源头保留更多峰值余量。"],
            )
        )
    elif maximum_dbtp is not None and maximum_dbtp > -1.0:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.true_peak_headroom",
                stage="post_render_delivery",
                message="True Peak 高于 -1 dBTP；PCM 本身仍可交付，但通用生产余量偏小。",
                basis="EBU R 128 generic linear audio guidance",
                confidence="high",
                scope=scope,
                evidence={"maximum_dbtp": maximum_dbtp, "guideline_dbtp": -1.0},
                suggestions=["按目标平台决定是否降低整体增益；自检不会自动归一化。"],
            )
        )

    extrema = measurements["extrema"]
    if extrema["longest_full_scale_run"] >= _FULL_SCALE_RUN_WARNING:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.full_scale_plateau",
                stage="post_render_delivery",
                message="检测到连续满幅 PCM 样本平台，存在数字削波迹象。",
                basis="EBU QC 0005B",
                confidence="high",
                scope=scope,
                evidence={
                    "longest_full_scale_run": extrema["longest_full_scale_run"],
                    "configured_run_threshold": _FULL_SCALE_RUN_WARNING,
                    "full_scale_sample_count": extrema["full_scale_sample_count"],
                },
                suggestions=["检查量化前峰值；若属于有意失真，可保留并记录创作理由。"],
            )
        )

    dc = measurements["sustained_dc"]
    if max(
        dc["left"]["longest_qualifying_run"],
        dc["right"]["longest_qualifying_run"],
    ) >= _DC_REQUIRED_WINDOWS:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.sustained_dc_candidate",
                stage="post_render_delivery",
                message="多个连续 400 ms 窗口呈现显著同向均值，可能存在持续 DC 分量。",
                basis="Tianlai conservative policy informed by ITU-R BS.647-3",
                confidence="medium",
                scope=scope,
                evidence=dc,
                suggestions=["结合低音乐器和波形试听复核；该结果不会自动触发高通处理。"],
            )
        )

    stereo = measurements["stereo"]
    mono_delta = stereo["mono_fold_delta_db"]
    if sample_peak > 0.0 and (
        stereo["mono_fold_silent"]
        or (mono_delta is not None and mono_delta < -6.0)
    ):
        issues.append(
            build_issue(
                severity="warning",
                code="audio.mono_fold_cancellation",
                stage="post_render_delivery",
                message="立体声折叠为单声道时出现明显电平损失或完全抵消。",
                basis="delivery_measurement",
                confidence="high",
                scope=scope,
                evidence={
                    "mono_fold_silent": stereo["mono_fold_silent"],
                    "mono_fold_delta_db": mono_delta,
                    "warning_threshold_db": -6.0,
                },
                suggestions=["在单声道试听中确认抵消是否符合空间设计。"],
            )
        )

    channels = measurements["channels"]
    left_rms = float(channels["left"]["rms"])
    right_rms = float(channels["right"]["rms"])
    difference = channels["rms_level_difference_db"]
    one_sided = (
        left_rms <= _PCM24_LSB < right_rms
        or right_rms <= _PCM24_LSB < left_rms
    )
    if one_sided or (difference is not None and difference > 40.0):
        issues.append(
            build_issue(
                severity="warning",
                code="audio.extreme_channel_imbalance",
                stage="post_render_delivery",
                message="左右声道全文件 RMS 相差超过 40 dB，或其中一侧近似静音。",
                basis="delivery_measurement",
                confidence="high",
                scope=scope,
                evidence={
                    "left_rms": left_rms,
                    "right_rms": right_rms,
                    "rms_level_difference_db": difference,
                    "warning_threshold_db": 40.0,
                },
                suggestions=["确认极端声像是否为有意安排，并进行耳机与扬声器试听。"],
            )
        )

    boundaries = measurements["boundaries"]
    tail_relative = boundaries["trailing"]["peak_relative_to_full_track_db"]
    final_sample = boundaries["final_sample"]["maximum_absolute"]
    final_jump = boundaries["final_jump"]["maximum_absolute"]
    tail_hot = tail_relative is not None and tail_relative > -50.0
    sample_hot = final_sample >= 0.02
    jump_hot = final_jump is not None and final_jump >= 0.05
    if tail_hot or sample_hot or jump_hot:
        issues.append(
            build_issue(
                severity="warning",
                code="audio.tail_boundary_candidate",
                stage="post_render_delivery",
                message="文件尾部仍有较强信号或末帧跳变，可能截断残响并产生边界点击。",
                basis="Tianlai conservative delivery policy",
                confidence="medium",
                scope=scope,
                evidence={
                    "tail_peak_relative_db": tail_relative,
                    "tail_relative_warning_db": -50.0,
                    "final_sample_maximum_absolute": final_sample,
                    "final_sample_warning": 0.02,
                    "final_jump_maximum_absolute": final_jump,
                    "final_jump_warning": 0.05,
                },
                suggestions=["试听结尾并确认是否需要在乐谱或尾音时长中留出更多空间。"],
            )
        )
    return issues


def _measurement_contract() -> dict[str, Any]:
    return {
        "version": MEASUREMENT_VERSION,
        "sample_domain": "decoded_pcm24_float64",
        "loudness": "ITU-R BS.1770-5 48kHz K-weighting; EBU Tech 3341",
        "lra": "EBU Tech 3342-2023",
        "true_peak": "ITU-R BS.1770-5 Annex 2 four-phase 48-tap FIR",
        "certified_sample_rate_hz": _CERTIFIED_SAMPLE_RATE,
        "audio_modified": False,
    }


def _policy_contract() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "name": "tianlai.strict_foundation.v1",
        "structural_mismatch_raises": True,
        "expected_activity_silence_blocks": True,
        "true_peak_is_advisory": True,
        "loudness_targets_block": False,
        "lra_blocks": False,
        "automatic_audio_changes": False,
        "thresholds": {
            "expected_activity_maximum_peak": 0.0,
            "near_silent_peak": _finite_float(_NEAR_SILENT_PEAK),
            "true_peak_overload_warning_dbtp": 0.0,
            "true_peak_headroom_warning_dbtp": -1.0,
            "full_scale_run_warning": _FULL_SCALE_RUN_WARNING,
            "dc_window_seconds": _DC_WINDOW_SECONDS,
            "dc_consecutive_windows": _DC_REQUIRED_WINDOWS,
            "mono_fold_delta_warning_db": -6.0,
            "channel_difference_warning_db": 40.0,
            "tail_window_seconds": _TAIL_SECONDS,
            "tail_relative_warning_db": -50.0,
            "final_sample_warning": 0.02,
            "final_jump_warning": 0.05,
        },
    }


def analyze_rendered_wav(
    path: str | Path,
    artifact_path: str | PurePath,
    expected_sample_rate: int,
    expected_frame_count: int,
    expected_activity: bool,
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Analyse exactly one delivered WAV without modifying it.

    ``artifact_path`` is a portable label for the report, not a filesystem
    lookup path.  Structural mismatches raise; the returned issues only cover
    the quality-review policy.
    """

    if (
        isinstance(expected_sample_rate, bool)
        or not isinstance(expected_sample_rate, int)
        or not 8_000 <= expected_sample_rate <= 384_000
    ):
        raise ValueError("expected_sample_rate must be an integer from 8000 to 384000")
    if (
        isinstance(expected_frame_count, bool)
        or not isinstance(expected_frame_count, int)
        or expected_frame_count <= 0
    ):
        raise ValueError("expected_frame_count must be a positive integer")
    if not isinstance(expected_activity, bool):
        raise ValueError("expected_activity must be boolean")
    artifact_label = _portable_artifact_label(artifact_path)
    plan_digest = _validate_sha256(plan_sha256, label="plan_sha256")
    source = Path(path)
    if source.is_symlink():
        raise ValueError("post-render check refuses symbolic-link WAV inputs")
    try:
        before = source.stat()
    except OSError as error:
        raise ValueError(f"cannot inspect delivered WAV: {source}") from error
    if not source.is_file() or before.st_size <= 0:
        raise ValueError("delivered WAV must be one non-empty regular file")
    before_sha256 = _sha256_file(source)

    try:
        import soundfile as sf
    except ImportError as error:  # pragma: no cover - required runtime dependency
        raise RuntimeError("post-render check requires soundfile") from error

    try:
        audio = sf.SoundFile(str(source), mode="r")
    except (OSError, RuntimeError) as error:
        raise ValueError("delivered WAV cannot be decoded by soundfile") from error
    with audio:
        container = str(audio.format)
        subtype = str(audio.subtype)
        channels = int(audio.channels)
        sample_rate = int(audio.samplerate)
        declared_frames = int(audio.frames)
        if container != "WAV":
            raise ValueError(f"delivered audio container must be WAV, got {container}")
        if subtype != "PCM_24":
            raise ValueError(f"delivered WAV encoding must be PCM_24, got {subtype}")
        if channels != 2:
            raise ValueError(f"delivered WAV must contain 2 channels, got {channels}")
        if sample_rate != expected_sample_rate:
            raise ValueError(
                "delivered WAV sample rate mismatch: "
                f"expected {expected_sample_rate}, got {sample_rate}"
            )
        if declared_frames != expected_frame_count:
            raise ValueError(
                "delivered WAV frame count mismatch: "
                f"expected {expected_frame_count}, got {declared_frames}"
            )
        accumulator = _Accumulator(sample_rate)
        try:
            while True:
                block = audio.read(
                    _READ_BLOCK_FRAMES,
                    dtype="float64",
                    always_2d=True,
                )
                if len(block) == 0:
                    break
                accumulator.process(block)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("delivered WAV payload cannot be read completely") from error
    if accumulator.input_frame_count != expected_frame_count:
        raise ValueError(
            "decoded WAV frame count mismatch: "
            f"expected {expected_frame_count}, got {accumulator.input_frame_count}"
        )
    accumulator.finish()
    if accumulator.frame_count != expected_frame_count:
        raise ValueError(
            "decoded WAV frame count mismatch: "
            f"expected {expected_frame_count}, got {accumulator.frame_count}"
        )

    after_sha256 = _sha256_file(source)
    try:
        after = source.stat()
    except OSError as error:
        raise ValueError("delivered WAV disappeared during post-render analysis") from error
    if (
        before_sha256 != after_sha256
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ValueError("delivered WAV changed during post-render analysis")

    measurements = accumulator.report()
    issues = _quality_issues(
        artifact_label=artifact_label,
        measurements=measurements,
        expected_activity=expected_activity,
    )
    summary = summarize_issues(issues)
    summary["issue_count"] = len(issues)
    summary["expected_activity"] = expected_activity
    return {
        "$schema": REPORT_SCHEMA_URI,
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "artifact": {
            "path": artifact_label,
            "sha256": after_sha256,
            "size_bytes": int(after.st_size),
        },
        "performance_plan": {"sha256": plan_digest},
        "audio_format": {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": sample_rate,
            "frame_count": accumulator.frame_count,
        },
        "measurement": _measurement_contract(),
        "policy": _policy_contract(),
        "measurements": measurements,
        "issues": issues,
        "summary": summary,
    }


def _require_exact_keys(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PostRenderCheckError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise PostRenderCheckError(
            f"{label} fields do not match version {REPORT_VERSION}: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return value


def _nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PostRenderCheckError(f"{label} must be a non-empty string")
    return value


def _finite_number(
    value: Any,
    *,
    label: str,
    nullable: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        suffix = " or null" if nullable else ""
        raise PostRenderCheckError(f"{label} must be a finite number{suffix}")
    result = float(value)
    if not math.isfinite(result):
        raise PostRenderCheckError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise PostRenderCheckError(f"{label} is below its minimum")
    if maximum is not None and result > maximum:
        raise PostRenderCheckError(f"{label} is above its maximum")
    return result


def _integer(
    value: Any,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PostRenderCheckError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise PostRenderCheckError(f"{label} exceeds its maximum")
    return value


def _validate_window_metrics(
    value: Any,
    *,
    label: str,
    trailing: bool,
    expected_frames: int,
) -> None:
    fields = {
        "frame_count",
        "sample_peak",
        "sample_peak_dbfs",
        "stereo_rms",
        "stereo_rms_dbfs",
    }
    if trailing:
        fields.add("peak_relative_to_full_track_db")
    window = _require_exact_keys(value, fields, label=label)
    if _integer(window["frame_count"], label=f"{label}.frame_count") != expected_frames:
        raise PostRenderCheckError(f"{label}.frame_count does not match the audio")
    _finite_number(
        window["sample_peak"], label=f"{label}.sample_peak", minimum=0.0, maximum=1.0
    )
    _finite_number(
        window["sample_peak_dbfs"], label=f"{label}.sample_peak_dbfs", nullable=True
    )
    _finite_number(
        window["stereo_rms"], label=f"{label}.stereo_rms", minimum=0.0, maximum=1.0
    )
    _finite_number(
        window["stereo_rms_dbfs"], label=f"{label}.stereo_rms_dbfs", nullable=True
    )
    if trailing:
        _finite_number(
            window["peak_relative_to_full_track_db"],
            label=f"{label}.peak_relative_to_full_track_db",
            nullable=True,
        )


def _validate_measurements(
    value: Any,
    *,
    sample_rate: int,
    frame_count: int,
) -> dict[str, Any]:
    measurements = _require_exact_keys(
        value,
        {
            "sample",
            "channels",
            "stereo",
            "extrema",
            "boundaries",
            "sustained_dc",
            "true_peak",
            "loudness",
        },
        label="measurements",
    )

    sample = _require_exact_keys(
        measurements["sample"],
        {
            "sample_peak",
            "sample_peak_dbfs",
            "sample_peak_frame",
            "sample_peak_channel",
            "stereo_rms",
            "stereo_rms_dbfs",
            "crest_factor_db",
        },
        label="measurements.sample",
    )
    sample_peak = _finite_number(
        sample["sample_peak"],
        label="measurements.sample.sample_peak",
        minimum=0.0,
        maximum=1.0,
    )
    _finite_number(
        sample["sample_peak_dbfs"],
        label="measurements.sample.sample_peak_dbfs",
        nullable=True,
    )
    _integer(
        sample["sample_peak_frame"],
        label="measurements.sample.sample_peak_frame",
        maximum=frame_count - 1,
    )
    if sample["sample_peak_channel"] not in {"left", "right"}:
        raise PostRenderCheckError("measurements.sample.sample_peak_channel is invalid")
    _finite_number(
        sample["stereo_rms"],
        label="measurements.sample.stereo_rms",
        minimum=0.0,
        maximum=1.0,
    )
    _finite_number(
        sample["stereo_rms_dbfs"],
        label="measurements.sample.stereo_rms_dbfs",
        nullable=True,
    )
    _finite_number(
        sample["crest_factor_db"],
        label="measurements.sample.crest_factor_db",
        nullable=True,
    )

    channels = _require_exact_keys(
        measurements["channels"],
        {"left", "right", "rms_level_difference_db"},
        label="measurements.channels",
    )
    channel_peaks: list[float] = []
    for name in ("left", "right"):
        channel = _require_exact_keys(
            channels[name],
            {
                "sample_peak",
                "sample_peak_dbfs",
                "sample_peak_frame",
                "rms",
                "rms_dbfs",
                "dc_mean",
                "dc_level_dbfs",
            },
            label=f"measurements.channels.{name}",
        )
        channel_peak = _finite_number(
            channel["sample_peak"],
            label=f"measurements.channels.{name}.sample_peak",
            minimum=0.0,
            maximum=1.0,
        )
        assert channel_peak is not None
        channel_peaks.append(channel_peak)
        _finite_number(
            channel["sample_peak_dbfs"],
            label=f"measurements.channels.{name}.sample_peak_dbfs",
            nullable=True,
        )
        _integer(
            channel["sample_peak_frame"],
            label=f"measurements.channels.{name}.sample_peak_frame",
            maximum=frame_count - 1,
        )
        _finite_number(
            channel["rms"],
            label=f"measurements.channels.{name}.rms",
            minimum=0.0,
            maximum=1.0,
        )
        _finite_number(
            channel["rms_dbfs"],
            label=f"measurements.channels.{name}.rms_dbfs",
            nullable=True,
        )
        _finite_number(
            channel["dc_mean"],
            label=f"measurements.channels.{name}.dc_mean",
            minimum=-1.0,
            maximum=1.0,
        )
        _finite_number(
            channel["dc_level_dbfs"],
            label=f"measurements.channels.{name}.dc_level_dbfs",
            nullable=True,
        )
    if sample_peak != max(channel_peaks):
        raise PostRenderCheckError("sample peak does not match the channel peaks")
    _finite_number(
        channels["rms_level_difference_db"],
        label="measurements.channels.rms_level_difference_db",
        nullable=True,
        minimum=0.0,
    )

    stereo = _require_exact_keys(
        measurements["stereo"],
        {
            "left_right_correlation",
            "mono_fold_rms",
            "mono_fold_rms_dbfs",
            "mono_fold_delta_db",
            "mono_fold_silent",
            "mid_rms",
            "mid_rms_dbfs",
            "side_rms",
            "side_rms_dbfs",
            "side_to_mid_ratio",
            "side_to_mid_db",
        },
        label="measurements.stereo",
    )
    _finite_number(
        stereo["left_right_correlation"],
        label="measurements.stereo.left_right_correlation",
        nullable=True,
        minimum=-1.0,
        maximum=1.0,
    )
    for field in ("mono_fold_rms", "mid_rms", "side_rms"):
        _finite_number(
            stereo[field],
            label=f"measurements.stereo.{field}",
            minimum=0.0,
            maximum=1.0,
        )
    for field in (
        "mono_fold_rms_dbfs",
        "mono_fold_delta_db",
        "mid_rms_dbfs",
        "side_rms_dbfs",
        "side_to_mid_db",
    ):
        _finite_number(
            stereo[field], label=f"measurements.stereo.{field}", nullable=True
        )
    _finite_number(
        stereo["side_to_mid_ratio"],
        label="measurements.stereo.side_to_mid_ratio",
        nullable=True,
        minimum=0.0,
    )
    if not isinstance(stereo["mono_fold_silent"], bool):
        raise PostRenderCheckError("measurements.stereo.mono_fold_silent must be boolean")

    extrema = _require_exact_keys(
        measurements["extrema"],
        {
            "full_scale_threshold",
            "full_scale_sample_count",
            "longest_full_scale_run",
            "left_full_scale_sample_count",
            "right_full_scale_sample_count",
            "left_longest_full_scale_run",
            "right_longest_full_scale_run",
            "exact_zero_sample_count",
            "left_exact_zero_sample_count",
            "right_exact_zero_sample_count",
            "longest_left_zero_run",
            "longest_right_zero_run",
            "exact_zero_frame_count",
            "longest_exact_zero_frame_run",
        },
        label="measurements.extrema",
    )
    _finite_number(
        extrema["full_scale_threshold"],
        label="measurements.extrema.full_scale_threshold",
        minimum=0.0,
        maximum=1.0,
    )
    sample_count_fields = (
        "full_scale_sample_count",
        "left_full_scale_sample_count",
        "right_full_scale_sample_count",
        "exact_zero_sample_count",
        "left_exact_zero_sample_count",
        "right_exact_zero_sample_count",
    )
    for field in sample_count_fields:
        maximum = frame_count * 2 if field in {"full_scale_sample_count", "exact_zero_sample_count"} else frame_count
        _integer(extrema[field], label=f"measurements.extrema.{field}", maximum=maximum)
    for field in (
        "longest_full_scale_run",
        "left_longest_full_scale_run",
        "right_longest_full_scale_run",
        "longest_left_zero_run",
        "longest_right_zero_run",
        "exact_zero_frame_count",
        "longest_exact_zero_frame_run",
    ):
        _integer(extrema[field], label=f"measurements.extrema.{field}", maximum=frame_count)
    if extrema["full_scale_sample_count"] != (
        extrema["left_full_scale_sample_count"]
        + extrema["right_full_scale_sample_count"]
    ):
        raise PostRenderCheckError("full-scale sample counts are inconsistent")
    if extrema["exact_zero_sample_count"] != (
        extrema["left_exact_zero_sample_count"]
        + extrema["right_exact_zero_sample_count"]
    ):
        raise PostRenderCheckError("exact-zero sample counts are inconsistent")

    boundaries = _require_exact_keys(
        measurements["boundaries"],
        {"window_seconds", "leading", "trailing", "final_sample", "final_jump"},
        label="measurements.boundaries",
    )
    if boundaries["window_seconds"] != _TAIL_SECONDS:
        raise PostRenderCheckError("measurements.boundaries.window_seconds is unsupported")
    boundary_frames = min(frame_count, max(1, round(_TAIL_SECONDS * sample_rate)))
    _validate_window_metrics(
        boundaries["leading"],
        label="measurements.boundaries.leading",
        trailing=False,
        expected_frames=boundary_frames,
    )
    _validate_window_metrics(
        boundaries["trailing"],
        label="measurements.boundaries.trailing",
        trailing=True,
        expected_frames=boundary_frames,
    )
    final_sample = _require_exact_keys(
        boundaries["final_sample"],
        {"left", "right", "maximum_absolute"},
        label="measurements.boundaries.final_sample",
    )
    for field in ("left", "right"):
        _finite_number(
            final_sample[field],
            label=f"measurements.boundaries.final_sample.{field}",
            minimum=-1.0,
            maximum=1.0,
        )
    _finite_number(
        final_sample["maximum_absolute"],
        label="measurements.boundaries.final_sample.maximum_absolute",
        minimum=0.0,
        maximum=1.0,
    )
    final_jump = _require_exact_keys(
        boundaries["final_jump"],
        {"left", "right", "maximum_absolute"},
        label="measurements.boundaries.final_jump",
    )
    for field in ("left", "right"):
        _finite_number(
            final_jump[field],
            label=f"measurements.boundaries.final_jump.{field}",
            nullable=frame_count == 1,
            minimum=-2.0,
            maximum=2.0,
        )
    _finite_number(
        final_jump["maximum_absolute"],
        label="measurements.boundaries.final_jump.maximum_absolute",
        nullable=frame_count == 1,
        minimum=0.0,
        maximum=2.0,
    )

    dc = _require_exact_keys(
        measurements["sustained_dc"],
        {
            "window_seconds",
            "window_frames",
            "required_consecutive_windows",
            "criterion",
            "left",
            "right",
        },
        label="measurements.sustained_dc",
    )
    if (
        dc["window_seconds"] != _DC_WINDOW_SECONDS
        or dc["window_frames"] != round(_DC_WINDOW_SECONDS * sample_rate)
        or dc["required_consecutive_windows"] != _DC_REQUIRED_WINDOWS
        or dc["criterion"] != "abs(mean) > max(0.01, 0.25 * window_rms)"
    ):
        raise PostRenderCheckError("sustained-DC measurement contract is inconsistent")
    complete_dc_windows = frame_count // dc["window_frames"]
    for name in ("left", "right"):
        channel_dc = _require_exact_keys(
            dc[name],
            {
                "qualifying_window_count",
                "longest_qualifying_run",
                "max_abs_mean",
                "max_mean_to_rms_ratio",
            },
            label=f"measurements.sustained_dc.{name}",
        )
        _integer(
            channel_dc["qualifying_window_count"],
            label=f"measurements.sustained_dc.{name}.qualifying_window_count",
            maximum=complete_dc_windows,
        )
        _integer(
            channel_dc["longest_qualifying_run"],
            label=f"measurements.sustained_dc.{name}.longest_qualifying_run",
            maximum=complete_dc_windows,
        )
        _finite_number(
            channel_dc["max_abs_mean"],
            label=f"measurements.sustained_dc.{name}.max_abs_mean",
            minimum=0.0,
            maximum=1.0,
        )
        _finite_number(
            channel_dc["max_mean_to_rms_ratio"],
            label=f"measurements.sustained_dc.{name}.max_mean_to_rms_ratio",
            minimum=0.0,
            maximum=1.0,
        )

    true_peak = _require_exact_keys(
        measurements["true_peak"],
        {
            "status",
            "reason",
            "oversampling_ratio",
            "oversampled_rate_hz",
            "left",
            "right",
            "maximum",
            "left_dbtp",
            "right_dbtp",
            "maximum_dbtp",
        },
        label="measurements.true_peak",
    )
    certified = sample_rate == _CERTIFIED_SAMPLE_RATE
    expected_true_peak_status = "available" if certified else "unavailable"
    if true_peak["status"] != expected_true_peak_status:
        raise PostRenderCheckError("true-peak availability does not match the sample rate")
    if certified:
        if (
            true_peak["reason"] is not None
            or true_peak["oversampling_ratio"] != 4
            or true_peak["oversampled_rate_hz"] != 192_000
        ):
            raise PostRenderCheckError("true-peak 48 kHz measurement metadata is invalid")
        for field in ("left", "right", "maximum"):
            _finite_number(
                true_peak[field], label=f"measurements.true_peak.{field}", minimum=0.0
            )
        for field in ("left_dbtp", "right_dbtp", "maximum_dbtp"):
            _finite_number(
                true_peak[field],
                label=f"measurements.true_peak.{field}",
                nullable=True,
            )
    else:
        _nonempty_string(true_peak["reason"], label="measurements.true_peak.reason")
        for field in (
            "oversampling_ratio",
            "oversampled_rate_hz",
            "left",
            "right",
            "maximum",
            "left_dbtp",
            "right_dbtp",
            "maximum_dbtp",
        ):
            if true_peak[field] is not None:
                raise PostRenderCheckError("unavailable true-peak fields must be null")

    loudness = _require_exact_keys(
        measurements["loudness"],
        {
            "status",
            "reason",
            "integrated_lufs",
            "max_momentary_lufs",
            "max_short_term_lufs",
            "block_seconds",
            "block_overlap_ratio",
            "block_count",
            "absolute_gate_lufs",
            "absolute_gated_block_count",
            "relative_gate_lu",
            "relative_gate_lufs",
            "final_gated_block_count",
            "lra",
        },
        label="measurements.loudness",
    )
    if (
        loudness["status"] not in {"available", "unavailable"}
        or loudness["block_seconds"] != 0.4
        or loudness["block_overlap_ratio"] != 0.75
        or loudness["absolute_gate_lufs"] != -70.0
        or loudness["relative_gate_lu"] != -10.0
    ):
        raise PostRenderCheckError("loudness measurement metadata is invalid")
    for field in (
        "integrated_lufs",
        "max_momentary_lufs",
        "max_short_term_lufs",
        "relative_gate_lufs",
    ):
        _finite_number(
            loudness[field], label=f"measurements.loudness.{field}", nullable=True
        )
    for field in (
        "block_count",
        "absolute_gated_block_count",
        "final_gated_block_count",
    ):
        _integer(loudness[field], label=f"measurements.loudness.{field}")
    if certified:
        if loudness["status"] == "available":
            if loudness["reason"] is not None or loudness["integrated_lufs"] is None:
                raise PostRenderCheckError(
                    "available 48 kHz loudness requires a value and null reason"
                )
        else:
            expected_reason = (
                "insufficient duration for one 400 ms loudness block"
                if loudness["block_count"] == 0
                else "no 400 ms loudness block passed the -70 LUFS absolute gate"
            )
            if (
                loudness["reason"] != expected_reason
                or loudness["integrated_lufs"] is not None
            ):
                raise PostRenderCheckError(
                    "unavailable 48 kHz loudness reason is inconsistent"
                )
    else:
        if loudness["status"] != "unavailable":
            raise PostRenderCheckError("non-48 kHz loudness must be unavailable")
        _nonempty_string(loudness["reason"], label="measurements.loudness.reason")
        if any(
            loudness[field] is not None
            for field in (
                "integrated_lufs",
                "max_momentary_lufs",
                "max_short_term_lufs",
                "relative_gate_lufs",
            )
        ):
            raise PostRenderCheckError("unavailable loudness values must be null")
        if any(
            loudness[field] != 0
            for field in (
                "block_count",
                "absolute_gated_block_count",
                "final_gated_block_count",
            )
        ):
            raise PostRenderCheckError("unavailable loudness counts must be zero")

    lra = _require_exact_keys(
        loudness["lra"],
        {
            "status",
            "stability",
            "value_lu",
            "short_term_window_seconds",
            "measurement_rate_hz",
            "absolute_gate_lufs",
            "relative_gate_lu",
            "relative_gate_lufs",
            "low_percentile",
            "high_percentile",
            "short_term_sample_count",
            "absolute_gated_sample_count",
            "relative_gated_sample_count",
            "p10_lufs",
            "p95_lufs",
        },
        label="measurements.loudness.lra",
    )
    expected_stability = "stable" if frame_count / sample_rate >= 60.0 else "not_recommended"
    if (
        lra["status"] not in {"available", "unavailable"}
        or lra["stability"] != expected_stability
        or lra["short_term_window_seconds"] != 3.0
        or lra["measurement_rate_hz"] != 10.0
        or lra["absolute_gate_lufs"] != -70.0
        or lra["relative_gate_lu"] != -20.0
        or lra["low_percentile"] != 10.0
        or lra["high_percentile"] != 95.0
    ):
        raise PostRenderCheckError("LRA measurement metadata is invalid")
    for field in ("value_lu", "relative_gate_lufs", "p10_lufs", "p95_lufs"):
        _finite_number(
            lra[field], label=f"measurements.loudness.lra.{field}", nullable=True
        )
    for field in (
        "short_term_sample_count",
        "absolute_gated_sample_count",
        "relative_gated_sample_count",
    ):
        _integer(lra[field], label=f"measurements.loudness.lra.{field}")
    if not certified and any(
        lra[field] != 0
        for field in (
            "short_term_sample_count",
            "absolute_gated_sample_count",
            "relative_gated_sample_count",
        )
    ):
        raise PostRenderCheckError("non-48 kHz LRA counts must be zero")
    return measurements


def validate_post_render_check(report: dict[str, Any]) -> None:
    """Fail closed when a report is malformed or internally inconsistent.

    This is deliberately a runtime validator rather than a replacement for the
    public JSON Schema.  It protects the release gate from a forged
    ``summary.can_proceed`` by validating issue semantics, regenerating policy
    findings from the measurements, and independently rebuilding the summary.
    """

    try:
        canonical_json_bytes(report)
    except (TypeError, ValueError, OverflowError) as error:
        raise PostRenderCheckError(
            "post-render report is not finite JSON-compatible data"
        ) from error

    top = _require_exact_keys(
        report,
        {
            "$schema",
            "format",
            "version",
            "artifact",
            "performance_plan",
            "audio_format",
            "measurement",
            "policy",
            "measurements",
            "issues",
            "summary",
        },
        label="post-render report",
    )
    if top["$schema"] != REPORT_SCHEMA_URI:
        raise PostRenderCheckError("post-render report schema URI is unsupported")
    if (
        top["format"] != REPORT_FORMAT
        or type(top["version"]) is not int
        or top["version"] != REPORT_VERSION
    ):
        raise PostRenderCheckError("post-render report format or version is unsupported")

    artifact = _require_exact_keys(
        top["artifact"], {"path", "sha256", "size_bytes"}, label="artifact"
    )
    try:
        artifact_path = _portable_artifact_label(artifact["path"])
        artifact_sha256 = _validate_sha256(artifact["sha256"], label="artifact.sha256")
    except ValueError as error:
        raise PostRenderCheckError(str(error)) from error
    if artifact_sha256 is None:
        raise PostRenderCheckError("artifact.sha256 cannot be null")
    if (
        isinstance(artifact["size_bytes"], bool)
        or not isinstance(artifact["size_bytes"], int)
        or artifact["size_bytes"] <= 0
    ):
        raise PostRenderCheckError("artifact.size_bytes must be a positive integer")

    performance_plan = _require_exact_keys(
        top["performance_plan"], {"sha256"}, label="performance_plan"
    )
    try:
        _validate_sha256(performance_plan["sha256"], label="performance_plan.sha256")
    except ValueError as error:
        raise PostRenderCheckError(str(error)) from error

    audio_format = _require_exact_keys(
        top["audio_format"],
        {
            "container",
            "encoding",
            "bits_per_sample",
            "channels",
            "sample_rate",
            "frame_count",
        },
        label="audio_format",
    )
    if (
        audio_format["container"] != "WAV"
        or audio_format["encoding"] != "PCM"
        or type(audio_format["bits_per_sample"]) is not int
        or audio_format["bits_per_sample"] != 24
        or type(audio_format["channels"]) is not int
        or audio_format["channels"] != 2
    ):
        raise PostRenderCheckError("audio_format does not describe stereo PCM-24 WAV")
    for field in ("sample_rate", "frame_count"):
        value = audio_format[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PostRenderCheckError(f"audio_format.{field} must be a positive integer")
    if not 8_000 <= audio_format["sample_rate"] <= 384_000:
        raise PostRenderCheckError("audio_format.sample_rate is outside 8000..384000")

    if (
        not isinstance(top["measurement"], dict)
        or type(top["measurement"].get("version")) is not int
        or type(top["measurement"].get("certified_sample_rate_hz")) is not int
        or top["measurement"] != _measurement_contract()
    ):
        raise PostRenderCheckError("measurement contract does not match this report version")
    if (
        not isinstance(top["policy"], dict)
        or type(top["policy"].get("version")) is not int
        or top["policy"] != _policy_contract()
    ):
        raise PostRenderCheckError("policy contract does not match this report version")
    _validate_measurements(
        top["measurements"],
        sample_rate=audio_format["sample_rate"],
        frame_count=audio_format["frame_count"],
    )
    if not isinstance(top["issues"], list):
        raise PostRenderCheckError("issues must be a JSON array")

    issue_keys = {
        "issue_schema_version",
        "id",
        "severity",
        "decision",
        "blocking",
        "code",
        "category",
        "stage",
        "basis",
        "confidence",
        "gate",
        "scope",
        "message",
        "suggestions",
        "override",
        "automatic_change",
        "evidence",
    }
    identifiers: set[str] = set()
    semantics = {
        "error": ("block", True, "render", "forbidden"),
        "warning": ("review", False, "none", "not_needed"),
        "info": ("inform", False, "none", "not_needed"),
    }
    for index, untrusted_issue in enumerate(top["issues"]):
        issue = _require_exact_keys(
            untrusted_issue, issue_keys, label=f"issues[{index}]"
        )
        if (
            type(issue["issue_schema_version"]) is not int
            or issue["issue_schema_version"] != ISSUE_SCHEMA_VERSION
        ):
            raise PostRenderCheckError(f"issues[{index}] schema version is unsupported")
        identifier = _nonempty_string(issue["id"], label=f"issues[{index}].id")
        if (
            not identifier.startswith("selfcheck-")
            or len(identifier) != 30
            or any(character not in "0123456789abcdef" for character in identifier[10:])
        ):
            raise PostRenderCheckError(f"issues[{index}].id is not a stable self-check id")
        if identifier in identifiers:
            raise PostRenderCheckError("post-render report contains duplicate issue ids")
        identifiers.add(identifier)
        severity = issue["severity"]
        if severity not in semantics:
            raise PostRenderCheckError(f"issues[{index}].severity is unsupported")
        decision, blocking, gate, override_mode = semantics[severity]
        if (
            issue["decision"] != decision
            or issue["blocking"] is not blocking
            or issue["gate"] != gate
            or issue["automatic_change"] is not False
            or issue["override"] != {"mode": override_mode}
        ):
            raise PostRenderCheckError(f"issues[{index}] gate semantics are inconsistent")
        for field in ("code", "category", "stage", "basis", "confidence", "message"):
            _nonempty_string(issue[field], label=f"issues[{index}].{field}")
        if issue["scope"] != {"kind": "artifact", "path": artifact_path}:
            raise PostRenderCheckError(f"issues[{index}] is not bound to the artifact")
        if not isinstance(issue["evidence"], dict):
            raise PostRenderCheckError(f"issues[{index}].evidence must be an object")
        suggestions = issue["suggestions"]
        if (
            not isinstance(suggestions, list)
            or not suggestions
            or any(not isinstance(item, str) or not item for item in suggestions)
        ):
            raise PostRenderCheckError(
                f"issues[{index}].suggestions must contain non-empty strings"
            )
        identity_document = {
            "code": issue["code"],
            "stage": issue["stage"],
            "scope": issue["scope"],
            "evidence": issue["evidence"],
            "details": {},
        }
        expected_identifier = (
            "selfcheck-"
            + hashlib.sha256(canonical_json_bytes(identity_document)).hexdigest()[:20]
        )
        if identifier != expected_identifier:
            raise PostRenderCheckError(f"issues[{index}].id does not bind its evidence")

    summary = top["summary"]
    if not isinstance(summary, dict):
        raise PostRenderCheckError("summary must be a JSON object")
    expected_activity = summary.get("expected_activity")
    if not isinstance(expected_activity, bool):
        raise PostRenderCheckError("summary.expected_activity must be boolean")
    try:
        expected_issues = _quality_issues(
            artifact_label=artifact_path,
            measurements=top["measurements"],
            expected_activity=expected_activity,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PostRenderCheckError(
            "measurements do not satisfy the versioned policy contract"
        ) from error
    if top["issues"] != expected_issues:
        raise PostRenderCheckError("issues do not match the versioned policy evaluation")
    expected_summary = summarize_issues(top["issues"])
    expected_summary["issue_count"] = len(top["issues"])
    expected_summary["expected_activity"] = expected_activity
    if summary != expected_summary:
        raise PostRenderCheckError("summary does not match the independently rebuilt result")


def require_post_render_check_pass(report: dict[str, Any]) -> None:
    """Validate one report and refuse delivery when an explicit error exists."""

    validate_post_render_check(report)
    if report["summary"]["can_proceed"] is not True:
        blocking_codes = sorted(
            {
                str(issue["code"])
                for issue in report["issues"]
                if issue["blocking"] is True or issue["severity"] == "error"
            }
        )
        joined = ", ".join(blocking_codes) or "unknown blocker"
        raise PostRenderCheckError(
            f"post-render check blocks delivery: {joined}"
        )


def write_post_render_check(path: str | Path, report: dict[str, Any]) -> None:
    """Atomically write one deterministic, finite JSON report."""

    validate_post_render_check(report)
    encoded = (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tianlai-part",
    )
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    # Successful publication consumes the temporary name.  If publication
    # fails, leave that name untouched for recovery and diagnostics; deleting
    # it by path would be unsafe if another entry replaced it concurrently.
    os.replace(temporary, destination)


__all__ = (
    "MEASUREMENT_VERSION",
    "POLICY_VERSION",
    "POST_RENDER_CHECK_NAME",
    "PostRenderCheckError",
    "REPORT_FORMAT",
    "REPORT_SCHEMA_URI",
    "REPORT_VERSION",
    "analyze_rendered_wav",
    "require_post_render_check_pass",
    "validate_post_render_check",
    "write_post_render_check",
)
