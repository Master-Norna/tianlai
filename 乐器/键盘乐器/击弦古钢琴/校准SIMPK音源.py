"""严格测量并冻结 SIMPK 击弦古钢琴逐采样音准。

正式模式通过 ``转换SIMPK音源.py`` 的源资源验证器取得 756 个有效攻击
采样。每个采样先在多个避开击弦瞬态的窗口运行宽频、八度感知的音高分析，
再用项目的谐波约束分析器和局部自相关估计器复核最稳健的窗口。只有全部采样
清晰、结果安全且同音色的真实轮替彼此一致时，才会原子写入转换器使用的
校准表；不同音色的独立录音差异只作为审计事实，不冒充同源一致性门槛。

校准表有意保持为严格的小型协议；详细证据另写入诊断文件。任何失败都只写
诊断文件并抛出 ``CalibrationRejectedError``，绝不猜测或填充校正值。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path, PurePosixPath
import statistics
import sys
from types import ModuleType
from typing import Callable, Iterable, Sequence


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "音源" / "SIMPK_03_Clavichord"
# `SIMPK调音表.json` is a deliberately tiny machine protocol consumed only by
# the converter.  The manifest-facing, human-auditable project report remains
# the conventional `音准校准.json`; this avoids two competing diagnostic files.
DEFAULT_CALIBRATION_PATH = HERE / "SIMPK调音表.json"
DEFAULT_DIAGNOSTICS_PATH = HERE / "音准校准.json"
CONVERTER_PATH = HERE / "转换SIMPK音源.py"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tianlai.analysis import (  # noqa: E402
    analyze_file_harmonic_pitch,
    analyze_signal_wide_pitch,
)


class CalibrationRejectedError(RuntimeError):
    """Raised when measurements are incomplete, ambiguous or unsafe."""


@dataclass(frozen=True, slots=True)
class CalibrationSettings:
    """Reproducible measurement and rejection thresholds."""

    reference_a4_hz: float = 440.0
    # The archive calls its source notes 40..102, but waveform periodicity and
    # partials consistently prove they sound at 28..90.  We preserve that real
    # historical-instrument register instead of pitch-shifting every WAV up an
    # octave.  The converter uses the same audited structural offset.
    source_root_note_offset: int = -12
    window_starts_seconds: tuple[float, ...] = (0.05, 0.15, 0.28)
    minimum_analysis_frames: int = 16_384
    minimum_cycles_per_window: float = 64.0
    maximum_frames: int = 65_536
    wide_search_cents: float = 1_800.0
    wide_cents_step: float = 0.5
    harmonic_search_cents: float = 180.0
    harmonic_count: int = 10
    local_autocorrelation_search_cents: float = 80.0
    # A two-period lag remains far from the adjacent periodic peak inside the
    # +/-80-cent search and is less vulnerable to slow beating/room modulation
    # than a long 8- or 16-period lag.  Stereo evidence is aggregated below.
    autocorrelation_period_multiple: int = 2
    maximum_autocorrelation_window_spread_cents: float = 12.0
    minimum_expected_periodicity: float = 0.55
    minimum_clear_windows: int = 2
    maximum_window_spread_cents: float = 12.0
    maximum_wide_harmonic_residual_cents: float = 18.0
    maximum_safe_absolute_detune_cents: float = 80.0
    maximum_peer_deviation_cents: float = 15.0

    def validate(self) -> None:
        if (
            not math.isfinite(self.reference_a4_hz)
            or self.reference_a4_hz <= 0.0
        ):
            raise ValueError("reference_a4_hz must be finite and positive")
        if (
            type(self.source_root_note_offset) is not int
            or not -24 <= self.source_root_note_offset <= 24
        ):
            raise ValueError("source_root_note_offset must be an integer in -24..24")
        if not self.window_starts_seconds:
            raise ValueError("at least one analysis window is required")
        if any(
            not math.isfinite(value) or value < 0.0
            for value in self.window_starts_seconds
        ):
            raise ValueError("window starts must be finite and non-negative")
        if tuple(sorted(set(self.window_starts_seconds))) != (
            self.window_starts_seconds
        ):
            raise ValueError("window starts must be unique and increasing")
        if self.maximum_frames < 4_096:
            raise ValueError("maximum_frames must be at least 4096")
        if (
            self.minimum_analysis_frames < 4_096
            or self.minimum_analysis_frames > self.maximum_frames
        ):
            raise ValueError(
                "minimum_analysis_frames must be between 4096 and maximum_frames"
            )
        if (
            not math.isfinite(self.minimum_cycles_per_window)
            or self.minimum_cycles_per_window < 8.0
        ):
            raise ValueError(
                "minimum_cycles_per_window must be finite and at least 8"
            )
        if (
            not math.isfinite(self.wide_search_cents)
            or self.wide_search_cents < 1_200.0
        ):
            raise ValueError("wide_search_cents must be at least 1200")
        if (
            not math.isfinite(self.wide_cents_step)
            or not 0.1 <= self.wide_cents_step <= 5.0
        ):
            raise ValueError("wide_cents_step must be between 0.1 and 5.0")
        if (
            not math.isfinite(self.harmonic_search_cents)
            or self.harmonic_search_cents <= 0.0
        ):
            raise ValueError("harmonic_search_cents must be finite and positive")
        if self.harmonic_count < 3:
            raise ValueError("harmonic_count must be at least 3")
        if (
            not math.isfinite(self.local_autocorrelation_search_cents)
            or not 10.0 <= self.local_autocorrelation_search_cents <= 100.0
        ):
            raise ValueError(
                "local_autocorrelation_search_cents must be in 10..100"
            )
        if not 2 <= self.autocorrelation_period_multiple <= 32:
            raise ValueError(
                "autocorrelation_period_multiple must be in 2..32"
            )
        if (
            not math.isfinite(self.minimum_expected_periodicity)
            or not 0.0 < self.minimum_expected_periodicity <= 1.0
        ):
            raise ValueError(
                "minimum_expected_periodicity must be in the interval (0, 1]"
            )
        if not 1 <= self.minimum_clear_windows <= len(
            self.window_starts_seconds
        ):
            raise ValueError("minimum_clear_windows is outside the window count")
        for name in (
            "maximum_window_spread_cents",
            "maximum_wide_harmonic_residual_cents",
            "maximum_autocorrelation_window_spread_cents",
            "maximum_safe_absolute_detune_cents",
            "maximum_peer_deviation_cents",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class AttackSample:
    """Converter-independent view of one validated attack sample."""

    sample_path: str
    root_note: int
    velocity_low: int
    velocity_high: int
    round_robin_position: int
    timbre: str
    offset_frames: int = 0
    end_frame_exclusive: int | None = None


@dataclass(frozen=True, slots=True)
class SampleMeasurement:
    """Result returned by the per-sample measurement stage."""

    status: str
    measured_detune_cents: float | None
    confidence: float
    diagnostics: dict[str, object]


def midi_to_hz(midi_note: int, reference_a4_hz: float = 440.0) -> float:
    return reference_a4_hz * (2.0 ** ((float(midi_note) - 69.0) / 12.0))


def _load_converter() -> ModuleType:
    if not CONVERTER_PATH.is_file():
        raise FileNotFoundError(f"SIMPK converter is missing: {CONVERTER_PATH}")
    module_name = "_tianlai_simpk_clavichord_converter"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, CONVERTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import SIMPK converter: {CONVERTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _canonical_sample_path(value: object) -> str:
    text = str(value)
    if not text or "\\" in text:
        raise ValueError(f"sample path is not canonical POSIX relative: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"sample path escapes the source root: {text!r}")
    canonical = path.as_posix()
    if canonical != text:
        raise ValueError(f"sample path is not canonical: {text!r}")
    return canonical


def _coerce_attack_sample(record: object) -> AttackSample:
    sample_path = _canonical_sample_path(getattr(record, "sample_path"))
    root_note = int(getattr(record, "root_note"))
    velocity_low = int(getattr(record, "velocity_low"))
    velocity_high = int(getattr(record, "velocity_high"))
    round_robin_position = int(getattr(record, "round_robin_position"))
    timbre = str(getattr(record, "timbre"))
    offset_frames = int(getattr(record, "offset_frames", 0))
    end_value = getattr(record, "end_frame_exclusive", None)
    end_frame_exclusive = None if end_value is None else int(end_value)

    if not 0 <= root_note <= 127:
        raise ValueError(f"invalid root note for {sample_path}: {root_note}")
    if not 0 <= velocity_low <= velocity_high <= 127:
        raise ValueError(
            f"invalid velocity interval for {sample_path}: "
            f"{velocity_low}-{velocity_high}"
        )
    if round_robin_position < 1:
        raise ValueError(f"invalid round robin for {sample_path}")
    if timbre not in {"lupe", "reso"}:
        raise ValueError(f"invalid SIMPK timbre for {sample_path}: {timbre!r}")
    if offset_frames < 0:
        raise ValueError(f"negative sample offset for {sample_path}")
    if end_frame_exclusive is not None and end_frame_exclusive <= offset_frames:
        raise ValueError(f"empty mapped sample interval for {sample_path}")
    return AttackSample(
        sample_path=sample_path,
        root_note=root_note,
        velocity_low=velocity_low,
        velocity_high=velocity_high,
        round_robin_position=round_robin_position,
        timbre=timbre,
        offset_frames=offset_frames,
        end_frame_exclusive=end_frame_exclusive,
    )


def load_attack_samples(
    source_root: str | Path,
    *,
    strict_source: bool = True,
) -> tuple[AttackSample, ...]:
    """Load attacks through the converter's parser/validator.

    ``strict_source=False`` exists only for controlled fixtures which still
    reproduce the converter's complete source XML.  Compact unit tests should
    inject selected ``attack_samples`` into :func:`calibrate_simpk_source`;
    neither route replaces the full validator used by the command-line path.
    """

    import soundfile as sf

    root = Path(source_root).resolve()
    converter = _load_converter()
    if strict_source:
        raw_records = converter.validate_simpk_source(root)
        attacks = tuple(_coerce_attack_sample(record) for record in raw_records)
    else:
        preset = root / "clavichord.dspreset"
        parsed = converter.parse_dspreset(preset)
        fixture_attacks: list[AttackSample] = []
        for record in parsed:
            if not bool(getattr(record, "enabled")):
                continue
            if str(getattr(record, "trigger")) != "attack":
                continue
            sample_path = _canonical_sample_path(getattr(record, "sample_path"))
            info = sf.info(str(root / Path(*PurePosixPath(sample_path).parts)))
            fixture_attacks.append(
                AttackSample(
                    sample_path=sample_path,
                    root_note=int(getattr(record, "root_note")),
                    velocity_low=int(getattr(record, "velocity_low")),
                    velocity_high=int(getattr(record, "velocity_high")),
                    round_robin_position=int(
                        getattr(record, "round_robin_position")
                    ),
                    timbre=str(getattr(record, "timbre")),
                    offset_frames=0,
                    end_frame_exclusive=int(info.frames),
                )
            )
        attacks = tuple(fixture_attacks)

    ordered = tuple(sorted(attacks, key=lambda item: item.sample_path))
    paths = [item.sample_path for item in ordered]
    if len(paths) != len(set(paths)):
        duplicates = sorted(
            path for path in set(paths) if paths.count(path) > 1
        )
        raise ValueError(f"duplicate enabled attack sample paths: {duplicates}")
    return ordered


def _resolved_sample_path(source_root: Path, sample_path: str) -> Path:
    relative = PurePosixPath(_canonical_sample_path(sample_path))
    path = (source_root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(source_root)
    except ValueError as error:
        raise ValueError(f"sample escapes source root: {sample_path}") from error
    if not path.is_file():
        raise FileNotFoundError(f"SIMPK sample is missing: {sample_path}")
    return path


def _audio_uniqueness_audit(
    source_root: Path,
    samples: Sequence[AttackSample],
    *,
    root_note_offset: int,
) -> dict[str, object]:
    """Prove whether the three mapped velocity zones contain distinct audio."""

    hashes: dict[str, str] = {}
    groups: dict[tuple[int, str, int], list[AttackSample]] = {}
    for sample in samples:
        path = _resolved_sample_path(source_root, sample.sample_path)
        digest = hashlib.sha256()
        with path.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
        hashes[sample.sample_path] = digest.hexdigest()
        groups.setdefault(
            (
                sample.root_note + root_note_offset,
                sample.timbre,
                sample.round_robin_position,
            ),
            [],
        ).append(sample)

    three_zone_groups = 0
    byte_identical_groups = 0
    nonidentical_groups: list[dict[str, object]] = []
    for (root_note, timbre, round_robin), group in sorted(groups.items()):
        velocities = {
            (sample.velocity_low, sample.velocity_high) for sample in group
        }
        group_hashes = {hashes[sample.sample_path] for sample in group}
        if len(group) == 3 and len(velocities) == 3:
            three_zone_groups += 1
            if len(group_hashes) == 1:
                byte_identical_groups += 1
            else:
                nonidentical_groups.append(
                    {
                        "root_note": root_note,
                        "timbre": timbre,
                        "round_robin_position": round_robin,
                        "sample_paths": sorted(
                            sample.sample_path for sample in group
                        ),
                    }
                )
    return {
        "hash_algorithm": "SHA-256 of complete source WAV bytes",
        "mapped_attack_path_count": len(samples),
        "unique_audio_sha256_count": len(set(hashes.values())),
        "note_timbre_round_robin_group_count": len(groups),
        "groups_with_three_velocity_zones": three_zone_groups,
        "groups_byte_identical_across_all_three_velocity_zones": (
            byte_identical_groups
        ),
        "groups_with_distinct_velocity_audio": nonidentical_groups,
        "effective_velocity_recording_layers": (
            1
            if three_zone_groups > 0
            and byte_identical_groups == three_zone_groups
            else None
        ),
        "mapped_velocity_zones": 3 if three_zone_groups > 0 else None,
        "interpretation": (
            "one real recorded velocity layer repeated across three mapped "
            "velocity zones"
            if three_zone_groups > 0
            and byte_identical_groups == three_zone_groups
            else "velocity-zone audio is not uniformly byte-identical"
        ),
    }


def _expected_periodicity(
    segment: object,
    sample_rate: int,
    expected_hz: float,
) -> tuple[float, dict[str, float]]:
    """Return normalized autocorrelation at the declared source period.

    A stereo room/resonance recording can partially cancel when downmixed, so
    each channel and the mono sum are retained as independent evidence.  The
    strongest exact-period result is used, while every component is frozen in
    diagnostics.  This does not search for a pitch and therefore cannot hide an
    octave mapping error.
    """

    import numpy as np

    audio = np.asarray(segment, dtype="float64")
    if audio.ndim == 1:
        signals = [("mono", audio)]
    elif audio.ndim == 2 and audio.shape[1] >= 1:
        signals = [
            (f"channel_{index + 1}", audio[:, index])
            for index in range(audio.shape[1])
        ]
        if audio.shape[1] > 1:
            signals.append(("downmix", np.mean(audio, axis=1)))
    else:
        raise ValueError("periodicity segment has no audio channels")
    lag = sample_rate / expected_hz
    lower_lag = int(math.floor(lag))
    upper_lag = lower_lag + 1
    if lower_lag < 1 or upper_lag >= len(audio) - 1:
        raise ValueError("expected pitch period is outside the analysis window")

    results: dict[str, float] = {}
    fraction = lag - lower_lag
    for name, raw in signals:
        signal = np.asarray(raw, dtype="float64").copy()
        signal -= float(np.mean(signal))
        if not np.all(np.isfinite(signal)):
            raise ValueError("periodicity segment contains non-finite samples")

        correlations: list[float] = []
        for integer_lag in (lower_lag, upper_lag):
            left = signal[:-integer_lag]
            right = signal[integer_lag:]
            denominator = math.sqrt(
                max(
                    float(np.dot(left, left)) * float(np.dot(right, right)),
                    1e-30,
                )
            )
            correlations.append(float(np.dot(left, right)) / denominator)
        value = correlations[0] + fraction * (
            correlations[1] - correlations[0]
        )
        results[name] = max(-1.0, min(1.0, value))
    return max(results.values()), results


def _local_autocorrelation_pitch(
    segment: object,
    sample_rate: int,
    expected_hz: float,
    *,
    signal_name: str,
    search_cents: float,
    requested_period_multiple: int,
) -> tuple[float, float, int, bool]:
    """Estimate fine detune from a local normalized-autocorrelation peak.

    Parabolic interpolation supplies sub-sample lag resolution.  The search is
    deliberately narrow, so this estimator cannot silently choose an adjacent
    octave; the independent wide analyzer remains the octave guard.
    """

    import numpy as np

    audio = np.asarray(segment, dtype="float64")
    if audio.ndim == 1:
        signal = audio.copy()
    elif audio.ndim == 2 and audio.shape[1] >= 1:
        if signal_name == "downmix":
            signal = np.mean(audio, axis=1)
        elif signal_name.startswith("channel_"):
            channel_index = int(signal_name.removeprefix("channel_")) - 1
            if not 0 <= channel_index < audio.shape[1]:
                raise ValueError("periodicity channel selection is invalid")
            signal = audio[:, channel_index].copy()
        else:
            raise ValueError("unknown periodicity signal selection")
    else:
        raise ValueError("autocorrelation segment has no audio channels")
    signal = np.asarray(signal, dtype="float64")
    signal -= float(np.mean(signal))
    if not np.all(np.isfinite(signal)):
        raise ValueError("autocorrelation segment contains non-finite samples")
    energy = float(np.dot(signal, signal))
    if energy <= 1e-16:
        raise ValueError("autocorrelation segment is silent")

    maximum_multiple = math.floor(
        (len(signal) - 2) * expected_hz / (2.0 * sample_rate)
    )
    period_multiple = min(requested_period_multiple, maximum_multiple)
    if period_multiple < 2:
        raise ValueError("analysis window is too short for local autocorrelation")
    target_lag = period_multiple * sample_rate / expected_hz
    ratio = 2.0 ** (search_cents / 1200.0)
    minimum_lag = max(1, math.floor(target_lag / ratio) - 1)
    maximum_lag = min(
        len(signal) - 2,
        math.ceil(target_lag * ratio) + 1,
    )
    if maximum_lag - minimum_lag < 4:
        raise ValueError("local autocorrelation search has too few lags")

    frame_count = len(signal)
    fft_size = 1 << (2 * frame_count - 1).bit_length()
    transformed = np.fft.rfft(signal, fft_size)
    correlation = np.fft.irfft(
        transformed * np.conjugate(transformed),
        fft_size,
    )
    squared_prefix = np.concatenate(([0.0], np.cumsum(signal * signal)))
    lags = np.arange(minimum_lag, maximum_lag + 1)
    left_energy = squared_prefix[frame_count - lags]
    right_energy = squared_prefix[frame_count] - squared_prefix[lags]
    denominator = np.sqrt(np.maximum(left_energy * right_energy, 1e-30))
    normalized = correlation[lags] / denominator
    peak_index = int(np.argmax(normalized))
    boundary_peak = peak_index in (0, len(normalized) - 1)
    refined_lag = float(lags[peak_index])
    if not boundary_peak:
        left, center, right = normalized[peak_index - 1 : peak_index + 2]
        curvature = left - 2.0 * center + right
        if curvature != 0.0:
            refined_lag += float(0.5 * (left - right) / curvature)
    measured_hz = period_multiple * sample_rate / refined_lag
    detune_cents = 1200.0 * math.log2(measured_hz / expected_hz)
    peak = float(normalized[peak_index])
    return detune_cents, peak, period_multiple, boundary_peak


def _local_autocorrelation_consensus(
    segment: object,
    sample_rate: int,
    expected_hz: float,
    *,
    signal_names: Sequence[str],
    search_cents: float,
    requested_period_multiple: int,
) -> tuple[float, float, int, dict[str, dict[str, object]]]:
    """Return a channel-robust local-autocorrelation estimate.

    Picking the strongest stereo channel independently in every time window can
    manufacture a pitch jump when room phase or beating merely changes which
    channel wins.  Instead, every channel plus the downmix is measured without
    consulting the harmonic estimator, and the median of a strict majority of
    non-boundary estimates is used.  Component evidence is retained verbatim
    so channel disagreement remains auditable.
    """

    if not signal_names:
        raise ValueError("local autocorrelation requires at least one signal")
    component_diagnostics: dict[str, dict[str, object]] = {}
    valid: list[tuple[float, float, int]] = []
    for signal_name in signal_names:
        try:
            detune, peak, period_multiple, boundary = (
                _local_autocorrelation_pitch(
                    segment,
                    sample_rate,
                    expected_hz,
                    signal_name=signal_name,
                    search_cents=search_cents,
                    requested_period_multiple=requested_period_multiple,
                )
            )
        except (RuntimeError, ValueError) as error:
            component_diagnostics[signal_name] = {
                "status": "analysis_error",
                "reason": f"{type(error).__name__}: {error}",
            }
            continue
        component_diagnostics[signal_name] = {
            "status": "boundary_peak" if boundary else "accepted",
            "detune_cents": round(detune, 6),
            "peak": round(peak, 6),
            "period_multiple": period_multiple,
            "boundary_peak": boundary,
        }
        if not boundary:
            valid.append((detune, peak, period_multiple))

    minimum_valid = len(signal_names) // 2 + 1
    if len(valid) < minimum_valid:
        component_reasons = "; ".join(
            f"{name}={record['reason']}"
            for name, record in component_diagnostics.items()
            if "reason" in record
        )
        raise ValueError(
            f"only {len(valid)} of {len(signal_names)} channel/downmix "
            f"autocorrelation estimates avoid the search boundary; "
            f"{minimum_valid} required"
            + (
                f"; components: {component_reasons}"
                if component_reasons
                else ""
            )
        )
    period_multiples = {item[2] for item in valid}
    if len(period_multiples) != 1:
        raise ValueError(
            "local autocorrelation components used inconsistent period multiples"
        )
    return (
        float(statistics.median(item[0] for item in valid)),
        float(statistics.median(item[1] for item in valid)),
        next(iter(period_multiples)),
        component_diagnostics,
    )


def _consistent_window_indices(
    evidence: Sequence[tuple[int, float, float, float]],
    *,
    minimum_count: int,
    harmonic_tolerance_cents: float,
    autocorrelation_tolerance_cents: float,
) -> tuple[int, ...]:
    """Choose the largest deterministic multi-estimator window consensus."""

    for count in range(len(evidence), minimum_count - 1, -1):
        candidates: list[tuple[tuple[float, float], tuple[int, ...]]] = []
        for selected in itertools.combinations(evidence, count):
            harmonic_values = [item[1] for item in selected]
            autocorrelation_values = [item[2] for item in selected]
            if (
                max(harmonic_values) - min(harmonic_values)
                > harmonic_tolerance_cents
            ):
                continue
            if (
                max(autocorrelation_values)
                - min(autocorrelation_values)
                > autocorrelation_tolerance_cents
            ):
                continue
            indices = tuple(item[0] for item in selected)
            # Prefer stronger evidence; ties use earlier windows.  Rounded
            # values are not involved, so selection remains deterministic.
            score = (
                sum(item[3] for item in selected),
                -float(sum(indices)),
            )
            candidates.append((score, indices))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    return ()


def measure_attack_sample(
    source_root: Path,
    sample: AttackSample,
    settings: CalibrationSettings,
) -> SampleMeasurement:
    """Measure one WAV with three-window harmonic and octave-aware checks."""

    import soundfile as sf

    calibrated_root_note = sample.root_note + settings.source_root_note_offset
    if not 0 <= calibrated_root_note <= 127:
        raise ValueError(
            f"calibrated source root is outside MIDI: {sample.sample_path}"
        )
    expected_hz = midi_to_hz(
        calibrated_root_note,
        settings.reference_a4_hz,
    )
    path = _resolved_sample_path(source_root, sample.sample_path)
    audio, sample_rate = sf.read(
        str(path),
        dtype="float32",
        always_2d=True,
    )
    frame_total = int(audio.shape[0])
    end = (
        frame_total
        if sample.end_frame_exclusive is None
        else min(frame_total, sample.end_frame_exclusive)
    )
    if sample.offset_frames >= end:
        raise ValueError(f"mapped interval is empty: {sample.sample_path}")
    mapped = audio[sample.offset_frames:end]
    windows: list[dict[str, object]] = []
    window_evidence: list[tuple[int, float, float, float]] = []
    wide_clear_windows = 0
    periodicity_consensus_windows = 0

    for start_seconds in settings.window_starts_seconds:
        start_frame = round(start_seconds * sample_rate)
        available = len(mapped) - start_frame
        window: dict[str, object] = {
            "start_seconds": round(start_seconds, 6),
            "available_frames": max(0, int(available)),
        }
        if available < 4_096:
            window.update(
                {
                    "status": "no_clear_pitch",
                    "reason": "fewer than 4096 mapped frames remain",
                }
            )
            windows.append(window)
            continue
        pitch_adaptive_frames = max(
            settings.minimum_analysis_frames,
            math.ceil(
                settings.minimum_cycles_per_window
                * sample_rate
                / expected_hz
            ),
        )
        maximum_frames = min(
            settings.maximum_frames,
            pitch_adaptive_frames,
            available,
        )
        window["analysis_frames"] = maximum_frames
        window["analysis_cycles_at_expected_pitch"] = round(
            maximum_frames * expected_hz / sample_rate,
            3,
        )
        segment = mapped[start_frame : start_frame + maximum_frames]
        try:
            assessment = analyze_signal_wide_pitch(
                mapped,
                sample_rate,
                expected_hz,
                start_seconds=start_seconds,
                maximum_frames=maximum_frames,
                search_cents=settings.wide_search_cents,
                cents_step=settings.wide_cents_step,
            )
            expected_periodicity, periodicity_components = (
                _expected_periodicity(segment, sample_rate, expected_hz)
            )
            periodicity_signal = max(
                periodicity_components,
                key=lambda name: periodicity_components[name],
            )
            (
                autocorrelation_detune,
                autocorrelation_peak,
                autocorrelation_period_multiple,
                autocorrelation_components,
            ) = _local_autocorrelation_consensus(
                segment,
                sample_rate,
                expected_hz,
                signal_names=tuple(periodicity_components),
                search_cents=settings.local_autocorrelation_search_cents,
                requested_period_multiple=(
                    settings.autocorrelation_period_multiple
                ),
            )
        except (OSError, RuntimeError, ValueError) as error:
            window.update(
                {
                    "status": "analysis_error",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            windows.append(window)
            continue

        window.update(
            {
                "wide_status": assessment.status,
                "wide_confidence": round(assessment.confidence, 6),
                "wide_candidate_periodicity": round(
                    assessment.periodicity, 6
                ),
                "wide_harmonic_peak_coverage": round(
                    assessment.harmonic_peak_coverage, 6
                ),
                "wide_reason": assessment.reason,
                "expected_periodicity": round(expected_periodicity, 6),
                "expected_periodicity_components": {
                    name: round(value, 6)
                    for name, value in periodicity_components.items()
                },
                "strongest_expected_periodicity_signal": periodicity_signal,
                "local_autocorrelation_detune_cents": round(
                    autocorrelation_detune, 6
                ),
                "local_autocorrelation_peak": round(
                    autocorrelation_peak, 6
                ),
                "local_autocorrelation_period_multiple": (
                    autocorrelation_period_multiple
                ),
                "local_autocorrelation_signal_strategy": (
                    "median of a strict majority of non-boundary "
                    "channel/downmix estimates"
                ),
                "local_autocorrelation_components": (
                    autocorrelation_components
                ),
            }
        )
        harmonic_start = (
            sample.offset_frames / float(sample_rate) + start_seconds
        )
        try:
            harmonic = analyze_file_harmonic_pitch(
                path,
                expected_hz,
                start_seconds=harmonic_start,
                maximum_frames=maximum_frames,
                search_cents=settings.harmonic_search_cents,
                harmonic_count=settings.harmonic_count,
            )
        except (OSError, RuntimeError, ValueError) as error:
            window.update(
                {
                    "status": "harmonic_analysis_error",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            windows.append(window)
            continue
        window.update(
            {
                "harmonic_measured_hz": round(harmonic.measured_hz, 6),
                "harmonic_detune_cents": round(
                    float(harmonic.detune_cents), 6
                ),
            }
        )
        harmonic_detune = float(harmonic.detune_cents)
        harmonic_autocorrelation_residual = (
            harmonic_detune - autocorrelation_detune
        )
        window["harmonic_autocorrelation_residual_cents"] = round(
            harmonic_autocorrelation_residual, 6
        )
        if abs(autocorrelation_detune) > (
            settings.maximum_safe_absolute_detune_cents
        ):
            window["status"] = "unsafe_autocorrelation_detune"
            window["reason"] = (
                f"autocorrelation |detune| exceeds "
                f"{settings.maximum_safe_absolute_detune_cents:g} cents"
            )
            windows.append(window)
            continue
        if assessment.clear_pitch and assessment.detune_cents is not None:
            wide_detune = float(assessment.detune_cents)
            window["wide_detune_cents"] = round(wide_detune, 6)
            window["nearest_octave_error"] = assessment.nearest_octave_error
            if assessment.nearest_octave_error not in (None, 0):
                window["status"] = "octave_error"
                window["reason"] = (
                    f"wide analysis selected octave displacement "
                    f"{assessment.nearest_octave_error:+d}"
                )
                windows.append(window)
                continue
            if abs(wide_detune) > settings.maximum_safe_absolute_detune_cents:
                window["status"] = "unsafe_wide_detune"
                window["reason"] = (
                    f"wide |detune| exceeds "
                    f"{settings.maximum_safe_absolute_detune_cents:g} cents"
                )
                windows.append(window)
                continue
            residual = harmonic_detune - wide_detune
            window["wide_harmonic_residual_cents"] = round(residual, 6)
            if abs(residual) > (
                settings.maximum_wide_harmonic_residual_cents
            ):
                window["status"] = "analysis_disagreement"
                window["reason"] = (
                    f"wide and harmonic analyses differ by "
                    f"{residual:+.3f} cents"
                )
                windows.append(window)
                continue
            accepted_by = "wide_and_harmonic"
            wide_clear_windows += 1
        elif expected_periodicity < settings.minimum_expected_periodicity:
            window["status"] = "insufficient_expected_periodicity"
            window["reason"] = (
                f"wide result is not clear and exact-root periodicity "
                f"{expected_periodicity:.3f} is below "
                f"{settings.minimum_expected_periodicity:g}"
            )
            windows.append(window)
            continue
        else:
            # The shared wide gate deliberately demands strong spectral
            # coverage.  A clavichord can miss that threshold while retaining
            # an unambiguous period.  We do not change the global gate: the
            # exact declared period plus narrow harmonic result and the
            # same-key peer checks below provide the instrument-specific proof.
            accepted_by = "harmonic_and_expected_periodicity"
            periodicity_consensus_windows += 1

        if abs(harmonic_detune) > settings.maximum_safe_absolute_detune_cents:
            window["status"] = "unsafe_harmonic_detune"
            window["reason"] = (
                f"harmonic |detune| exceeds "
                f"{settings.maximum_safe_absolute_detune_cents:g} cents"
            )
            windows.append(window)
            continue
        evidence_confidence = 0.5 * (
            float(assessment.confidence)
            + max(0.0, min(1.0, autocorrelation_peak))
        )
        window["status"] = "accepted"
        window["accepted_by"] = accepted_by
        window["evidence_confidence"] = round(evidence_confidence, 6)
        window["reason"] = (
            "narrow harmonic pitch is supported by the octave guard and "
            "exact-root periodicity"
        )
        window_evidence.append(
            (
                len(windows),
                harmonic_detune,
                autocorrelation_detune,
                evidence_confidence,
            )
        )
        windows.append(window)

    base_diagnostics: dict[str, object] = {
        "sample_path": sample.sample_path,
        "upstream_root_note": sample.root_note,
        "root_note": calibrated_root_note,
        "source_root_note_offset": settings.source_root_note_offset,
        "expected_hz": round(expected_hz, 6),
        "velocity_low": sample.velocity_low,
        "velocity_high": sample.velocity_high,
        "velocity_layer": f"{sample.velocity_low}-{sample.velocity_high}",
        "round_robin_position": sample.round_robin_position,
        "timbre": sample.timbre,
        "sample_rate": int(sample_rate),
        "channels": int(audio.shape[1]),
        "mapped_start_frame": sample.offset_frames,
        "mapped_end_frame_exclusive": end,
        "windows": windows,
        "clear_window_count": len(window_evidence),
        "wide_clear_window_count": wide_clear_windows,
        "periodicity_consensus_window_count": periodicity_consensus_windows,
    }
    if len(window_evidence) < settings.minimum_clear_windows:
        base_diagnostics["reason"] = (
            f"only {len(window_evidence)} safe clear windows; "
            f"{settings.minimum_clear_windows} required"
        )
        confidence = statistics.median(
            [item[3] for item in window_evidence]
        ) if window_evidence else 0.0
        return SampleMeasurement(
            "rejected_insufficient_clear_windows",
            None,
            float(confidence),
            base_diagnostics,
        )

    consensus_indices = _consistent_window_indices(
        window_evidence,
        minimum_count=settings.minimum_clear_windows,
        harmonic_tolerance_cents=settings.maximum_window_spread_cents,
        autocorrelation_tolerance_cents=(
            settings.maximum_autocorrelation_window_spread_cents
        ),
    )
    base_diagnostics["consensus_window_indices"] = list(consensus_indices)
    base_diagnostics["consensus_window_count"] = len(consensus_indices)
    if not consensus_indices:
        base_diagnostics["reason"] = (
            "no multi-window consensus satisfies both the harmonic-detune "
            f"{settings.maximum_window_spread_cents:g}-cent and independent "
            "local-autocorrelation "
            f"{settings.maximum_autocorrelation_window_spread_cents:g}-cent "
            "gates"
        )
        return SampleMeasurement(
            "rejected_unstable_windows",
            None,
            float(statistics.median([item[3] for item in window_evidence])),
            base_diagnostics,
        )
    selected = [
        item for item in window_evidence if item[0] in consensus_indices
    ]
    rejected_indices = {
        item[0] for item in window_evidence if item[0] not in consensus_indices
    }
    for index in sorted(rejected_indices):
        windows[index]["status"] = "cross_window_outlier"
        windows[index]["reason"] = (
            "excluded by the deterministic harmonic/autocorrelation "
            "multi-window consensus"
        )
    clear_values = [item[1] for item in selected]
    autocorrelation_values = [item[2] for item in selected]
    cross_method_residuals = [
        harmonic - autocorrelation
        for _, harmonic, autocorrelation, _ in selected
    ]
    clear_confidences = [item[3] for item in selected]
    harmonic_median = float(statistics.median(clear_values))
    harmonic_spread = max(
        abs(value - harmonic_median) for value in clear_values
    )
    harmonic_range = max(clear_values) - min(clear_values)
    autocorrelation_median = float(statistics.median(autocorrelation_values))
    autocorrelation_spread = max(
        abs(value - autocorrelation_median)
        for value in autocorrelation_values
    )
    autocorrelation_range = (
        max(autocorrelation_values) - min(autocorrelation_values)
    )
    base_diagnostics["harmonic_median_detune_cents"] = round(
        harmonic_median, 6
    )
    base_diagnostics["calibration_pitch_estimator"] = (
        "median of selected harmonic-constrained FFT detunes"
    )
    base_diagnostics[
        "harmonic_maximum_deviation_from_median_cents"
    ] = round(harmonic_spread, 6)
    base_diagnostics["harmonic_selected_window_range_cents"] = round(
        harmonic_range, 6
    )
    base_diagnostics[
        "local_autocorrelation_median_detune_cents"
    ] = round(autocorrelation_median, 6)
    base_diagnostics[
        "local_autocorrelation_maximum_deviation_from_median_cents"
    ] = round(autocorrelation_spread, 6)
    base_diagnostics[
        "local_autocorrelation_selected_window_range_cents"
    ] = round(autocorrelation_range, 6)
    cross_method_center = float(statistics.median(cross_method_residuals))
    base_diagnostics[
        "harmonic_autocorrelation_residual_median_cents"
    ] = round(cross_method_center, 6)
    base_diagnostics[
        "harmonic_autocorrelation_residual_maximum_deviation_cents"
    ] = round(
        max(
            abs(value - cross_method_center)
            for value in cross_method_residuals
        ),
        6,
    )
    if harmonic_spread > settings.maximum_window_spread_cents:
        base_diagnostics["reason"] = (
            f"harmonic windows disagree by {harmonic_spread:.3f} cents "
            "from median"
        )
        return SampleMeasurement(
            "rejected_unstable_windows",
            None,
            float(statistics.median(clear_confidences)),
            base_diagnostics,
        )

    base_diagnostics["reason"] = (
        "stable harmonic and independent local-autocorrelation estimates "
        "confirmed across post-attack windows; final calibration uses the "
        "harmonic median, with octave-aware wide checks and exact-root "
        "periodicity evidence"
    )
    return SampleMeasurement(
        "accepted",
        harmonic_median,
        float(statistics.median(clear_confidences)),
        base_diagnostics,
    )


def _rounded(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("diagnostic value is not finite")
    rounded = round(float(value), 6)
    return 0.0 if rounded == -0.0 else rounded


def _distribution(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"count": 0}
    median = float(statistics.median(values))
    deviations = [abs(value - median) for value in values]
    return {
        "count": len(values),
        "minimum_detune_cents": _rounded(min(values)),
        "median_detune_cents": _rounded(median),
        "maximum_detune_cents": _rounded(max(values)),
        "median_absolute_deviation_cents": _rounded(
            statistics.median(deviations)
        ),
        "maximum_absolute_detune_cents": _rounded(max(map(abs, values))),
    }


def _breakdown(
    samples: Sequence[AttackSample],
    values: dict[str, float],
    key: Callable[[AttackSample], str],
    diagnostics: dict[str, dict[str, object]],
) -> dict[str, object]:
    buckets: dict[str, list[AttackSample]] = {}
    for sample in samples:
        buckets.setdefault(key(sample), []).append(sample)
    result: dict[str, object] = {}
    for name, bucket in sorted(buckets.items()):
        present = [
            values[item.sample_path]
            for item in bucket
            if item.sample_path in values
        ]
        status_counts: dict[str, int] = {}
        confidences: list[float] = []
        for item in bucket:
            record = diagnostics.get(item.sample_path, {})
            status = str(record.get("status", "missing_diagnostic"))
            status_counts[status] = status_counts.get(status, 0) + 1
            confidence = record.get("confidence")
            if (
                item.sample_path in values
                and isinstance(confidence, (int, float))
                and math.isfinite(float(confidence))
            ):
                confidences.append(float(confidence))
        record = _distribution(present)
        record.update(
            {
                "declared_sample_count": len(bucket),
                "accepted_sample_count": len(present),
                "rejected_sample_count": len(bucket) - len(present),
                "status_counts": {
                    status: status_counts[status]
                    for status in sorted(status_counts)
                },
            }
        )
        if confidences:
            record["minimum_confidence"] = _rounded(min(confidences))
            record["median_confidence"] = _rounded(
                statistics.median(confidences)
            )
        result[name] = record
    return result


def _subgroup_centers(
    samples: Sequence[AttackSample],
    values: dict[str, float],
    key: Callable[[AttackSample], str],
) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for sample in samples:
        value = values.get(sample.sample_path)
        if value is not None:
            buckets.setdefault(key(sample), []).append(value)
    return {
        name: float(statistics.median(bucket))
        for name, bucket in sorted(buckets.items())
    }


def _write_json_atomic(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        allow_nan=False,
    ) + "\n"
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def calibrate_simpk_source(
    source_root: str | Path,
    *,
    calibration_path: str | Path = DEFAULT_CALIBRATION_PATH,
    diagnostics_path: str | Path = DEFAULT_DIAGNOSTICS_PATH,
    settings: CalibrationSettings | None = None,
    strict_source: bool = True,
    expected_sample_count: int | None = 756,
    expected_samples_per_note: int | None = 12,
    attack_samples: Iterable[AttackSample] | None = None,
    measurement_function: Callable[
        [Path, AttackSample, CalibrationSettings], SampleMeasurement
    ] = measure_attack_sample,
) -> tuple[dict[str, object], dict[str, object]]:
    """Measure all attacks and write the strict map only after every gate passes."""

    active_settings = settings or CalibrationSettings()
    active_settings.validate()
    root = Path(source_root).resolve()
    if attack_samples is None:
        if strict_source:
            converter = _load_converter()
            converter_offset = getattr(
                converter,
                "PLAYBACK_NOTE_OFFSET",
                None,
            )
            if type(converter_offset) is not int:
                raise ValueError(
                    "SIMPK converter must declare integer PLAYBACK_NOTE_OFFSET"
                )
            if converter_offset != active_settings.source_root_note_offset:
                raise ValueError(
                    "SIMPK calibration/converter playback-note offset drift: "
                    f"calibration={active_settings.source_root_note_offset}, "
                    f"converter={converter_offset}"
                )
        samples = load_attack_samples(root, strict_source=strict_source)
    else:
        samples = tuple(
            sorted(
                (_coerce_attack_sample(item) for item in attack_samples),
                key=lambda item: item.sample_path,
            )
        )

    failures: list[dict[str, object]] = []
    if not samples:
        failures.append({"code": "empty_sample_set"})
    if expected_sample_count is not None and len(samples) != expected_sample_count:
        failures.append(
            {
                "code": "sample_count_mismatch",
                "expected": expected_sample_count,
                "actual": len(samples),
            }
        )
    paths = [sample.sample_path for sample in samples]
    if len(paths) != len(set(paths)):
        failures.append(
            {
                "code": "duplicate_sample_paths",
                "paths": sorted(
                    path for path in set(paths) if paths.count(path) > 1
                ),
            }
        )
    try:
        uniqueness_audit = _audio_uniqueness_audit(
            root,
            samples,
            root_note_offset=active_settings.source_root_note_offset,
        )
    except (OSError, RuntimeError, ValueError) as error:
        uniqueness_audit = {
            "status": "failed",
            "reason": f"{type(error).__name__}: {error}",
        }
        failures.append(
            {
                "code": "audio_uniqueness_audit_failed",
                "reason": str(uniqueness_audit["reason"]),
            }
        )
    if expected_sample_count == 756 and len(samples) == 756:
        expected_uniqueness = {
            "unique_audio_sha256_count": 252,
            "note_timbre_round_robin_group_count": 252,
            "groups_with_three_velocity_zones": 252,
            "groups_byte_identical_across_all_three_velocity_zones": 252,
            "effective_velocity_recording_layers": 1,
            "mapped_velocity_zones": 3,
        }
        mismatches = {
            name: {
                "expected": expected,
                "actual": uniqueness_audit.get(name),
            }
            for name, expected in expected_uniqueness.items()
            if uniqueness_audit.get(name) != expected
        }
        if mismatches:
            failures.append(
                {
                    "code": "source_velocity_duplication_pattern_changed",
                    "mismatches": mismatches,
                }
            )

    sample_diagnostics: dict[str, dict[str, object]] = {}
    measured_values: dict[str, float] = {}
    measurement_status_counts: dict[str, int] = {}
    for sample in samples:
        try:
            result = measurement_function(root, sample, active_settings)
        except (OSError, RuntimeError, ValueError) as error:
            result = SampleMeasurement(
                status="rejected_measurement_exception",
                measured_detune_cents=None,
                confidence=0.0,
                diagnostics={
                    "sample_path": sample.sample_path,
                    "upstream_root_note": sample.root_note,
                    "root_note": (
                        sample.root_note
                        + active_settings.source_root_note_offset
                    ),
                    "source_root_note_offset": (
                        active_settings.source_root_note_offset
                    ),
                    "velocity_low": sample.velocity_low,
                    "velocity_high": sample.velocity_high,
                    "velocity_layer": (
                        f"{sample.velocity_low}-{sample.velocity_high}"
                    ),
                    "round_robin_position": sample.round_robin_position,
                    "timbre": sample.timbre,
                    "reason": f"{type(error).__name__}: {error}",
                },
            )
        diagnostics = dict(result.diagnostics)
        diagnostics["status"] = result.status
        diagnostics["confidence"] = _rounded(result.confidence)
        sample_diagnostics[sample.sample_path] = diagnostics
        measurement_status_counts[result.status] = (
            measurement_status_counts.get(result.status, 0) + 1
        )
        if result.status != "accepted" or result.measured_detune_cents is None:
            failures.append(
                {
                    "code": "sample_measurement_rejected",
                    "sample_path": sample.sample_path,
                    "status": result.status,
                    "reason": str(diagnostics.get("reason", "")),
                }
            )
            continue
        value = float(result.measured_detune_cents)
        if (
            not math.isfinite(value)
            or abs(value) > active_settings.maximum_safe_absolute_detune_cents
        ):
            diagnostics["status"] = "rejected_unsafe_measurement"
            diagnostics["reason"] = (
                "accepted measurement function returned a non-finite or "
                "out-of-policy detune"
            )
            failures.append(
                {
                    "code": "unsafe_measurement_value",
                    "sample_path": sample.sample_path,
                    "measured_detune_cents": value,
                }
            )
            continue
        diagnostics["measured_detune_cents"] = _rounded(value)
        diagnostics["sfz_tune_cents"] = _rounded(-value)
        measured_values[sample.sample_path] = value

    groups: dict[int, list[AttackSample]] = {}
    for sample in samples:
        calibrated_root = (
            sample.root_note + active_settings.source_root_note_offset
        )
        groups.setdefault(calibrated_root, []).append(sample)
    note_diagnostics: dict[str, dict[str, object]] = {}
    peer_outliers: set[str] = set()
    for root_note, group in sorted(groups.items()):
        present = [
            measured_values[item.sample_path]
            for item in group
            if item.sample_path in measured_values
        ]
        note_record: dict[str, object] = {
            "root_note": root_note,
            "upstream_root_note": (
                root_note - active_settings.source_root_note_offset
            ),
            "expected_hz": _rounded(
                midi_to_hz(root_note, active_settings.reference_a4_hz)
            ),
            "sample_count": len(group),
            "measured_sample_count": len(present),
        }
        if expected_samples_per_note is not None and len(group) != (
            expected_samples_per_note
        ):
            failures.append(
                {
                    "code": "per_note_sample_count_mismatch",
                    "root_note": root_note,
                    "expected": expected_samples_per_note,
                    "actual": len(group),
                }
            )
            note_record["status"] = "rejected_sample_count"
        if present:
            center = float(statistics.median(present))
            deviations: dict[str, float] = {}
            for item in group:
                value = measured_values.get(item.sample_path)
                if value is None:
                    continue
                deviation = value - center
                deviations[item.sample_path] = _rounded(deviation)
                sample_diagnostics[item.sample_path][
                    "residual_to_note_median_cents"
                ] = _rounded(deviation)
            exact_peer_centers = _subgroup_centers(
                group,
                measured_values,
                lambda item: (
                    f"{item.timbre}/rr{item.round_robin_position}"
                ),
            )
            exact_peer_deviations: dict[str, float] = {}
            for item in group:
                value = measured_values.get(item.sample_path)
                if value is None:
                    continue
                peer_name = (
                    f"{item.timbre}/rr{item.round_robin_position}"
                )
                peer_deviation = value - exact_peer_centers[peer_name]
                exact_peer_deviations[item.sample_path] = _rounded(
                    peer_deviation
                )
                sample_diagnostics[item.sample_path][
                    "residual_to_same_timbre_rr_median_cents"
                ] = _rounded(peer_deviation)
                if abs(peer_deviation) > (
                    active_settings.maximum_peer_deviation_cents
                ):
                    peer_outliers.add(item.sample_path)
            note_record.update(_distribution(present))
            note_record["residuals_to_median_cents"] = {
                path: deviations[path] for path in sorted(deviations)
            }
            note_record["residuals_to_same_timbre_rr_median_cents"] = {
                path: exact_peer_deviations[path]
                for path in sorted(exact_peer_deviations)
            }
            subgroup_checks: dict[str, dict[str, object]] = {}
            round_robin_failed = False
            dimensions: tuple[
                tuple[str, Callable[[AttackSample], str]], ...
            ] = (
                ("timbre", lambda item: item.timbre),
                (
                    "velocity_layer",
                    lambda item: f"{item.velocity_low}-{item.velocity_high}",
                ),
                (
                    "round_robin",
                    lambda item: str(item.round_robin_position),
                ),
            )
            for dimension, grouping_key in dimensions:
                centers = _subgroup_centers(group, measured_values, grouping_key)
                spread = (
                    max(centers.values()) - min(centers.values())
                    if len(centers) >= 2
                    else 0.0
                )
                subgroup_checks[dimension] = {
                    "median_detune_cents": {
                        name: _rounded(value)
                        for name, value in centers.items()
                    },
                    "maximum_median_spread_cents": _rounded(spread),
                    "status": (
                        "accepted"
                        if spread
                        <= active_settings.maximum_peer_deviation_cents
                        else "review_difference"
                    ),
                    "gate_role": "review_only",
                }
            round_robin_by_timbre: dict[str, dict[str, object]] = {}
            for timbre in sorted({item.timbre for item in group}):
                timbre_group = [
                    item for item in group if item.timbre == timbre
                ]
                centers = _subgroup_centers(
                    timbre_group,
                    measured_values,
                    lambda item: str(item.round_robin_position),
                )
                spread = (
                    max(centers.values()) - min(centers.values())
                    if len(centers) >= 2
                    else 0.0
                )
                failed = (
                    spread > active_settings.maximum_peer_deviation_cents
                )
                round_robin_by_timbre[timbre] = {
                    "median_detune_cents": {
                        name: _rounded(value)
                        for name, value in centers.items()
                    },
                    "maximum_median_spread_cents": _rounded(spread),
                    "status": (
                        "rejected_round_robin_disagreement"
                        if failed
                        else "accepted"
                    ),
                    "gate_role": "formal",
                }
                if failed:
                    round_robin_failed = True
                    failures.append(
                        {
                            "code": "round_robin_disagreement",
                            "root_note": root_note,
                            "timbre": timbre,
                            "median_spread_cents": _rounded(spread),
                            "limit_cents": (
                                active_settings.maximum_peer_deviation_cents
                            ),
                        }
                    )
            subgroup_checks[
                "round_robin_within_timbre"
            ] = round_robin_by_timbre
            note_record["subgroup_cross_checks"] = subgroup_checks
            flagged = sorted(path for path in deviations if path in peer_outliers)
            note_record["peer_outliers"] = flagged
            if flagged:
                note_record["status"] = "rejected_peer_outlier"
                for path in flagged:
                    failures.append(
                        {
                            "code": "peer_outlier",
                            "sample_path": path,
                            "root_note": root_note,
                            "residual_to_note_median_cents": deviations[path],
                            "limit_cents": (
                                active_settings.maximum_peer_deviation_cents
                            ),
                        }
                    )
                    sample_diagnostics[path]["status"] = "rejected_peer_outlier"
                    sample_diagnostics[path]["reason"] = (
                        "measurement disagrees with byte-equivalent velocity "
                        "peers in the same timbre and round robin"
                    )
            elif round_robin_failed:
                note_record["status"] = "rejected_round_robin_disagreement"
            elif note_record.get("status") is None:
                note_record["status"] = "accepted"
        else:
            note_record["status"] = "rejected_no_measurements"
        note_diagnostics[str(root_note)] = note_record

    accepted_values = {
        path: value
        for path, value in measured_values.items()
        if path not in peer_outliers
    }
    successful = (
        not failures
        and len(accepted_values) == len(samples)
        and (
            expected_sample_count is None
            or len(accepted_values) == expected_sample_count
        )
    )
    ordered_values = {
        path: _rounded(accepted_values[path])
        for path in sorted(accepted_values)
    }
    settings_document = asdict(active_settings)
    settings_document["window_starts_seconds"] = list(
        active_settings.window_starts_seconds
    )
    final_status_counts: dict[str, int] = {}
    for record in sample_diagnostics.values():
        status = str(record.get("status", "missing_diagnostic"))
        final_status_counts[status] = final_status_counts.get(status, 0) + 1
    diagnostics_document: dict[str, object] = {
        "schema_version": 1,
        "status": "passed" if successful else "failed",
        "applicable": True,
        "pitch_mode": "pitched",
        "source_root_name": root.name,
        "reference_a4_hz": active_settings.reference_a4_hz,
        "source_note_range": (
            [
                min(sample.root_note for sample in samples),
                max(sample.root_note for sample in samples),
            ]
            if samples
            else []
        ),
        "playback_note_range": (
            [
                min(
                    sample.root_note
                    + active_settings.source_root_note_offset
                    for sample in samples
                ),
                max(
                    sample.root_note
                    + active_settings.source_root_note_offset
                    for sample in samples
                ),
            ]
            if samples
            else []
        ),
        "source_root_note_offset": active_settings.source_root_note_offset,
        "measurement_algorithm": (
            f"{len(active_settings.window_starts_seconds)} pitch-adaptive "
            "post-attack windows through octave-aware wide analysis; "
            "independent temporal-consistency gates for harmonic-constrained "
            "FFT and local autocorrelation, with exact-root periodicity"
        ),
        "calibration_semantics": (
            "SFZ conversion applies tune=-measured_detune_cents"
        ),
        "source_audio_uniqueness": uniqueness_audit,
        "settings": settings_document,
        "summary": {
            "declared_sample_count": len(samples),
            "accepted_before_peer_check": len(measured_values),
            "accepted_after_peer_check": len(accepted_values),
            "rejected_sample_count": len(samples) - len(accepted_values),
            "note_count": len(groups),
            "peer_outlier_count": len(peer_outliers),
            "status_counts": {
                name: final_status_counts[name]
                for name in sorted(final_status_counts)
            },
            "raw_measurement_status_counts": {
                name: measurement_status_counts[name]
                for name in sorted(measurement_status_counts)
            },
            "accepted_distribution": _distribution(
                list(accepted_values.values())
            ),
        },
        "breakdown": {
            "timbre": _breakdown(
                samples,
                accepted_values,
                lambda item: item.timbre,
                sample_diagnostics,
            ),
            "velocity_layer": _breakdown(
                samples,
                accepted_values,
                lambda item: f"{item.velocity_low}-{item.velocity_high}",
                sample_diagnostics,
            ),
            "round_robin": _breakdown(
                samples,
                accepted_values,
                lambda item: str(item.round_robin_position),
                sample_diagnostics,
            ),
        },
        "notes": note_diagnostics,
        "samples": {
            path: sample_diagnostics[path] for path in sorted(sample_diagnostics)
        },
        "failures": failures,
    }
    _write_json_atomic(Path(diagnostics_path).resolve(), diagnostics_document)
    if not successful:
        preview = "; ".join(
            f"{item['code']}"
            + (
                f":{item['sample_path']}"
                if "sample_path" in item
                else ""
            )
            for item in failures[:8]
        )
        raise CalibrationRejectedError(
            f"SIMPK calibration rejected with {len(failures)} failure(s): "
            f"{preview}. Full diagnostics: {Path(diagnostics_path).resolve()}"
        )

    calibration_document: dict[str, object] = {
        "schema_version": 1,
        "unit": "cents",
        "measured_detune_cents": ordered_values,
    }
    _write_json_atomic(Path(calibration_path).resolve(), calibration_document)
    return calibration_document, diagnostics_document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strictly calibrate all 756 SIMPK clavichord attack WAVs."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help=f"extracted SIMPK source root (default: {DEFAULT_SOURCE_ROOT})",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=DEFAULT_CALIBRATION_PATH,
        help="strict calibration JSON consumed by the converter",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        default=DEFAULT_DIAGNOSTICS_PATH,
        help="detailed measurement and rejection diagnostics JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        calibration, diagnostics = calibrate_simpk_source(
            args.source_root,
            calibration_path=args.calibration,
            diagnostics_path=args.diagnostics,
        )
    except CalibrationRejectedError as error:
        print(str(error), file=sys.stderr)
        return 2
    summary = diagnostics["summary"]
    assert isinstance(summary, dict)
    print(
        "SIMPK clavichord calibration passed: "
        f"{len(calibration['measured_detune_cents'])} samples, "
        f"{summary['note_count']} notes"
    )
    print(f"Calibration: {Path(args.calibration).resolve()}")
    print(f"Diagnostics: {Path(args.diagnostics).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
