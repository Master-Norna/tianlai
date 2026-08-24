"""Contract tests for charter settlement and whole-work fork declarations.

Charter settlement closes the promise ledger: every accept must account for
the one-sentence promise, each identity invariant and the ending contract.
A fork declares that two or more complete candidates are variant worlds of
the same work; one possibility is always one whole piece, never a
replaceable fragment.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

import tianlai.creative_workflow as workflow_module
from tianlai.authoring_project import (
    create_authoring_project,
    save_authoring_project,
)
from tianlai.creative_workflow import (
    CreativeWorkflowError,
    activate_creative_workflow,
    create_creative_workflow,
    decide_workflow_iteration,
    record_workflow_authoring_revision,
    record_workflow_evidence,
    record_workflow_fork,
    record_workflow_review,
    terminate_creative_workflow,
)


ROOT = Path(__file__).resolve().parents[1]


def _charter() -> dict[str, object]:
    return {
        "title": "Settlement and fork trial",
        "one_sentence_promise": "Let one motive earn its return.",
        "target_listener_and_scene": "A focused listener in a quiet room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening contour"],
            "transformable_parts": ["register"],
        },
        "ending_contract": "End with consequence, not merely with silence.",
    }


def _score_metadata_revision_scope() -> dict[str, object]:
    return {
        "change_scale": "bounded",
        "documents": ["score"],
        "allowed_document_paths": {"score": ["/title"]},
        "score": {
            "part_ids": [],
            "event_ids": [],
            "bar_ranges": [],
            "allowed_note_fields": [],
            "allow_event_additions": False,
            "allow_event_deletions": False,
            "allow_reordering": False,
        },
        "whole_work_cost": None,
    }


def _constitution() -> dict[str, object]:
    # Deliberately retain a v0.1 binding here: core workflow history remains
    # usable without reinterpreting its clause IDs as the current official text.
    return {
        "document_id": "tianlai-music-constitution",
        "version": "0.1",
        "language": "zh-CN",
        "content_sha256": "0" * 64,
    }


def _active_clauses() -> list[dict[str, object]]:
    return [
        {
            "clause_id": "C0.03",
            "role": "review_lens",
            "rationale": "The ear keeps final authority in this trial.",
            "interpretation": "Metrics argue; listening decides.",
        }
    ]


def _error_code(call) -> str:
    with unittest.TestCase().assertRaises(CreativeWorkflowError) as captured:
        call()
    return captured.exception.code


def _settlement_item(
    target: str,
    *,
    status: str = "kept",
    basis_ids: list[str],
    event_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "target": target,
        "status": status,
        "rationale": "Bounded settlement rationale.",
        "basis_ids": basis_ids,
        "event_ids": [] if event_ids is None else event_ids,
    }


def _forge_revision_directory(layout, state: dict[str, object]) -> tuple[str, Path]:
    payload = workflow_module.json_document_bytes(
        state, limits=workflow_module._WORKFLOW_LIMITS
    )
    state_hash = workflow_module.canonical_json_sha256(state)
    revision = workflow_module._workflow_revision_identity(
        workflow_id=state["workflow_id"],
        project_id=state["project_id"],
        sequence=state["sequence"],
        parent_revision=state["parent_revision"],
        state_sha256=state_hash,
    )
    directory = workflow_module._revision_path(layout, revision)
    os.mkdir(directory)
    workflow_module._write_new_file(
        directory / workflow_module.WORKFLOW_STATE_NAME, payload
    )
    metadata = workflow_module._revision_manifest(
        workflow_id=state["workflow_id"],
        project_id=state["project_id"],
        revision=revision,
        sequence=state["sequence"],
        parent_revision=state["parent_revision"],
        created_at_utc=state["updated_at_utc"],
        state=state,
        payload=payload,
    )
    workflow_module._write_new_file(
        directory / workflow_module.WORKFLOW_REVISION_MANIFEST_NAME,
        workflow_module.json_document_bytes(
            metadata, limits=workflow_module._WORKFLOW_LIMITS
        ),
    )
    return revision, directory


def _repoint_current(layout, parent, state: dict[str, object]) -> str:
    state["parent_revision"] = parent.revision
    state["sequence"] = parent.state["sequence"] + 1
    revision, _directory = _forge_revision_directory(layout, state)
    workflow_module._replace_manifest(
        layout,
        workflow_module._manifest_document(
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            created_at_utc=state["created_at_utc"],
            updated_at_utc=state["updated_at_utc"],
            revision=revision,
            sequence=state["sequence"],
        ),
    )
    return revision


def _replace_equivalent_current(layout, state: dict[str, object]) -> str:
    """Publish a resealed sibling at the state's existing history position."""

    revision, _directory = _forge_revision_directory(layout, state)
    workflow_module._replace_manifest(
        layout,
        workflow_module._manifest_document(
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            created_at_utc=state["created_at_utc"],
            updated_at_utc=state["updated_at_utc"],
            revision=revision,
            sequence=state["sequence"],
        ),
    )
    return revision


