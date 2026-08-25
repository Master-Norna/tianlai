"""Deterministic machine triage for potentially mechanical performance plans.

This module does not score naturalness and never claims that music is good,
bad, natural, or unnatural.  It only exposes reproducible facts and bounded
review candidates that can reduce the amount of undirected human listening.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
import math
import re
from typing import Any

from .canonical_json import canonical_json_sha256
from .conductor import _merge_ties, _onset_groups
from .score import ScoreDocument


REPORT_FORMAT = "tianlai.performance_naturalness"
REPORT_VERSION = 1

_RESIDUAL_PATTERN = re.compile(
    r"^时值 ([+-]?(?:\d+(?:\.\d*)?|\.\d+))ms / 力度 "
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))$"
)
_ONSET_AUDIT_STATUSES = frozenset(
    {
        "applied",
        "not_applied_runtime_configuration_mismatch",
        "not_applied_unapproved_context",
    }
)
_RESIDUAL_RECONSTRUCTION_UNCERTAINTY_SECONDS = 0.000105


def _plan_document(plan: Any) -> Mapping[str, Any]:
    document = plan.to_dict() if hasattr(plan, "to_dict") else plan
    if not isinstance(document, Mapping):
        raise ValueError("performance plan must be an object")
    return document


def _bounded_positive_integer(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _example(note: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bar": int(note.bar),
        "beat": float(note.beat),
        "pitch_midi": round(float(note.midi), 6),
    }
    if note.source_event_id is not None:
        result["event_id"] = str(note.source_event_id)
    return result


def _candidate(
    *,
    code: str,
    level: str,
    confidence: str,
    basis: str,
    scope: Mapping[str, Any],
    message: str,
    evidence: Mapping[str, Any],
    review_question: str,
    suggestions: tuple[str, ...],
) -> dict[str, Any]:
    identity = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "code": code,
        "scope": dict(scope),
    }
    body = {
        "code": code,
        "level": level,
        "confidence": confidence,
        "basis": basis,
        "scope": dict(scope),
        "message": message,
        "evidence": dict(evidence),
        "review_question": review_question,
        "suggestions": list(suggestions),
        "blocking": False,
        "automatic_change": False,
    }
    return {
        # Presentation bounds (for example ``max_examples``) may change the
        # evidence excerpt, but must not rename the same semantic candidate.
        "candidate_id": "naturalness-" + canonical_json_sha256(identity)[:20],
        **body,
    }


def _phrase_analysis(
    score: ScoreDocument,
    *,
    max_examples: int,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    candidates: list[dict[str, Any]] = []
    explicit_part_count = 0
    valid_phrase_count = 0
    empty_phrase_count = 0
    uncovered_onset_count = 0
    overlapping_onset_count = 0
    event_context: dict[str, dict[str, Any]] = {}
    expected_part_events: dict[str, dict[str, Any]] = {}

    for part in score.parts:
        merged = _merge_ties(part.notes, score)
        expected_part_events[part.id] = {
            "merged_event_count": len(merged),
            "event_ids": {
                str(note.source_event_id)
                for note in merged
                if note.source_event_id is not None
            },
        }
        for note in part.notes:
            if note.source_event_id is None:
                continue
            event_context[str(note.source_event_id)] = {
                "part_id": part.id,
                "staff": 0 if note.staff is None else int(note.staff),
                "voice": "__default__" if note.voice is None else str(note.voice),
            }
        if not part.phrases or not merged:
            continue
        explicit_part_count += 1
        coverage = [0] * len(merged)
        empty_indexes: list[int] = []
        for phrase_index, phrase in enumerate(part.phrases):
            start_q = score.tempo_map.quarter_at(
                phrase.start_bar, phrase.start_beat
            )
            end_q = score.tempo_map.quarter_at(phrase.end_bar, phrase.end_beat)
            indexes = [
                index
                for index, note in enumerate(merged)
                if start_q - 1.0e-6 <= note.start_quarter <= end_q + 1.0e-6
            ]
            if not indexes:
                empty_indexes.append(phrase_index)
                continue
            valid_phrase_count += 1
            for index in range(indexes[0], indexes[-1] + 1):
                coverage[index] += 1

        if empty_indexes:
            empty_phrase_count += len(empty_indexes)
            candidates.append(
                _candidate(
                    code="performance.explicit_phrase_empty",
                    level="info",
                    confidence="high",
                    basis="execution_semantics",
                    scope={"part_id": part.id},
                    message=(
                        "显式乐句标记没有命中任何合并后的起音；它不会直接参与当前演奏塑形。"
                    ),
                    evidence={
                        "empty_phrase_indexes": empty_indexes[:max_examples],
                        "empty_phrase_count": len(empty_indexes),
                        "examples_truncated": len(empty_indexes) > max_examples,
                    },
                    review_question=(
                        "这些空乐句标记是坐标失配、连音合并后的遗留，还是有意保留？"
                    ),
                    suggestions=("核对乐句坐标；若无意则修正或删除空标记。",),
                )
            )

        # When every explicit mark is empty the conductor falls back to inferred
        # phrases.  There is no uncovered-material discontinuity in that case.
        if not any(coverage):
            continue

        onset_groups = _onset_groups(merged)
        uncovered_groups = [
            group
            for group in onset_groups
            if all(coverage[index] == 0 for index in range(group[0], group[1] + 1))
        ]
        overlap_groups = [
            group
            for group in onset_groups
            if any(coverage[index] > 1 for index in range(group[0], group[1] + 1))
        ]
        uncovered_onset_count += len(uncovered_groups)
        overlapping_onset_count += len(overlap_groups)

        if uncovered_groups:
            examples = [_example(merged[group[0]]) for group in uncovered_groups]
            affected_note_count = sum(
                group[1] - group[0] + 1 for group in uncovered_groups
            )
            candidates.append(
                _candidate(
                    code="performance.explicit_phrase_coverage_gap",
                    level="warning",
                    confidence="high",
                    basis="execution_semantics",
                    scope={"part_id": part.id},
                    message=(
                        "该声部已有显式乐句，但仍有材料未被任何乐句覆盖；当前实现不会为这些位置补做自动乐句塑形。"
                    ),
                    evidence={
                        "affected_onset_count": len(uncovered_groups),
                        "affected_note_count": affected_note_count,
                        "examples": examples[:max_examples],
                        "examples_truncated": len(examples) > max_examples,
                    },
                    review_question=(
                        "这些位置失去乐句塑形是有意的断面，还是显式乐句覆盖遗漏？"
                    ),
                    suggestions=(
                        "若是遗漏，补齐相邻乐句边界后重新编译。",
                        "若是有意的机械或裸露段落，保留并在本轮审视中说明。",
                    ),
                )
            )

        if overlap_groups:
            examples = [_example(merged[group[0]]) for group in overlap_groups]
            affected_note_count = sum(
                sum(
                    1
                    for index in range(group[0], group[1] + 1)
                    if coverage[index] > 1
                )
                for group in overlap_groups
            )
            candidates.append(
                _candidate(
                    code="performance.explicit_phrase_overlap",
                    level="warning",
                    confidence="high",
                    basis="execution_semantics",
                    scope={"part_id": part.id},
                    message=(
                        "同一起音被多个显式乐句覆盖；当前演奏塑形会采用后写入的乐句，结果受乐句数组顺序影响。"
                    ),
                    evidence={
                        "affected_onset_count": len(overlap_groups),
                        "affected_note_count": affected_note_count,
                        "examples": examples[:max_examples],
                        "examples_truncated": len(examples) > max_examples,
                    },
                    review_question=(
                        "重叠是有意的边界共享，还是会让同一材料获得意外的后写覆盖？"
                    ),
                    suggestions=(
                        "明确相邻乐句边界，避免同一起音同时属于两条乐句。",
                        "若重叠确属意图，保留并把数组顺序视为演奏设计的一部分。",
                    ),
                )
            )

    return candidates, {
        "status": "ready",
        "explicit_phrase_part_count": explicit_part_count,
        "valid_explicit_phrase_count": valid_phrase_count,
        "empty_explicit_phrase_count": empty_phrase_count,
        "uncovered_onset_count": uncovered_onset_count,
        "overlapping_onset_count": overlapping_onset_count,
    }, event_context, expected_part_events


def _residual_values(trace: Mapping[str, Any]) -> tuple[str, float]:
    derivation = trace.get("推导")
    if not isinstance(derivation, Mapping):
        return "absent", 0.0
    if "残差随机" not in derivation:
        return "absent", 0.0
    raw = derivation["残差随机"]
    if not isinstance(raw, str):
        return "invalid", 0.0
    match = _RESIDUAL_PATTERN.fullmatch(raw)
    if match is None:
        return "invalid", 0.0
    timing = float(match.group(1)) / 1000.0
    velocity = float(match.group(2))
    if not math.isfinite(timing) or not math.isfinite(velocity):
        return "invalid", 0.0
    return "valid", timing


def _connection_category(gap: float, tolerance: float) -> str:
    if gap < -tolerance:
        return "overlap"
    if gap > tolerance:
        return "separate"
    return "touch"


def _performance_plan_analysis(
    plan: Mapping[str, Any],
    *,
    event_context: Mapping[str, Mapping[str, Any]],
    expected_part_events: Mapping[str, Mapping[str, Any]],
    max_examples: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_parts = plan.get("parts")
    if not isinstance(raw_parts, list):
        raise ValueError("performance plan parts must be an array")
    candidates: list[dict[str, Any]] = []
    executor_summaries: list[dict[str, Any]] = []
    total_trace_count = 0
    total_parsed_residual_count = 0
    total_usable_timing_residual_count = 0
    total_invalid_residual_count = 0
    total_missing_residual_count = 0
    total_unexpected_residual_count = 0
    total_out_of_range_residual_count = 0

    raw_expression = plan.get("expression")
    raw_humanize = (
        raw_expression.get("humanize")
        if isinstance(raw_expression, Mapping)
        else None
    )
    raw_depth = (
        raw_humanize.get("depth")
        if isinstance(raw_humanize, Mapping)
        else None
    )
    raw_timing_ms = (
        raw_humanize.get("timing_ms")
        if isinstance(raw_humanize, Mapping)
        else None
    )
    humanize_contract_valid = bool(
        isinstance(raw_depth, (int, float))
        and not isinstance(raw_depth, bool)
        and math.isfinite(float(raw_depth))
        and 0.0 <= float(raw_depth) <= 4.0
        and isinstance(raw_timing_ms, (int, float))
        and not isinstance(raw_timing_ms, bool)
        and math.isfinite(float(raw_timing_ms))
        and float(raw_timing_ms) >= 0.0
    )
    humanize_depth = float(raw_depth) if humanize_contract_valid else 0.0
    humanize_timing_ms = (
        float(raw_timing_ms) if humanize_contract_valid else 0.0
    )
    timing_residual_expected = bool(
        humanize_contract_valid
        and humanize_depth > 0.0
        and humanize_timing_ms > 0.0
    )

    planned_part_ids: set[str] = set()
    observed_event_ids_by_part: dict[str, set[str]] = defaultdict(set)
    unexpected_event_ids_by_part: dict[str, set[str]] = defaultdict(set)
    unidentified_trace_events_by_part: Counter[str] = Counter()
    malformed_trace_events_by_part: Counter[str] = Counter()
    duplicate_trace_events_by_part: Counter[str] = Counter()
    for raw_part in raw_parts:
        if not isinstance(raw_part, Mapping):
            continue
        part_id = str(raw_part.get("part_id", "unknown"))
        planned_part_ids.add(part_id)
        raw_trace = raw_part.get("trace")
        if not isinstance(raw_trace, list):
            continue
        expected = expected_part_events.get(part_id)
        expected_ids = (
            expected.get("event_ids") if isinstance(expected, Mapping) else set()
        )
        expected_ids = expected_ids if isinstance(expected_ids, set) else set()
        executor_seen_event_ids: set[str] = set()
        for item in raw_trace:
            if not isinstance(item, Mapping):
                malformed_trace_events_by_part[part_id] += 1
                continue
            event_id = item.get("source_event_id")
            if event_id is None:
                unidentified_trace_events_by_part[part_id] += 1
                continue
            stable_event_id = str(event_id)
            if stable_event_id in executor_seen_event_ids:
                duplicate_trace_events_by_part[part_id] += 1
            else:
                executor_seen_event_ids.add(stable_event_id)
            context = event_context.get(stable_event_id)
            if (
                context is not None
                and str(context.get("part_id")) == part_id
                and stable_event_id in expected_ids
            ):
                observed_event_ids_by_part[part_id].add(stable_event_id)
            else:
                unexpected_event_ids_by_part[part_id].add(stable_event_id)

    part_trace_coverage: list[dict[str, Any]] = []
    partial_coverage_parts: set[str] = set()
    for part_id in sorted(planned_part_ids):
        expected = expected_part_events.get(part_id)
        if not isinstance(expected, Mapping):
            partial_coverage_parts.add(part_id)
            part_trace_coverage.append(
                {
                    "part_id": part_id,
                    "status": "partial_evidence_unknown_score_part",
                    "merged_score_event_count": 0,
                    "stable_score_event_count": 0,
                    "observed_score_event_count": 0,
                    "missing_score_event_count": 0,
                    "unidentified_score_event_count": 0,
                    "unexpected_trace_event_count": len(
                        unexpected_event_ids_by_part.get(part_id, set())
                    ),
                    "unidentified_trace_event_count": (
                        unidentified_trace_events_by_part[part_id]
                    ),
                    "malformed_trace_event_count": (
                        malformed_trace_events_by_part[part_id]
                    ),
                    "duplicate_trace_event_count": (
                        duplicate_trace_events_by_part[part_id]
                    ),
                    "missing_event_ids": [],
                    "missing_event_ids_truncated": False,
                }
            )
            continue
        merged_event_count = int(expected.get("merged_event_count", 0))
        expected_ids = expected.get("event_ids")
        expected_ids = expected_ids if isinstance(expected_ids, set) else set()
        observed_ids = observed_event_ids_by_part.get(part_id, set())
        unexpected_ids = unexpected_event_ids_by_part.get(part_id, set())
        unidentified_trace_count = unidentified_trace_events_by_part[part_id]
        malformed_trace_count = malformed_trace_events_by_part[part_id]
        duplicate_trace_count = duplicate_trace_events_by_part[part_id]
        missing_ids = sorted(expected_ids - observed_ids)
        unidentified_count = max(0, merged_event_count - len(expected_ids))
        if merged_event_count == 0:
            coverage_status = "not_applicable_empty_part"
        elif (
            missing_ids
            or unidentified_count
            or unexpected_ids
            or unidentified_trace_count
            or malformed_trace_count
            or duplicate_trace_count
        ):
            coverage_status = "partial_evidence"
            partial_coverage_parts.add(part_id)
        else:
            coverage_status = "complete"
        part_trace_coverage.append(
            {
                "part_id": part_id,
                "status": coverage_status,
                "merged_score_event_count": merged_event_count,
                "stable_score_event_count": len(expected_ids),
                "observed_score_event_count": len(observed_ids),
                "missing_score_event_count": len(missing_ids),
                "unidentified_score_event_count": unidentified_count,
                "unexpected_trace_event_count": len(unexpected_ids),
                "unidentified_trace_event_count": unidentified_trace_count,
                "malformed_trace_event_count": malformed_trace_count,
                "duplicate_trace_event_count": duplicate_trace_count,
                "missing_event_ids": missing_ids[:max_examples],
                "missing_event_ids_truncated": len(missing_ids) > max_examples,
            }
        )

    for raw_part in raw_parts:
        if not isinstance(raw_part, Mapping):
            continue
        executor_id = str(raw_part.get("executor_id", "unknown"))
        part_id = str(raw_part.get("part_id", "unknown"))
        raw_trace = raw_part.get("trace")
        if not isinstance(raw_trace, list):
            continue
        trace = [item for item in raw_trace if isinstance(item, Mapping)]
        total_trace_count += len(trace)
        parsed_residual_evidence_count = 0
        usable_timing_residual_count = 0
        invalid_residual_evidence_count = 0
        missing_residual_evidence_count = 0
        unexpected_residual_evidence_count = 0
        out_of_range_residual_evidence_count = 0
        streams: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        # A kit assignment is an independently triggered one-shot voice.  Its
        # score duration/note_off is transport bookkeeping rather than audible
        # legato, so an overlap/touch/separate counterfactual would manufacture
        # a connection problem that the renderer cannot produce.
        connection_semantics_applicable = raw_part.get("kit_pitch") is None
        connection_trace_count = 0
        mapped_connection_event_count = 0
        usable_connection_event_count = 0
        unmapped_connection_event_count = 0
        mismatched_connection_event_count = 0
        excluded_connection_event_count = 0
        invalid_connection_event_count = 0
        duplicate_connection_event_count = 0
        executor_seen_event_ids: set[str] = set()
        onset_statuses: Counter[str] = Counter()
        onset_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
        onset_example_totals: Counter[str] = Counter()
        onset_groups: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)

        for item in trace:
            residual_status, jitter_seconds = _residual_values(item)
            residual_present = False
            if residual_status == "valid":
                parsed_residual_evidence_count += 1
                total_parsed_residual_count += 1
            if residual_status == "absent":
                if timing_residual_expected:
                    missing_residual_evidence_count += 1
                    total_missing_residual_count += 1
            elif residual_status == "invalid":
                invalid_residual_evidence_count += 1
                total_invalid_residual_count += 1
            elif not humanize_contract_valid:
                unexpected_residual_evidence_count += 1
                total_unexpected_residual_count += 1
            elif humanize_depth <= 0.0:
                unexpected_residual_evidence_count += 1
                total_unexpected_residual_count += 1
            elif humanize_timing_ms <= 0.0:
                if abs(jitter_seconds) > 0.000051:
                    unexpected_residual_evidence_count += 1
                    total_unexpected_residual_count += 1
            elif abs(jitter_seconds) > (
                humanize_depth * humanize_timing_ms / 1000.0 + 0.000051
            ):
                out_of_range_residual_evidence_count += 1
                total_out_of_range_residual_count += 1
            else:
                residual_present = True
                usable_timing_residual_count += 1
                total_usable_timing_residual_count += 1
            derivation = item.get("推导")
            derivation = derivation if isinstance(derivation, Mapping) else {}
            realization = derivation.get("realization")

            onset_audit = derivation.get("发音补偿审计")
            if isinstance(onset_audit, Mapping):
                status = str(onset_audit.get("status", "unknown"))
                if status not in _ONSET_AUDIT_STATUSES:
                    status = "unknown"
                onset_statuses[status] += 1
                onset_example_totals[status] += 1
                onset_group = (
                    str(onset_audit.get("context", "unknown")),
                    str(onset_audit.get("final_articulation", "unknown")),
                )
                onset_groups[onset_group][status] += 1
                if len(onset_examples[status]) < max_examples:
                    example = {
                        "bar": item.get("小节"),
                        "beat": item.get("拍"),
                    }
                    if item.get("source_event_id") is not None:
                        example["event_id"] = item["source_event_id"]
                    onset_examples[status].append(example)

            event_id = item.get("source_event_id")
            if event_id is not None:
                stable_event_id = str(event_id)
                if stable_event_id in executor_seen_event_ids:
                    duplicate_connection_event_count += 1
                else:
                    executor_seen_event_ids.add(stable_event_id)

            if not connection_semantics_applicable:
                continue
            connection_trace_count += 1
            context = event_context.get(str(event_id)) if event_id is not None else None
            if context is None:
                unmapped_connection_event_count += 1
                continue
            if str(context.get("part_id")) != part_id:
                mismatched_connection_event_count += 1
                continue
            mapped_connection_event_count += 1
            raw_start = item.get("时间")
            raw_duration = item.get("时长")
            if (
                isinstance(raw_start, bool)
                or not isinstance(raw_start, (int, float))
                or isinstance(raw_duration, bool)
                or not isinstance(raw_duration, (int, float))
                or not math.isfinite(float(raw_start))
                or not math.isfinite(float(raw_duration))
                or float(raw_start) < 0.0
                or float(raw_duration) <= 0.0
            ):
                invalid_connection_event_count += 1
                continue
            # Boundary clipping and sample-grid realization paths make
            # subtraction from the rounded trace insufficiently exact.  Exclude
            # them rather than manufacture a counterfactual.
            if residual_present and (
                "时间边界" in derivation
                or (
                    isinstance(onset_audit, Mapping)
                    and float(
                        onset_audit.get("clipped_delay_seconds", 0.0) or 0.0
                    )
                    > 0.0
                )
            ):
                excluded_connection_event_count += 1
                continue
            # Any authored realization enters the conductor's exact sample-grid
            # resolution path, including velocity-only overrides.  Subtracting
            # the rounded jitter from that quantized trace cannot reproduce a
            # depth=0 compile, so exclude it rather than guess.
            has_realization_override = isinstance(realization, Mapping) and bool(
                realization
            )
            if has_realization_override and residual_present:
                excluded_connection_event_count += 1
                continue
            actual_start = float(raw_start)
            actual_end = actual_start + float(raw_duration)
            baseline_start = actual_start - jitter_seconds
            baseline_end = actual_end - jitter_seconds
            if residual_present and baseline_start < 0.0:
                excluded_connection_event_count += 1
                continue
            usable_connection_event_count += 1
            stream_key = (int(context["staff"]), str(context["voice"]))
            streams[stream_key].append(
                {
                    "event_id": str(event_id),
                    "bar": item.get("小节"),
                    "beat": item.get("拍"),
                    "actual_start": actual_start,
                    "actual_end": actual_end,
                    "baseline_start": baseline_start,
                    "baseline_end": baseline_end,
                    "has_residual": residual_present,
                }
            )

        connection_examples: list[dict[str, Any]] = []
        connection_flip_count = 0
        comparable_connections = 0
        indeterminate_connections = 0
        # Missing or deliberately excluded events are barriers, not permission
        # to connect the surviving neighbours across an unknown middle event.
        # Because an unmapped event has no reliable staff/voice key, suppress
        # this executor's relation counterfactual conservatively.
        connection_evidence_partial = bool(
            unmapped_connection_event_count
            or mismatched_connection_event_count
            or excluded_connection_event_count
            or invalid_connection_event_count
            or duplicate_connection_event_count
            or invalid_residual_evidence_count
            or missing_residual_evidence_count
            or unexpected_residual_evidence_count
            or out_of_range_residual_evidence_count
            or part_id in partial_coverage_parts
        )
        if connection_evidence_partial:
            streams.clear()
        for (staff, voice), events in sorted(streams.items()):
            ordered = sorted(
                events,
                key=lambda value: (
                    value["baseline_start"],
                    value["actual_start"],
                    value["event_id"],
                ),
            )
            groups: list[dict[str, Any]] = []
            for event in ordered:
                if not groups or not math.isclose(
                    event["baseline_start"],
                    groups[-1]["baseline_start"],
                    rel_tol=0.0,
                    abs_tol=0.00025,
                ):
                    groups.append(
                        {
                            "baseline_start": event["baseline_start"],
                            "baseline_end": event["baseline_end"],
                            "actual_start": event["actual_start"],
                            "actual_end": event["actual_end"],
                            "event_ids": [event["event_id"]],
                            "bar": event["bar"],
                            "beat": event["beat"],
                            "has_residual": event["has_residual"],
                        }
                    )
                else:
                    group = groups[-1]
                    group["baseline_end"] = max(
                        group["baseline_end"], event["baseline_end"]
                    )
                    group["actual_start"] = min(
                        group["actual_start"], event["actual_start"]
                    )
                    group["actual_end"] = max(
                        group["actual_end"], event["actual_end"]
                    )
                    group["event_ids"].append(event["event_id"])
                    group["has_residual"] = (
                        group["has_residual"] or event["has_residual"]
                    )
            for previous, current in zip(groups, groups[1:]):
                local_ioi = current["baseline_start"] - previous["baseline_start"]
                if local_ioi <= 0.0:
                    continue
                tolerance = max(0.002, min(0.01, local_ioi * 0.01))
                baseline_gap = current["baseline_start"] - previous["baseline_end"]
                actual_gap = current["actual_start"] - previous["actual_end"]
                boundary_distance = min(
                    abs(baseline_gap - tolerance),
                    abs(baseline_gap + tolerance),
                    abs(actual_gap - tolerance),
                    abs(actual_gap + tolerance),
                )
                if (
                    (previous["has_residual"] or current["has_residual"])
                    and boundary_distance
                    <= _RESIDUAL_RECONSTRUCTION_UNCERTAINTY_SECONDS
                ):
                    indeterminate_connections += 1
                    continue
                comparable_connections += 1
                baseline_category = _connection_category(baseline_gap, tolerance)
                actual_category = _connection_category(actual_gap, tolerance)
                if baseline_category == actual_category:
                    continue
                connection_flip_count += 1
                if len(connection_examples) < max_examples:
                    connection_examples.append(
                        {
                            "staff": staff,
                            "voice": voice,
                            "from_event_ids": sorted(previous["event_ids"]),
                            "to_event_ids": sorted(current["event_ids"]),
                            "bar": current["bar"],
                            "beat": current["beat"],
                            "baseline_relation": baseline_category,
                            "actual_relation": actual_category,
                            "baseline_gap_seconds": round(baseline_gap, 6),
                            "actual_gap_seconds": round(actual_gap, 6),
                            "tolerance_seconds": round(tolerance, 6),
                        }
                    )

        if not connection_semantics_applicable:
            connection_check_status = "not_applicable_one_shot_kit"
        elif connection_evidence_partial:
            connection_check_status = "partial_evidence"
        elif indeterminate_connections:
            connection_check_status = "partial_evidence"
        elif comparable_connections == 0:
            connection_check_status = "not_applicable_no_adjacent_events"
        else:
            connection_check_status = "ready"

        onset_group_summaries = [
            {
                "context": context,
                "final_articulation": articulation,
                "status_counts": dict(sorted(statuses.items())),
            }
            for (context, articulation), statuses in sorted(onset_groups.items())
        ]
        executor_summaries.append(
            {
                "executor_id": executor_id,
                "part_id": part_id,
                "trace_event_count": len(trace),
                "parsed_residual_evidence_count": (
                    parsed_residual_evidence_count
                ),
                "usable_timing_residual_event_count": (
                    usable_timing_residual_count
                ),
                "invalid_residual_evidence_count": (
                    invalid_residual_evidence_count
                ),
                "missing_residual_evidence_count": (
                    missing_residual_evidence_count
                ),
                "unexpected_residual_evidence_count": (
                    unexpected_residual_evidence_count
                ),
                "out_of_range_residual_evidence_count": (
                    out_of_range_residual_evidence_count
                ),
                "connection_semantics": (
                    "note_gate_relations"
                    if connection_semantics_applicable
                    else "not_applicable_one_shot_kit"
                ),
                "connection_check_status": connection_check_status,
                "connection_trace_event_count": connection_trace_count,
                "mapped_connection_event_count": mapped_connection_event_count,
                "usable_connection_event_count": usable_connection_event_count,
                "unmapped_connection_event_count": unmapped_connection_event_count,
                "mismatched_connection_event_count": (
                    mismatched_connection_event_count
                ),
                "part_trace_coverage_status": next(
                    (
                        str(item["status"])
                        for item in part_trace_coverage
                        if item["part_id"] == part_id
                    ),
                    "partial_evidence_unknown_score_part",
                ),
                "excluded_connection_event_count": excluded_connection_event_count,
                "invalid_connection_event_count": invalid_connection_event_count,
                "duplicate_trace_event_count": (
                    duplicate_connection_event_count
                ),
                "comparable_connection_count": comparable_connections,
                "indeterminate_connection_count": indeterminate_connections,
                "undeclared_connection_flip_count": connection_flip_count,
                "onset_compensation_status_counts": dict(sorted(onset_statuses.items())),
                "onset_compensation_group_count": len(onset_group_summaries),
                "onset_compensation_groups": onset_group_summaries[
                    :max_examples
                ],
                "onset_compensation_groups_truncated": (
                    len(onset_group_summaries) > max_examples
                ),
            }
        )

        if connection_flip_count and not connection_evidence_partial:
            candidates.append(
                _candidate(
                    code="performance.residual_connection_flip_candidate",
                    level="info",
                    confidence="medium",
                    basis="counterfactual_plan_trace",
                    scope={"executor_id": executor_id, "part_id": part_id},
                    message=(
                        "去掉残差随机后，相邻音的连接类别与当前计划不同；机器只能确认关系被随机微差改写，不能判断这种断口或涂抹是否必要。"
                    ),
                    evidence={
                        "reported_flip_count": connection_flip_count,
                        "examples": connection_examples,
                        "examples_truncated": connection_flip_count
                        > len(connection_examples),
                        "indeterminate_connection_count": (
                            indeterminate_connections
                        ),
                    },
                    review_question=(
                        "这些连接变化是有意的演奏呼吸，还是随机微差反客为主？"
                    ),
                    suggestions=(
                        "以 humanize.depth=0 做一次只读对照，优先核对列出的完整上下文位置。",
                        "若变化有明确演奏意图，用 realization 固定它；若无意则降低残差随机。",
                    ),
                )
            )

        runtime_mismatch_count = onset_statuses.get(
            "not_applied_runtime_configuration_mismatch", 0
        )
        if runtime_mismatch_count:
            candidates.append(
                _candidate(
                    code="performance.onset_runtime_configuration_mismatch",
                    level="info",
                    confidence="high",
                    basis="instrument_contract",
                    scope={"executor_id": executor_id, "part_id": part_id},
                    message=(
                        "该执行器存在已批准的发音证据，但当前运行配置与证据绑定不一致，因此相关位置没有应用补偿。"
                    ),
                    evidence={
                        "status_counts": dict(sorted(onset_statuses.items())),
                        "examples_by_status": dict(sorted(onset_examples.items())),
                        "examples_truncated_by_status": {
                            status: onset_example_totals[status]
                            > len(onset_examples[status])
                            for status in sorted(onset_examples)
                        },
                    },
                    review_question=(
                        "当前样本变体与运行配置是否正是作品需要的版本；若是，是否应为它补做对应证据？"
                    ),
                    suggestions=(
                        "核对运行配置、样本变体与证据绑定；不要借用另一配置的延迟，也不要凭乐器名称猜补偿。",
                    ),
                )
            )

    connection_status_counts = Counter(
        str(item["connection_check_status"]) for item in executor_summaries
    )
    if (
        not humanize_contract_valid
        or partial_coverage_parts
        or total_invalid_residual_count
        or total_missing_residual_count
        or total_unexpected_residual_count
        or total_out_of_range_residual_count
        or connection_status_counts.get("partial_evidence", 0)
    ):
        plan_status = "partial_evidence"
    elif not total_trace_count:
        plan_status = "insufficient_evidence"
    else:
        plan_status = "ready"
    return candidates, {
        "status": plan_status,
        "trace_event_count": total_trace_count,
        "parsed_residual_evidence_count": total_parsed_residual_count,
        "usable_timing_residual_event_count": (
            total_usable_timing_residual_count
        ),
        "invalid_residual_evidence_count": total_invalid_residual_count,
        "missing_residual_evidence_count": total_missing_residual_count,
        "unexpected_residual_evidence_count": total_unexpected_residual_count,
        "out_of_range_residual_evidence_count": (
            total_out_of_range_residual_count
        ),
        "humanize_timing_contract": {
            "status": "ready" if humanize_contract_valid else "invalid",
            "depth": humanize_depth if humanize_contract_valid else None,
            "timing_ms": (
                humanize_timing_ms if humanize_contract_valid else None
            ),
            "timing_residual_expected": timing_residual_expected,
        },
        "connection_check_status_counts": dict(
            sorted(connection_status_counts.items())
        ),
        "part_trace_coverage": part_trace_coverage,
        "executors": sorted(
            executor_summaries,
            key=lambda item: (item["executor_id"], item["part_id"]),
        ),
    }


def _authored_direction_analysis(
    score: ScoreDocument,
    plan: Mapping[str, Any],
    *,
    max_examples: int,
) -> list[dict[str, Any]]:
    raw_parts = plan.get("parts")
    plan_parts = raw_parts if isinstance(raw_parts, list) else []
    by_part: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    realization_event_ids_by_part: dict[str, set[str]] = defaultdict(set)
    for raw_part in plan_parts:
        if isinstance(raw_part, Mapping):
            part_id = str(raw_part.get("part_id", "unknown"))
            by_part[part_id].append(raw_part)
            trace = raw_part.get("trace")
            if isinstance(trace, list):
                for item in trace:
                    if (
                        isinstance(item, Mapping)
                        and item.get("source_event_id") is not None
                        and isinstance(item.get("推导"), Mapping)
                        and isinstance(item["推导"].get("realization"), Mapping)
                        and bool(item["推导"]["realization"])
                    ):
                        realization_event_ids_by_part[part_id].add(
                            str(item["source_event_id"])
                        )

    candidates: list[dict[str, Any]] = []
    for part in score.parts:
        merged = _merge_ties(part.notes, score)
        groups = _onset_groups(merged) if merged else []
        if len(groups) < 8:
            continue
        directed_groups = 0
        examples: list[dict[str, Any]] = []
        for group in groups:
            explicit = any(
                merged[index].velocity is not None
                or bool(merged[index].dynamic)
                or merged[index].articulation is not None
                or (
                    merged[index].source_event_id is not None
                    and str(merged[index].source_event_id)
                    in realization_event_ids_by_part.get(part.id, set())
                )
                for index in range(group[0], group[1] + 1)
            )
            if explicit:
                directed_groups += 1
            elif len(examples) < max_examples:
                examples.append(_example(merged[group[0]]))
        has_automation = any(
            bool(plan_part.get("gain_automation"))
            or bool(plan_part.get("gain_envelope"))
            or bool(plan_part.get("control_trace"))
            for plan_part in by_part.get(part.id, [])
        )
        has_realization = bool(realization_event_ids_by_part.get(part.id))
        if (
            part.phrases
            or part.default_articulation is not None
            or has_automation
            or directed_groups / len(groups) >= 0.1
        ):
            continue
        candidates.append(
            _candidate(
                code="performance.authored_direction_sparse",
                level="info",
                confidence="low",
                basis="coverage",
                scope={"part_id": part.id},
                message=(
                    "这个较长声部几乎没有作品自写的乐句、逐音力度/奏法或自动化；当前细节主要来自通用指挥规则与可选残差微差。"
                ),
                evidence={
                    "onset_count": len(groups),
                    "explicitly_directed_onset_count": directed_groups,
                    "has_explicit_phrases": bool(part.phrases),
                    "has_gain_or_control_automation": has_automation,
                    "has_realization": has_realization,
                    "examples_without_explicit_direction": examples,
                    "examples_truncated": len(groups) - directed_groups > len(examples),
                },
                review_question=(
                    "这条声部的静态或机械性是作品意图，还是演奏设计尚未发生？"
                ),
                suggestions=(
                    "可以保持委托给通用指挥、减少通用指挥或残差介入，或只在必要位置补写方向；三者都合法，不要为了消除候选默认增加控制。",
                ),
            )
        )
    return candidates


def build_unavailable_performance_naturalness_report(
    *,
    binding: Mapping[str, Any] | None = None,
    reason_code: str = "analysis_failed",
    error_type: str | None = None,
    post_render_check_available: bool = False,
    mix_report_available: bool = False,
) -> dict[str, Any]:
    """Return the same bounded contract when this advisory analysis fails."""

    unavailable_item: dict[str, str] = {
        "check": "performance_naturalness",
        "reason_code": reason_code,
    }
    if error_type is not None:
        unavailable_item["error_type"] = error_type
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "scope": "machine_triage_only",
        "status": "unavailable",
        "evidence_coverage": "unavailable",
        "bindings": dict(binding or {}),
        "facts": {
            "phrase_coverage": {"status": "unavailable"},
            "performance_plan": {
                "status": "unavailable",
                "trace_event_count": 0,
                "parsed_residual_evidence_count": 0,
                "usable_timing_residual_event_count": 0,
                "invalid_residual_evidence_count": 0,
                "missing_residual_evidence_count": 0,
                "unexpected_residual_evidence_count": 0,
                "out_of_range_residual_evidence_count": 0,
                "humanize_timing_contract": {"status": "unavailable"},
                "connection_check_status_counts": {},
                "part_trace_coverage": [],
                "executors": [],
            },
            "waveform_response": {
                "status": "unavailable",
                "reason_code": "event_isolated_envelope_evidence_not_recorded",
                "available_render_evidence": {
                    "post_render_check": post_render_check_available,
                    "mix_report": mix_report_available,
                },
                "global_metrics_used_as_naturalness_evidence": False,
                "explanation": (
                    "全曲工程指标不能替代本次不可用的逐事件演奏审查。"
                ),
            },
        },
        "candidates": [],
        "summary": {
            "candidate_count": 0,
            "reported_candidate_count": 0,
            "warning_candidate_count": 0,
            "information_candidate_count": 0,
            "candidates_truncated": False,
        },
        "unavailable": [unavailable_item],
        "authority": {
            "audio_audition_performed": False,
            "naturalness_proven": False,
            "aesthetic_quality_proven": False,
            "automatic_change": False,
            "workflow_blocking": False,
            "intentional_mechanics_allowed": True,
        },
        "notice": (
            "机器预审不可用不影响候选完整性或渲染资格，也不能据此推断听感。"
        ),
    }
    report["report_sha256"] = canonical_json_sha256(report)
    return report


def analyze_performance_naturalness(
    score: ScoreDocument | None,
    performance_plan: Any,
    *,
    binding: Mapping[str, Any] | None = None,
    post_render_check: Mapping[str, Any] | None = None,
    mix_report: Mapping[str, Any] | None = None,
    max_candidates: int = 32,
    max_examples: int = 8,
) -> dict[str, Any]:
    """Return a path-free, non-authoritative naturalness-risk inspection."""

    candidate_limit = _bounded_positive_integer(
        max_candidates, field="max_candidates"
    )
    example_limit = _bounded_positive_integer(max_examples, field="max_examples")
    plan = _plan_document(performance_plan)
    bindings = dict(binding or {})

    candidates: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    if score is None:
        phrase_summary: dict[str, Any] = {"status": "unavailable"}
        event_context: dict[str, dict[str, Any]] = {}
        expected_part_events: dict[str, dict[str, Any]] = {}
        unavailable.append(
            {
                "check": "score_phrase_and_authored_direction",
                "reason_code": "parsed_score_not_supplied",
            }
        )
    else:
        (
            phrase_candidates,
            phrase_summary,
            event_context,
            expected_part_events,
        ) = _phrase_analysis(score, max_examples=example_limit)
        candidates.extend(phrase_candidates)
        candidates.extend(
            _authored_direction_analysis(
                score, plan, max_examples=example_limit
            )
        )

    plan_candidates, plan_summary = _performance_plan_analysis(
        plan,
        event_context=event_context,
        expected_part_events=expected_part_events,
        max_examples=example_limit,
    )
    candidates.extend(plan_candidates)
    if plan_summary["status"] != "ready":
        unavailable.append(
            {
                "check": "note_connection_counterfactual",
                "reason_code": (
                    "connection_evidence_partial"
                    if plan_summary["status"] == "partial_evidence"
                    else "performance_trace_insufficient"
                ),
            }
        )
    candidates = sorted(
        candidates,
        key=lambda item: (
            0 if item["level"] == "warning" else 1,
            item["code"],
            canonical_json_sha256(item["scope"]),
            item["candidate_id"],
        ),
    )
    reported = candidates[:candidate_limit]

    waveform_response = {
        "status": "unavailable",
        "reason_code": "event_isolated_envelope_evidence_not_recorded",
        "available_render_evidence": {
            "post_render_check": post_render_check is not None,
            "mix_report": mix_report is not None,
        },
        "global_metrics_used_as_naturalness_evidence": False,
        "explanation": (
            "全曲响度、峰值、crest、LRA 与频谱统计可以排查工程问题，但不能证明逐事件演奏响应自然。"
        ),
    }
    unavailable.append(
        {
            "check": "plan_to_waveform_event_response",
            "reason_code": waveform_response["reason_code"],
        }
    )

    evidence_coverage = (
        "partial"
        if score is None or plan_summary["status"] != "ready"
        else "complete_for_current_checks"
    )
    if reported:
        status = "review_candidates"
    elif evidence_coverage == "partial":
        status = "partial_evidence"
    else:
        status = "no_machine_candidate"
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "scope": "machine_triage_only",
        "status": status,
        "evidence_coverage": evidence_coverage,
        "bindings": bindings,
        "facts": {
            "phrase_coverage": phrase_summary,
            "performance_plan": plan_summary,
            "waveform_response": waveform_response,
        },
        "candidates": reported,
        "summary": {
            "candidate_count": len(candidates),
            "reported_candidate_count": len(reported),
            "warning_candidate_count": sum(
                item["level"] == "warning" for item in candidates
            ),
            "information_candidate_count": sum(
                item["level"] == "info" for item in candidates
            ),
            "candidates_truncated": len(candidates) > len(reported),
        },
        "unavailable": unavailable,
        "authority": {
            "audio_audition_performed": False,
            "naturalness_proven": False,
            "aesthetic_quality_proven": False,
            "automatic_change": False,
            "workflow_blocking": False,
            "intentional_mechanics_allowed": True,
        },
        "notice": (
            "机器未发现候选不等于听感自然；候选也不是错误。它们只用于把有限听审集中到可定位的位置。"
        ),
    }
    report["report_sha256"] = canonical_json_sha256(report)
    return report


__all__ = [
    "REPORT_FORMAT",
    "REPORT_VERSION",
    "analyze_performance_naturalness",
    "build_unavailable_performance_naturalness_report",
]
