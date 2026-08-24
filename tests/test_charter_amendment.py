from __future__ import annotations

import copy

import pytest

from tianlai.canonical_json import canonical_json_sha256
from tianlai.charter_amendment import (
    CharterAmendmentError,
    apply_charter_patch,
    charter_amendment_cost_acknowledgement,
    commit_charter_amendment,
    effective_charter_from_ledger,
    index_charter_claims,
    normalize_charter_patch_proposal,
    preflight_charter_amendment,
    verify_charter_amendment_ledger,
)


def _charter() -> dict[str, object]:
    return {
        "title": "A bounded work",
        "one_sentence_promise": "One small contour earns a single opening.",
        "target_listener_and_scene": "A focused listener at night.",
        "primary_sovereignty": ["M"],
        "secondary_sovereignties": ["T"],
        "identity_kernel": {
            "invariants": ["falling three-note contour", "one foreground voice"],
            "transformable_parts": ["register", "instrument"],
        },
        "energy_curve": [
            {"position": 0.0, "intent": "withheld"},
            {"position": 1.0, "intent": "opened"},
        ],
        "scarce_resources": ["full register"],
        "ending_contract": "The opening answers the first contour.",
    }


def _by_path(index: dict[str, object], *path: str):
    return next(
        item
        for item in index["claims"]
        if item["field_path"] == list(path)
    )


def _list_item(index: dict[str, object], path: tuple[str, ...], value: object):
    return next(
        item
        for item in index["claims"]
        if item["field_path"] == list(path) and item["value"] == value
    )


def _collection(index: dict[str, object], *path: str):
    return next(
        item
        for item in index["collections"]
        if item["field_path"] == list(path)
    )


def _proposal(
    operations: list[dict[str, object]], *, basis_ids: list[str] | None = None
) -> dict[str, object]:
    return {
        "summary": "The original promise blocks the discovered consequence.",
        "why_score_revision_is_insufficient": (
            "Keeping the old promise would make the new ending self-contradictory."
        ),
        "why_bounded_exception_is_insufficient": (
            "The conflict spans the whole work rather than one exceptional passage."
        ),
        "expected_gain": "The ending can answer material discovered during iteration.",
        "accepted_costs": ["Recheck the complete form", "Revalidate old derivations"],
        "replacement_constraints": [
            "The discovered opening must still grow from the original contour."
        ],
        "failure_conditions": [
            "The new ending could be transplanted into an unrelated work."
        ],
        "basis_ids": basis_ids or ["evidence-promise"],
        "operations": operations,
    }


def _replace_promise(charter: dict[str, object]) -> dict[str, object]:
    claim = _by_path(index_charter_claims(charter), "one_sentence_promise")
    return _proposal(
        [
            {
                "op": "replace",
                "claim_id": claim["claim_id"],
                "value": "One small contour earns an opening that changes its own law.",
            }
        ]
    )


def _evidence() -> list[dict[str, object]]:
    return [
        {
            "evidence_id": "evidence-promise",
            "category": "promise_conflict",
            "basis": {
                "kind": "declared_promise",
                "reference": "one_sentence_promise",
            },
            "observation": "The last section recalls a transformed contour.",
            "interpretation": "The old promise excludes that consequence.",
        },
        {
            "evidence_id": "evidence-hard",
            "category": "hard_failure",
            "basis": {"kind": "engine_contract", "reference": "invalid_range"},
            "observation": "A trusted validator reproduced invalid_range.",
            "interpretation": "Rendering remains blocked.",
        },
        {
            "evidence_id": "evidence-identity",
            "category": "aesthetic_risk",
            "claim_ids": [
                _list_item(
                    index_charter_claims(_charter()),
                    ("identity_kernel", "invariants"),
                    "falling three-note contour",
                )["claim_id"]
            ],
            "basis": {"kind": "diagnostic_hypothesis", "reference": "identity"},
            "observation": "The contour is absent in one transition.",
            "interpretation": "The identity may weaken there.",
        },
    ]


