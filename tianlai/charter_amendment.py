"""Pure, bounded charter-amendment planning and linear replay.

The creative workflow deliberately keeps the initial work charter immutable.
This module models later changes as a monotonic amendment ledger instead of a
second version tree.  An amendment has two distinct moments:

* preflight explains the exact proposed patch and its revalidation cost;
* commit applies only that preflight-bound patch and appends one ledger entry.

The functions here do no I/O and make no aesthetic judgement.  They preserve
observations, never invalidate technical hard failures, and only mark
charter-dependent interpretations, derivations, reviews, and composition-map
dependencies as needing revalidation.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Sequence

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_sha256


CHARTER_CLAIM_INDEX_KIND = "tianlai.charter_claim_index"
CHARTER_PATCH_PROPOSAL_KIND = "tianlai.charter_patch_proposal"
CHARTER_AMENDMENT_PREFLIGHT_KIND = "tianlai.charter_amendment_preflight"
CHARTER_AMENDMENT_ENTRY_KIND = "tianlai.charter_amendment_entry"
CHARTER_AMENDMENT_VERSION = 1

MAX_CHARTER_DOCUMENT_BYTES = 512 * 1024
MAX_CHARTER_CLAIMS = 1024
MAX_PATCH_OPERATIONS = 32
MAX_AMENDMENTS = 32
MAX_IMPACT_RECORDS = 2048
MAX_TEXT_BYTES = 4096
MAX_TEXT_LIST_ITEMS = 64

_LIMITS = AuthoringJsonLimits(
    max_document_bytes=MAX_CHARTER_DOCUMENT_BYTES,
    max_depth=64,
    max_nodes=20_000,
    max_string_bytes=16 * 1024,
    max_array_items=4096,
    max_object_members=512,
)

_PROPOSAL_FIELDS = frozenset(
    {
        "summary",
        "why_score_revision_is_insufficient",
        "why_bounded_exception_is_insufficient",
        "expected_gain",
        "accepted_costs",
        "replacement_constraints",
        "failure_conditions",
        "basis_ids",
        "operations",
    }
)
_NORMALIZED_PROPOSAL_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "base_charter_sha256",
        *_PROPOSAL_FIELDS,
        "proposal_id",
        "proposal_sha256",
    }
)
_WHOLE_WORK_ROOT_FIELDS = frozenset(
    {"one_sentence_promise", "primary_sovereignty", "identity_kernel"}
)
_STRUCTURAL_ROOT_FIELDS = frozenset(
    {
        "ending_contract",
        "dramatic_question",
        "energy_curve",
        "tension_curve",
        "memory_landmarks",
        "scarce_resources",
        "climax_privileges",
        "prohibited_shortcuts",
        "style_recipe",
    }
)
_EVIDENCE_CATEGORIES = frozenset(
    {"hard_failure", "promise_conflict", "aesthetic_risk"}
)


class CharterAmendmentError(RuntimeError):
    """Stable, path-free charter-amendment failure."""

    def __init__(
        self,
        code: str,
        *,
        location_segments: Iterable[str | int] = (),
    ) -> None:
        self.code = code
        self.message_key = f"charterAmendment.{code.replace('.', '_')}"
        self.source = "charter_amendment"
        self.location_segments = tuple(location_segments)
        super().__init__(code)

    def to_issue(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "source": self.source,
            "severity": "error",
            "decision": "block",
            "location": {"segments": list(self.location_segments)},
        }


def _detach_object(value: object, *, field: str) -> dict[str, Any]:
    """Capture one bounded built-in JSON generation."""

    if not isinstance(value, Mapping):
        raise CharterAmendmentError(
            "object_required", location_segments=(field,)
        )
    # Convert workflow snapshot dict/list subclasses before the exact built-in
    # authoring JSON boundary.  The graph is already bounded immediately after
    # conversion and is captured again by the strict encoder/parser pair.
    def plain(item: object, depth: int = 0) -> object:
        if depth > _LIMITS.max_depth:
            raise CharterAmendmentError(
                "json.too_deep", location_segments=(field,)
            )
        if isinstance(item, Mapping):
            return {key: plain(child, depth + 1) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(child, depth + 1) for child in item]
        return item

    try:
        payload = bounded_canonical_json_bytes(plain(value), limits=_LIMITS)
        detached = strict_json_loads(payload, limits=_LIMITS)
    except AuthoringJsonError as exc:
        raise CharterAmendmentError(
            f"json.{exc.code}",
            location_segments=(field, *exc.location_segments),
        ) from exc
    assert isinstance(detached, dict)
    return detached


def _bounded_text(
    value: object,
    *,
    field: str,
    maximum_bytes: int = MAX_TEXT_BYTES,
) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CharterAmendmentError(
            "invalid_text", location_segments=(field,)
        )
    result = value.strip()
    try:
        size = len(result.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise CharterAmendmentError(
            "invalid_text", location_segments=(field,)
        ) from exc
    if not result or size > maximum_bytes:
        raise CharterAmendmentError(
            "invalid_text", location_segments=(field,)
        )
    return result


def _bounded_text_list(
    value: object,
    *,
    field: str,
    maximum_items: int = MAX_TEXT_LIST_ITEMS,
) -> list[str]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= maximum_items:
        raise CharterAmendmentError(
            "invalid_text_list", location_segments=(field,)
        )
    result = [
        _bounded_text(item, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(result)) != len(result):
        raise CharterAmendmentError(
            "duplicate_list_item", location_segments=(field,)
        )
    return result


def _strict_sequence(value: object, *, field: str, maximum: int) -> list[Any]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise CharterAmendmentError(
            "invalid_sequence", location_segments=(field,)
        )
    return list(value)


def _claim_id(field_path: Sequence[str], *, list_value: object = ...) -> str:
    identity: dict[str, Any] = {
        "kind": "tianlai.charter_claim_identity",
        "schema_version": CHARTER_AMENDMENT_VERSION,
        "field_path": list(field_path),
    }
    if list_value is ...:
        identity["unit"] = "singleton"
    else:
        identity["unit"] = "list_item"
        identity["value"] = list_value
    return "claim-" + canonical_json_sha256(identity)


def _collection_id(field_path: Sequence[str]) -> str:
    return "collection-" + canonical_json_sha256(
        {
            "kind": "tianlai.charter_collection_identity",
            "schema_version": CHARTER_AMENDMENT_VERSION,
            "field_path": list(field_path),
        }
    )


def _build_claim_index(work_charter: Mapping[str, Any]) -> dict[str, Any]:
    charter = _detach_object(work_charter, field="work_charter")
    claims: list[dict[str, Any]] = []
    collections: list[dict[str, Any]] = []

    def visit(value: object, field_path: list[str]) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key], [*field_path, key])
            return
        if isinstance(value, list):
            collection_id = _collection_id(field_path)
            collections.append(
                {
                    "collection_id": collection_id,
                    "field_path": list(field_path),
                    "root_field": field_path[0],
                    "item_count": len(value),
                }
            )
            seen_values: set[str] = set()
            for position, item in enumerate(value):
                value_sha256 = canonical_json_sha256(item)
                if value_sha256 in seen_values:
                    raise CharterAmendmentError(
                        "duplicate_charter_claim",
                        location_segments=("work_charter", *field_path, position),
                    )
                seen_values.add(value_sha256)
                claims.append(
                    {
                        "claim_id": _claim_id(field_path, list_value=item),
                        "field_path": list(field_path),
                        "root_field": field_path[0],
                        "unit": "list_item",
                        "collection_id": collection_id,
                        "position": position,
                        "value": copy.deepcopy(item),
                        "value_sha256": value_sha256,
                    }
                )
            return
        claims.append(
            {
                "claim_id": _claim_id(field_path),
                "field_path": list(field_path),
                "root_field": field_path[0],
                "unit": "singleton",
                "collection_id": None,
                "position": None,
                "value": copy.deepcopy(value),
                "value_sha256": canonical_json_sha256(value),
            }
        )

    for root_field in sorted(charter):
        visit(charter[root_field], [root_field])
    if len(claims) > MAX_CHARTER_CLAIMS:
        raise CharterAmendmentError("charter_claim_limit_exceeded")
    claim_ids = [item["claim_id"] for item in claims]
    if len(set(claim_ids)) != len(claim_ids):
        raise CharterAmendmentError("charter_claim_identity_collision")
    collection_ids = [item["collection_id"] for item in collections]
    if len(set(collection_ids)) != len(collection_ids):
        raise CharterAmendmentError("charter_collection_identity_collision")
    body = {
        "kind": CHARTER_CLAIM_INDEX_KIND,
        "schema_version": CHARTER_AMENDMENT_VERSION,
        "charter_sha256": canonical_json_sha256(charter),
        "claims": claims,
        "collections": collections,
    }
    return {
        **body,
        "claim_index_sha256": canonical_json_sha256(body),
    }


def index_charter_claims(work_charter: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable claim registry for one effective work charter.

    Singleton claim identities depend on their structural field path, not on
    their text, so replacing a promise does not rename the promise claim.
    List-item identities depend on collection path and item value, so they
    remain stable when another item is inserted or the list is reordered.
    """

    return _build_claim_index(work_charter)


