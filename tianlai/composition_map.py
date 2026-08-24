"""Current-work composition maps and a read-only whole-score mirror.

A composition map is the workflow-side bridge between an abstract work charter
and note-level authoring.  It contains only declarations about the current
work: stable sequence nodes, current-charter claim references and stable event
references from the current score.  Historical examples, preference labels and
automatic edits are deliberately outside this contract.

The inspector in this module reports score facts and deterministic questions.
It does not render, rank, score, diagnose aesthetic quality or mutate either
input.  In particular, an overlap, silence or uncovered range is surfaced as a
question rather than treated as a defective musical form.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .canonical_json import canonical_json_sha256
from .score import ScoreDocument, ScoreNote, parse_score_document
from .score_ops import ScoreOpsError, canonical_score_sha256


COMPOSITION_MAP_KIND = "tianlai.composition_map"
COMPOSITION_MAP_INSPECTION_KIND = "tianlai.composition_map_inspection"
COMPOSITION_MAP_ERROR_KIND = "tianlai.composition_map_error"
COMPOSITION_MAP_SCHEMA_VERSION = 1

MAX_NODES = 256
MAX_TEXT_BYTES = 4096
MAX_LIST_ITEMS = 256
MAX_EVENT_REFERENCES = 1024
MAX_LOCATION_EVENT_IDS = 128

_STABLE_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_MAP_KEYS = frozenset({"kind", "schema_version", "nodes"})
_NODE_REQUIRED_KEYS = frozenset({"node_id", "label", "function"})
_NODE_OPTIONAL_KEYS = frozenset(
    {
        "bar_range",
        "depends_on_claim_ids",
        "established_material",
        "preserve",
        "transform",
        "role_changes",
        "scarce_resources",
        "ending_response",
        "open_questions",
    }
)
_BAR_RANGE_KEYS = frozenset({"start", "end"})
_ESTABLISHED_MATERIAL_KEYS = frozenset({"event_ids"})
_ROLE_CHANGE_KEYS = frozenset({"part_id", "change"})


class CompositionMapError(ValueError):
    """A stable, machine-readable composition-map contract failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = copy.deepcopy(dict(details)) if details else {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": COMPOSITION_MAP_ERROR_KIND,
            "schema_version": COMPOSITION_MAP_SCHEMA_VERSION,
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = copy.deepcopy(self.details)
        return result


def _expect_object(value: object, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompositionMapError(
            "object_required",
            f"{path} must be an object",
            details={"path": path},
        )
    non_string = [key for key in value if not isinstance(key, str)]
    if non_string:
        raise CompositionMapError(
            "non_string_key",
            f"{path} contains a non-string key",
            details={"path": path},
        )
    return value


def _check_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    path: str,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        raise CompositionMapError(
            "missing_field",
            f"{path} is missing required fields: {', '.join(missing)}",
            details={"path": path, "fields": missing},
        )
    unknown = sorted(keys - required - optional)
    if unknown:
        raise CompositionMapError(
            "unknown_field",
            f"{path} contains unsupported fields: {', '.join(unknown)}",
            details={"path": path, "fields": unknown},
        )


def _text(
    value: object,
    *,
    path: str,
    maximum_bytes: int = MAX_TEXT_BYTES,
) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CompositionMapError(
            "invalid_text",
            f"{path} must be a non-empty string",
            details={"path": path},
        )
    text = value.strip()
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise CompositionMapError(
            "invalid_text",
            f"{path} must be valid UTF-8 text",
            details={"path": path},
        ) from exc
    if not text or size > maximum_bytes:
        raise CompositionMapError(
            "invalid_text",
            f"{path} must contain between 1 and {maximum_bytes} UTF-8 bytes",
            details={"path": path, "maximum_bytes": maximum_bytes},
        )
    return text


def _stable_id(value: object, *, path: str) -> str:
    identifier = _text(value, path=path, maximum_bytes=128)
    if _STABLE_ID.fullmatch(identifier) is None:
        raise CompositionMapError(
            "invalid_stable_id",
            f"{path} must be a lowercase stable identifier",
            details={"path": path},
        )
    return identifier


def _text_list(
    value: object,
    *,
    path: str,
    maximum_items: int = MAX_LIST_ITEMS,
    item_bytes: int = 2048,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise CompositionMapError(
            "invalid_text_list",
            f"{path} must be an array with at most {maximum_items} items",
            details={"path": path, "maximum_items": maximum_items},
        )
    result = [
        _text(item, path=f"{path}[{index}]", maximum_bytes=item_bytes)
        for index, item in enumerate(value)
    ]
    if len(set(result)) != len(result):
        raise CompositionMapError(
            "duplicate_list_item",
            f"{path} must not contain duplicate items",
            details={"path": path},
        )
    return result


def _positive_integer(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CompositionMapError(
            "positive_integer_required",
            f"{path} must be an integer starting at 1",
            details={"path": path},
        )
    return value


def _bar_range(value: object, *, path: str) -> dict[str, int] | None:
    if value is None:
        return None
    raw = _expect_object(value, path=path)
    _check_keys(raw, required=_BAR_RANGE_KEYS, path=path)
    start = _positive_integer(raw["start"], path=f"{path}.start")
    end = _positive_integer(raw["end"], path=f"{path}.end")
    if end < start:
        raise CompositionMapError(
            "invalid_bar_range",
            f"{path}.end must not precede {path}.start",
            details={"path": path, "start": start, "end": end},
        )
    return {"start": start, "end": end}


def _established_material(value: object, *, path: str) -> dict[str, list[str]]:
    if value is None:
        return {"event_ids": []}
    raw = _expect_object(value, path=path)
    _check_keys(raw, required=_ESTABLISHED_MATERIAL_KEYS, path=path)
    return {
        "event_ids": _text_list(
            raw["event_ids"],
            path=f"{path}.event_ids",
            maximum_items=MAX_EVENT_REFERENCES,
            item_bytes=256,
        )
    }


def _role_changes(value: object, *, path: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise CompositionMapError(
            "invalid_role_changes",
            f"{path} must be an array with at most {MAX_LIST_ITEMS} items",
            details={"path": path, "maximum_items": MAX_LIST_ITEMS},
        )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _expect_object(item, path=item_path)
        _check_keys(raw, required=_ROLE_CHANGE_KEYS, path=item_path)
        normalized = {
            "part_id": _text(
                raw["part_id"], path=f"{item_path}.part_id", maximum_bytes=256
            ),
            "change": _text(raw["change"], path=f"{item_path}.change"),
        }
        identity = (normalized["part_id"], normalized["change"])
        if identity in seen:
            raise CompositionMapError(
                "duplicate_role_change",
                f"{path} contains a duplicate role change",
                details={"path": item_path},
            )
        seen.add(identity)
        result.append(normalized)
    return result


def _normalize_node(value: object, *, index: int) -> dict[str, Any]:
    path = f"composition_map.nodes[{index}]"
    raw = _expect_object(value, path=path)
    _check_keys(
        raw,
        required=_NODE_REQUIRED_KEYS,
        optional=_NODE_OPTIONAL_KEYS,
        path=path,
    )
    preserve = _text_list(raw.get("preserve", []), path=f"{path}.preserve")
    transform = _text_list(raw.get("transform", []), path=f"{path}.transform")
    overlap = sorted(set(preserve) & set(transform))
    if overlap:
        raise CompositionMapError(
            "conflicting_node_directive",
            f"{path} cannot preserve and transform the same declaration",
            details={"path": path, "items": overlap},
        )
    ending = raw.get("ending_response")
    return {
        "node_id": _stable_id(raw["node_id"], path=f"{path}.node_id"),
        "label": _text(raw["label"], path=f"{path}.label", maximum_bytes=1024),
        "function": _text(raw["function"], path=f"{path}.function"),
        "bar_range": _bar_range(raw.get("bar_range"), path=f"{path}.bar_range"),
        "depends_on_claim_ids": _text_list(
            raw.get("depends_on_claim_ids", []),
            path=f"{path}.depends_on_claim_ids",
            maximum_items=MAX_LIST_ITEMS,
            item_bytes=256,
        ),
        "established_material": _established_material(
            raw.get("established_material"), path=f"{path}.established_material"
        ),
        "preserve": preserve,
        "transform": transform,
        "role_changes": _role_changes(
            raw.get("role_changes"), path=f"{path}.role_changes"
        ),
        "scarce_resources": _text_list(
            raw.get("scarce_resources", []), path=f"{path}.scarce_resources"
        ),
        "ending_response": (
            None
            if ending is None
            else _text(ending, path=f"{path}.ending_response")
        ),
        "open_questions": _text_list(
            raw.get("open_questions", []), path=f"{path}.open_questions"
        ),
    }


def normalize_composition_map(document: object) -> dict[str, Any]:
    """Return the canonical, default-expanded composition-map value.

    Unknown fields are rejected so the map cannot quietly acquire historical
    examples, preference labels or another parallel evidence ledger.  Node
    order is preserved because it is part of the declared sequence; object key
    order and omitted optional fields are not semantic.
    """

    raw = _expect_object(document, path="composition_map")
    _check_keys(raw, required=_MAP_KEYS, path="composition_map")
    if raw["kind"] != COMPOSITION_MAP_KIND:
        raise CompositionMapError(
            "invalid_kind",
            f"composition_map.kind must be {COMPOSITION_MAP_KIND!r}",
            details={"path": "composition_map.kind", "actual": raw["kind"]},
        )
    version = raw["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != COMPOSITION_MAP_SCHEMA_VERSION
    ):
        raise CompositionMapError(
            "unsupported_schema_version",
            "composition_map.schema_version must be the integer 1",
            details={"path": "composition_map.schema_version", "actual": version},
        )
    nodes = raw["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise CompositionMapError(
            "invalid_nodes",
            f"composition_map.nodes must contain between 1 and {MAX_NODES} nodes",
            details={"path": "composition_map.nodes", "maximum_items": MAX_NODES},
        )
    normalized_nodes = [
        _normalize_node(node, index=index) for index, node in enumerate(nodes)
    ]
    node_ids = [node["node_id"] for node in normalized_nodes]
    if len(set(node_ids)) != len(node_ids):
        raise CompositionMapError(
            "duplicate_node_id",
            "composition_map.nodes must use unique stable node_id values",
            details={"path": "composition_map.nodes"},
        )
    return {
        "kind": COMPOSITION_MAP_KIND,
        "schema_version": COMPOSITION_MAP_SCHEMA_VERSION,
        "nodes": normalized_nodes,
    }


def _claim_ids(value: object, *, path: str = "charter_claims") -> list[str]:
    if isinstance(value, Mapping):
        raw_ids = list(value.keys())
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        raw_ids = list(value)
    else:
        raise CompositionMapError(
            "invalid_charter_claims",
            f"{path} must be an object keyed by claim_id or an array of claim_id strings",
            details={"path": path},
        )
    result = [
        _text(item, path=f"{path}[{index}]", maximum_bytes=256)
        for index, item in enumerate(raw_ids)
    ]
    if len(set(result)) != len(result):
        raise CompositionMapError(
            "duplicate_claim_id",
            f"{path} must not contain duplicate claim_id values",
            details={"path": path},
        )
    return sorted(result)


def validate_composition_map(
    document: object,
    *,
    charter_claim_ids: Iterable[str] | Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a map, optionally requiring current claims.

    The optional strict claim check is useful at a commit boundary.  The
    read-only inspector intentionally does not use that strict mode: it must be
    able to report missing references as questions before a commit is allowed.
    """

    normalized = normalize_composition_map(document)
    if charter_claim_ids is not None:
        if isinstance(charter_claim_ids, Mapping):
            known = set(_claim_ids(charter_claim_ids))
        else:
            if isinstance(charter_claim_ids, (str, bytes, bytearray)):
                raise CompositionMapError(
                    "invalid_charter_claims",
                    "charter_claim_ids must be an iterable of claim_id strings",
                    details={"path": "charter_claim_ids"},
                )
            known = set(
                _claim_ids(list(charter_claim_ids), path="charter_claim_ids")
            )
        missing = sorted(
            {
                claim_id
                for node in normalized["nodes"]
                for claim_id in node["depends_on_claim_ids"]
                if claim_id not in known
            }
        )
        if missing:
            raise CompositionMapError(
                "claim_not_found",
                "composition map references claims outside the supplied current charter",
                details={"claim_ids": missing},
            )
    return normalized


def composition_map_sha256(document: object) -> str:
    """Return the canonical hash of the normalized composition map."""

    return canonical_json_sha256(normalize_composition_map(document))


def _position(note: ScoreNote, *, part_id: str, part_index: int) -> dict[str, Any]:
    return {
        "part_id": part_id,
        "event_id": note.source_event_id,
        "bar": note.bar,
        "beat": note.beat,
        "score_path": ["parts", part_index, "notes", note.index],
    }


def _bar_span(notes: Sequence[tuple[int, str, ScoreNote]]) -> dict[str, int] | None:
    if not notes:
        return None
    bars = [note.bar for _part_index, _part_id, note in notes]
    return {"start": min(bars), "end": max(bars)}


def _coordinate(note: ScoreNote, *, part_id: str) -> dict[str, Any]:
    return {
        "bar": note.bar,
        "beat": note.beat,
        "part_id": part_id,
        "event_id": note.source_event_id,
    }


def _compress_bars(bars: Iterable[int]) -> list[dict[str, int]]:
    ordered = sorted(set(bars))
    if not ordered:
        return []
    result: list[dict[str, int]] = []
    start = previous = ordered[0]
    for bar in ordered[1:]:
        if bar == previous + 1:
            previous = bar
            continue
        result.append({"start": start, "end": previous})
        start = previous = bar
    result.append({"start": start, "end": previous})
    return result


def _question(
    *,
    question_kind: str,
    prompt: str,
    basis: Mapping[str, Any],
    score_sha256: str,
    map_sha256: str,
    node_id: str | None = None,
    bar_range: Mapping[str, int] | None = None,
    event_ids: Sequence[str] = (),
    part_ids: Sequence[str] = (),
) -> dict[str, Any]:
    location = {
        "score_sha256": score_sha256,
        "node_id": node_id,
        "bar_range": None if bar_range is None else dict(bar_range),
        "event_ids": list(event_ids)[:MAX_LOCATION_EVENT_IDS],
        "event_ids_truncated": len(event_ids) > MAX_LOCATION_EVENT_IDS,
        "part_ids": list(part_ids),
    }
    body = {
        "question_kind": question_kind,
        "prompt": prompt,
        "basis": copy.deepcopy(dict(basis)),
        "location": location,
    }
    identity = canonical_json_sha256(
        {
            "score_sha256": score_sha256,
            "composition_map_sha256": map_sha256,
            **body,
        }
    )
    return {"question_id": f"question-{identity[:20]}", **body}


def _score_index(
    score_document: object,
) -> tuple[
    ScoreDocument,
    str,
    list[tuple[int, str, ScoreNote]],
    dict[str, tuple[int, str, ScoreNote]],
]:
    if not isinstance(score_document, dict):
        raise CompositionMapError(
            "invalid_score",
            "score must be a score-v1 object",
            details={"path": "score"},
        )
    try:
        score_hash = canonical_score_sha256(score_document)
        parsed = parse_score_document(score_document)
    except (ScoreOpsError, KeyError, TypeError, ValueError, OverflowError) as exc:
        details = {"path": "score"}
        if isinstance(exc, ScoreOpsError):
            details.update(exc.details)
        raise CompositionMapError(
            "invalid_score",
            f"score must be a valid score-v1 document: {exc}",
            details=details,
        ) from exc
    notes: list[tuple[int, str, ScoreNote]] = []
    events: dict[str, tuple[int, str, ScoreNote]] = {}
    for part_index, part in enumerate(parsed.parts):
        for note in part.notes:
            notes.append((part_index, part.id, note))
            assert note.source_event_id is not None
            events[note.source_event_id] = (part_index, part.id, note)
    notes.sort(key=lambda item: (item[2].bar, item[2].beat, item[0], item[2].index))
    return parsed, score_hash, notes, events


def inspect_composition_map(
    score: object,
    composition_map: object,
    charter_claims: Mapping[str, object] | Sequence[str],
) -> dict[str, Any]:
    """Return whole-score facts and questions without judging or editing.

    Bar ranges are inclusive and select notes by notated onset.  This explicit
    selection rule avoids pretending that sustain, rendered masking or human
    listening can be inferred from symbolic events.  Missing/overlapping ranges
    and references become questions; they are never aesthetic failures.
    """

    normalized = normalize_composition_map(composition_map)
    map_hash = canonical_json_sha256(normalized)
    claim_ids = _claim_ids(charter_claims)
    known_claims = set(claim_ids)
    parsed, score_hash, all_notes, event_index = _score_index(score)
    part_ids = [part.id for part in parsed.parts]
    part_id_set = set(part_ids)

    referenced_claims = {
        claim_id
        for node in normalized["nodes"]
        for claim_id in node["depends_on_claim_ids"]
    }
    covered_claim_ids = sorted(referenced_claims & known_claims)
    missing_claim_ids = sorted(referenced_claims - known_claims)
    unreferenced_claim_ids = sorted(known_claims - referenced_claims)

    ranged_nodes = [node for node in normalized["nodes"] if node["bar_range"]]
    ranged_node_entries = [
        (array_index, node)
        for array_index, node in enumerate(normalized["nodes"])
        if node["bar_range"] is not None
    ]
    node_facts: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    mapped_event_ids: set[str] = set()
    scarce_occurrences: dict[str, list[str]] = {}
    ending_nodes: list[str] = []

    for node in normalized["nodes"]:
        node_id = node["node_id"]
        declared_range = node["bar_range"]
        if declared_range is None:
            selected: list[tuple[int, str, ScoreNote]] = []
        else:
            selected = [
                item
                for item in all_notes
                if declared_range["start"] <= item[2].bar <= declared_range["end"]
            ]
            mapped_event_ids.update(
                note.source_event_id
                for _part_index, _part_id, note in selected
                if note.source_event_id is not None
            )

        active_parts = [
            part_id
            for part_id in part_ids
            if any(item[1] == part_id for item in selected)
        ]
        effective_dynamics = sorted(
            {
                note.dynamic or parsed.parts[part_index].default_dynamic
                for part_index, _part_id, note in selected
            }
        )
        effective_articulations = sorted(
            {
                articulation
                for part_index, _part_id, note in selected
                if (
                    articulation := (
                        note.articulation
                        or parsed.parts[part_index].default_articulation
                    )
                )
                is not None
            }
        )
        pitches = [note.midi for _part_index, _part_id, note in selected]
        first = selected[0] if selected else None
        last = selected[-1] if selected else None

        declared_event_ids = node["established_material"]["event_ids"]
        material_locations: list[dict[str, Any]] = []
        found_event_ids: list[str] = []
        missing_events: list[str] = []
        outside_range: list[str] = []
        for event_id in declared_event_ids:
            event = event_index.get(event_id)
            if event is None:
                missing_events.append(event_id)
                continue
            part_index, part_id, note = event
            found_event_ids.append(event_id)
            material_locations.append(
                _position(note, part_id=part_id, part_index=part_index)
            )
            if declared_range is not None and not (
                declared_range["start"] <= note.bar <= declared_range["end"]
            ):
                outside_range.append(event_id)

        dependency_found = sorted(
            set(node["depends_on_claim_ids"]) & known_claims
        )
        dependency_missing = sorted(
            set(node["depends_on_claim_ids"]) - known_claims
        )
        declared_role_parts = [item["part_id"] for item in node["role_changes"]]
        missing_role_parts = sorted(set(declared_role_parts) - part_id_set)
        observed = {
            "selection_basis": "note_onset_in_inclusive_bar_range",
            "onset_event_count": len(selected),
            "active_part_ids": active_parts,
            "first_onset": (
                None
                if first is None
                else _coordinate(first[2], part_id=first[1])
            ),
            "last_onset": (
                None if last is None else _coordinate(last[2], part_id=last[1])
            ),
            "midi_range": (
                None
                if not pitches
                else {"minimum": min(pitches), "maximum": max(pitches)}
            ),
            "effective_dynamic_marks": effective_dynamics,
            "effective_articulations": effective_articulations,
        }
        node_facts.append(
            {
                "node_id": node_id,
                "label": node["label"],
                "function": node["function"],
                "location": {
                    "score_sha256": score_hash,
                    "bar_range": (
                        None if declared_range is None else dict(declared_range)
                    ),
                },
                "declared": {
                    "preserve": copy.deepcopy(node["preserve"]),
                    "transform": copy.deepcopy(node["transform"]),
                    "role_changes": copy.deepcopy(node["role_changes"]),
                    "scarce_resources": copy.deepcopy(node["scarce_resources"]),
                    "ending_response": node["ending_response"],
                    "open_questions": copy.deepcopy(node["open_questions"]),
                },
                "observed": observed,
                "claim_dependencies": {
                    "declared_claim_ids": copy.deepcopy(
                        node["depends_on_claim_ids"]
                    ),
                    "covered_claim_ids": dependency_found,
                    "missing_claim_ids": dependency_missing,
                },
                "established_material": {
                    "declared_event_ids": copy.deepcopy(declared_event_ids),
                    "found_event_ids": found_event_ids,
                    "missing_event_ids": missing_events,
                    "outside_declared_bar_range_event_ids": outside_range,
                    "locations": material_locations,
                },
                "role_part_coverage": {
                    "declared_part_ids": declared_role_parts,
                    "found_part_ids": [
                        item for item in declared_role_parts if item in part_id_set
                    ],
                    "missing_part_ids": missing_role_parts,
                },
            }
        )

        location_events = declared_event_ids
        if not node["depends_on_claim_ids"]:
            questions.append(
                _question(
                    question_kind="node_without_claim_dependency",
                    prompt=(
                        "Which current-work charter claim, if any, gives this "
                        "node its function?"
                    ),
                    basis={"declared_claim_count": 0},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                )
            )
        if dependency_missing:
            questions.append(
                _question(
                    question_kind="missing_claim_dependency",
                    prompt=(
                        "Do these claim references belong to the current work, "
                        "or should the node be rebound before it guides revision?"
                    ),
                    basis={"missing_claim_ids": dependency_missing},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                )
            )
        if declared_range is not None and not selected:
            questions.append(
                _question(
                    question_kind="node_without_observed_onsets",
                    prompt=(
                        "Is the absence of notated onsets in this declared node "
                        "intentional?"
                    ),
                    basis={
                        "selection_basis": "note_onset_in_inclusive_bar_range",
                        "onset_event_count": 0,
                    },
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                )
            )
        if missing_events:
            questions.append(
                _question(
                    question_kind="established_material_not_found",
                    prompt=(
                        "Which current-score events now establish the intended "
                        "material?"
                    ),
                    basis={"missing_event_ids": missing_events},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                    event_ids=missing_events,
                )
            )
        if outside_range:
            questions.append(
                _question(
                    question_kind="established_material_outside_node_range",
                    prompt=(
                        "Is this material intentionally established outside the "
                        "node that declares it?"
                    ),
                    basis={"outside_event_ids": outside_range},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                    event_ids=outside_range,
                )
            )
        if missing_role_parts:
            questions.append(
                _question(
                    question_kind="role_part_not_found",
                    prompt=(
                        "Which current-score part now carries the declared role "
                        "change?"
                    ),
                    basis={"missing_part_ids": missing_role_parts},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                    event_ids=location_events,
                    part_ids=missing_role_parts,
                )
            )
        for open_question in node["open_questions"]:
            questions.append(
                _question(
                    question_kind="declared_open_question",
                    prompt=open_question,
                    basis={"source": "composition_map.open_questions"},
                    score_sha256=score_hash,
                    map_sha256=map_hash,
                    node_id=node_id,
                    bar_range=declared_range,
                    event_ids=location_events,
                    part_ids=declared_role_parts,
                )
            )
        for resource in node["scarce_resources"]:
            scarce_occurrences.setdefault(resource, []).append(node_id)
        if node["ending_response"] is not None:
            ending_nodes.append(node_id)

    overlapping_pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(ranged_nodes):
        left_range = left["bar_range"]
        assert left_range is not None
        for right in ranged_nodes[left_index + 1 :]:
            right_range = right["bar_range"]
            assert right_range is not None
            overlap_start = max(left_range["start"], right_range["start"])
            overlap_end = min(left_range["end"], right_range["end"])
            if overlap_start <= overlap_end:
                overlapping_pairs.append(
                    {
                        "node_ids": [left["node_id"], right["node_id"]],
                        "bar_range": {"start": overlap_start, "end": overlap_end},
                    }
                )
    if overlapping_pairs:
        questions.append(
            _question(
                question_kind="overlapping_node_ranges",
                prompt=(
                    "How do the declared functions coexist in these overlapping "
                    "ranges?"
                ),
                basis={"overlaps": overlapping_pairs},
                score_sha256=score_hash,
                map_sha256=map_hash,
            )
        )

    # Node order expresses the declared sequence, but a composition may
    # intentionally return to an earlier notated region. Surface each such
    # inversion as one deterministic whole-work question instead of imposing a
    # linear-form rule or silently sorting the map.
    non_monotonic_pairs: list[dict[str, Any]] = []
    for left_position, (left_index, left) in enumerate(ranged_node_entries):
        left_range = left["bar_range"]
        assert left_range is not None
        for right_index, right in ranged_node_entries[left_position + 1 :]:
            right_range = right["bar_range"]
            assert right_range is not None
            if right_range["start"] < left_range["start"]:
                non_monotonic_pairs.append(
                    {
                        "declared_before": {
                            "array_index": left_index,
                            "node_id": left["node_id"],
                            "bar_range": dict(left_range),
                        },
                        "declared_after": {
                            "array_index": right_index,
                            "node_id": right["node_id"],
                            "bar_range": dict(right_range),
                        },
                    }
                )
    if non_monotonic_pairs:
        involved_ranges = [
            endpoint["bar_range"]
            for pair in non_monotonic_pairs
            for endpoint in (pair["declared_before"], pair["declared_after"])
        ]
        questions.append(
            _question(
                question_kind="non_monotonic_node_order",
                prompt=(
                    "The declared node sequence returns to an earlier notated "
                    "bar region. Is this an intentional nonlinear return, "
                    "intercut, or retrospective dependency, and how should the "
                    "whole-work sequence be read?"
                ),
                basis={"declared_order_inversions": non_monotonic_pairs},
                score_sha256=score_hash,
                map_sha256=map_hash,
                bar_range={
                    "start": min(item["start"] for item in involved_ranges),
                    "end": max(item["end"] for item in involved_ranges),
                },
            )
        )

    unmapped = [
        item
        for item in all_notes
        if item[2].source_event_id not in mapped_event_ids
    ]
    unmapped_ids = [
        note.source_event_id
        for _part_index, _part_id, note in unmapped
        if note.source_event_id is not None
    ]
    unmapped_bar_ranges = _compress_bars(note.bar for _, _, note in unmapped)
    if unmapped:
        questions.append(
            _question(
                question_kind="unmapped_score_regions",
                prompt=(
                    "Are these notated onset regions intentionally outside the "
                    "current sequence map?"
                ),
                basis={
                    "unmapped_onset_event_count": len(unmapped),
                    "unmapped_onset_bar_ranges": unmapped_bar_ranges,
                },
                score_sha256=score_hash,
                map_sha256=map_hash,
                event_ids=unmapped_ids,
            )
        )

    if unreferenced_claim_ids:
        questions.append(
            _question(
                question_kind="unreferenced_charter_claims",
                prompt=(
                    "Are these current-work charter claims intentionally absent "
                    "from the sequence map?"
                ),
                basis={"unreferenced_claim_ids": unreferenced_claim_ids},
                score_sha256=score_hash,
                map_sha256=map_hash,
            )
        )

    repeated_scarce_resources = [
        {"resource": resource, "node_ids": node_ids}
        for resource, node_ids in sorted(scarce_occurrences.items())
        if len(node_ids) > 1
    ]
    if repeated_scarce_resources:
        questions.append(
            _question(
                question_kind="scarce_resource_declared_in_multiple_nodes",
                prompt=(
                    "How does repeated use preserve the declared scarcity of "
                    "these resources?"
                ),
                basis={"occurrences": repeated_scarce_resources},
                score_sha256=score_hash,
                map_sha256=map_hash,
            )
        )

    part_facts = []
    for part_index, part in enumerate(parsed.parts):
        selected = [item for item in all_notes if item[0] == part_index]
        part_facts.append(
            {
                "part_id": part.id,
                "part_name": part.name,
                "onset_event_count": len(selected),
                "onset_bar_range": _bar_span(selected),
            }
        )

    return {
        "kind": COMPOSITION_MAP_INSPECTION_KIND,
        "schema_version": COMPOSITION_MAP_SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "authority_boundary": {
            "aesthetic_score": False,
            "automatic_edit": False,
            "audio_audition": False,
            "fixed_form_assumption": False,
            "facts_are_not_acceptance": True,
        },
        "score_sha256": score_hash,
        "composition_map_sha256": map_hash,
        "score_facts": {
            "title": parsed.title,
            "identity_contract": parsed.identity_contract,
            "part_count": len(parsed.parts),
            "onset_event_count": len(all_notes),
            "onset_bar_range": _bar_span(all_notes),
            "parts": part_facts,
            "mapped_onset_event_count": len(mapped_event_ids),
            "unmapped_onset_event_count": len(unmapped),
            "unmapped_onset_bar_ranges": unmapped_bar_ranges,
        },
        "dependency_coverage": {
            "available_claim_ids": claim_ids,
            "referenced_claim_ids": sorted(referenced_claims),
            "covered_claim_ids": covered_claim_ids,
            "missing_claim_ids": missing_claim_ids,
            "unreferenced_claim_ids": unreferenced_claim_ids,
        },
        "map_facts": {
            "node_count": len(normalized["nodes"]),
            "ranged_node_count": len(ranged_nodes),
            "nodes_without_bar_range": [
                node["node_id"]
                for node in normalized["nodes"]
                if node["bar_range"] is None
            ],
            "overlapping_node_ranges": overlapping_pairs,
            "ending_response_node_ids": ending_nodes,
            "scarce_resource_occurrences": [
                {"resource": resource, "node_ids": node_ids}
                for resource, node_ids in sorted(scarce_occurrences.items())
            ],
        },
        "node_facts": node_facts,
        "questions": questions,
    }


__all__ = [
    "COMPOSITION_MAP_ERROR_KIND",
    "COMPOSITION_MAP_INSPECTION_KIND",
    "COMPOSITION_MAP_KIND",
    "COMPOSITION_MAP_SCHEMA_VERSION",
    "CompositionMapError",
    "composition_map_sha256",
    "inspect_composition_map",
    "normalize_composition_map",
    "validate_composition_map",
]