def _preflight(
    charter: dict[str, object], proposal: dict[str, object]
) -> dict[str, object]:
    index = index_charter_claims(charter)
    promise_id = _by_path(index, "one_sentence_promise")["claim_id"]
    identity_id = _list_item(
        index,
        ("identity_kernel", "invariants"),
        "falling three-note contour",
    )["claim_id"]
    return preflight_charter_amendment(
        charter,
        proposal,
        composition_map_dependencies={
            "kind": "tianlai.composition_map",
            "schema_version": 1,
            "nodes": [
                {"node_id": "map-ending", "depends_on_claim_ids": [promise_id]},
                {"dependency_id": "map-contour", "claim_ids": [identity_id]},
            ],
        },
        derivations=[
            {
                "derivation_id": "derivation-ending",
                "premises": [
                    {
                        "kind": "declared_promise",
                        "reference": "one_sentence_promise",
                    }
                ],
            },
            {
                "derivation_id": "derivation-material-only",
                "premises": [{"kind": "established_material", "reference": None}],
            },
        ],
        reviews=[
            {"review_id": "review-unscoped", "phase": "intent"},
            {
                "review_id": "review-identity",
                "phase": "symbolic_structure",
                "coverage": {"claim_ids": [identity_id]},
            },
        ],
        evidence=_evidence(),
    )


def _error_code(call) -> str:
    with pytest.raises(CharterAmendmentError) as captured:
        call()
    return captured.value.code


def test_claim_ids_are_stable_across_unrelated_change_and_list_reorder() -> None:
    charter = _charter()
    first = index_charter_claims(charter)
    changed = copy.deepcopy(charter)
    changed["ending_contract"] = "A different ending contract."
    changed["identity_kernel"]["invariants"].reverse()
    second = index_charter_claims(changed)

    assert _by_path(first, "one_sentence_promise")["claim_id"] == _by_path(
        second, "one_sentence_promise"
    )["claim_id"]
    for value in charter["identity_kernel"]["invariants"]:
        assert _list_item(
            first, ("identity_kernel", "invariants"), value
        )["claim_id"] == _list_item(
            second, ("identity_kernel", "invariants"), value
        )["claim_id"]
    assert first["charter_sha256"] != second["charter_sha256"]


def test_normalized_proposal_is_json_persistable_and_idempotent() -> None:
    charter = _charter()
    normalized = normalize_charter_patch_proposal(charter, _replace_promise(charter))
    assert normalized["kind"] == "tianlai.charter_patch_proposal"
    assert normalized["proposal_id"].startswith("charter-proposal-")
    assert normalized["proposal_sha256"] == canonical_json_sha256(
        {
            key: value
            for key, value in normalized.items()
            if key not in {"proposal_id", "proposal_sha256"}
        }
    )
    assert normalize_charter_patch_proposal(charter, normalized) == normalized


def test_whole_charter_replacement_and_undeclared_fields_are_forbidden() -> None:
    charter = _charter()
    proposal = _replace_promise(charter)
    proposal["work_charter"] = copy.deepcopy(charter)
    assert (
        _error_code(lambda: normalize_charter_patch_proposal(charter, proposal))
        == "invalid_patch_proposal_shape"
    )


def test_add_position_is_strict_integer_and_rejects_bool() -> None:
    charter = _charter()
    collection_id = _collection(
        index_charter_claims(charter), "scarce_resources"
    )["collection_id"]
    proposal = _proposal(
        [
            {
                "op": "add",
                "collection_id": collection_id,
                "position": True,
                "value": "highest register",
            }
        ]
    )
    assert (
        _error_code(lambda: normalize_charter_patch_proposal(charter, proposal))
        == "invalid_patch_position"
    )


def test_removal_without_a_replacement_constraint_operation_is_rejected() -> None:
    charter = _charter()
    target = _list_item(
        index_charter_claims(charter),
        ("scarce_resources",),
        "full register",
    )
    proposal = _proposal([{"op": "remove", "claim_id": target["claim_id"]}])
    assert (
        _error_code(lambda: normalize_charter_patch_proposal(charter, proposal))
        == "deletion_requires_replacement"
    )


