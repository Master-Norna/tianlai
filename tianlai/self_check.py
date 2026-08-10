"""Shared contracts for strict blockers and creator-owned review findings.

The self-check boundary deliberately separates two questions:

* can Tianlai execute the request without violating an explicit contract, and
* is there evidence that a renderable musical choice deserves human review?

Only the first question may block.  Review findings never mutate a score,
silently change audio, or require a generic force flag.  Their stable IDs are
also suitable for a future UI acknowledgement that is bound to project hashes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .canonical_json import canonical_json_sha256


ISSUE_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
REVIEW_SCHEMA_URI = "https://tianlai.local/schemas/project-review.schema.json"

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_REVIEW_LEVELS = frozenset(("warning", "info"))
_SCOPE_KEYS = (
    "part_id",
    "executor_id",
    "executor_ids",
    "instrument_id",
    "instrument",
    "bar",
    "beat",
    "event_id",
    "source_event_id",
    "resource_family",
    "location",
)


def _category_for(code: str) -> str:
    prefix = code.split(".", 1)[0]
    if prefix in {"creative", "performance", "collaboration", "orchestration", "range"}:
        return "creative_context"
    if prefix in {"resource", "layout", "runtime", "capability", "distribution"}:
        return "execution_environment"
    if prefix in {"license", "trust", "instrument", "catalog", "availability"}:
        return "policy"
    return "contract"


def _scope_from_details(details: Mapping[str, Any]) -> dict[str, Any]:
    scope = {
        key: details[key]
        for key in _SCOPE_KEYS
        if key in details and details[key] is not None
    }
    return scope or {"kind": "project"}


def _stable_id(
    *,
    code: str,
    stage: str,
    scope: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    details: Mapping[str, Any],
) -> str:
    digest = canonical_json_sha256(
        {
            "code": code,
            "stage": stage,
            "scope": dict(scope),
            "evidence": None if evidence is None else dict(evidence),
            "details": dict(details),
        }
    )
    return f"selfcheck-{digest[:20]}"


def build_issue(
    *,
    severity: str,
    code: str,
    stage: str,
    message: object,
    category: str | None = None,
    basis: str | None = None,
    confidence: str | None = None,
    gate: str | None = None,
    scope: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    suggestions: Sequence[str] | None = None,
    **details: Any,
) -> dict[str, Any]:
    """Build one backward-compatible issue with explicit gate semantics."""

    if severity not in _SEVERITY_ORDER:
        raise ValueError("self-check severity must be error, warning, or info")
    blocking = severity == "error"
    decision = "block" if blocking else "review" if severity == "warning" else "inform"
    resolved_category = category or _category_for(code)
    resolved_scope = dict(scope) if scope is not None else _scope_from_details(details)
    resolved_basis = basis or ("contract" if blocking else "operational_state")
    resolved_confidence = confidence or ("high" if blocking else "medium")
    resolved_gate = gate or ("render" if blocking else "none")
    if suggestions is None:
        if blocking:
            resolved_suggestions = [
                "Fix the reported contract violation and run the self-check again."
            ]
        elif resolved_category == "creative_context":
            resolved_suggestions = [
                "Review the evidence and keep the musical choice when it is intentional."
            ]
        else:
            resolved_suggestions = ["Review this condition before the next render."]
    else:
        resolved_suggestions = [str(item) for item in suggestions]
    item: dict[str, Any] = {
        "issue_schema_version": ISSUE_SCHEMA_VERSION,
        "id": _stable_id(
            code=code,
            stage=stage,
            scope=resolved_scope,
            evidence=evidence,
            details=details,
        ),
        "severity": severity,
        "decision": decision,
        "blocking": blocking,
        "code": code,
        "category": resolved_category,
        "stage": stage,
        "basis": resolved_basis,
        "confidence": resolved_confidence,
        "gate": resolved_gate,
        "scope": resolved_scope,
        "message": str(message),
        "suggestions": resolved_suggestions,
        "override": {"mode": "forbidden" if blocking else "not_needed"},
        "automatic_change": False,
    }
    if evidence is not None:
        item["evidence"] = dict(evidence)
    reserved = frozenset(item)
    item.update(
        {
            key: value
            for key, value in details.items()
            if value is not None and key not in reserved
        }
    )
    return item


def paginate_issues(
    issues: Iterable[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    """Page issues blocker-first without losing complete severity counts."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("issue page limit must be a positive integer")
    materialized = list(issues)
    ordered = [
        item
        for _index, item in sorted(
            enumerate(materialized),
            key=lambda pair: (
                _SEVERITY_ORDER.get(str(pair[1].get("severity")), 99),
                pair[0],
            ),
        )
    ]
    counts = Counter(str(item.get("severity", "unknown")) for item in materialized)
    return ordered[:limit], dict(sorted(counts.items())), len(ordered) > limit


