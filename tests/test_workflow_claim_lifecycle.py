"""Red/green contracts for workflow claim identity and lifecycle closure.

The workflow revision hash protects the complete serialized state.  These
tests deliberately exercise the independent semantic boundary underneath it:
record content identities, score-scope referents, measurement scope, exception
targeting, and the disposition of every evidence claim at a decision.

Only this test module forges state.  Production APIs remain the source of all
ordinary records; the reseal helper models a hash-consistent migration or
recovery error so that semantic verification cannot accidentally rely on the
outer revision digest alone.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
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
    _validate_state_document,
    activate_creative_workflow,
    create_creative_workflow,
    decide_workflow_iteration,
    record_workflow_evidence,
    record_workflow_review,
    register_workflow_exception,
    terminate_creative_workflow,
)


def _charter() -> dict[str, object]:
    return {
        "title": "Claim lifecycle trial",
        "one_sentence_promise": "Let one motive earn one irreversible return.",
        "target_listener_and_scene": "A focused listener in a quiet room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening contour"],
            "transformable_parts": ["register", "orchestration"],
        },
        "ending_contract": "End with consequence, not merely with silence.",
    }


def _score_metadata_revision_scope() -> dict[str, object]:
    return {
        "change_scale": "bounded",
        "documents": ["score"],
        "allowed_document_paths": {"score": ["/tail_seconds"]},
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


def _error_code(call) -> str:
    with unittest.TestCase().assertRaises(CreativeWorkflowError) as captured:
        call()
    return captured.exception.code


def _content_id(
    prefix: str,
    *,
    state: dict[str, object],
    iteration: dict[str, object],
    body: dict[str, object],
) -> str:
    return prefix + workflow_module.canonical_json_sha256(
        {
            "workflow_id": state["workflow_id"],
            "iteration_number": iteration["iteration_number"],
            **body,
        }
    )[:20]


def _fake_candidate_anchor(
    authoring_revision: str, *, verified_at_utc: str
) -> dict[str, object]:
    return {
        "candidate_id": "candidate-claim-contract",
        "work_id": "work-claim-contract",
        "authoring_revision": authoring_revision,
        "candidate_manifest_sha256": "1" * 64,
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


def _fake_managed_candidate_anchor(
    state: dict[str, object],
    *,
    reservation_revision: str,
    operation_id: str = "8" * 32,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return one internally consistent managed anchor and completed attempt."""

    iteration = state["iterations"][-1]
    candidate = _fake_candidate_anchor(
        iteration["anchor"]["authoring_revision"],
        verified_at_utc=state["updated_at_utc"],
    )
    candidate_id = workflow_module.portable_slug(
        f"workflow-{operation_id}", maximum_length=96
    )
    candidate["candidate_id"] = candidate_id
    candidate["workflow_managed"] = True
    candidate["workflow_authorization"] = {
        "workflow_id": state["workflow_id"],
        "project_id": state["project_id"],
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
    attempt = {
        "attempt_number": 1,
        "operation_id": operation_id,
        "expected_work_id": candidate["work_id"],
        "expected_candidate_id": candidate_id,
        "reservation_revision": reservation_revision,
        "authoring_revision": iteration["anchor"]["authoring_revision"],
        "parent_candidate": None,
        "status": "completed",
        "requested_at_utc": state["updated_at_utc"],
        "finished_at_utc": state["updated_at_utc"],
    }
    return candidate, attempt


def _append_review(
    state: dict[str, object], *, phase: str, reviewer: str = "agent"
) -> str:
    iteration = state["iterations"][-1]
    candidate = iteration["anchor"]["candidate"]
    perception_basis = (
        "audio_audition" if phase == "audio_audition" else "report_only"
    )
    body = {
        "phase": phase,
        "reviewer": reviewer,
        "perception_basis": perception_basis,
        "summary": f"Review the {phase} phase under its bounded claim.",
        "candidate_id": (
            candidate["candidate_id"] if isinstance(candidate, dict) else None
        ),
        "reviewed_at_utc": state["updated_at_utc"],
    }
    review_id = _content_id(
        "review-", state=state, iteration=iteration, body=body
    )
    iteration["reviews"].append({"review_id": review_id, **body})
    return review_id


def _decision(
    *,
    iteration: dict[str, object],
    disposition: str,
    evidence_ids: list[str],
    evidence_dispositions: list[dict[str, object]],
    review_ids: list[str],
    expected_audible_change: str | None = None,
) -> dict[str, object]:
    return {
        "disposition": disposition,
        "summary": f"Exercise the {disposition} claim contract.",
        "rationale": "Every durable claim must be explicitly accounted for.",
        "protected_values": [],
        "sacrificed_values": [],
        "evidence_ids": evidence_ids,
        "exception_ids": [],
        "derivation_ids": [],
        "charter_settlement": [],
        "evidence_dispositions": evidence_dispositions,
        "review_ids": review_ids,
        "expected_audible_change": expected_audible_change,
        "final_authority": "agent",
        "perception_basis": "report_only",
        "claim_scope": "contextual_workflow_decision_not_objective_quality",
        "decided_at_utc": iteration["opened_at_utc"],
    }


def _forge_revision_directory(
    layout, state: dict[str, object]
) -> tuple[str, Path]:
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


def _append_forged_child_and_repoint(layout, parent, state: dict[str, object]) -> str:
    """Append one hash-consistent child without using a production transition.

    This models a migration/recovery implementation that preserves the real
    parent chain but emits an illegal full-state delta.  Unlike rewriting the
    complete unsigned history, a verifier can and should reject this by
    comparing the child snapshot with its named parent.
    """

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


def _close_forged_accept(
    state: dict[str, object],
    *,
    candidate: dict[str, object],
) -> None:
    """Project one structurally complete accept without using the public API."""

    iteration = state["iterations"][-1]
    iteration["anchor"]["candidate"] = candidate
    review_ids = [
        _append_review(state, phase=phase)
        for phase in (
            "intent",
            "symbolic_structure",
            "orchestration_performance",
            "render_report",
        )
    ]
    decision = _decision(
        iteration=iteration,
        disposition="accept",
        evidence_ids=[],
        evidence_dispositions=[],
        review_ids=review_ids,
    )
    # The forged accept is shared by historical acceptance-gate tests and by
    # latest-policy tests.  The latter opt in with the complete ledger helper.
    decision.pop("charter_settlement")
    timestamp = state["updated_at_utc"]
    decision["decided_at_utc"] = timestamp
    iteration["decision"] = decision
    iteration["status"] = "closed"
    iteration["closed_at_utc"] = timestamp
    iteration["outcome"] = "accepted"
    state["status"] = "completed"
    state["termination"] = {
        "reason": "accepted_under_charter",
        "summary": decision["summary"],
        "final_authority": decision["final_authority"],
        "perception_basis": decision["perception_basis"],
        "selected_candidate": dict(candidate),
        "open_evidence_ids": [],
        "terminated_at_utc": timestamp,
    }
    workflow_module._refresh_usage(state)


def _attach_acceptance_gate(state: dict[str, object]) -> dict[str, object]:
    """Attach the acceptance-gate-tier proof to a forged accepted state."""

    iteration = state["iterations"][-1]
    termination = state["termination"]
    candidate = termination["selected_candidate"]
    hard_failure_ids = [
        item["evidence_id"]
        for item in iteration["evidence"]
        if item["category"] == "hard_failure"
    ]
    gate = {
        "kind": workflow_module._ACCEPTANCE_GATE_KIND,
        "schema_version": workflow_module.WORKFLOW_VERSION,
        "profile": workflow_module._ACCEPTANCE_GATE_PROFILE,
        "authoring_revision": iteration["anchor"]["authoring_revision"],
        "candidate_manifest_sha256": candidate["candidate_manifest_sha256"],
        "checked_hard_failure_evidence_ids": hard_failure_ids,
        "unresolved_hard_failure_evidence_ids": [],
        "readiness_result_sha256": "a" * 64 if hard_failure_ids else None,
        "recorded_at_utc": termination["terminated_at_utc"],
        "claim_scope": workflow_module._ACCEPTANCE_GATE_CLAIM_SCOPE,
    }
    termination["acceptance_gate"] = gate
    return gate


def _attach_complete_charter_settlement(
    state: dict[str, object],
) -> list[dict[str, object]]:
    """Attach a complete latest-policy promise ledger to a forged accept."""

    iteration = state["iterations"][-1]
    decision = iteration["decision"]
    basis_id = decision["review_ids"][0]
    settlement = [
        {
            "target": target,
            "status": "kept",
            "rationale": "The accepted candidate keeps this charter promise.",
            "basis_ids": [basis_id],
            "event_ids": [],
        }
        for target in workflow_module._charter_settlement_targets(
            state["work_charter"]
        )
    ]
    decision["charter_settlement"] = settlement
    return settlement


class WorkflowClaimLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "claim-workspace"
        initial = create_authoring_project(self.root, title="Claim Contract")
        documents = initial.detached_documents()
        score = documents["score"]
        score["tempo_map"][0]["bpm"] = 120.0
        self.part_1_id = score["parts"][0]["id"]
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
        score["parts"].append(
            {
                "id": "part-2",
                "name": "Part 2",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "event_id": "event-2",
                        "bar": 1,
                        "beat": 2.0,
                        "duration_beats": 1.0,
                        "pitch": 67,
                        "dynamic": "mp",
                    }
                ],
            }
        )
        documents["authoring_roster"]["assignments"].append(
            {"part": "part-2", "instrument": None}
        )
        self.authoring = save_authoring_project(
            self.root,
            expected_revision=initial.revision,
            documents=documents,
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
        )

    def review(self, snapshot, phase: str = "intent"):
        return record_workflow_review(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            phase=phase,
            reviewer="agent",
            perception_basis=(
                "audio_audition" if phase == "audio_audition" else "report_only"
            ),
            summary=f"Review {phase} without turning taste into a score.",
        )

    def evidence(
        self,
        snapshot,
        *,
        category: str = "promise_conflict",
        code: str = "promise.return_too_early",
        scope: dict[str, object] | None = None,
    ):
        return record_workflow_evidence(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            category=category,
            code=code,
            basis_kind=(
                "declared_promise"
                if category == "promise_conflict"
                else "diagnostic_hypothesis"
            ),
            basis_reference=(
                "one_sentence_promise"
                if category == "promise_conflict"
                else "bounded structural diagnosis"
            ),
            reporter="agent",
            perception_basis="report_only",
            summary="The return may arrive before it is earned.",
            observation="The current plan spends its strongest return immediately.",
            interpretation="The promised consequence may therefore be weakened.",
            confidence="medium",
            scope=scope,
        )

    def valid_managed_accept_state(self) -> dict[str, object]:
        """Return a historical acceptance-gate-policy accepted state."""

        state = self.activate().detached_state()
        iteration = state["iterations"][-1]
        candidate, attempt = _fake_managed_candidate_anchor(
            state,
            reservation_revision="7" * 64,
        )
        iteration["render_attempts"].append(attempt)
        _close_forged_accept(state, candidate=candidate)
        _attach_acceptance_gate(state)
        state["policy"] = copy.deepcopy(
            workflow_module._ACCEPTANCE_GATE_POLICY
        )
        return state

    def valid_latest_accept_state(self) -> dict[str, object]:
        """Return a complete accepted state under the settlement policy."""

        state = self.valid_managed_accept_state()
        state["policy"] = copy.deepcopy(workflow_module._POLICY)
        _attach_complete_charter_settlement(state)
        return state

    def test_review_identity_rejects_body_drift(self) -> None:
        reviewed = self.review(self.activate())
        state = reviewed.detached_state()
        state["iterations"][0]["reviews"][0]["summary"] = "Drifted review body."
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "review_identity_mismatch",
        )

    def test_evidence_identity_rejects_body_drift(self) -> None:
        recorded = self.evidence(self.activate())
        state = recorded.detached_state()
        state["iterations"][0]["evidence"][0][
            "observation"
        ] = "Drifted evidence body."
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "evidence_identity_mismatch",
        )

    def test_exception_identity_rejects_body_drift(self) -> None:
        evidence = self.evidence(self.activate())
        evidence_id = evidence.state["iterations"][0]["evidence"][0][
            "evidence_id"
        ]
        registered = register_workflow_exception(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            target_type="work_charter",
            target_ref="one_sentence_promise",
            purpose="Preserve a deliberate early rupture.",
            scope="This iteration and this promise conflict only.",
            higher_value="Narrative shock.",
            cost="The original arc becomes less legible.",
            recovery="Preserve the unmodified candidate.",
            evidence_ids=[evidence_id],
        )
        state = registered.detached_state()
        state["iterations"][0]["exceptions"][0][
            "recovery"
        ] = "Drifted exception body."
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "exception_identity_mismatch",
        )

    def test_evidence_scope_rejects_unknown_event(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.evidence(
                    active, scope={"event_ids": ["missing-event"]}
                )
            ),
            "evidence_event_not_found",
        )

    def test_evidence_scope_rejects_unknown_part(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.evidence(
                    active, scope={"part_ids": ["missing-part"]}
                )
            ),
            "evidence_part_not_found",
        )

    def test_evidence_scope_rejects_event_part_mismatch(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.evidence(
                    active,
                    scope={"event_ids": ["event-1"], "part_ids": ["part-2"]},
                )
            ),
            "evidence_event_part_mismatch",
        )

    def test_render_measurement_requires_current_candidate_scope(self) -> None:
        active = self.activate()
        state = active.detached_state()
        iteration = state["iterations"][0]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
        )
        body = {
            "category": "aesthetic_risk",
            "code": "mix.loudness_claim",
            "basis": {
                "kind": "render_measurement",
                "reference": "/measurements/loudness/integrated",
                "artifact_sha256": "6" * 64,
                "artifact_role": "post_render_check",
            },
            "reporter": "agent",
            "perception_basis": "report_only",
            "summary": "A report-bound loudness concern.",
            "observation": "The referenced measurement needs interpretation.",
            "interpretation": "It may affect the intended dynamic arc.",
            "confidence": "medium",
            "scope": {
                "authoring_revision": iteration["anchor"]["authoring_revision"],
                "candidate_id": None,
                "start_seconds": None,
                "end_seconds": None,
                "event_ids": [],
                "part_ids": [],
            },
            "blocking": False,
            "automatic_change": False,
            "recorded_at_utc": state["updated_at_utc"],
        }
        evidence_id = _content_id(
            "evidence-", state=state, iteration=iteration, body=body
        )
        iteration["evidence"].append({"evidence_id": evidence_id, **body})
        workflow_module._refresh_usage(state)
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "render_measurement_requires_current_candidate_scope",
        )

    def test_exception_rejects_basis_target_mismatch(self) -> None:
        evidence = self.evidence(self.activate())
        evidence_id = evidence.state["iterations"][0]["evidence"][0][
            "evidence_id"
        ]
        self.assertEqual(
            _error_code(
                lambda: register_workflow_exception(
                    self.root,
                    workflow_id=evidence.workflow_id,
                    expected_revision=evidence.revision,
                    target_type="work_charter",
                    target_ref="ending_contract",
                    purpose="Try to except an unrelated charter field.",
                    scope="This iteration.",
                    higher_value="A claimed higher value.",
                    cost="A claimed cost.",
                    recovery="A claimed recovery.",
                    evidence_ids=[evidence_id],
                )
            ),
            "exception_target_evidence_mismatch",
        )

    def test_new_decision_requires_complete_evidence_disposition_coverage(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        first = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.boundary_blurs",
        )
        second = self.evidence(
            first,
            category="aesthetic_risk",
            code="structure.identity_thins",
        )
        iteration = second.state["iterations"][0]
        evidence_ids = [item["evidence_id"] for item in iteration["evidence"]]
        review_ids = [item["review_id"] for item in iteration["reviews"]]
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=second.workflow_id,
                    expected_revision=second.revision,
                    disposition="stop",
                    summary="Do not silently drop the second claim.",
                    rationale="Every new decision closes its complete claim set.",
                    final_authority="agent",
                    perception_basis="report_only",
                    evidence_ids=evidence_ids,
                    evidence_dispositions=[
                        {
                            "evidence_id": evidence_ids[0],
                            "disposition": "accepted_risk",
                            "rationale": "The authority accepts this bounded risk.",
                            "basis_ids": [],
                        }
                    ],
                    review_ids=review_ids,
                )
            ),
            "evidence_disposition_incomplete",
        )

    def test_accept_rejects_every_nonterminal_evidence_disposition(self) -> None:
        first = self.evidence(self.activate())
        evidence = self.evidence(
            first,
            category="aesthetic_risk",
            code="structure.acceptance_boundary",
        )
        state = evidence.detached_state()
        iteration = state["iterations"][0]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
        )
        review_ids = [
            _append_review(state, phase=phase)
            for phase in (
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
            )
        ]
        promise_id, risk_id = [
            item["evidence_id"] for item in iteration["evidence"]
        ]
        for open_disposition in ("deferred", "revision_target", "contested"):
            with self.subTest(disposition=open_disposition):
                decision = _decision(
                    iteration=iteration,
                    disposition="accept",
                    evidence_ids=[promise_id, risk_id],
                    evidence_dispositions=[
                        {
                            "evidence_id": promise_id,
                            "disposition": open_disposition,
                            "rationale": (
                                "Acceptance cannot leave this claim open."
                            ),
                            "basis_ids": (
                                [risk_id]
                                if open_disposition == "contested"
                                else []
                            ),
                        },
                        {
                            "evidence_id": risk_id,
                            "disposition": "accepted_risk",
                            "rationale": "The authority accepts this bounded risk.",
                            "basis_ids": [],
                        },
                    ],
                    review_ids=review_ids,
                )
                self.assertEqual(
                    _error_code(
                        lambda decision=decision: workflow_module._validate_decision(
                            decision, iteration=iteration
                        )
                    ),
                    "acceptance_evidence_still_open",
                )

    def test_revise_requires_at_least_one_revision_target(self) -> None:
        reviewed = self.review(self.activate())
        evidence = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.return_register",
        )
        iteration = evidence.state["iterations"][0]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=evidence.workflow_id,
                    expected_revision=evidence.revision,
                    disposition="revise",
                    summary="A revision must name its target claim.",
                    rationale="Deferred alone does not define the experiment.",
                    final_authority="agent",
                    perception_basis="report_only",
                    evidence_ids=[evidence_id],
                    evidence_dispositions=[
                        {
                            "evidence_id": evidence_id,
                            "disposition": "deferred",
                            "rationale": "This deliberately omits revision_target.",
                            "basis_ids": [],
                        }
                    ],
                    review_ids=[review_id],
                    expected_audible_change="The return moves to a lower register.",
                    revision_scope=_score_metadata_revision_scope(),
                    withdrawal_condition="Withdraw if the change exceeds score metadata.",
                )
            ),
            "revision_target_required",
        )

    def test_evidence_disposition_cannot_use_its_own_claim_as_basis(self) -> None:
        evidence = self.evidence(
            self.review(self.activate()),
            category="aesthetic_risk",
            code="structure.self_reference",
        )
        iteration = evidence.detached_state()["iterations"][-1]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        decision = _decision(
            iteration=iteration,
            disposition="preserve",
            evidence_ids=[evidence_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "resolved",
                    "rationale": "A claim cannot prove its own resolution.",
                    "basis_ids": [evidence_id],
                }
            ],
            review_ids=[review_id],
        )
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_decision(
                    decision, iteration=iteration
                )
            ),
            "evidence_disposition_basis_self_reference",
        )

    def test_contested_disposition_requires_another_evidence_claim(self) -> None:
        evidence = self.evidence(
            self.review(self.activate()),
            category="aesthetic_risk",
            code="structure.contested_without_counterclaim",
        )
        iteration = evidence.detached_state()["iterations"][-1]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        decision = _decision(
            iteration=iteration,
            disposition="preserve",
            evidence_ids=[evidence_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "contested",
                    "rationale": "A review alone is not a competing claim.",
                    "basis_ids": [review_id],
                }
            ],
            review_ids=[review_id],
        )
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_decision(
                    decision, iteration=iteration
                )
            ),
            "contested_evidence_basis_required",
        )

    def test_selected_reviews_must_include_the_decision_authority_basis(self) -> None:
        state = self.activate().detached_state()
        iteration = state["iterations"][-1]
        creator_review_id = _append_review(
            state, phase="intent", reviewer="creator"
        )
        decision = _decision(
            iteration=iteration,
            disposition="preserve",
            evidence_ids=[],
            evidence_dispositions=[],
            review_ids=[creator_review_id],
        )
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_decision(
                    decision, iteration=iteration
                )
            ),
            "decision_perception_basis_unproven",
        )

    def test_accept_review_ids_must_cover_the_four_required_phases(self) -> None:
        evidence = self.evidence(
            self.activate(),
            category="aesthetic_risk",
            code="structure.review_coverage",
        )
        state = evidence.detached_state()
        iteration = state["iterations"][0]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
        )
        review_ids = [
            _append_review(state, phase=phase)
            for phase in (
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
            )
        ]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        decision = _decision(
            iteration=iteration,
            disposition="accept",
            evidence_ids=[evidence_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "accepted_risk",
                    "rationale": "The authority knowingly accepts this risk.",
                    "basis_ids": [],
                }
            ],
            review_ids=review_ids[:-1],
        )
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_decision(
                    decision, iteration=iteration
                )
            ),
            "acceptance_review_ids_incomplete",
        )

    def test_direct_stop_exposes_all_open_evidence_ids(self) -> None:
        evidence = self.evidence(self.activate())
        evidence_id = evidence.state["iterations"][0]["evidence"][0][
            "evidence_id"
        ]
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            reason="cancelled",
            summary="Stop without pretending that the open claim vanished.",
            final_authority="agent",
            perception_basis="report_only",
        )
        self.assertEqual(
            stopped.state["termination"]["open_evidence_ids"], [evidence_id]
        )

    def test_direct_stop_reopens_every_claim_after_a_revision_decision(self) -> None:
        reviewed = self.review(self.activate())
        promise = self.evidence(reviewed)
        risk = self.evidence(
            promise,
            category="aesthetic_risk",
            code="structure.direct_stop_target",
        )
        iteration = risk.state["iterations"][-1]
        review_id = iteration["reviews"][0]["review_id"]
        promise_id, risk_id = [
            item["evidence_id"] for item in iteration["evidence"]
        ]
        pending = decide_workflow_iteration(
            self.root,
            workflow_id=risk.workflow_id,
            expected_revision=risk.revision,
            disposition="revise",
            summary="Prepare a bounded revision before an external stop.",
            rationale="One claim appears resolved while another remains the target.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[promise_id, risk_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": promise_id,
                    "disposition": "resolved",
                    "rationale": "The selected review resolves this claim for the revision decision.",
                    "basis_ids": [review_id],
                },
                {
                    "evidence_id": risk_id,
                    "disposition": "revision_target",
                    "rationale": "This risk remains the explicit revision target.",
                    "basis_ids": [],
                },
            ],
            expected_audible_change="The target passage changes without erasing the promise.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            reason="cancelled",
            summary="External cancellation does not claim the revision was completed.",
            final_authority="agent",
            perception_basis="report_only",
        )
        self.assertEqual(
            stopped.state["termination"]["open_evidence_ids"],
            [promise_id, risk_id],
        )

    def test_history_rejects_child_that_rewrites_content_bound_claim_records(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        evidence = self.evidence(reviewed)
        evidence_id = evidence.state["iterations"][0]["evidence"][0][
            "evidence_id"
        ]
        registered = register_workflow_exception(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            target_type="work_charter",
            target_ref="one_sentence_promise",
            purpose="Preserve a deliberate early rupture.",
            scope="This iteration and this promise conflict only.",
            higher_value="Narrative shock.",
            cost="The original arc becomes less legible.",
            recovery="Preserve the unmodified candidate.",
            evidence_ids=[evidence_id],
        )
        state = registered.detached_state()
        iteration = state["iterations"][0]

        review = iteration["reviews"][0]
        review["summary"] = "Rewritten historical review in an appended child."
        review_body = {
            key: value for key, value in review.items() if key != "review_id"
        }
        review["review_id"] = _content_id(
            "review-", state=state, iteration=iteration, body=review_body
        )

        claim = iteration["evidence"][0]
        claim["observation"] = (
            "Rewritten historical evidence in an appended child."
        )
        claim_body = {
            key: value for key, value in claim.items() if key != "evidence_id"
        }
        claim["evidence_id"] = _content_id(
            "evidence-", state=state, iteration=iteration, body=claim_body
        )

        exception = iteration["exceptions"][0]
        exception["evidence_ids"] = [claim["evidence_id"]]
        exception["recovery"] = (
            "Rewritten historical exception in an appended child."
        )
        exception_body = {
            key: value
            for key, value in exception.items()
            if key != "exception_id"
        }
        exception["exception_id"] = _content_id(
            "exception-", state=state, iteration=iteration, body=exception_body
        )

        layout = workflow_module._existing_layout(
            self.root, registered.workflow_id
        )
        _append_forged_child_and_repoint(layout, registered, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=registered.workflow_id
                )
            ),
            "workflow_history_claim_record_rewritten",
        )

    def test_new_terminal_projection_cannot_drop_open_evidence_ids(self) -> None:
        reviewed = self.review(self.activate())
        evidence = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.open_projection",
        )
        iteration = evidence.state["iterations"][0]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        stopped = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="stop",
            summary="Keep the unresolved projection explicit.",
            rationale="New lifecycle decisions must project their open claims.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "deferred",
                    "rationale": "This claim remains open at termination.",
                    "basis_ids": [],
                }
            ],
        )
        state = stopped.detached_state()
        state["termination"].pop("open_evidence_ids")
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "termination_open_evidence_missing",
        )

    def test_new_dialect_child_cannot_downgrade_claim_lifecycle_to_legacy(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        evidence = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.dialect_downgrade",
        )
        iteration = evidence.state["iterations"][0]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        stopped = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="stop",
            summary="Create an explicitly new-dialect terminal decision.",
            rationale="A child may not relabel this record as legacy.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "deferred",
                    "rationale": "This claim remains open at termination.",
                    "basis_ids": [],
                }
            ],
        )
        state = stopped.detached_state()
        decision = state["iterations"][0]["decision"]
        decision.pop("review_ids")
        decision.pop("evidence_dispositions")
        decision.pop("charter_settlement")
        state["termination"].pop("open_evidence_ids")
        state["policy"] = copy.deepcopy(workflow_module._LEGACY_POLICY)
        layout = workflow_module._existing_layout(self.root, stopped.workflow_id)
        _append_forged_child_and_repoint(layout, stopped, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=stopped.workflow_id
                )
            ),
            "workflow_claim_lifecycle_downgrade",
        )

    def test_resolved_evidence_dependencies_must_be_acyclic(self) -> None:
        promise = self.evidence(self.activate())
        risk = self.evidence(
            promise,
            category="aesthetic_risk",
            code="structure.circular_resolution",
        )
        state = risk.detached_state()
        iteration = state["iterations"][0]
        iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
        )
        review_ids = [
            _append_review(state, phase=phase)
            for phase in (
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
            )
        ]
        promise_id, risk_id = [
            item["evidence_id"] for item in iteration["evidence"]
        ]
        decision = _decision(
            iteration=iteration,
            disposition="accept",
            evidence_ids=[promise_id, risk_id],
            evidence_dispositions=[
                {
                    "evidence_id": promise_id,
                    "disposition": "resolved",
                    "rationale": "The risk is claimed to resolve the promise conflict.",
                    "basis_ids": [risk_id],
                },
                {
                    "evidence_id": risk_id,
                    "disposition": "resolved",
                    "rationale": "The promise conflict is claimed to resolve the risk.",
                    "basis_ids": [promise_id],
                },
            ],
            review_ids=review_ids,
        )
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_decision(
                    decision, iteration=iteration
                )
            ),
            "evidence_resolution_cycle",
        )

    def test_decision_driven_terminal_timestamps_are_one_event(self) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][0]["reviews"][0][
            "review_id"
        ]
        stopped = decide_workflow_iteration(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            disposition="stop",
            summary="Close one decision-driven terminal event.",
            rationale="Its decision, close and termination timestamps are one event.",
            final_authority="agent",
            perception_basis="report_only",
            review_ids=[review_id],
            evidence_dispositions=[],
        )
        state = stopped.detached_state()
        iteration = state["iterations"][0]
        decision_time = datetime.strptime(
            iteration["decision"]["decided_at_utc"], "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=timezone.utc)
        later = (decision_time + timedelta(seconds=3)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        iteration["closed_at_utc"] = later
        state["termination"]["terminated_at_utc"] = later
        state["updated_at_utc"] = later
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_terminal_decision_timestamp_mismatch",
        )

    def test_legacy_decision_and_termination_without_new_fields_remain_readable(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        review_id = reviewed.state["iterations"][0]["reviews"][0]["review_id"]
        stopped = decide_workflow_iteration(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            disposition="stop",
            summary="Create a new-shape stop before modeling its legacy form.",
            rationale="Legacy history remains readable but cannot claim new closure.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_dispositions=[],
            review_ids=[review_id],
        )
        state = stopped.detached_state()
        decision = state["iterations"][0]["decision"]
        decision.pop("evidence_dispositions")
        decision.pop("review_ids")
        decision.pop("charter_settlement")
        state["termination"].pop("open_evidence_ids")
        state["policy"] = copy.deepcopy(workflow_module._LEGACY_POLICY)
        _validate_state_document(state)

    def test_hash_consistent_terminal_reseal_cannot_hide_open_evidence(
        self,
    ) -> None:
        evidence = self.evidence(self.activate())
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            reason="cancelled",
            summary="Leave the conflict explicitly open.",
            final_authority="agent",
            perception_basis="report_only",
        )
        state = stopped.detached_state()
        self.assertTrue(state["termination"]["open_evidence_ids"])
        state["termination"]["open_evidence_ids"] = []
        layout = workflow_module._existing_layout(
            self.root, stopped.workflow_id
        )
        revision, directory = _forge_revision_directory(layout, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_revision_directory(
                    directory,
                    workflow_id=state["workflow_id"],
                    project_id=state["project_id"],
                    revision=revision,
                )
            ),
            "termination_open_evidence_mismatch",
        )

    def test_managed_candidate_authorization_is_bound_back_to_its_iteration(
        self,
    ) -> None:
        state = self.activate().detached_state()
        iteration = state["iterations"][-1]
        candidate, attempt = _fake_managed_candidate_anchor(
            state,
            reservation_revision="7" * 64,
        )
        iteration["render_attempts"].append(attempt)
        iteration["anchor"]["candidate"] = candidate
        workflow_module._refresh_usage(state)
        _validate_state_document(state)
        candidate["workflow_authorization"]["workflow_id"] = "0" * 32
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "candidate_workflow_binding_mismatch",
        )

    def test_current_policy_managed_acceptance_gate_is_valid(self) -> None:
        state = self.valid_latest_accept_state()
        _validate_state_document(state)
        gate = state["termination"]["acceptance_gate"]
        self.assertEqual(gate["checked_hard_failure_evidence_ids"], [])
        self.assertIsNone(gate["readiness_result_sha256"])
        self.assertEqual(
            {
                item["target"]
                for item in state["iterations"][-1]["decision"][
                    "charter_settlement"
                ]
            },
            set(
                workflow_module._charter_settlement_targets(
                    state["work_charter"]
                )
            ),
        )

    def test_acceptance_gate_rejects_binding_and_nullable_tamper(self) -> None:
        cases = (
            (
                "authoring_revision",
                lambda gate: gate.__setitem__("authoring_revision", "b" * 64),
                "acceptance_gate_binding_mismatch",
            ),
            (
                "candidate_manifest",
                lambda gate: gate.__setitem__(
                    "candidate_manifest_sha256", "b" * 64
                ),
                "acceptance_gate_binding_mismatch",
            ),
            (
                "checked_evidence",
                lambda gate: gate.update(
                    {
                        "checked_hard_failure_evidence_ids": [
                            "evidence-" + "b" * 20
                        ],
                        "readiness_result_sha256": "c" * 64,
                    }
                ),
                "acceptance_gate_binding_mismatch",
            ),
            (
                "recorded_at",
                lambda gate: gate.__setitem__(
                    "recorded_at_utc", "2099-01-01T00:00:00.000Z"
                ),
                "acceptance_gate_binding_mismatch",
            ),
            (
                "readiness_nullable_contract",
                lambda gate: gate.__setitem__(
                    "readiness_result_sha256", "d" * 64
                ),
                "invalid_acceptance_gate",
            ),
        )
        for label, mutate, expected_error in cases:
            with self.subTest(label=label):
                state = self.valid_managed_accept_state()
                mutate(state["termination"]["acceptance_gate"])
                self.assertEqual(
                    _error_code(lambda state=state: _validate_state_document(state)),
                    expected_error,
                )

    def test_current_policy_accept_cannot_omit_acceptance_gate(self) -> None:
        state = self.valid_latest_accept_state()
        state["termination"].pop("acceptance_gate")
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "acceptance_gate_missing",
        )

    def test_pre_gate_policy_accepts_remain_readable_without_gate(self) -> None:
        claim_state = self.valid_managed_accept_state()
        claim_state["termination"].pop("acceptance_gate")
        claim_state["policy"] = copy.deepcopy(
            workflow_module._CLAIM_LIFECYCLE_POLICY
        )
        _validate_state_document(claim_state)

        legacy_state = self.valid_managed_accept_state()
        legacy_state["termination"].pop("acceptance_gate")
        legacy_state["termination"].pop("open_evidence_ids")
        legacy_decision = legacy_state["iterations"][-1]["decision"]
        legacy_decision.pop("review_ids")
        legacy_decision.pop("evidence_dispositions")
        legacy_decision.pop("charter_settlement", None)
        legacy_state["policy"] = copy.deepcopy(workflow_module._LEGACY_POLICY)
        _validate_state_document(legacy_state)

    def test_non_accept_terminations_cannot_smuggle_an_acceptance_gate(
        self,
    ) -> None:
        gate = copy.deepcopy(
            self.valid_managed_accept_state()["termination"]["acceptance_gate"]
        )
        disabled = create_creative_workflow(
            self.root, mode="off", final_authority="agent"
        ).detached_state()
        pending = create_creative_workflow(
            self.root, mode="iterate", final_authority="agent"
        )
        zero_iteration = terminate_creative_workflow(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            reason="cancelled",
            summary="Stop before activation.",
            final_authority="agent",
        ).detached_state()
        active = self.activate()
        ordinary_stop = terminate_creative_workflow(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            reason="cancelled",
            summary="Stop an ordinary active workflow.",
            final_authority="agent",
        ).detached_state()

        for label, state in (
            ("disabled", disabled),
            ("zero_iteration", zero_iteration),
            ("ordinary_stop", ordinary_stop),
        ):
            with self.subTest(label=label):
                state["termination"]["acceptance_gate"] = copy.deepcopy(gate)
                self.assertEqual(
                    _error_code(lambda state=state: _validate_state_document(state)),
                    "acceptance_gate_not_applicable",
                )

    def test_records_and_candidate_are_frozen_after_a_decision(self) -> None:
        reviewed = self.review(self.activate())
        recorded = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.freeze_after_decision",
        )
        iteration = recorded.state["iterations"][-1]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        decided = decide_workflow_iteration(
            self.root,
            workflow_id=recorded.workflow_id,
            expected_revision=recorded.revision,
            disposition="revise",
            summary="Freeze the complete decision input snapshot.",
            rationale="Later records must belong to a later iteration.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[iteration["reviews"][0]["review_id"]],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "This is the bounded target of the revision.",
                    "basis_ids": [],
                }
            ],
            expected_audible_change="Delay the return by one structural unit.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        previous = decided.detached_state()
        parent_revision = "e" * 64

        for label in ("review", "candidate"):
            with self.subTest(label=label):
                current = copy.deepcopy(previous)
                current["parent_revision"] = parent_revision
                current["sequence"] = previous["sequence"] + 1
                if label == "review":
                    _append_review(current, phase="symbolic_structure")
                else:
                    current["iterations"][-1]["anchor"][
                        "candidate"
                    ] = _fake_candidate_anchor(
                        current["iterations"][-1]["anchor"][
                            "authoring_revision"
                        ],
                        verified_at_utc=current["updated_at_utc"],
                    )
                expected_error = (
                    "workflow_history_candidate_injected"
                    if label == "candidate"
                    else "workflow_history_after_decision_rewritten"
                )
                self.assertEqual(
                    _error_code(
                        lambda current=current: workflow_module._validate_state_transition(
                            previous,
                            current,
                            parent_revision=parent_revision,
                        )
                    ),
                    expected_error,
                )

    def test_appended_pending_attempt_cannot_also_settle_the_old_attempt(
        self,
    ) -> None:
        previous = self.activate().detached_state()
        _candidate, completed = _fake_managed_candidate_anchor(
            previous,
            reservation_revision="7" * 64,
        )
        pending = copy.deepcopy(completed)
        pending["reservation_revision"] = None
        pending["status"] = "pending"
        pending["finished_at_utc"] = None
        previous["iterations"][-1]["render_attempts"] = [pending]
        workflow_module._refresh_usage(previous)

        parent_revision = "f" * 64
        current = copy.deepcopy(previous)
        current["parent_revision"] = parent_revision
        current["sequence"] = previous["sequence"] + 1
        completed["reservation_revision"] = parent_revision
        current_attempts = current["iterations"][-1]["render_attempts"]
        current_attempts[0] = completed
        appended = copy.deepcopy(pending)
        appended["attempt_number"] = 2
        appended["operation_id"] = "9" * 32
        appended["expected_candidate_id"] = workflow_module.portable_slug(
            f"workflow-{appended['operation_id']}", maximum_length=96
        )
        current_attempts.append(appended)

        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_state_transition(
                    previous,
                    current,
                    parent_revision=parent_revision,
                )
            ),
            "workflow_history_render_attempt_rewritten",
        )

    def test_decision_child_cannot_also_add_a_claim_or_candidate(self) -> None:
        reviewed = self.review(self.activate())
        claim_child = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.same_child_decision_claim",
        ).detached_state()
        claim_iteration = claim_child["iterations"][-1]
        evidence_id = claim_iteration["evidence"][-1]["evidence_id"]
        claim_iteration["decision"] = _decision(
            iteration=claim_iteration,
            disposition="revise",
            evidence_ids=[evidence_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "Move this claim into the next transition.",
                    "basis_ids": [],
                }
            ],
            review_ids=[claim_iteration["reviews"][0]["review_id"]],
            expected_audible_change="Test the claim in a later revision.",
        )

        previous = reviewed.detached_state()
        candidate_child = copy.deepcopy(previous)
        candidate_parent_revision = "d" * 64
        candidate_child["parent_revision"] = candidate_parent_revision
        candidate_child["sequence"] = previous["sequence"] + 1
        candidate_iteration = candidate_child["iterations"][-1]
        candidate_iteration["anchor"]["candidate"] = _fake_candidate_anchor(
            candidate_iteration["anchor"]["authoring_revision"],
            verified_at_utc=candidate_child["updated_at_utc"],
        )
        candidate_iteration["decision"] = _decision(
            iteration=candidate_iteration,
            disposition="preserve",
            evidence_ids=[],
            evidence_dispositions=[],
            review_ids=[candidate_iteration["reviews"][0]["review_id"]],
        )

        cases = (
            ("claim", claim_child, reviewed.revision),
            ("candidate", candidate_child, candidate_parent_revision),
        )
        for label, current, parent_revision in cases:
            with self.subTest(label=label):
                expected_error = (
                    "workflow_history_candidate_injected"
                    if label == "candidate"
                    else "workflow_history_decision_inputs_rewritten"
                )
                self.assertEqual(
                    _error_code(
                        lambda current=current, parent_revision=parent_revision: workflow_module._validate_state_transition(
                            previous,
                            current,
                            parent_revision=parent_revision,
                        )
                    ),
                    expected_error,
                )

    def test_managed_candidate_authorization_matches_source_completed_attempt(
        self,
    ) -> None:
        base = self.activate().detached_state()
        candidate, attempt = _fake_managed_candidate_anchor(
            base,
            reservation_revision="7" * 64,
        )
        iteration = base["iterations"][-1]
        iteration["render_attempts"].append(attempt)
        iteration["anchor"]["candidate"] = candidate
        workflow_module._refresh_usage(base)
        _validate_state_document(base)

        for field, replacement in (
            ("reservation_revision", "9" * 64),
            ("operation_id", "a" * 32),
            ("candidate_work_id", "different-work"),
        ):
            with self.subTest(field=field):
                state = copy.deepcopy(base)
                state["iterations"][-1]["anchor"]["candidate"][
                    "workflow_authorization"
                ][field] = replacement
                self.assertEqual(
                    _error_code(lambda state=state: _validate_state_document(state)),
                    "candidate_workflow_binding_mismatch",
                )

    def test_managed_candidate_reservation_must_be_a_real_parent_transition(
        self,
    ) -> None:
        parent = self.activate()
        state = parent.detached_state()
        iteration = state["iterations"][-1]
        candidate, attempt = _fake_managed_candidate_anchor(
            state,
            reservation_revision="7" * 64,
        )
        iteration["render_attempts"].append(attempt)
        iteration["anchor"]["candidate"] = candidate
        workflow_module._refresh_usage(state)
        layout = workflow_module._existing_layout(self.root, parent.workflow_id)
        _append_forged_child_and_repoint(layout, parent, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=parent.workflow_id
                )
            ),
            "workflow_history_candidate_injected",
        )

    def test_hash_consistent_accept_child_rechecks_managed_candidate(self) -> None:
        parent = self.activate()
        state = parent.detached_state()
        candidate = _fake_candidate_anchor(
            state["iterations"][-1]["anchor"]["authoring_revision"],
            verified_at_utc=state["updated_at_utc"],
        )
        _close_forged_accept(state, candidate=candidate)
        _attach_complete_charter_settlement(state)
        layout = workflow_module._existing_layout(self.root, parent.workflow_id)
        _append_forged_child_and_repoint(layout, parent, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=parent.workflow_id
                )
            ),
            "workflow_acceptance_contract_mismatch",
        )

    def test_hash_consistent_accept_child_requires_frozen_hard_failure_gate(self) -> None:
        active = self.activate()
        operation_id = "8" * 32
        pending_state = active.detached_state()
        pending_iteration = pending_state["iterations"][-1]
        candidate, completed_attempt = _fake_managed_candidate_anchor(
            pending_state,
            reservation_revision="7" * 64,
            operation_id=operation_id,
        )
        pending_attempt = copy.deepcopy(completed_attempt)
        pending_attempt["reservation_revision"] = None
        pending_attempt["status"] = "pending"
        pending_attempt["finished_at_utc"] = None
        pending_iteration["render_attempts"].append(pending_attempt)
        pending_iteration["status"] = "candidate_pending"
        pending_state["status"] = "candidate_pending"
        workflow_module._refresh_usage(pending_state)
        layout = workflow_module._existing_layout(self.root, active.workflow_id)
        _append_forged_child_and_repoint(layout, active, pending_state)
        pending = workflow_module.open_creative_workflow(
            self.root, workflow_id=active.workflow_id
        )

        recorded_state = pending.detached_state()
        recorded_iteration = recorded_state["iterations"][-1]
        candidate, completed_attempt = _fake_managed_candidate_anchor(
            recorded_state,
            reservation_revision=pending.revision,
            operation_id=operation_id,
        )
        recorded_iteration["render_attempts"][0] = completed_attempt
        recorded_iteration["anchor"]["candidate"] = candidate
        recorded_iteration["status"] = "reviewing"
        recorded_state["status"] = "reviewing"
        _append_forged_child_and_repoint(layout, pending, recorded_state)
        recorded = workflow_module.open_creative_workflow(
            self.root, workflow_id=active.workflow_id
        )
        blocked = workflow_module.record_verified_workflow_hard_failure(
            self.root,
            workflow_id=recorded.workflow_id,
            expected_revision=recorded.revision,
            issue_code="authoring_roster.unassigned_part",
        )

        accepted_state = blocked.detached_state()
        _close_forged_accept(
            accepted_state,
            candidate=accepted_state["iterations"][-1]["anchor"]["candidate"],
        )
        _attach_complete_charter_settlement(accepted_state)
        _append_forged_child_and_repoint(layout, blocked, accepted_state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=active.workflow_id
                )
            ),
            "acceptance_gate_missing",
        )

    def test_hash_consistent_child_cannot_append_already_completed_iteration(
        self,
    ) -> None:
        created = create_creative_workflow(
            self.root, mode="iterate", final_authority="agent"
        )
        activated = activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
        )
        state = activated.detached_state()
        candidate, completed_attempt = _fake_managed_candidate_anchor(
            state,
            reservation_revision=created.revision,
        )
        state["iterations"][-1]["render_attempts"].append(completed_attempt)
        _close_forged_accept(state, candidate=candidate)
        _attach_acceptance_gate(state)
        _attach_complete_charter_settlement(state)

        layout = workflow_module._existing_layout(self.root, created.workflow_id)
        _append_forged_child_and_repoint(layout, created, state)
        self.assertEqual(
            _error_code(
                lambda: workflow_module.verify_creative_workflow_history(
                    self.root, workflow_id=created.workflow_id
                )
            ),
            "workflow_history_iteration_appended_invalid",
        )

    def test_decision_terminal_cannot_mix_legacy_decision_with_new_projection(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        stopped = decide_workflow_iteration(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            disposition="stop",
            summary="Stop with an explicit empty claim projection.",
            rationale="The decision and its projection must use one dialect.",
            final_authority="agent",
            perception_basis="report_only",
            review_ids=[reviewed.state["iterations"][-1]["reviews"][0]["review_id"]],
            evidence_dispositions=[],
        )
        state = stopped.detached_state()
        decision = state["iterations"][-1]["decision"]
        decision.pop("review_ids")
        decision.pop("evidence_dispositions")
        decision.pop("charter_settlement")
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_claim_lifecycle_hybrid",
        )

    def test_direct_termination_may_project_open_claims_from_legacy_revise(
        self,
    ) -> None:
        reviewed = self.review(self.activate())
        evidence = self.evidence(
            reviewed,
            category="aesthetic_risk",
            code="structure.legacy_revision_handoff",
        )
        iteration = evidence.state["iterations"][-1]
        evidence_id = iteration["evidence"][0]["evidence_id"]
        review_id = iteration["reviews"][0]["review_id"]
        explicit = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="revise",
            summary="Model a historical revise decision.",
            rationale="A later direct stop must not pretend its claim vanished.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "This was the historical revision target.",
                    "basis_ids": [],
                }
            ],
            expected_audible_change="Test one bounded historical change.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        legacy_state = explicit.detached_state()
        legacy_decision = legacy_state["iterations"][-1]["decision"]
        legacy_decision.pop("review_ids")
        legacy_decision.pop("evidence_dispositions")
        legacy_decision.pop("charter_settlement")
        legacy_decision.pop("revision_contract")
        legacy_state["policy"] = copy.deepcopy(workflow_module._LEGACY_POLICY)
        _validate_state_document(legacy_state)
        stopped_state = copy.deepcopy(legacy_state)
        workflow_module._upgrade_legacy_derivation_shape_for_transition(
            stopped_state
        )
        prior_time = datetime.strptime(
            legacy_state["updated_at_utc"], "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=timezone.utc)
        stopped_at = (prior_time + timedelta(milliseconds=1)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        stopped_iteration = stopped_state["iterations"][-1]
        stopped_iteration["status"] = "closed"
        stopped_iteration["closed_at_utc"] = stopped_at
        stopped_iteration["outcome"] = "stopped"
        stopped_state["status"] = "stopped"
        stopped_state["termination"] = {
            "reason": "cancelled",
            "summary": "Stop after opening a legacy revision target.",
            "final_authority": "agent",
            "perception_basis": "report_only",
            "selected_candidate": None,
            "open_evidence_ids": [evidence_id],
            "terminated_at_utc": stopped_at,
        }
        stopped_state["updated_at_utc"] = stopped_at
        stopped_state["sequence"] = legacy_state["sequence"] + 1
        parent_revision = "f" * 64
        stopped_state["parent_revision"] = parent_revision
        workflow_module._refresh_usage(stopped_state)
        _validate_state_document(stopped_state)
        workflow_module._validate_state_transition(
            legacy_state,
            stopped_state,
            parent_revision=parent_revision,
        )
        self.assertEqual(
            stopped_state["termination"]["open_evidence_ids"],
            [evidence_id],
        )


if __name__ == "__main__":
    unittest.main()