def _claim_maps(index: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return (
        {item["claim_id"]: item for item in index["claims"]},
        {item["collection_id"]: item for item in index["collections"]},
    )


def _normalize_operation(
    operation: object,
    *,
    index: int,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw = _detach_object(operation, field=f"operations[{index}]")
    kind = raw.get("op")
    if kind == "replace":
        raw_fields = set(raw)
        allowed = {"op", "claim_id", "value"}
        normalized_allowed = {*allowed, "expected_value_sha256"}
        if raw_fields not in (allowed, normalized_allowed):
            raise CharterAmendmentError(
                "invalid_patch_operation", location_segments=("operations", index)
            )
        claim_id = raw.get("claim_id")
        claim = claims.get(claim_id) if isinstance(claim_id, str) else None
        if claim is None:
            raise CharterAmendmentError(
                "unknown_charter_claim", location_segments=("operations", index)
            )
        value = copy.deepcopy(raw["value"])
        if claim["unit"] == "singleton" and isinstance(value, (dict, list)):
            raise CharterAmendmentError(
                "whole_claim_container_replacement_forbidden",
                location_segments=("operations", index, "value"),
            )
        if claim["unit"] == "list_item" and (
            isinstance(value, (dict, list)) is not isinstance(claim["value"], (dict, list))
        ):
            raise CharterAmendmentError(
                "patch_value_shape_mismatch",
                location_segments=("operations", index, "value"),
            )
        value_hash = canonical_json_sha256(value)
        if value_hash == claim["value_sha256"]:
            raise CharterAmendmentError(
                "no_op_patch_operation", location_segments=("operations", index)
            )
        expected = raw.get("expected_value_sha256", claim["value_sha256"])
        if expected != claim["value_sha256"]:
            raise CharterAmendmentError(
                "patch_claim_value_mismatch", location_segments=("operations", index)
            )
        return {
            "op": "replace",
            "claim_id": claim_id,
            "expected_value_sha256": claim["value_sha256"],
            "value": value,
        }
    if kind == "remove":
        raw_fields = set(raw)
        allowed = {"op", "claim_id"}
        normalized_allowed = {*allowed, "expected_value_sha256"}
        if raw_fields not in (allowed, normalized_allowed):
            raise CharterAmendmentError(
                "invalid_patch_operation", location_segments=("operations", index)
            )
        claim_id = raw.get("claim_id")
        claim = claims.get(claim_id) if isinstance(claim_id, str) else None
        if claim is None:
            raise CharterAmendmentError(
                "unknown_charter_claim", location_segments=("operations", index)
            )
        if claim["unit"] != "list_item":
            raise CharterAmendmentError(
                "required_claim_removal_forbidden",
                location_segments=("operations", index),
            )
        expected = raw.get("expected_value_sha256", claim["value_sha256"])
        if expected != claim["value_sha256"]:
            raise CharterAmendmentError(
                "patch_claim_value_mismatch", location_segments=("operations", index)
            )
        return {
            "op": "remove",
            "claim_id": claim_id,
            "expected_value_sha256": claim["value_sha256"],
        }
    if kind == "add":
        raw_fields = set(raw)
        allowed = {"op", "collection_id", "position", "value"}
        normalized_allowed = {*allowed, "value_sha256"}
        if raw_fields not in (allowed, normalized_allowed):
            raise CharterAmendmentError(
                "invalid_patch_operation", location_segments=("operations", index)
            )
        collection_id = raw.get("collection_id")
        collection = (
            collections.get(collection_id) if isinstance(collection_id, str) else None
        )
        if collection is None:
            raise CharterAmendmentError(
                "unknown_charter_collection", location_segments=("operations", index)
            )
        position = raw.get("position")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position <= collection["item_count"]
        ):
            raise CharterAmendmentError(
                "invalid_patch_position",
                location_segments=("operations", index, "position"),
            )
        value = copy.deepcopy(raw["value"])
        existing = [
            claim
            for claim in claims.values()
            if claim["collection_id"] == collection_id
        ]
        if existing and (
            isinstance(value, (dict, list)) is not isinstance(existing[0]["value"], (dict, list))
        ):
            raise CharterAmendmentError(
                "patch_value_shape_mismatch",
                location_segments=("operations", index, "value"),
            )
        value_hash = canonical_json_sha256(value)
        if any(item["value_sha256"] == value_hash for item in existing):
            raise CharterAmendmentError(
                "duplicate_charter_claim", location_segments=("operations", index)
            )
        supplied_hash = raw.get("value_sha256", value_hash)
        if supplied_hash != value_hash:
            raise CharterAmendmentError(
                "patch_value_hash_mismatch", location_segments=("operations", index)
            )
        return {
            "op": "add",
            "collection_id": collection_id,
            "position": position,
            "value": value,
            "value_sha256": value_hash,
        }
    raise CharterAmendmentError(
        "invalid_patch_operation", location_segments=("operations", index, "op")
    )


def normalize_charter_patch_proposal(
    work_charter: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one evidence-bound, claim-level charter patch proposal."""

    charter = _detach_object(work_charter, field="work_charter")
    raw = _detach_object(proposal, field="proposal")
    normalized_input = set(raw) == _NORMALIZED_PROPOSAL_FIELDS
    if not normalized_input and set(raw) != _PROPOSAL_FIELDS:
        raise CharterAmendmentError("invalid_patch_proposal_shape")
    if normalized_input:
        if (
            isinstance(raw["schema_version"], bool)
            or not isinstance(raw["schema_version"], int)
            or raw["schema_version"] != CHARTER_AMENDMENT_VERSION
            or raw["kind"] != CHARTER_PATCH_PROPOSAL_KIND
        ):
            raise CharterAmendmentError("invalid_patch_proposal_version")
        if raw["base_charter_sha256"] != canonical_json_sha256(charter):
            raise CharterAmendmentError("patch_base_charter_mismatch")

    claim_index = _build_claim_index(charter)
    claims, collections = _claim_maps(claim_index)
    raw_operations = _strict_sequence(
        raw["operations"], field="operations", maximum=MAX_PATCH_OPERATIONS
    )
    if not raw_operations:
        raise CharterAmendmentError("patch_operation_required")
    operations = [
        _normalize_operation(
            operation,
            index=operation_index,
            claims=claims,
            collections=collections,
        )
        for operation_index, operation in enumerate(raw_operations)
    ]
    targets: set[str] = set()
    added_values: set[tuple[str, str]] = set()
    for operation_index, operation in enumerate(operations):
        if operation["op"] in {"replace", "remove"}:
            target = operation["claim_id"]
            if target in targets:
                raise CharterAmendmentError(
                    "duplicate_patch_target",
                    location_segments=("operations", operation_index),
                )
            targets.add(target)
        else:
            target = (operation["collection_id"], operation["value_sha256"])
            if target in added_values:
                raise CharterAmendmentError(
                    "duplicate_patch_addition",
                    location_segments=("operations", operation_index),
                )
            added_values.add(target)
    if any(item["op"] == "remove" for item in operations) and not any(
        item["op"] in {"add", "replace"} for item in operations
    ):
        raise CharterAmendmentError("deletion_requires_replacement")

    body = {
        "kind": CHARTER_PATCH_PROPOSAL_KIND,
        "schema_version": CHARTER_AMENDMENT_VERSION,
        "base_charter_sha256": canonical_json_sha256(charter),
        "summary": _bounded_text(raw["summary"], field="proposal.summary"),
        "why_score_revision_is_insufficient": _bounded_text(
            raw["why_score_revision_is_insufficient"],
            field="proposal.why_score_revision_is_insufficient",
        ),
        "why_bounded_exception_is_insufficient": _bounded_text(
            raw["why_bounded_exception_is_insufficient"],
            field="proposal.why_bounded_exception_is_insufficient",
        ),
        "expected_gain": _bounded_text(
            raw["expected_gain"], field="proposal.expected_gain"
        ),
        "accepted_costs": _bounded_text_list(
            raw["accepted_costs"], field="proposal.accepted_costs"
        ),
        "replacement_constraints": _bounded_text_list(
            raw["replacement_constraints"],
            field="proposal.replacement_constraints",
        ),
        "failure_conditions": _bounded_text_list(
            raw["failure_conditions"], field="proposal.failure_conditions"
        ),
        "basis_ids": _bounded_text_list(
            raw["basis_ids"], field="proposal.basis_ids"
        ),
        "operations": operations,
    }
    proposal_sha256 = canonical_json_sha256(body)
    normalized = {
        **body,
        "proposal_id": "charter-proposal-" + proposal_sha256[:20],
        "proposal_sha256": proposal_sha256,
    }
    if normalized_input and (
        raw["proposal_id"] != normalized["proposal_id"]
        or raw["proposal_sha256"] != proposal_sha256
    ):
        raise CharterAmendmentError("patch_proposal_identity_mismatch")
    return normalized


def _value_at_path(document: dict[str, Any], path: Sequence[str]) -> object:
    current: object = document
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise CharterAmendmentError("charter_claim_path_mismatch")
        current = current[segment]
    return current


def _set_at_path(document: dict[str, Any], path: Sequence[str], value: object) -> None:
    current: dict[str, Any] = document
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            raise CharterAmendmentError("charter_claim_path_mismatch")
        current = child
    current[path[-1]] = copy.deepcopy(value)


def _validate_known_cross_field_invariants(charter: Mapping[str, Any]) -> None:
    primary = charter.get("primary_sovereignty")
    secondary = charter.get("secondary_sovereignties")
    if isinstance(primary, list):
        if not 1 <= len(primary) <= 3:
            raise CharterAmendmentError("invalid_primary_sovereignty_after_patch")
        if len({canonical_json_sha256(item) for item in primary}) != len(primary):
            raise CharterAmendmentError("duplicate_charter_claim")
    if isinstance(primary, list) and isinstance(secondary, list):
        if set(primary) & set(secondary):
            raise CharterAmendmentError("sovereignty_overlap_after_patch")


def _apply_normalized_patch(
    work_charter: Mapping[str, Any], normalized: Mapping[str, Any]
) -> dict[str, Any]:
    charter = _detach_object(work_charter, field="work_charter")
    index = _build_claim_index(charter)
    claims, collections = _claim_maps(index)
    result = copy.deepcopy(charter)

    # Every list coordinate in a proposal refers to the original collection,
    # not to a collection already shortened by an earlier removal.  Accumulate
    # structural edits against that immutable coordinate system and rebuild
    # each touched list once.  This also gives additions at the same boundary a
    # deterministic proposal-order tie break.
    list_replacements: dict[str, dict[int, object]] = {}
    list_removals: dict[str, set[int]] = {}
    list_additions: dict[str, dict[int, list[object]]] = {}
    for operation in normalized["operations"]:
        if operation["op"] == "replace":
            claim = claims[operation["claim_id"]]
            if claim["unit"] == "singleton":
                _set_at_path(result, claim["field_path"], operation["value"])
            else:
                collection_id = claim["collection_id"]
                assert isinstance(collection_id, str)
                list_replacements.setdefault(collection_id, {})[
                    claim["position"]
                ] = copy.deepcopy(operation["value"])
        elif operation["op"] == "remove":
            claim = claims[operation["claim_id"]]
            collection_id = claim["collection_id"]
            assert isinstance(collection_id, str)
            list_removals.setdefault(collection_id, set()).add(claim["position"])
        elif operation["op"] == "add":
            collection_id = operation["collection_id"]
            list_additions.setdefault(collection_id, {}).setdefault(
                operation["position"], []
            ).append(copy.deepcopy(operation["value"]))

    touched_collections = (
        set(list_replacements) | set(list_removals) | set(list_additions)
    )
    for collection_id in sorted(touched_collections):
        collection_record = collections[collection_id]
        original = _value_at_path(charter, collection_record["field_path"])
        if not isinstance(original, list):
            raise CharterAmendmentError("charter_claim_path_mismatch")
        replacements = list_replacements.get(collection_id, {})
        removals = list_removals.get(collection_id, set())
        additions = list_additions.get(collection_id, {})
        rebuilt: list[object] = []
        for original_position in range(len(original) + 1):
            rebuilt.extend(copy.deepcopy(additions.get(original_position, [])))
            if original_position == len(original):
                break
            if original_position in removals:
                continue
            rebuilt.append(
                copy.deepcopy(
                    replacements.get(original_position, original[original_position])
                )
            )
        _set_at_path(result, collection_record["field_path"], rebuilt)

    _validate_known_cross_field_invariants(result)
    # Re-indexing rejects duplicate list claims and proves all values remain in
    # the bounded JSON model.
    _build_claim_index(result)
    return result


def apply_charter_patch(
    work_charter: Mapping[str, Any], proposal: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply only the declared proposal operations to one effective charter."""

    normalized = normalize_charter_patch_proposal(work_charter, proposal)
    return _apply_normalized_patch(work_charter, normalized)


def _change_set(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    before_index = _build_claim_index(before)
    after_index = _build_claim_index(after)
    before_claims, _ = _claim_maps(before_index)
    after_claims, _ = _claim_maps(after_index)
    before_ids = set(before_claims)
    after_ids = set(after_claims)
    retained_changed = sorted(
        claim_id
        for claim_id in before_ids & after_ids
        if before_claims[claim_id]["value_sha256"]
        != after_claims[claim_id]["value_sha256"]
    )
    introduced = sorted(after_ids - before_ids)
    retired = sorted(before_ids - after_ids)
    affected = sorted(set(retained_changed) | set(introduced) | set(retired))
    roots = sorted(
        {
            item["root_field"]
            for claim_id, item in before_claims.items()
            if claim_id in affected
        }
        | {
            item["root_field"]
            for claim_id, item in after_claims.items()
            if claim_id in affected
        }
    )
    return {
        "affected_claim_ids": affected,
        "retained_changed_claim_ids": retained_changed,
        "introduced_claim_ids": introduced,
        "retired_claim_ids": retired,
        "affected_root_fields": roots,
        "before_claim_count": len(before_claims),
        "after_claim_count": len(after_claims),
    }


def _record_id(
    record: Mapping[str, Any], candidates: Sequence[str], *, prefix: str
) -> str:
    for field in candidates:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return _bounded_text(value, field=f"{prefix}.{field}", maximum_bytes=512)
    return prefix + "-" + canonical_json_sha256(record)[:20]


def _resolve_reference(
    reference: object,
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    if not isinstance(reference, str):
        raise CharterAmendmentError("invalid_charter_claim_reference")
    checked = _bounded_text(reference, field="claim_reference", maximum_bytes=512)
    if checked in claims:
        return {checked}, set()
    if checked in collections:
        return set(), {checked}
    path_text = checked.removeprefix("work_charter.").removeprefix("charter.")
    path = path_text.split(".")
    claim_matches = {
        claim_id
        for claim_id, claim in claims.items()
        if claim["field_path"][: len(path)] == path
    }
    collection_matches = {
        collection_id
        for collection_id, collection in collections.items()
        if collection["field_path"][: len(path)] == path
    }
    if not claim_matches and not collection_matches:
        raise CharterAmendmentError("unknown_charter_claim_reference")
    return claim_matches, collection_matches


def _explicit_reference_scope(
    record: Mapping[str, Any],
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    references: list[object] = []
    for field in (
        "claim_ids",
        "charter_claim_ids",
        "depends_on_claim_ids",
        "collection_ids",
        "charter_collection_ids",
        "depends_on_collection_ids",
    ):
        value = record.get(field)
        if value is not None:
            if not isinstance(value, list):
                raise CharterAmendmentError("invalid_charter_claim_references")
            references.extend(value)
    singular = record.get("claim_id")
    if singular is not None:
        references.append(singular)
    coverage = record.get("coverage")
    if isinstance(coverage, Mapping):
        for field in (
            "claim_ids",
            "charter_claim_ids",
            "depends_on_claim_ids",
            "collection_ids",
            "charter_collection_ids",
            "depends_on_collection_ids",
        ):
            value = coverage.get(field)
            if value is not None:
                if not isinstance(value, list):
                    raise CharterAmendmentError("invalid_charter_claim_references")
                references.extend(value)
    resolved_claims: set[str] = set()
    resolved_collections: set[str] = set()
    for reference in references:
        claim_ids, collection_ids = _resolve_reference(
            reference, claims=claims, collections=collections
        )
        resolved_claims.update(claim_ids)
        resolved_collections.update(collection_ids)
    return resolved_claims, resolved_collections


def _derivation_reference_scope(
    record: Mapping[str, Any],
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    resolved_claims, resolved_collections = _explicit_reference_scope(
        record, claims=claims, collections=collections
    )
    premises = record.get("premises", [])
    if not isinstance(premises, list):
        raise CharterAmendmentError("invalid_derivation_for_preflight")
    for premise in premises:
        if not isinstance(premise, Mapping):
            raise CharterAmendmentError("invalid_derivation_for_preflight")
        if premise.get("kind") == "declared_promise":
            claim_ids, collection_ids = _resolve_reference(
                premise.get("reference"), claims=claims, collections=collections
            )
            resolved_claims.update(claim_ids)
            resolved_collections.update(collection_ids)
    return resolved_claims, resolved_collections


def _evidence_reference_scope(
    record: Mapping[str, Any],
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    resolved_claims, resolved_collections = _explicit_reference_scope(
        record, claims=claims, collections=collections
    )
    basis = record.get("basis")
    if isinstance(basis, Mapping) and basis.get("kind") == "declared_promise":
        claim_ids, collection_ids = _resolve_reference(
            basis.get("reference"), claims=claims, collections=collections
        )
        resolved_claims.update(claim_ids)
        resolved_collections.update(collection_ids)
    return resolved_claims, resolved_collections


def _references_impacted(
    references: set[str],
    *,
    collection_references: set[str],
    affected_claim_ids: set[str],
    affected_root_fields: set[str],
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> bool:
    if references & affected_claim_ids:
        return True
    # A list addition can conflict with another claim in the same charter
    # domain even though that new claim did not exist when dependencies were
    # recorded.  Root-domain matching makes that cost visible up front.
    return any(
        claims[claim_id]["root_field"] in affected_root_fields
        for claim_id in references
        if claim_id in claims
    ) or any(
        collections[collection_id]["root_field"] in affected_root_fields
        for collection_id in collection_references
        if collection_id in collections
    )


def _scope_band(affected_root_fields: set[str], affected_claim_count: int) -> str:
    if affected_root_fields & _WHOLE_WORK_ROOT_FIELDS or len(affected_root_fields) >= 3:
        return "whole_work"
    if (
        affected_root_fields & _STRUCTURAL_ROOT_FIELDS
        or len(affected_root_fields) >= 2
        or affected_claim_count >= 4
    ):
        return "structural"
    return "bounded"


def _freeze_preflight_input_snapshot(
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
    composition_map_dependencies: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    derivations: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep only the source facts needed to replay impact and cost.

    The source records can be much larger than the amendment calculation
    needs.  Persisting stable record identity, category, and resolved charter
    claim scope is sufficient to reproduce every derived impact/cost field
    without retaining unrelated prose or score material.
    """

    dependency_source: object = composition_map_dependencies
    if isinstance(composition_map_dependencies, Mapping):
        dependency_document = _detach_object(
            composition_map_dependencies, field="composition_map_dependencies"
        )
        dependency_source = dependency_document.get("nodes")
        if dependency_source is None:
            raise CharterAmendmentError("composition_map_nodes_required")
    dependency_items = _strict_sequence(
        dependency_source,
        field="composition_map_dependencies",
        maximum=MAX_IMPACT_RECORDS,
    )
    derivation_items = _strict_sequence(
        derivations, field="derivations", maximum=MAX_IMPACT_RECORDS
    )
    review_items = _strict_sequence(
        reviews, field="reviews", maximum=MAX_IMPACT_RECORDS
    )
    evidence_items = _strict_sequence(
        evidence, field="evidence", maximum=MAX_IMPACT_RECORDS
    )

    dependency_snapshot: list[dict[str, Any]] = []
    for item_index, item in enumerate(dependency_items):
        record = _detach_object(
            item, field=f"composition_map_dependencies[{item_index}]"
        )
        claim_ids, collection_ids = _explicit_reference_scope(
            record, claims=claims, collections=collections
        )
        dependency_snapshot.append(
            {
                "dependency_id": _record_id(
                    record,
                    ("dependency_id", "node_id", "section_id", "stage_id", "id"),
                    prefix="composition-dependency",
                ),
                "claim_ids": sorted(claim_ids),
                "collection_ids": sorted(collection_ids),
            }
        )

    derivation_snapshot: list[dict[str, Any]] = []
    for item_index, item in enumerate(derivation_items):
        record = _detach_object(item, field=f"derivations[{item_index}]")
        claim_ids, collection_ids = _derivation_reference_scope(
            record, claims=claims, collections=collections
        )
        derivation_snapshot.append(
            {
                "derivation_id": _record_id(
                    record, ("derivation_id", "id"), prefix="derivation"
                ),
                "claim_ids": sorted(claim_ids),
                "collection_ids": sorted(collection_ids),
            }
        )

    review_snapshot: list[dict[str, Any]] = []
    for item_index, item in enumerate(review_items):
        record = _detach_object(item, field=f"reviews[{item_index}]")
        claim_ids, collection_ids = _explicit_reference_scope(
            record, claims=claims, collections=collections
        )
        review_snapshot.append(
            {
                "review_id": _record_id(
                    record, ("review_id", "id"), prefix="review"
                ),
                "claim_ids": sorted(claim_ids),
                "collection_ids": sorted(collection_ids),
            }
        )

    evidence_snapshot: list[dict[str, Any]] = []
    for item_index, item in enumerate(evidence_items):
        record = _detach_object(item, field=f"evidence[{item_index}]")
        category = record.get("category")
        if category not in _EVIDENCE_CATEGORIES:
            raise CharterAmendmentError("invalid_evidence_for_preflight")
        claim_ids, collection_ids = _evidence_reference_scope(
            record, claims=claims, collections=collections
        )
        evidence_snapshot.append(
            {
                "evidence_id": _record_id(
                    record, ("evidence_id", "id"), prefix="evidence"
                ),
                "category": category,
                "claim_ids": sorted(claim_ids),
                "collection_ids": sorted(collection_ids),
            }
        )

    return {
        "composition_map_dependencies": dependency_snapshot,
        "derivations": derivation_snapshot,
        "reviews": review_snapshot,
        "evidence": evidence_snapshot,
    }


def _normalized_preflight_input_snapshot(
    snapshot: Mapping[str, Any],
    *,
    claims: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    document = _detach_object(snapshot, field="preflight.input_snapshot")
    if set(document) != {
        "composition_map_dependencies",
        "derivations",
        "reviews",
        "evidence",
    }:
        raise CharterAmendmentError("invalid_charter_amendment_input_snapshot")

    specifications = (
        (
            "composition_map_dependencies",
            "dependency_id",
            frozenset({"dependency_id", "claim_ids", "collection_ids"}),
        ),
        (
            "derivations",
            "derivation_id",
            frozenset({"derivation_id", "claim_ids", "collection_ids"}),
        ),
        (
            "reviews",
            "review_id",
            frozenset({"review_id", "claim_ids", "collection_ids"}),
        ),
        (
            "evidence",
            "evidence_id",
            frozenset(
                {"evidence_id", "category", "claim_ids", "collection_ids"}
            ),
        ),
    )
    normalized: dict[str, list[dict[str, Any]]] = {}
    for field, identifier_field, expected_fields in specifications:
        records = _strict_sequence(
            document[field], field=f"preflight.input_snapshot.{field}", maximum=MAX_IMPACT_RECORDS
        )
        normalized_records: list[dict[str, Any]] = []
        for record_index, item in enumerate(records):
            record = _detach_object(
                item, field=f"preflight.input_snapshot.{field}[{record_index}]"
            )
            if set(record) != expected_fields:
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            identifier = record[identifier_field]
            if not isinstance(identifier, str):
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            checked_identifier = _bounded_text(
                identifier,
                field=f"preflight.input_snapshot.{field}[{record_index}].{identifier_field}",
                maximum_bytes=512,
            )
            if checked_identifier != identifier:
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            raw_claim_ids = _strict_sequence(
                record["claim_ids"],
                field=f"preflight.input_snapshot.{field}[{record_index}].claim_ids",
                maximum=MAX_CHARTER_CLAIMS,
            )
            if any(
                not isinstance(claim_id, str) or claim_id not in claims
                for claim_id in raw_claim_ids
            ):
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            claim_ids = sorted(set(raw_claim_ids))
            if claim_ids != raw_claim_ids:
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            raw_collection_ids = _strict_sequence(
                record["collection_ids"],
                field=(
                    f"preflight.input_snapshot.{field}[{record_index}]."
                    "collection_ids"
                ),
                maximum=MAX_CHARTER_CLAIMS,
            )
            if any(
                not isinstance(collection_id, str)
                or collection_id not in collections
                for collection_id in raw_collection_ids
            ):
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            collection_ids = sorted(set(raw_collection_ids))
            if collection_ids != raw_collection_ids:
                raise CharterAmendmentError(
                    "invalid_charter_amendment_input_snapshot"
                )
            normalized_record: dict[str, Any] = {
                identifier_field: identifier,
                "claim_ids": claim_ids,
                "collection_ids": collection_ids,
            }
            if field == "evidence":
                category = record["category"]
                if category not in _EVIDENCE_CATEGORIES:
                    raise CharterAmendmentError(
                        "invalid_charter_amendment_input_snapshot"
                    )
                normalized_record["category"] = category
            normalized_records.append(normalized_record)
        normalized[field] = normalized_records
    return normalized


def _charter_amendment_preflight_body(
    work_charter: Mapping[str, Any],
    normalized_proposal: Mapping[str, Any],
    input_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive every mutable-looking preflight field from replayable inputs."""

    charter = _detach_object(work_charter, field="work_charter")
    index = _build_claim_index(charter)
    claims, collections = _claim_maps(index)
    snapshot = _normalized_preflight_input_snapshot(
        input_snapshot, claims=claims, collections=collections
    )
    expected = _apply_normalized_patch(charter, normalized_proposal)
    change = _change_set(charter, expected)
    if not change["affected_claim_ids"]:
        raise CharterAmendmentError("empty_charter_change")
    affected_claim_ids = set(change["affected_claim_ids"])
    affected_root_fields = set(change["affected_root_fields"])

    known_basis: dict[str, dict[str, Any]] = {}
    seen_record_ids: set[str] = set()

    def retain_record_id(identifier: str) -> None:
        if identifier in seen_record_ids:
            raise CharterAmendmentError("duplicate_preflight_record_id")
        seen_record_ids.add(identifier)

    def retain_basis(
        identifier: str,
        kind: str,
        references: set[str],
        collection_references: set[str],
    ) -> None:
        retain_record_id(identifier)
        known_basis[identifier] = {
            "kind": kind,
            "claim_ids": sorted(references),
            "collection_ids": sorted(collection_references),
        }

    dependency_impact: list[dict[str, Any]] = []
    for record in snapshot["composition_map_dependencies"]:
        retain_record_id(record["dependency_id"])
        references = set(record["claim_ids"])
        collection_references = set(record["collection_ids"])
        impacted = (not references and not collection_references) or _references_impacted(
            references,
            collection_references=collection_references,
            affected_claim_ids=affected_claim_ids,
            affected_root_fields=affected_root_fields,
            claims=claims,
            collections=collections,
        )
        dependency_impact.append(
            {
                "dependency_id": record["dependency_id"],
                "claim_ids": sorted(references),
                "collection_ids": sorted(collection_references),
                "status": "needs_revalidation" if impacted else "preserved",
                "reason": (
                    "unscoped_composition_dependency_cannot_prove_independence"
                    if impacted and not references and not collection_references
                    else (
                        "charter_dependency_changed"
                        if impacted
                        else "declared_dependencies_unchanged"
                    )
                ),
            }
        )

    derivation_impact: list[dict[str, Any]] = []
    for record in snapshot["derivations"]:
        references = set(record["claim_ids"])
        collection_references = set(record["collection_ids"])
        retain_basis(
            record["derivation_id"],
            "derivation",
            references,
            collection_references,
        )
        impacted = _references_impacted(
            references,
            collection_references=collection_references,
            affected_claim_ids=affected_claim_ids,
            affected_root_fields=affected_root_fields,
            claims=claims,
            collections=collections,
        )
        derivation_impact.append(
            {
                "derivation_id": record["derivation_id"],
                "claim_ids": sorted(references),
                "collection_ids": sorted(collection_references),
                "status": "needs_revalidation" if impacted else "preserved",
                "reason": (
                    "charter_premise_changed" if impacted else "premises_unchanged"
                ),
            }
        )

    review_impact: list[dict[str, Any]] = []
    for record in snapshot["reviews"]:
        references = set(record["claim_ids"])
        collection_references = set(record["collection_ids"])
        retain_basis(
            record["review_id"], "review", references, collection_references
        )
        impacted = (not references and not collection_references) or _references_impacted(
            references,
            collection_references=collection_references,
            affected_claim_ids=affected_claim_ids,
            affected_root_fields=affected_root_fields,
            claims=claims,
            collections=collections,
        )
        review_impact.append(
            {
                "review_id": record["review_id"],
                "claim_ids": sorted(references),
                "collection_ids": sorted(collection_references),
                "status": "needs_revalidation" if impacted else "preserved",
                "reason": (
                    "unscoped_review_cannot_prove_independence"
                    if impacted and not references and not collection_references
                    else (
                        "review_basis_changed" if impacted else "review_basis_unchanged"
                    )
                ),
            }
        )

    evidence_impact: list[dict[str, Any]] = []
    for record in snapshot["evidence"]:
        references = set(record["claim_ids"])
        collection_references = set(record["collection_ids"])
        category = record["category"]
        retain_basis(
            record["evidence_id"],
            "hard_failure" if category == "hard_failure" else "evidence",
            references,
            collection_references,
        )
        impacted = category != "hard_failure" and (
            (not references and not collection_references)
            or _references_impacted(
                references,
                collection_references=collection_references,
                affected_claim_ids=affected_claim_ids,
                affected_root_fields=affected_root_fields,
                claims=claims,
                collections=collections,
            )
        )
        evidence_impact.append(
            {
                "evidence_id": record["evidence_id"],
                "category": category,
                "claim_ids": sorted(references),
                "collection_ids": sorted(collection_references),
                "observation_status": "preserved",
                "interpretation_status": (
                    "needs_revalidation" if impacted else "preserved"
                ),
                "reason": (
                    "technical_hard_failure_survives_charter_change"
                    if category == "hard_failure"
                    else (
                        "unscoped_evidence_cannot_prove_independence"
                        if impacted and not references and not collection_references
                        else (
                            "charter_interpretation_changed"
                            if impacted
                            else "interpretation_basis_unchanged"
                        )
                    )
                ),
            }
        )

    missing_basis = [
        identifier
        for identifier in normalized_proposal["basis_ids"]
        if identifier not in known_basis
    ]
    if missing_basis:
        raise CharterAmendmentError("amendment_basis_not_found")
    selected_basis = [
        known_basis[identifier] for identifier in normalized_proposal["basis_ids"]
    ]
    if not any(item["kind"] != "hard_failure" for item in selected_basis):
        raise CharterAmendmentError("charter_amendment_requires_non_hard_basis")
    if not any(
        item["kind"] != "hard_failure"
        and _references_impacted(
            set(item["claim_ids"]),
            collection_references=set(item["collection_ids"]),
            affected_claim_ids=affected_claim_ids,
            affected_root_fields=affected_root_fields,
            claims=claims,
            collections=collections,
        )
        for item in selected_basis
    ):
        raise CharterAmendmentError(
            "charter_amendment_requires_related_non_hard_basis"
        )

    needs_dependencies = sum(
        item["status"] == "needs_revalidation" for item in dependency_impact
    )
    needs_derivations = sum(
        item["status"] == "needs_revalidation" for item in derivation_impact
    )
    needs_reviews = sum(
        item["status"] == "needs_revalidation" for item in review_impact
    )
    needs_evidence = sum(
        item["interpretation_status"] == "needs_revalidation"
        for item in evidence_impact
    )
    return {
        "kind": CHARTER_AMENDMENT_PREFLIGHT_KIND,
        "schema_version": CHARTER_AMENDMENT_VERSION,
        "base_charter_sha256": canonical_json_sha256(charter),
        "base_claim_index_sha256": index["claim_index_sha256"],
        "proposal": copy.deepcopy(dict(normalized_proposal)),
        "proposal_sha256": normalized_proposal["proposal_sha256"],
        "input_snapshot": snapshot,
        "change_set": change,
        "impact": {
            "composition_map_dependencies": dependency_impact,
            "derivations": derivation_impact,
            "reviews": review_impact,
            "evidence": evidence_impact,
        },
        "cost": {
            "minimum_reconstruction_scope": _scope_band(
                affected_root_fields, len(affected_claim_ids)
            ),
            "operation_count": len(normalized_proposal["operations"]),
            "affected_claim_count": len(affected_claim_ids),
            "affected_root_field_count": len(affected_root_fields),
            "composition_dependencies_to_revalidate": needs_dependencies,
            "derivations_to_revalidate": needs_derivations,
            "reviews_to_revalidate": needs_reviews,
            "evidence_interpretations_to_revalidate": needs_evidence,
            "observations_preserved": len(evidence_impact),
            "hard_failures_preserved": sum(
                item["category"] == "hard_failure" for item in evidence_impact
            ),
            "whole_work_consistency_review_required": True,
        },
        "commit_guard": {
            "exact_proposal_required": True,
            "undeclared_changes_forbidden": True,
            "expanded_scope_requires_new_preflight": True,
        },
    }


def preflight_charter_amendment(
    work_charter: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    composition_map_dependencies: (
        Sequence[Mapping[str, Any]] | Mapping[str, Any]
    ) = (),
    derivations: Sequence[Mapping[str, Any]] = (),
    reviews: Sequence[Mapping[str, Any]] = (),
    evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the immutable impact/cost sheet that must precede a patch.

    The returned document preserves factual observations.  Only a dependent
    interpretation or argument is marked ``needs_revalidation``.  A technical
    ``hard_failure`` is preserved regardless of charter changes.
    """

    charter = _detach_object(work_charter, field="work_charter")
    normalized = normalize_charter_patch_proposal(charter, proposal)
    claims, collections = _claim_maps(_build_claim_index(charter))
    input_snapshot = _freeze_preflight_input_snapshot(
        claims=claims,
        collections=collections,
        composition_map_dependencies=composition_map_dependencies,
        derivations=derivations,
        reviews=reviews,
        evidence=evidence,
    )
    body = _charter_amendment_preflight_body(
        charter, normalized, input_snapshot
    )
    return {**body, "preflight_sha256": canonical_json_sha256(body)}


def _validated_preflight(
    preflight: Mapping[str, Any],
    *,
    current_charter: Mapping[str, Any],
    normalized_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    document = _detach_object(preflight, field="preflight")
    expected_fields = {
        "kind",
        "schema_version",
        "base_charter_sha256",
        "base_claim_index_sha256",
        "proposal",
        "proposal_sha256",
        "input_snapshot",
        "change_set",
        "impact",
        "cost",
        "commit_guard",
        "preflight_sha256",
    }
    if set(document) != expected_fields:
        raise CharterAmendmentError("invalid_charter_amendment_preflight")
    if (
        document["kind"] != CHARTER_AMENDMENT_PREFLIGHT_KIND
        or isinstance(document["schema_version"], bool)
        or not isinstance(document["schema_version"], int)
        or document["schema_version"] != CHARTER_AMENDMENT_VERSION
    ):
        raise CharterAmendmentError("invalid_charter_amendment_preflight")
    body = {key: value for key, value in document.items() if key != "preflight_sha256"}
    if document["preflight_sha256"] != canonical_json_sha256(body):
        raise CharterAmendmentError("charter_amendment_preflight_identity_mismatch")
    current_hash = canonical_json_sha256(current_charter)
    current_index = _build_claim_index(current_charter)
    if (
        document["base_charter_sha256"] != current_hash
        or document["base_claim_index_sha256"] != current_index["claim_index_sha256"]
    ):
        raise CharterAmendmentError("charter_amendment_preflight_stale")
    if (
        document["proposal_sha256"] != normalized_proposal["proposal_sha256"]
        or document["proposal"] != normalized_proposal
    ):
        raise CharterAmendmentError("amendment_scope_exceeds_preflight")
    try:
        replayed_body = _charter_amendment_preflight_body(
            current_charter,
            normalized_proposal,
            document["input_snapshot"],
        )
    except CharterAmendmentError as exc:
        raise CharterAmendmentError(
            "charter_amendment_preflight_replay_mismatch"
        ) from exc
    if body != replayed_body:
        raise CharterAmendmentError(
            "charter_amendment_preflight_replay_mismatch"
        )
    return document


def charter_amendment_cost_acknowledgement(
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact cost fields a committer must explicitly echo.

    This helper is intended for displaying or projecting the acknowledgement
    contract.  Commit still requires the caller to supply the complete object
    independently; merely possessing the preflight is not treated as consent
    to its reconstruction cost.
    """

    document = _detach_object(preflight, field="preflight")
    cost = document.get("cost")
    if not isinstance(cost, dict) or not isinstance(
        document.get("preflight_sha256"), str
    ):
        raise CharterAmendmentError("invalid_charter_amendment_preflight")
    expected_cost_fields = {
        "minimum_reconstruction_scope",
        "operation_count",
        "affected_claim_count",
        "affected_root_field_count",
        "composition_dependencies_to_revalidate",
        "derivations_to_revalidate",
        "reviews_to_revalidate",
        "evidence_interpretations_to_revalidate",
        "observations_preserved",
        "hard_failures_preserved",
        "whole_work_consistency_review_required",
    }
    if set(cost) != expected_cost_fields:
        raise CharterAmendmentError("invalid_charter_amendment_preflight")
    return {
        "preflight_sha256": document["preflight_sha256"],
        **copy.deepcopy(cost),
    }


def _validated_cost_acknowledgement(
    value: Mapping[str, Any], *, preflight: Mapping[str, Any]
) -> dict[str, Any]:
    acknowledgement = _detach_object(value, field="cost_acknowledgement")
    expected = charter_amendment_cost_acknowledgement(preflight)
    if set(acknowledgement) != set(expected):
        raise CharterAmendmentError("invalid_cost_acknowledgement")
    integer_fields = {
        "operation_count",
        "affected_claim_count",
        "affected_root_field_count",
        "composition_dependencies_to_revalidate",
        "derivations_to_revalidate",
        "reviews_to_revalidate",
        "evidence_interpretations_to_revalidate",
        "observations_preserved",
        "hard_failures_preserved",
    }
    if any(
        isinstance(acknowledgement[field], bool)
        or not isinstance(acknowledgement[field], int)
        or acknowledgement[field] < 0
        for field in integer_fields
    ):
        raise CharterAmendmentError("invalid_cost_acknowledgement")
    if acknowledgement["minimum_reconstruction_scope"] not in {
        "bounded",
        "structural",
        "whole_work",
    } or not isinstance(
        acknowledgement["whole_work_consistency_review_required"], bool
    ):
        raise CharterAmendmentError("invalid_cost_acknowledgement")
    if acknowledgement != expected:
        raise CharterAmendmentError("charter_amendment_cost_not_acknowledged")
    return acknowledgement


def _validate_ledger_entry_shape(entry: Mapping[str, Any]) -> None:
    if set(entry) != {
        "kind",
        "schema_version",
        "sequence",
        "amendment_id",
        "initial_charter_sha256",
        "previous_effective_charter_sha256",
        "proposal",
        "proposal_sha256",
        "preflight",
        "preflight_sha256",
        "cost_acknowledgement",
        "change_set",
        "effective_charter_sha256",
        "amendment_sha256",
    }:
        raise CharterAmendmentError("invalid_charter_amendment_entry")


def verify_charter_amendment_ledger(
    initial_charter: Mapping[str, Any],
    amendments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay and verify a single monotonic amendment sequence."""

    initial = _detach_object(initial_charter, field="initial_charter")
    entries = _strict_sequence(amendments, field="amendments", maximum=MAX_AMENDMENTS)
    current = copy.deepcopy(initial)
    initial_hash = canonical_json_sha256(initial)
    verified_entries: list[dict[str, Any]] = []
    for offset, raw_entry in enumerate(entries):
        entry = _detach_object(raw_entry, field=f"amendments[{offset}]")
        _validate_ledger_entry_shape(entry)
        sequence = entry["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != offset + 1:
            raise CharterAmendmentError("invalid_charter_amendment_sequence")
        if (
            entry["kind"] != CHARTER_AMENDMENT_ENTRY_KIND
            or isinstance(entry["schema_version"], bool)
            or not isinstance(entry["schema_version"], int)
            or entry["schema_version"] != CHARTER_AMENDMENT_VERSION
        ):
            raise CharterAmendmentError("invalid_charter_amendment_entry")
        current_hash = canonical_json_sha256(current)
        if entry["initial_charter_sha256"] != initial_hash:
            raise CharterAmendmentError("charter_amendment_initial_hash_mismatch")
        if entry["previous_effective_charter_sha256"] != current_hash:
            raise CharterAmendmentError("charter_amendment_chain_mismatch")
        proposal = normalize_charter_patch_proposal(current, entry["proposal"])
        if entry["proposal_sha256"] != proposal["proposal_sha256"]:
            raise CharterAmendmentError("patch_proposal_identity_mismatch")
        preflight = _validated_preflight(
            entry["preflight"], current_charter=current, normalized_proposal=proposal
        )
        if entry["preflight_sha256"] != preflight["preflight_sha256"]:
            raise CharterAmendmentError("charter_amendment_preflight_identity_mismatch")
        _validated_cost_acknowledgement(
            entry["cost_acknowledgement"], preflight=preflight
        )
        next_charter = _apply_normalized_patch(current, proposal)
        change = _change_set(current, next_charter)
        if entry["change_set"] != change:
            raise CharterAmendmentError("charter_amendment_change_set_mismatch")
        if entry["effective_charter_sha256"] != canonical_json_sha256(next_charter):
            raise CharterAmendmentError("effective_charter_hash_mismatch")
        entry_body = {
            key: value
            for key, value in entry.items()
            if key not in {"amendment_id", "amendment_sha256"}
        }
        amendment_sha256 = canonical_json_sha256(entry_body)
        if (
            entry["amendment_sha256"] != amendment_sha256
            or entry["amendment_id"]
            != "charter-amendment-" + amendment_sha256[:20]
        ):
            raise CharterAmendmentError("charter_amendment_identity_mismatch")
        verified_entries.append(entry)
        current = next_charter
    return {
        "verified": True,
        "amendment_count": len(verified_entries),
        "initial_charter_sha256": initial_hash,
        "effective_charter_sha256": canonical_json_sha256(current),
        "effective_charter": current,
    }


def effective_charter_from_ledger(
    initial_charter: Mapping[str, Any],
    amendments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the effective charter derived from initial charter plus ledger."""

    return verify_charter_amendment_ledger(initial_charter, amendments)[
        "effective_charter"
    ]


def commit_charter_amendment(
    initial_charter: Mapping[str, Any],
    amendments: Sequence[Mapping[str, Any]],
    *,
    proposal: Mapping[str, Any],
    preflight: Mapping[str, Any],
    cost_acknowledgement: Mapping[str, Any],
    actual_effective_charter: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit exactly one preflighted patch to the monotonic ledger.

    ``actual_effective_charter`` is optional because the normal path lets this
    function construct the value.  If a caller has already materialized an
    edited charter, supplying it makes any undeclared or out-of-scope change a
    hard failure instead of silently accepting the larger rewrite.  The
    separate ``cost_acknowledgement`` must exactly echo the server-computed
    preflight cost, preventing a caller from committing without first seeing
    the reconstruction and revalidation obligations.
    """

    initial = _detach_object(initial_charter, field="initial_charter")
    entries = _strict_sequence(amendments, field="amendments", maximum=MAX_AMENDMENTS)
    if len(entries) >= MAX_AMENDMENTS:
        raise CharterAmendmentError("charter_amendment_limit_exceeded")
    verification = verify_charter_amendment_ledger(initial, entries)
    current = verification["effective_charter"]
    normalized = normalize_charter_patch_proposal(current, proposal)
    checked_preflight = _validated_preflight(
        preflight,
        current_charter=current,
        normalized_proposal=normalized,
    )
    checked_acknowledgement = _validated_cost_acknowledgement(
        cost_acknowledgement, preflight=checked_preflight
    )
    expected = _apply_normalized_patch(current, normalized)
    if actual_effective_charter is not None:
        actual = _detach_object(
            actual_effective_charter, field="actual_effective_charter"
        )
        if actual != expected:
            raise CharterAmendmentError("amendment_scope_exceeds_preflight")
    sequence = len(entries) + 1
    entry_body = {
        "kind": CHARTER_AMENDMENT_ENTRY_KIND,
        "schema_version": CHARTER_AMENDMENT_VERSION,
        "sequence": sequence,
        "initial_charter_sha256": verification["initial_charter_sha256"],
        "previous_effective_charter_sha256": verification[
            "effective_charter_sha256"
        ],
        "proposal": normalized,
        "proposal_sha256": normalized["proposal_sha256"],
        "preflight": checked_preflight,
        "preflight_sha256": checked_preflight["preflight_sha256"],
        "cost_acknowledgement": checked_acknowledgement,
        "change_set": checked_preflight["change_set"],
        "effective_charter_sha256": canonical_json_sha256(expected),
    }
    amendment_sha256 = canonical_json_sha256(entry_body)
    entry = {
        **entry_body,
        "amendment_id": "charter-amendment-" + amendment_sha256[:20],
        "amendment_sha256": amendment_sha256,
    }
    ledger = [*copy.deepcopy(entries), entry]
    # Replay before returning so the persisted shape and all derived hashes are
    # proven by the same verifier used on reopen.
    verified = verify_charter_amendment_ledger(initial, ledger)
    return {
        "effective_charter": verified["effective_charter"],
        "effective_charter_sha256": verified["effective_charter_sha256"],
        "ledger_entry": entry,
        "amendments": ledger,
    }


__all__ = [
    "CHARTER_AMENDMENT_ENTRY_KIND",
    "CHARTER_AMENDMENT_PREFLIGHT_KIND",
    "CHARTER_AMENDMENT_VERSION",
    "CHARTER_CLAIM_INDEX_KIND",
    "CHARTER_PATCH_PROPOSAL_KIND",
    "CharterAmendmentError",
    "apply_charter_patch",
    "charter_amendment_cost_acknowledgement",
    "commit_charter_amendment",
    "effective_charter_from_ledger",
    "index_charter_claims",
    "normalize_charter_patch_proposal",
    "preflight_charter_amendment",
    "verify_charter_amendment_ledger",
]