def _reseal_fork_identity(
    state: dict[str, object], iteration: dict[str, object], fork: dict[str, object]
) -> None:
    body = {key: value for key, value in fork.items() if key != "fork_id"}
    fork["fork_id"] = "fork-" + workflow_module.canonical_json_sha256(
        {
            "workflow_id": state["workflow_id"],
            "iteration_number": iteration["iteration_number"],
            **body,
        }
    )[:20]


def _fake_candidate_anchor(
    authoring_revision: str,
    *,
    verified_at_utc: str,
    candidate_id: str = "candidate-lineage",
    manifest: str = "1",
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "work_id": "work-lineage",
        "authoring_revision": authoring_revision,
        "candidate_manifest_sha256": manifest * 64,
        "render_receipt_sha256": "2" * 64,
        "performance_plan_sha256": "3" * 64,
        "performance_plan_file_sha256": "4" * 64,
        "mix_sha256": "5" * 64,
        "post_render_check_sha256": "6" * 64,
        "mix_report_sha256": None,
        "workflow_managed": False,
        "workflow_authorization": None,
        "complete_review_artifacts": True,
        "verified_at_utc": verified_at_utc,
    }


class WorkflowSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "清偿 空间"
        state = create_authoring_project(self.root, title="Settlement Test")
        documents = state.detached_documents()
        score = documents["score"]
        score["schema_version"] = 1
        score["tempo_map"][0]["bpm"] = 120.0
        score["parts"][0]["notes"] = [
            {
                "event_id": "event-1",
                "bar": 1,
                "beat": 1.0,
                "duration_beats": 1.0,
                "pitch": 72,
                "dynamic": "mf",
            }
        ]
        self.authoring = save_authoring_project(
            self.root, expected_revision=state.revision, documents=documents
        )

    def activate(self):
        created = create_creative_workflow(
            self.root, mode="audit", final_authority="agent"
        )
        return activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
            constitution=_constitution(),
            active_clauses=_active_clauses(),
        )

    def review(self, snapshot):
        return record_workflow_review(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="Bounded review for settlement basis.",
        )

    def decide_stop(self, snapshot, *, settlement, review_ids=()):
        return decide_workflow_iteration(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            disposition="stop",
            summary="Stop the trial.",
            rationale="Settlement rules are under test.",
            final_authority="agent",
            perception_basis="report_only",
            review_ids=list(review_ids),
            evidence_dispositions=[],
            charter_settlement=settlement,
        )

    def forge_candidate(self, snapshot, *, candidate_id: str, manifest: str):
        layout = workflow_module._existing_layout(self.root, snapshot.workflow_id)
        state = copy.deepcopy(snapshot.detached_state())
        iteration = state["iterations"][-1]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
            candidate_id=candidate_id,
            manifest=manifest,
        )
        revision = _repoint_current(layout, snapshot, state)
        return workflow_module.open_creative_workflow(
            self.root, workflow_id=snapshot.workflow_id, revision=revision
        )

    def review_all_phases(self, snapshot):
        review_ids = []
        current = snapshot
        for phase in (
            "intent",
            "symbolic_structure",
            "orchestration_performance",
            "render_report",
        ):
            current = record_workflow_review(
                self.root,
                workflow_id=current.workflow_id,
                expected_revision=current.revision,
                phase=phase,
                reviewer="agent",
                perception_basis="report_only",
                summary=f"Bounded {phase} review.",
            )
            review_ids.append(
                current.state["iterations"][-1]["reviews"][-1]["review_id"]
            )
        return current, review_ids

    def accepted_transition_states(self):
        """Build a valid parent/accept pair without depending on renderer I/O."""

        active = self.activate()
        parent = active.detached_state()
        iteration = parent["iterations"][-1]
        operation_id = "c" * 32
        candidate_id = workflow_module.portable_slug(
            f"workflow-{operation_id}", maximum_length=96
        )
        reservation_revision = "e" * 64
        candidate = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=parent["updated_at_utc"],
            candidate_id=candidate_id,
        )
        candidate["workflow_managed"] = True
        candidate["workflow_authorization"] = {
            "workflow_id": parent["workflow_id"],
            "project_id": parent["project_id"],
            "reservation_revision": reservation_revision,
            "iteration_number": iteration["iteration_number"],
            "operation_id": operation_id,
            "authoring_revision": iteration["anchor"]["authoring_revision"],
            "candidate_work_id": candidate["work_id"],
            "candidate_id": candidate_id,
            "parent_work_id": None,
            "parent_candidate_id": None,
            "parent_manifest_sha256": None,
        }
        iteration["render_attempts"].append(
            {
                "attempt_number": 1,
                "operation_id": operation_id,
                "expected_work_id": candidate["work_id"],
                "expected_candidate_id": candidate_id,
                "reservation_revision": reservation_revision,
                "authoring_revision": iteration["anchor"][
                    "authoring_revision"
                ],
                "parent_candidate": None,
                "status": "completed",
                "requested_at_utc": parent["updated_at_utc"],
                "finished_at_utc": parent["updated_at_utc"],
            }
        )
        iteration["anchor"]["candidate"] = candidate
        review_ids = []
        for phase in (
            "intent",
            "symbolic_structure",
            "orchestration_performance",
            "render_report",
        ):
            body = {
                "phase": phase,
                "reviewer": "agent",
                "perception_basis": "report_only",
                "summary": f"Bounded {phase} review.",
                "candidate_id": candidate["candidate_id"],
                "reviewed_at_utc": parent["updated_at_utc"],
            }
            review_id = workflow_module._review_identity(
                workflow_id=parent["workflow_id"],
                iteration_number=iteration["iteration_number"],
                body=body,
            )
            iteration["reviews"].append({"review_id": review_id, **body})
            review_ids.append(review_id)
        workflow_module._refresh_usage(parent)
        workflow_module._validate_state_document(parent)

        accepted = copy.deepcopy(parent)
        accepted_iteration = accepted["iterations"][-1]
        timestamp = accepted["updated_at_utc"]
        settlement = [
            _settlement_item(
                target,
                basis_ids=[review_ids[0]],
                event_ids=["event-1"],
            )
            for target in (
                "one_sentence_promise",
                "identity_kernel.invariants[0]",
                "ending_contract",
            )
        ]
        decision = workflow_module._decision_record(
            disposition="accept",
            summary="Accept the fully settled test state.",
            rationale="Every charter target has an explicit disposition.",
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=(),
            exception_ids=(),
            expected_audible_change=None,
            final_authority="agent",
            perception_basis="report_only",
            timestamp=timestamp,
            derivation_ids=(),
            review_ids=review_ids,
            evidence_dispositions=(),
            charter_settlement=settlement,
        )
        accepted_iteration["decision"] = decision
        accepted_iteration["status"] = "closed"
        accepted_iteration["closed_at_utc"] = timestamp
        accepted_iteration["outcome"] = "accepted"
        accepted["status"] = "completed"
        accepted["termination"] = {
            "reason": "accepted_under_charter",
            "summary": decision["summary"],
            "final_authority": "agent",
            "perception_basis": "report_only",
            "selected_candidate": copy.deepcopy(candidate),
            "open_evidence_ids": [],
            "acceptance_gate": {
                "kind": workflow_module._ACCEPTANCE_GATE_KIND,
                "schema_version": workflow_module.WORKFLOW_VERSION,
                "profile": workflow_module._ACCEPTANCE_GATE_PROFILE,
                "authoring_revision": accepted_iteration["anchor"][
                    "authoring_revision"
                ],
                "candidate_manifest_sha256": candidate[
                    "candidate_manifest_sha256"
                ],
                "checked_hard_failure_evidence_ids": [],
                "unresolved_hard_failure_evidence_ids": [],
                "readiness_result_sha256": None,
                "recorded_at_utc": timestamp,
                "claim_scope": workflow_module._ACCEPTANCE_GATE_CLAIM_SCOPE,
            },
            "terminated_at_utc": timestamp,
        }
        accepted["parent_revision"] = "f" * 64
        accepted["sequence"] = parent["sequence"] + 1
        workflow_module._refresh_usage(accepted)
        return parent, accepted

    def test_accept_requires_charter_settlement(self) -> None:
        with_candidate = self.forge_candidate(
            self.activate(), candidate_id="candidate-settle", manifest="3"
        )
        reviewed, review_ids = self.review_all_phases(with_candidate)
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=reviewed.workflow_id,
                    expected_revision=reviewed.revision,
                    disposition="accept",
                    summary="Accept without settling the charter.",
                    rationale="Acceptance must account for every promise.",
                    final_authority="agent",
                    perception_basis="report_only",
                    review_ids=review_ids,
                    evidence_dispositions=[],
                )
            ),
            # New decisions always carry the settlement list; an accept with
            # zero items is therefore incomplete rather than missing.
            "acceptance_charter_settlement_incomplete",
        )

    def test_accept_requires_complete_settlement(self) -> None:
        with_candidate = self.forge_candidate(
            self.activate(), candidate_id="candidate-settle", manifest="3"
        )
        reviewed, review_ids = self.review_all_phases(with_candidate)
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=reviewed.workflow_id,
                    expected_revision=reviewed.revision,
                    disposition="accept",
                    summary="Accept with a partial ledger.",
                    rationale="Only one promise is settled.",
                    final_authority="agent",
                    perception_basis="report_only",
                    review_ids=review_ids,
                    evidence_dispositions=[],
                    charter_settlement=[
                        _settlement_item(
                            "one_sentence_promise", basis_ids=[review_ids[0]]
                        )
                    ],
                )
            ),
            "acceptance_charter_settlement_incomplete",
        )

    def test_stop_may_settle_partially(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        stopped = self.decide_stop(
            reviewed,
            settlement=[
                _settlement_item(
                    "one_sentence_promise",
                    basis_ids=[review_id],
                    event_ids=["event-1"],
                )
            ],
            review_ids=[review_id],
        )
        decision = stopped.state["iterations"][-1]["decision"]
        self.assertEqual(len(decision["charter_settlement"]), 1)

    def test_transformed_requires_derivation_basis(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[
                        _settlement_item(
                            "identity_kernel.invariants[0]",
                            status="transformed",
                            basis_ids=[review_id],
                        )
                    ],
                    review_ids=[review_id],
                )
            ),
            "charter_settlement_transformation_requires_derivation",
        )

    def test_refused_requires_exception_or_derivation_basis(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[
                        _settlement_item(
                            "ending_contract",
                            status="refused",
                            basis_ids=[review_id],
                        )
                    ],
                    review_ids=[review_id],
                )
            ),
            "charter_settlement_refusal_requires_declaration",
        )

    def test_settlement_basis_must_be_selected(self) -> None:
        reviewed = self.review(self.activate())
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[
                        _settlement_item(
                            "one_sentence_promise",
                            basis_ids=["review-" + "0" * 20],
                        )
                    ],
                )
            ),
            "charter_settlement_basis_not_selected",
        )

    def test_settlement_rejects_unknown_target(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[_settlement_item("title", basis_ids=[review_id])],
                    review_ids=[review_id],
                )
            ),
            "invalid_charter_settlement_target",
        )

    def test_settlement_rejects_duplicate_target(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[
                        _settlement_item(
                            "one_sentence_promise", basis_ids=[review_id]
                        ),
                        _settlement_item(
                            "one_sentence_promise", basis_ids=[review_id]
                        ),
                    ],
                    review_ids=[review_id],
                )
            ),
            "duplicate_charter_settlement_target",
        )

    def test_settlement_event_referents_are_verified(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: self.decide_stop(
                    reviewed,
                    settlement=[
                        _settlement_item(
                            "one_sentence_promise",
                            basis_ids=[review_id],
                            event_ids=["event-999"],
                        )
                    ],
                    review_ids=[review_id],
                )
            ),
            "charter_settlement_event_not_found",
        )

    def test_reopen_revalidates_settlement_event_referents(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][-1]["reviews"][0][
            "review_id"
        ]
        stopped = self.decide_stop(
            reviewed,
            settlement=[
                _settlement_item(
                    "one_sentence_promise",
                    basis_ids=[review_id],
                    event_ids=["event-1"],
                )
            ],
            review_ids=[review_id],
        )
        state = stopped.detached_state()
        state["iterations"][-1]["decision"]["charter_settlement"][0][
            "event_ids"
        ] = ["event-never-existed"]
        layout = workflow_module._existing_layout(
            self.root, stopped.workflow_id
        )
        _replace_equivalent_current(layout, state)

        self.assertEqual(
            _error_code(
                lambda: workflow_module.open_creative_workflow(
                    self.root, workflow_id=stopped.workflow_id
                )
            ),
            "charter_settlement_event_not_found",
        )

    def test_new_accept_transition_cannot_omit_charter_settlement(self) -> None:
        parent, accepted = self.accepted_transition_states()
        accepted["iterations"][-1]["decision"].pop("charter_settlement")

        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_state_transition(
                    parent,
                    accepted,
                    parent_revision="f" * 64,
                )
            ),
            "workflow_settlement_contract_downgrade",
        )

    def test_old_acceptance_gate_terminal_without_settlement_remains_readable(
        self,
    ) -> None:
        _parent, accepted = self.accepted_transition_states()
        accepted["iterations"][-1]["decision"].pop("charter_settlement")
        accepted["policy"] = copy.deepcopy(
            workflow_module._ACCEPTANCE_GATE_POLICY
        )

        workflow_module._validate_state_document(accepted)

    def test_budget_exhausted_reason_requires_an_actually_spent_limit(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: terminate_creative_workflow(
                    self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                    reason="budget_exhausted",
                    summary="A label cannot manufacture budget exhaustion.",
                    final_authority="agent",
                )
            ),
            "workflow_budget_not_exhausted",
        )

    def test_budget_exhausted_reason_accepts_a_spent_positive_limit(self) -> None:
        created = create_creative_workflow(
            self.root,
            mode="audit",
            final_authority="agent",
            budget={"max_reviews_per_iteration": 1},
        )
        active = activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
            constitution=_constitution(),
            active_clauses=_active_clauses(),
        )
        reviewed = self.review(active)
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            reason="budget_exhausted",
            summary="The one-review budget is now genuinely spent.",
            final_authority="agent",
        )
        self.assertEqual(stopped.state["status"], "stopped")
        self.assertEqual(
            stopped.state["termination"]["reason"], "budget_exhausted"
        )

    def test_latest_history_rechecks_the_budget_exhausted_reason(self) -> None:
        active = self.activate()
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            reason="cancelled",
            summary="Create an otherwise valid direct termination.",
            final_authority="agent",
        )
        rewritten = stopped.detached_state()
        rewritten["termination"]["reason"] = "budget_exhausted"
        workflow_module._validate_state_document(rewritten)
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_state_transition(
                    active.detached_state(),
                    rewritten,
                    parent_revision=active.revision,
                )
            ),
            "workflow_budget_not_exhausted",
        )


class WorkflowForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "分支 空间"
        state = create_authoring_project(self.root, title="Fork Test")
        documents = state.detached_documents()
        score = documents["score"]
        score["schema_version"] = 1
        score["tempo_map"][0]["bpm"] = 120.0
        score["parts"][0]["notes"] = [
            {
                "event_id": "event-1",
                "bar": 1,
                "beat": 1.0,
                "duration_beats": 1.0,
                "pitch": 72,
                "dynamic": "mf",
            },
            {
                "event_id": "event-2",
                "bar": 2,
                "beat": 1.0,
                "duration_beats": 1.0,
                "pitch": 74,
                "dynamic": "mf",
            },
        ]
        self.authoring = save_authoring_project(
            self.root, expected_revision=state.revision, documents=documents
        )

    def activate(self):
        created = create_creative_workflow(
            self.root, mode="iterate", final_authority="agent"
        )
        return activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
            constitution=_constitution(),
            active_clauses=_active_clauses(),
        )

    @staticmethod
    def managed_identity(candidate_alias: str) -> tuple[str, str]:
        operation_id = workflow_module.canonical_json_sha256(
            {"candidate_alias": candidate_alias}
        )[:32]
        candidate_id = workflow_module.portable_slug(
            f"workflow-{operation_id}", maximum_length=96
        )
        return operation_id, candidate_id

    def forge_candidate(self, snapshot, *, candidate_id: str, manifest: str):
        """Append the legal reservation and managed-completion transitions."""

        layout = workflow_module._existing_layout(self.root, snapshot.workflow_id)
        operation_id, managed_candidate_id = self.managed_identity(candidate_id)

        pending_state = copy.deepcopy(snapshot.detached_state())
        pending_iteration = pending_state["iterations"][-1]
        pending_attempt = {
            "attempt_number": len(pending_iteration["render_attempts"]) + 1,
            "operation_id": operation_id,
            "expected_work_id": "work-lineage",
            "expected_candidate_id": managed_candidate_id,
            "reservation_revision": None,
            "authoring_revision": pending_iteration["anchor"][
                "authoring_revision"
            ],
            "parent_candidate": copy.deepcopy(
                pending_iteration["anchor"]["parent_candidate"]
            ),
            "status": "pending",
            "requested_at_utc": pending_state["updated_at_utc"],
            "finished_at_utc": None,
        }
        pending_iteration["render_attempts"].append(pending_attempt)
        pending_iteration["status"] = "candidate_pending"
        pending_state["status"] = "candidate_pending"
        workflow_module._refresh_usage(pending_state)
        pending_revision = _repoint_current(layout, snapshot, pending_state)
        pending = workflow_module.open_creative_workflow(
            self.root,
            workflow_id=snapshot.workflow_id,
            revision=pending_revision,
        )

        completed_state = copy.deepcopy(pending.detached_state())
        completed_iteration = completed_state["iterations"][-1]
        completed_attempt = completed_iteration["render_attempts"][-1]
        completed_attempt["reservation_revision"] = pending.revision
        completed_attempt["status"] = "completed"
        completed_attempt["finished_at_utc"] = completed_state[
            "updated_at_utc"
        ]
        candidate = _fake_candidate_anchor(
            completed_iteration["anchor"]["authoring_revision"],
            verified_at_utc=completed_state["updated_at_utc"],
            candidate_id=managed_candidate_id,
            manifest=manifest,
        )
        parent = completed_iteration["anchor"]["parent_candidate"]
        candidate["workflow_managed"] = True
        candidate["workflow_authorization"] = {
            "workflow_id": completed_state["workflow_id"],
            "project_id": completed_state["project_id"],
            "reservation_revision": pending.revision,
            "iteration_number": completed_iteration["iteration_number"],
            "operation_id": operation_id,
            "authoring_revision": completed_iteration["anchor"][
                "authoring_revision"
            ],
            "candidate_work_id": candidate["work_id"],
            "candidate_id": managed_candidate_id,
            "parent_work_id": None if parent is None else parent["work_id"],
            "parent_candidate_id": (
                None if parent is None else parent["candidate_id"]
            ),
            "parent_manifest_sha256": (
                None if parent is None else parent["manifest_sha256"]
            ),
        }
        completed_iteration["anchor"]["candidate"] = candidate
        completed_iteration["status"] = "reviewing"
        completed_state["status"] = "reviewing"
        workflow_module._refresh_usage(completed_state)
        revision = _repoint_current(layout, pending, completed_state)
        return workflow_module.open_creative_workflow(
            self.root, workflow_id=snapshot.workflow_id, revision=revision
        )

    def locator(self, *, candidate_id: str, manifest: str) -> dict[str, str]:
        _operation_id, managed_candidate_id = self.managed_identity(candidate_id)
        return {
            "work_id": "work-lineage",
            "candidate_id": managed_candidate_id,
            "manifest_sha256": manifest * 64,
        }

    def two_candidate_snapshot(self):
        """Reach a reviewing iteration with two recorded whole candidates."""

        active = self.activate()
        candidate_a = self.forge_candidate(
            active, candidate_id="candidate-a", manifest="1"
        )
        reviewed = record_workflow_review(
            self.root,
            workflow_id=candidate_a.workflow_id,
            expected_revision=candidate_a.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="Review before the revision fork.",
        )
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            category="aesthetic_risk",
            code="structure.register_trial",
            basis_kind="diagnostic_hypothesis",
            basis_reference="The register may carry a different stance.",
            reporter="agent",
            perception_basis="report_only",
            summary="A bounded risk worth a variant world.",
            observation="One candidate explores a raised register.",
            interpretation="Both worlds may remain the same piece.",
            confidence="medium",
        )
        evidence_id = evidence.state["iterations"][-1]["evidence"][0][
            "evidence_id"
        ]
        review_id = evidence.state["iterations"][-1]["reviews"][0]["review_id"]
        revised = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="revise",
            summary="Open a variant world.",
            rationale="The fork needs a second complete candidate.",
            final_authority="agent",
            perception_basis="report_only",
            review_ids=[review_id],
            evidence_ids=[evidence_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "The variant revision answers this risk.",
                    "basis_ids": [],
                }
            ],
            expected_audible_change="A second complete variant becomes recordable.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        documents = self.authoring.detached_documents()
        documents["score"]["title"] = "Fork variant revision"
        advanced_authoring = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=documents,
        )
        second_iteration = record_workflow_authoring_revision(
            self.root,
            workflow_id=revised.workflow_id,
            expected_revision=revised.revision,
            authoring_revision=advanced_authoring.revision,
        )
        return self.forge_candidate(
            second_iteration, candidate_id="candidate-b", manifest="7"
        )

    def record_two_candidate_fork(self):
        snapshot = self.two_candidate_snapshot()
        return record_workflow_fork(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            branches=[
                {
                    "candidate": self.locator(
                        candidate_id="candidate-a", manifest="1"
                    ),
                    "stance": "The motive stays in its original register.",
                    "derivation_ids": [],
                },
                {
                    "candidate": self.locator(
                        candidate_id="candidate-b", manifest="7"
                    ),
                    "stance": "The return rises an octave.",
                    "derivation_ids": [],
                },
            ],
            invariant_indexes=[0],
            event_ids=["event-2"],
            note="Both worlds keep the opening contour.",
        )

    def test_fork_declares_complete_variant_worlds(self) -> None:
        recorded = self.record_two_candidate_fork()
        forks = recorded.state["iterations"][-1]["forks"]
        self.assertEqual(len(forks), 1)
        fork = forks[0]
        self.assertRegex(fork["fork_id"], r"^fork-[0-9a-f]{20}$")
        self.assertEqual(
            fork["anchor"]["authoring_revision"],
            recorded.state["iterations"][-1]["anchor"]["authoring_revision"],
        )
        self.assertRegex(fork["anchor"]["score_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(fork["invariant_indexes"], [0])
        self.assertEqual(
            [branch["candidate"]["candidate_id"] for branch in fork["branches"]],
            [
                self.managed_identity("candidate-a")[1],
                self.managed_identity("candidate-b")[1],
            ],
        )

    def test_reopen_recomputes_fork_identity(self) -> None:
        recorded = self.record_two_candidate_fork()
        state = recorded.detached_state()
        state["iterations"][-1]["forks"][0]["note"] = (
            "A rewritten declaration under the old identity."
        )
        layout = workflow_module._existing_layout(
            self.root, recorded.workflow_id
        )
        _replace_equivalent_current(layout, state)

        self.assertEqual(
            _error_code(
                lambda: workflow_module.open_creative_workflow(
                    self.root, workflow_id=recorded.workflow_id
                )
            ),
            "fork_identity_mismatch",
        )

    def test_reopen_revalidates_fork_score_hash_and_event(self) -> None:
        recorded = self.record_two_candidate_fork()
        layout = workflow_module._existing_layout(
            self.root, recorded.workflow_id
        )
        mutations = (
            ("score_sha256", "f" * 64, "fork_score_hash_mismatch"),
            ("event_ids", ["event-never-existed"], "fork_event_not_found"),
        )
        for field, replacement, expected_code in mutations:
            with self.subTest(field=field):
                state = recorded.detached_state()
                iteration = state["iterations"][-1]
                fork = iteration["forks"][0]
                fork["anchor"][field] = replacement
                _reseal_fork_identity(state, iteration, fork)
                _replace_equivalent_current(layout, state)
                self.assertEqual(
                    _error_code(
                        lambda: workflow_module.open_creative_workflow(
                            self.root, workflow_id=recorded.workflow_id
                        )
                    ),
                    expected_code,
                )

    def test_reopen_revalidates_fork_branch_candidate(self) -> None:
        recorded = self.record_two_candidate_fork()
        state = recorded.detached_state()
        iteration = state["iterations"][-1]
        fork = iteration["forks"][0]
        fork["branches"][1]["candidate"] = self.locator(
            candidate_id="candidate-never-recorded", manifest="9"
        )
        _reseal_fork_identity(state, iteration, fork)
        layout = workflow_module._existing_layout(
            self.root, recorded.workflow_id
        )
        _replace_equivalent_current(layout, state)

        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=recorded.workflow_id
                )
            ),
            "fork_branch_candidate_not_recorded",
        )

    def test_iterate_history_rejects_unmanaged_candidate_injection(self) -> None:
        active = self.activate()
        state = active.detached_state()
        iteration = state["iterations"][-1]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
            candidate_id="candidate-injected",
            manifest="8",
        )
        layout = workflow_module._existing_layout(
            self.root, active.workflow_id
        )
        _repoint_current(layout, active, state)

        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=active.workflow_id
                )
            ),
            "workflow_history_candidate_injected",
        )

    def test_fork_branch_must_be_a_recorded_candidate(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "Recorded world.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-x", manifest="9"
                            ),
                            "stance": "A world this workflow never rendered.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    event_ids=["event-2"],
                )
            ),
            "fork_branch_candidate_not_recorded",
        )

    def test_fork_requires_symbolic_anchor(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "One stance.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-b", manifest="7"
                            ),
                            "stance": "Another stance.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    part_ids=["part-1"],
                )
            ),
            "fork_anchor_empty",
        )

    def test_fork_rejects_unknown_event_anchor(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "One stance.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-b", manifest="7"
                            ),
                            "stance": "Another stance.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    event_ids=["event-999"],
                )
            ),
            "fork_event_not_found",
        )

    def test_fork_event_anchor_must_fall_inside_its_declared_range(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "Earlier complete world.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-b", manifest="7"
                            ),
                            "stance": "Current complete world.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    event_ids=["event-2"],
                    start_bar=1,
                    start_beat=1.0,
                    end_bar=2,
                    end_beat=1.0,
                )
            ),
            "fork_event_range_mismatch",
        )

    def test_fork_worlds_must_include_the_current_candidate(self) -> None:
        fork = {
            "branches": [
                {"candidate": self.locator(candidate_id="candidate-a", manifest="1")},
                {"candidate": self.locator(candidate_id="candidate-b", manifest="7")},
            ]
        }
        recorded = {
            (
                branch["candidate"]["work_id"],
                branch["candidate"]["candidate_id"],
                branch["candidate"]["manifest_sha256"],
            )
            for branch in fork["branches"]
        }
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_fork_candidate_referents(
                    fork,
                    recorded_candidates=recorded,
                    current_candidate=(
                        "work-lineage",
                        "candidate-third-world",
                        "9" * 64,
                    ),
                )
            ),
            "fork_current_candidate_required",
        )

    def test_fork_requires_at_least_two_branches(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "A single world is not a fork.",
                            "derivation_ids": [],
                        }
                    ],
                    invariant_indexes=[0],
                    event_ids=["event-2"],
                )
            ),
            "fork_branches_required",
        )

    def test_fork_rejects_duplicate_branch_candidate(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "One stance.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "The same world twice.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    event_ids=["event-2"],
                )
            ),
            "duplicate_fork_branch_candidate",
        )

    def test_fork_invariant_index_must_fit_the_charter(self) -> None:
        snapshot = self.two_candidate_snapshot()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "One stance.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-b", manifest="7"
                            ),
                            "stance": "Another stance.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[5],
                    event_ids=["event-2"],
                )
            ),
            "fork_invariant_index_out_of_range",
        )

    def test_fork_bar_range_must_be_complete(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: record_workflow_fork(
                    self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                    branches=[
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-a", manifest="1"
                            ),
                            "stance": "One stance.",
                            "derivation_ids": [],
                        },
                        {
                            "candidate": self.locator(
                                candidate_id="candidate-b", manifest="7"
                            ),
                            "stance": "Another stance.",
                            "derivation_ids": [],
                        },
                    ],
                    invariant_indexes=[0],
                    start_bar=1,
                )
            ),
            "invalid_fork_bar_range",
        )


if __name__ == "__main__":
    unittest.main()
