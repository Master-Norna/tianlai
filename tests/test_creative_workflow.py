from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tianlai.creative_workflow as workflow_module
from tianlai.authoring_project import create_authoring_project, save_authoring_project
from tianlai.creative_workflow import (
    CreativeWorkflowError,
    activate_creative_workflow,
    attach_existing_candidate_for_audit,
    cancel_workflow_render,
    create_creative_workflow,
    decide_workflow_iteration,
    inspect_workflow_candidate_status,
    open_creative_workflow,
    record_verified_workflow_hard_failure,
    record_workflow_authoring_revision,
    record_workflow_evidence,
    record_workflow_review,
    register_workflow_exception,
    request_workflow_render,
    terminate_creative_workflow,
    verify_active_render_reservation,
    verify_creative_workflow_history,
    verify_render_reservation_history,
    workflow_render_authorization,
)


ROOT = Path(__file__).resolve().parents[1]


def _charter() -> dict[str, object]:
    return {
        "title": "A bounded experiment",
        "one_sentence_promise": "Let a small motif earn one irreversible climax.",
        "target_listener_and_scene": "A focused listener in a quiet room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening three-note contour"],
            "transformable_parts": ["register", "orchestration"],
        },
        "ending_contract": "End with consequence, not merely with silence.",
    }


def _error_code(call) -> str:
    with unittest.TestCase().assertRaises(CreativeWorkflowError) as captured:
        call()
    return captured.exception.code


class CreativeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "创作 空间"
        self.authoring = create_authoring_project(self.root, title="Workflow Test")

    def create(self, *, mode: str = "iterate", authority: str = "agent"):
        return create_creative_workflow(
            self.root,
            mode=mode,
            final_authority=authority,
        )

    def activate(self, *, mode: str = "iterate", authority: str = "agent"):
        created = self.create(mode=mode, authority=authority)
        return activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
        )

    def test_core_activation_rejects_new_external_constitution_bindings_without_writing(
        self,
    ) -> None:
        created = self.create()
        constitution = {
            "document_id": "tianlai-music-constitution",
            "version": "0.1",
            "language": "zh-CN",
            "content_sha256": "0" * 64,
        }
        clause = {
            "clause_id": "C0.03",
            "role": "review_lens",
            "rationale": "Historical provenance cannot become a new binding.",
            "interpretation": "Reason from the work charter instead.",
        }

        for legacy_arguments in (
            {"constitution": constitution},
            {"active_clauses": [clause]},
            {"constitution": constitution, "active_clauses": [clause]},
        ):
            with self.subTest(legacy_arguments=sorted(legacy_arguments)):
                self.assertEqual(
                    _error_code(
                        lambda legacy_arguments=legacy_arguments: (
                            activate_creative_workflow(
                                self.root,
                                workflow_id=created.workflow_id,
                                expected_revision=created.revision,
                                work_charter=_charter(),
                                **legacy_arguments,
                            )
                        )
                    ),
                    "constitution_binding_provenance_only",
                )
                unchanged = open_creative_workflow(
                    self.root, workflow_id=created.workflow_id
                )
                self.assertEqual(unchanged.revision, created.revision)
                self.assertEqual(unchanged.state["status"], "charter_pending")
                self.assertIsNone(unchanged.state["constitution"])
                self.assertEqual(unchanged.state["active_clauses"], [])

    def review(self, snapshot, phase: str, *, reviewer: str = "agent"):
        return record_workflow_review(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            phase=phase,
            reviewer=reviewer,
            perception_basis=(
                "audio_audition" if phase == "audio_audition" else "report_only"
            ),
            summary=f"Reviewed {phase} without converting taste into a score.",
        )

    def render_ready(self):
        snapshot = self.activate()
        snapshot = self.review(snapshot, "symbolic_structure")
        return self.review(snapshot, "orchestration_performance")

    def test_mode_off_is_terminal_and_serializable(self) -> None:
        snapshot = self.create(mode="off", authority="creator")
        payload = json.loads(json.dumps(snapshot.to_dict(), ensure_ascii=False))
        self.assertEqual(payload["state"]["status"], "disabled")
        self.assertEqual(payload["state"]["final_authority"], "creator")
        self.assertEqual(payload["allowed_actions"], [])
        self.assertEqual(payload["state"]["termination"]["reason"], "mode_off")
        with self.assertRaises(TypeError):
            snapshot.state["status"] = "reviewing"
        self.assertEqual(
            _error_code(
                lambda: activate_creative_workflow(
                    self.root,
                    workflow_id=snapshot.workflow_id,
                    expected_revision=snapshot.revision,
                    work_charter=_charter(),
                )
            ),
            "illegal_workflow_transition",
        )

    def test_workflow_state_schema_version_requires_strict_integer(self) -> None:
        snapshot = self.create()
        for invalid_version in (True, 1.0):
            with self.subTest(invalid_version=invalid_version):
                state = snapshot.detached_state()
                state["schema_version"] = invalid_version
                self.assertEqual(
                    _error_code(
                        lambda: workflow_module._validate_state_document(state)
                    ),
                    "invalid_workflow_state",
                )

    def test_cas_rejects_stale_revision_and_history_is_append_only(self) -> None:
        active = self.activate()
        first = self.review(active, "intent")
        self.assertEqual(
            _error_code(lambda: self.review(active, "symbolic_structure")),
            "workflow_revision_conflict",
        )
        verification = verify_creative_workflow_history(
            self.root, workflow_id=first.workflow_id
        )
        self.assertEqual(verification["verified_revision_count"], 3)
        self.assertEqual(verification["current_sequence"], 3)
        reopened = open_creative_workflow(
            self.root, workflow_id=first.workflow_id, revision=active.revision
        )
        self.assertEqual(reopened.detached_state()["sequence"], 2)

    def test_revision_identity_race_returns_stable_workflow_error(self) -> None:
        active = self.activate()
        real_revalidate = workflow_module.revalidate_plain_directory

        def replace_current_revision(identity):
            if identity.path.name == active.revision:
                raise OSError("revision disappeared at C:\\private\\workflow")
            return real_revalidate(identity)

        with mock.patch.object(
            workflow_module,
            "revalidate_plain_directory",
            side_effect=replace_current_revision,
        ):
            with self.assertRaises(CreativeWorkflowError) as captured:
                open_creative_workflow(
                    self.root,
                    workflow_id=active.workflow_id,
                )

        self.assertEqual(captured.exception.code, "unsafe_workflow_revision")
        self.assertEqual(str(captured.exception), "unsafe_workflow_revision")

    def test_existing_candidate_attachment_is_advertised_only_for_audit(self) -> None:
        iterate = self.activate(mode="iterate")
        self.assertNotIn(
            "attach_existing_candidate_for_audit",
            iterate.to_dict()["allowed_actions"],
        )
        self.assertEqual(
            _error_code(
                lambda: attach_existing_candidate_for_audit(
                    self.root,
                    workflow_id=iterate.workflow_id,
                    expected_revision=iterate.revision,
                    candidate_path=self.root / "renders" / "missing-candidate",
                )
            ),
            "candidate_attachment_requires_audit_mode",
        )
        unchanged = open_creative_workflow(
            self.root,
            workflow_id=iterate.workflow_id,
        )
        self.assertEqual(unchanged.revision, iterate.revision)

        audit = self.activate(mode="audit")
        self.assertIn(
            "attach_existing_candidate_for_audit",
            audit.to_dict()["allowed_actions"],
        )
        audit = self.review(audit, "symbolic_structure")
        audit = self.review(audit, "orchestration_performance")
        pending_audit = request_workflow_render(
            self.root,
            workflow_id=audit.workflow_id,
            expected_revision=audit.revision,
        )
        cancelled_audit = cancel_workflow_render(
            self.root,
            workflow_id=audit.workflow_id,
            expected_revision=pending_audit.revision,
        )
        self.assertNotIn(
            "attach_existing_candidate_for_audit",
            cancelled_audit.to_dict()["allowed_actions"],
        )

    def test_clock_rollback_preserves_causal_iteration_timestamps(self) -> None:
        ready = self.render_ready()
        rolled_back_clock = "2000-01-01T00:00:00.000Z"
        with mock.patch.object(
            workflow_module,
            "_now",
            return_value=rolled_back_clock,
        ):
            pending = request_workflow_render(
                self.root,
                workflow_id=ready.workflow_id,
                expected_revision=ready.revision,
            )
            cancelled = cancel_workflow_render(
                self.root,
                workflow_id=pending.workflow_id,
                expected_revision=pending.revision,
            )
            stopped = terminate_creative_workflow(
                self.root,
                workflow_id=cancelled.workflow_id,
                expected_revision=cancelled.revision,
                reason="cancelled",
                summary="Stop after a simulated wall-clock rollback.",
                final_authority="agent",
            )

        state = stopped.detached_state()
        iteration = state["iterations"][0]
        attempt = iteration["render_attempts"][0]
        self.assertLessEqual(
            attempt["requested_at_utc"], attempt["finished_at_utc"]
        )
        self.assertLessEqual(
            iteration["opened_at_utc"], iteration["closed_at_utc"]
        )
        self.assertLessEqual(iteration["closed_at_utc"], state["updated_at_utc"])
        self.assertEqual(
            verify_creative_workflow_history(
                self.root, workflow_id=stopped.workflow_id
            )["complete"],
            True,
        )

        invalid_render = json.loads(json.dumps(state))
        invalid_render["iterations"][0]["render_attempts"][0][
            "finished_at_utc"
        ] = rolled_back_clock
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_state_document(invalid_render)
            ),
            "invalid_render_timestamp",
        )

        invalid_iteration = json.loads(json.dumps(state))
        invalid_iteration["iterations"][0][
            "closed_at_utc"
        ] = rolled_back_clock
        self.assertEqual(
            _error_code(
                lambda: workflow_module._validate_state_document(
                    invalid_iteration
                )
            ),
            "invalid_iteration_timestamp",
        )

    def test_render_reservation_is_exact_active_then_historical(self) -> None:
        ready = self.render_ready()
        pending = request_workflow_render(
            self.root,
            workflow_id=ready.workflow_id,
            expected_revision=ready.revision,
        )
        authorization = workflow_render_authorization(pending)
        verified = verify_active_render_reservation(self.root, authorization)
        self.assertEqual(verified.revision, pending.revision)
        forged = dict(authorization)
        forged["operation_id"] = "0" * 32
        self.assertEqual(
            _error_code(lambda: verify_active_render_reservation(self.root, forged)),
            "render_reservation_mismatch",
        )
        cancelled = cancel_workflow_render(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
        )
        self.assertEqual(cancelled.detached_state()["status"], "reviewing")
        self.assertEqual(
            _error_code(
                lambda: verify_active_render_reservation(self.root, authorization)
            ),
            "render_reservation_not_active",
        )
        historical = verify_render_reservation_history(self.root, authorization)
        self.assertEqual(historical.revision, pending.revision)

    def test_orphan_reservation_is_not_authorized_as_history(self) -> None:
        ready = self.render_ready()
        pending = request_workflow_render(
            self.root,
            workflow_id=ready.workflow_id,
            expected_revision=ready.revision,
        )
        authorization = workflow_render_authorization(pending)
        layout = workflow_module._existing_layout(self.root, ready.workflow_id)
        ready_state = ready.detached_state()
        workflow_module._replace_manifest(
            layout,
            workflow_module._manifest_document(
                workflow_id=ready.workflow_id,
                project_id=ready.project_id,
                created_at_utc=ready.created_at_utc,
                updated_at_utc=ready.updated_at_utc,
                revision=ready.revision,
                sequence=ready_state["sequence"],
            ),
        )
        replacement = self.review(ready, "intent")
        self.assertNotEqual(replacement.revision, pending.revision)
        self.assertEqual(
            _error_code(
                lambda: verify_render_reservation_history(self.root, authorization)
            ),
            "render_reservation_not_in_current_history",
        )

    def test_public_evidence_cannot_forge_hard_or_machine_testimony(self) -> None:
        active = self.activate()
        common = {
            "project_root": self.root,
            "workflow_id": active.workflow_id,
            "expected_revision": active.revision,
            "code": "quality.claim",
            "basis_kind": "declared_promise",
            "basis_reference": "one_sentence_promise",
            "perception_basis": "report_only",
            "summary": "A claim",
            "observation": "An observation",
            "interpretation": "A contextual interpretation",
            "confidence": "medium",
        }
        self.assertEqual(
            _error_code(
                lambda: record_workflow_evidence(
                    category="hard_failure", reporter="agent", **common
                )
            ),
            "hard_failure_requires_trusted_boundary",
        )
        self.assertEqual(
            _error_code(
                lambda: record_workflow_evidence(
                    category="promise_conflict", reporter="validator", **common
                )
            ),
            "trusted_reporter_requires_internal_boundary",
        )
        self.assertEqual(
            _error_code(
                lambda: record_workflow_evidence(
                    category="aesthetic_risk",
                    code="audition.unavailable",
                    reporter="agent",
                    basis_kind="audio_audition",
                    basis_reference="unheard",
                    perception_basis="audio_audition",
                    summary="No candidate",
                    observation="No candidate exists",
                    interpretation="Listening cannot be claimed",
                    confidence="low",
                    project_root=self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                )
            ),
            "audio_audition_requires_current_candidate",
        )

    def test_promise_conflict_is_nonblocking_and_can_support_exception(self) -> None:
        active = self.activate()
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            category="promise_conflict",
            code="promise.climax_too_early",
            basis_kind="declared_promise",
            basis_reference="one_sentence_promise",
            reporter="agent",
            perception_basis="report_only",
            summary="The climax may arrive before it is earned.",
            observation="The current plan spends its densest texture early.",
            interpretation="This conflicts with the declared dramatic promise.",
            confidence="medium",
        )
        item = evidence.detached_state()["iterations"][-1]["evidence"][0]
        self.assertFalse(item["blocking"])
        self.assertFalse(item["automatic_change"])
        exception = register_workflow_exception(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            target_type="work_charter",
            target_ref="one_sentence_promise",
            purpose="Preserve a deliberately premature rupture.",
            scope="This iteration only.",
            higher_value="Narrative shock.",
            cost="The planned arc becomes less legible.",
            recovery="Retain the unmodified draft as a Pareto candidate.",
            evidence_ids=[item["evidence_id"]],
        )
        stored = exception.detached_state()["iterations"][-1]["exceptions"][0]
        self.assertEqual(stored["evidence_ids"], [item["evidence_id"]])

    def test_trusted_hard_failure_is_reproduced_and_blocks_render(self) -> None:
        active = self.activate()
        verified = record_verified_workflow_hard_failure(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            issue_code="authoring_roster.unassigned_part",
        )
        stored = verified.detached_state()["iterations"][-1]["evidence"][0]
        self.assertTrue(stored["blocking"])
        verified = self.review(verified, "symbolic_structure")
        verified = self.review(verified, "orchestration_performance")
        self.assertEqual(
            _error_code(
                lambda: request_workflow_render(
                    self.root,
                    workflow_id=verified.workflow_id,
                    expected_revision=verified.revision,
                )
            ),
            "hard_failure_blocks_render",
        )

    def test_resolved_environment_hard_failure_does_not_lock_same_revision(
        self,
    ) -> None:
        active = self.activate()
        blocked_readiness = {
            "status": "blocked",
            "render_allowed": False,
            "issues": [
                {
                    "code": "output.not_writable",
                    "decision": "block",
                }
            ],
            "issues_truncated": False,
        }
        ready_readiness = {
            "status": "ready",
            "render_allowed": True,
            "issues": [],
            "issues_truncated": False,
        }
        with mock.patch(
            "tianlai.creative_workflow.validate_project_readiness",
            return_value=blocked_readiness,
        ):
            recorded = record_verified_workflow_hard_failure(
                self.root,
                workflow_id=active.workflow_id,
                expected_revision=active.revision,
                issue_code="output.not_writable",
            )
            unresolved = workflow_module.unresolved_workflow_hard_failures(
                self.root,
                recorded,
            )
            self.assertEqual(
                [item["code"] for item in unresolved],
                ["output.not_writable"],
            )
        recorded = self.review(recorded, "symbolic_structure")
        recorded = self.review(recorded, "orchestration_performance")

        with mock.patch(
            "tianlai.creative_workflow.validate_project_readiness",
            return_value=ready_readiness,
        ):
            self.assertEqual(
                workflow_module.unresolved_workflow_hard_failures(
                    self.root,
                    recorded,
                ),
                [],
            )
            pending = request_workflow_render(
                self.root,
                workflow_id=recorded.workflow_id,
                expected_revision=recorded.revision,
            )

        state = pending.detached_state()
        self.assertEqual(state["status"], "candidate_pending")
        evidence = state["iterations"][0]["evidence"][0]
        self.assertEqual(evidence["code"], "output.not_writable")
        self.assertTrue(evidence["blocking"])

    def test_iterate_requires_evidence_and_binds_new_authoring_revision(self) -> None:
        active = self.activate()
        reviewed = self.review(active, "intent")
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            category="aesthetic_risk",
            code="structure.motive_evaporation",
            basis_kind="diagnostic_hypothesis",
            basis_reference="symbolic self-review",
            reporter="agent",
            perception_basis="report_only",
            summary="The motive may disappear rather than transform.",
            observation="No later event presently recalls its contour.",
            interpretation="A revision can test a transformed recurrence.",
            confidence="medium",
        )
        evidence_id = evidence.detached_state()["iterations"][-1]["evidence"][0][
            "evidence_id"
        ]
        review_id = evidence.detached_state()["iterations"][-1]["reviews"][0][
            "review_id"
        ]
        pending = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="revise",
            summary="Test one bounded recurrence.",
            rationale="The evidence names a falsifiable structural concern.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "This is the bounded claim the next revision tests.",
                    "basis_ids": [review_id],
                }
            ],
            expected_audible_change="The opening contour returns once in a new register.",
            revision_scope={
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
            },
            withdrawal_condition="Withdraw if the edit exceeds score metadata.",
        )
        documents = self.authoring.detached_documents()
        documents["score"]["tail_seconds"] = 2.25
        authoring = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=documents,
        )
        next_iteration = record_workflow_authoring_revision(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            authoring_revision=authoring.revision,
        )
        state = next_iteration.detached_state()
        self.assertEqual(state["iterations"][0]["outcome"], "revised")
        self.assertEqual(
            state["iterations"][1]["anchor"]["authoring_revision"],
            authoring.revision,
        )

    def test_final_authority_is_frozen(self) -> None:
        active = self.activate(authority="creator")
        self.assertEqual(
            _error_code(
                lambda: terminate_creative_workflow(
                    self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                    reason="cancelled",
                    summary="An agent cannot impersonate the creator.",
                    final_authority="agent",
                )
            ),
            "termination_authority_mismatch",
        )

    def test_stop_cannot_claim_audio_audition_without_candidate_and_review(
        self,
    ) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                    disposition="stop",
                    summary="Do not fabricate a listening claim.",
                    rationale="No candidate exists to hear.",
                    final_authority="agent",
                    perception_basis="audio_audition",
                )
            ),
            "audio_audition_review_required",
        )

    def test_unactivated_termination_cannot_claim_audio_audition(self) -> None:
        created = self.create()
        self.assertEqual(created.detached_state()["status"], "charter_pending")
        self.assertEqual(
            _error_code(
                lambda: terminate_creative_workflow(
                    self.root,
                    workflow_id=created.workflow_id,
                    expected_revision=created.revision,
                    reason="cancelled",
                    summary="No work or candidate has been auditioned.",
                    final_authority="agent",
                    perception_basis="audio_audition",
                )
            ),
            "decision_perception_basis_unproven",
        )

    def test_unactivated_audit_or_iterate_workflow_can_stop_report_only(
        self,
    ) -> None:
        for mode in ("audit", "iterate"):
            with self.subTest(mode=mode):
                created = self.create(mode=mode)
                stopped = terminate_creative_workflow(
                    self.root,
                    workflow_id=created.workflow_id,
                    expected_revision=created.revision,
                    reason="cancelled",
                    summary="Stop before activating a work charter.",
                    final_authority="agent",
                    perception_basis="report_only",
                )
                state = stopped.detached_state()
                self.assertEqual(state["status"], "stopped")
                self.assertIsNone(state["work_charter"])
                self.assertEqual(state["iterations"], [])
                self.assertEqual(
                    state["termination"]["perception_basis"], "report_only"
                )
                reopened = open_creative_workflow(
                    self.root, workflow_id=stopped.workflow_id
                )
                self.assertEqual(reopened.revision, stopped.revision)

    def test_revision_file_tampering_is_detected(self) -> None:
        active = self.activate()
        path = (
            self.root
            / ".tianlai"
            / "workflows"
            / f"workflow-{active.workflow_id}"
            / "revisions"
            / active.revision
            / "workflow-state.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["status"] = "stopped"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertIn(
            _error_code(
                lambda: open_creative_workflow(
                    self.root, workflow_id=active.workflow_id
                )
            ),
            {"workflow_revision_tampered", "workflow_revision_identity_mismatch"},
        )

    def test_candidate_inspection_rejects_relative_paths_before_io(self) -> None:
        self.assertEqual(
            _error_code(
                lambda: inspect_workflow_candidate_status(
                    self.root, candidate_path="relative/candidate"
                )
            ),
            "candidate_path_must_be_absolute",
        )

    def test_generated_snapshot_and_storage_metadata_match_schemas(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is an optional development dependency")
        snapshot = self.activate()
        public_schema = json.loads(
            (ROOT / "schemas" / "creative-workflow.schema.json").read_text(
                encoding="utf-8"
            )
        )
        storage_schema = json.loads(
            (ROOT / "schemas" / "creative-workflow-storage.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(public_schema)
        Draft202012Validator.check_schema(storage_schema)
        Draft202012Validator(public_schema).validate(snapshot.to_dict())
        directory = (
            self.root
            / ".tianlai"
            / "workflows"
            / f"workflow-{snapshot.workflow_id}"
        )
        pointer = json.loads((directory / "workflow.json").read_text(encoding="utf-8"))
        revision = json.loads(
            (directory / "revisions" / snapshot.revision / "revision.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(storage_schema)
        validator.validate(pointer)
        validator.validate(revision)


if __name__ == "__main__":
    unittest.main()
