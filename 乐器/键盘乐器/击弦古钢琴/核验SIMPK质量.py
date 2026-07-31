"""逐个流式核验 SIMPK 击弦古钢琴的 756 个有效攻击样本。

这份检查只把可客观确认的问题列为硬失败：资源/映射损坏、无有效信号、
明确的数字削波、PCM 数据截断，以及格式或采用边界异常。低电平、较长的
起音前静音、直流偏移和尾部突变只进入待听清单；历史乐器的机械声和环境
底噪本身不在这里被判为缺陷。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
from typing import Any, Iterable, Sequence
import wave

import numpy as np


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
CONVERTER_PATH = HERE / "转换SIMPK音源.py"
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "音源" / "SIMPK_03_Clavichord"
DEFAULT_REPORT_PATH = HERE / "样本质量核验.json"
PLAYBACK_MAPPING = {
    "source_note_range": [40, 102],
    "playback_note_offset": -12,
    "sounding_note_range": [28, 90],
    "policy": "preserve_recorded_native_octave",
}

EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH_BYTES = 3
EXPECTED_COMPRESSION = "NONE"
DEFAULT_CHUNK_FRAMES = 65_536

# The thresholds intentionally err on the side of requesting a listening check.
# They do not try to turn historical mechanical noise into a binary defect.
SILENCE_MAX_LSB = 1
LOW_LEVEL_PEAK_DBFS = -60.0
LOW_LEVEL_RMS_DBFS = -72.0
LEADING_QUIET_GATE_DBFS = -60.0
LEADING_DIGITAL_SILENCE_REVIEW_SECONDS = 0.050
LEADING_QUIET_REVIEW_SECONDS = 0.100
DC_OFFSET_REVIEW_LINEAR = 0.010
CLIPPING_MIN_CONSECUTIVE_FRAMES = 3
TAIL_WINDOW_SECONDS = 0.010
TAIL_END_LEVEL_REVIEW_LINEAR = 0.020
TAIL_LAST_STEP_REVIEW_LINEAR = 0.050


def _load_converter() -> Any:
    if not CONVERTER_PATH.is_file():
        raise FileNotFoundError(f"SIMPK converter is missing: {CONVERTER_PATH}")
    spec = importlib.util.spec_from_file_location(
        "tianlai_simpk_clavichord_quality_converter",
        CONVERTER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import SIMPK converter: {CONVERTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONVERTER = _load_converter()


@dataclass(frozen=True, slots=True)
class WavMetrics:
    compression: str
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    bit_depth: int
    declared_frame_count: int
    decoded_frame_count: int
    offset_frame: int
    end_frame_exclusive: int
    adopted_frame_count: int
    effective_duration_seconds: float
    peak_linear: float
    peak_dbfs: float | None
    rms_linear: float
    rms_dbfs: float | None
    dc_offset_linear: float
    dc_offset_by_channel_linear: tuple[float, ...]
    dc_offset_absolute_linear: float
    rail_sample_count: int
    longest_rail_run_frames: int
    digital_clipping: bool
    silent: bool
    low_level: bool
    leading_digital_silence_frames: int
    leading_digital_silence_ms: float
    leading_quiet_frames: int
    leading_quiet_ms: float
    tail_window_rms_linear: float
    tail_end_level_linear: float
    tail_last_step_linear: float
    tail_cutoff_risk: bool
    pcm_data_truncated: bool
    pcm_sha256: str


def _dbfs(linear: float) -> float | None:
    if linear <= 0.0:
        return None
    return round(20.0 * math.log10(linear), 6)


def _round_float(value: float, places: int = 9) -> float:
    rounded = round(float(value), places)
    return 0.0 if rounded == -0.0 else rounded


def _decode_pcm(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    """Decode little-endian integer PCM into a frames-by-channels int32 array."""

    bytes_per_frame = sample_width * channels
    if bytes_per_frame <= 0 or len(raw) % bytes_per_frame:
        raise ValueError(
            f"PCM byte count {len(raw)} is not frame-aligned "
            f"for {channels}ch/{sample_width}-byte samples"
        )
    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.int32) - 128
    elif sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8)
        triples = octets.reshape(-1, 3).astype(np.int32)
        values = (
            triples[:, 0]
            | (triples[:, 1] << 8)
            | (triples[:, 2] << 16)
        )
        values = values - ((values & 0x800000) != 0).astype(np.int32) * 0x1000000
    elif sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.int32, copy=False)
    else:
        raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")
    return values.reshape(-1, channels)


def _updated_rail_run(
    mask: np.ndarray,
    carry: int,
    previous_maximum: int,
) -> tuple[int, int]:
    """Update a consecutive-true run without iterating over individual frames."""

    length = int(mask.size)
    if length == 0:
        return carry, previous_maximum
    false_positions = np.flatnonzero(~mask)
    if false_positions.size == 0:
        carry += length
        return carry, max(previous_maximum, carry)

    leading = int(false_positions[0])
    maximum = max(previous_maximum, carry + leading)
    if false_positions.size > 1:
        internal = int(np.max(np.diff(false_positions) - 1))
        maximum = max(maximum, internal)
    trailing = length - 1 - int(false_positions[-1])
    maximum = max(maximum, trailing)
    return trailing, maximum


def _first_active_frame(frame_magnitude: np.ndarray, threshold: int) -> int | None:
    positions = np.flatnonzero(frame_magnitude > threshold)
    return None if positions.size == 0 else int(positions[0])


def analyze_wav_stream(
    path: Path,
    *,
    offset_frame: int,
    end_frame_exclusive: int,
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
) -> WavMetrics:
    """Stream and measure one WAV.

    Level statistics use only the adopted mapping interval. The whole declared
    data chunk is still read so a physically truncated WAV cannot hide after the
    adopted boundary.
    """

    if chunk_frames <= 0:
        raise ValueError("chunk_frames must be positive")
    source = Path(path)
    try:
        input_file = wave.open(str(source), "rb")
    except (OSError, EOFError, wave.Error) as error:
        raise ValueError(f"cannot open PCM WAV: {source.name}: {error}") from error

    with input_file:
        channels = input_file.getnchannels()
        sample_width = input_file.getsampwidth()
        sample_rate = input_file.getframerate()
        declared_frames = input_file.getnframes()
        compression = input_file.getcomptype()
        if channels <= 0 or sample_width <= 0 or sample_rate <= 0:
            raise ValueError(f"invalid WAV metadata: {source.name}")
        if compression != "NONE":
            raise ValueError(
                f"unsupported compressed WAV: {source.name}: {compression!r}"
            )
        if not 0 <= offset_frame < end_frame_exclusive <= declared_frames:
            raise ValueError(
                f"invalid adopted boundary for {source.name}: "
                f"{offset_frame}:{end_frame_exclusive}/{declared_frames}"
            )

        full_scale = float(1 << (sample_width * 8 - 1))
        positive_rail = (1 << (sample_width * 8 - 1)) - 1
        negative_rail = -(1 << (sample_width * 8 - 1))
        quiet_gate = max(
            SILENCE_MAX_LSB,
            int(round(full_scale * 10.0 ** (LEADING_QUIET_GATE_DBFS / 20.0))),
        )
        tail_frame_limit = max(2, int(math.ceil(sample_rate * TAIL_WINDOW_SECONDS)))

        decoded_frames = 0
        adopted_frames = 0
        normalized_sum_by_channel = np.zeros(channels, dtype=np.float64)
        normalized_square_sum = 0.0
        peak_integer = 0
        rail_sample_count = 0
        rail_carry = [0] * channels
        rail_maximum = [0] * channels
        leading_digital_frames = 0
        leading_quiet_frames = 0
        digital_leading_open = True
        quiet_leading_open = True
        tail = np.empty((0, channels), dtype=np.int32)
        pcm_data_truncated = False
        pcm_hasher = hashlib.sha256()

        while decoded_frames < declared_frames:
            requested = min(chunk_frames, declared_frames - decoded_frames)
            try:
                raw = input_file.readframes(requested)
            except (EOFError, OSError, wave.Error) as error:
                raise ValueError(
                    f"cannot decode PCM data: {source.name}: {error}"
                ) from error
            if not raw:
                pcm_data_truncated = decoded_frames < declared_frames
                break
            pcm_hasher.update(raw)
            try:
                decoded = _decode_pcm(raw, sample_width, channels)
            except ValueError as error:
                raise ValueError(f"{source.name}: {error}") from error

            frame_count = int(decoded.shape[0])
            if frame_count > requested:
                raise ValueError(
                    f"WAV decoder returned too many frames for {source.name}"
                )
            chunk_start = decoded_frames
            chunk_end = decoded_frames + frame_count
            selected_start = max(offset_frame, chunk_start)
            selected_end = min(end_frame_exclusive, chunk_end)
            if selected_start < selected_end:
                selected = decoded[
                    selected_start - chunk_start : selected_end - chunk_start
                ]
                selected_frame_count = int(selected.shape[0])
                adopted_frames += selected_frame_count
                absolute = np.abs(selected.astype(np.int64))
                frame_magnitude = np.max(absolute, axis=1)
                peak_integer = max(peak_integer, int(np.max(frame_magnitude)))

                normalized = selected.astype(np.float64) / full_scale
                normalized_sum_by_channel += np.sum(
                    normalized,
                    axis=0,
                    dtype=np.float64,
                )
                normalized_square_sum += float(
                    np.sum(normalized * normalized, dtype=np.float64)
                )

                rail = (selected == positive_rail) | (selected == negative_rail)
                rail_sample_count += int(np.count_nonzero(rail))
                for channel in range(channels):
                    rail_carry[channel], rail_maximum[channel] = _updated_rail_run(
                        rail[:, channel],
                        rail_carry[channel],
                        rail_maximum[channel],
                    )

                if digital_leading_open:
                    first = _first_active_frame(frame_magnitude, SILENCE_MAX_LSB)
                    if first is None:
                        leading_digital_frames += selected_frame_count
                    else:
                        leading_digital_frames += first
                        digital_leading_open = False
                if quiet_leading_open:
                    first = _first_active_frame(frame_magnitude, quiet_gate)
                    if first is None:
                        leading_quiet_frames += selected_frame_count
                    else:
                        leading_quiet_frames += first
                        quiet_leading_open = False

                if tail.size:
                    tail = np.concatenate((tail, selected), axis=0)
                else:
                    tail = selected.copy()
                if tail.shape[0] > tail_frame_limit:
                    tail = tail[-tail_frame_limit:]

            decoded_frames = chunk_end
            if frame_count < requested:
                pcm_data_truncated = decoded_frames < declared_frames
                break

        if adopted_frames != end_frame_exclusive - offset_frame:
            pcm_data_truncated = True
        sample_count = adopted_frames * channels
        if sample_count <= 0:
            raise ValueError(f"adopted interval contains no decoded PCM: {source.name}")

        peak_linear = peak_integer / full_scale
        rms_linear = math.sqrt(max(0.0, normalized_square_sum / sample_count))
        dc_offsets = normalized_sum_by_channel / adopted_frames
        dc_offset = float(np.mean(dc_offsets))
        dc_offset_absolute = float(np.max(np.abs(dc_offsets)))
        longest_rail_run = max(rail_maximum, default=0)
        digital_clipping = (
            longest_rail_run >= CLIPPING_MIN_CONSECUTIVE_FRAMES
        )
        silent = peak_integer <= SILENCE_MAX_LSB
        low_level = (
            (_dbfs(peak_linear) is None or _dbfs(peak_linear) < LOW_LEVEL_PEAK_DBFS)
            or (_dbfs(rms_linear) is None or _dbfs(rms_linear) < LOW_LEVEL_RMS_DBFS)
        )

        tail_normalized = tail.astype(np.float64) / full_scale
        tail_rms = math.sqrt(float(np.mean(tail_normalized * tail_normalized)))
        tail_end_level = float(np.max(np.abs(tail_normalized[-1])))
        if tail.shape[0] >= 2:
            tail_last_step = float(
                np.max(
                    np.abs(
                        tail[-1].astype(np.float64)
                        - tail[-2].astype(np.float64)
                    )
                )
                / full_scale
            )
        else:
            tail_last_step = tail_end_level
        tail_cutoff_risk = (
            tail_end_level >= TAIL_END_LEVEL_REVIEW_LINEAR
            or tail_last_step >= TAIL_LAST_STEP_REVIEW_LINEAR
        )

        return WavMetrics(
            compression=compression,
            sample_rate_hz=sample_rate,
            channels=channels,
            sample_width_bytes=sample_width,
            bit_depth=sample_width * 8,
            declared_frame_count=declared_frames,
            decoded_frame_count=decoded_frames,
            offset_frame=offset_frame,
            end_frame_exclusive=end_frame_exclusive,
            adopted_frame_count=adopted_frames,
            effective_duration_seconds=_round_float(adopted_frames / sample_rate, 6),
            peak_linear=_round_float(peak_linear),
            peak_dbfs=_dbfs(peak_linear),
            rms_linear=_round_float(rms_linear),
            rms_dbfs=_dbfs(rms_linear),
            dc_offset_linear=_round_float(dc_offset),
            dc_offset_by_channel_linear=tuple(
                _round_float(value) for value in dc_offsets
            ),
            dc_offset_absolute_linear=_round_float(dc_offset_absolute),
            rail_sample_count=rail_sample_count,
            longest_rail_run_frames=longest_rail_run,
            digital_clipping=digital_clipping,
            silent=silent,
            low_level=low_level,
            leading_digital_silence_frames=leading_digital_frames,
            leading_digital_silence_ms=_round_float(
                leading_digital_frames * 1000.0 / sample_rate,
                3,
            ),
            leading_quiet_frames=leading_quiet_frames,
            leading_quiet_ms=_round_float(
                leading_quiet_frames * 1000.0 / sample_rate,
                3,
            ),
            tail_window_rms_linear=_round_float(tail_rms),
            tail_end_level_linear=_round_float(tail_end_level),
            tail_last_step_linear=_round_float(tail_last_step),
            tail_cutoff_risk=tail_cutoff_risk,
            pcm_data_truncated=pcm_data_truncated,
            pcm_sha256=pcm_hasher.hexdigest(),
        )


def _mapping_dict(sample: Any) -> dict[str, Any]:
    return {
        "timbre": str(sample.timbre),
        "root_midi": int(sample.root_note),
        "velocity_low": int(sample.velocity_low),
        "velocity_high": int(sample.velocity_high),
        "round_robin": int(sample.round_robin_position),
    }


def _format_key(metrics: WavMetrics) -> str:
    return (
        f"{metrics.sample_rate_hz}Hz/"
        f"{metrics.channels}ch/"
        f"{metrics.bit_depth}bit/"
        f"{metrics.compression}"
    )


def _failed_sample(sample: Any, message: str) -> dict[str, Any]:
    return {
        "sample_path": str(sample.sample_path).replace("\\", "/"),
        "mapping": _mapping_dict(sample),
        "status": "fail",
        "format": None,
        "boundary": {
            "declared_mapping_frame_count": int(sample.frame_count),
            "offset_frame": int(sample.offset_frames),
            "end_frame_exclusive": int(sample.end_frame_exclusive),
        },
        "levels": None,
        "diagnostics": None,
        "hard_failures": [f"unreadable_or_invalid_wav: {message}"],
        "review_risks": [],
    }


def _audit_one(sample: Any, chunk_frames: int) -> dict[str, Any]:
    hard_failures: list[str] = []
    review_risks: list[str] = []
    try:
        metrics = analyze_wav_stream(
            Path(sample.sample_file),
            offset_frame=int(sample.offset_frames),
            end_frame_exclusive=int(sample.end_frame_exclusive),
            chunk_frames=chunk_frames,
        )
    except (OSError, EOFError, ValueError, wave.Error) as error:
        return _failed_sample(sample, str(error))

    if metrics.compression != EXPECTED_COMPRESSION:
        hard_failures.append("unexpected_compression")
    if metrics.sample_rate_hz != EXPECTED_SAMPLE_RATE:
        hard_failures.append("unexpected_sample_rate")
    if metrics.channels != EXPECTED_CHANNELS:
        hard_failures.append("unexpected_channel_count")
    if metrics.sample_width_bytes != EXPECTED_SAMPLE_WIDTH_BYTES:
        hard_failures.append("unexpected_bit_depth")
    if metrics.declared_frame_count != int(sample.frame_count):
        hard_failures.append("mapping_frame_count_mismatch")
    if metrics.channels != int(sample.channels):
        hard_failures.append("mapping_channel_count_mismatch")
    if metrics.pcm_data_truncated:
        hard_failures.append("truncated_pcm_data")
    if metrics.silent:
        hard_failures.append("silent_or_only_one_lsb")
    if metrics.digital_clipping:
        hard_failures.append("confirmed_digital_clipping")

    if metrics.low_level and not metrics.silent:
        review_risks.append("very_low_level")
    if (
        metrics.leading_digital_silence_ms
        >= LEADING_DIGITAL_SILENCE_REVIEW_SECONDS * 1000.0
    ):
        review_risks.append("long_leading_digital_silence")
    if metrics.leading_quiet_ms >= LEADING_QUIET_REVIEW_SECONDS * 1000.0:
        review_risks.append("long_leading_quiet_section")
    if metrics.dc_offset_absolute_linear >= DC_OFFSET_REVIEW_LINEAR:
        review_risks.append("dc_offset")
    if metrics.rail_sample_count and not metrics.digital_clipping:
        review_risks.append("isolated_full_scale_hit")
    if metrics.tail_cutoff_risk:
        review_risks.append("tail_discontinuity_or_cutoff")

    if hard_failures:
        status = "fail"
    elif review_risks:
        status = "review"
    else:
        status = "pass"
    return {
        "sample_path": str(sample.sample_path).replace("\\", "/"),
        "mapping": _mapping_dict(sample),
        "status": status,
        "format": {
            "compression": metrics.compression,
            "sample_rate_hz": metrics.sample_rate_hz,
            "channels": metrics.channels,
            "sample_width_bytes": metrics.sample_width_bytes,
            "bit_depth": metrics.bit_depth,
            "declared_frame_count": metrics.declared_frame_count,
            "decoded_frame_count": metrics.decoded_frame_count,
            "pcm_sha256": metrics.pcm_sha256,
        },
        "boundary": {
            "declared_mapping_frame_count": int(sample.frame_count),
            "offset_frame": metrics.offset_frame,
            "end_frame_exclusive": metrics.end_frame_exclusive,
            "adopted_frame_count": metrics.adopted_frame_count,
            "effective_duration_seconds": metrics.effective_duration_seconds,
        },
        "levels": {
            "peak_linear": metrics.peak_linear,
            "peak_dbfs": metrics.peak_dbfs,
            "rms_linear": metrics.rms_linear,
            "rms_dbfs": metrics.rms_dbfs,
            "dc_offset_linear": metrics.dc_offset_linear,
            "dc_offset_by_channel_linear": list(
                metrics.dc_offset_by_channel_linear
            ),
            "dc_offset_absolute_linear": metrics.dc_offset_absolute_linear,
        },
        "diagnostics": {
            "rail_sample_count": metrics.rail_sample_count,
            "longest_rail_run_frames": metrics.longest_rail_run_frames,
            "digital_clipping": metrics.digital_clipping,
            "silent": metrics.silent,
            "low_level": metrics.low_level,
            "leading_digital_silence_frames": (
                metrics.leading_digital_silence_frames
            ),
            "leading_digital_silence_ms": metrics.leading_digital_silence_ms,
            "leading_quiet_frames": metrics.leading_quiet_frames,
            "leading_quiet_ms": metrics.leading_quiet_ms,
            "tail_window_rms_linear": metrics.tail_window_rms_linear,
            "tail_end_level_linear": metrics.tail_end_level_linear,
            "tail_last_step_linear": metrics.tail_last_step_linear,
            "tail_cutoff_risk": metrics.tail_cutoff_risk,
            "pcm_data_truncated": metrics.pcm_data_truncated,
        },
        "hard_failures": hard_failures,
        "review_risks": review_risks,
    }


def _summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    durations = [
        record["boundary"]["effective_duration_seconds"]
        for record in records
        if record["boundary"].get("effective_duration_seconds") is not None
    ]
    peaks = [
        record["levels"]["peak_dbfs"]
        for record in records
        if record["levels"] is not None
        and record["levels"]["peak_dbfs"] is not None
    ]
    rms_values = [
        record["levels"]["rms_dbfs"]
        for record in records
        if record["levels"] is not None
        and record["levels"]["rms_dbfs"] is not None
    ]

    def distribution(values: Sequence[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {
            "minimum": _round_float(min(values), 6),
            "median": _round_float(statistics.median(values), 6),
            "maximum": _round_float(max(values), 6),
        }

    return {
        "sample_count": len(records),
        "pass_count": sum(record["status"] == "pass" for record in records),
        "review_count": sum(record["status"] == "review" for record in records),
        "fail_count": sum(record["status"] == "fail" for record in records),
        "hard_failure_count": sum(
            len(record["hard_failures"]) for record in records
        ),
        "review_risk_count": sum(
            len(record["review_risks"]) for record in records
        ),
        "effective_duration_seconds": distribution(durations),
        "peak_dbfs": distribution(peaks),
        "rms_dbfs": distribution(rms_values),
    }


def _group_summaries(
    records: Sequence[dict[str, Any]],
    key_function: Any,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key = str(key_function(record))
        grouped.setdefault(key, []).append(record)
    return {key: _summary(grouped[key]) for key in sorted(grouped)}


def _coverage(
    samples: Sequence[Any],
    *,
    require_full_coverage: bool,
) -> tuple[dict[str, Any], list[str]]:
    expected_keys = {
        (timbre, note, velocity_low, velocity_high, round_robin)
        for timbre in CONVERTER.TIMBRES
        for note in range(CONVERTER.NOTE_MIN, CONVERTER.NOTE_MAX + 1)
        for velocity_low, velocity_high in CONVERTER.VELOCITY_LAYERS
        for round_robin in range(1, CONVERTER.ROUND_ROBIN_LENGTH + 1)
    }
    actual_keys = [
        (
            str(sample.timbre),
            int(sample.root_note),
            int(sample.velocity_low),
            int(sample.velocity_high),
            int(sample.round_robin_position),
        )
        for sample in samples
    ]
    actual_key_set = set(actual_keys)
    paths = [str(sample.sample_path).replace("\\", "/") for sample in samples]
    missing = sorted(expected_keys - actual_key_set)
    unexpected = sorted(actual_key_set - expected_keys)
    duplicate_mapping_count = len(actual_keys) - len(actual_key_set)
    duplicate_path_count = len(paths) - len(set(paths))
    complete = (
        not missing
        and not unexpected
        and duplicate_mapping_count == 0
        and duplicate_path_count == 0
    )
    failures: list[str] = []
    if require_full_coverage and not complete:
        failures.append("mapping_coverage_mismatch")
    return (
        {
            "complete": complete,
            "expected_mapping_count": len(expected_keys),
            "actual_mapping_count": len(samples),
            "unique_wav_count": len(set(paths)),
            "duplicate_mapping_count": duplicate_mapping_count,
            "duplicate_sample_path_count": duplicate_path_count,
            "missing_mapping_count": len(missing),
            "unexpected_mapping_count": len(unexpected),
            "missing_mapping_examples": [
                {
                    "timbre": key[0],
                    "root_midi": key[1],
                    "velocity_low": key[2],
                    "velocity_high": key[3],
                    "round_robin": key[4],
                }
                for key in missing[:10]
            ],
            "unexpected_mapping_examples": [
                {
                    "timbre": key[0],
                    "root_midi": key[1],
                    "velocity_low": key[2],
                    "velocity_high": key[3],
                    "round_robin": key[4],
                }
                for key in unexpected[:10]
            ],
            "expected_dimensions": {
                "timbres": list(CONVERTER.TIMBRES),
                "note_min": CONVERTER.NOTE_MIN,
                "note_max": CONVERTER.NOTE_MAX,
                "velocity_layers": [
                    {"low": low, "high": high}
                    for low, high in CONVERTER.VELOCITY_LAYERS
                ],
                "round_robin_length": CONVERTER.ROUND_ROBIN_LENGTH,
            },
        },
        failures,
    )


def _content_uniqueness(
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Classify exact PCM reuse without treating it as file corruption."""

    by_content: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        format_record = record["format"]
        if format_record is None or not format_record.get("pcm_sha256"):
            continue
        key = (
            format_record["pcm_sha256"],
            format_record["sample_rate_hz"],
            format_record["channels"],
            format_record["bit_depth"],
            format_record["decoded_frame_count"],
        )
        by_content.setdefault(key, []).append(record)

    duplicate_groups = [
        sorted(group, key=lambda record: record["sample_path"])
        for group in by_content.values()
        if len(group) > 1
    ]
    duplicate_groups.sort(key=lambda group: group[0]["sample_path"])
    categories = {
        "cross_velocity_layer": 0,
        "cross_round_robin": 0,
        "cross_key": 0,
        "cross_timbre": 0,
    }
    group_details: list[dict[str, Any]] = []
    for group in duplicate_groups:
        mappings = [record["mapping"] for record in group]
        velocity_layers = {
            (mapping["velocity_low"], mapping["velocity_high"])
            for mapping in mappings
        }
        round_robins = {mapping["round_robin"] for mapping in mappings}
        keys = {mapping["root_midi"] for mapping in mappings}
        timbres = {mapping["timbre"] for mapping in mappings}
        group_categories: list[str] = []
        if len(velocity_layers) > 1:
            group_categories.append("cross_velocity_layer")
        if len(round_robins) > 1:
            group_categories.append("cross_round_robin")
        if len(keys) > 1:
            group_categories.append("cross_key")
        if len(timbres) > 1:
            group_categories.append("cross_timbre")
        for category in group_categories:
            categories[category] += 1

        risk = (
            "exact_pcm_duplicate_"
            + ("_and_".join(group_categories) if group_categories else "same_mapping")
        )
        for record in group:
            if risk not in record["review_risks"]:
                record["review_risks"].append(risk)
                record["review_risks"].sort()
            if record["status"] == "pass":
                record["status"] = "review"
        group_details.append(
            {
                "pcm_sha256": group[0]["format"]["pcm_sha256"],
                "sample_count": len(group),
                "categories": group_categories,
                "sample_paths": [record["sample_path"] for record in group],
                "mappings": mappings,
            }
        )

    duplicated_paths = {
        record["sample_path"]
        for group in duplicate_groups
        for record in group
    }
    return {
        "hashed_sample_count": sum(len(group) for group in by_content.values()),
        "unique_pcm_content_count": len(by_content),
        "duplicate_pcm_group_count": len(duplicate_groups),
        "sample_count_in_duplicate_groups": len(duplicated_paths),
        "duplicate_group_categories": categories,
        "duplicate_groups": group_details,
    }


