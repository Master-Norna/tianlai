"""Reusable authoring validation and catalogue facade.

This module is deliberately independent from the optional MCP extra.  Command
line tools, future interfaces, and tests can consume the same readiness and
instrument-catalogue decisions without importing an MCP transport.
"""

from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterable, Mapping

from .authoring_project import (
    AuthoringProjectState,
    RENDERS_DIRECTORY_NAME,
    validate_authoring_project_state,
)
from .authoring_roster import (
    AuthoringRosterError,
    parse_authoring_roster_document,
    project_authoring_roster_readiness,
    to_formal_roster,
)
from .canonical_json import canonical_json_sha256
from .capability import InstrumentCapability, load_capabilities
from .conductor import ExpressionSettings, build_plan
from .doctor import collect_doctor_report
from .preflight import enforce_roster_availability
from .project_review import build_project_review_safely
from .render_lock import capture_plain_directory, revalidate_plain_directory
from .render_profile import parse_render_profile
from .resource_limits import validate_render_request_resource_limits
from .roster import parse_roster_document
from .runtime_layout import discover_runtime_layout
from .score import ScoreDocument, parse_score_document
from .score_time import validate_score_time_coordinates
from .self_check import build_review_report


SNAPSHOT_KIND = "tianlai.authoring_project_snapshot"
SNAPSHOT_VERSION = 1
INSTRUMENT_CATALOG_KIND = "tianlai.instrument_catalog"
INSTRUMENT_CATALOG_VERSION = 1
MAX_READINESS_ISSUES = 128
MAX_INSTRUMENT_PAGE_SIZE = 256
READINESS_ISSUE_SOURCES = frozenset(
    {
        "project",
        "score",
        "authoring_roster",
        "render_profile",
        "resources",
        "project_review",
        "output",
    }
)
_SAFE_INTEGER = 9_007_199_254_740_991
_CODE_PATTERN = re.compile(r"^[a-z0-9_]+(?:[._][a-z0-9_]+)*$")


def _issue(
    code: str,
    *,
    source: str,
    severity: str,
    decision: str,
    location: Iterable[str | int] = (),
) -> dict[str, Any]:
    stable_code = code if _CODE_PATTERN.fullmatch(code) else "project.invalid"
    stable_source = source if source in READINESS_ISSUE_SOURCES else "project"
    safe_segments: list[str | int] = []
    for segment in location:
        if isinstance(segment, bool):
            continue
        if isinstance(segment, int):
            if 0 <= segment <= _SAFE_INTEGER:
                safe_segments.append(segment)
            continue
        if isinstance(segment, str) and len(segment) <= 128 and "\x00" not in segment:
            safe_segments.append(segment)
    return {
        "code": stable_code,
        "message_key": "authoring." + stable_code.replace(".", "_"),
        "source": stable_source,
        "severity": severity,
        "decision": decision,
        "location": {"segments": safe_segments},
    }


def _score_duration_seconds(score: ScoreDocument) -> float:
    latest = 0.0
    for part in score.parts:
        for note in part.notes:
            entry = score.tempo_map.entry_at_bar(note.bar)
            start = score.tempo_map.quarter_at(note.bar, note.beat)
            end = start + note.duration_beats * entry.quarters_per_beat
            latest = max(latest, score.tempo_map.seconds_at_quarter(end))
    return float(latest + score.tail_seconds) if latest > 0.0 else 0.0


def _probe_output_writable(project_root: Path) -> bool:
    return _probe_plain_directory_writable(
        project_root / RENDERS_DIRECTORY_NAME
    )


