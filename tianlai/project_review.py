"""Read-only project review that preserves unusual but renderable choices.

This module consumes an already compiled performance plan.  It never opens an
audio asset, mutates the score, changes the plan, or decides whether music is
good.  Findings are limited to declared instrument evidence, measured plan
behavior, and narrowly scoped orchestration topology candidates.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from .orchestration_topology import analyze_orchestration_topology
from .self_check import build_review_item, build_review_report


_RANGE_MESSAGES = {
    "range.outside_declared_hard_profile": (
        "该音仍可由兼容路径渲染，但位于当前配置声明的硬演奏画像之外；"
        "这可能是刻意的拉伸音色，请结合实际试听复核。"
    ),
    "range.quality_rejected": (
        "该配置的严格高质量画像曾被拒绝；兼容模式保留当前写法并建议试听复核。"
    ),
    "range.outside_current_hq_candidate": (
        "该音位于硬可演奏范围内，但超出当前高质量候选范围；边缘或拉伸音色"
        "可以保留，建议与核心音域做一次 A/B。"
    ),
    "range.profile_not_found": (
        "当前奏法或运行配置没有匹配的严格音域画像；兼容模式继续使用已声明的"
        "可演奏范围。"
    ),
    "range.quality_pending": (
        "当前配置的严格高质量证据仍在复核中；兼容模式继续渲染并保留创作选择。"
    ),
    "range.extended_or_nonidiomatic": (
        "该音位于乐器声明的惯用音域之外但仍在可演奏范围内；这是一项扩展技法"
        "提示，不是配器错误。"
    ),
}


def _range_code(contract: Mapping[str, Any]) -> str | None:
    status = str(contract.get("status", "unknown"))
    if status == "outside_hard_playable_range":
        return "range.outside_declared_hard_profile"
    if status == "quality_rejected":
        return "range.quality_rejected"
    if status == "outside_candidate_high_quality":
        return "range.outside_current_hq_candidate"
    if status == "profile_not_found":
        return "range.profile_not_found"
    if status == "quality_pending":
        return "range.quality_pending"
    coverage = contract.get("coverage")
    if (
        isinstance(coverage, dict)
        and coverage.get("idiomatic") is False
        and (
            coverage.get("hard_playable") is True
            or contract.get("legacy_covered") is True
        )
    ):
        return "range.extended_or_nonidiomatic"
    return None


def _range_review_items(plan: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    contract_count = 0
    for part in getattr(plan, "parts", ()):
        executor = part.executor
        executor_id = str(executor.executor_id)
        for trace in getattr(part, "trace", ()):
            derivation = trace.get("推导")
            if not isinstance(derivation, dict):
                continue
            contract = derivation.get("音域合同")
            if not isinstance(contract, dict):
                continue
            contract_count += 1
            code = _range_code(contract)
            if code is None:
                continue
            key = (executor_id, code)
            group = groups.setdefault(
                key,
                {
                    "executor_id": executor_id,
                    "part_id": str(executor.part_id),
                    "instrument": str(executor.capability.relative_path),
                    "code": code,
                    "count": 0,
                    "status_counts": Counter(),
                    "examples": [],
                },
            )
            group["count"] += 1
            group["status_counts"][str(contract.get("status", "unknown"))] += 1
            if len(group["examples"]) < 8:
                example = {
                    "bar": trace.get("小节"),
                    "beat": trace.get("拍"),
                    "pitch": trace.get("音"),
                    "status": contract.get("status"),
                    "profile_id": contract.get("profile_id"),
                    "coverage": contract.get("coverage"),
                }
                if trace.get("source_event_id") is not None:
                    example["event_id"] = trace["source_event_id"]
                group["examples"].append(example)

    items: list[dict[str, Any]] = []
    for group in groups.values():
        code = str(group["code"])
        items.append(
            build_review_item(
                level="warning",
                code=code,
                stage="range_review",
                basis="instrument_contract",
                confidence="high",
                scope={
                    "executor_id": group["executor_id"],
                    "part_id": group["part_id"],
                    "instrument": group["instrument"],
                },
                message=_RANGE_MESSAGES[code],
                evidence={
                    "affected_note_count": group["count"],
                    "status_counts": dict(sorted(group["status_counts"].items())),
                    "examples": group["examples"],
                    "examples_truncated": group["count"] > len(group["examples"]),
                    "range_mode": str(plan.expression.range_mode),
                },
                suggestions=(
                    "保留当前写法并先试听实际音色。",
                    "把相关音移入核心范围后做一次 A/B。",
                    "若目标不是边缘音色，可改写、显式移调或更换乐器。",
                ),
            )
        )
    return items, {
        "contract_count": contract_count,
        "finding_group_count": len(groups),
        "affected_note_count": sum(int(group["count"]) for group in groups.values()),
    }


def _performance_review_items(plan: Any) -> list[dict[str, Any]]:
    advisories = tuple(getattr(plan, "advisories", ()))
    items: list[dict[str, Any]] = []
    onset_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    represented_messages: set[str] = set()
    represented_codes: set[str] = set()
    for advisory in advisories:
        represented_messages.add(str(advisory.message))
        represented_codes.add(str(advisory.code))
        if advisory.code == "onset.compensation_clipped_at_zero":
            onset_groups[
                (
                    str(advisory.scope.get("executor_id", "unknown")),
                    str(advisory.scope.get("part_id", "unknown")),
                )
            ].append(advisory)
            continue
        items.append(
            build_review_item(
                level=advisory.level,
                code=advisory.code,
                stage="performance_plan",
                basis=advisory.basis,
                confidence=advisory.confidence,
                scope=advisory.scope,
                message=advisory.message,
                evidence=advisory.evidence,
                suggestions=advisory.suggestions,
            )
        )

    for (executor_id, part_id), group in onset_groups.items():
        examples = [
            {**advisory.scope, **advisory.evidence}
            for advisory in group[:8]
        ]
        items.append(
            build_review_item(
                level="warning",
                code="onset.compensation_clipped_at_zero",
                stage="performance_plan",
                basis="measurement",
                confidence="high",
                scope={"executor_id": executor_id, "part_id": part_id},
                message=(
                    "该执行器有发音补偿在时间零点被截断；渲染仍可继续，建议确认"
                    "开头是否需要预留空间。"
                ),
                evidence={
                    "affected_note_count": len(group),
                    "total_clipped_delay_seconds": round(
                        sum(
                            float(item.evidence.get("clipped_delay_seconds", 0.0))
                            for item in group
                        ),
                        9,
                    ),
                    "examples": examples,
                    "examples_truncated": len(group) > len(examples),
                },
                suggestions=(
                    "在开头预留空拍或空小节后重新自检。",
                    "若当前起音正是意图，可保留并试听确认。",
                ),
            )
        )

    # Preserve forward compatibility when the conductor gains a new warning
    # before a structured advisory is added for it.
    for warning in getattr(plan, "warnings", ()):
        warning_text = str(warning)
        structured_legacy_warning = (
            (
                "onset.compensation_clipped_at_zero" in represented_codes
                and ("发音补偿" in warning_text or "负时间" in warning_text)
            )
            or (
                "articulation.auto_dominant" in represented_codes
                and (
                    "articulation_auto=false" in warning_text
                    or "自动换奏法" in warning_text
                )
            )
        )
        if warning_text in represented_messages or structured_legacy_warning:
            continue
        items.append(
            build_review_item(
                level="info",
                code="performance.unclassified_advisory",
                stage="performance_plan",
                basis="unknown",
                confidence="low",
                message=warning_text,
                evidence={"legacy_warning": warning_text},
                suggestions=("结合演奏计划与实际试听复核该提示。",),
            )
        )
    return items


def _collaboration_review_items(roster: Any) -> list[dict[str, Any]]:
    executors = tuple(getattr(roster, "executors", ()))
    items: list[dict[str, Any]] = []
    unreviewed = sorted(
        {
            str(executor.capability.relative_path)
            for executor in executors
            if executor.capability.collaboration_review_status != "passed"
        }
    )
    if unreviewed:
        items.append(
            build_review_item(
                level="info",
                code="coverage.collaboration_unrecorded",
                stage="creative_context",
                basis="coverage",
                confidence="high",
                message=(
                    "部分乐器尚未记录此类组合的专门覆盖结论；这是一项覆盖信息，"
                    "不表示当前配器有误。"
                ),
                evidence={
                    "instrument_count": len(unreviewed),
                    "instruments": unreviewed[:16],
                    "instruments_truncated": len(unreviewed) > 16,
                },
                suggestions=("继续渲染并把有价值的试听反馈留给后续复核。",),
            )
        )

    if len(executors) > 1:
        for executor in executors:
            instrument = str(executor.capability.relative_path)
            role = getattr(executor, "role", None)
            if instrument == "世界乐器/西塔琴":
                items.append(
                    build_review_item(
                        level="warning",
                        code="balance.sitar_low_level_context",
                        stage="creative_context",
                        basis="listening_evidence",
                        confidence="medium",
                        scope={
                            "executor_id": str(executor.executor_id),
                            "part_id": str(executor.part_id),
                            "instrument": instrument,
                        },
                        message=(
                            "西塔琴在既有组合试听中电平偏轻；这不是音色故障，"
                            "请按当前作品中的角色复核平衡。"
                        ),
                        evidence={"gain_db": float(executor.gain_db)},
                        suggestions=(
                            "先试听分轨与总线中的实际存在感。",
                            "若需要突出，可用 gain_db 或自动化做作品内调整。",
                            "若轻远质感正是意图，可保持当前设置。",
                        ),
                    )
                )
            if (
                instrument == "管弦乐/弦乐组/大提琴"
                and role is not None
                and role.prominence == "background"
            ):
                items.append(
                    build_review_item(
                        level="warning",
                        code="balance.cello_background_masking_candidate",
                        stage="creative_context",
                        basis="listening_evidence",
                        confidence="medium",
                        scope={
                            "executor_id": str(executor.executor_id),
                            "part_id": str(executor.part_id),
                            "instrument": instrument,
                        },
                        message=(
                            "背景大提琴在既有试听中出现过长尾与低中频遮蔽候选；"
                            "系统不自动配平，请按当前作品复核。"
                        ),
                        evidence={"declared_role": role.to_dict()},
                        suggestions=(
                            "试听大提琴分轨、目标前景声部与总线。",
                            "按作品意图尝试 gain、自动化或空间位置 A/B。",
                            "若厚重背景正是目标，可保留当前设计。",
                        ),
                    )
                )

    collaboration = getattr(roster, "collaboration", None)
    if collaboration is not None and collaboration.mode in {"analyze", "suggest"}:
        missing_roles = [
            str(executor.executor_id)
            for executor in executors
            if getattr(executor, "role", None) is None
        ]
        if missing_roles:
            items.append(
                build_review_item(
                    level="info",
                    code="context.role_not_declared",
                    stage="creative_context",
                    basis="declared_intent",
                    confidence="high",
                    message=(
                        "分析模式下仍有执行器未声明角色；补充角色可让后续建议更贴近"
                        "创作者意图，但不会影响当前渲染。"
                    ),
                    evidence={
                        "executor_count": len(missing_roles),
                        "executor_ids": missing_roles[:16],
                        "executor_ids_truncated": len(missing_roles) > 16,
                    },
                    suggestions=("按需要补充 role.function 与 role.prominence。",),
                )
            )
    return items


def _topology_review_items(plan: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        topology = analyze_orchestration_topology(plan)
    except Exception as exc:
        return [
            build_review_item(
                level="info",
                code="diagnostics.orchestration_topology_unavailable",
                stage="orchestration_topology",
                basis="unknown",
                confidence="low",
                message="编配拓扑复核本次不可用；这不会改变项目的渲染合同。",
                evidence={"error_type": type(exc).__name__},
                suggestions=("可继续渲染，并在需要时单独重跑自检。",),
            )
        ], {"status": "unavailable"}

    items: list[dict[str, Any]] = []
    for warning in topology["warnings"]:
        code = f"orchestration.{warning['code']}"
        evidence = {
            key: value
            for key, value in warning.items()
            if key not in {"code", "message", "first_executor_id", "second_executor_id"}
        }
        items.append(
            build_review_item(
                level="warning",
                code=code,
                stage="orchestration_topology",
                basis="measurement",
                confidence="medium",
                scope={
                    "executor_ids": [
                        warning["first_executor_id"],
                        warning["second_executor_id"],
                    ],
                    "instrument": warning["instrument"],
                },
                message=warning["message"],
                evidence=evidence,
                suggestions=(
                    "试听两条分轨、总线与单声道折叠。",
                    "若齐奏叠加正是意图，可保留当前设计。",
                    "若不是意图，可改用独立音源变体或调整分谱。",
                ),
            )
        )
    return items, {
        "status": "ready",
        "format": topology["format"],
        "version": topology["version"],
        "summary": topology["summary"],
        "notice": topology["notice"],
    }


def build_project_review(
    plan: Any,
    roster: Any,
    *,
    binding: Mapping[str, Any] | None = None,
    max_items: int = 64,
) -> dict[str, Any]:
    """Build one deterministic review without changing plan or render gates."""

    range_items, range_summary = _range_review_items(plan)
    topology_items, topology_summary = _topology_review_items(plan)
    report = build_review_report(
        [
            *_performance_review_items(plan),
            *range_items,
            *_collaboration_review_items(roster),
            *topology_items,
        ],
        binding=binding,
        max_items=max_items,
    )
    report["diagnostics"] = {
        "range": range_summary,
        "orchestration_topology": topology_summary,
    }
    return report


def build_project_review_safely(
    plan: Any,
    roster: Any,
    *,
    binding: Mapping[str, Any] | None = None,
    max_items: int = 64,
) -> dict[str, Any]:
    """Keep an unexpected diagnostic failure outside the render gate."""

    try:
        return build_project_review(
            plan,
            roster,
            binding=binding,
            max_items=max_items,
        )
    except Exception as exc:
        report = build_review_report(
            [
                build_review_item(
                    level="info",
                    code="diagnostics.project_review_unavailable",
                    stage="project_review",
                    basis="runtime_diagnostic",
                    confidence="high",
                    message=(
                        "创作复核本次不可用；硬合同检查与演奏计划结果不受影响。"
                    ),
                    evidence={"error_type": type(exc).__name__},
                    suggestions=("可继续渲染，并单独重跑项目自检。",),
                )
            ],
            binding=binding,
            max_items=max_items,
        )
        report["diagnostics"] = {"status": "unavailable"}
        return report


__all__ = ("build_project_review", "build_project_review_safely")