def audit_validated_samples(
    samples: Iterable[Any],
    *,
    source_name: str = "SIMPK_03_Clavichord",
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
    require_full_coverage: bool = True,
) -> dict[str, Any]:
    """Audit already validated mappings; exposed separately for fixture tests."""

    ordered = tuple(
        sorted(
            samples,
            key=lambda sample: (
                str(sample.timbre),
                int(sample.root_note),
                int(sample.velocity_low),
                int(sample.round_robin_position),
                str(sample.sample_path),
            ),
        )
    )
    coverage, global_failures = _coverage(
        ordered,
        require_full_coverage=require_full_coverage,
    )

    seen_paths: set[str] = set()
    records: list[dict[str, Any]] = []
    for sample in ordered:
        sample_path = str(sample.sample_path).replace("\\", "/")
        if sample_path in seen_paths:
            continue
        seen_paths.add(sample_path)
        records.append(_audit_one(sample, chunk_frames))
    records.sort(key=lambda record: record["sample_path"])
    content_uniqueness = _content_uniqueness(records)

    formats: dict[str, int] = {}
    for record in records:
        if record["format"] is None:
            key = "unreadable"
        else:
            key = (
                f'{record["format"]["sample_rate_hz"]}Hz/'
                f'{record["format"]["channels"]}ch/'
                f'{record["format"]["bit_depth"]}bit/'
                f'{record["format"]["compression"]}'
            )
        formats[key] = formats.get(key, 0) + 1

    sample_hard_failure_count = sum(
        len(record["hard_failures"]) for record in records
    )
    review_queue = [
        {
            "sample_path": record["sample_path"],
            "mapping": record["mapping"],
            "risks": record["review_risks"],
        }
        for record in records
        if record["review_risks"]
    ]
    hard_failure_samples = [
        {
            "sample_path": record["sample_path"],
            "mapping": record["mapping"],
            "failures": record["hard_failures"],
        }
        for record in records
        if record["hard_failures"]
    ]
    report = {
        "schema_version": 1,
        "instrument": "击弦古钢琴",
        "source": source_name,
        "playback_mapping": dict(PLAYBACK_MAPPING),
        "analysis_scope": (
            "levels use the adopted attack interval; PCM integrity reads the "
            "whole declared WAV"
        ),
        "policy": {
            "hard_failures": [
                "unreadable/corrupt/truncated PCM",
                "mapping boundary or required format mismatch",
                "silent or only one-LSB signal",
                (
                    "confirmed digital clipping: at least "
                    f"{CLIPPING_MIN_CONSECUTIVE_FRAMES} consecutive rail frames"
                ),
            ],
            "review_only": [
                "very low level",
                "long leading digital silence or quiet section",
                "DC offset",
                "isolated full-scale hit",
                "tail discontinuity/cutoff risk",
            ],
            "noise_policy": (
                "historical mechanical and ambient noise is not a hard failure"
            ),
            "thresholds": {
                "silence_max_lsb": SILENCE_MAX_LSB,
                "low_level_peak_dbfs": LOW_LEVEL_PEAK_DBFS,
                "low_level_rms_dbfs": LOW_LEVEL_RMS_DBFS,
                "leading_quiet_gate_dbfs": LEADING_QUIET_GATE_DBFS,
                "leading_digital_silence_review_ms": (
                    LEADING_DIGITAL_SILENCE_REVIEW_SECONDS * 1000.0
                ),
                "leading_quiet_review_ms": (
                    LEADING_QUIET_REVIEW_SECONDS * 1000.0
                ),
                "dc_offset_review_linear": DC_OFFSET_REVIEW_LINEAR,
                "tail_end_level_review_linear": TAIL_END_LEVEL_REVIEW_LINEAR,
                "tail_last_step_review_linear": TAIL_LAST_STEP_REVIEW_LINEAR,
            },
        },
        "coverage": coverage,
        "content_uniqueness": content_uniqueness,
        "format_summary": {key: formats[key] for key in sorted(formats)},
        "summary": _summary(records),
        "summaries": {
            "by_timbre": _group_summaries(
                records, lambda record: record["mapping"]["timbre"]
            ),
            "by_velocity_layer": _group_summaries(
                records,
                lambda record: (
                    f'{record["mapping"]["velocity_low"]}-'
                    f'{record["mapping"]["velocity_high"]}'
                ),
            ),
            "by_key": _group_summaries(
                records, lambda record: record["mapping"]["root_midi"]
            ),
            "by_round_robin": _group_summaries(
                records, lambda record: record["mapping"]["round_robin"]
            ),
        },
        "global_hard_failures": global_failures,
        "hard_failure_count": len(global_failures) + sample_hard_failure_count,
        "hard_failure_samples": hard_failure_samples,
        "review_sample_count": len(review_queue),
        "review_queue": review_queue,
        "samples": records,
    }
    report["status"] = (
        "fail" if report["hard_failure_count"] else
        "review" if report["review_sample_count"] else
        "pass"
    )
    return report


