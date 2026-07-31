"""Read-only orchestration topology checks for collaboration reports.

This module deliberately inspects the *written and scheduled performance*,
not the rendered waveform.  Its first narrow job is to find the risky case
where two stems use the same curated source and reproduce a large amount of
the same pitch at the same score position with only a tiny scheduling offset.
That pattern can create short-delay comb filtering even when the final stereo
bus passes a coarse mono-fold check.

The result is advisory.  It never changes notes, timing, variants, pan, gain,
or audio, and octave doubling is reported as context rather than treated as a
phase fault.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from statistics import median
from typing import Any

from .score import parse_pitch


TOPOLOGY_FORMAT = "tianlai.orchestration_topology"
TOPOLOGY_VERSION = 1
MINIMUM_UNISON_EVENT_COUNT = 8
MINIMUM_SHORTER_PART_COVERAGE = 0.5
NEAR_SIMULTANEOUS_SECONDS = 0.015
# Scheduled trace times are serialized to microsecond precision.  Treat only
# that quantization cell as exact; even a 0.1 ms physical offset can already
# comb-filter high frequencies and must remain in the short-delay class.
MINIMUM_PHASE_DELAY_SECONDS = 0.000001
MINIMUM_NEAR_SIMULTANEOUS_RATIO = 0.8
MAX_REPORTED_PAIRS = 128
MAX_REPORTED_WARNINGS = 128


def _round(value: float, digits: int = 6) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def _variant(executor: Any) -> str | None:
    override_map = getattr(executor, "override_map", {})
    value = override_map.get("sample_variant")
    return str(value) if value is not None else None


def _source_identity(executor: Any) -> tuple[str, str | None]:
    capability = executor.capability
    return str(capability.relative_path), _variant(executor)


def _score_position(trace: dict[str, Any]) -> tuple[int, float]:
    return int(trace["小节"]), _round(float(trace["拍"]), 6)


def _pitch(trace: dict[str, Any]) -> float:
    return _round(parse_pitch(trace["音"]), 6)


def _events(part: Any) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for trace in getattr(part, "trace", ()):
        time_seconds = float(trace["时间"])
        if not math.isfinite(time_seconds) or time_seconds < 0.0:
            raise ValueError("orchestration topology requires finite note times")
        events.append(
            {
                "position": _score_position(trace),
                "pitch": _pitch(trace),
                "time_seconds": time_seconds,
            }
        )
    return tuple(events)


@dataclass(frozen=True, slots=True)
class _PartEvidence:
    part: Any
    events: tuple[dict[str, Any], ...]
    by_onset_pitch: dict[tuple[int, float, float], tuple[float, ...]]
    by_position: dict[tuple[int, float], tuple[float, ...]]


def _part_evidence(part: Any) -> _PartEvidence:
    events = _events(part)
    onset_pitch: dict[
        tuple[int, float, float], list[float]
    ] = defaultdict(list)
    position: dict[tuple[int, float], list[float]] = defaultdict(list)
    for event in events:
        onset_pitch[(*event["position"], event["pitch"])].append(
            event["time_seconds"]
        )
        position[event["position"]].append(event["pitch"])
    return _PartEvidence(
        part=part,
        events=events,
        by_onset_pitch={
            key: tuple(sorted(values))
            for key, values in onset_pitch.items()
        },
        by_position={
            key: tuple(sorted(values))
            for key, values in position.items()
        },
    )


def _same_pitch_matches(
    first_index: dict[tuple[int, float, float], tuple[float, ...]],
    second_index: dict[tuple[int, float, float], tuple[float, ...]],
) -> tuple[int, tuple[float, ...]]:
    deltas: list[float] = []
    for key in sorted(first_index.keys() & second_index.keys()):
        left = first_index[key]
        right = second_index[key]
        deltas.extend(
            abs(a - b) for a, b in zip(left, right, strict=False)
        )
    return len(deltas), tuple(deltas)


def _is_octave_apart(first: float, second: float) -> bool:
    distance = abs(first - second)
    if distance < 11.999999:
        return False
    octaves = distance / 12.0
    return math.isclose(octaves, round(octaves), abs_tol=1e-6)


def _octave_match_count(
    first_by_position: dict[tuple[int, float], tuple[float, ...]],
    second_by_position: dict[tuple[int, float], tuple[float, ...]],
) -> int:
    total = 0
    for position in sorted(
        first_by_position.keys() & second_by_position.keys()
    ):
        available = sorted(second_by_position[position])
        for pitch in sorted(first_by_position[position]):
            candidates = [
                (abs(pitch - other), index)
                for index, other in enumerate(available)
                if _is_octave_apart(pitch, other)
            ]
            if not candidates:
                continue
            _distance, index = min(candidates)
            available.pop(index)
            total += 1
    return total


def _executor_document(executor: Any) -> dict[str, Any]:
    instrument, variant = _source_identity(executor)
    return {
        "executor_id": str(executor.executor_id),
        "part_id": str(executor.part_id),
        "instrument": instrument,
        "sample_variant": variant,
    }


def _pair_document(
    first: _PartEvidence,
    second: _PartEvidence,
) -> dict[str, Any] | None:
    same_count, deltas = _same_pitch_matches(
        first.by_onset_pitch,
        second.by_onset_pitch,
    )
    octave_count = _octave_match_count(
        first.by_position,
        second.by_position,
    )
    if same_count == 0 and octave_count == 0:
        return None

    shorter_count = min(len(first.events), len(second.events))
    coverage = same_count / shorter_count if shorter_count else 0.0
    exact_count = sum(
        delta <= MINIMUM_PHASE_DELAY_SECONDS + 1e-12
        for delta in deltas
    )
    short_delay_count = sum(
        MINIMUM_PHASE_DELAY_SECONDS < delta
        <= NEAR_SIMULTANEOUS_SECONDS + 1e-12
        for delta in deltas
    )
    near_count = sum(
        delta <= NEAR_SIMULTANEOUS_SECONDS + 1e-12
        for delta in deltas
    )
    near_ratio = near_count / same_count if same_count else 0.0
    exact_ratio = exact_count / same_count if same_count else 0.0
    short_delay_ratio = (
        short_delay_count / same_count if same_count else 0.0
    )
    first_part = first.part
    second_part = second.part
    source_match = _source_identity(
        first_part.executor
    ) == _source_identity(
        second_part.executor
    )
    common_candidate_gate = (
        source_match
        and same_count >= MINIMUM_UNISON_EVENT_COUNT
        and coverage >= MINIMUM_SHORTER_PART_COVERAGE
    )
    phase_candidate = (
        common_candidate_gate
        and short_delay_ratio >= MINIMUM_NEAR_SIMULTANEOUS_RATIO
    )
    exact_stack_candidate = (
        common_candidate_gate
        and exact_ratio >= MINIMUM_NEAR_SIMULTANEOUS_RATIO
    )
    if phase_candidate:
        status = "same_source_unison_phase_candidate"
    elif exact_stack_candidate:
        status = "same_source_exact_unison_level_stack_candidate"
    else:
        status = "context_only"
    return {
        "first": _executor_document(first_part.executor),
        "second": _executor_document(second_part.executor),
        "same_source": source_match,
        "first_note_count": len(first.events),
        "second_note_count": len(second.events),
        "same_pitch_same_score_position_count": same_count,
        "same_pitch_coverage_of_shorter_part": _round(coverage),
        "octave_same_score_position_count": octave_count,
        "exact_simultaneous_same_pitch_count": exact_count,
        "exact_simultaneous_same_pitch_ratio": _round(exact_ratio),
        "short_delay_same_pitch_count": short_delay_count,
        "short_delay_same_pitch_ratio": _round(short_delay_ratio),
        "near_simultaneous_same_pitch_count": near_count,
        "near_simultaneous_same_pitch_ratio": _round(near_ratio),
        "median_scheduled_start_delta_ms": (
            _round(median(deltas) * 1000.0)
            if deltas
            else None
        ),
        "maximum_scheduled_start_delta_ms": (
            _round(max(deltas) * 1000.0)
            if deltas
            else None
        ),
        "status": status,
    }


def analyze_orchestration_topology(plan: Any) -> dict[str, Any]:
    """Return deterministic, bounded orchestration topology diagnostics."""

    evidence = tuple(
        _part_evidence(part)
        for part in sorted(
            tuple(plan.parts),
            key=lambda part: str(part.executor.executor_id),
        )
        if getattr(part, "trace", ())
    )
    all_rows: list[dict[str, Any]] = []
    total_pair_count = 0
    same_pitch_total = 0
    octave_total = 0
    candidate_count = 0
    exact_stack_candidate_count = 0
    all_warnings: list[dict[str, Any]] = []

    for first_index, first in enumerate(evidence):
        for second in evidence[first_index + 1 :]:
            row = _pair_document(first, second)
            if row is None:
                continue
            total_pair_count += 1
            same_pitch_total += int(
                row["same_pitch_same_score_position_count"]
            )
            octave_total += int(row["octave_same_score_position_count"])
            if row["status"] == "same_source_unison_phase_candidate":
                candidate_count += 1
                warning = (
                    {
                        "code": "same_source_unison_phase_candidate",
                        "first_executor_id": row["first"]["executor_id"],
                        "second_executor_id": row["second"]["executor_id"],
                        "instrument": row["first"]["instrument"],
                        "same_pitch_same_score_position_count": row[
                            "same_pitch_same_score_position_count"
                        ],
                        "same_pitch_coverage_of_shorter_part": row[
                            "same_pitch_coverage_of_shorter_part"
                        ],
                        "short_delay_same_pitch_ratio": row[
                            "short_delay_same_pitch_ratio"
                        ],
                        "near_simultaneous_same_pitch_ratio": row[
                            "near_simultaneous_same_pitch_ratio"
                        ],
                        "median_scheduled_start_delta_ms": row[
                            "median_scheduled_start_delta_ms"
                        ],
                        "message": (
                            "两个执行器大面积使用同一音源配置候选，以非零"
                            "短延迟齐奏同音，存在梳状染色候选；请由创作者"
                            "检查分谱或显式选择独立音源变体，系统没有改动"
                            "谱面、时序或音频"
                        ),
                    }
                )
                all_warnings.append(warning)
            elif (
                row["status"]
                == "same_source_exact_unison_level_stack_candidate"
            ):
                exact_stack_candidate_count += 1
                warning = {
                    "code": (
                        "same_source_exact_unison_level_stack_candidate"
                    ),
                    "first_executor_id": row["first"]["executor_id"],
                    "second_executor_id": row["second"]["executor_id"],
                    "instrument": row["first"]["instrument"],
                    "same_pitch_same_score_position_count": row[
                        "same_pitch_same_score_position_count"
                    ],
                    "same_pitch_coverage_of_shorter_part": row[
                        "same_pitch_coverage_of_shorter_part"
                    ],
                    "exact_simultaneous_same_pitch_ratio": row[
                        "exact_simultaneous_same_pitch_ratio"
                    ],
                    "message": (
                        "两个执行器大面积使用同一音源配置候选精确齐奏同音；"
                        "这不是短延迟梳状相位候选，但可能造成重复声部与电平"
                        "叠加，请由创作者检查，系统没有改动谱面或音频"
                    ),
                }
                all_warnings.append(warning)
            all_rows.append(row)

    severity = {
        "same_source_unison_phase_candidate": 0,
        "same_source_exact_unison_level_stack_candidate": 1,
        "context_only": 2,
    }
    rows = sorted(
        all_rows,
        key=lambda row: (
            severity[row["status"]],
            row["first"]["executor_id"],
            row["second"]["executor_id"],
        ),
    )[:MAX_REPORTED_PAIRS]
    warning_severity = {
        "same_source_unison_phase_candidate": 0,
        "same_source_exact_unison_level_stack_candidate": 1,
    }
    warnings = sorted(
        all_warnings,
        key=lambda warning: (
            warning_severity[warning["code"]],
            warning["first_executor_id"],
            warning["second_executor_id"],
        ),
    )[:MAX_REPORTED_WARNINGS]

    return {
        "format": TOPOLOGY_FORMAT,
        "version": TOPOLOGY_VERSION,
        "scope": "machine_triage_only",
        "audio_modified": False,
        "analysis": {
            "same_score_position_resolution": "bar_beat_and_sounding_pitch",
            "same_source_identity": (
                "instrument_path_and_explicit_sample_variant"
            ),
            "minimum_unison_event_count": MINIMUM_UNISON_EVENT_COUNT,
            "minimum_shorter_part_coverage": (
                MINIMUM_SHORTER_PART_COVERAGE
            ),
            "near_simultaneous_ms": _round(
                NEAR_SIMULTANEOUS_SECONDS * 1000.0
            ),
            "minimum_nonzero_phase_delay_ms": _round(
                MINIMUM_PHASE_DELAY_SECONDS * 1000.0
            ),
            "minimum_near_simultaneous_ratio": (
                MINIMUM_NEAR_SIMULTANEOUS_RATIO
            ),
            "octave_doubling_is_phase_candidate": False,
            "maximum_reported_pairs": MAX_REPORTED_PAIRS,
            "maximum_reported_warnings": MAX_REPORTED_WARNINGS,
            "truncation_priority": (
                "phase_candidate_then_exact_stack_then_context"
            ),
        },
        "pairs": rows,
        "summary": {
            "pair_count": total_pair_count,
            "reported_pair_count": len(rows),
            "pairs_truncated": total_pair_count > len(rows),
            "same_pitch_same_score_position_count": same_pitch_total,
            "octave_same_score_position_count": octave_total,
            "same_source_unison_phase_candidate_count": candidate_count,
            "same_source_exact_unison_level_stack_candidate_count": (
                exact_stack_candidate_count
            ),
            "reported_warning_count": len(warnings),
            "warnings_truncated": (
                candidate_count + exact_stack_candidate_count
                > len(warnings)
            ),
        },
        "warnings": warnings,
        "notice": (
            "编配拓扑只做机器筛查；异源同音和八度叠奏不会被当成相位"
            "故障，候选也不会触发自动换源、微移、失谐、删音或改音频。"
        ),
    }


def attach_orchestration_topology(
    report: dict[str, Any],
    topology: dict[str, Any],
) -> None:
    """Attach topology evidence and mirror actionable warnings at report top."""

    report["orchestration_topology"] = topology
    warnings = report["warnings"]
    warnings.extend(topology["warnings"])
    report["summary"]["same_source_unison_phase_candidates"] = topology[
        "summary"
    ]["same_source_unison_phase_candidate_count"]
    report["summary"][
        "same_source_exact_unison_level_stack_candidates"
    ] = topology["summary"][
        "same_source_exact_unison_level_stack_candidate_count"
    ]
    report["summary"]["warning_count"] = len(warnings)


__all__ = (
    "MAX_REPORTED_PAIRS",
    "MAX_REPORTED_WARNINGS",
    "MINIMUM_PHASE_DELAY_SECONDS",
    "MINIMUM_NEAR_SIMULTANEOUS_RATIO",
    "MINIMUM_SHORTER_PART_COVERAGE",
    "MINIMUM_UNISON_EVENT_COUNT",
    "NEAR_SIMULTANEOUS_SECONDS",
    "TOPOLOGY_FORMAT",
    "TOPOLOGY_VERSION",
    "analyze_orchestration_topology",
    "attach_orchestration_topology",
)
