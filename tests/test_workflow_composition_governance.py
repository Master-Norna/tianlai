from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import tianlai.creative_workflow as workflow_module
from tianlai.authoring_project import create_authoring_project, save_authoring_project
from tianlai.composition_map import COMPOSITION_MAP_KIND
from tianlai.creative_workflow import (
    CreativeWorkflowError,
    activate_creative_workflow,
    commit_workflow_charter_amendment,
    create_creative_workflow,
    decide_workflow_iteration,
    inspect_workflow_composition,
    preflight_workflow_charter_amendment,
    record_workflow_authoring_revision,
    record_workflow_composition_map,
    record_workflow_evidence,
    record_workflow_review,
    request_workflow_render,
)


def _charter() -> dict:
    return {
        "title": "One current work",
        "one_sentence_promise": "Let one contour acquire consequence.",
        "target_listener_and_scene": "A focused listener in one sitting.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening contour"],
            "transformable_parts": ["register", "orchestration"],
        },
        "scarce_resources": ["the highest register"],
        "ending_contract": "The ending must answer the opening contour.",
    }


def _score_metadata_revision_scope() -> dict[str, object]:
    return {
        "change_scale": "bounded",
        "documents": ["score"],
        "allowed_document_paths": {"score": ["/tail_seconds", "/title"]},
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


class WorkflowCompositionGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "governed work"
        self.authoring = create_authoring_project(self.root, title="Governed")
        created = create_creative_workflow(
            self.root,
            mode="iterate",
            final_authority="agent",
            composition_governance=True,
        )
        self.active = activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
        )

    def _claim(self, field: str, *, snapshot=None) -> str:
        current = self.active if snapshot is None else snapshot
        context = inspect_workflow_composition(
            self.root, workflow_id=current.workflow_id
        )
        for claim in context["charter_claim_index"]["claims"]:
            if claim["field_path"] == [field]:
                return claim["claim_id"]
        raise AssertionError(field)

    def _record_map(self, snapshot, *, dependency_field="ending_contract"):
        claim_id = self._claim(dependency_field, snapshot=snapshot)
        return record_workflow_composition_map(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            composition_map={
                "kind": COMPOSITION_MAP_KIND,
                "schema_version": 1,
                "nodes": [
                    {
                        "node_id": "whole-work",
                        "label": "Whole work",
                        "function": "Carry one established identity through a complete sequence.",
                        "depends_on_claim_ids": [claim_id],
                        "ending_response": "Return the identity with consequence.",
                    }
                ],
            },
        )

    def _review(self, snapshot, phase: str):
        context = inspect_workflow_composition(
            self.root, workflow_id=snapshot.workflow_id
        )
        known_claims = {
            item["claim_id"]
            for item in context["charter_claim_index"]["claims"]
        }
        node_claims = {
            item["node_id"]: item["depends_on_claim_ids"]
            for item in context["composition_map"]["nodes"]
        }

        def hints(value, suffix):
            found = set()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == suffix[:-1] or key.endswith(suffix):
                        values = child if isinstance(child, list) else [child]
                        found.update(item for item in values if isinstance(item, str))
                    found.update(hints(child, suffix))
            elif isinstance(value, list):
                for child in value:
                    found.update(hints(child, suffix))
            return found

        answers = []
        for question in context["review_questions"][phase]:
            claim_ids = sorted(hints(question, "claim_ids") & known_claims)
            node_ids = sorted(hints(question, "node_ids") & set(node_claims))
            if question["basis"] == {"source": "whole_work_governance"}:
                node_ids = ["whole-work"]
            if node_ids and not claim_ids:
                claim_ids = list(node_claims[node_ids[0]])
            answers.append(
                {
                    "question_id": question["question_id"],
                    "answer": "The cited current-work scope provides the bounded evidence.",
                    "claim_ids": claim_ids,
                    "node_ids": node_ids,
                    "event_ids": [],
                }
            )
        return record_workflow_review(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            phase=phase,
            reviewer="agent",
            perception_basis="report_only",
            summary=f"Answered the {phase} whole-work questions.",
            question_answers=answers,
        )

    def test_map_precedes_iteration_work_and_clean_map_still_asks_questions(self) -> None:
        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_review(
                self.root,
                workflow_id=self.active.workflow_id,
                expected_revision=self.active.revision,
                phase="intent",
                reviewer="agent",
                perception_basis="report_only",
                summary="This must not bypass the map.",
            )
        self.assertEqual(caught.exception.code, "composition_map_required_for_review")

        mapped = self._record_map(self.active)
        context = inspect_workflow_composition(
            self.root, workflow_id=mapped.workflow_id
        )
        self.assertTrue(context["read_only"])
        for phase in (
            "intent",
            "symbolic_structure",
            "orchestration_performance",
        ):
            self.assertGreaterEqual(len(context["review_questions"][phase]), 2)

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_review(
                self.root,
                workflow_id=mapped.workflow_id,
                expected_revision=mapped.revision,
                phase="intent",
                reviewer="agent",
                perception_basis="report_only",
                summary="Missing answers are not a review.",
            )
        self.assertEqual(caught.exception.code, "review_question_coverage_incomplete")

    def test_draft_map_may_expose_stale_referents_but_recording_rejects_them(self) -> None:
        claim_id = self._claim("ending_contract")
        draft_map = {
            "kind": COMPOSITION_MAP_KIND,
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "stale-draft",
                    "label": "Stale draft",
                    "function": "Expose references that still need rebinding.",
                    "depends_on_claim_ids": [claim_id],
                    "established_material": {"event_ids": ["missing-event"]},
                    "role_changes": [
                        {
                            "part_id": "missing-part",
                            "change": "would carry the foreground",
                        }
                    ],
                }
            ],
        }
        draft = inspect_workflow_composition(
            self.root,
            workflow_id=self.active.workflow_id,
            composition_map=draft_map,
        )
        kinds = {
            question["question_kind"]
            for question in draft["inspection"]["questions"]
        }
        self.assertIn("established_material_not_found", kinds)
        self.assertIn("role_part_not_found", kinds)

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_composition_map(
                self.root,
                workflow_id=self.active.workflow_id,
                expected_revision=self.active.revision,
                composition_map=draft_map,
            )
        self.assertEqual(
            caught.exception.code,
            "composition_map.score_referent_not_found",
        )

        stale_claim_map = {
            "kind": COMPOSITION_MAP_KIND,
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "stale-claim",
                    "label": "Stale claim",
                    "function": "Expose an obsolete charter binding before commit.",
                    "depends_on_claim_ids": ["claim-" + ("0" * 64)],
                }
            ],
        }
        stale_claim_draft = inspect_workflow_composition(
            self.root,
            workflow_id=self.active.workflow_id,
            composition_map=stale_claim_map,
        )
        self.assertIn(
            "missing_claim_dependency",
            {
                question["question_kind"]
                for question in stale_claim_draft["inspection"]["questions"]
            },
        )
        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_composition_map(
                self.root,
                workflow_id=self.active.workflow_id,
                expected_revision=self.active.revision,
                composition_map=stale_claim_map,
            )
        self.assertEqual(caught.exception.code, "composition_map.claim_not_found")

    def test_recorded_map_cannot_create_an_unanswerable_question_set(self) -> None:
        claim_id = self._claim("ending_contract")
        composition_map = {
            "kind": COMPOSITION_MAP_KIND,
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "question-budget",
                    "label": "Question budget",
                    "function": "Keep the forced review finite and answerable.",
                    "depends_on_claim_ids": [claim_id],
                    # symbolic_structure already contributes two mandatory
                    # whole-work questions, so 127 declared questions exceed
                    # the 128-answer public contract.
                    "open_questions": [
                        f"Question {index}" for index in range(127)
                    ],
                }
            ],
        }
        inspected = inspect_workflow_composition(
            self.root,
            workflow_id=self.active.workflow_id,
            composition_map=composition_map,
        )
        self.assertGreater(
            len(inspected["review_questions"]["symbolic_structure"]),
            128,
        )

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_composition_map(
                self.root,
                workflow_id=self.active.workflow_id,
                expected_revision=self.active.revision,
                composition_map=composition_map,
            )
        self.assertEqual(caught.exception.code, "review_question_budget_exceeded")

    def test_question_answers_cover_every_current_reference_in_the_question(self) -> None:
        claim_id = self._claim("ending_contract")
        mapped = record_workflow_composition_map(
            self.root,
            workflow_id=self.active.workflow_id,
            expected_revision=self.active.revision,
            composition_map={
                "kind": COMPOSITION_MAP_KIND,
                "schema_version": 1,
                "nodes": [
                    {
                        "node_id": "first",
                        "label": "First",
                        "function": "Establish the first state.",
                        "bar_range": {"start": 1, "end": 2},
                        "depends_on_claim_ids": [claim_id],
                    },
                    {
                        "node_id": "second",
                        "label": "Second",
                        "function": "Transform the same state.",
                        "bar_range": {"start": 2, "end": 3},
                        "depends_on_claim_ids": [claim_id],
                    },
                ],
            },
        )
        context = inspect_workflow_composition(
            self.root, workflow_id=mapped.workflow_id
        )
        questions = context["review_questions"]["symbolic_structure"]
        all_claim_ids = [
            item["claim_id"] for item in context["charter_claim_index"]["claims"]
        ]
        all_node_ids = [
            item["node_id"] for item in context["composition_map"]["nodes"]
        ]
        answers = [
            {
                "question_id": question["question_id"],
                "answer": "Every current reference named by the question is cited.",
                "claim_ids": all_claim_ids,
                "node_ids": all_node_ids,
                "event_ids": [],
            }
            for question in questions
        ]
        overlap = next(
            question
            for question in questions
            if question["question_kind"] == "overlapping_node_ranges"
        )
        next(
            answer
            for answer in answers
            if answer["question_id"] == overlap["question_id"]
        )["node_ids"] = ["first"]

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_review(
                self.root,
                workflow_id=mapped.workflow_id,
                expected_revision=mapped.revision,
                phase="symbolic_structure",
                reviewer="agent",
                perception_basis="report_only",
                summary="A partial citation must not pass a multi-node question.",
                question_answers=answers,
            )
        self.assertEqual(
            caught.exception.code,
            "review_question_reference_scope_mismatch",
        )

    def test_question_answers_must_cite_the_question_scope(self) -> None:
        mapped = self._record_map(self.active)
        context = inspect_workflow_composition(
            self.root, workflow_id=mapped.workflow_id
        )
        unrelated_claim = self._claim("title", snapshot=mapped)
        answers = [
            {
                "question_id": question["question_id"],
                "answer": "This deliberately cites a different charter claim.",
                "claim_ids": [unrelated_claim],
                "node_ids": ["whole-work"],
                "event_ids": [],
            }
            for question in context["review_questions"]["intent"]
        ]
        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_review(
                self.root,
                workflow_id=mapped.workflow_id,
                expected_revision=mapped.revision,
                phase="intent",
                reviewer="agent",
                perception_basis="report_only",
                summary="Unrelated references must not satisfy the review gate.",
                question_answers=answers,
            )
        self.assertEqual(
            caught.exception.code,
            "review_question_reference_scope_mismatch",
        )

    def test_complete_question_reviews_are_required_before_render(self) -> None:
        current = self._record_map(self.active)
        current = self._review(current, "intent")
        current = self._review(current, "symbolic_structure")
        with self.assertRaises(CreativeWorkflowError) as caught:
            request_workflow_render(
                self.root,
                workflow_id=current.workflow_id,
                expected_revision=current.revision,
            )
        self.assertEqual(caught.exception.code, "pre_render_review_incomplete")
        current = self._review(current, "orchestration_performance")
        pending = request_workflow_render(
            self.root,
            workflow_id=current.workflow_id,
            expected_revision=current.revision,
        )
        self.assertEqual(pending.state["status"], "candidate_pending")

    def _prepare_amendment(self):
        current = self._record_map(self.active)
        current = self._review(current, "intent")
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=current.workflow_id,
            expected_revision=current.revision,
            category="promise_conflict",
            code="ending.contract_conflict",
            basis_kind="declared_promise",
            basis_reference="ending_contract",
            reporter="agent",
            perception_basis="report_only",
            summary="The current ending contract blocks the discovered consequence.",
            observation="The whole-work review identified a different necessary response.",
            interpretation="A bounded charter amendment may be more honest than an exception.",
            confidence="high",
        )
        state = evidence.detached_state()
        iteration = state["iterations"][-1]
        evidence_id = iteration["evidence"][-1]["evidence_id"]
        review_id = iteration["reviews"][-1]["review_id"]
        target_claim = self._claim("ending_contract", snapshot=evidence)
        proposal = {
            "summary": "Change only the ending obligation.",
            "why_score_revision_is_insufficient": "The old ending standard itself rejects the newly discovered consequence.",
            "why_bounded_exception_is_insufficient": "An exception would preserve two contradictory ending standards.",
            "expected_gain": "The ending can answer the opening without pretending the old wording still governs.",
            "accepted_costs": ["Rebuild and review the complete sequence."],
            "replacement_constraints": ["Keep the opening contour invariant."],
            "failure_conditions": ["Reject if the contour becomes unrecognizable."],
            "basis_ids": [evidence_id],
            "operations": [
                {
                    "op": "replace",
                    "claim_id": target_claim,
                    "value": "The ending must transform the opening contour into a necessary consequence.",
                }
            ],
        }
        preflight = preflight_workflow_charter_amendment(
            self.root,
            workflow_id=evidence.workflow_id,
            proposal=proposal,
        )
        self.assertFalse(preflight["active"])
        self.assertTrue(
            preflight["preflight"]["cost"][
                "whole_work_consistency_review_required"
            ]
        )
        pending = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="revise",
            summary="Undertake the bounded ending reconstruction.",
            rationale="The proposal states its replacement rule and failure condition.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "This is the conflict the next complete version tests.",
                    "basis_ids": [review_id],
                }
            ],
            expected_audible_change="The ending answers the contour through transformation.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        return proposal, preflight, pending

    def test_amendment_cost_is_acknowledged_before_authoring_change(self) -> None:
        proposal, preflight, pending = self._prepare_amendment()
        self.assertFalse(preflight["active"])
        self.assertTrue(
            preflight["preflight"]["cost"][
                "whole_work_consistency_review_required"
            ]
        )
        pending_preflight = preflight_workflow_charter_amendment(
            self.root,
            workflow_id=pending.workflow_id,
            proposal=proposal,
        )
        self.assertEqual(
            pending_preflight["preflight"]["preflight_sha256"],
            preflight["preflight"]["preflight_sha256"],
        )
        bad_ack = dict(preflight["cost_acknowledgement_required_for_commit"])
        bad_ack["affected_claim_count"] = True
        with self.assertRaises(CreativeWorkflowError):
            commit_workflow_charter_amendment(
                self.root,
                workflow_id=pending.workflow_id,
                expected_revision=pending.revision,
                proposal=proposal,
                expected_preflight_sha256=preflight["preflight"][
                    "preflight_sha256"
                ],
                cost_acknowledgement=bad_ack,
            )
        committed = commit_workflow_charter_amendment(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            proposal=proposal,
            expected_preflight_sha256=preflight["preflight"]["preflight_sha256"],
            cost_acknowledgement=preflight[
                "cost_acknowledgement_required_for_commit"
            ],
        )
        self.assertEqual(len(committed.state["governance"]["amendments"]), 1)
        amendment = committed.state["governance"]["amendments"][0]
        fence = amendment["authoring_causal_fence"]
        self.assertEqual(fence["anchor_revision"], self.authoring.revision)
        self.assertEqual(fence["save_sequence"], self.authoring.save_sequence)
        self.assertEqual(
            fence["anchor_save_event_sha256"],
            self.authoring.save_event_sha256,
        )
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            pass
        else:
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "schemas"
                    / "creative-workflow.schema.json"
                ).read_text(encoding="utf-8")
            )
            Draft202012Validator(schema).validate(committed.to_dict())

        documents = self.authoring.detached_documents()
        documents["score"]["tail_seconds"] = 3
        first_revised_authoring = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=documents,
        )
        documents = first_revised_authoring.detached_documents()
        documents["score"]["title"] = "Second post-amendment descendant"
        revised_authoring = save_authoring_project(
            self.root,
            expected_revision=first_revised_authoring.revision,
            documents=documents,
        )
        self.assertEqual(
            revised_authoring.revision_parent_revision,
            first_revised_authoring.revision,
        )
        self.assertGreater(
            revised_authoring.revision_first_save_sequence,
            fence["save_sequence"],
        )
        next_iteration = record_workflow_authoring_revision(
            self.root,
            workflow_id=committed.workflow_id,
            expected_revision=committed.revision,
            authoring_revision=revised_authoring.revision,
        )
        context = inspect_workflow_composition(
            self.root, workflow_id=next_iteration.workflow_id
        )
        self.assertEqual(
            context["effective_work_charter"]["ending_contract"],
            proposal["operations"][0]["value"],
        )
        self.assertIsNone(context["composition_map"])
        with self.assertRaises(CreativeWorkflowError):
            record_workflow_review(
                self.root,
                workflow_id=next_iteration.workflow_id,
                expected_revision=next_iteration.revision,
                phase="intent",
                reviewer="agent",
                perception_basis="report_only",
                summary="The amended work must rebuild its map first.",
            )

    def test_pre_authored_revision_cannot_be_hidden_by_restoring_anchor_content(
        self,
    ) -> None:
        proposal, preflight, pending = self._prepare_amendment()
        pre_authored_documents = self.authoring.detached_documents()
        pre_authored_documents["score"]["tail_seconds"] = 3
        pre_authored = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=pre_authored_documents,
        )
        restored = save_authoring_project(
            self.root,
            expected_revision=pre_authored.revision,
            documents=self.authoring.detached_documents(),
        )

        # Content IDs remain stable, but the internal publication event cannot
        # rewind to the event captured by the workflow anchor.
        self.assertEqual(restored.revision, self.authoring.revision)
        self.assertNotEqual(
            restored.save_event_sha256,
            self.authoring.save_event_sha256,
        )
        with self.assertRaises(CreativeWorkflowError) as caught:
            preflight_workflow_charter_amendment(
                self.root,
                workflow_id=pending.workflow_id,
                proposal=proposal,
            )
        self.assertEqual(
            caught.exception.code,
            "charter_amendment_must_precede_authoring_change",
        )
        with self.assertRaises(CreativeWorkflowError) as caught:
            commit_workflow_charter_amendment(
                self.root,
                workflow_id=pending.workflow_id,
                expected_revision=pending.revision,
                proposal=proposal,
                expected_preflight_sha256=preflight["preflight"][
                    "preflight_sha256"
                ],
                cost_acknowledgement=preflight[
                    "cost_acknowledgement_required_for_commit"
                ],
            )
        self.assertEqual(
            caught.exception.code,
            "charter_amendment_must_precede_authoring_change",
        )

    def test_record_revision_requires_current_head_after_causal_fence(self) -> None:
        proposal, preflight, pending = self._prepare_amendment()
        committed = commit_workflow_charter_amendment(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            proposal=proposal,
            expected_preflight_sha256=preflight["preflight"][
                "preflight_sha256"
            ],
            cost_acknowledgement=preflight[
                "cost_acknowledgement_required_for_commit"
            ],
        )
        first_documents = self.authoring.detached_documents()
        first_documents["score"]["tail_seconds"] = 3
        first = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=first_documents,
        )
        second_documents = first.detached_documents()
        second_documents["score"]["title"] = "Current causal head"
        save_authoring_project(
            self.root,
            expected_revision=first.revision,
            documents=second_documents,
        )

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_authoring_revision(
                self.root,
                workflow_id=committed.workflow_id,
                expected_revision=committed.revision,
                authoring_revision=first.revision,
            )
        self.assertEqual(
            caught.exception.code,
            "authoring_revision_not_current_head",
        )

    def test_pre_fence_revision_stays_ineligible_even_if_resaved_after_fence(
        self,
    ) -> None:
        pre_authored_documents = self.authoring.detached_documents()
        pre_authored_documents["score"]["tail_seconds"] = 3
        pre_authored = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=pre_authored_documents,
        )
        restored = save_authoring_project(
            self.root,
            expected_revision=pre_authored.revision,
            documents=self.authoring.detached_documents(),
        )
        created = create_creative_workflow(
            self.root,
            mode="iterate",
            final_authority="agent",
            composition_governance=True,
        )
        self.active = activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
        )
        self.authoring = restored
        proposal, preflight, pending = self._prepare_amendment()
        committed = commit_workflow_charter_amendment(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            proposal=proposal,
            expected_preflight_sha256=preflight["preflight"][
                "preflight_sha256"
            ],
            cost_acknowledgement=preflight[
                "cost_acknowledgement_required_for_commit"
            ],
        )
        resaved = save_authoring_project(
            self.root,
            expected_revision=restored.revision,
            documents=pre_authored_documents,
        )
        self.assertEqual(resaved.revision, pre_authored.revision)
        self.assertGreater(
            resaved.save_sequence,
            committed.state["governance"]["amendments"][0][
                "authoring_causal_fence"
            ]["save_sequence"],
        )

        with self.assertRaises(CreativeWorkflowError) as caught:
            record_workflow_authoring_revision(
                self.root,
                workflow_id=committed.workflow_id,
                expected_revision=committed.revision,
                authoring_revision=resaved.revision,
            )
        self.assertEqual(
            caught.exception.code,
            "authoring_revision_causality_invalid",
        )

    def test_legacy_amendment_wrapper_without_fence_remains_readable(self) -> None:
        proposal, preflight, pending = self._prepare_amendment()
        committed = commit_workflow_charter_amendment(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            proposal=proposal,
            expected_preflight_sha256=preflight["preflight"][
                "preflight_sha256"
            ],
            cost_acknowledgement=preflight[
                "cost_acknowledgement_required_for_commit"
            ],
        )
        legacy_state = committed.detached_state()
        legacy_state["governance"]["amendments"][0].pop(
            "authoring_causal_fence"
        )

        # State validation is the compatibility boundary used when opening an
        # immutable workflow history written before causal fences existed.
        workflow_module._validate_state_document(legacy_state)

    def test_amendment_is_rejected_after_score_authoring_changes(self) -> None:
        proposal, preflight, pending = self._prepare_amendment()
        documents = self.authoring.detached_documents()
        documents["score"]["title"] = "Changed before charter cost acknowledgement"
        save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=documents,
        )

        with self.assertRaises(CreativeWorkflowError) as caught:
            preflight_workflow_charter_amendment(
                self.root,
                workflow_id=pending.workflow_id,
                proposal=proposal,
            )
        self.assertEqual(
            caught.exception.code,
            "charter_amendment_must_precede_authoring_change",
        )

        with self.assertRaises(CreativeWorkflowError) as caught:
            commit_workflow_charter_amendment(
                self.root,
                workflow_id=pending.workflow_id,
                expected_revision=pending.revision,
                proposal=proposal,
                expected_preflight_sha256=preflight["preflight"][
                    "preflight_sha256"
                ],
                cost_acknowledgement=preflight[
                    "cost_acknowledgement_required_for_commit"
                ],
            )
        self.assertEqual(
            caught.exception.code,
            "charter_amendment_must_precede_authoring_change",
        )


if __name__ == "__main__":
    unittest.main()