def test_remove_and_add_share_the_original_collection_coordinate_system() -> None:
    charter = _charter()
    index = index_charter_claims(charter)
    path = ("identity_kernel", "invariants")
    removed = _list_item(index, path, "falling three-note contour")
    collection_id = _collection(index, *path)["collection_id"]
    proposal = _proposal(
        [
            {"op": "remove", "claim_id": removed["claim_id"]},
            {
                "op": "add",
                "collection_id": collection_id,
                "position": 1,
                "value": "transformed three-note answer",
            },
            {
                "op": "add",
                "collection_id": collection_id,
                "position": 0,
                "value": "withheld opening gesture",
            },
        ]
    )

    patched = apply_charter_patch(charter, proposal)

    assert patched["identity_kernel"]["invariants"] == [
        "withheld opening gesture",
        "transformed three-note answer",
        "one foreground voice",
    ]


def test_preflight_makes_reconstruction_cost_and_revalidation_explicit() -> None:
    charter = _charter()
    preflight = _preflight(charter, _replace_promise(charter))

    dependencies = {
        item["dependency_id"]: item
        for item in preflight["impact"]["composition_map_dependencies"]
    }
    assert dependencies["map-ending"]["status"] == "needs_revalidation"
    assert dependencies["map-contour"]["status"] == "preserved"
    derivations = {
        item["derivation_id"]: item
        for item in preflight["impact"]["derivations"]
    }
    assert derivations["derivation-ending"]["status"] == "needs_revalidation"
    assert derivations["derivation-material-only"]["status"] == "preserved"
    reviews = {
        item["review_id"]: item for item in preflight["impact"]["reviews"]
    }
    assert reviews["review-unscoped"]["status"] == "needs_revalidation"
    assert reviews["review-identity"]["status"] == "preserved"
    assert preflight["cost"]["minimum_reconstruction_scope"] == "whole_work"
    assert preflight["cost"]["whole_work_consistency_review_required"] is True


def test_unscoped_composition_dependency_does_not_block_and_is_revalidated() -> None:
    charter = _charter()
    preflight = preflight_charter_amendment(
        charter,
        _replace_promise(charter),
        composition_map_dependencies=[{"node_id": "map-unscoped"}],
        evidence=_evidence(),
    )

    dependency = preflight["impact"]["composition_map_dependencies"][0]
    assert dependency == {
        "dependency_id": "map-unscoped",
        "claim_ids": [],
        "collection_ids": [],
        "status": "needs_revalidation",
        "reason": "unscoped_composition_dependency_cannot_prove_independence",
    }
    assert preflight["cost"]["composition_dependencies_to_revalidate"] == 1


def test_duplicate_composition_dependency_identity_is_rejected() -> None:
    charter = _charter()
    assert (
        _error_code(
            lambda: preflight_charter_amendment(
                charter,
                _replace_promise(charter),
                composition_map_dependencies=[
                    {"node_id": "map-duplicate"},
                    {"node_id": "map-duplicate"},
                ],
                evidence=_evidence(),
            )
        )
        == "duplicate_preflight_record_id"
    )


def test_input_snapshot_persists_minimal_resolved_basis_claim_scope() -> None:
    charter = _charter()
    preflight = _preflight(charter, _replace_promise(charter))
    promise_id = _by_path(
        index_charter_claims(charter), "one_sentence_promise"
    )["claim_id"]
    evidence = {
        item["evidence_id"]: item
        for item in preflight["input_snapshot"]["evidence"]
    }

    assert set(preflight["input_snapshot"]) == {
        "composition_map_dependencies",
        "derivations",
        "reviews",
        "evidence",
    }
    assert evidence["evidence-promise"] == {
        "evidence_id": "evidence-promise",
        "category": "promise_conflict",
        "claim_ids": [promise_id],
        "collection_ids": [],
    }


def test_observations_survive_while_dependent_interpretations_revalidate() -> None:
    charter = _charter()
    preflight = _preflight(charter, _replace_promise(charter))
    evidence = {
        item["evidence_id"]: item for item in preflight["impact"]["evidence"]
    }

    assert evidence["evidence-promise"]["observation_status"] == "preserved"
    assert (
        evidence["evidence-promise"]["interpretation_status"]
        == "needs_revalidation"
    )
    assert evidence["evidence-identity"]["observation_status"] == "preserved"
    assert evidence["evidence-identity"]["interpretation_status"] == "preserved"


