"""Auditable ensemble-balance reports built from rendered dry stems.

The report is deliberately advisory.  It measures the post-assignment-gain,
pre-pan, pre-space signal and compares only relationships explicitly declared
by the roster.  Neither a role nor a failed relationship authorizes hidden
gain changes; ``suggest`` merely adds a bounded number that a caller may
review and write back into ``gain_db`` or ``gain_automation``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from numbers import Real
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, BinaryIO

import numpy as np

from .analysis_cache import CollaborationAnalysisCache
from ._window_batches import window_rms
from .mix_analysis import (
    ANALYSIS_VERSION,
    FREQUENCY_BANDS_HZ,
    MixAnalysisConfig,
    OverlapActiveRmsDifference,
    analyze_track,
    overlap_active_rms_difference,
)
from .roster import (
    MAX_BALANCE_RELATIONS,
    CollaborationSettings,
    Executor,
)
from .render_lock import (
    capture_plain_directory,
    revalidate_plain_directory,
)
from .spectral_overlap import (
    SPECTRAL_OVERLAP_VERSION,
    SpectralOverlapAnalysis,
    analyze_spectral_overlap,
)
from .stem_cache import (
    PROCESS_SOURCE_TREE_SHA256,
    build_cache_key,
    current_source_tree_matches,
)
from .temporal_balance import (
    TEMPORAL_BALANCE_VERSION,
    TemporalBalanceAnalysis,
    analyze_temporal_balance,
)


MIX_REPORT_FORMAT = "tianlai.mix_report"
MIX_REPORT_VERSION = 2
MIX_REPORT_NAME = "混音诊断.json"
GAIN_AUTOMATION_DRAFT_FORMAT = "tianlai.gain_automation_draft"
GAIN_AUTOMATION_DRAFT_VERSION = 1
_RELATIVE_GATE_DB = -40.0
_MINIMUM_OVERLAP_SECONDS = 0.5
_MINIMUM_SHARED_WINDOWS = 3
_ACTIVITY_BLOCK_MS = 10.0
_SPECTRAL_OVERLAP_CANDIDATE_THRESHOLD = 0.65
_BAND_ENERGY_RELEVANCE_THRESHOLD = 0.08
_MONO_FOLD_CANDIDATE_DB = -6.0
_TAIL_CUTOFF_CANDIDATE_DB = -50.0
TAIL_ANALYSIS_SECONDS = 0.25
ANALYSIS_CACHE_STAGE = (
    "post_assignment_gain_pre_pan_pre_space_pre_master_pre_normalize_v1"
)
ANALYSIS_CACHE_IDENTITY_VERSION = 1
_STEM_TRANSACTION_MAX_BLOCK_FRAMES = 65_536
_FLOAT32_STEREO_FRAME_BYTES = 2 * np.dtype(np.float32).itemsize


def _round_optional(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0.0 else rounded


def _bounded_adjustment(
    *,
    target_offset_db: float,
    measured_offset_db: float,
    maximum_absolute_adjustment_db: float,
) -> float:
    if not all(
        math.isfinite(value)
        for value in (
            target_offset_db,
            measured_offset_db,
            maximum_absolute_adjustment_db,
        )
    ):
        raise ValueError("balance suggestion inputs must be finite")
    if maximum_absolute_adjustment_db < 0.0:
        raise ValueError("balance suggestion bound must not be negative")
    adjustment = max(
        -maximum_absolute_adjustment_db,
        min(
            maximum_absolute_adjustment_db,
            target_offset_db - measured_offset_db,
        ),
    )
    if not math.isfinite(adjustment):
        raise ValueError("balance suggestion is not finite")
    rounded = _round_optional(adjustment)
    if rounded is None:
        raise RuntimeError("balance suggestion did not produce a number")
    return rounded


def _gain_automation_draft(
    *,
    subject: str,
    reference: str,
    subject_endpoint: dict[str, Any],
    reference_endpoint: dict[str, Any],
    target_offset_db: float,
    maximum_absolute_adjustment_db: float,
    candidate_segments: list[dict[str, Any]],
    source_candidate_segment_count: int,
    segments_truncated: bool,
) -> dict[str, Any]:
    """Turn coarse diagnostics into a non-executable creator review draft."""

    subject_kind = str(subject_endpoint["endpoint_kind"])
    subject_parts = list(subject_endpoint["expanded_parts"])
    segments: list[dict[str, Any]] = []
    for segment in candidate_segments:
        measured = float(segment["median_offset_db"])
        segments.append(
            {
                **segment,
                "suggested_subject_gain_adjustment_db": (
                    _bounded_adjustment(
                        target_offset_db=target_offset_db,
                        measured_offset_db=measured,
                        maximum_absolute_adjustment_db=(
                            maximum_absolute_adjustment_db
                        ),
                    )
                ),
            }
        )
    return {
        "format": GAIN_AUTOMATION_DRAFT_FORMAT,
        "version": GAIN_AUTOMATION_DRAFT_VERSION,
        "status": "creator_review_required",
        "executable": False,
        "audio_modified": False,
        "subject": subject,
        "reference": reference,
        "subject_endpoint": {
            "endpoint_kind": subject_kind,
            "expanded_parts": subject_parts,
        },
        "reference_endpoint": {
            "endpoint_kind": reference_endpoint["endpoint_kind"],
            "expanded_parts": list(reference_endpoint["expanded_parts"]),
        },
        "time_basis": "seconds",
        "adjustment_semantics": (
            "additive_subject_gain_offset_db_not_absolute_gain"
        ),
        "maximum_absolute_adjustment_db": (
            maximum_absolute_adjustment_db
        ),
        "source_candidate_segment_count": source_candidate_segment_count,
        "segments_truncated": segments_truncated,
        "segments": segments,
        "workflow": {
            "locate_boundaries_with": "MCP locate(at_seconds=...)",
            "creator_review_required": True,
            "write_target": "roster.assignments[].gain_automation",
            "write_target_parts": subject_parts,
            "subject_application": (
                "creator_distributes_or_uniformly_applies_to_expanded_parts"
                if subject_kind == "part_group"
                else "review_then_write_subject_part"
            ),
        },
        "notice": (
            "该草稿只把粗粒度诊断转换为有界的相对增益建议；它不会执行、"
            "不会修改音频或 roster，创作者应先按乐句意图审阅并用 locate "
            "把秒数边界映射到小节与拍位；subject 若为 part_group，roster "
            "中不存在同名组级 assignment，必须由创作者把变化分配或统一应用"
            "到 expanded_parts。"
        ),
    }


def _config(settings: CollaborationSettings) -> MixAnalysisConfig:
    analysis = settings.analysis
    return MixAnalysisConfig(
        window_seconds=analysis.window_ms / 1000.0,
        hop_seconds=analysis.hop_ms / 1000.0,
        absolute_gate_dbfs=analysis.gate_dbfs,
        relative_gate_db=_RELATIVE_GATE_DB,
    )


def attach_stage_diagnostics(
    report: dict[str, Any],
    *,
    post_pan_pre_space: dict[str, Any],
    post_space_pre_master: dict[str, Any],
    final: dict[str, Any],
) -> None:
    """Attach actual mix-stage measurements without changing any samples."""

    report["stage_metrics"] = {
        "post_pan_pre_space": post_pan_pre_space,
        "post_space_pre_master": post_space_pre_master,
        "final": final,
    }
    analysis = report["analysis"]
    analysis["stage_screening"] = {
        "mono_fold_delta_db_below": _MONO_FOLD_CANDIDATE_DB,
        "tail_peak_relative_db_above": _TAIL_CUTOFF_CANDIDATE_DB,
        "tail_window_seconds": TAIL_ANALYSIS_SECONDS,
    }
    warnings_list = report["warnings"]
    candidate_count = 0

    final_peak = float(final["sample_peak"])
    mono_silent = bool(final["mono_fold_silent"])
    mono_delta = final["mono_fold_delta_db"]
    if final_peak > 0.0 and (
        mono_silent
        or (
            isinstance(mono_delta, Real)
            and float(mono_delta) < _MONO_FOLD_CANDIDATE_DB
        )
    ):
        candidate_count += 1
        warnings_list.append(
            {
                "code": "final_mix_mono_fold_candidate",
                "mono_fold_silent": mono_silent,
                "mono_fold_delta_db": mono_delta,
                "threshold_db": _MONO_FOLD_CANDIDATE_DB,
                "message": (
                    "最终合奏折叠为单声道时能量明显下降；请检查反相、"
                    "过宽声像或空间处理"
                ),
            }
        )

    tail = post_space_pre_master.get("tail_window")
    if isinstance(tail, dict):
        relative = tail.get("peak_relative_to_full_track_db")
        if (
            tail.get("silent") is False
            and isinstance(relative, Real)
            and float(relative) > _TAIL_CUTOFF_CANDIDATE_DB
        ):
            candidate_count += 1
            warnings_list.append(
                {
                    "code": "space_tail_cutoff_candidate",
                    "peak_relative_to_full_track_db": float(relative),
                    "threshold_db": _TAIL_CUTOFF_CANDIDATE_DB,
                    "tail_window_seconds": TAIL_ANALYSIS_SECONDS,
                    "message": (
                        "合奏末尾窗口仍有较高峰值，可能存在厅堂尾音截断"
                    ),
                }
            )

    report["summary"]["stage_candidate_count"] = candidate_count
    report["summary"]["warning_count"] = len(warnings_list)


@dataclass(frozen=True, slots=True)
class _StemEntry:
    executor_id: str
    part_id: str
    instrument: str
    gain_db: float
    pan: float
    role: dict[str, str] | None
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "part_id": self.part_id,
            "instrument": self.instrument,
            "gain_db": self.gain_db,
            "pan": self.pan,
            "role": self.role,
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class _BlockActivity:
    frame_count: int
    block_frames: int
    starts: range
    active: np.ndarray


def _block_activity(
    audio: np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig,
) -> _BlockActivity:
    """Gate fixed, non-overlapping blocks for conservative overlap evidence."""

    block_frames = max(
        1,
        round((_ACTIVITY_BLOCK_MS / 1000.0) * sample_rate),
    )
    # A long score can contain tens of thousands of 10 ms blocks per part.
    # Keep the arithmetic progression itself instead of retaining a Python
    # integer object for every block start.
    starts = range(0, int(audio.shape[0]), block_frames)
    levels = window_rms(audio, starts, block_frames)
    loudest = float(np.max(levels))
    if loudest <= 0.0:
        active = np.zeros(len(starts), dtype=bool)
    else:
        absolute = 10.0 ** (config.absolute_gate_dbfs / 20.0)
        relative = loudest * 10.0 ** (config.relative_gate_db / 20.0)
        threshold = max(absolute, relative)
        active = (levels >= threshold) & (levels > 0.0)
    return _BlockActivity(
        frame_count=int(audio.shape[0]),
        block_frames=block_frames,
        starts=starts,
        active=active,
    )


def _shared_block_evidence(
    first: _BlockActivity,
    second: _BlockActivity,
    sample_rate: int,
    shared_main_window_count: int,
) -> dict[str, Any]:
    if (
        first.frame_count != second.frame_count
        or first.block_frames != second.block_frames
        or first.starts != second.starts
    ):
        raise ValueError("relation activity blocks must share one timeline")
    shared_active = first.active & second.active
    shared_block_count = int(np.count_nonzero(shared_active))
    shared_frames = shared_block_count * first.block_frames
    # Every block except the last spans block_frames exactly.  If the common
    # activity includes a partial final block, deduct only its zero padding.
    if (
        shared_block_count
        and shared_active.size
        and bool(shared_active[-1])
    ):
        final_start = first.starts[-1]
        shared_frames -= max(
            0,
            first.block_frames - (first.frame_count - final_start),
        )
    shared_seconds = _round_optional(shared_frames / sample_rate, 9)
    if shared_seconds is None:
        raise RuntimeError("shared block coverage did not produce a number")
    sufficient = (
        shared_main_window_count >= _MINIMUM_SHARED_WINDOWS
        and shared_seconds >= _MINIMUM_OVERLAP_SECONDS
    )
    return {
        "metric": "shared_active_non_overlapping_block_rms_coverage",
        "block_ms": _ACTIVITY_BLOCK_MS,
        "effective_block_ms": _round_optional(
            first.block_frames * 1000.0 / sample_rate,
            9,
        ),
        "shared_active_block_count": shared_block_count,
        "shared_active_seconds": shared_seconds,
        "minimum_seconds": _MINIMUM_OVERLAP_SECONDS,
        "shared_main_window_count": shared_main_window_count,
        "minimum_main_window_count": _MINIMUM_SHARED_WINDOWS,
        "status": "sufficient" if sufficient else "insufficient",
        "coverage_resolution": (
            "non_overlapping_block_rms_not_sample_exact"
        ),
    }


def _inconclusive_spectral_document(
    comparison: OverlapActiveRmsDifference,
) -> dict[str, Any]:
    """Describe the known window counts without performing any FFT."""

    null_bands = tuple(
        (name, None)
        for name, _low_hz, _high_hz in FREQUENCY_BANDS_HZ
    )
    return SpectralOverlapAnalysis(
        sample_rate_hz=comparison.sample_rate_hz,
        frame_count=comparison.frame_count,
        config=comparison.config,
        shared_active_window_count=(
            comparison.shared_active_window_count
        ),
        window_count=comparison.window_count,
        overlap_ratio=comparison.overlap_ratio,
        first_band_energy_ratios=null_bands,
        second_band_energy_ratios=null_bands,
        band_first_minus_second_db=null_bands,
        spectral_overlap_coefficient=None,
    ).to_dict()


def _inconclusive_temporal_document(
    comparison: OverlapActiveRmsDifference,
    *,
    target_offset_db: float,
    tolerance_db: float,
    minimum_shared_window_count: int,
) -> dict[str, Any]:
    """Describe an inconclusive relation without repeating RMS window scans."""

    overlap_ratio = _round_optional(comparison.overlap_ratio, 6)
    if overlap_ratio is None:
        raise RuntimeError("relation overlap ratio did not produce a number")
    return TemporalBalanceAnalysis(
        sample_rate_hz=comparison.sample_rate_hz,
        frame_count=comparison.frame_count,
        config=comparison.config,
        target_offset_db=target_offset_db,
        tolerance_db=tolerance_db,
        minimum_shared_window_count=minimum_shared_window_count,
        shared_active_window_count=(
            comparison.shared_active_window_count
        ),
        window_count=comparison.window_count,
        overlap_ratio=overlap_ratio,
        p10_db=None,
        median_db=None,
        p90_db=None,
        robust_span_db=None,
        within_tolerance_window_ratio=None,
        below_tolerance_window_count=None,
        above_tolerance_window_count=None,
        status="insufficient_overlap",
    ).to_dict()


def _analysis_cache_summary(
    *,
    stem_count: int | None,
    relation_count: int,
) -> dict[str, Any]:
    def section(total: int | None) -> dict[str, Any]:
        return {
            "total": total,
            "accounted": 0,
            "unaccounted": None if total is None else total,
            "hits": 0,
            "misses": 0,
            "bypassed": 0,
            "corrupt_fallbacks": 0,
            "writes": 0,
            "write_skips": 0,
            "write_failures": 0,
            "conflicts": 0,
        }

    return {
        "requested": True,
        "active": True,
        "stage": ANALYSIS_CACHE_STAGE,
        "identity_version": ANALYSIS_CACHE_IDENTITY_VERSION,
        "producer_source_tree_sha256": PROCESS_SOURCE_TREE_SHA256,
        "stem": section(stem_count),
        "relation": section(relation_count),
        "events": [],
        "reason_counts": {},
        "performed_fft_input_frame_visits": 0,
        "avoided_fft_input_frame_visits": 0,
    }


def _finalize_analysis_cache_summary(
    summary: dict[str, Any],
    *,
    stem_count: int,
) -> None:
    summary["stem"]["total"] = stem_count
    for scope in ("stem", "relation"):
        section = summary[scope]
        accounted = (
            int(section["hits"])
            + int(section["misses"])
            + int(section["bypassed"])
        )
        section["accounted"] = accounted
        section["unaccounted"] = int(section["total"]) - accounted


def _note_analysis_cache(
    summary: dict[str, Any],
    *,
    scope: str,
    status: str,
    reason: str,
    key: str | None,
) -> None:
    section = summary[scope]
    section[status] = int(section[status]) + 1
    reasons = summary["reason_counts"]
    reason_key = f"{scope}:{reason}"
    reasons[reason_key] = int(reasons.get(reason_key, 0)) + 1
    summary["events"].append(
        {
            "scope": scope,
            "status": {
                "hits": "hit",
                "misses": "miss",
                "bypassed": "bypassed",
            }[status],
            "reason": reason,
            "key": key,
        }
    )


def _note_analysis_cache_store(
    summary: dict[str, Any],
    *,
    scope: str,
    status: str,
) -> None:
    section = summary[scope]
    if status in ("stored", "repaired"):
        section["writes"] = int(section["writes"]) + 1
    elif status in ("exists", "busy", "source_changed_before_store"):
        section["write_skips"] = int(section["write_skips"]) + 1
    elif status == "conflict":
        section["conflicts"] = int(section["conflicts"]) + 1
    else:
        section["write_failures"] = int(section["write_failures"]) + 1
    summary["events"][-1]["store_status"] = status


def _note_source_change_before_store(
    summary: dict[str, Any],
    *,
    scope: str,
) -> None:
    """Disable this session and explain why a computed entry was not stored."""

    summary["active"] = False
    reasons = summary["reason_counts"]
    reason = f"{scope}:producer_source_changed_before_store"
    reasons[reason] = int(reasons.get(reason, 0)) + 1
    _note_analysis_cache_store(
        summary,
        scope=scope,
        status="source_changed_before_store",
    )


def _audio_content_sha256(audio: np.ndarray) -> str:
    array = np.asarray(audio)
    if (
        array.ndim != 2
        or array.shape[1] != 2
        or array.dtype != np.dtype(np.float32)
    ):
        raise ValueError(
            "analysis cache audio must be float32 stereo"
        )
    digest = hashlib.sha256()
    for start in range(0, int(array.shape[0]), 262_144):
        chunk = np.asarray(array[start : start + 262_144])
        if not chunk.flags.c_contiguous:
            raise ValueError("analysis cache audio must be C-contiguous")
        if not bool(np.isfinite(chunk).all()):
            raise ValueError("analysis cache audio is not finite")
        digest.update(memoryview(chunk).cast("B"))
    return digest.hexdigest()


def _finite_json_tree(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json_tree(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_json_tree(item)
            for key, item in value.items()
        )
    return False


_STEM_METRIC_KEYS = frozenset(
    {
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
    }
)


def _valid_cached_stem_metrics(
    payload: dict[str, Any],
    *,
    sample_rate: int,
    frame_count: int,
    config: MixAnalysisConfig,
) -> bool:
    try:
        if (
            set(payload) != _STEM_METRIC_KEYS
            or not _finite_json_tree(payload)
        ):
            return False
        active_count = payload.get("active_window_count")
        window_count = payload.get("window_count")
        ratios = payload.get("band_energy_ratios")
        return (
            payload.get("analysis_version") == ANALYSIS_VERSION
            and payload.get("sample_rate_hz") == sample_rate
            and payload.get("frame_count") == frame_count
            and payload.get("gate") == config.to_dict()
            and isinstance(active_count, int)
            and not isinstance(active_count, bool)
            and isinstance(window_count, int)
            and not isinstance(window_count, bool)
            and 0 <= active_count <= window_count
            and window_count >= 1
            and isinstance(ratios, dict)
            and set(ratios)
            == {name for name, _low, _high in FREQUENCY_BANDS_HZ}
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False


_RELATION_PAYLOAD_KEYS = frozenset(
    {
        "measurement",
        "overlap_evidence",
        "spectral_overlap",
        "temporal_balance",
        "relation_shared_active_window_count",
    }
)


def _valid_cached_relation_payload(
    payload: dict[str, Any],
    *,
    sample_rate: int,
    frame_count: int,
    config: MixAnalysisConfig,
    target_offset_db: float,
    tolerance_db: float,
    minimum_shared_window_count: int,
) -> bool:
    try:
        if (
            set(payload) != _RELATION_PAYLOAD_KEYS
            or not _finite_json_tree(payload)
        ):
            return False
        measurement = payload.get("measurement")
        overlap = payload.get("overlap_evidence")
        spectral = payload.get("spectral_overlap")
        temporal = payload.get("temporal_balance")
        shared = payload.get("relation_shared_active_window_count")
        if not all(
            isinstance(item, dict)
            for item in (measurement, overlap, spectral, temporal)
        ):
            return False
        assert isinstance(measurement, dict)
        assert isinstance(overlap, dict)
        assert isinstance(spectral, dict)
        assert isinstance(temporal, dict)
        spectral_shared = spectral.get("shared_active_window_count")
        if (
            not isinstance(spectral_shared, int)
            or isinstance(spectral_shared, bool)
            or spectral_shared < 0
        ):
            return False
        return (
            isinstance(shared, int)
            and not isinstance(shared, bool)
            and shared >= 0
            and measurement.get("analysis_version") == ANALYSIS_VERSION
            and measurement.get("sample_rate_hz") == sample_rate
            and measurement.get("frame_count") == frame_count
            and measurement.get("gate") == config.to_dict()
            and overlap.get("shared_main_window_count")
            == measurement.get("shared_active_window_count")
            and spectral.get("version") == SPECTRAL_OVERLAP_VERSION
            and spectral.get("sample_rate_hz") == sample_rate
            and spectral.get("frame_count") == frame_count
            and spectral.get("gate") == config.to_dict()
            and temporal.get("version") == TEMPORAL_BALANCE_VERSION
            and temporal.get("sample_rate_hz") == sample_rate
            and temporal.get("frame_count") == frame_count
            and temporal.get("gate") == config.to_dict()
            and temporal.get("target_offset_db") == target_offset_db
            and temporal.get("tolerance_db") == tolerance_db
            and temporal.get("minimum_shared_window_count")
            == minimum_shared_window_count
            and shared
            == (
                spectral_shared
                if overlap.get("status") == "sufficient"
                else 0
            )
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False


def _same_scratch_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _close_private_stem_scratch(
    audio: np.memmap,
    scratch: BinaryIO,
    *,
    flush: bool,
) -> None:
    """Release a mapping before its delete-on-close descriptor."""

    first_error: BaseException | None = None
    if flush:
        try:
            audio.flush()
        except BaseException as exc:
            first_error = exc
    mapping = getattr(audio, "_mmap", None)
    if mapping is not None:
        try:
            mapping.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    try:
        scratch.close()
    except BaseException as exc:
        if first_error is None:
            first_error = exc
    if first_error is not None:
        raise first_error


class _AnalyzedStemView:
    """Explicit post-diagnostic access to one transaction mapping.

    A first relation executor transfers its scratch mapping to the report
    builder, so that view is borrowed and ``close()`` only drops the caller's
    reference.  Every other transaction mapping stays owned by the view and is
    closed, mapping first, when the renderer finishes its WAV and mix stages.
    """

    __slots__ = (
        "_audio",
        "_scratch",
        "_audio_sha256",
        "_builder_owned",
        "_closed",
    )

    def __init__(
        self,
        audio: np.memmap,
        scratch: BinaryIO | None,
        *,
        audio_sha256: str,
        builder_owned: bool,
    ) -> None:
        if builder_owned != (scratch is None):
            raise ValueError("analyzed stem view ownership is inconsistent")
        self._audio: np.memmap | None = audio
        self._scratch = scratch
        self._audio_sha256 = audio_sha256
        self._builder_owned = builder_owned
        self._closed = False

    @property
    def audio(self) -> np.memmap:
        audio = self._audio
        if self._closed or audio is None:
            raise RuntimeError("analyzed stem view is closed")
        return audio

    @property
    def audio_sha256(self) -> str:
        return self._audio_sha256

    @property
    def builder_owned(self) -> bool:
        return self._builder_owned

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        audio = self._audio
        scratch = self._scratch
        self._audio = None
        self._scratch = None
        if audio is not None and scratch is not None:
            _close_private_stem_scratch(audio, scratch, flush=False)

    def __enter__(self) -> "_AnalyzedStemView":
        if self._closed:
            raise RuntimeError("analyzed stem view is closed")
        return self

    def __exit__(
        self,
        exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except BaseException:
            # Context-manager cleanup must not replace the body failure.  The
            # production renderer closes explicitly and applies the same
            # first-error rule around its broader raw-stem phase.
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class _StemAnalysisTransaction:
    """Private bounded sink for one complete float32 analysis stem."""

    def __init__(
        self,
        builder: "CollaborationReportBuilder",
        executor: Executor,
        *,
        frame_count: int,
        expected_audio_sha256: str | None,
        audio: np.memmap,
        scratch: BinaryIO,
        opened_status: os.stat_result,
    ) -> None:
        self._builder = builder
        self._executor = executor
        self._frame_count = frame_count
        self._expected_audio_sha256 = expected_audio_sha256
        self._audio: np.memmap | None = audio
        self._scratch: BinaryIO | None = scratch
        self._opened_status = opened_status
        self._digest = hashlib.sha256()
        self._frames_written = 0
        self._validated_sha256: str | None = None
        self._closed = False
        self._retained = False

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def retained(self) -> bool:
        return self._retained

    @property
    def audio_sha256(self) -> str:
        if self._validated_sha256 is None:
            raise RuntimeError("stem analysis transaction is not complete")
        return self._validated_sha256

    def _release(self, *, flush: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._builder._stem_transactions.discard(self)
        audio = self._audio
        scratch = self._scratch
        self._audio = None
        self._scratch = None
        if audio is not None and scratch is not None:
            _close_private_stem_scratch(audio, scratch, flush=flush)

    def _release_without_masking_error(self) -> None:
        try:
            self._release(flush=False)
        except BaseException:
            pass

    def append(self, block: np.ndarray) -> None:
        """Append one finite float32 stereo block of at most 65,536 frames."""

        if self._closed:
            raise RuntimeError("stem analysis transaction is closed")
        try:
            array = np.asarray(block)
            if (
                array.ndim != 2
                or array.shape[1] != 2
                or array.dtype != np.dtype(np.float32)
            ):
                raise ValueError(
                    "stem analysis blocks must be float32 stereo"
                )
            block_frames = int(array.shape[0])
            if not 1 <= block_frames <= _STEM_TRANSACTION_MAX_BLOCK_FRAMES:
                raise ValueError(
                    "stem analysis blocks must contain between 1 and "
                    "65536 frames"
                )
            stop = self._frames_written + block_frames
            if stop > self._frame_count:
                raise ValueError(
                    "stem analysis transaction received too many frames"
                )
            stable = (
                array
                if array.flags.c_contiguous
                else np.ascontiguousarray(array, dtype=np.float32)
            )
            if not bool(np.isfinite(stable).all()):
                raise ValueError("stem analysis block is not finite")
            audio = self._audio
            if audio is None:
                raise RuntimeError("stem analysis transaction lost its mapping")
            audio[self._frames_written : stop] = stable
            self._digest.update(memoryview(stable).cast("B"))
            self._frames_written = stop
        except BaseException:
            self._release_without_masking_error()
            raise

    def finish_view(self) -> _AnalyzedStemView:
        """Validate, diagnose, and transfer an explicit mapping view."""

        if self._closed:
            raise RuntimeError("stem analysis transaction is closed")
        retained = False
        try:
            if self._frames_written != self._frame_count:
                raise ValueError(
                    "stem analysis transaction frame count is incomplete"
                )
            audio = self._audio
            scratch = self._scratch
            if audio is None or scratch is None:
                raise RuntimeError("stem analysis transaction lost its scratch")
            audio.flush()
            status = os.fstat(scratch.fileno())
            expected_bytes = self._frame_count * _FLOAT32_STEREO_FRAME_BYTES
            if (
                not _same_scratch_file_identity(self._opened_status, status)
                or status.st_size != expected_bytes
            ):
                raise ValueError(
                    "stem analysis scratch identity or length changed"
                )
            appended_sha256 = self._digest.hexdigest()
            mapped_sha256 = _audio_content_sha256(audio)
            final_status = os.fstat(scratch.fileno())
            if (
                not _same_scratch_file_identity(status, final_status)
                or final_status.st_size != expected_bytes
            ):
                raise ValueError(
                    "stem analysis scratch changed during validation"
                )
            if mapped_sha256 != appended_sha256:
                raise ValueError(
                    "stem analysis scratch SHA-256 differs from appended blocks"
                )
            if (
                self._expected_audio_sha256 is not None
                and mapped_sha256 != self._expected_audio_sha256
            ):
                raise ValueError(
                    "stem analysis SHA-256 differs from the expected source"
                )
            self._validated_sha256 = mapped_sha256
            retained = self._builder._add_stem(
                self._executor,
                audio,
                validated_audio_sha256=mapped_sha256,
                owned_scratch=(audio, scratch),
            )
        except BaseException:
            self._release_without_masking_error()
            raise

        if retained:
            # _add_stem registered both the mapping and its handle before it
            # returned.  Clear transaction ownership before allocating the
            # borrowed view so a MemoryError cannot double-close builder state.
            self._builder._stem_transactions.discard(self)
            self._closed = True
            self._retained = True
            self._audio = None
            self._scratch = None
            try:
                return _AnalyzedStemView(
                    audio,
                    None,
                    audio_sha256=self.audio_sha256,
                    builder_owned=True,
                )
            except BaseException:
                self._builder._mark_abort_only()
                raise

        # Construct the owning view before relinquishing transaction state. If
        # allocation fails, the transaction's normal error path still owns and
        # releases both resources.
        try:
            view = _AnalyzedStemView(
                audio,
                scratch,
                audio_sha256=self.audio_sha256,
                builder_owned=False,
            )
        except BaseException:
            self._builder._mark_abort_only()
            self._release_without_masking_error()
            raise
        self._builder._stem_transactions.discard(self)
        self._closed = True
        self._audio = None
        self._scratch = None
        return view

    def finish(self) -> str:
        """Validate and diagnose, then retain the legacy digest result."""

        view = self.finish_view()
        try:
            return view.audio_sha256
        finally:
            view.close()

    def close(self) -> None:
        self._release(flush=False)

    def __enter__(self) -> "_StemAnalysisTransaction":
        if self._closed:
            raise RuntimeError("stem analysis transaction is closed")
        return self

    def __exit__(
        self,
        exc_type: Any,
        _value: Any,
        _traceback: Any,
    ) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.close()
        else:
            self._release_without_masking_error()

    def __del__(self) -> None:
        try:
            self._release(flush=False)
        except BaseException:
            pass


class CollaborationReportBuilder:
    """Collect per-stem measurements and declared part-to-part comparisons."""

    def __init__(
        self,
        settings: CollaborationSettings,
        sample_rate: int,
        *,
        scratch_parent: str | Path | None = None,
        cache_directory: str | Path | None = None,
        expected_stem_count: int | None = None,
    ) -> None:
        if not isinstance(settings, CollaborationSettings):
            raise ValueError("settings must be CollaborationSettings")
        if settings.mode not in ("analyze", "suggest"):
            raise ValueError(
                "collaboration report requires analyze or suggest mode"
            )
        if len(settings.balance_relations) > MAX_BALANCE_RELATIONS:
            raise ValueError(
                "collaboration report supports at most "
                f"{MAX_BALANCE_RELATIONS} balance relations"
            )
        self.settings = settings
        self.sample_rate = sample_rate
        self.config = _config(settings)
        # Validate rounded frame lengths before any stem analysis begins.
        self.config.frame_lengths(sample_rate)
        self.minimum_shared_window_count = _MINIMUM_SHARED_WINDOWS
        self._entries: list[_StemEntry] = []
        self._part_groups = {
            group.id: group.parts for group in settings.part_groups
        }
        if len(self._part_groups) != len(settings.part_groups):
            raise ValueError("collaboration part groups must have unique ids")
        group_ids = frozenset(self._part_groups)
        for group_id, members in self._part_groups.items():
            if not group_id or not members:
                raise ValueError(
                    "collaboration part groups require a non-empty id and parts"
                )
            if len(set(members)) != len(members):
                raise ValueError(
                    f"collaboration part group {group_id!r} repeats a part"
                )
            nested = sorted(set(members) & group_ids)
            if nested:
                raise ValueError(
                    f"collaboration part group {group_id!r} cannot nest "
                    f"part groups: {', '.join(nested)}"
                )
        for relation in settings.balance_relations:
            subject_parts = set(
                self._part_groups.get(
                    relation.subject,
                    (relation.subject,),
                )
            )
            reference_parts = set(
                self._part_groups.get(
                    relation.reference,
                    (relation.reference,),
                )
            )
            shared_parts = sorted(subject_parts & reference_parts)
            if relation.subject == relation.reference or shared_parts:
                raise ValueError(
                    "balance relation endpoints must expand to disjoint "
                    "parts"
                    + (
                        f"; shared parts: {', '.join(shared_parts)}"
                        if shared_parts
                        else ""
                    )
                )
        self._relation_parts = {
            part
            for relation in settings.balance_relations
            for endpoint in (relation.subject, relation.reference)
            for part in self._part_groups.get(endpoint, (endpoint,))
        }
        self._part_buffers: dict[str, np.ndarray] = {}
        self._endpoint_buffers: dict[str, np.ndarray] = {}
        self._endpoint_sha256: dict[str, str] = {}
        self._scratch_parent: Path | None = None
        self._scratch_identity: Any | None = None
        self._scratch_handles: list[BinaryIO] = []
        self._stem_transactions: set[_StemAnalysisTransaction] = set()
        self._closed = False
        self._abort_only = False
        if (
            expected_stem_count is not None
            and (
                isinstance(expected_stem_count, bool)
                or not isinstance(expected_stem_count, int)
                or expected_stem_count < 0
            )
        ):
            raise ValueError(
                "expected_stem_count must be a non-negative integer"
            )
        self._cache = (
            CollaborationAnalysisCache(cache_directory)
            if cache_directory is not None
            else None
        )
        self._cache_summary = (
            _analysis_cache_summary(
                stem_count=expected_stem_count,
                relation_count=len(settings.balance_relations),
            )
            if self._cache is not None
            else None
        )
        if (
            self._cache_summary is not None
            and not current_source_tree_matches()
        ):
            self._cache_summary["active"] = False
            self._cache_summary["reason_counts"][
                "session:producer_source_changed_restart_required"
            ] = 1
        if scratch_parent is not None:
            parent = Path(scratch_parent)
            parent.mkdir(parents=True, exist_ok=True)
            identity = capture_plain_directory(parent)
            self._scratch_parent = revalidate_plain_directory(identity)
            self._scratch_identity = identity

    @property
    def cache_summary(self) -> dict[str, Any] | None:
        """Return runtime-only cache telemetry for the enclosing renderer."""

        return self._cache_summary

    def _require_writable(self) -> None:
        if self._closed:
            raise RuntimeError("collaboration report builder is closed")
        if self._abort_only:
            raise RuntimeError(
                "collaboration report builder is abort-only after a failed "
                "stem transaction"
            )

    def _mark_abort_only(self) -> None:
        self._abort_only = True

    def _new_scratch_memmap(
        self,
        shape: tuple[int, ...],
    ) -> tuple[np.memmap, BinaryIO, os.stat_result]:
        """Allocate an unregistered mapping in the bound scratch directory."""

        identity = self._scratch_identity
        if self._scratch_parent is None or identity is None:
            raise RuntimeError("collaboration scratch storage is disabled")
        parent = revalidate_plain_directory(identity)
        scratch = tempfile.TemporaryFile(
            mode="w+b",
            dir=parent,
            prefix=".collaboration-analysis.",
            suffix=".f32",
        )
        audio: np.memmap | None = None
        try:
            revalidate_plain_directory(identity)
            audio = np.memmap(
                scratch,
                mode="w+",
                dtype=np.float32,
                shape=shape,
            )
            opened_status = os.fstat(scratch.fileno())
            if (
                not stat.S_ISREG(opened_status.st_mode)
                or opened_status.st_size != int(audio.nbytes)
            ):
                raise OSError(
                    "collaboration scratch mapping has an invalid identity"
                )
        except BaseException:
            if audio is not None:
                try:
                    _close_private_stem_scratch(
                        audio,
                        scratch,
                        flush=False,
                    )
                except BaseException:
                    pass
            else:
                try:
                    scratch.close()
                except BaseException:
                    pass
            raise
        return audio, scratch, opened_status

    def _scratch_memmap(self, shape: tuple[int, ...]) -> np.memmap:
        """Allocate a builder-owned delete-on-close mapping."""

        audio, scratch, _opened_status = self._new_scratch_memmap(shape)
        try:
            self._scratch_handles.append(scratch)
        except BaseException:
            try:
                _close_private_stem_scratch(audio, scratch, flush=False)
            except BaseException:
                pass
            raise
        return audio

    def _begin_stem_transaction(
        self,
        executor: Executor,
        *,
        frame_count: int,
        expected_audio_sha256: str | None = None,
    ) -> _StemAnalysisTransaction:
        """Begin one private bounded stem-analysis transaction."""

        self._require_writable()
        if (
            isinstance(frame_count, bool)
            or not isinstance(frame_count, int)
            or frame_count <= 0
        ):
            raise ValueError("stem analysis frame_count must be positive")
        if expected_audio_sha256 is not None and (
            not isinstance(expected_audio_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_audio_sha256) is None
        ):
            raise ValueError(
                "expected_audio_sha256 must be lowercase SHA-256 hex"
            )
        audio, scratch, opened_status = self._new_scratch_memmap(
            (frame_count, 2)
        )
        transaction = _StemAnalysisTransaction(
            self,
            executor,
            frame_count=frame_count,
            expected_audio_sha256=expected_audio_sha256,
            audio=audio,
            scratch=scratch,
            opened_status=opened_status,
        )
        try:
            self._stem_transactions.add(transaction)
        except BaseException:
            transaction._release_without_masking_error()
            raise
        return transaction

    def _cache_identity(
        self,
        *,
        kind: str,
        audio: dict[str, Any],
        relation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "format": "tianlai.collaboration_analysis_cache_identity",
            "version": ANALYSIS_CACHE_IDENTITY_VERSION,
            "kind": kind,
            "stage": ANALYSIS_CACHE_STAGE,
            "producer_source_tree_sha256": PROCESS_SOURCE_TREE_SHA256,
            "algorithms": {
                "mix_analysis_version": ANALYSIS_VERSION,
                "spectral_overlap_version": SPECTRAL_OVERLAP_VERSION,
                "temporal_balance_version": TEMPORAL_BALANCE_VERSION,
                "mix_report_version": MIX_REPORT_VERSION,
            },
            "sample_rate_hz": self.sample_rate,
            "analysis_config": self.config.to_dict(),
            "audio": audio,
        }
        if relation is not None:
            identity["relation"] = relation
        return identity

    def _cache_can_read(self) -> bool:
        if self._cache is None or self._cache_summary is None:
            return False
        if not self._cache_summary["active"]:
            return False
        if current_source_tree_matches():
            return True
        self._cache_summary["active"] = False
        reasons = self._cache_summary["reason_counts"]
        key = "session:producer_source_changed_restart_required"
        reasons[key] = int(reasons.get(key, 0)) + 1
        return False

    def add_stem(self, executor: Executor, buffer: np.ndarray) -> None:
        """Measure one post-gain dry stem without changing its samples."""

        self._add_stem(executor, buffer)

    def _add_stem(
        self,
        executor: Executor,
        buffer: np.ndarray,
        *,
        validated_audio_sha256: str | None = None,
        owned_scratch: tuple[np.memmap, BinaryIO] | None = None,
    ) -> bool:
        """Run the common diagnostic path and optionally adopt its mapping."""

        self._require_writable()
        transaction_call = owned_scratch is not None
        try:
            if owned_scratch is not None and owned_scratch[0] is not buffer:
                raise ValueError(
                    "owned analysis scratch does not match its buffer"
                )
            previous_relation_buffer = (
                self._part_buffers.get(executor.part_id)
                if executor.part_id in self._relation_parts
                else None
            )
            if (
                previous_relation_buffer is not None
                and previous_relation_buffer.shape != buffer.shape
            ):
                raise ValueError(
                    f"声部 {executor.part_id!r} "
                    "的套件执行器时间线长度不一致"
                )
            return self._add_stem_after_preflight(
                executor,
                buffer,
                validated_audio_sha256=validated_audio_sha256,
                owned_scratch=owned_scratch,
                previous_relation_buffer=previous_relation_buffer,
            )
        except BaseException:
            if transaction_call:
                self._mark_abort_only()
            raise

    def _add_stem_after_preflight(
        self,
        executor: Executor,
        buffer: np.ndarray,
        *,
        validated_audio_sha256: str | None,
        owned_scratch: tuple[np.memmap, BinaryIO] | None,
        previous_relation_buffer: np.ndarray | None,
    ) -> bool:
        """Mutate diagnostics only after transaction-wide preflight gates."""

        metrics: dict[str, Any]
        cache_identity: dict[str, Any] | None = None
        cache_key: str | None = None
        cache_hit = False
        if self._cache is not None and self._cache_summary is not None:
            if self._cache_can_read():
                try:
                    audio_sha256 = (
                        validated_audio_sha256
                        if validated_audio_sha256 is not None
                        else _audio_content_sha256(buffer)
                    )
                    cache_identity = self._cache_identity(
                        kind="stem_metrics",
                        audio={
                            "sha256": audio_sha256,
                            "frame_count": int(buffer.shape[0]),
                            "channels": 2,
                            "dtype": "<f4",
                        },
                    )
                    cache_key = build_cache_key(cache_identity)
                    lookup = self._cache.load(
                        cache_identity,
                        kind="stem_metrics",
                    )
                except Exception:
                    lookup = None
                live_source_matches = current_source_tree_matches()
                if (
                    lookup is not None
                    and lookup.hit
                    and lookup.payload is not None
                    and live_source_matches
                    and _valid_cached_stem_metrics(
                        lookup.payload,
                        sample_rate=self.sample_rate,
                        frame_count=int(buffer.shape[0]),
                        config=self.config,
                    )
                ):
                    metrics = lookup.payload
                    cache_hit = True
                    _note_analysis_cache(
                        self._cache_summary,
                        scope="stem",
                        status="hits",
                        reason="verified_hit",
                        key=cache_key,
                    )
                    window_frames, _hop_frames = (
                        self.config.frame_lengths(self.sample_rate)
                    )
                    self._cache_summary[
                        "avoided_fft_input_frame_visits"
                    ] += (
                        window_frames
                        * int(metrics["active_window_count"])
                    )
                else:
                    if not live_source_matches:
                        self._cache_summary["active"] = False
                        status = "bypassed"
                        reason = "producer_source_changed_during_lookup"
                    elif lookup is None:
                        status = "bypassed"
                        reason = "live_identity_unavailable"
                    elif lookup.status == "unavailable":
                        status = "bypassed"
                        reason = "lookup_unavailable"
                    elif (
                        lookup.status in ("corrupt", "incomplete")
                        or lookup.hit
                    ):
                        self._cache_summary["stem"][
                            "corrupt_fallbacks"
                        ] += 1
                        reason = (
                            "payload_mismatch"
                            if lookup.hit
                            else lookup.status
                        )
                        status = "misses"
                    elif lookup.status == "missing":
                        status = "misses"
                        reason = "not_found"
                    else:
                        status = "bypassed"
                        reason = f"lookup_{lookup.status}"
                    _note_analysis_cache(
                        self._cache_summary,
                        scope="stem",
                        status=status,
                        reason=reason,
                        key=cache_key,
                    )
            else:
                _note_analysis_cache(
                    self._cache_summary,
                    scope="stem",
                    status="bypassed",
                    reason="session_disabled",
                    key=None,
                )

        if not cache_hit:
            measured = analyze_track(
                buffer,
                self.sample_rate,
                self.config,
            )
            metrics = measured.to_dict()
            if self._cache_summary is not None:
                window_frames, _hop_frames = self.config.frame_lengths(
                    self.sample_rate
                )
                self._cache_summary[
                    "performed_fft_input_frame_visits"
                ] += window_frames * measured.active_window_count
            if (
                cache_identity is not None
                and self._cache is not None
                and self._cache_summary is not None
            ):
                if current_source_tree_matches():
                    stored = self._cache.store(
                        cache_identity,
                        kind="stem_metrics",
                        payload=metrics,
                    )
                    _note_analysis_cache_store(
                        self._cache_summary,
                        scope="stem",
                        status=stored.status,
                    )
                else:
                    _note_source_change_before_store(
                        self._cache_summary,
                        scope="stem",
                    )
        role = executor.role.to_dict() if executor.role is not None else None
        self._entries.append(
            _StemEntry(
                executor_id=executor.executor_id,
                part_id=executor.part_id,
                instrument=executor.capability.relative_path,
                gain_db=executor.gain_db,
                pan=executor.pan,
                role=role,
                metrics=metrics,
            )
        )
        if executor.part_id not in self._relation_parts:
            return False
        # Relation endpoints can be several minutes long.  The renderer
        # already supplies float32 stems, so retaining float64 duplicates
        # would double memory without adding source precision.
        if previous_relation_buffer is not None:
            previous_relation_buffer += np.asarray(buffer, dtype=np.float32)
            return False
        scratch: BinaryIO | None = None
        if owned_scratch is not None:
            audio, scratch = owned_scratch
        elif self._scratch_parent is None:
            audio = np.array(buffer, dtype=np.float32, copy=True)
        else:
            audio = self._scratch_memmap(buffer.shape)
            audio[:] = buffer
        # Relation endpoints are normally a single executor.  Retain only
        # these explicitly referenced parts; unrelated stems do not
        # accumulate in memory or scratch storage.
        try:
            self._part_buffers[executor.part_id] = audio
            if scratch is not None:
                self._scratch_handles.append(scratch)
        except BaseException:
            try:
                if self._part_buffers.get(executor.part_id) is audio:
                    self._part_buffers.pop(executor.part_id, None)
                if scratch is not None:
                    self._scratch_handles[:] = [
                        candidate
                        for candidate in self._scratch_handles
                        if candidate is not scratch
                    ]
            except BaseException:
                pass
            raise
        return owned_scratch is not None

    def _endpoint_document(self, endpoint: str) -> dict[str, Any]:
        expanded = self._part_groups.get(endpoint)
        return {
            "endpoint_kind": (
                "part_group" if expanded is not None else "part"
            ),
            "expanded_parts": list(
                expanded if expanded is not None else (endpoint,)
            ),
        }

    def _endpoint_buffer(self, endpoint: str) -> np.ndarray:
        """Return one analysis endpoint, summing declared member parts only."""

        cached = self._endpoint_buffers.get(endpoint)
        if cached is not None:
            return cached
        expanded = self._part_groups.get(endpoint)
        if expanded is None:
            audio = self._part_buffers.get(endpoint)
            if audio is None:
                raise ValueError(
                    "balance relation references a part with no rendered "
                    f"stem: {endpoint!r}"
                )
            self._endpoint_buffers[endpoint] = audio
            return audio

        first = self._part_buffers.get(expanded[0])
        if first is None:
            raise ValueError(
                f"part group {endpoint!r} references a part with no rendered "
                f"stem: {expanded[0]!r}"
            )
        if self._scratch_parent is None:
            combined = np.array(first, dtype=np.float32, copy=True)
        else:
            combined = self._scratch_memmap(first.shape)
            combined[:] = first
        for part_id in expanded[1:]:
            audio = self._part_buffers.get(part_id)
            if audio is None:
                raise ValueError(
                    f"part group {endpoint!r} references a part with no "
                    f"rendered stem: {part_id!r}"
                )
            if audio.shape != combined.shape:
                raise ValueError(
                    f"part group {endpoint!r} member timelines have "
                    "different lengths"
                )
            combined += np.asarray(audio, dtype=np.float32)
        self._endpoint_buffers[endpoint] = combined
        return combined

    def _endpoint_content_sha256(self, endpoint: str) -> str:
        cached = self._endpoint_sha256.get(endpoint)
        if cached is not None:
            return cached
        digest = _audio_content_sha256(self._endpoint_buffer(endpoint))
        self._endpoint_sha256[endpoint] = digest
        return digest

    def _build_report(self) -> dict[str, Any]:
        relations: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        within_tolerance = 0
        outside_tolerance = 0
        insufficient_overlap = 0
        spectral_overlap_candidates = 0
        temporal_balance_drift_candidates = 0
        automation_draft_relation_count = 0
        automation_draft_segment_count = 0
        relation_shared_active_window_count = 0
        rendered_parts = {entry.part_id for entry in self._entries}
        conflicts = sorted(rendered_parts & set(self._part_groups))
        if conflicts:
            raise ValueError(
                "part group id conflicts with an assigned part: "
                + ", ".join(conflicts)
            )
        block_activity_by_endpoint: dict[str, _BlockActivity] = {}

        for relation in self.settings.balance_relations:
            subject = self._endpoint_buffer(relation.subject)
            reference = self._endpoint_buffer(relation.reference)
            relation_payload: dict[str, Any] | None = None
            cache_identity: dict[str, Any] | None = None
            cache_key: str | None = None
            cache_hit = False
            if self._cache is not None and self._cache_summary is not None:
                if self._cache_can_read():
                    try:
                        cache_identity = self._cache_identity(
                            kind="balance_relation",
                            audio={
                                "subject_sha256": (
                                    self._endpoint_content_sha256(
                                        relation.subject
                                    )
                                ),
                                "reference_sha256": (
                                    self._endpoint_content_sha256(
                                        relation.reference
                                    )
                                ),
                                "frame_count": int(subject.shape[0]),
                                "channels": 2,
                                "dtype": "<f4",
                            },
                            relation={
                                "declaration": relation.to_dict(),
                                "subject_endpoint": (
                                    self._endpoint_document(
                                        relation.subject
                                    )
                                ),
                                "reference_endpoint": (
                                    self._endpoint_document(
                                        relation.reference
                                    )
                                ),
                                "minimum_shared_window_count": (
                                    self.minimum_shared_window_count
                                ),
                            },
                        )
                        cache_key = build_cache_key(cache_identity)
                        lookup = self._cache.load(
                            cache_identity,
                            kind="balance_relation",
                        )
                    except Exception:
                        lookup = None
                    live_source_matches = current_source_tree_matches()
                    if (
                        lookup is not None
                        and lookup.hit
                        and lookup.payload is not None
                        and live_source_matches
                        and _valid_cached_relation_payload(
                            lookup.payload,
                            sample_rate=self.sample_rate,
                            frame_count=int(subject.shape[0]),
                            config=self.config,
                            target_offset_db=relation.target_offset_db,
                            tolerance_db=relation.tolerance_db,
                            minimum_shared_window_count=(
                                self.minimum_shared_window_count
                            ),
                        )
                    ):
                        relation_payload = lookup.payload
                        cache_hit = True
                        _note_analysis_cache(
                            self._cache_summary,
                            scope="relation",
                            status="hits",
                            reason="verified_hit",
                            key=cache_key,
                        )
                    else:
                        if not live_source_matches:
                            self._cache_summary["active"] = False
                            status = "bypassed"
                            reason = (
                                "producer_source_changed_during_lookup"
                            )
                        elif lookup is None:
                            status = "bypassed"
                            reason = "live_identity_unavailable"
                        elif lookup.status == "unavailable":
                            status = "bypassed"
                            reason = "lookup_unavailable"
                        elif (
                            lookup.status in ("corrupt", "incomplete")
                            or lookup.hit
                        ):
                            self._cache_summary["relation"][
                                "corrupt_fallbacks"
                            ] += 1
                            reason = (
                                "payload_mismatch"
                                if lookup.hit
                                else lookup.status
                            )
                            status = "misses"
                        elif lookup.status == "missing":
                            status = "misses"
                            reason = "not_found"
                        else:
                            status = "bypassed"
                            reason = f"lookup_{lookup.status}"
                        _note_analysis_cache(
                            self._cache_summary,
                            scope="relation",
                            status=status,
                            reason=reason,
                            key=cache_key,
                        )
                else:
                    _note_analysis_cache(
                        self._cache_summary,
                        scope="relation",
                        status="bypassed",
                        reason="session_disabled",
                        key=None,
                    )

            if not cache_hit:
                comparison = overlap_active_rms_difference(
                    subject,
                    reference,
                    self.sample_rate,
                    self.config,
                )
                for endpoint, audio in (
                    (relation.subject, subject),
                    (relation.reference, reference),
                ):
                    if endpoint not in block_activity_by_endpoint:
                        block_activity_by_endpoint[endpoint] = (
                            _block_activity(
                                audio,
                                self.sample_rate,
                                self.config,
                            )
                        )
                overlap_evidence = _shared_block_evidence(
                    block_activity_by_endpoint[relation.subject],
                    block_activity_by_endpoint[relation.reference],
                    self.sample_rate,
                    comparison.shared_active_window_count,
                )
                enough_overlap = (
                    overlap_evidence["status"] == "sufficient"
                )
                measurement = comparison.to_dict()
                if not enough_overlap:
                    # Preserve observed counts but suppress a fragile level
                    # estimate based on only momentary overlap.
                    measurement["first_active_rms_dbfs"] = None
                    measurement["second_active_rms_dbfs"] = None
                    measurement["first_minus_second_db"] = None
                    spectral_document = _inconclusive_spectral_document(
                        comparison
                    )
                    temporal_document = (
                        _inconclusive_temporal_document(
                            comparison,
                            target_offset_db=(
                                relation.target_offset_db
                            ),
                            tolerance_db=relation.tolerance_db,
                            minimum_shared_window_count=(
                                self.minimum_shared_window_count
                            ),
                        )
                    )
                    relation_fft_windows = 0
                else:
                    spectral = analyze_spectral_overlap(
                        subject,
                        reference,
                        self.sample_rate,
                        self.config,
                    )
                    spectral_document = spectral.to_dict()
                    relation_fft_windows = (
                        spectral.shared_active_window_count
                    )
                    temporal = analyze_temporal_balance(
                        subject,
                        reference,
                        self.sample_rate,
                        self.config,
                        target_offset_db=relation.target_offset_db,
                        tolerance_db=relation.tolerance_db,
                        minimum_shared_window_count=(
                            self.minimum_shared_window_count
                        ),
                    )
                    temporal_document = temporal.to_dict()
                relation_payload = {
                    "measurement": measurement,
                    "overlap_evidence": overlap_evidence,
                    "spectral_overlap": spectral_document,
                    "temporal_balance": temporal_document,
                    "relation_shared_active_window_count": (
                        relation_fft_windows
                    ),
                }
                if (
                    cache_identity is not None
                    and self._cache is not None
                    and self._cache_summary is not None
                ):
                    if current_source_tree_matches():
                        stored = self._cache.store(
                            cache_identity,
                            kind="balance_relation",
                            payload=relation_payload,
                        )
                        _note_analysis_cache_store(
                            self._cache_summary,
                            scope="relation",
                            status=stored.status,
                        )
                    else:
                        _note_source_change_before_store(
                            self._cache_summary,
                            scope="relation",
                        )

            if relation_payload is None:
                raise RuntimeError(
                    "collaboration relation analysis produced no payload"
                )
            measurement = relation_payload["measurement"]
            overlap_evidence = relation_payload["overlap_evidence"]
            spectral_document = relation_payload["spectral_overlap"]
            temporal_document = relation_payload["temporal_balance"]
            relation_fft_windows = int(
                relation_payload[
                    "relation_shared_active_window_count"
                ]
            )
            relation_shared_active_window_count += relation_fft_windows
            if self._cache_summary is not None:
                window_frames, _hop_frames = self.config.frame_lengths(
                    self.sample_rate
                )
                field = (
                    "avoided_fft_input_frame_visits"
                    if cache_hit
                    else "performed_fft_input_frame_visits"
                )
                self._cache_summary[field] += (
                    2 * window_frames * relation_fft_windows
                )
            measured = measurement["first_minus_second_db"]
            row: dict[str, Any] = {
                **relation.to_dict(),
                "subject_endpoint": self._endpoint_document(
                    relation.subject
                ),
                "reference_endpoint": self._endpoint_document(
                    relation.reference
                ),
                "measurement": measurement,
                "overlap_evidence": overlap_evidence,
                "spectral_overlap": spectral_document,
                "temporal_balance": temporal_document,
            }
            if measured is None:
                status = "insufficient_overlap"
                row["status"] = status
                row["deviation_db"] = None
                row["spectral_screening"] = {
                    "status": "insufficient_overlap",
                    "candidate_bands": [],
                }
                insufficient_overlap += 1
                warnings.append(
                    {
                        "code": "balance_relation_insufficient_overlap",
                        "subject": relation.subject,
                        "reference": relation.reference,
                        "message": (
                            f"{relation.subject} 与 {relation.reference} "
                            "没有达到共同活动 block 覆盖与主分析窗双门槛，"
                            "无法判断相对电平"
                        ),
                    }
                )
                relations.append(row)
                continue

            deviation = _round_optional(
                measured - relation.target_offset_db
            )
            if deviation is None:
                raise RuntimeError("relation deviation did not produce a number")
            row["measured_offset_db"] = _round_optional(measured)
            row["deviation_db"] = deviation
            if abs(deviation) <= relation.tolerance_db:
                row["status"] = "within_tolerance"
                within_tolerance += 1
            else:
                row["status"] = "outside_tolerance"
                outside_tolerance += 1
                warnings.append(
                    {
                        "code": "balance_relation_outside_tolerance",
                        "subject": relation.subject,
                        "reference": relation.reference,
                        "measured_offset_db": _round_optional(measured),
                        "target_offset_db": relation.target_offset_db,
                        "deviation_db": _round_optional(deviation),
                        "message": (
                            f"{relation.subject} 相对 {relation.reference} "
                            f"实测 {measured:+.2f} dB，目标 "
                            f"{relation.target_offset_db:+.2f}±"
                            f"{relation.tolerance_db:.2f} dB"
                        ),
                    }
                )
            if self.settings.mode == "suggest":
                adjustment = _bounded_adjustment(
                    target_offset_db=relation.target_offset_db,
                    measured_offset_db=measured,
                    maximum_absolute_adjustment_db=(
                        relation.max_suggestion_db
                    ),
                )
                if abs(deviation) <= relation.tolerance_db:
                    adjustment = 0.0
                row["suggested_subject_gain_adjustment_db"] = adjustment
                draft = _gain_automation_draft(
                    subject=relation.subject,
                    reference=relation.reference,
                    subject_endpoint=row["subject_endpoint"],
                    reference_endpoint=row["reference_endpoint"],
                    target_offset_db=relation.target_offset_db,
                    maximum_absolute_adjustment_db=(
                        relation.max_suggestion_db
                    ),
                    candidate_segments=temporal_document[
                        "candidate_segments"
                    ],
                    source_candidate_segment_count=temporal_document[
                        "candidate_segment_count"
                    ],
                    segments_truncated=temporal_document[
                        "candidate_segments_truncated"
                    ],
                )
                row["gain_automation_draft"] = draft
                automation_draft_relation_count += 1
                automation_draft_segment_count += len(draft["segments"])

            first_ratios = spectral_document[
                "first_band_energy_ratios"
            ]
            second_ratios = spectral_document[
                "second_band_energy_ratios"
            ]
            band_differences = spectral_document[
                "band_first_minus_second_db"
            ]
            candidate_bands = [
                name
                for name in band_differences
                if (
                    band_differences[name] is not None
                    and first_ratios[name] is not None
                    and second_ratios[name] is not None
                    and float(first_ratios[name])
                    >= _BAND_ENERGY_RELEVANCE_THRESHOLD
                    and float(second_ratios[name])
                    >= _BAND_ENERGY_RELEVANCE_THRESHOLD
                    and float(band_differences[name])
                    > relation.target_offset_db + relation.tolerance_db
                )
            ]
            overlap_coefficient = spectral_document[
                "spectral_overlap_coefficient"
            ]
            overall_candidate = (
                overlap_coefficient is not None
                and overlap_coefficient
                >= _SPECTRAL_OVERLAP_CANDIDATE_THRESHOLD
                and deviation > relation.tolerance_db
            )
            is_spectral_candidate = bool(candidate_bands) or overall_candidate
            row["spectral_screening"] = {
                "status": (
                    "candidate" if is_spectral_candidate else "clear"
                ),
                "candidate_bands": candidate_bands,
            }
            if is_spectral_candidate:
                spectral_overlap_candidates += 1
                warnings.append(
                    {
                        "code": "spectral_overlap_candidate",
                        "subject": relation.subject,
                        "reference": relation.reference,
                        "spectral_overlap_coefficient": _round_optional(
                            overlap_coefficient
                        ),
                        "candidate_bands": candidate_bands,
                        "message": (
                            f"{relation.subject} 与 {relation.reference} "
                            "存在频带重叠候选；这只是工程排查指标，不是"
                            "心理声学掩蔽结论"
                        ),
                    }
                )
            allowed_robust_span = round(
                2.0 * relation.tolerance_db,
                6,
            )
            temporal_robust_span = temporal_document["robust_span_db"]
            is_temporal_drift_candidate = (
                temporal_robust_span is not None
                and float(temporal_robust_span) > allowed_robust_span
            )
            if is_temporal_drift_candidate:
                temporal_balance_drift_candidates += 1
                warnings.append(
                    {
                        "code": "temporal_balance_drift_candidate",
                        "subject": relation.subject,
                        "reference": relation.reference,
                        "p10_db": temporal_document["p10_db"],
                        "p90_db": temporal_document["p90_db"],
                        "robust_span_db": temporal_robust_span,
                        "allowed_robust_span_db": round(
                            allowed_robust_span,
                            6,
                        ),
                        "message": (
                            f"{relation.subject} 相对 {relation.reference} "
                            "的中央 80% 窗口跨度超过目标容差带宽；单一静态"
                            "增益无法同时修正这些段落"
                        ),
                    }
                )
            relations.append(row)

        window_frames, _hop_frames = self.config.frame_lengths(
            self.sample_rate
        )
        stem_window_count = sum(
            int(entry.metrics["window_count"]) for entry in self._entries
        )
        stem_active_fft_window_count = sum(
            int(entry.metrics["active_window_count"])
            for entry in self._entries
        )
        relation_pair_fft_window_count = (
            2 * relation_shared_active_window_count
        )
        retained_buffers: dict[int, np.ndarray] = {
            id(audio): audio
            for audio in (
                *self._part_buffers.values(),
                *self._endpoint_buffers.values(),
            )
        }
        relation_buffer_bytes = sum(
            int(audio.nbytes) for audio in retained_buffers.values()
        )
        if self._cache_summary is not None:
            _finalize_analysis_cache_summary(
                self._cache_summary,
                stem_count=len(self._entries),
            )

        return {
            "format": MIX_REPORT_FORMAT,
            "version": MIX_REPORT_VERSION,
            "scope": "machine_triage_only",
            "relation_audio_stage": (
                "post_assignment_gain_pre_pan_pre_space_pre_master_pre_normalize"
            ),
            "relation_sample_stage": "float32_pre_pcm24",
            "stage_metrics_sample_stage": "float64_pre_pcm24",
            "mode": self.settings.mode,
            "audio_modified": False,
            "analysis": {
                **self.settings.analysis.to_dict(),
                "relative_gate_db": _RELATIVE_GATE_DB,
                "minimum_overlap_seconds": _MINIMUM_OVERLAP_SECONDS,
                "minimum_shared_window_count": (
                    self.minimum_shared_window_count
                ),
                "spectral_screening": {
                    "overlap_coefficient_min": (
                        _SPECTRAL_OVERLAP_CANDIDATE_THRESHOLD
                    ),
                    "minimum_band_energy_ratio": (
                        _BAND_ENERGY_RELEVANCE_THRESHOLD
                    ),
                },
                "temporal_screening": {
                    "criterion": (
                        "robust_span_db_greater_than_2x_relation_tolerance"
                    ),
                },
                "relation_buffer_storage": (
                    "scratch_float32_memmap"
                    if self._scratch_parent is not None
                    else "memory_float32"
                ),
                "workload": {
                    "executor_count": len(self._entries),
                    "relation_count": len(relations),
                    "unique_relation_part_count": len(
                        self._part_buffers
                    ),
                    "window_frames": window_frames,
                    "stem_window_count": stem_window_count,
                    "stem_active_fft_window_count": (
                        stem_active_fft_window_count
                    ),
                    "relation_shared_active_window_count": (
                        relation_shared_active_window_count
                    ),
                    "relation_pair_fft_window_count": (
                        relation_pair_fft_window_count
                    ),
                    "fft_input_frame_visits": (
                        window_frames
                        * (
                            stem_active_fft_window_count
                            + relation_pair_fft_window_count
                        )
                    ),
                    "relation_buffer_bytes": relation_buffer_bytes,
                },
            },
            "stems": [entry.to_dict() for entry in self._entries],
            "balance_relations": relations,
            "summary": {
                "stem_count": len(self._entries),
                "relation_count": len(relations),
                "within_tolerance": within_tolerance,
                "outside_tolerance": outside_tolerance,
                "insufficient_overlap": insufficient_overlap,
                "spectral_overlap_candidates": (
                    spectral_overlap_candidates
                ),
                "temporal_balance_drift_candidates": (
                    temporal_balance_drift_candidates
                ),
                "automation_draft_relation_count": (
                    automation_draft_relation_count
                ),
                "automation_draft_segment_count": (
                    automation_draft_segment_count
                ),
                "warning_count": len(warnings),
            },
            "warnings": warnings,
            "notice": (
                "机器报告只用于协奏排查；它不修改音频，也不能自动把乐器标记为"
                "协奏验收通过。"
            ),
        }

    def close(self) -> None:
        """Close mappings before their delete-on-close scratch handles."""

        if self._closed:
            return
        self._closed = True
        cleanup_errors: list[BaseException] = []
        transactions = tuple(self._stem_transactions)
        for transaction in transactions:
            try:
                transaction.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        self._stem_transactions.clear()
        buffers_by_identity = {
            id(audio): audio
            for audio in (
                *self._part_buffers.values(),
                *self._endpoint_buffers.values(),
            )
        }
        buffers = tuple(buffers_by_identity.values())
        self._part_buffers.clear()
        self._endpoint_buffers.clear()
        self._endpoint_sha256.clear()
        scratch_handles = tuple(self._scratch_handles)
        self._scratch_handles.clear()
        self._scratch_parent = None
        self._scratch_identity = None
        for audio in buffers:
            if not isinstance(audio, np.memmap):
                continue
            try:
                audio.flush()
            except BaseException as exc:
                cleanup_errors.append(exc)
            mapping = getattr(audio, "_mmap", None)
            if mapping is not None:
                try:
                    mapping.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        for scratch in scratch_handles:
            try:
                scratch.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

    def build(self) -> dict[str, Any]:
        """Build the immutable report, then release potentially large buffers."""

        if self._closed:
            raise RuntimeError("collaboration report builder is closed")
        try:
            self._require_writable()
            if self._stem_transactions:
                raise RuntimeError(
                    "collaboration report has an unfinished stem transaction"
                )
            report = self._build_report()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise
        self.close()
        return report

    def __enter__(self) -> "CollaborationReportBuilder":
        return self

    def __exit__(self, exc_type: Any, _value: Any, _traceback: Any) -> None:
        if exc_type is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException:
                pass


__all__ = (
    "CollaborationReportBuilder",
    "GAIN_AUTOMATION_DRAFT_FORMAT",
    "GAIN_AUTOMATION_DRAFT_VERSION",
    "MIX_REPORT_FORMAT",
    "MIX_REPORT_NAME",
    "MIX_REPORT_VERSION",
    "TAIL_ANALYSIS_SECONDS",
    "attach_stage_diagnostics",
)
