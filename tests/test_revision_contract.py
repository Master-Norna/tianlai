from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import tianlai.creative_workflow as workflow_module
from tianlai.authoring_project import create_authoring_project, save_authoring_project
from tianlai.creative_workflow import (
    CreativeWorkflowError,
    activate_creative_workflow,
    create_creative_workflow,
    decide_workflow_iteration,
    record_workflow_authoring_revision,
    record_workflow_evidence,
    record_workflow_review,
)


def _charter() -> dict[str, object]:
    return {
        "title": "Revision contract test",
        "one_sentence_promise": "One idea changes only through declared work.",
        "target_listener_and_scene": "A focused listener.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening identity"],
            "transformable_parts": ["surface detail"],
        },
        "ending_contract": "End with a consequence.",
    }


def _metadata_scope(*, documents: list[str] | None = None) -> dict[str, object]:
    selected = ["score"] if documents is None else documents
    return {
        "change_scale": "bounded",
        "documents": selected,
        "allowed_document_paths": {
            document: (["/tail_seconds"] if document == "score" else [])
            for document in selected
        },
        "score": (
            {
                "part_ids": [],
                "event_ids": [],
                "bar_ranges": [],
                "allowed_note_fields": [],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            }
            if "score" in selected
            else None
        ),
        "whole_work_cost": None,
    }


def _event_scope(event_ids: list[str]) -> dict[str, object]:
    return {
        "change_scale": "bounded",
        "documents": ["score"],
        "allowed_document_paths": {"score": []},
        "score": {
            "part_ids": [],
            "event_ids": event_ids,
            "bar_ranges": [],
            "allowed_note_fields": ["pitch"],
            "allow_event_additions": False,
            "allow_event_deletions": False,
            "allow_reordering": False,
        },
        "whole_work_cost": None,
    }


def _error_code(call) -> str:
    with unittest.TestCase().assertRaises(CreativeWorkflowError) as captured:
        call()
    return captured.exception.code


class RevisionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "revision-contract"
        self.authoring = create_authoring_project(self.root, title="Contract")

    def _reviewed_target(self):
        created = create_creative_workflow(
            self.root, mode="iterate", final_authority="agent"
        )
        active = activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
        )
        reviewed = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="Review the declared revision without claiming listening.",
        )
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            category="aesthetic_risk",
            code="revision.bounded_test",
            basis_kind="diagnostic_hypothesis",
            basis_reference="declared bounded test",
            reporter="agent",
            perception_basis="report_only",
            summary="A bounded change is worth testing.",
            observation="The declared target has not been tested.",
            interpretation="Only the named scope should change.",
            confidence="medium",
        )
        iteration = evidence.detached_state()["iterations"][-1]
        return evidence, iteration["reviews"][0]["review_id"], iteration["evidence"][0]["evidence_id"]

    def _revise(self, snapshot, review_id: str, evidence_id: str, *, scope):
        return decide_workflow_iteration(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            disposition="revise",
            summary="Run the bounded test.",
            rationale="The contract makes the structural cost inspectable.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[{
                "evidence_id": evidence_id,
                "disposition": "revision_target",
                "rationale": "This is the exact target.",
                "basis_ids": [review_id],
            }],
            expected_audible_change="Only the declared target changes.",
            revision_scope=scope,
            withdrawal_condition="Withdraw if the edit crosses the declared scope.",
        )

    def test_bounded_metadata_revision_records_hash_bound_contract(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        pending = self._revise(snapshot, review_id, evidence_id, scope=_metadata_scope())
        contract = pending.detached_state()["iterations"][-1]["decision"]["revision_contract"]
        self.assertEqual(contract["baseline"]["authoring_revision"], self.authoring.revision)
        self.assertRegex(contract["contract_sha256"], r"^[0-9a-f]{64}$")
        documents = self.authoring.detached_documents()
        documents["score"]["tail_seconds"] = 2.5
        revised = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        recorded = record_workflow_authoring_revision(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            authoring_revision=revised.revision,
        )
        self.assertEqual(recorded.detached_state()["status"], "reviewing")

    def test_undeclared_document_change_is_rejected(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        pending = self._revise(snapshot, review_id, evidence_id, scope=_metadata_scope())
        documents = self.authoring.detached_documents()
        documents["authoring_roster"]["name"] = "changed outside contract"
        revised = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        self.assertEqual(
            _error_code(lambda: record_workflow_authoring_revision(
                self.root, workflow_id=pending.workflow_id,
                expected_revision=pending.revision, authoring_revision=revised.revision,
            )),
            "revision_scope_document_overreach",
        )

    def test_exact_roster_leaf_path_allows_only_that_leaf(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        scope = {
            "change_scale": "bounded",
            "documents": ["authoring_roster"],
            "allowed_document_paths": {"authoring_roster": ["/name"]},
            "score": None,
            "whole_work_cost": None,
        }
        pending = self._revise(snapshot, review_id, evidence_id, scope=scope)
        documents = self.authoring.detached_documents()
        documents["authoring_roster"]["name"] = "one exact roster leaf"
        revised = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        recorded = record_workflow_authoring_revision(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            authoring_revision=revised.revision,
        )
        self.assertEqual(recorded.detached_state()["status"], "reviewing")

    def test_document_path_is_exact_not_a_prefix(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        scope = {
            "change_scale": "bounded",
            "documents": ["render_profile"],
            "allowed_document_paths": {"render_profile": ["/space"]},
            "score": None,
            "whole_work_cost": None,
        }
        pending = self._revise(snapshot, review_id, evidence_id, scope=scope)
        documents = self.authoring.detached_documents()
        documents["render_profile"]["space"]["config"]["wet_db"] = -14.0
        revised = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        self.assertEqual(
            _error_code(lambda: record_workflow_authoring_revision(
                self.root, workflow_id=pending.workflow_id,
                expected_revision=pending.revision, authoring_revision=revised.revision,
            )),
            "revision_scope_document_path_overreach",
        )

    def test_document_paths_reject_root_bad_escape_and_score_note_bypass(self) -> None:
        cases = (
            ("", "invalid_revision_document_path"),
            ("/bad~2escape", "invalid_revision_document_path"),
            ("/parts/0/notes/0/pitch", "score_note_path_must_use_event_scope"),
        )
        for pointer, expected in cases:
            with self.subTest(pointer=pointer):
                scope = _metadata_scope()
                scope["allowed_document_paths"] = {"score": [pointer]}
                self.assertEqual(
                    _error_code(lambda scope=scope: workflow_module._normalize_revision_scope(scope)),
                    expected,
                )

    def test_changed_leaf_expansion_includes_empty_container_and_new_leaf(self) -> None:
        self.assertEqual(
            workflow_module._changed_json_leaf_pointers(
                {"items": []}, {"items": [{"value": 1}]}
            ),
            {"/items", "/items/0/value"},
        )

    def test_out_of_scope_intermediate_then_restored_final_is_rejected(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        pending = self._revise(snapshot, review_id, evidence_id, scope=_metadata_scope())
        overreaching = self.authoring.detached_documents()
        overreaching["authoring_roster"]["name"] = "temporary whole-project rewrite"
        intermediate = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=overreaching,
        )
        restored = self.authoring.detached_documents()
        restored["score"]["tail_seconds"] = 2.75
        revised = save_authoring_project(
            self.root,
            expected_revision=intermediate.revision,
            documents=restored,
        )
        self.assertEqual(
            _error_code(lambda: record_workflow_authoring_revision(
                self.root,
                workflow_id=pending.workflow_id,
                expected_revision=pending.revision,
                authoring_revision=revised.revision,
            )),
            "revision_scope_document_overreach",
        )

    def test_authoring_head_must_not_drift_before_contract(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        documents = self.authoring.detached_documents()
        documents["score"]["tail_seconds"] = 2.25
        save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        self.assertEqual(
            _error_code(lambda: self._revise(
                snapshot, review_id, evidence_id, scope=_metadata_scope()
            )),
            "authoring_changed_before_revision_contract",
        )

    def test_whole_work_requires_all_three_costs(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        scope = {
            "change_scale": "whole_work",
            "documents": ["score"],
            "allowed_document_paths": None,
            "score": None,
            "whole_work_cost": {
                "accepted_costs": ["expanded_change_surface"],
                "rationale": "A broad rewrite is intended.",
            },
        }
        self.assertEqual(
            _error_code(lambda: self._revise(snapshot, review_id, evidence_id, scope=scope)),
            "whole_work_cost_acknowledgement_required",
        )

    def test_full_scan_rejects_overreach_after_1024_authorized_events(self) -> None:
        documents = self.authoring.detached_documents()
        notes = []
        for index in range(1025):
            notes.append({
                "event_id": f"event-{index:04d}",
                "bar": 1 + index // 4,
                "beat": 1.0,
                "duration_beats": 1.0,
                "pitch": 60,
            })
        documents["score"]["parts"][0]["notes"] = notes
        self.authoring = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=documents
        )
        snapshot, review_id, evidence_id = self._reviewed_target()
        pending = self._revise(
            snapshot, review_id, evidence_id,
            scope=_event_scope([f"event-{index:04d}" for index in range(1024)]),
        )
        changed = self.authoring.detached_documents()
        for note in changed["score"]["parts"][0]["notes"]:
            note["pitch"] = 61
        revised = save_authoring_project(
            self.root, expected_revision=self.authoring.revision, documents=changed
        )
        self.assertEqual(
            _error_code(lambda: record_workflow_authoring_revision(
                self.root, workflow_id=pending.workflow_id,
                expected_revision=pending.revision, authoring_revision=revised.revision,
            )),
            "revision_scope_event_update_overreach",
        )

    def test_legacy_pending_revision_delays_policy_upgrade(self) -> None:
        snapshot, review_id, evidence_id = self._reviewed_target()
        pending = self._revise(snapshot, review_id, evidence_id, scope=_metadata_scope())
        state = pending.detached_state()
        state["iterations"][-1]["decision"].pop("revision_contract")
        state["policy"] = dict(workflow_module._SETTLEMENT_POLICY)
        workflow_module._upgrade_legacy_derivation_shape_for_transition(state)
        self.assertNotIn("revision_contract_profile", state["policy"])
        state["status"] = "reviewing"
        state["iterations"][-1]["status"] = "reviewing"
        state["iterations"][-1]["decision"] = None
        workflow_module._upgrade_legacy_derivation_shape_for_transition(state)
        self.assertEqual(
            state["policy"]["revision_contract_profile"],
            "bounded-change-and-explicit-challenger-settlement-v1",
        )

    def test_assessment_actions_and_terminal_selection_are_factual(self) -> None:
        baseline = {"candidate_id": "baseline", "work_id": "work", "candidate_manifest_sha256": "1" * 64}
        baseline_locator = {"candidate_id": "baseline", "work_id": "work", "manifest_sha256": "1" * 64}
        challenger = {"candidate_id": "challenger", "work_id": "work", "candidate_manifest_sha256": "2" * 64}
        contract = {
            "contract_sha256": "3" * 64,
            "baseline": {
                "candidate": baseline_locator,
                "candidate_source_iteration_number": 1,
            },
        }
        source = {"outcome": "revised", "decision": {"revision_contract": contract}, "anchor": {"candidate": baseline}}
        current = {"iteration_number": 2, "anchor": {"candidate": challenger}, "decision": None}
        state = {"iterations": [source, current]}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current), baseline
        )
        current["decision"] = {"prior_revision_assessment": {"outcome": "promote_challenger"}}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current), challenger
        )
        current["decision"] = {"prior_revision_assessment": {"outcome": "retain_baseline"}}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current), baseline
        )

    def test_retain_can_withdraw_before_render_but_promote_cannot(self) -> None:
        contract = {"contract_sha256": "3" * 64}
        iteration = {
            "anchor": {"candidate": None},
            "reviews": [{
                "review_id": "review-" + "1" * 20,
                "candidate_id": None,
                "reviewer": "agent",
                "perception_basis": "report_only",
            }],
        }
        decision = {"final_authority": "agent", "perception_basis": "report_only"}
        basis_id = iteration["reviews"][0]["review_id"]
        retain = {
            "contract_sha256": "3" * 64,
            "outcome": "retain_baseline",
            "rationale": "Withdraw before spending a render on this challenger.",
            "basis_ids": [basis_id],
        }
        workflow_module._validate_prior_revision_assessment(
            retain,
            contract=contract,
            iteration=iteration,
            decision=decision,
            selected_basis_ids={basis_id},
        )
        promote = dict(retain)
        promote["outcome"] = "promote_challenger"
        self.assertEqual(
            _error_code(lambda: workflow_module._validate_prior_revision_assessment(
                promote,
                contract=contract,
                iteration=iteration,
                decision=decision,
                selected_basis_ids={basis_id},
            )),
            "revision_assessment_requires_challenger",
        )
        too_many = dict(retain)
        too_many["basis_ids"] = [basis_id] + [
            f"evidence-{index:020x}" for index in range(16)
        ]
        self.assertEqual(
            _error_code(lambda: workflow_module._validate_prior_revision_assessment(
                too_many,
                contract=contract,
                iteration=iteration,
                decision=decision,
                selected_basis_ids=set(too_many["basis_ids"]),
            )),
            "revision_assessment_basis_not_selected",
        )

    def test_legacy_challenger_terminal_paths_keep_the_previous_candidate(self) -> None:
        baseline = {
            "candidate_id": "baseline",
            "work_id": "work",
            "candidate_manifest_sha256": "1" * 64,
        }
        challenger = {
            "candidate_id": "challenger",
            "work_id": "work",
            "candidate_manifest_sha256": "2" * 64,
        }
        previous = {
            "outcome": "revised",
            "decision": {"disposition": "revise"},
            "anchor": {"candidate": baseline, "parent_candidate": None},
        }
        current = {
            "iteration_number": 2,
            "anchor": {"candidate": challenger},
            "decision": None,
        }
        state = {"iterations": [previous, current]}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current),
            baseline,
        )
        for disposition in ("stop", "preserve"):
            current["decision"] = {"disposition": disposition}
            self.assertEqual(
                workflow_module._terminal_candidate_for_iteration(state, current),
                baseline,
            )
        current["decision"] = {"disposition": "accept"}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current),
            challenger,
        )

    def test_legacy_challenger_resolves_parent_locator_in_same_history(self) -> None:
        baseline = {
            "candidate_id": "baseline",
            "work_id": "work",
            "candidate_manifest_sha256": "1" * 64,
        }
        locator = {
            "candidate_id": "baseline",
            "work_id": "work",
            "manifest_sha256": "1" * 64,
        }
        source = {"anchor": {"candidate": baseline}}
        previous = {
            "outcome": "revised",
            "decision": {"disposition": "revise"},
            "anchor": {"candidate": None, "parent_candidate": locator},
        }
        current = {
            "iteration_number": 3,
            "anchor": {"candidate": {"candidate_id": "challenger"}},
            "decision": None,
        }
        state = {"iterations": [source, previous, current]}
        self.assertEqual(
            workflow_module._terminal_candidate_for_iteration(state, current),
            baseline,
        )


if __name__ == "__main__":
    unittest.main()