def test_unscoped_non_hard_evidence_interpretation_is_revalidated() -> None:
    charter = _charter()
    evidence_source = [
        *_evidence(),
        {
            "evidence_id": "evidence-unscoped",
            "category": "aesthetic_risk",
            "basis": {"kind": "diagnostic_hypothesis", "reference": "form"},
            "observation": "A transition felt disconnected in the report.",
            "interpretation": "The charter may be responsible.",
        },
    ]
    preflight = preflight_charter_amendment(
        charter,
        _replace_promise(charter),
        evidence=evidence_source,
    )
    evidence = {
        item["evidence_id"]: item for item in preflight["impact"]["evidence"]
    }

    assert evidence["evidence-unscoped"]["observation_status"] == "preserved"
    assert (
        evidence["evidence-unscoped"]["interpretation_status"]
        == "needs_revalidation"
    )
    assert (
        evidence["evidence-unscoped"]["reason"]
        == "unscoped_evidence_cannot_prove_independence"
    )


def test_standalone_preflight_rejects_unknown_evidence_category() -> None:
    charter = _charter()
    invalid = copy.deepcopy(_evidence())
    invalid[0]["category"] = "subjective_failure"

    assert (
        _error_code(
            lambda: preflight_charter_amendment(
                charter, _replace_promise(charter), evidence=invalid
            )
        )
        == "invalid_evidence_for_preflight"
    )


def test_non_hard_basis_must_be_related_to_the_affected_claim_domain() -> None:
    charter = _charter()
    proposal = _replace_promise(charter)
    proposal["basis_ids"] = ["evidence-identity", "evidence-hard"]

    assert (
        _error_code(
            lambda: preflight_charter_amendment(
                charter, proposal, evidence=_evidence()
            )
        )
        == "charter_amendment_requires_related_non_hard_basis"
    )


def test_basis_for_existing_claim_can_support_addition_in_the_same_root() -> None:
    charter = _charter()
    index = index_charter_claims(charter)
    collection_id = _collection(
        index, "identity_kernel", "invariants"
    )["collection_id"]
    proposal = _proposal(
        [
            {
                "op": "add",
                "collection_id": collection_id,
                "position": 2,
                "value": "the answer retains the contour rhythm",
            }
        ],
        basis_ids=["evidence-identity"],
    )

    preflight = preflight_charter_amendment(
        charter, proposal, evidence=_evidence()
    )

    assert preflight["change_set"]["affected_root_fields"] == [
        "identity_kernel"
    ]


def test_empty_collection_scope_can_support_its_first_addition() -> None:
    charter = _charter()
    charter["secondary_sovereignties"] = []
    collection_id = _collection(
        index_charter_claims(charter), "secondary_sovereignties"
    )["collection_id"]
    proposal = _proposal(
        [
            {
                "op": "add",
                "collection_id": collection_id,
                "position": 0,
                "value": "U",
            }
        ],
        basis_ids=["evidence-empty-secondary"],
    )
    preflight = preflight_charter_amendment(
        charter,
        proposal,
        evidence=[
            {
                "evidence_id": "evidence-empty-secondary",
                "category": "promise_conflict",
                "basis": {
                    "kind": "declared_promise",
                    "reference": "secondary_sovereignties",
                },
                "observation": "A secondary sovereignty emerged in iteration.",
                "interpretation": "The empty collection no longer describes the work.",
            }
        ],
    )
    snapshot_evidence = preflight["input_snapshot"]["evidence"][0]
    impact_evidence = preflight["impact"]["evidence"][0]

    assert snapshot_evidence["claim_ids"] == []
    assert snapshot_evidence["collection_ids"] == [collection_id]
    assert impact_evidence["claim_ids"] == []
    assert impact_evidence["collection_ids"] == [collection_id]
    assert impact_evidence["interpretation_status"] == "needs_revalidation"
    assert impact_evidence["reason"] == "charter_interpretation_changed"