def _resource_statuses(
    instrument_ids: Iterable[str],
) -> dict[str, str]:
    selected = tuple(sorted(set(instrument_ids)))
    if not selected:
        return {}
    layout = discover_runtime_layout(require_catalog=True)
    report = collect_doctor_report(
        layout=layout,
        verify_references=True,
        active_probes=False,
        selected_instrument_ids=selected,
    )
    statuses: dict[str, str] = {}
    for item in report.get("instruments", []):
        if not isinstance(item, dict):
            continue
        instrument_id = item.get("id")
        resource = item.get("resource")
        if isinstance(instrument_id, str) and isinstance(resource, dict):
            status = resource.get("status")
            if status in {"ready", "missing", "invalid"}:
                statuses[instrument_id] = status
    return statuses


def validate_project_readiness(
    state: AuthoringProjectState,
    *,
    project_root: str | os.PathLike[str],
    render_output_root: str | os.PathLike[str] | None = None,
    include_project_review: bool = False,
) -> dict[str, Any]:
    """Validate one revision; optionally expose its bounded read-only review."""

    if not isinstance(include_project_review, bool):
        raise TypeError("include_project_review must be boolean")
    issues: list[dict[str, Any]] = []
    project_review: dict[str, Any] | None = None
    documents = validate_authoring_project_state(state)
    score = parse_score_document(documents["score"])
    validate_score_time_coordinates(score)
    profile = parse_render_profile(documents["render_profile"])
    authoring = parse_authoring_roster_document(
        documents["authoring_roster"], score
    )
    route_readiness = project_authoring_roster_readiness(authoring, score)
    assigned_count = int(route_readiness["assigned_parts"])
    note_count = sum(len(part.notes) for part in score.parts)
    duration_seconds = _score_duration_seconds(score)

    for unassigned in route_readiness["unassigned"]:
        location = unassigned.get("location", [])
        issues.append(
            _issue(
                "authoring_roster.unassigned_part",
                source="authoring_roster",
                severity="error",
                decision="block",
                location=location if isinstance(location, list) else (),
            )
        )

    if route_readiness["ready"]:
        try:
            layout = discover_runtime_layout(require_catalog=True)
            capabilities = load_capabilities(layout.catalog)
            formal_document = to_formal_roster(authoring, score, capabilities)
            formal_roster = parse_roster_document(formal_document, capabilities)
            enforce_roster_availability(formal_roster)
            instrument_ids = {
                executor.capability.relative_path
                for executor in formal_roster.executors
            }
            statuses = _resource_statuses(instrument_ids)
            for instrument_id in sorted(instrument_ids):
                status = statuses.get(instrument_id, "invalid")
                if status == "ready":
                    continue
                issues.append(
                    _issue(
                        "resource.missing"
                        if status == "missing"
                        else "resource.invalid",
                        source="resources",
                        severity="error",
                        decision="block",
                        location=("assignments",),
                    )
                )

            settings = ExpressionSettings.from_dict(
                {
                    "mode": profile.expression,
                    "range_mode": profile.range_mode,
                    "humanize": {"seed": profile.seed},
                }
            )
            plan = build_plan(score, formal_roster, settings)
            duration_seconds = float(plan.duration_seconds)
            validate_render_request_resource_limits(
                plan,
                write_stems=profile.write_stems,
                space=profile.space,
                collaboration_mode=profile.collaboration_mode,
                stem_cache_enabled=profile.use_stem_cache,
            )
            project_review = build_project_review_safely(
                plan,
                formal_roster,
                score=score,
                include_performance_naturalness=include_project_review,
                binding={
                    "score_sha256": state.document_revisions["score"],
                    "roster_sha256": canonical_json_sha256(formal_document),
                    "performance_plan_sha256": canonical_json_sha256(
                        plan.to_dict()
                    ),
                },
                max_items=64,
            )
            for item in project_review.get("items", []):
                if not isinstance(item, dict):
                    continue
                if (
                    not include_project_review
                    and item.get("stage") == "performance_naturalness"
                ):
                    # Preserve the durable v1 snapshot/readiness bytes.  The
                    # MCP readiness endpoint opts into the new diagnostic and
                    # receives these review-only projections by default.
                    continue
                level = item.get("level")
                if level not in {"warning", "info"}:
                    continue
                code = item.get("code")
                issues.append(
                    _issue(
                        code if isinstance(code, str) else "review.finding",
                        source="project_review",
                        severity=level,
                        decision="review" if level == "warning" else "inform",
                        location=("score",),
                    )
                )
        except AuthoringRosterError as exc:
            issues.append(
                _issue(
                    exc.code,
                    source="authoring_roster",
                    severity="error",
                    decision="block",
                    location=exc.location_segments,
                )
            )
        except Exception:
            issues.append(
                _issue(
                    "project.render_preflight_failed",
                    source="project",
                    severity="error",
                    decision="block",
                )
            )

    root = Path(project_root).resolve(strict=False)
    output_probe_root = (
        root / RENDERS_DIRECTORY_NAME
        if render_output_root is None
        else Path(render_output_root).resolve(strict=False)
    )
    # ``_probe_output_writable`` accepts a project root because normal
    # snapshots always target ``<project>/renders``.  A trusted caller may use
    # a separately authorised output root, which is probed directly here.
    output_writable = (
        _probe_output_writable(root)
        if render_output_root is None
        else _probe_plain_directory_writable(output_probe_root)
    )
    if not output_writable:
        issues.append(
            _issue(
                "output.not_writable",
                source="output",
                severity="error",
                decision="block",
            )
        )

    truncated = len(issues) > MAX_READINESS_ISSUES
    bounded = issues[:MAX_READINESS_ISSUES]
    blocked = any(item["decision"] == "block" for item in bounded) or (
        truncated
        and any(item["decision"] == "block" for item in issues[MAX_READINESS_ISSUES:])
    )
    review_required = any(item["decision"] == "review" for item in bounded)
    status = "blocked" if blocked else "review" if review_required else "ready"
    result = {
        "status": status,
        "render_allowed": not blocked,
        "summary": {
            "part_count": len(score.parts),
            "note_count": note_count,
            "assigned_part_count": assigned_count,
            "duration_seconds": duration_seconds,
            "sample_rate": score.sample_rate,
        },
        "issues": bounded,
        "issues_truncated": truncated,
    }
    if include_project_review:
        if project_review is None:
            project_review = build_review_report(
                [],
                binding={
                    "score_sha256": state.document_revisions["score"],
                },
            )
            project_review["diagnostics"] = {
                "performance_naturalness": {
                    "format": "tianlai.performance_naturalness",
                    "version": 1,
                    "scope": "machine_triage_only",
                    "status": "not_run",
                }
            }
        result["project_review"] = project_review
    return result