def _stable_validation_error(error: Exception, source_root: Path) -> str:
    message = str(error)
    root_text = str(Path(source_root).resolve())
    return message.replace(root_text, "${SOURCE_ROOT}")


def audit_simpk_source(
    source_root: Path,
    *,
    chunk_frames: int = DEFAULT_CHUNK_FRAMES,
) -> dict[str, Any]:
    root = Path(source_root).resolve()
    try:
        samples = CONVERTER.validate_simpk_source(root)
    except (OSError, EOFError, ValueError, wave.Error) as error:
        message = _stable_validation_error(error, root)
        return {
            "schema_version": 1,
            "instrument": "击弦古钢琴",
            "source": root.name,
            "playback_mapping": dict(PLAYBACK_MAPPING),
            "status": "fail",
            "source_validation": {
                "passed": False,
                "error": message,
            },
            "hard_failure_count": 1,
            "global_hard_failures": [f"source_validation_failed: {message}"],
            "hard_failure_samples": [],
            "review_sample_count": 0,
            "review_queue": [],
            "samples": [],
        }
    report = audit_validated_samples(
        samples,
        source_name=root.name,
        chunk_frames=chunk_frames,
        require_full_coverage=True,
    )
    report["source_validation"] = {
        "passed": True,
        "validated_attack_mapping_count": len(samples),
    }
    return report


def render_report_json(report: dict[str, Any]) -> str:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    destination = Path(path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(render_report_json(report))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # Preserve the original write/replace exception. A later run
                # uses a unique name and cannot mistake this file for a report.
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream-audit all 756 SIMPK clavichord attack WAV mappings."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help=f"extracted SIMPK source root (default: {DEFAULT_SOURCE_ROOT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"deterministic JSON report (default: {DEFAULT_REPORT_PATH})",
    )
    parser.add_argument(
        "--chunk-frames",
        type=int,
        default=DEFAULT_CHUNK_FRAMES,
        help="streaming read size in audio frames",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    report = audit_simpk_source(
        arguments.source_root,
        chunk_frames=arguments.chunk_frames,
    )
    write_report(arguments.output, report)
    print(
        "SIMPK clavichord sample-quality audit: "
        f"status={report['status']}, "
        f"hard_failures={report['hard_failure_count']}, "
        f"review_samples={report['review_sample_count']}, "
        f"report={arguments.output}"
    )
    return 0 if report["hard_failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
