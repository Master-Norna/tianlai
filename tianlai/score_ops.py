"""Deterministic, conflict-safe operations for editable score-v1 documents.

This module is deliberately independent from the CLI and MCP transport layers.
It accepts and returns JSON-compatible dictionaries, so those layers can expose
the same editing contract without duplicating musical validation or conflict
handling.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .canonical_json import canonical_json_bytes as _project_canonical_json_bytes
from .score import parse_score_document
from .score_time import validate_score_time_coordinates


SCHEMA_VERSION = 1
SCORE_SLICE_QUERY_KIND = "tianlai.score_slice_query"
SCORE_SLICE_RESULT_KIND = "tianlai.score_slice_result"
SCORE_PATCH_KIND = "tianlai.score_patch"
SCORE_PATCH_RESULT_KIND = "tianlai.score_patch_result"
SCORE_COMPARE_RESULT_KIND = "tianlai.score_compare_result"
SCORE_OPS_ERROR_KIND = "tianlai.score_ops_error"

DEFAULT_RESULT_LIMIT = 256
MAX_RESULT_LIMIT = 1024
MAX_FILTER_IDS = 4096
MAX_PATCH_OPERATIONS = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_QUERY_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "part_ids",
        "event_ids",
        "bar_range",
        "max_notes",
    }
)
_BAR_RANGE_KEYS = frozenset({"start", "end"})
_PATCH_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "base_score_sha256",
        "operations",
        "max_diff_entries",
    }
)
_UPDATE_KEYS = frozenset({"op", "event_id", "changes", "expect"})
_DELETE_KEYS = frozenset({"op", "event_id", "expect"})
_ADD_KEYS = frozenset({"op", "part_id", "note"})
_NOTE_KEYS = frozenset(
    {
        "event_id",
        "bar",
        "beat",
        "duration_beats",
        "pitch",
        "dynamic",
        "velocity",
        "articulation",
        "tie",
        "staff",
        "voice",
    }
)
_REQUIRED_NOTE_KEYS = frozenset({"bar", "beat", "duration_beats", "pitch"})
_INTEGER_NOTE_KEYS = frozenset({"bar", "staff"})
_MISSING = object()


class ScoreOpsError(ValueError):
    """A stable, machine-readable score-operation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = copy.deepcopy(details) if details else {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": SCORE_OPS_ERROR_KIND,
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = copy.deepcopy(self.details)
        return result


def _canonical_json_bytes(value: object, *, path: str) -> bytes:
    try:
        return _project_canonical_json_bytes(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ScoreOpsError(
            "non_canonical_json",
            f"{path} must contain only finite, JSON-compatible values: {exc}",
            details={"path": path},
        ) from exc


def _value_sha256(value: object, *, path: str) -> str:
    return hashlib.sha256(_canonical_json_bytes(value, path=path)).hexdigest()


def _validate_score_v1(score: object, *, path: str) -> dict[str, Any]:
    if not isinstance(score, dict):
        raise ScoreOpsError(
            "invalid_score",
            f"{path} must be a JSON object",
            details={"path": path},
        )
    _canonical_json_bytes(score, path=path)
    try:
        parsed = parse_score_document(score)
        validate_score_time_coordinates(parsed)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ScoreOpsError(
            "invalid_score",
            f"{path} is not a valid score-v1 document: {exc}",
            details={"path": path},
        ) from exc
    if parsed.schema_version != SCHEMA_VERSION:
        raise ScoreOpsError(
            "score_version_required",
            f"{path}.schema_version must be 1 for precise editing",
            details={"path": f"{path}.schema_version"},
        )
    return score


def _validated_score_hash(score: object, *, path: str) -> str:
    checked = _validate_score_v1(score, path=path)
    return _value_sha256(checked, path=path)


def canonical_score_sha256(score: dict[str, Any]) -> str:
    """Return the canonical SHA-256 of one fully validated score-v1 document."""

    return _validated_score_hash(score, path="score")


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: frozenset[str],
    *,
    path: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ScoreOpsError(
            "unknown_field",
            f"{path} contains unsupported fields: {', '.join(unknown)}",
            details={"path": path, "fields": unknown},
        )


def _require_protocol_header(
    value: dict[str, Any],
    *,
    expected_kind: str,
    path: str,
) -> None:
    if value.get("kind") != expected_kind:
        raise ScoreOpsError(
            "invalid_kind",
            f"{path}.kind must be {expected_kind!r}",
            details={
                "path": f"{path}.kind",
                "expected": expected_kind,
                "actual": value.get("kind"),
            },
        )
    version = value.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        raise ScoreOpsError(
            "unsupported_schema_version",
            f"{path}.schema_version must be {SCHEMA_VERSION}",
            details={
                "path": f"{path}.schema_version",
                "actual": version,
            },
        )


def _positive_limit(value: object, *, path: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_RESULT_LIMIT
    ):
        raise ScoreOpsError(
            "limit_out_of_range",
            f"{path} must be an integer between 1 and {MAX_RESULT_LIMIT}",
            details={"path": path, "maximum": MAX_RESULT_LIMIT},
        )
    return value


def _identifier_list(
    value: object,
    *,
    path: str,
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ScoreOpsError(
            "invalid_filter",
            f"{path} must be an array of non-empty strings",
            details={"path": path},
        )
    if len(value) > MAX_FILTER_IDS:
        raise ScoreOpsError(
            "filter_too_large",
            f"{path} must contain at most {MAX_FILTER_IDS} identifiers",
            details={"path": path, "maximum": MAX_FILTER_IDS},
        )
    identifiers: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ScoreOpsError(
                "invalid_filter",
                f"{path}[{index}] must be a non-empty string",
                details={"path": f"{path}[{index}]"},
            )
        if item in seen:
            raise ScoreOpsError(
                "duplicate_filter_id",
                f"{path} repeats identifier {item!r}",
                details={"path": path, "identifier": item},
            )
        identifiers.append(item)
        seen.add(item)
    return identifiers


def _normalize_slice_query(query: object) -> dict[str, Any]:
    if not isinstance(query, dict):
        raise ScoreOpsError(
            "invalid_query",
            "query must be a JSON object",
            details={"path": "query"},
        )
    _canonical_json_bytes(query, path="query")
    _reject_unknown_keys(query, _QUERY_KEYS, path="query")
    _require_protocol_header(
        query,
        expected_kind=SCORE_SLICE_QUERY_KIND,
        path="query",
    )
    part_ids = _identifier_list(query.get("part_ids"), path="query.part_ids")
    event_ids = _identifier_list(query.get("event_ids"), path="query.event_ids")
    if part_ids == []:
        raise ScoreOpsError(
            "invalid_filter",
            "query.part_ids must not be empty; omit it to select all parts",
            details={"path": "query.part_ids"},
        )

    normalized_range: dict[str, int] | None = None
    raw_range = query.get("bar_range")
    if raw_range is not None:
        if not isinstance(raw_range, dict):
            raise ScoreOpsError(
                "invalid_bar_range",
                "query.bar_range must be an object with start and end",
                details={"path": "query.bar_range"},
            )
        _reject_unknown_keys(
            raw_range,
            _BAR_RANGE_KEYS,
            path="query.bar_range",
        )
        start = raw_range.get("start")
        end = raw_range.get("end")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            raise ScoreOpsError(
                "invalid_bar_range",
                "query.bar_range must satisfy 1 <= start <= end",
                details={"path": "query.bar_range"},
            )
        normalized_range = {"start": start, "end": end}

    max_notes = _positive_limit(
        query.get("max_notes", DEFAULT_RESULT_LIMIT),
        path="query.max_notes",
    )
    normalized: dict[str, Any] = {
        "kind": SCORE_SLICE_QUERY_KIND,
        "schema_version": SCHEMA_VERSION,
        "max_notes": max_notes,
    }
    if part_ids is not None:
        normalized["part_ids"] = part_ids
    if event_ids is not None:
        normalized["event_ids"] = event_ids
    if normalized_range is not None:
        normalized["bar_range"] = normalized_range
    return normalized


def _raw_part_id(part: dict[str, Any]) -> str:
    return str(part.get("id", "")).strip()


def _all_event_ids(score: dict[str, Any]) -> set[str]:
    return {
        note["event_id"]
        for part in score["parts"]
        for note in part["notes"]
    }


def slice_score(
    score: dict[str, Any],
    query: dict[str, Any],
) -> dict[str, Any]:
    """Select score-v1 notes by intersecting part, event and inclusive bar filters.

    A selection that fits ``max_notes`` is returned as a complete score-v1
    fragment and is validated again.  A larger selection returns a bounded
    structured summary instead of pretending that a partial score is complete.
    """

    score_hash = _validated_score_hash(score, path="score")
    normalized = _normalize_slice_query(query)
    requested_parts = normalized.get("part_ids")
    requested_events = normalized.get("event_ids")
    bar_range = normalized.get("bar_range")
    max_notes = normalized["max_notes"]

    known_parts = {_raw_part_id(part) for part in score["parts"]}
    if requested_parts is not None:
        unknown_parts = sorted(set(requested_parts) - known_parts)
        if unknown_parts:
            raise ScoreOpsError(
                "part_not_found",
                f"query references unknown parts: {', '.join(unknown_parts)}",
                details={"part_ids": unknown_parts},
            )

    known_events = _all_event_ids(score)
    if requested_events is not None:
        unknown_events = sorted(set(requested_events) - known_events)
        if unknown_events:
            raise ScoreOpsError(
                "event_not_found",
                "query references unknown event IDs",
                details={"event_ids": unknown_events},
            )

    requested_part_set = (
        None if requested_parts is None else set(requested_parts)
    )
    requested_event_set = (
        None if requested_events is None else set(requested_events)
    )
    selected_parts: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    by_part: list[dict[str, Any]] = []
    matched_count = 0

    for part in score["parts"]:
        part_id = _raw_part_id(part)
        if requested_part_set is not None and part_id not in requested_part_set:
            continue
        selected = copy.deepcopy(part)
        selected_notes: list[dict[str, Any]] = []
        for note in part["notes"]:
            if (
                requested_event_set is not None
                and note["event_id"] not in requested_event_set
            ):
                continue
            if bar_range is not None and not (
                bar_range["start"] <= note["bar"] <= bar_range["end"]
            ):
                continue
            selected_notes.append(copy.deepcopy(note))
            if len(preview) < max_notes:
                preview.append(
                    {
                        "part_id": part_id,
                        "event_id": note["event_id"],
                        "bar": note["bar"],
                        "beat": note.get("beat", 1),
                        "duration_beats": note.get("duration_beats", 1),
                        "pitch": copy.deepcopy(note["pitch"]),
                    }
                )
        selected["notes"] = selected_notes
        selected_parts.append(selected)
        matched_count += len(selected_notes)
        by_part.append(
            {
                "part_id": part_id,
                "matched_note_count": len(selected_notes),
            }
        )

    base_result: dict[str, Any] = {
        "kind": SCORE_SLICE_RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "score_sha256": score_hash,
        "query": normalized,
        "matched_note_count": matched_count,
        "by_part": by_part,
    }
    if matched_count > max_notes:
        base_result.update(
            {
                "mode": "summary",
                "truncated": True,
                "event_preview": preview,
                "preview_note_count": len(preview),
            }
        )
        return base_result

    fragment = copy.deepcopy(score)
    fragment["parts"] = selected_parts
    _validate_score_v1(fragment, path="result.fragment")
    base_result.update(
        {
            "mode": "fragment",
            "truncated": False,
            "fragment": fragment,
            "fragment_sha256": _value_sha256(
                fragment,
                path="result.fragment",
            ),
        }
    )
    return base_result


def _score_note_index(
    score: dict[str, Any],
) -> tuple[
    dict[str, tuple[str, dict[str, Any], list[dict[str, Any]]]],
    dict[str, dict[str, Any]],
]:
    notes: dict[
        str,
        tuple[str, dict[str, Any], list[dict[str, Any]]],
    ] = {}
    parts: dict[str, dict[str, Any]] = {}
    for part in score["parts"]:
        part_id = _raw_part_id(part)
        parts[part_id] = part
        raw_notes = part["notes"]
        for note in raw_notes:
            notes[note["event_id"]] = (part_id, note, raw_notes)
    return notes, parts


def _note_field_state(note: dict[str, Any], field: str) -> dict[str, Any]:
    if field in note:
        return {"present": True, "value": copy.deepcopy(note[field])}
    return {"present": False}


def _note_field_changes(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field in sorted(set(before) | set(after)):
        if field == "event_id":
            continue
        before_state = _note_field_state(before, field)
        after_state = _note_field_state(after, field)
        if _canonical_json_bytes(
            before_state,
            path=f"before note field {field}",
        ) != _canonical_json_bytes(
            after_state,
            path=f"after note field {field}",
        ):
            changes[field] = {
                "before": before_state,
                "after": after_state,
            }
    return changes


def _score_metadata_shell(score: dict[str, Any]) -> dict[str, Any]:
    shell = copy.deepcopy(score)
    for part in shell["parts"]:
        part["notes"] = []
    return shell


def compare_scores(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    max_changes: int = DEFAULT_RESULT_LIMIT,
) -> dict[str, Any]:
    """Return a deterministic, bounded event-ID diff of two score-v1 documents."""

    limit = _positive_limit(max_changes, path="max_changes")
    before_hash = _validated_score_hash(before, path="before")
    after_hash = _validated_score_hash(after, path="after")
    before_notes, _ = _score_note_index(before)
    after_notes, _ = _score_note_index(after)

    changes: list[dict[str, Any]] = []
    counts = {
        "added": 0,
        "deleted": 0,
        "updated": 0,
        "reordered": 0,
        "metadata": 0,
    }

    before_shell = _score_metadata_shell(before)
    after_shell = _score_metadata_shell(after)
    before_shell_hash = _value_sha256(before_shell, path="before metadata")
    after_shell_hash = _value_sha256(after_shell, path="after metadata")
    if before_shell_hash != after_shell_hash:
        counts["metadata"] = 1
        changes.append(
            {
                "change": "metadata",
                "before_sha256": before_shell_hash,
                "after_sha256": after_shell_hash,
            }
        )

    for event_id, (part_id, note, _) in before_notes.items():
        candidate = after_notes.get(event_id)
        if candidate is None:
            counts["deleted"] += 1
            changes.append(
                {
                    "change": "deleted",
                    "event_id": event_id,
                    "part_id": part_id,
                    "before": copy.deepcopy(note),
                }
            )
            continue
        after_part_id, after_note, _ = candidate
        fields = _note_field_changes(note, after_note)
        if part_id != after_part_id or fields:
            counts["updated"] += 1
            changes.append(
                {
                    "change": "updated",
                    "event_id": event_id,
                    "part_id_before": part_id,
                    "part_id_after": after_part_id,
                    "field_changes": fields,
                    "before": copy.deepcopy(note),
                    "after": copy.deepcopy(after_note),
                }
            )

    for event_id, (part_id, note, _) in after_notes.items():
        if event_id not in before_notes:
            counts["added"] += 1
            changes.append(
                {
                    "change": "added",
                    "event_id": event_id,
                    "part_id": part_id,
                    "after": copy.deepcopy(note),
                }
            )

    before_part_orders = {
        _raw_part_id(part): [note["event_id"] for note in part["notes"]]
        for part in before["parts"]
    }
    after_part_orders = {
        _raw_part_id(part): [note["event_id"] for note in part["notes"]]
        for part in after["parts"]
    }
    for part_id in before_part_orders:
        if part_id not in after_part_orders:
            continue
        stable_ids = {
            event_id
            for event_id, (original_part_id, _, _) in before_notes.items()
            if (
                original_part_id == part_id
                and event_id in after_notes
                and after_notes[event_id][0] == part_id
            )
        }
        order_before = [
            event_id
            for event_id in before_part_orders[part_id]
            if event_id in stable_ids
        ]
        order_after = [
            event_id
            for event_id in after_part_orders[part_id]
            if event_id in stable_ids
        ]
        if order_before != order_after:
            counts["reordered"] += 1
            changes.append(
                {
                    "change": "reordered",
                    "part_id": part_id,
                    "event_count": len(order_before),
                    "before_order_sha256": _value_sha256(
                        order_before,
                        path=f"before part {part_id!r} order",
                    ),
                    "after_order_sha256": _value_sha256(
                        order_after,
                        path=f"after part {part_id!r} order",
                    ),
                    "before_preview": order_before[:16],
                    "after_preview": order_after[:16],
                    "preview_truncated": len(order_before) > 16,
                }
            )

    total = sum(counts.values())
    returned = changes[:limit]
    return {
        "kind": SCORE_COMPARE_RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "before_score_sha256": before_hash,
        "after_score_sha256": after_hash,
        "changed": before_hash != after_hash,
        "counts": counts,
        "total_change_count": total,
        "returned_change_count": len(returned),
        "changes_truncated": total > len(returned),
        "changes": returned,
    }


def _validate_patch(patch: object) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ScoreOpsError(
            "invalid_patch",
            "patch must be a JSON object",
            details={"path": "patch"},
        )
    _canonical_json_bytes(patch, path="patch")
    _reject_unknown_keys(patch, _PATCH_KEYS, path="patch")
    _require_protocol_header(
        patch,
        expected_kind=SCORE_PATCH_KIND,
        path="patch",
    )
    base_hash = patch.get("base_score_sha256")
    if not isinstance(base_hash, str) or not _SHA256_RE.fullmatch(base_hash):
        raise ScoreOpsError(
            "invalid_base_score_sha256",
            "patch.base_score_sha256 must be a lowercase SHA-256 digest",
            details={"path": "patch.base_score_sha256"},
        )
    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ScoreOpsError(
            "invalid_operations",
            "patch.operations must be a non-empty array",
            details={"path": "patch.operations"},
        )
    if len(operations) > MAX_PATCH_OPERATIONS:
        raise ScoreOpsError(
            "too_many_operations",
            f"patch.operations must contain at most {MAX_PATCH_OPERATIONS} items",
            details={
                "path": "patch.operations",
                "maximum": MAX_PATCH_OPERATIONS,
            },
        )
    _positive_limit(
        patch.get("max_diff_entries", DEFAULT_RESULT_LIMIT),
        path="patch.max_diff_entries",
    )
    return patch


def _require_event_id(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScoreOpsError(
            "invalid_event_id",
            f"{path} must be a non-empty string",
            details={"path": path},
        )
    return value


def _require_note_mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScoreOpsError(
            "invalid_note_fields",
            f"{path} must be a JSON object",
            details={"path": path},
        )
    return value


def _check_expectation(
    note: dict[str, Any],
    expect: object,
    *,
    path: str,
    event_id: str,
) -> None:
    if expect is None:
        return
    expected = _require_note_mapping(expect, path=path)
    _reject_unknown_keys(expected, _NOTE_KEYS, path=path)
    for field, expected_value in expected.items():
        actual_value = note.get(field, _MISSING)
        # ``null`` is the explicit, JSON-safe way to assert that an optional
        # field is absent.  Valid score-v1 notes never need an explicit null.
        if expected_value is None and actual_value is _MISSING:
            continue
        if (
            actual_value is _MISSING
            or isinstance(actual_value, bool) != isinstance(expected_value, bool)
            or (
                field in _INTEGER_NOTE_KEYS
                and (
                    isinstance(actual_value, bool)
                    or not isinstance(actual_value, int)
                    or isinstance(expected_value, bool)
                    or not isinstance(expected_value, int)
                )
            )
            or actual_value != expected_value
        ):
            actual: dict[str, Any]
            if actual_value is _MISSING:
                actual = {"present": False}
            else:
                actual = {
                    "present": True,
                    "value": copy.deepcopy(actual_value),
                }
            raise ScoreOpsError(
                "expectation_failed",
                f"precondition failed for event {event_id!r} field {field!r}",
                details={
                    "event_id": event_id,
                    "field": field,
                    "expected": copy.deepcopy(expected_value),
                    "actual": actual,
                },
            )


def _deterministic_event_id(
    *,
    base_score_sha256: str,
    operation_index: int,
    part_id: str,
    note: dict[str, Any],
    reserved_ids: set[str],
) -> str:
    nonce = 0
    while True:
        identity_material = {
            "base_score_sha256": base_score_sha256,
            "operation_index": operation_index,
            "part_id": part_id,
            "note": note,
            "nonce": nonce,
        }
        digest = _value_sha256(
            identity_material,
            path=f"patch.operations[{operation_index}] identity",
        )
        event_id = f"event-{digest[:24]}"
        if event_id not in reserved_ids:
            return event_id
        nonce += 1


def apply_score_patch(
    score: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Atomically apply a conflict-checked note patch to a score-v1 document."""

    before_hash = _validated_score_hash(score, path="score")
    checked_patch = _validate_patch(patch)
    expected_hash = checked_patch["base_score_sha256"]
    if expected_hash != before_hash:
        raise ScoreOpsError(
            "base_score_hash_mismatch",
            "patch was created for a different score revision",
            details={
                "expected": expected_hash,
                "actual": before_hash,
            },
        )

    edited = copy.deepcopy(score)
    note_index, part_index = _score_note_index(edited)
    reserved_ids = set(note_index)
    targeted_existing_ids: set[str] = set()
    operation_results: list[dict[str, Any]] = []

    for operation_index, raw_operation in enumerate(checked_patch["operations"]):
        path = f"patch.operations[{operation_index}]"
        if not isinstance(raw_operation, dict):
            raise ScoreOpsError(
                "invalid_operation",
                f"{path} must be a JSON object",
                details={"path": path},
            )
        operation = raw_operation.get("op")
        if operation == "update_note":
            _reject_unknown_keys(raw_operation, _UPDATE_KEYS, path=path)
            event_id = _require_event_id(
                raw_operation.get("event_id"),
                path=f"{path}.event_id",
            )
            if event_id in targeted_existing_ids:
                raise ScoreOpsError(
                    "duplicate_operation_target",
                    f"event {event_id!r} is targeted more than once",
                    details={"event_id": event_id},
                )
            candidate = note_index.get(event_id)
            if candidate is None:
                raise ScoreOpsError(
                    "event_not_found",
                    f"score has no event {event_id!r}",
                    details={"event_id": event_id},
                )
            _, note, _ = candidate
            _check_expectation(
                note,
                raw_operation.get("expect"),
                path=f"{path}.expect",
                event_id=event_id,
            )
            changes = _require_note_mapping(
                raw_operation.get("changes"),
                path=f"{path}.changes",
            )
            if not changes:
                raise ScoreOpsError(
                    "empty_update",
                    f"{path}.changes must not be empty",
                    details={"path": f"{path}.changes"},
                )
            _reject_unknown_keys(changes, _NOTE_KEYS, path=f"{path}.changes")
            if "event_id" in changes:
                raise ScoreOpsError(
                    "event_id_immutable",
                    "event_id cannot be modified, even to the same value",
                    details={"event_id": event_id},
                )
            for field, value in changes.items():
                if value is None:
                    if field in _REQUIRED_NOTE_KEYS:
                        raise ScoreOpsError(
                            "required_note_field",
                            f"{field} cannot be removed from a note",
                            details={"event_id": event_id, "field": field},
                        )
                    note.pop(field, None)
                else:
                    note[field] = copy.deepcopy(value)
            targeted_existing_ids.add(event_id)
            operation_results.append(
                {
                    "operation_index": operation_index,
                    "op": operation,
                    "event_id": event_id,
                    "status": "applied",
                }
            )
        elif operation == "delete_note":
            _reject_unknown_keys(raw_operation, _DELETE_KEYS, path=path)
            event_id = _require_event_id(
                raw_operation.get("event_id"),
                path=f"{path}.event_id",
            )
            if event_id in targeted_existing_ids:
                raise ScoreOpsError(
                    "duplicate_operation_target",
                    f"event {event_id!r} is targeted more than once",
                    details={"event_id": event_id},
                )
            candidate = note_index.get(event_id)
            if candidate is None:
                raise ScoreOpsError(
                    "event_not_found",
                    f"score has no event {event_id!r}",
                    details={"event_id": event_id},
                )
            _, note, containing_notes = candidate
            _check_expectation(
                note,
                raw_operation.get("expect"),
                path=f"{path}.expect",
                event_id=event_id,
            )
            containing_notes.remove(note)
            del note_index[event_id]
            targeted_existing_ids.add(event_id)
            operation_results.append(
                {
                    "operation_index": operation_index,
                    "op": operation,
                    "event_id": event_id,
                    "status": "applied",
                }
            )
        elif operation == "add_note":
            _reject_unknown_keys(raw_operation, _ADD_KEYS, path=path)
            part_id = raw_operation.get("part_id")
            if not isinstance(part_id, str) or not part_id.strip():
                raise ScoreOpsError(
                    "invalid_part_id",
                    f"{path}.part_id must be a non-empty string",
                    details={"path": f"{path}.part_id"},
                )
            part = part_index.get(part_id)
            if part is None:
                raise ScoreOpsError(
                    "part_not_found",
                    f"score has no part {part_id!r}",
                    details={"part_id": part_id},
                )
            note = copy.deepcopy(
                _require_note_mapping(
                    raw_operation.get("note"),
                    path=f"{path}.note",
                )
            )
            _reject_unknown_keys(note, _NOTE_KEYS, path=f"{path}.note")
            if "event_id" in note:
                raise ScoreOpsError(
                    "event_id_engine_owned",
                    "add_note must not provide event_id; the engine assigns it",
                    details={"path": f"{path}.note.event_id"},
                )
            event_id = _deterministic_event_id(
                base_score_sha256=before_hash,
                operation_index=operation_index,
                part_id=part_id,
                note=note,
                reserved_ids=reserved_ids,
            )
            note["event_id"] = event_id
            part["notes"].append(note)
            reserved_ids.add(event_id)
            note_index[event_id] = (part_id, note, part["notes"])
            operation_results.append(
                {
                    "operation_index": operation_index,
                    "op": operation,
                    "event_id": event_id,
                    "part_id": part_id,
                    "status": "applied",
                }
            )
        else:
            raise ScoreOpsError(
                "unsupported_operation",
                f"{path}.op must be update_note, delete_note or add_note",
                details={"path": f"{path}.op", "actual": operation},
            )

    try:
        after_hash = _validated_score_hash(edited, path="patched_score")
    except ScoreOpsError as exc:
        if exc.code == "non_canonical_json":
            raise
        raise ScoreOpsError(
            "patched_score_invalid",
            f"patch would produce an invalid score: {exc.message}",
            details={"cause": exc.to_dict()},
        ) from exc

    diff = compare_scores(
        score,
        edited,
        max_changes=checked_patch.get(
            "max_diff_entries",
            DEFAULT_RESULT_LIMIT,
        ),
    )
    return {
        "kind": SCORE_PATCH_RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "before_score_sha256": before_hash,
        "after_score_sha256": after_hash,
        "changed": before_hash != after_hash,
        "operation_results": operation_results,
        "score": edited,
        "diff": diff,
    }


__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "MAX_RESULT_LIMIT",
    "SCHEMA_VERSION",
    "SCORE_COMPARE_RESULT_KIND",
    "SCORE_OPS_ERROR_KIND",
    "SCORE_PATCH_KIND",
    "SCORE_PATCH_RESULT_KIND",
    "SCORE_SLICE_QUERY_KIND",
    "SCORE_SLICE_RESULT_KIND",
    "ScoreOpsError",
    "apply_score_patch",
    "canonical_score_sha256",
    "compare_scores",
    "slice_score",
]