def _probe_plain_directory_writable(directory: Path) -> bool:
    """Probe writes through a delete-on-close handle, never path cleanup."""

    try:
        identity = capture_plain_directory(directory)
        verified = revalidate_plain_directory(identity)
        with tempfile.TemporaryFile(dir=verified, mode="w+b") as probe:
            probe.write(b"ok\n")
            probe.flush()
            os.fsync(probe.fileno())
            status = os.fstat(probe.fileno())
            if not stat.S_ISREG(status.st_mode):
                return False
        revalidate_plain_directory(identity)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def build_authoring_snapshot(
    state: AuthoringProjectState,
    *,
    project_root: str | os.PathLike[str],
) -> dict[str, Any]:
    return {
        "kind": SNAPSHOT_KIND,
        "schema_version": SNAPSHOT_VERSION,
        "project": {
            "project_id": state.project_id,
            "title": state.title,
            "created_at_utc": state.created_at_utc,
            "updated_at_utc": state.updated_at_utc,
            "revision": state.revision,
            "document_revisions": dict(state.document_revisions),
        },
        "documents": state.detached_documents(),
        "readiness": validate_project_readiness(
            state, project_root=project_root
        ),
    }


def _instrument_allowed(capability: InstrumentCapability) -> bool:
    return (
        capability.license_status != "quarantined"
        and capability.implementation_type != "soundfont"
    )


