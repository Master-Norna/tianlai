"""Contract tests for decisive passage-level workflow derivation records.

A derivation closes a materially live alternative at an identity, formal,
climax, or ending hinge. Reversible qiyun details do not require one. These
tests pin the referential-integrity rules (score events and parts verified at
record time), the mandatory excluded-alternatives gate, the non-blocking policy
and the backward-compatible shapes for pre-derivation-contract revisions.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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
    open_creative_workflow,
    record_workflow_authoring_revision,
    record_workflow_derivation,
    record_workflow_evidence,
    record_workflow_review,
    terminate_creative_workflow,
    verify_creative_workflow_history,
)


ROOT = Path(__file__).resolve().parents[1]


def _charter() -> dict[str, object]:
    return {
        "title": "Derivation trial",
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


def _constitution() -> dict[str, object]:
    return {
        "document_id": "tianlai-music-constitution",
        "version": "0.2",
        "language": "zh-CN",
        "content_sha256": "0" * 64,
    }


def _active_clauses() -> list[dict[str, object]]:
    return [
        {
            "clause_id": "C0.04",
            "role": "review_lens",
            "rationale": "The ear keeps final authority in this trial.",
            "interpretation": "Metrics argue; listening decides.",
        }
    ]


def _error_code(call) -> str:
    with unittest.TestCase().assertRaises(CreativeWorkflowError) as captured:
        call()
    return captured.exception.code


def _legacy_decision(**arguments):
    """Return the pre-Claim-Lifecycle decision shape for legacy-state tests."""

    decision = workflow_module._decision_record(**arguments)
    decision.pop("review_ids")
    decision.pop("evidence_dispositions")
    decision.pop("charter_settlement")
    return decision


def _material_premise(event_ids=("event-1",)) -> dict[str, object]:
    return {
        "kind": "established_material",
        "reference": None,
        "event_ids": list(event_ids),
        "artifact_sha256": None,
        "artifact_role": None,
    }


def _promise_premise(reference="one_sentence_promise") -> dict[str, object]:
    return {
        "kind": "declared_promise",
        "reference": reference,
        "event_ids": [],
        "artifact_sha256": None,
        "artifact_role": None,
    }


def _clause_premise(reference="C0.04") -> dict[str, object]:
    return {
        "kind": "active_clause",
        "reference": reference,
        "event_ids": [],
        "artifact_sha256": None,
        "artifact_role": None,
    }


def _alternatives(
    premise_indexes: tuple[int, ...] = (0,),
) -> list[dict[str, object]]:
    return [
        {
            "alternative": "Repeat the motive at the original register.",
            "failure": "It would spend the climax privilege promised to the return.",
            "premise_indexes": list(premise_indexes),
        }
    ]


def _reseal_derivation_identity(
    state: dict[str, object], *, iteration_index: int = 0
) -> None:
    iteration = state["iterations"][iteration_index]
    derivation = iteration["derivations"][0]
    body = {
        key: value for key, value in derivation.items() if key != "derivation_id"
    }
    derivation["derivation_id"] = "derivation-" + (
        workflow_module.canonical_json_sha256(
            {
                "workflow_id": state["workflow_id"],
                "iteration_number": iteration["iteration_number"],
                **body,
            }
        )[:20]
    )


def _forge_revision_directory(layout, state: dict[str, object]) -> tuple[str, Path]:
    """Write a hash-consistent revision while bypassing semantic publication.

    This models a migration/recovery bug (or a same-authority re-seal), then
    lets the ordinary revision reader prove that semantic bindings are checked
    independently of the directory and manifest hashes.
    """

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


def _fake_candidate_anchor(
    authoring_revision: str, *, verified_at_utc: str
) -> dict[str, object]:
    return {
        "candidate_id": "candidate-lineage",
        "work_id": "work-lineage",
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


class WorkflowDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "推导 空间"
        state = create_authoring_project(self.root, title="Derivation Test")
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
        score["parts"].append(
            {
                "id": "part-2",
                "name": "声部 2",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "event_id": "event-3",
                        "bar": 2,
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
            self.root, expected_revision=state.revision, documents=documents
        )

    def activate(self, *, budget: dict[str, int] | None = None):
        created = create_creative_workflow(
            self.root,
            mode="iterate",
            final_authority="agent",
            budget=budget,
        )
        return activate_creative_workflow(
            self.root,
            workflow_id=created.workflow_id,
            expected_revision=created.revision,
            work_charter=_charter(),
            constitution=_constitution(),
            active_clauses=_active_clauses(),
        )

    def record(self, snapshot, **overrides):
        arguments = {
            "claim": "The return must land an octave lower than the opening.",
            "premises": [_material_premise()],
            "excluded_alternatives": _alternatives(),
            "event_ids": ["event-2"],
        }
        arguments.update(overrides)
        return record_workflow_derivation(
            self.root,
            workflow_id=snapshot.workflow_id,
            expected_revision=snapshot.revision,
            **arguments,
        )

    def _record(self, snapshot, **overrides):
        result = self.record(snapshot, **overrides)
        derivations = result.state["iterations"][-1]["derivations"]
        self.assertEqual(len(derivations), 1)
        return derivations[0]

    def open_revised_iteration(self):
        active = self.activate()
        reviewed = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="The revision target is bounded and audible.",
        )
        evidence = record_workflow_evidence(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            category="aesthetic_risk",
            code="structure.return_register",
            basis_kind="diagnostic_hypothesis",
            basis_reference="symbolic lineage test",
            reporter="agent",
            perception_basis="report_only",
            summary="The return may need a lower register.",
            observation="The current return repeats the opening register.",
            interpretation="A bounded revision can test stronger consequence.",
            confidence="medium",
        )
        evidence_id = evidence.state["iterations"][-1]["evidence"][0][
            "evidence_id"
        ]
        review_id = evidence.state["iterations"][-1]["reviews"][0][
            "review_id"
        ]
        pending = decide_workflow_iteration(
            self.root,
            workflow_id=evidence.workflow_id,
            expected_revision=evidence.revision,
            disposition="revise",
            summary="Test one bounded revision.",
            rationale="The next authoring revision must be named exactly.",
            final_authority="agent",
            perception_basis="report_only",
            evidence_ids=[evidence_id],
            review_ids=[review_id],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "revision_target",
                    "rationale": "This is the bounded claim the child revision tests.",
                    "basis_ids": [review_id],
                }
            ],
            expected_audible_change="The return moves to a lower register.",
            revision_scope=_score_metadata_revision_scope(),
            withdrawal_condition="Withdraw if the change exceeds score metadata.",
        )
        documents = self.authoring.detached_documents()
        documents["score"]["tail_seconds"] = 2.25
        child = save_authoring_project(
            self.root,
            expected_revision=self.authoring.revision,
            documents=documents,
        )
        next_iteration = record_workflow_authoring_revision(
            self.root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            authoring_revision=child.revision,
        )
        return next_iteration, child

    def test_records_passage_derivation_with_verified_referents(self) -> None:
        active = self.activate()
        result = self.record(
            active,
            premises=[
                _material_premise(),
                _promise_premise(),
                _clause_premise(),
            ],
            clause_ids=["C0.04"],
            sacrificed_values=["surface brightness"],
        )
        derivations = result.state["iterations"][-1]["derivations"]
        self.assertEqual(len(derivations), 1)
        record = derivations[0]
        self.assertRegex(record["derivation_id"], r"^derivation-[0-9a-f]{20}$")
        anchor = record["anchor"]
        self.assertEqual(
            anchor["authoring_revision"], self.authoring.revision
        )
        self.assertRegex(anchor["score_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(anchor["event_ids"], ["event-2"])
        self.assertIsNone(anchor["candidate_id"])
        self.assertEqual(
            [premise["kind"] for premise in record["premises"]],
            ["established_material", "declared_promise", "active_clause"],
        )
        self.assertEqual(len(record["excluded_alternatives"]), 1)
        self.assertEqual(
            record["excluded_alternatives"][0]["premise_indexes"], [0]
        )
        self.assertEqual(result.state["usage"]["derivations"], 1)

    def test_records_derivation_anchored_by_bar_range(self) -> None:
        active = self.activate()
        record = self._record(
            active,
            event_ids=[],
            part_ids=["part-1"],
            start_bar=2,
            start_beat=1.0,
            end_bar=2,
            end_beat=2.0,
        )
        self.assertEqual(
            {
                field: record["anchor"][field]
                for field in ("start_bar", "start_beat", "end_bar", "end_beat")
            },
            {
                "start_bar": 2,
                "start_beat": 1.0,
                "end_bar": 2,
                "end_beat": 2.0,
            },
        )
        self.assertEqual(record["anchor"]["part_ids"], ["part-1"])
        self.assertEqual(record["anchor"]["event_ids"], [])

    def test_rejects_part_only_anchor(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(active, event_ids=[], part_ids=["part-1"])
            ),
            "derivation_anchor_requires_event_or_bar_range",
        )

    def test_rejects_incomplete_or_reversed_bar_range(self) -> None:
        active = self.activate()
        cases = (
            {"start_bar": 2},
            {
                "start_bar": 2,
                "start_beat": 1.0,
                "end_bar": 2,
            },
            {
                "start_bar": 2,
                "start_beat": 2.0,
                "end_bar": 2,
                "end_beat": 1.0,
            },
            {
                "start_bar": 2,
                "start_beat": 1.0,
                "end_bar": 2,
                "end_beat": 1.0,
            },
        )
        for scope in cases:
            with self.subTest(scope=scope):
                self.assertEqual(
                    _error_code(
                        lambda scope=scope: self.record(
                            active, event_ids=[], **scope
                        )
                    ),
                    "invalid_derivation_bar_range",
                )

    def test_rejects_unknown_event_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(lambda: self.record(active, event_ids=["event-999"])),
            "derivation_event_not_found",
        )

    def test_rejects_unknown_material_event_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active,
                    event_ids=["event-2"],
                    premises=[_material_premise(("event-999",))],
                )
            ),
            "derivation_material_event_not_found",
        )

    def test_rejects_unknown_part_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active, event_ids=["event-2"], part_ids=["part-x"]
                )
            ),
            "derivation_part_not_found",
        )

    def test_rejects_event_outside_declared_parts(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active, event_ids=["event-2"], part_ids=["part-2"]
                )
            ),
            "derivation_event_part_mismatch",
        )

    def test_rejects_empty_anchor(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(lambda: self.record(active, event_ids=[])),
            "derivation_anchor_requires_event_or_bar_range",
        )

    def test_rejects_same_or_future_established_material(self) -> None:
        active = self.activate()
        cases = (
            (["event-2"], ("event-2",)),
            (["event-1"], ("event-2",)),
        )
        for event_ids, premise_event_ids in cases:
            with self.subTest(
                event_ids=event_ids, premise_event_ids=premise_event_ids
            ):
                self.assertEqual(
                    _error_code(
                        lambda event_ids=event_ids, premise_event_ids=premise_event_ids: self.record(
                            active,
                            event_ids=event_ids,
                            premises=[_material_premise(premise_event_ids)],
                        )
                    ),
                    "derivation_material_not_preceding_anchor",
                )

    def test_rejects_missing_excluded_alternatives(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(active, excluded_alternatives=[])
            ),
            "derivation_alternatives_required",
        )

    def test_rejects_alternative_without_premise_indexes(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active,
                    excluded_alternatives=[
                        {
                            "alternative": "Repeat the motive unchanged.",
                            "failure": "It spends the promised return too early.",
                        }
                    ],
                )
            ),
            "invalid_derivation_alternative",
        )

    def test_rejects_empty_unknown_or_duplicate_alternative_premise_indexes(
        self,
    ) -> None:
        active = self.activate()
        cases = (
            ((), "derivation_alternative_premise_required"),
            ((1,), "derivation_alternative_premise_not_found"),
            ((0, 0), "duplicate_derivation_alternative_premise_reference"),
        )
        for indexes, code in cases:
            with self.subTest(indexes=indexes):
                self.assertEqual(
                    _error_code(
                        lambda indexes=indexes: self.record(
                            active,
                            excluded_alternatives=_alternatives(indexes),
                        )
                    ),
                    code,
                )

    def test_rejects_missing_premises(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(lambda: self.record(active, premises=[])),
            "derivation_premise_required",
        )

    def test_rejects_inactive_clause_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(lambda: self.record(active, clause_ids=["C0.10"])),
            "derivation_clause_not_active",
        )

    def test_rejects_invalid_promise_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active, premises=[_promise_premise("no_such_field")]
                )
            ),
            "derivation_promise_reference_invalid",
        )

    def test_rejects_inactive_clause_premise(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    active, premises=[_clause_premise("C0.10")]
                )
            ),
            "derivation_clause_not_active",
        )

    def test_rejects_measurement_premise_without_candidate(self) -> None:
        active = self.activate()
        premise = {
            "kind": "render_measurement",
            "reference": "post-render true peak",
            "event_ids": [],
            "artifact_sha256": "1" * 64,
            "artifact_role": "post_render_check",
        }
        self.assertEqual(
            _error_code(lambda: self.record(active, premises=[premise])),
            "derivation_artifact_requires_candidate",
        )

    def test_duplicate_derivation_is_rejected(self) -> None:
        active = self.activate()
        frozen = "2026-08-18T12:00:00.000Z"
        with mock.patch.object(workflow_module, "_now", return_value=frozen):
            first = self.record(active)
        with mock.patch.object(workflow_module, "_now", return_value=frozen):
            self.assertEqual(
                _error_code(lambda: self.record(first)),
                "duplicate_derivation_record",
            )

    def test_budget_zero_opts_out_of_derivations(self) -> None:
        active = self.activate(
            budget={"max_derivations_per_iteration": 0}
        )
        self.assertEqual(
            _error_code(lambda: self.record(active)),
            "derivation_budget_exhausted",
        )

    def test_budget_limits_derivations_per_iteration(self) -> None:
        active = self.activate(
            budget={"max_derivations_per_iteration": 1}
        )
        first = self.record(active)
        self.assertEqual(
            _error_code(
                lambda: self.record(
                    first,
                    event_ids=["event-1"],
                    premises=[_promise_premise()],
                    claim="The opening must remain singular before its return.",
                )
            ),
            "derivation_budget_exhausted",
        )

    def test_derivations_require_reviewing_status(self) -> None:
        created = create_creative_workflow(
            self.root, mode="iterate", final_authority="agent"
        )
        self.assertEqual(
            _error_code(lambda: self.record(created)),
            "illegal_workflow_transition",
        )

    def test_decision_references_derivations(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        derivation_id = recorded.state["iterations"][-1]["derivations"][0][
            "derivation_id"
        ]
        for phase in ("intent", "symbolic_structure", "orchestration_performance"):
            recorded = record_workflow_review(
                self.root,
                workflow_id=recorded.workflow_id,
                expected_revision=recorded.revision,
                phase=phase,
                reviewer="agent",
                perception_basis="report_only",
                summary=f"reviewed {phase}",
            )
        decided = decide_workflow_iteration(
            self.root,
            workflow_id=recorded.workflow_id,
            expected_revision=recorded.revision,
            disposition="stop",
            summary="Stop with the derivation preserved.",
            rationale="The trial is complete; the necessity claim stays on record.",
            final_authority="agent",
            perception_basis="report_only",
            derivation_ids=[derivation_id],
        )
        decision = decided.state["iterations"][-1]["decision"]
        self.assertEqual(decision["derivation_ids"], [derivation_id])

    def test_decision_rejects_unknown_derivation_reference(self) -> None:
        active = self.activate()
        self.assertEqual(
            _error_code(
                lambda: decide_workflow_iteration(
                    self.root,
                    workflow_id=active.workflow_id,
                    expected_revision=active.revision,
                    disposition="stop",
                    summary="Stop.",
                    rationale="Stopping the trial.",
                    final_authority="agent",
                    perception_basis="report_only",
                    derivation_ids=["derivation-" + "0" * 20],
                )
            ),
            "decision_derivation_not_found",
        )

    def test_derivation_identity_rejects_body_drift(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        base = recorded.detached_state()
        mutations = (
            lambda derivation: derivation.__setitem__(
                "claim", "A different claim under the old identity."
            ),
            lambda derivation: derivation["excluded_alternatives"][0].__setitem__(
                "failure", "A different failure under the old identity."
            ),
        )
        for mutate in mutations:
            state = copy.deepcopy(base)
            mutate(state["iterations"][0]["derivations"][0])
            with self.subTest(mutate=mutate):
                self.assertEqual(
                    _error_code(lambda state=state: _validate_state_document(state)),
                    "derivation_identity_mismatch",
                )

    def test_publish_rejects_derivation_score_hash_drift(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        state = recorded.detached_state()
        state["iterations"][0]["derivations"][0]["anchor"][
            "score_sha256"
        ] = "f" * 64
        _reseal_derivation_identity(state)
        layout = workflow_module._existing_layout(self.root, recorded.workflow_id)
        self.assertEqual(
            _error_code(lambda: workflow_module._publish_revision(layout, state)),
            "derivation_score_hash_mismatch",
        )

    def test_revision_reader_rejects_resealed_score_hash_drift(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        state = recorded.detached_state()
        state["iterations"][0]["derivations"][0]["anchor"][
            "score_sha256"
        ] = "e" * 64
        _reseal_derivation_identity(state)
        layout = workflow_module._existing_layout(self.root, recorded.workflow_id)
        revision, directory = _forge_revision_directory(layout, state)
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
        self.assertEqual(
            _error_code(
                lambda: open_creative_workflow(
                    self.root, workflow_id=recorded.workflow_id
                )
            ),
            "derivation_score_hash_mismatch",
        )

    def test_first_iteration_anchor_must_match_initial_revision(self) -> None:
        active = self.activate()
        state = active.detached_state()
        state["iterations"][0]["anchor"]["authoring_revision"] = "f" * 64
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_iteration_lineage_mismatch",
        )

    def test_revised_iteration_anchor_must_match_declared_next_revision(
        self,
    ) -> None:
        next_iteration, _child = self.open_revised_iteration()
        state = next_iteration.detached_state()
        state["iterations"][1]["anchor"][
            "authoring_revision"
        ] = self.authoring.revision
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_iteration_lineage_mismatch",
        )

    def test_revised_iteration_cannot_reuse_unchanged_authoring_revision(
        self,
    ) -> None:
        next_iteration, _child = self.open_revised_iteration()
        state = next_iteration.detached_state()
        state["iterations"][0][
            "next_authoring_revision"
        ] = self.authoring.revision
        state["iterations"][1]["anchor"][
            "authoring_revision"
        ] = self.authoring.revision
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_iteration_lineage_mismatch",
        )

    def test_publish_rejects_unavailable_revised_authoring_revision(self) -> None:
        next_iteration, _child = self.open_revised_iteration()
        state = next_iteration.detached_state()
        state["policy"] = copy.deepcopy(workflow_module._SETTLEMENT_POLICY)
        state["iterations"][0]["decision"].pop("revision_contract")
        unavailable = "f" * 64
        state["iterations"][0]["next_authoring_revision"] = unavailable
        state["iterations"][1]["anchor"]["authoring_revision"] = unavailable
        layout = workflow_module._existing_layout(
            self.root, next_iteration.workflow_id
        )
        self.assertEqual(
            _error_code(lambda: workflow_module._publish_revision(layout, state)),
            "workflow_authoring_revision_unavailable",
        )

    def test_revised_iteration_may_later_bind_its_own_candidate(self) -> None:
        next_iteration, child = self.open_revised_iteration()
        state = next_iteration.detached_state()
        current = state["iterations"][-1]
        current["anchor"]["candidate"] = {
            "candidate_id": "candidate-child-generation",
            "work_id": "work-child-generation",
            "authoring_revision": child.revision,
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
            "verified_at_utc": state["updated_at_utc"],
        }
        _validate_state_document(state)

    def test_revised_outcome_requires_matching_decision(self) -> None:
        next_iteration, _child = self.open_revised_iteration()
        state = next_iteration.detached_state()
        state["iterations"][0]["decision"] = None
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "iteration_outcome_decision_mismatch",
        )

    def test_direct_termination_exception_does_not_hide_forged_decision(
        self,
    ) -> None:
        active = self.activate()
        reviewed = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="Review before an explicit workflow termination.",
        )
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=reviewed.workflow_id,
            expected_revision=reviewed.revision,
            reason="cancelled",
            summary="Stop this workflow without deciding the iteration.",
            final_authority="agent",
            perception_basis="report_only",
        )
        state = stopped.detached_state()
        # This test forges the pre-Claim-Lifecycle decision dialect.  Keep the
        # containing revision on the matching legacy policy tier so the newer
        # settlement contract cannot mask the intended outcome mismatch.
        state["policy"] = copy.deepcopy(workflow_module._LEGACY_POLICY)
        iteration = state["iterations"][-1]
        iteration["decision"] = _legacy_decision(
            disposition="accept",
            summary="Forged acceptance under a direct termination.",
            rationale="The direct-termination exception must not hide this.",
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=(),
            exception_ids=(),
            expected_audible_change=None,
            final_authority="agent",
            perception_basis="report_only",
            timestamp=iteration["closed_at_utc"],
        )
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "iteration_outcome_decision_mismatch",
        )

    def test_open_iteration_cannot_carry_a_forged_decision(self) -> None:
        active = self.activate()
        reviewed = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="Review while the iteration remains open.",
        )
        state = reviewed.detached_state()
        iteration = state["iterations"][-1]
        iteration["decision"] = _legacy_decision(
            disposition="accept",
            summary="Forged decision on an open iteration.",
            rationale="An open iteration cannot already carry a decision.",
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=(),
            exception_ids=(),
            expected_audible_change=None,
            final_authority="agent",
            perception_basis="report_only",
            timestamp=state["updated_at_utc"],
        )
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "invalid_iteration_decision_state",
        )

    def test_direct_termination_exception_requires_stopped_workflow(self) -> None:
        active = self.activate()
        stopped = terminate_creative_workflow(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            reason="cancelled",
            summary="A direct termination always stops the workflow.",
            final_authority="agent",
            perception_basis="report_only",
        )
        state = stopped.detached_state()
        state["status"] = "completed"
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "iteration_outcome_decision_mismatch",
        )

    def test_rollback_lineage_cannot_select_the_iteration_being_closed(
        self,
    ) -> None:
        active = self.activate()
        reviewed = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="A rollback must select a strictly earlier candidate.",
        )
        state = reviewed.detached_state()
        timestamp = state["updated_at_utc"]
        iteration = state["iterations"][0]
        candidate = _fake_candidate_anchor(
            iteration["anchor"]["authoring_revision"],
            verified_at_utc=timestamp,
        )
        iteration["anchor"]["candidate"] = candidate
        iteration["decision"] = _legacy_decision(
            disposition="rollback",
            summary="Pretend to roll back in place.",
            rationale="This re-sealed state must not count as a rollback.",
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=(),
            exception_ids=(),
            expected_audible_change=None,
            final_authority="agent",
            perception_basis="report_only",
            timestamp=timestamp,
        )
        iteration["status"] = "closed"
        iteration["closed_at_utc"] = timestamp
        iteration["outcome"] = "rolled_back"
        state["iterations"].append(
            workflow_module._new_iteration(
                2,
                authoring_revision=iteration["anchor"]["authoring_revision"],
                parent_candidate=None,
                candidate=copy.deepcopy(candidate),
                opened_at_utc=timestamp,
            )
        )
        state["status"] = "reviewing"
        workflow_module._refresh_usage(state)
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_iteration_lineage_mismatch",
        )

    def test_resealed_derivations_cannot_exceed_iteration_budget(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        state = recorded.detached_state()
        state["budget"]["max_derivations_per_iteration"] = 0
        self.assertEqual(
            _error_code(lambda: _validate_state_document(state)),
            "workflow_budget_exceeded",
        )

    def test_history_rejects_budget_increase_after_creation(self) -> None:
        active = self.activate(budget={"max_derivations_per_iteration": 1})
        state = active.detached_state()
        state["budget"]["max_derivations_per_iteration"] = 64
        state["parent_revision"] = active.revision
        state["sequence"] += 1
        layout = workflow_module._existing_layout(self.root, active.workflow_id)
        raised_revision = workflow_module._publish_revision(layout, state)
        workflow_module._replace_manifest(
            layout,
            workflow_module._manifest_document(
                workflow_id=state["workflow_id"],
                project_id=state["project_id"],
                created_at_utc=state["created_at_utc"],
                updated_at_utc=state["updated_at_utc"],
                revision=raised_revision,
                sequence=state["sequence"],
            ),
        )
        self.assertEqual(
            _error_code(
                lambda: verify_creative_workflow_history(
                    self.root, workflow_id=active.workflow_id
                )
            ),
            "workflow_history_budget_mismatch",
        )

    def test_legacy_state_shapes_still_validate(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        state = copy.deepcopy(recorded.detached_state())
        # Strip every additive derivation field to model a legacy revision.
        del state["budget"]["max_derivations_per_iteration"]
        del state["usage"]["derivations"]
        iteration = state["iterations"][-1]
        del iteration["derivations"]
        _validate_state_document(state)

    def test_legacy_transition_publishes_fully_closed_new_shape(self) -> None:
        active = self.activate()
        state = active.detached_state()
        del state["budget"]["max_derivations_per_iteration"]
        del state["usage"]["derivations"]
        del state["iterations"][0]["derivations"]

        layout = workflow_module._existing_layout(self.root, active.workflow_id)
        legacy_revision = workflow_module._publish_revision(layout, state)
        workflow_module._replace_manifest(
            layout,
            workflow_module._manifest_document(
                workflow_id=state["workflow_id"],
                project_id=state["project_id"],
                created_at_utc=state["created_at_utc"],
                updated_at_utc=state["updated_at_utc"],
                revision=legacy_revision,
                sequence=state["sequence"],
            ),
        )
        opened = open_creative_workflow(
            self.root, workflow_id=active.workflow_id
        )
        self.assertNotIn(
            "max_derivations_per_iteration", opened.state["budget"]
        )

        transitioned = record_workflow_review(
            self.root,
            workflow_id=active.workflow_id,
            expected_revision=legacy_revision,
            phase="intent",
            reviewer="agent",
            perception_basis="report_only",
            summary="A legacy workflow can continue without losing history.",
        )
        closed = transitioned.detached_state()
        self.assertEqual(closed["budget"]["max_derivations_per_iteration"], 8)
        self.assertEqual(closed["usage"]["derivations"], 0)
        self.assertEqual(closed["iterations"][0]["derivations"], [])
        self.assertTrue(
            verify_creative_workflow_history(
                self.root, workflow_id=active.workflow_id
            )["complete"]
        )

    def test_snapshot_round_trip_carries_derivation(self) -> None:
        active = self.activate()
        recorded = self.record(active)
        payload = json.loads(json.dumps(recorded.to_dict(), ensure_ascii=False))
        derivations = payload["state"]["iterations"][-1]["derivations"]
        self.assertEqual(len(derivations), 1)
        self.assertIn("record_derivation", payload["allowed_actions"])


if __name__ == "__main__":
    unittest.main()