def summarize_issues(issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise hard gates without treating review findings as blockers."""

    materialized = list(issues)
    decision_counts = Counter(
        str(item.get("decision", "unknown")) for item in materialized
    )
    severity_counts = Counter(
        str(item.get("severity", "unknown")) for item in materialized
    )
    category_counts = Counter(
        str(item.get("category", "unknown")) for item in materialized
    )
    blocking_count = sum(
        1
        for item in materialized
        if item.get("blocking") is True or item.get("severity") == "error"
    )
    review_count = sum(
        1
        for item in materialized
        if item.get("decision") == "review"
        or (
            item.get("decision") is None
            and item.get("severity") == "warning"
        )
    )
    info_count = sum(
        1
        for item in materialized
        if item.get("decision") == "inform"
        or (
            item.get("decision") is None
            and item.get("severity") == "info"
        )
    )
    status = (
        "blocked"
        if blocking_count
        else "review_recommended"
        if review_count
        else "informational"
        if info_count
        else "clear"
    )
    return {
        "status": status,
        "can_proceed": blocking_count == 0,
        "blocking_count": blocking_count,
        "review_count": review_count,
        "advisory_count": info_count,
        "severity_counts": dict(sorted(severity_counts.items())),
        "decision_counts": dict(sorted(decision_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "policy": {
            "only_explicit_contract_failures_block": True,
            "warnings_block": False,
            "information_blocks": False,
            "automatic_changes": False,
        },
    }


def build_review_item(
    *,
    level: str,
    code: str,
    stage: str,
    message: object,
    basis: str,
    confidence: str,
    scope: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    suggestions: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one non-blocking creator review item."""

    if level not in _REVIEW_LEVELS:
        raise ValueError("project review level must be warning or info")
    resolved_scope = dict(scope or {"kind": "project"})
    resolved_evidence = None if evidence is None else dict(evidence)
    return {
        "id": _stable_id(
            code=code,
            stage=stage,
            scope=resolved_scope,
            evidence=resolved_evidence,
            details={},
        ),
        "level": level,
        "decision": "review" if level == "warning" else "inform",
        "blocking": False,
        "code": code,
        "stage": stage,
        "basis": basis,
        "confidence": confidence,
        "gate": "none",
        "scope": resolved_scope,
        "message": str(message),
        "evidence": resolved_evidence or {},
        "suggestions": [str(item) for item in suggestions],
        "override": {"mode": "not_needed"},
        "automatic_change": False,
    }


def build_review_report(
    items: Iterable[dict[str, Any]],
    *,
    binding: Mapping[str, Any] | None = None,
    max_items: int = 64,
) -> dict[str, Any]:
    """Return a bounded report whose findings never change renderability."""

    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("project review max_items must be a positive integer")
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = str(item.get("id", ""))
        if not identifier:
            raise ValueError("project review items require a stable id")
        if (
            item.get("level") not in _REVIEW_LEVELS
            or item.get("blocking") is not False
            or item.get("decision") not in {"review", "inform"}
            or item.get("gate") != "none"
            or item.get("automatic_change") is not False
        ):
            raise ValueError(
                "project review items must remain non-blocking and read-only"
            )
        unique.setdefault(identifier, item)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            0 if item.get("level") == "warning" else 1,
            str(item.get("code", "")),
            str(item.get("id", "")),
        ),
    )
    counts = Counter(str(item.get("level", "unknown")) for item in ordered)
    warning_count = counts.get("warning", 0)
    info_count = counts.get("info", 0)
    status = (
        "review_recommended"
        if warning_count
        else "informational"
        if info_count
        else "clear"
    )
    report: dict[str, Any] = {
        "$schema": REVIEW_SCHEMA_URI,
        "kind": "tianlai.project_review",
        "schema_version": REVIEW_SCHEMA_VERSION,
        "status": status,
        "review_recommended": warning_count > 0,
        "continuation_allowed": True,
        "blocking_count": 0,
        "review_count": warning_count,
        "advisory_count": info_count,
        "item_counts": dict(sorted(counts.items())),
        "items": ordered[:max_items],
        "items_truncated": len(ordered) > max_items,
        "policy": {
            "hard_contracts_reported_separately": True,
            "review_findings_block_render": False,
            "automatic_score_changes": False,
            "automatic_audio_changes": False,
            "generic_force_override": False,
        },
    }
    if binding is not None:
        report["binding"] = {
            str(key): value
            for key, value in binding.items()
            if value is not None
        }
    return report


__all__ = (
    "ISSUE_SCHEMA_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "REVIEW_SCHEMA_URI",
    "build_issue",
    "build_review_item",
    "build_review_report",
    "paginate_issues",
    "summarize_issues",
)