def test_technical_hard_failure_never_expires_and_cannot_be_sole_basis() -> None:
    charter = _charter()
    preflight = _preflight(charter, _replace_promise(charter))
    hard = next(
        item
        for item in preflight["impact"]["evidence"]
        if item["evidence_id"] == "evidence-hard"
    )
    assert hard["observation_status"] == "preserved"
    assert hard["interpretation_status"] == "preserved"
    assert hard["reason"] == "technical_hard_failure_survives_charter_change"

    proposal = _replace_promise(charter)
    proposal["basis_ids"] = ["evidence-hard"]
    assert (
        _error_code(
            lambda: preflight_charter_amendment(
                charter, proposal, evidence=_evidence()
            )
        )
        == "charter_amendment_requires_non_hard_basis"
    )


def test_commit_builds_linear_ledger_and_replays_effective_charter() -> None:
    initial = _charter()
    first_proposal = _replace_promise(initial)
    first_preflight = _preflight(initial, first_proposal)
    first = commit_charter_amendment(
        initial,
        [],
        proposal=first_proposal,
        preflight=first_preflight,
        cost_acknowledgement=charter_amendment_cost_acknowledgement(
            first_preflight
        ),
    )
    assert first["ledger_entry"]["sequence"] == 1
    assert "parent" not in first["ledger_entry"]
    assert initial["one_sentence_promise"] != first["effective_charter"][
        "one_sentence_promise"
    ]

    effective = first["effective_charter"]
    ending_claim = _by_path(index_charter_claims(effective), "ending_contract")
    second_proposal = _proposal(
        [
            {
                "op": "replace",
                "claim_id": ending_claim["claim_id"],
                "value": "The ending transforms, rather than quotes, the first contour.",
            }
        ],
        basis_ids=["review-ending"],
    )
    second_preflight = preflight_charter_amendment(
        effective,
        second_proposal,
        reviews=[
            {
                "review_id": "review-ending",
                "phase": "intent",
                "claim_ids": [ending_claim["claim_id"]],
            }
        ],
    )
    second = commit_charter_amendment(
        initial,
        first["amendments"],
        proposal=second_proposal,
        preflight=second_preflight,
        cost_acknowledgement=charter_amendment_cost_acknowledgement(
            second_preflight
        ),
    )
    assert second["ledger_entry"]["sequence"] == 2
    verification = verify_charter_amendment_ledger(initial, second["amendments"])
    assert verification["verified"] is True
    assert verification["amendment_count"] == 2
    assert effective_charter_from_ledger(initial, second["amendments"]) == second[
        "effective_charter"
    ]


def test_actual_extra_change_cannot_hide_behind_preflight() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    actual = copy.deepcopy(initial)
    actual["one_sentence_promise"] = (
        "One small contour earns an opening that changes its own law."
    )
    actual["ending_contract"] = "An undeclared replacement ending."

    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=proposal,
                preflight=preflight,
                cost_acknowledgement=charter_amendment_cost_acknowledgement(
                    preflight
                ),
                actual_effective_charter=actual,
            )
        )
        == "amendment_scope_exceeds_preflight"
    )


def test_changed_proposal_requires_a_new_preflight() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    ending = _by_path(index_charter_claims(initial), "ending_contract")
    expanded = copy.deepcopy(proposal)
    expanded["operations"].append(
        {
            "op": "replace",
            "claim_id": ending["claim_id"],
            "value": "An additional, undeclared ending change.",
        }
    )
    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=expanded,
                preflight=preflight,
                cost_acknowledgement=charter_amendment_cost_acknowledgement(
                    preflight
                ),
            )
        )
        == "amendment_scope_exceeds_preflight"
    )


def test_commit_requires_exact_cost_acknowledgement_before_change() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    acknowledgement = charter_amendment_cost_acknowledgement(preflight)
    acknowledgement["derivations_to_revalidate"] -= 1
    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=proposal,
                preflight=preflight,
                cost_acknowledgement=acknowledgement,
            )
        )
        == "charter_amendment_cost_not_acknowledged"
    )

    bool_acknowledgement = charter_amendment_cost_acknowledgement(preflight)
    bool_acknowledgement["affected_claim_count"] = True
    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=proposal,
                preflight=preflight,
                cost_acknowledgement=bool_acknowledgement,
            )
        )
        == "invalid_cost_acknowledgement"
    )


