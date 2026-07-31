"""天籁 MCP 服务:把渲染内核做成"AI 手上的乐器"。

这不是一个胖函数("一句话→音频"),而是一小把细颗粒工具,让任何会调工具的
AI **反复演奏、边写边改**:先问调色板(能弹什么)→ 照格式写乐谱与编制 →
渲染 → 读客观仪表(峰值/平衡/削波)→ 改一处再渲。魂(确定性、可审计、分轨、
干净来源)全留着;AI 拿到的是**音频路径 + 客观测量**,而"好不好听"这一锤
始终留给人——分析是仪表,不是品味。

依赖隔离:只有本模块 import ``mcp``,核心引擎不受影响(``pip install
"tianlai-audio[mcp]"`` 才需要它)。

运行:``python -m tianlai.mcp_server``(stdio 传输,供 MCP 客户端接入)。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .candidate import (
    CANDIDATE_MANIFEST_NAME,
    canonical_json_sha256,
    compare_candidates,
    locate_candidate,
    prepare_candidate_target,
    publish_candidate_metadata,
)
from .capability import load_capabilities
from .conductor import ExpressionSettings, build_plan
from .ensemble import render_plan
from .path_policy import (
    InputPathPolicyError,
    discover_mcp_input_policy,
)
from .preflight import roster_availability_problems
from .project_import import (
    import_project as import_project_bundle,
    promote_roster as promote_imported_roster,
)
from .render_lock import RenderLockError
from .render_profile import (
    RenderProfile,
    parse_render_profile,
    profile_with_overrides,
)
from .resource_limits import (
    ResourceLimitError,
    validate_render_request_resource_limits,
    validate_score_resource_limits,
)
from .roster import parse_roster_document
from .runtime_layout import discover_runtime_layout
from .score import (
    parse_score_document,
    pitch_name,
    upgrade_legacy_score_to_v1,
)
from .score_ops import (
    ScoreOpsError,
    apply_score_patch,
    compare_scores,
    slice_score,
)
from .score_time import (
    coordinate_at_position,
    coordinate_at_seconds,
    seconds_window_around,
    validate_score_time_coordinates,
)
from .space import SpaceConfig
from .trust import (
    TrustPolicyError,
    load_trusted_instruments,
    load_variant_hints,
)

_RUNTIME_LAYOUT = discover_runtime_layout()
ROOT = _RUNTIME_LAYOUT.home
CATALOG = _RUNTIME_LAYOUT.catalog
ALLOWLIST_FILE = _RUNTIME_LAYOUT.allowlist
OUTPUT_DIR = _RUNTIME_LAYOUT.output / "mcp"

mcp = FastMCP("tianlai")

_caps_cache: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ProjectCompilation:
    """One read-only score/roster compilation shared by MCP inspection tools."""

    score: Any | None
    roster: Any | None
    settings: ExpressionSettings | None
    plan: Any | None
    checks: dict[str, dict[str, Any]]
    issues: tuple[dict[str, Any], ...]
    project: dict[str, str | None]

    @property
    def ok(self) -> bool:
        return self.plan is not None and not any(
            issue.get("severity") == "error" for issue in self.issues
        )


def _caps() -> dict[str, Any]:
    global _caps_cache
    if _caps_cache is None:
        _caps_cache = load_capabilities(CATALOG)
    return _caps_cache


def _trusted_set() -> set[str]:
    """Load and validate the curated palette; never silently fail open."""

    try:
        return set(load_trusted_instruments(ALLOWLIST_FILE, _caps()))
    except TrustPolicyError as exc:
        raise TrustPolicyError(
            f"{exc};trusted_only=true 已按 fail-closed 拒绝"
        ) from exc


def _variant_hints() -> dict[str, str]:
    """Return curated per-instrument usage hints from the same allowlist."""

    try:
        return load_variant_hints(ALLOWLIST_FILE)
    except TrustPolicyError:
        return {}


def _resolve_mcp_input(value: str) -> Path:
    """Authorise a local source path without granting ambient disk access."""

    return discover_mcp_input_policy(
        layout=_RUNTIME_LAYOUT,
    ).resolve_file(value)


def _articulation_range_contracts(cap: Any) -> dict[str, dict[str, Any]]:
    """Resolve every articulation to an Agent-friendly score-writing range.

    The legacy fields intentionally distinguish explicit declarations from
    inherited ranges.  That is useful for audits but easy for a score-writing
    Agent to misread.  This view resolves the inheritance while retaining its
    source, and pairs exact MIDI spans with readable pitch names.
    """

    explicit = dict(cap.articulation_playable_ranges)
    contracts: dict[str, dict[str, Any]] = {}
    for articulation in cap.articulations:
        ranges = cap.ranges_for(articulation)
        if articulation in explicit:
            source = "articulation_override"
        elif cap.playable_ranges:
            source = "instrument_playable_ranges"
        elif cap.note_min is not None and cap.note_max is not None:
            source = "instrument_note_bounds"
        else:
            source = "unspecified"
        contracts[articulation] = {
            "midi_ranges": [[low, high] for low, high in ranges],
            "note_ranges": [
                f"{pitch_name(low)}~{pitch_name(high)}"
                for low, high in ranges
            ],
            "source": source,
        }
    return contracts


def _range_diagnostic_summary(plan: Any) -> dict[str, Any]:
    """Compress per-note range contracts into an Agent-sized render receipt."""

    statuses: Counter[str] = Counter()
    attention: list[dict[str, Any]] = []
    by_executor_statuses: dict[str, Counter[str]] = {}
    by_executor_attention: dict[str, list[dict[str, Any]]] = {}
    by_executor_attention_count: Counter[str] = Counter()
    by_executor_contract_count: Counter[str] = Counter()
    attention_statuses = {
        "outside_hard_playable_range",
        "outside_candidate_high_quality",
        "profile_not_found",
        "quality_pending",
        "quality_rejected",
    }
    for part in plan.parts:
        executor_id = str(part.executor.executor_id)
        executor_statuses = by_executor_statuses.setdefault(
            executor_id,
            Counter(),
        )
        executor_attention = by_executor_attention.setdefault(
            executor_id,
            [],
        )
        for trace in part.trace:
            derivation = trace.get("推导")
            if not isinstance(derivation, dict):
                continue
            contract = derivation.get("音域合同")
            if not isinstance(contract, dict):
                continue
            status = str(contract.get("status", "unknown"))
            statuses[status] += 1
            executor_statuses[status] += 1
            by_executor_contract_count[executor_id] += 1
            if status not in attention_statuses:
                continue
            example = {
                "executor": executor_id,
                "bar": trace.get("小节"),
                "beat": trace.get("拍"),
                "pitch": trace.get("音"),
                "status": status,
                "profile_id": contract.get("profile_id"),
                "legacy_covered": contract.get("legacy_covered"),
            }
            by_executor_attention_count[executor_id] += 1
            if len(executor_attention) < 8:
                executor_attention.append(example)
            if len(attention) < 16:
                attention.append(example)
    by_executor = {
        executor_id: {
            "contract_count": by_executor_contract_count[executor_id],
            "status_counts": dict(
                sorted(by_executor_statuses[executor_id].items())
            ),
            "attention_count": by_executor_attention_count[executor_id],
            "attention_examples": by_executor_attention[executor_id],
            "attention_examples_truncated": (
                by_executor_attention_count[executor_id]
                > len(by_executor_attention[executor_id])
            ),
        }
        for executor_id in sorted(by_executor_statuses)
    }
    return {
        "mode": plan.expression.range_mode,
        "status_counts": dict(sorted(statuses.items())),
        "attention_count": sum(
            count
            for status, count in statuses.items()
            if status in attention_statuses
        ),
        "attention_examples": attention,
        "by_executor": by_executor,
        "semantics": (
            "compatibility 会保留旧可演奏范围并报告风险；strict_hq 对缺失、"
            "未批准、配置不匹配或超出当前高质量范围的音符直接拒绝。"
        ),
    }


def _assignment_instruments(assignment: dict) -> list[str]:
    """一个 assignment 涉及的所有乐器相对路径。

    普通声部走顶层 ``instrument``;**鼓组用 ``kit`` 把不同音符路由到不同打击
    乐器,没有顶层 instrument**。两种都要收齐,预检才不会把 kit 声部误判成
    "不可用乐器 None"。kit 的值可以是乐器路径字符串,或带 ``instrument`` 键的对象。
    """
    paths: list[str] = []
    top = assignment.get("instrument")
    if isinstance(top, str):
        paths.append(top)
    kit = assignment.get("kit")
    if isinstance(kit, dict):
        for value in kit.values():
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, dict) and isinstance(value.get("instrument"), str):
                paths.append(value["instrument"])
    return paths


def _roster_instrument_problems(roster: dict, trusted_only: bool) -> list[str]:
    """校验路径、许可隔离与可信状态；返回问题清单（空=通过）。

    ``trusted_only=false`` 只放开“尚未进入人工可信白名单”的候选，不会放开
    ``quarantined`` 或仅限本机兼容测试的 ``type=soundfont``。后两者都不能靠
    质量开关绕过。
    """
    # 保留这个原始 JSON 兼容入口供 MCP 与既有测试使用，但真正的策略只对
    # parse_roster_document 已解析出的 capability 执行。这样完整路径和唯一
    # 短名经过同一个 resolve_capability，不会出现核心认得、MCP 却说不存在。
    problems: list[str] = []
    assignments = roster.get("assignments", [])
    if not isinstance(assignments, list):
        return ["assignments must be a non-empty array"]
    for a in assignments:
        if not isinstance(a, dict):
            continue
        paths = _assignment_instruments(a)
        if not paths:
            problems.append(f"{a.get('executor_id', '?')}(既无 instrument 也无 kit)")
    if problems:
        return problems
    # 旧的兼容辅助函数只做“涉及哪些乐器”的预检，部分调用方测试数据没有
    # part（正式 render 随后仍会严格解析并拒绝）。为保持该入口的既有契约，
    # 仅在这份临时副本里补一个不会碰撞的声部 id，再交给统一解析/策略路径。
    normalized = dict(roster)
    normalized["assignments"] = [
        (
            {**assignment, "part": f"__availability_preflight_{position}"}
            if isinstance(assignment, dict) and not str(assignment.get("part", "")).strip()
            else assignment
        )
        for position, assignment in enumerate(assignments)
    ]
    try:
        parsed = parse_roster_document(normalized, _caps())
    except Exception as exc:
        # MCP 工具返回可修正的结构化错误，不让无效编制把服务调用本身打断。
        return [str(exc)]
    try:
        trusted = _trusted_set() if trusted_only else None
    except TrustPolicyError as exc:
        return [f"可信策略配置错误: {exc}"]
    return list(
        roster_availability_problems(
            parsed,
            trusted_only=trusted_only,
            trusted_instruments=trusted,
        )
    )


def _collaboration_warnings(roster: Any) -> list[str]:
    """Return non-blocking mix/context warnings for an already parsed roster."""

    executors = tuple(roster.executors)
    untested = sorted(
        {
            executor.capability.relative_path
            for executor in executors
            if executor.capability.collaboration_review_status != "passed"
        }
    )
    warnings: list[str] = []
    if untested:
        warnings.append(
            "该编制含尚未通过协奏/实际曲目验收的乐器；"
            "quality_tier=formal 只代表单音色独立测试通过。"
        )
    paths = {
        executor.capability.relative_path for executor in executors
    }
    if "世界乐器/西塔琴" in paths:
        warnings.append(
            "西塔琴已知单独听音色正常但电平偏小；"
            "请在当前编配中用 gain_db/自动化实听决定，不要据此全局改音色。"
        )
    if "管弦乐/弦乐组/大提琴" in paths:
        warnings.append(
            "大提琴作背景时曾出现过响、长尾和低中频遮蔽；"
            "请声明 role 与 balance_relations 后使用 analyze/suggest 诊断；"
            "当前引擎仍不会暗中自动配平或 ducking。"
        )
    return warnings


def _canonical_json_sha256(value: object) -> str | None:
    """Hash JSON data without accepting non-portable NaN/Infinity values."""

    try:
        return canonical_json_sha256(value)
    except (TypeError, ValueError):
        return None


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _issue(
    *,
    severity: str,
    code: str,
    stage: str,
    message: object,
    **details: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "stage": stage,
        "message": str(message),
    }
    item.update(
        {
            key: value
            for key, value in details.items()
            if value is not None
        }
    )
    return item


def _resolve_mcp_render_profile(
    *,
    render_profile: dict | None,
    seed: int | None,
    expression: str | None,
    range_mode: str | None,
    normalize_peak_db: float | None,
    hall: bool | None,
    master_gain_db: float | None,
    space_config: dict | None,
    collaboration_mode: str | None,
    write_stems: bool | None,
    use_stem_cache: bool | None,
    refresh_stem_cache: bool | None,
) -> RenderProfile:
    """Resolve the exact profile shared by MCP preflight and rendering."""

    if hall is not None and space_config is not None:
        raise ValueError("hall 与 space_config 不能同时覆盖 render_profile")
    explicit_space: SpaceConfig | bool | None
    if space_config is not None:
        explicit_space = SpaceConfig.from_dict(space_config)
        # ``SpaceConfig.from_dict`` accepts an explicit disabled object.  The
        # profile override API uses ``False`` to preserve that distinction.
        if explicit_space is None:
            explicit_space = False
    elif hall is not None:
        explicit_space = SpaceConfig() if hall else False
    else:
        explicit_space = None
    return profile_with_overrides(
        parse_render_profile(render_profile),
        seed=seed,
        expression=expression,
        range_mode=range_mode,
        normalize_peak_db=normalize_peak_db,
        master_gain_db=master_gain_db,
        space=explicit_space,
        collaboration_mode=collaboration_mode,
        write_stems=write_stems,
        use_stem_cache=use_stem_cache,
        refresh_stem_cache=refresh_stem_cache,
    )


def _compile_project(
    score: dict,
    roster: dict,
    *,
    expression: str,
    seed: int,
    range_mode: str,
    trusted_only: bool,
    write_stems: bool = True,
    space: SpaceConfig | None = None,
    collaboration_mode: str | None = None,
    stem_cache_enabled: bool = False,
) -> _ProjectCompilation:
    """Compile a project entirely in memory and retain every independent issue.

    This is deliberately not a light wrapper around :func:`render`: it never
    creates an output directory, opens an audio asset, instantiates an
    instrument backend, or calls ``render_plan``.
    """

    checks: dict[str, dict[str, Any]] = {
        "settings": {"status": "not_run"},
        "score_document": {"status": "not_run"},
        "score_time_coordinates": {"status": "not_run"},
        "resource_limits": {"status": "not_run"},
        "roster_document": {"status": "not_run"},
        "availability_policy": {"status": "not_run"},
        "cross_document": {"status": "not_run"},
        "performance_plan": {"status": "not_run"},
        "resources": {
            "status": "not_run",
            "level": "catalog_only",
            "ready_to_render": None,
            "reason_code": "audio_assets_not_opened",
        },
    }
    issues: list[dict[str, Any]] = []
    score_document = None
    roster_document = None
    settings = None
    plan = None
    normalized_seed: int | None = None

    try:
        normalized_seed = int(seed)
        settings = ExpressionSettings.from_dict(
            {
                "mode": expression,
                "range_mode": range_mode,
                "humanize": {"seed": normalized_seed},
            }
        )
    except Exception as exc:
        checks["settings"] = {"status": "failed"}
        issues.append(
            _issue(
                severity="error",
                code="settings.invalid",
                stage="settings",
                message=exc,
            )
        )
    else:
        checks["settings"] = {"status": "passed"}

    try:
        score_document = parse_score_document(score)
    except Exception as exc:
        checks["score_document"] = {"status": "failed"}
        checks["score_time_coordinates"] = {
            "status": "skipped",
            "blocked_by": ["score_document"],
        }
        issues.append(
            _issue(
                severity="error",
                code="score.parse_failed",
                stage="score_document",
                message=exc,
            )
        )
    else:
        checks["score_document"] = {"status": "passed"}
        try:
            validate_score_time_coordinates(score_document)
        except Exception as exc:
            checks["score_time_coordinates"] = {"status": "failed"}
            issues.append(
                _issue(
                    severity="error",
                    code="score.time_coordinate_invalid",
                    stage="score_time_coordinates",
                    message=exc,
                )
            )
        else:
            checks["score_time_coordinates"] = {"status": "passed"}
            try:
                score_resource_summary = validate_score_resource_limits(
                    score,
                    score_document,
                )
            except Exception as exc:
                checks["resource_limits"] = {"status": "failed"}
                issues.append(
                    _issue(
                        severity="error",
                        code=getattr(
                            exc,
                            "code",
                            "limits.score_invalid",
                        ),
                        stage="resource_limits",
                        message=exc,
                        actual=getattr(exc, "actual", None),
                        limit=getattr(exc, "limit", None),
                    )
                )
            else:
                checks["resource_limits"] = {
                    "status": "passed",
                    **score_resource_summary,
                }

    try:
        roster_document = parse_roster_document(roster, _caps())
    except Exception as exc:
        checks["roster_document"] = {"status": "failed"}
        checks["availability_policy"] = {
            "status": "skipped",
            "blocked_by": ["roster_document"],
        }
        issues.append(
            _issue(
                severity="error",
                code="roster.parse_failed",
                stage="roster_document",
                message=exc,
            )
        )
    else:
        checks["roster_document"] = {"status": "passed"}
        try:
            availability = roster_availability_problems(
                roster_document,
                trusted_only=trusted_only,
                trusted_instruments=(
                    _trusted_set() if trusted_only else None
                ),
            )
        except Exception as exc:
            availability = (str(exc),)
        if availability:
            checks["availability_policy"] = {"status": "failed"}
            issues.extend(
                _issue(
                    severity="error",
                    code="instrument.unavailable",
                    stage="availability_policy",
                    message=problem,
                )
                for problem in availability
            )
        else:
            checks["availability_policy"] = {"status": "passed"}

    cross_errors: list[dict[str, Any]] = []
    if score_document is None or roster_document is None:
        blocked_by = []
        if score_document is None:
            blocked_by.append("score_document")
        if roster_document is None:
            blocked_by.append("roster_document")
        checks["cross_document"] = {
            "status": "skipped",
            "blocked_by": blocked_by,
        }
    else:
        score_parts = {part.id for part in score_document.parts}
        assigned_parts = {
            executor.part_id for executor in roster_document.executors
        }
        dropped_parts = set(roster_document.dropped_parts)
        for part_id in sorted(assigned_parts - score_parts):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unknown_assigned_part",
                    stage="cross_document",
                    message=f"编制引用了总谱中不存在的声部 {part_id!r}",
                    part_id=part_id,
                )
            )
        for part_id in sorted(dropped_parts - score_parts):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unknown_dropped_part",
                    stage="cross_document",
                    message=f"drop_parts 引用了总谱中不存在的声部 {part_id!r}",
                    part_id=part_id,
                )
            )
        for part_id in sorted(
            score_parts - assigned_parts - dropped_parts
        ):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unassigned_part",
                    stage="cross_document",
                    message=f"总谱声部 {part_id!r} 既未指派乐器也未显式丢弃",
                    part_id=part_id,
                )
            )
        issues.extend(cross_errors)
        checks["cross_document"] = {
            "status": "failed" if cross_errors else "passed"
        }

    blocking_stages = [
        name
        for name in (
            "settings",
            "score_document",
            "score_time_coordinates",
            "resource_limits",
            "roster_document",
            "availability_policy",
            "cross_document",
        )
        if checks[name]["status"] != "passed"
    ]
    if blocking_stages:
        checks["performance_plan"] = {
            "status": "skipped",
            "blocked_by": blocking_stages,
        }
    else:
        try:
            plan = build_plan(
                score_document,
                roster_document,
                settings,
            )
        except Exception as exc:
            checks["performance_plan"] = {"status": "failed"}
            issues.append(
                _issue(
                    severity="error",
                    code="performance.compile_failed",
                    stage="performance_plan",
                    message=exc,
                )
            )
        else:
            try:
                render_resource_summary = (
                    validate_render_request_resource_limits(
                        plan,
                        write_stems=write_stems,
                        space=space,
                        collaboration_mode=collaboration_mode,
                        stem_cache_enabled=stem_cache_enabled,
                    )
                )
            except Exception as exc:
                preflight = getattr(exc, "preflight", None)
                checks["resource_limits"] = {
                    **checks["resource_limits"],
                    **(preflight if isinstance(preflight, dict) else {}),
                    "status": "failed",
                }
                checks["performance_plan"] = {"status": "passed"}
                issues.append(
                    _issue(
                        severity="error",
                        code=getattr(
                            exc,
                            "code",
                            "limits.plan_invalid",
                        ),
                        stage="resource_limits",
                        message=exc,
                        actual=getattr(exc, "actual", None),
                        limit=getattr(exc, "limit", None),
                    )
                )
            else:
                checks["performance_plan"] = {"status": "passed"}
                checks["resource_limits"] = {
                    **checks["resource_limits"],
                    **render_resource_summary,
                }
            for message in plan.warnings:
                issues.append(
                    _issue(
                        severity="warning",
                        code="performance.warning",
                        stage="performance_plan",
                        message=message,
                    )
                )

    if roster_document is not None:
        for message in _collaboration_warnings(roster_document):
            issues.append(
                _issue(
                    severity="warning",
                    code="collaboration.review_pending",
                    stage="cross_document",
                    message=message,
                )
            )

    settings_binding = {
        "expression": expression,
        "seed": normalized_seed if normalized_seed is not None else seed,
        "range_mode": range_mode,
        "trusted_only": bool(trusted_only),
    }
    project_binding = {
        "score": score,
        "roster": roster,
        "settings": settings_binding,
    }
    project: dict[str, str | None] = {
        "score_sha256": _canonical_json_sha256(score),
        "roster_sha256": _canonical_json_sha256(roster),
        "plan_input_sha256": _canonical_json_sha256(project_binding),
        "performance_plan_sha256": (
            _canonical_json_sha256(plan.to_dict())
            if plan is not None
            else None
        ),
    }
    stage_order = {
        name: index
        for index, name in enumerate(
            (
                "settings",
                "score_document",
                "score_time_coordinates",
                "resource_limits",
                "roster_document",
                "availability_policy",
                "cross_document",
                "performance_plan",
            )
        )
    }
    issues.sort(
        key=lambda item: (
            stage_order.get(str(item.get("stage")), 999),
            str(item.get("code", "")),
            str(item.get("part_id", "")),
            str(item.get("message", "")),
        )
    )
    return _ProjectCompilation(
        score=score_document,
        roster=roster_document,
        settings=settings,
        plan=plan,
        checks=checks,
        issues=tuple(issues),
        project=project,
    )


def _bounded_limit(value: int, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between 1 and 256")
    limit = int(value)
    if not 1 <= limit <= 256:
        raise ValueError(f"{field_name} must be between 1 and 256")
    return limit


def _validation_summary(compilation: _ProjectCompilation) -> dict[str, Any] | None:
    score = compilation.score
    roster = compilation.roster
    plan = compilation.plan
    if score is None or roster is None or plan is None:
        return None

    logical_end = 0.0
    for part in score.parts:
        for note in part.notes:
            meter = score.tempo_map.entry_at_bar(note.bar)
            end_quarter = (
                score.tempo_map.quarter_at(note.bar, note.beat)
                + note.duration_beats * meter.quarters_per_beat
            )
            logical_end = max(
                logical_end,
                score.tempo_map.seconds_at_quarter(end_quarter),
            )
    scheduled_gate_end = 0.0
    for part in plan.parts:
        for event in part.performance.get("events", []):
            if event.get("type") == "note_off":
                scheduled_gate_end = max(
                    scheduled_gate_end,
                    float(event.get("time", 0.0)),
                )
    return {
        "score_schema_version": score.schema_version,
        "score_part_count": len(score.parts),
        "assigned_part_count": len(
            {executor.part_id for executor in roster.executors}
        ),
        "dropped_part_count": len(roster.dropped_parts),
        "executor_count": len(roster.executors),
        "source_event_count": sum(
            len(part.notes) for part in score.parts
        ),
        "planned_note_count": sum(
            len(part.trace) for part in plan.parts
        ),
        "logical_music_end_seconds": round(logical_end, 9),
        "scheduled_gate_end_seconds": round(scheduled_gate_end, 9),
        "tail_seconds": score.tail_seconds,
        "total_plan_seconds": round(plan.duration_seconds, 9),
    }


def _render_preflight_summary(
    compilation: _ProjectCompilation,
) -> dict[str, Any]:
    resource_check = compilation.checks["resource_limits"]
    render_parameters = resource_check.get("render_parameters")
    if not isinstance(render_parameters, dict):
        return {
            "status": "not_run",
            "passed": None,
            "reason_code": "performance_plan_not_available",
        }
    # Keep this document byte-for-byte shape-compatible with the report
    # returned by the common preflight gate.  ``resource_limits`` also holds
    # score-document counters, which deliberately stay outside this render
    # report.
    fields = (
        "duration_seconds",
        "sample_rate",
        "executor_count",
        "frame_count",
        "estimated_audio_memory_bytes",
        "estimated_primary_output_bytes",
        "status",
        "passed",
        "render_parameters",
        "memory_model",
        "limits",
        "gates",
    )
    return {
        field: resource_check[field]
        for field in fields
        if field in resource_check
    }


def _instrument_policy_summary(
    compilation: _ProjectCompilation,
) -> list[dict[str, Any]]:
    roster = compilation.roster
    if roster is None:
        return []
    try:
        trusted = _trusted_set()
    except TrustPolicyError:
        trusted = None
    rows: dict[str, dict[str, Any]] = {}
    for executor in roster.executors:
        capability = executor.capability
        path = capability.relative_path
        if path in rows:
            continue
        rows[path] = {
            "instrument": path,
            "implementation_type": capability.implementation_type,
            "license_status": capability.license_status,
            "trusted": None if trusted is None else path in trusted,
            "collaboration_review_status": (
                capability.collaboration_review_status
            ),
            "decision": (
                "allowed"
                if compilation.checks["availability_policy"]["status"]
                == "passed"
                else "see_issues"
            ),
        }
    return [rows[path] for path in sorted(rows)]


def _issue_page(
    issues: tuple[dict[str, Any], ...],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    counts = Counter(str(item.get("severity", "unknown")) for item in issues)
    return (
        list(issues[:limit]),
        dict(sorted(counts.items())),
        len(issues) > limit,
    )


@mcp.tool()
def list_instruments(trusted_only: bool = True, pitched_only: bool = False) -> dict:
    """列出可用乐器调色板(配器前先调这个,照它的音域/奏法去写编制)。

    trusted_only=True(默认)只端出默认策展调色板；设 False
    可看全部未被许可证隔离的单音色入口。formal 只表示独立单音色测试通过，
    collaboration_review_status 才表示协奏/编配/混音验收状态。
    ``license_status=quarantined`` 与 ``type=soundfont`` 的本机兼容入口无论此
    参数为何值都不会列出。
    每件给出音域(音名)、奏法、实现类型、质量层和可选的 ``variant_hint``。
    ``playable_ranges`` 是
    显式声明的全局分段音域，``articulation_playable_ranges`` 只列显式的
    奏法覆盖；未列出的奏法继承全局分段，未声明分段时继承 note_min/note_max。
    写谱时必须先选奏法，再直接读取
    ``articulation_range_contracts[奏法].midi_ranges``；这里已经把继承解析完，
    并附有可读的 ``note_ranges``。顶层 ``range`` 仅是整件乐器的总包络。
    ``range_profiles`` 若存在，会进一步区分物理范围、升调扩展与当前高质量
    候选范围；不存在时 ``range_contract_status`` 明确为 unmigrated。
    ``duration_articulation_rules`` 只列乐器明确授权的无记号短音替换合同；
    空列表表示不能凭奏法名字自动猜测。
    ``pitch_mode=pitched`` 按谱面音高发声；``ignore`` 用谱面键位选择既有
    样本/变体但不做十二平均律移调；``fixed`` 则任意谱面音高都触发
    ``fixed_midi_note``，此时 ``ignores_pitch=true``。
    """
    try:
        trusted = _trusted_set() if trusted_only else None
    except TrustPolicyError as exc:
        return {
            "kind": "tianlai.instrument_list",
            "schema_version": 1,
            "ok": False,
            "count": 0,
            "instruments": [],
            "issues": [
                _issue(
                    severity="error",
                    code="trust_policy.configuration_error",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    variant_hints = _variant_hints()
    items = []
    for cap in _caps().values():
        if cap.quality_tier is None:  # 参考振荡器等非高仿入口不端出
            continue
        if cap.implementation_type == "soundfont":
            continue
        if cap.license_status == "quarantined":
            continue
        if trusted_only and cap.relative_path not in trusted:
            continue
        if pitched_only and not cap.pitched:
            continue
        lo = pitch_name(cap.note_min) if cap.note_min is not None else None
        hi = pitch_name(cap.note_max) if cap.note_max is not None else None
        capability_document = cap.to_dict()
        effective_pitch_mode = (
            cap.pitch_mode
            if cap.pitch_mode is not None
            else ("pitched" if cap.pitched else "unspecified")
        )
        items.append({
            "instrument": cap.relative_path,
            "category": cap.relative_path.split("/")[0],
            "name": cap.name,
            "implementation_type": cap.implementation_type,
            "pitched": cap.pitched,
            "pitch_mode": effective_pitch_mode,
            "pitch_mode_declared": cap.pitch_mode is not None,
            "fixed_midi_note": cap.fixed_midi_note,
            "fixed_note": (
                pitch_name(cap.fixed_midi_note)
                if cap.fixed_midi_note is not None
                else None
            ),
            "ignores_pitch": cap.ignores_pitch,
            "range": (f"{lo}~{hi}" if lo and hi else None),
            "note_min": cap.note_min,
            "note_max": cap.note_max,
            "playable_ranges": [
                [low, high] for low, high in cap.playable_ranges
            ],
            "articulations": list(cap.articulations),
            "default_articulation": cap.default_articulation,
            "duration_articulation_rules": capability_document[
                "duration_articulation_rules"
            ],
            "articulation_playable_ranges": {
                name: [[low, high] for low, high in ranges]
                for name, ranges in cap.articulation_playable_ranges
            },
            "articulation_range_contracts": (
                _articulation_range_contracts(cap)
            ),
            "range_contract_status": capability_document[
                "range_contract_status"
            ],
            "range_base_runtime_configuration": capability_document[
                "range_base_runtime_configuration"
            ],
            "range_profiles": capability_document["range_profiles"],
            "quality_tier": cap.quality_tier,
            "collaboration_review_status": (
                cap.collaboration_review_status
            ),
            "license_status": cap.license_status,
            "variant_hint": variant_hints.get(cap.relative_path),
        })
    items.sort(key=lambda x: x["instrument"])
    note = ("仅列默认策展乐器；传 trusted_only=false 看全部未隔离单音色入口"
            if trusted_only
            else "formal=单音色独立测试通过；协奏状态与许可、默认策展相互独立")
    return {
        "kind": "tianlai.instrument_list",
        "schema_version": 1,
        "ok": True,
        "count": len(items),
        "note": note,
        "agent_writing_rule": (
            "先选择 articulation，再按该乐器 articulation_range_contracts"
            "[articulation].midi_ranges 写音符；note_ranges 供人阅读。"
            "这是 compatibility 的基础可演奏范围；顶层 range 只是整件乐器的"
            "总包络，不能代替具体奏法音域。strict_hq 还须匹配 range_profiles。"
        ),
        "range_semantics": (
            "playable_ranges 仅含显式全局分段；"
            "articulation_playable_ranges 仅含显式奏法覆盖；未列奏法继承全局分段，"
            "没有显式分段时继承 note_min/note_max；range_profiles 若存在，"
            "会把物理/兼容扩展与当前高质量候选范围分开；"
            "articulation_range_contracts 已为每个奏法解析上述继承，"
            "应作为 AI 首次写谱的直接入口；duration_articulation_rules "
            "为空时不得凭 accent/normal 名字推断自动短音替换"
        ),
        "pitch_mode_semantics": {
            "pitched": "按谱面音高移调或选择对应音高样本",
            "ignore": (
                "谱面键位可选择既有打击样本/变体，但后端不按十二平均律移调"
            ),
            "fixed": (
                "任意谱面音高都触发 fixed_midi_note；"
                "ignores_pitch=true"
            ),
        },
        "instruments": items,
    }


@mcp.tool()
def score_and_roster_format() -> dict:
    """返回乐谱与编制的写法说明 + 一个最小可用示例。

    你(AI)据此直接写出 score 与 roster 两个 JSON 对象,再交给 render。
    铁律:bpm 恒数四分音符;beat/duration_beats 用拍号的拍单位(6/8 里一拍=八分)。
    velocity 在 (0,1];pitch 用科学音名如 "C4"/"F#3" 或 MIDI 数字。
    """
    example_score = {
        "schema_version": 1,
        "title": "示例", "sample_rate": 48000, "tail_seconds": 3.0,
        "tempo_map": [{"bar": 1, "beat": 1.0, "bpm": 72.0,
                       "beats_per_bar": 4, "beat_unit": 4}],
        "parts": [
            {"id": "Piano", "name": "Piano", "notes": [
                {"event_id": "piano-0001", "bar": 1, "beat": 1.0,
                 "duration_beats": 1.0, "pitch": "C4", "velocity": 0.5},
                {"event_id": "piano-0002", "bar": 1, "beat": 2.0,
                 "duration_beats": 1.0, "pitch": "E4", "velocity": 0.5},
                {"event_id": "piano-0003", "bar": 1, "beat": 3.0,
                 "duration_beats": 2.0, "pitch": "G4", "velocity": 0.6}]},
            {"id": "Flute", "name": "Flute", "notes": [
                {"event_id": "flute-0001", "bar": 1, "beat": 1.0,
                 "duration_beats": 4.0, "pitch": "C5", "velocity": 0.55}]},
        ],
    }
    example_roster = {
        "name": "示例编制",
        "collaboration": {
            "mode": "analyze",
            "analysis": {
                "metric": "overlap_active_rms",
                "window_ms": 400,
                "hop_ms": 100,
                "gate_dbfs": -60,
            },
            "balance_relations": [
                {
                    "subject": "Piano",
                    "reference": "Flute",
                    "target_offset_db": -4.0,
                    "tolerance_db": 2.0,
                    "max_suggestion_db": 3.0,
                }
            ],
        },
        "assignments": [
            {"part": "Piano", "executor_id": "1_钢琴", "instrument": "键盘乐器/钢琴",
             "gain_db": -4.0,
             "role": {"function": "harmony", "prominence": "midground"},
             "seat": {"azimuth_deg": -3, "distance_m": 2.5}},
            {"part": "Flute", "executor_id": "2_长笛", "instrument": "管弦乐/木管组/长笛",
             "gain_db": -6.0,
             "role": {"function": "lead", "prominence": "foreground"},
             "gain_automation": [
                 {"bar": 1, "beat": 1.0, "offset_db": 0.0},
                 {"bar": 1, "beat": 3.0, "offset_db": 1.5},
             ],
             "seat": {"azimuth_deg": -18, "distance_m": 4.0}},
        ],
    }
    return {
        "score_fields": {
            "schema_version": "新谱写 1；此时每个音符必须带全谱唯一的稳定 event_id。"
                              "旧谱可继续渲染，但局部编辑前应先调用 upgrade_score",
            "tempo_map": "至少一条,首条须在 bar1 beat1 且带 bpm/beats_per_bar/beat_unit;"
                         "后续条目可只带 bpm 做变速(rubato),小节中途变速用 beat!=1(不可带拍号)",
            "parts[].id": "声部标识,须与 roster 的 assignment.part 对应",
            "parts[].notes[]": "event_id(v1 必填且全谱唯一)、bar(1起)、beat(1起,拍单位)、"
                               "duration_beats、pitch、"
                               "可选 velocity(0,1]、可选 dynamic 记号、可选 articulation",
        },
        "roster_fields": {
            "assignments[].part": "对应 score 里的 part id",
            "assignments[].instrument": "来自 list_instruments 的 instrument 相对路径",
            "gain_db": "该声部电平(负值);seat.distance_m 越大在厅堂里越靠里",
            "gain_automation": "可选声部推子包络:[{bar,beat,offset_db},...];"
                               "首点必须 bar1 beat1,相邻点按真实时间在 dB 域线性插值。"
                               "它只改混音电平,不会像 velocity 一样改变音色",
            "role": "可选编制意图:{function,prominence,label?};function 可写 lead/"
                    "countermelody/harmony/pad/bass/rhythm/accent/texture/"
                    "ambience/effect/other，prominence 为 foreground/midground/"
                    "background。角色本身绝不自动改增益",
            "collaboration": "可选协奏诊断:{mode:manual|analyze|suggest,analysis,"
                             "part_groups?,balance_relations};关系端点可为 part id，"
                             "或创作者显式声明的非嵌套 part group。不得按名称猜组；"
                             "组只做求和分析，不是渲染总线。关系还须写目标相对 dB、"
                             "容差和建议上限，suggest 也不改音频",
            "dynamic_compression": "可选 0..1，把演奏 velocity 向 0.78 收拢；"
                                   "这是力度映射，不是音频压缩器",
            "duration_scale": "可选 0.1..2，缩放该声部音符发声时长，"
                              "可用于控制密集段落的尾音堆积",
            "overrides": "可选受控乐器参数。release_seconds 缩短主释音；"
                         "大提琴密集旋律可用 release_tail_gain=0..1 缩放或"
                         "关闭独立离弦尾采样；sample_variant 只选已审定变体",
            "pan_and_seat": "pan 是 -1..1 静态平衡；不写时由 seat.azimuth_deg "
                            "决定。seat.distance_m 当前只影响共享厅堂送出",
        },
        "rules": [
            "bpm 恒数四分音符,与拍号无关",
            "beat/duration_beats 用拍号的拍单位(6/8:一拍=八分音符)",
            "小节内 beat 使用半开区间；4/4 只能写 1<=beat<5，下一小节写 bar+1 beat1",
            "移动、改音高、改力度或改时值时保留原 event_id；新音符分配新的唯一 ID",
            "一件乐器一个 assignment;同一乐器别开多个实例(会相位互撞)",
            "先 list_instruments 确认音域,别写出乐器够不到的音",
            "不要从乐器名猜主次；在 roster.role 和 balance_relations 中显式声明",
            "不要从 Piano L/R、乐器名或轨道顺序猜组合端点；只有 roster 明确写入"
            " collaboration.part_groups 才可按组分析",
        ],
        "example_score": example_score,
        "example_roster": example_roster,
    }


@mcp.tool()
def import_midi(midi_path: str) -> dict:
    """把标准 MIDI 解析成 score 与待创作者确认的 roster 草稿。

    草稿保留 Program Change、CC7、CC10、CC11；不会自动选择天籁乐器或把
    MIDI 控制器猜成 dB。不产生音频。
    """
    from .midi_import import (  # 延迟导入,避免拉高启动
        build_roster_draft,
        read_midi,
    )
    try:
        path = _resolve_mcp_input(midi_path)
        score_doc, report = read_midi(path)
        parsed = parse_score_document(score_doc)
        roster_draft = build_roster_draft(score_doc, report)
    except InputPathPolicyError as exc:
        result = exc.to_result(stage="source_import")
        result["kind"] = "tianlai.midi_import_result"
        result["audio_rendered"] = False
        return result
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return {"error": f"MIDI 导入失败:{exc}"}
    parts = []
    for p in parsed.parts:
        ps = [n.midi for n in p.notes]
        parts.append({"id": p.id, "notes": len(p.notes),
                      "range": f"{pitch_name(min(ps))}~{pitch_name(max(ps))}" if ps else None})
    return {
        "score": score_doc,
        "roster_draft": roster_draft,
        "parts": parts,
        "warnings": list(report.warnings),
        "report": report.to_dict(),
    }


@mcp.tool()
def import_musicxml(musicxml_path: str) -> dict:
    """把 MusicXML 总谱解析成天籁乐谱，支持 .musicxml/.xml 与压缩 .mxl。

    保留谱面声部、和弦、多声部时序、拍号、速度、力度、常见奏法、连音线和
    移调乐器的实音；返回警告会明确列出当前没有展开的谱面语义。不产生音频。
    """
    from .musicxml_import import read_musicxml  # 延迟导入，保持 MCP 冷启动轻量

    try:
        path = _resolve_mcp_input(musicxml_path)
        score_doc, report = read_musicxml(path)
        parsed = parse_score_document(score_doc)
    except InputPathPolicyError as exc:
        result = exc.to_result(stage="source_import")
        result["kind"] = "tianlai.musicxml_import_result"
        result["audio_rendered"] = False
        return result
    except (OSError, ValueError, KeyError) as exc:
        return {"error": f"MusicXML 导入失败:{exc}"}
    parts = []
    for part in parsed.parts:
        pitches = [note.midi for note in part.notes]
        parts.append(
            {
                "id": part.id,
                "name": part.name,
                "notes": len(part.notes),
                "range": (
                    f"{pitch_name(min(pitches))}~{pitch_name(max(pitches))}"
                    if pitches
                    else None
                ),
            }
        )
    return {
        "score": score_doc,
        "parts": parts,
        "warnings": list(report.warnings),
        "report": report.to_dict(),
    }


@mcp.tool()
def import_score_project(
    source_path: str,
    trusted_only: bool = True,
    candidate_limit: int = 8,
) -> dict:
    """统一导入 MIDI/MusicXML/MXL，返回有 Hash 绑定的三文档工程包。

    返回 score、可持久化的 import_report 与明确 ``executable=false`` 的
    roster_draft。候选乐器只作有界提示，不会自动写入正式编制，也不会产生
    文件或音频。
    """

    try:
        path = _resolve_mcp_input(source_path)
        trusted = _trusted_set() if trusted_only else None
        bundle = import_project_bundle(
            path,
            capabilities=_caps(),
            trusted_only=trusted_only,
            trusted_instruments=trusted,
            candidate_limit=candidate_limit,
        )
    except InputPathPolicyError as exc:
        return {
            "kind": "tianlai.project_import_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [exc.to_issue(stage="source_import")],
        }
    except Exception as exc:
        return {
            "kind": "tianlai.project_import_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="project_import.failed",
                    stage="source_import",
                    message=exc,
                )
            ],
        }
    return {
        "kind": "tianlai.project_import_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "bundle": bundle,
    }


@mcp.tool()
def confirm_roster(
    score: dict,
    roster_draft: dict,
    assignments: list[dict],
    trusted_only: bool = True,
    name: str | None = None,
    collaboration: dict | None = None,
) -> dict:
    """把导入草稿提升为正式 roster；每个声部必须由创作者显式选择。

    普通声部提交 instrument，打击声部提交逐键 kit。工具会重验
    score/draft Hash、完整覆盖、乐器存在性、许可隔离与可信策略；候选提示、
    MIDI Program Change 或轨道名永远不会自动取得执行权限。
    """

    try:
        trusted = _trusted_set() if trusted_only else None
        roster = promote_imported_roster(
            roster_draft,
            score,
            assignments,
            _caps(),
            trusted_only=trusted_only,
            trusted_instruments=trusted,
            name=name,
            collaboration=collaboration,
        )
    except Exception as exc:
        return {
            "kind": "tianlai.roster_confirmation_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="roster_confirmation.failed",
                    stage="roster_confirmation",
                    message=exc,
                )
            ],
        }
    return {
        "kind": "tianlai.roster_confirmation_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "roster": roster,
        "assignment_count": len(roster["assignments"]),
    }


@mcp.tool()
def upgrade_score(score: dict) -> dict:
    """把 legacy score 升级为带稳定 event_id 的 score v1；不写文件、不渲染音频。

    已经是合法 v1 的输入会原样深拷贝返回。legacy 输入按原始数组遍历顺序分配
    ``event-000001`` 等稳定身份；保存升级结果后，后续移动、改音高、改力度或
    改时值时都应保留对应 ``event_id``。
    """

    before_version = (
        score.get("schema_version") if isinstance(score, dict) else None
    )
    try:
        upgraded = upgrade_legacy_score_to_v1(score)
        parsed = parse_score_document(upgraded)
        validate_score_time_coordinates(parsed)
    except Exception as exc:
        return {
            "kind": "tianlai.upgrade_score_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="score.upgrade_failed",
                    stage="score_document",
                    message=exc,
                )
            ],
        }
    changed = before_version != 1
    return {
        "kind": "tianlai.upgrade_score_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "changed": changed,
        "from_schema_version": before_version,
        "to_schema_version": 1,
        "score_sha256": _canonical_json_sha256(upgraded),
        "event_count": sum(len(part.notes) for part in parsed.parts),
        "score": upgraded,
        "warnings": (
            [
                "legacy 的人性化身份原先绑定数组下标；升级后绑定稳定 event_id，"
                "因此首次升级不承诺与 legacy 音频逐字节相同。保存 v1 后，"
                "未编辑事件在后续局部修改中会保持自己的随机身份。"
            ]
            if changed
            else []
        ),
    }


@mcp.tool()
def get_score_slice(score: dict, query: dict) -> dict:
    """按声部、event_id 或小节读取有界乐谱片段，不产生文件或音频。

    ``query`` 必须使用 ``kind=tianlai.score_slice_query``、
    ``schema_version=1``；可给 ``part_ids``、``event_ids``、
    ``bar_range={start,end}`` 和 ``max_notes``。匹配项过多时只返回有界摘要，
    不会把一个被截断的对象伪装成完整乐谱。
    """

    try:
        return slice_score(score, query)
    except ScoreOpsError as exc:
        return exc.to_dict()


@mcp.tool()
def patch_score(score: dict, patch: dict) -> dict:
    """用稳定 event_id 原子修改 score-v1，返回新乐谱、Hash 与结构化差异。

    Patch 必须绑定 ``base_score_sha256``，支持 ``update_note``、
    ``delete_note`` 与 ``add_note``。更新和删除可用 ``expect`` 声明旧值前置
    条件；任何 Hash/旧值冲突都会整批拒绝，不会部分套用。新增 event_id 由
    引擎确定性分配，现有 event_id 不可修改。
    """

    try:
        return apply_score_patch(score, patch)
    except ScoreOpsError as exc:
        return exc.to_dict()


@mcp.tool()
def compare_score_versions(
    before: dict,
    after: dict,
    max_changes: int = 256,
) -> dict:
    """按稳定 event_id 比较两份 score-v1；返回完整计数和有界差异样例。"""

    try:
        return compare_scores(
            before,
            after,
            max_changes=max_changes,
        )
    except ScoreOpsError as exc:
        return exc.to_dict()


@mcp.tool()
def validate_project(
    score: dict,
    roster: dict,
    expression: str | None = None,
    seed: int | None = None,
    range_mode: str | None = None,
    trusted_only: bool = True,
    max_issues: int = 64,
    render_profile: dict | None = None,
    normalize_peak_db: float | None = None,
    hall: bool | None = None,
    master_gain_db: float | None = None,
    space_config: dict | None = None,
    collaboration_mode: str | None = None,
    write_stems: bool | None = None,
    use_stem_cache: bool | None = None,
    refresh_stem_cache: bool | None = None,
) -> dict:
    """只编译并检查 score+roster，不实例化乐器、不产生音频或 output 文件。

    该入口检查文档结构、严格小节/拍坐标、许可与可信策略、跨文档声部路由以及
    指挥计划。资源状态明确标成 ``catalog_only``：本调用没有打开外部 WAV/SFZ，
    因而不会谎称资源已经 ready_to_render。资源预算按与 ``render`` 完全相同的
    render profile（包括共享厅堂、分轨、协奏分析和缓存设置）估算并明确报告
    当前参数是否过门。
    """

    try:
        limit = _bounded_limit(max_issues, "max_issues")
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.validate_project_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="query.invalid_limit",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    try:
        profile = _resolve_mcp_render_profile(
            render_profile=render_profile,
            seed=seed,
            expression=expression,
            range_mode=range_mode,
            normalize_peak_db=normalize_peak_db,
            hall=hall,
            master_gain_db=master_gain_db,
            space_config=space_config,
            collaboration_mode=collaboration_mode,
            write_stems=write_stems,
            use_stem_cache=use_stem_cache,
            refresh_stem_cache=refresh_stem_cache,
        )
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.validate_project_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="settings.invalid",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    compilation = _compile_project(
        score,
        roster,
        expression=profile.expression,
        seed=profile.seed,
        range_mode=profile.range_mode,
        trusted_only=trusted_only,
        write_stems=profile.write_stems,
        space=profile.space,
        collaboration_mode=profile.collaboration_mode,
        stem_cache_enabled=profile.use_stem_cache,
    )
    issues, counts, truncated = _issue_page(
        compilation.issues,
        limit,
    )
    resolved_profile = profile.to_dict()
    profile_sha256 = canonical_json_sha256(resolved_profile)
    return {
        "kind": "tianlai.validate_project_result",
        "schema_version": 1,
        "ok": compilation.ok,
        "audio_rendered": False,
        "project": compilation.project,
        "settings": {
            "expression": profile.expression,
            "seed": profile.seed,
            "range_mode": profile.range_mode,
            "trusted_only": trusted_only,
            "render_profile": resolved_profile,
            "render_profile_canonical_sha256": profile_sha256,
        },
        "render_handoff": {
            "render_profile": resolved_profile,
            "expected_render_profile_sha256": profile_sha256,
        },
        "checks": compilation.checks,
        "render_preflight": _render_preflight_summary(compilation),
        "summary": _validation_summary(compilation),
        "instrument_policy": _instrument_policy_summary(compilation),
        "range_diagnostics": (
            _range_diagnostic_summary(compilation.plan)
            if compilation.plan is not None
            else None
        ),
        "issues": issues,
        "issue_counts": counts,
        "issues_truncated": truncated,
    }


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


def _gain_at_seconds(part: Any, seconds: float) -> dict[str, float]:
    points = tuple(part.gain_envelope)
    if not points:
        offset = 0.0
    elif seconds <= points[0].time_seconds:
        offset = points[0].offset_db
    elif seconds >= points[-1].time_seconds:
        offset = points[-1].offset_db
    else:
        offset = points[-1].offset_db
        for left, right in zip(points, points[1:]):
            if left.time_seconds <= seconds <= right.time_seconds:
                span = right.time_seconds - left.time_seconds
                ratio = (seconds - left.time_seconds) / span
                offset = left.offset_db + ratio * (
                    right.offset_db - left.offset_db
                )
                break
    static = float(part.executor.gain_db)
    return {
        "static_db": round(static, 6),
        "automation_offset_db": round(float(offset), 6),
        "effective_db": round(static + float(offset), 6),
    }


def _located_events(
    compilation: _ProjectCompilation,
    *,
    at_seconds: float,
    window: Any,
    selected_parts: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Collect scheduled gate spans; acoustic release and hall tails stay out."""

    score = compilation.score
    plan = compilation.plan
    if score is None or plan is None:
        return [], {}
    source_notes = {
        note.source_event_id: (part.id, note)
        for part in score.parts
        for note in part.notes
        if note.source_event_id is not None
    }
    rows: list[dict[str, Any]] = []
    executor_counts: dict[str, dict[str, int]] = {}
    point_query = math.isclose(
        window.start_seconds,
        window.end_seconds,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    for part in plan.parts:
        executor = part.executor
        if (
            selected_parts is not None
            and executor.part_id not in selected_parts
        ):
            continue
        note_offs = {
            int(event["note_id"]): event
            for event in part.performance.get("events", [])
            if event.get("type") == "note_off"
        }
        traces_by_source = {
            trace.get("source_event_id"): trace
            for trace in part.trace
            if trace.get("source_event_id") is not None
        }
        for event in part.performance.get("events", []):
            if event.get("type") != "note_on":
                continue
            note_id = int(event["note_id"])
            release = note_offs.get(note_id)
            if release is None:
                continue
            scheduled_start = float(event["time"])
            scheduled_release = float(release["time"])
            active_at_anchor = (
                scheduled_start <= at_seconds < scheduled_release
            )
            overlaps = (
                active_at_anchor
                if point_query
                else (
                    scheduled_start < window.end_seconds
                    and scheduled_release > window.start_seconds
                )
            )
            if not overlaps:
                continue
            source_event_id = event.get("source_event_id")
            trace = (
                traces_by_source.get(source_event_id)
                if source_event_id is not None
                else (
                    part.trace[note_id - 1]
                    if 0 < note_id <= len(part.trace)
                    else {}
                )
            )
            if trace is None:
                trace = {}
            source = source_notes.get(source_event_id)
            written_note = source[1] if source is not None else None
            bar = int(trace.get("小节", written_note.bar if written_note else 1))
            beat = float(
                trace.get("拍", written_note.beat if written_note else 1.0)
            )
            logical = coordinate_at_position(
                score.tempo_map,
                bar,
                beat,
            )
            derivation = trace.get("推导")
            range_contract = (
                derivation.get("音域合同")
                if isinstance(derivation, dict)
                else None
            )
            sounding_midi = float(event["midi_note"])
            row = {
                "source": {
                    "part_id": executor.part_id,
                    "event_id": source_event_id,
                    "stable_identity": source_event_id is not None,
                },
                "executor_id": executor.executor_id,
                "instrument": executor.capability.relative_path,
                "note_id": note_id,
                "pitch": {
                    "written_midi": (
                        written_note.midi
                        if written_note is not None
                        else None
                    ),
                    "written_name": (
                        pitch_name(written_note.midi)
                        if written_note is not None
                        else None
                    ),
                    "sounding_midi": sounding_midi,
                    "sounding_name": pitch_name(sounding_midi),
                },
                "articulation": {
                    "written": (
                        written_note.articulation
                        if written_note is not None
                        else None
                    ),
                    "resolved": trace.get("奏法"),
                },
                "velocity": float(event.get("velocity", 0.0)),
                "logical": logical.to_dict(),
                "scheduled": {
                    "start_seconds": scheduled_start,
                    "release_seconds": scheduled_release,
                    "gate_duration_seconds": round(
                        scheduled_release - scheduled_start,
                        9,
                    ),
                    "delta_from_logical_ms": round(
                        (scheduled_start - logical.seconds) * 1000.0,
                        6,
                    ),
                },
                "relation": {
                    "active_at_anchor": active_at_anchor,
                    "starts_in_window": (
                        window.start_seconds
                        <= scheduled_start
                        < window.end_seconds
                    ),
                    "ends_in_window": (
                        window.start_seconds
                        < scheduled_release
                        <= window.end_seconds
                    ),
                },
                "range_status": (
                    range_contract.get("status")
                    if isinstance(range_contract, dict)
                    else None
                ),
            }
            rows.append(row)
            counts = executor_counts.setdefault(
                executor.executor_id,
                {"matched_event_count": 0, "active_event_count": 0},
            )
            counts["matched_event_count"] += 1
            if active_at_anchor:
                counts["active_event_count"] += 1
    rows.sort(
        key=lambda row: (
            row["scheduled"]["start_seconds"],
            row["source"]["part_id"],
            row["executor_id"],
            row["source"]["event_id"] or "",
            row["note_id"],
        )
    )
    return rows, executor_counts


@mcp.tool()
def locate(
    score: dict,
    roster: dict,
    at_seconds: float,
    before_seconds: float = 2.0,
    after_seconds: float = 2.0,
    part_ids: list[str] | None = None,
    expression: str = "ensemble",
    seed: int = 0,
    range_mode: str = "compatibility",
    trusted_only: bool = True,
    max_events: int = 64,
) -> dict:
    """按最终演奏计划的秒数窗口定位音符；只读，不渲染音频。

    返回的 ``scheduled`` 是 note_on 到 note_off 的门控区间；采样自身 release、
    共鸣和厅堂尾声不在其中。``logical`` 是同一谱面位置经 TempoMap 得到的纯谱面
    时间，不含结构表情、人性化或发音补偿。
    """

    try:
        limit = _bounded_limit(max_events, "max_events")
        anchor = _finite_nonnegative(at_seconds, "at_seconds")
        before = _finite_nonnegative(before_seconds, "before_seconds")
        after = _finite_nonnegative(after_seconds, "after_seconds")
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="query.invalid",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    compilation = _compile_project(
        score,
        roster,
        expression=expression,
        seed=seed,
        range_mode=range_mode,
        trusted_only=trusted_only,
    )
    if not compilation.ok:
        issues, counts, truncated = _issue_page(
            compilation.issues,
            limit,
        )
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "project": compilation.project,
            "checks": compilation.checks,
            "issues": issues,
            "issue_counts": counts,
            "issues_truncated": truncated,
        }
    score_document = compilation.score
    roster_document = compilation.roster
    plan = compilation.plan
    if score_document is None or roster_document is None or plan is None:
        raise RuntimeError("successful project compilation lost its documents")

    selected_parts = None
    if part_ids is not None:
        selected_parts = {
            str(part_id).strip()
            for part_id in part_ids
            if str(part_id).strip()
        }
        known_parts = {part.id for part in score_document.parts}
        unknown_parts = sorted(selected_parts - known_parts)
        if unknown_parts:
            return {
                "kind": "tianlai.locate_result",
                "schema_version": 1,
                "ok": False,
                "audio_rendered": False,
                "project": compilation.project,
                "issues": [
                    _issue(
                        severity="error",
                        code="query.unknown_part",
                        stage="settings",
                        message=(
                            "part_ids 含总谱中不存在的声部: "
                            + ", ".join(unknown_parts)
                        ),
                    )
                ],
            }
    try:
        window = seconds_window_around(
            anchor,
            before_seconds=before,
            after_seconds=after,
            maximum_seconds=plan.duration_seconds,
        )
        logical_anchor = coordinate_at_seconds(
            score_document.tempo_map,
            anchor,
        )
    except Exception as exc:
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "project": compilation.project,
            "issues": [
                _issue(
                    severity="error",
                    code="query.time_out_of_range",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    all_rows, executor_counts = _located_events(
        compilation,
        at_seconds=anchor,
        window=window,
        selected_parts=selected_parts,
    )
    rows = all_rows[:limit]
    returned_executors = {
        row["executor_id"] for row in rows
    }
    executors = []
    for part in plan.parts:
        executor = part.executor
        if executor.executor_id not in returned_executors:
            continue
        counts = executor_counts.get(executor.executor_id, {})
        executors.append(
            {
                "executor_id": executor.executor_id,
                "part_id": executor.part_id,
                "instrument": executor.capability.relative_path,
                "matched_event_count": counts.get(
                    "matched_event_count", 0
                ),
                "active_event_count": counts.get(
                    "active_event_count", 0
                ),
                "gain_at_anchor": _gain_at_seconds(part, anchor),
                "gain_semantics": (
                    "编制推子与自动化状态，不是实际音频响度预测"
                ),
                "pan": executor.pan,
                "role": (
                    executor.role.to_dict()
                    if executor.role is not None
                    else None
                ),
            }
        )
    legacy = score_document.schema_version is None
    non_error_issues = [
        issue
        for issue in compilation.issues
        if issue.get("severity") != "error"
    ]
    if legacy:
        non_error_issues.append(
            _issue(
                severity="warning",
                code="score.legacy_identity",
                stage="score_document",
                message=(
                    "该谱没有稳定 event_id；秒数仍可定位，但局部编辑前应先调用 "
                    "upgrade_score，之后才能跨修订可靠指向同一个音符。"
                ),
            )
        )
    return {
        "kind": "tianlai.locate_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "project": compilation.project,
        "time_semantics": {
            "logical": (
                "纯谱面位置经 TempoMap 换算，不含结构表情、人性化和发音补偿"
            ),
            "scheduled": "最终送入后端的 note_on 到 note_off 门控时间",
            "audible_tail_included": False,
        },
        "anchor": {
            "basis": "scheduled",
            "seconds": anchor,
            "nominal_logical_coordinate": logical_anchor.to_dict(),
            "warning": (
                "scheduled 秒数只能给出同一时钟位置的名义谱面坐标；"
                "具体事件以 events 内各自 logical/scheduled 字段为准"
            ),
        },
        "window": {
            "basis": "scheduled",
            **window.to_dict(),
        },
        "summary": {
            "matched_event_count": len(all_rows),
            "returned_event_count": len(rows),
            "truncated": len(all_rows) > limit,
        },
        "events": rows,
        "executors": executors,
        "issues": non_error_issues,
    }


def _candidate_output_path(value: str) -> Path:
    """Resolve one MCP render candidate without allowing output-root escape."""

    raw = Path(value).expanduser()
    candidate = (
        raw.resolve()
        if raw.is_absolute()
        else (OUTPUT_DIR / raw).resolve()
    )
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError as exc:
        raise ValueError(
            "候选必须位于当前天籁 MCP 输出目录内"
        ) from exc
    return candidate


@mcp.tool()
def locate_rendered_candidate(
    candidate_directory: str,
    at_seconds: float,
    tail_lookback_seconds: float = 5.0,
    upcoming_seconds: float = 2.0,
    max_events: int = 128,
) -> dict:
    """从已保存候选的回执和演奏计划定位实际听到的秒数。

    与 ``locate`` 重新编译当前 score/roster 不同，本工具校验候选中的
    score、roster、render-profile、演奏计划和渲染回执 Hash，再报告该候选
    在指定秒数的活动事件、可能仍有释音/厅堂贡献的近期事件和即将到来的事件。
    """

    try:
        return locate_candidate(
            _candidate_output_path(candidate_directory),
            at_seconds=at_seconds,
            tail_lookback_seconds=tail_lookback_seconds,
            upcoming_seconds=upcoming_seconds,
            max_events=max_events,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {
            "kind": "tianlai.candidate_locate_result",
            "schema_version": 1,
            "ok": False,
            "issues": [
                _issue(
                    severity="error",
                    code="candidate.locate_failed",
                    stage="candidate_receipt",
                    message=exc,
                )
            ],
        }


@mcp.tool()
def compare_rendered_candidates(
    before_candidate_directory: str,
    after_candidate_directory: str,
    max_changes: int = 256,
) -> dict:
    """比较两个不可变候选的乐谱、编制、配置、演奏计划和混音身份。"""

    try:
        return compare_candidates(
            _candidate_output_path(before_candidate_directory),
            _candidate_output_path(after_candidate_directory),
            max_changes=max_changes,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {
            "kind": "tianlai.candidate_compare_result",
            "schema_version": 1,
            "ok": False,
            "issues": [
                _issue(
                    severity="error",
                    code="candidate.compare_failed",
                    stage="candidate_receipt",
                    message=exc,
                )
            ],
        }


@mcp.tool()
def render(
    score: dict,
    roster: dict,
    title: str = "untitled",
    seed: int | None = None,
    expression: str | None = None,
    range_mode: str | None = None,
    normalize_peak_db: float | None = None,
    hall: bool | None = None,
    master_gain_db: float | None = None,
    space_config: dict | None = None,
    collaboration_mode: str | None = None,
    write_stems: bool | None = None,
    use_stem_cache: bool | None = None,
    refresh_stem_cache: bool | None = None,
    trusted_only: bool = True,
    render_profile: dict | None = None,
    output_id: str | None = None,
    parent_candidate_id: str | None = None,
    overwrite: bool = False,
    expected_receipt_sha256: str | None = None,
    expected_render_profile_sha256: str | None = None,
) -> dict:
    """把 score+roster 渲成 24bit 立体声 WAV(合奏 + 可选分轨),返回路径与客观仪表。

    仪表包含每声部峰值/复音、总线峰值、归一增益、时长和削波状态。显式选择
    collaboration_mode=analyze/suggest 时还会返回门控 active RMS、频带/立体声
    指标，以及 roster 明确声明的 balance_relations；suggest 只给有界建议，
    不会改动音频。它是机器排查，不代替人耳。厅堂默认开。
    trusted_only=True 时会拒绝白名单外的乐器并说明,方便只用验过的音色。
    许可证据为 quarantined 的入口始终拒绝，不能用该质量开关绕过。
    range_mode="compatibility" 保持旧可演奏范围并返回逐音风险摘要；
    "strict_hq" 对未获严格高质量证据或超出核心范围的音符直接拒绝。
    use_stem_cache=True 时会校验并复用增益前的原始乐器分轨；修改 gain、pan、
    厅堂或 master 只重新混音。refresh_stem_cache=True 强制重算原始分轨。
    validate_project 返回的 render_handoff 可原样传入；预期 profile Hash
    不一致时会在创建候选前拒绝，避免把不同配置的预检当成正式渲染依据。
    """
    try:
        profile = _resolve_mcp_render_profile(
            render_profile=render_profile,
            seed=seed,
            expression=expression,
            range_mode=range_mode,
            normalize_peak_db=normalize_peak_db,
            hall=hall,
            master_gain_db=master_gain_db,
            space_config=space_config,
            collaboration_mode=collaboration_mode,
            write_stems=write_stems,
            use_stem_cache=use_stem_cache,
            refresh_stem_cache=refresh_stem_cache,
        )
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": f"render_profile 无效: {exc}",
        }
    resolved_profile = profile.to_dict()
    profile_sha256 = canonical_json_sha256(resolved_profile)
    if expected_render_profile_sha256 is not None:
        if not _is_lower_sha256(expected_render_profile_sha256):
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "code": "render_profile.invalid_expected_sha256",
                "error": (
                    "expected_render_profile_sha256 必须是 64 位小写 "
                    "SHA-256"
                ),
                "render_profile_sha256": profile_sha256,
            }
        if expected_render_profile_sha256 != profile_sha256:
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "code": "render_profile.preflight_mismatch",
                "error": (
                    "正式渲染解析出的 render profile 与预检交接 Hash "
                    "不一致；请原样复用 validate_project.render_handoff"
                ),
                "expected_render_profile_sha256": (
                    expected_render_profile_sha256
                ),
                "render_profile_sha256": profile_sha256,
                "resolved_render_profile": resolved_profile,
            }
    seed = profile.seed
    expression = profile.expression
    range_mode = profile.range_mode
    normalize_peak_db = profile.normalize_peak_db
    master_gain_db = profile.master_gain_db
    collaboration_mode = profile.collaboration_mode
    write_stems = profile.write_stems
    use_stem_cache = profile.use_stem_cache
    refresh_stem_cache = profile.refresh_stem_cache

    # 前置校验:普通声部与鼓组 kit 涉及的乐器是否都存在、可信(见 _assignment_instruments)
    bad = _roster_instrument_problems(roster, trusted_only)
    if bad:
        return {"error": "编制里有不可用乐器", "offenders": bad}
    caps = _caps()

    try:
        score_doc = parse_score_document(score)
        validate_score_resource_limits(score, score_doc)
        roster_doc = parse_roster_document(roster, caps)
        settings = ExpressionSettings.from_dict(
            {
                "mode": expression,
                "range_mode": range_mode,
                "humanize": {"seed": int(seed)},
            }
        )
        plan = build_plan(score_doc, roster_doc, settings)
    except Exception as exc:  # 把校验错误如实回给 AI,让它改
        return {"error": f"乐谱/编制解析失败:{exc}"}

    try:
        resource_preflight = validate_render_request_resource_limits(
            plan,
            write_stems=profile.write_stems,
            space=profile.space,
            collaboration_mode=profile.collaboration_mode,
            stem_cache_enabled=profile.use_stem_cache,
        )
    except ResourceLimitError as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": str(exc),
            "render_preflight": exc.preflight,
            "render_profile_sha256": profile_sha256,
        }

    if not isinstance(overwrite, bool):
        return {"error": "overwrite 必须是布尔值"}
    plan_sha256 = canonical_json_sha256(plan.to_dict())
    try:
        candidate_target = prepare_candidate_target(
            OUTPUT_DIR,
            title,
            plan_sha256=plan_sha256,
            output_id=output_id,
            overwrite=overwrite,
            expected_receipt_sha256=expected_receipt_sha256,
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": str(exc),
        }
    work_id = candidate_target.work_id
    candidate_id = candidate_target.candidate_id
    directory = candidate_target.directory
    try:
        space = profile.space
        result = render_plan(
            plan,
            directory,
            write_stems=write_stems,
            master_gain_db=master_gain_db,
            normalize_peak_db=normalize_peak_db,
            space=space,
            collaboration_mode=collaboration_mode,
            stem_cache_directory=(
                OUTPUT_DIR.parent / ".tianlai-cache" / "stems"
                if use_stem_cache
                else None
            ),
            refresh_stem_cache=refresh_stem_cache,
            analysis_cache_directory=(
                OUTPUT_DIR.parent / ".tianlai-cache" / "analysis"
                if use_stem_cache
                else None
            ),
        )
    except (OSError, ValueError, RenderLockError) as exc:
        return {"error": str(exc)}

    mix_report = getattr(result, "mix_report", None)
    effective_collaboration_mode = getattr(
        result,
        "collaboration_mode",
        None,
    )
    if effective_collaboration_mode is None:
        effective_collaboration_mode = (
            collaboration_mode
            or (mix_report or {}).get("mode")
            or getattr(
                getattr(plan, "collaboration", None),
                "mode",
                "manual",
            )
        )
    metrics_by_executor = {
        row["executor_id"]: row["metrics"]
        for row in (mix_report or {}).get("stems", [])
    }
    candidate_manifest_path = directory / CANDIDATE_MANIFEST_NAME
    candidate_manifest: dict[str, Any] | None = None
    published_receipt = Path(result.receipt_path)
    if published_receipt.is_file():
        try:
            candidate_manifest = publish_candidate_metadata(
                candidate_target,
                title=title,
                score=score,
                roster=roster,
                render_profile=resolved_profile,
                receipt_path=published_receipt,
                plan_sha256=plan_sha256,
                parent_candidate_id=parent_candidate_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "error": f"音频已渲染，但候选元数据写入失败: {exc}",
                "candidate_id": candidate_id,
                "candidate_directory": str(directory),
                "render_receipt": result.receipt_path,
            }

    meter = {
        "kind": "tianlai.render_result",
        "schema_version": 2,
        "ok": True,
        "candidate_id": candidate_id,
        "parent_candidate_id": parent_candidate_id,
        "candidate_directory": str(directory),
        "candidate_manifest": (
            str(candidate_manifest_path)
            if candidate_manifest is not None
            else None
        ),
        "mix_wav": str(directory / "合奏.wav"),
        "performance_plan": getattr(result, "plan_path", None),
        "render_receipt": result.receipt_path,
        "mix_report_path": getattr(result, "mix_report_path", None),
        "mix_report": mix_report,
        "license_sidecar": result.license_sidecar_path,
        "attribution_notice": result.attribution_path,
        "stems_dir": str(directory / "分轨") if write_stems else None,
        "duration_seconds": round(result.duration_seconds, 2),
        "mix_peak": round(result.mix_peak, 4),
        "clipped": result.mix_peak > 1.0,
        "normalize_gain_db": round(result.normalize_gain_db, 2),
        "hall": space.to_dict() if space else None,
        "render_profile_sha256": profile_sha256,
        "render_preflight": resource_preflight,
        "parts": [{"executor": s.executor_id, "peak": round(s.peak, 4),
                   "peak_voices": s.peak_voices,
                   "mix_metrics": metrics_by_executor.get(s.executor_id)}
                  for s in result.stems],
        "resolved_render_options": {
            "render_profile": resolved_profile,
            "expression": expression,
            "range_mode": range_mode,
            "master_gain_db": master_gain_db,
            "normalize_peak_db": normalize_peak_db,
            "write_stems": write_stems,
            "use_stem_cache": use_stem_cache,
            "refresh_stem_cache": refresh_stem_cache,
            "space": space.to_dict() if space else None,
            "collaboration_mode": effective_collaboration_mode,
        },
        "collaboration_warnings": _collaboration_warnings(roster_doc),
        "range_diagnostics": _range_diagnostic_summary(plan),
        "stem_cache": getattr(result, "stem_cache", None),
        "analysis_cache": getattr(result, "analysis_cache", None),
        "cache_telemetry": getattr(
            result,
            "cache_telemetry_path",
            None,
        ),
        "note": (
            "这里只报告已测技术指标，不保证无缺陷、乐器真实性或作品质量；"
            "请以人耳判断为准。"
        ),
    }
    return meter


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