def _category(capability: InstrumentCapability) -> str:
    return capability.relative_path.split("/", 1)[0]


def list_instruments(
    *,
    instrument_scope: str = "production",
    query: str | None = None,
    category: str | None = None,
    routing_class: str | None = None,
    articulation: str | None = None,
    pitch_mode: str | None = None,
    detail_level: str = "full",
    offset: int = 0,
    limit: int = 128,
) -> dict[str, Any]:
    """Return a path-free, deterministic authoring instrument page."""

    if instrument_scope not in {"production", "all"}:
        raise ValueError("invalid instrument_scope")
    if detail_level != "full":
        raise ValueError("detail_level must be full")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= _SAFE_INTEGER
    ):
        raise ValueError("invalid offset")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_INSTRUMENT_PAGE_SIZE
    ):
        raise ValueError("invalid limit")
    selectors = (query, category, routing_class, articulation, pitch_mode)
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or "\x00" in value
        )
        for value in selectors
    ):
        raise ValueError("invalid instrument selector")

    layout = discover_runtime_layout(require_catalog=True)
    capabilities = load_capabilities(layout.catalog)
    rows: list[InstrumentCapability] = []
    folded_query = query.strip().casefold() if query is not None else None
    for capability in capabilities.values():
        if (
            instrument_scope == "production"
            and capability.relative_path.startswith("测试工具/")
        ):
            continue
        if folded_query is not None and folded_query not in (
            capability.name + " " + capability.relative_path
        ).casefold():
            continue
        if category is not None and _category(capability) != category.strip():
            continue
        if routing_class is not None and capability.routing_class != routing_class.strip():
            continue
        if articulation is not None and articulation.strip() not in capability.articulations:
            continue
        effective_pitch_mode = capability.pitch_mode or (
            "pitched" if capability.pitched else "unpitched"
        )
        if pitch_mode is not None and effective_pitch_mode != pitch_mode.strip():
            continue
        rows.append(capability)
    rows.sort(
        key=lambda item: (
            _category(item).casefold(),
            item.name.casefold(),
            item.relative_path.casefold(),
            item.relative_path,
        )
    )
    total = len(rows)
    page = rows[offset : offset + limit]
    resource_status = _resource_statuses(
        capability.relative_path for capability in page
    )
    items: list[dict[str, Any]] = []
    for capability in page:
        allowed = _instrument_allowed(capability)
        ready = resource_status.get(capability.relative_path) == "ready"
        items.append(
            {
                "instrument_id": capability.relative_path,
                "display_name": capability.name,
                "category": _category(capability),
                "routing_class": capability.routing_class,
                "pitched": capability.pitched,
                "pitch_mode": capability.pitch_mode,
                "note_min": capability.note_min,
                "note_max": capability.note_max,
                "default_articulation": capability.default_articulation,
                "quality_tier": capability.quality_tier,
                "license_status": capability.license_status,
                "articulations": list(capability.articulations),
                "playable_ranges": [
                    [float(low), float(high)]
                    for low, high in capability.ranges_for()
                ],
                "availability": {
                    "render_allowed": allowed and ready,
                    "status": "available" if allowed and ready else "blocked",
                },
            }
        )
    returned = len(items)
    return {
        "kind": INSTRUMENT_CATALOG_KIND,
        "schema_version": INSTRUMENT_CATALOG_VERSION,
        "offset": offset,
        "limit": limit,
        "total": total,
        "returned": returned,
        "truncated": offset + returned < total,
        "items": items,
    }


__all__ = [
    "INSTRUMENT_CATALOG_KIND",
    "INSTRUMENT_CATALOG_VERSION",
    "MAX_INSTRUMENT_PAGE_SIZE",
    "MAX_READINESS_ISSUES",
    "READINESS_ISSUE_SOURCES",
    "SNAPSHOT_KIND",
    "SNAPSHOT_VERSION",
    "build_authoring_snapshot",
    "list_instruments",
    "validate_project_readiness",
]