def test_commit_replays_snapshot_and_rejects_rehashed_forged_cost() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    forged = copy.deepcopy(preflight)
    forged["cost"]["derivations_to_revalidate"] = 0
    forged_body = {
        key: value for key, value in forged.items() if key != "preflight_sha256"
    }
    forged["preflight_sha256"] = canonical_json_sha256(forged_body)

    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=proposal,
                preflight=forged,
                cost_acknowledgement=charter_amendment_cost_acknowledgement(
                    forged
                ),
            )
        )
        == "charter_amendment_preflight_replay_mismatch"
    )


def test_ledger_replay_rejects_rehashed_forged_cost() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    committed = commit_charter_amendment(
        initial,
        [],
        proposal=proposal,
        preflight=preflight,
        cost_acknowledgement=charter_amendment_cost_acknowledgement(preflight),
    )
    forged = copy.deepcopy(committed["amendments"])
    entry = forged[0]
    entry["preflight"]["cost"]["derivations_to_revalidate"] = 0
    preflight_body = {
        key: value
        for key, value in entry["preflight"].items()
        if key != "preflight_sha256"
    }
    entry["preflight"]["preflight_sha256"] = canonical_json_sha256(
        preflight_body
    )
    entry["preflight_sha256"] = entry["preflight"]["preflight_sha256"]
    entry["cost_acknowledgement"] = charter_amendment_cost_acknowledgement(
        entry["preflight"]
    )
    entry_body = {
        key: value
        for key, value in entry.items()
        if key not in {"amendment_id", "amendment_sha256"}
    }
    entry["amendment_sha256"] = canonical_json_sha256(entry_body)
    entry["amendment_id"] = (
        "charter-amendment-" + entry["amendment_sha256"][:20]
    )

    assert (
        _error_code(lambda: verify_charter_amendment_ledger(initial, forged))
        == "charter_amendment_preflight_replay_mismatch"
    )


def test_ledger_sequence_rejects_bool_even_though_bool_is_an_int_subclass() -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    preflight = _preflight(initial, proposal)
    result = commit_charter_amendment(
        initial,
        [],
        proposal=proposal,
        preflight=preflight,
        cost_acknowledgement=charter_amendment_cost_acknowledgement(
            preflight
        ),
    )
    tampered = copy.deepcopy(result["amendments"])
    tampered[0]["sequence"] = True
    assert (
        _error_code(lambda: verify_charter_amendment_ledger(initial, tampered))
        == "invalid_charter_amendment_sequence"
    )


@pytest.mark.parametrize("invalid_version", [True, 1.0])
def test_all_amendment_schema_versions_require_strict_integer(
    invalid_version: object,
) -> None:
    initial = _charter()
    proposal = _replace_promise(initial)
    normalized = normalize_charter_patch_proposal(initial, proposal)
    invalid_proposal = copy.deepcopy(normalized)
    invalid_proposal["schema_version"] = invalid_version
    assert (
        _error_code(
            lambda: normalize_charter_patch_proposal(initial, invalid_proposal)
        )
        == "invalid_patch_proposal_version"
    )

    preflight = _preflight(initial, proposal)
    acknowledgement = charter_amendment_cost_acknowledgement(preflight)
    invalid_preflight = copy.deepcopy(preflight)
    invalid_preflight["schema_version"] = invalid_version
    assert (
        _error_code(
            lambda: commit_charter_amendment(
                initial,
                [],
                proposal=proposal,
                preflight=invalid_preflight,
                cost_acknowledgement=acknowledgement,
            )
        )
        == "invalid_charter_amendment_preflight"
    )

    committed = commit_charter_amendment(
        initial,
        [],
        proposal=proposal,
        preflight=preflight,
        cost_acknowledgement=acknowledgement,
    )
    invalid_ledger = copy.deepcopy(committed["amendments"])
    invalid_ledger[0]["schema_version"] = invalid_version
    assert (
        _error_code(
            lambda: verify_charter_amendment_ledger(initial, invalid_ledger)
        )
        == "invalid_charter_amendment_entry"
    )
