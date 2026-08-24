from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from tianlai.candidate import (
    canonical_json_sha256,
    prepare_candidate_target,
    publish_candidate_metadata,
    sha256_file,
)
from tianlai.render_profile import RenderProfile
from tianlai.score_ops import canonical_score_sha256


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _workflow_contract_validator(pointer: str) -> Draft202012Validator:
    workflow_schema = _schema("creative-workflow.schema.json")
    return Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": workflow_schema["$defs"],
            "$ref": pointer,
        }
    )


def _legacy_workflow_policy() -> dict:
    return {
        "hard_failures_may_block": True,
        "promise_conflicts_block_automatically": False,
        "aesthetic_risks_block_automatically": False,
        "automatic_score_changes": False,
        "automatic_audio_changes": False,
        "single_aesthetic_objective": False,
        "unresolved_candidates_preserved": True,
        "rollback_is_selection_not_overwrite": True,
        "acceptance_is_contextual_not_objective_quality": True,
    }


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "协议测试",
        "tempo_map": [
            {
                "bar": 1,
                "bpm": 120,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "lead",
                "notes": [
                    {
                        "event_id": "lead-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    }
                ],
            }
        ],
    }


class PublicProtocolSchemaTests(unittest.TestCase):
    def test_schemas_are_valid_draft_2020_12(self) -> None:
        for name in (
            "authoring-project-snapshot.schema.json",
            "authoring-roster.schema.json",
            "candidate.schema.json",
            "candidate-playback-map.schema.json",
            "creative-workflow.schema.json",
            "render-profile.schema.json",
            "score-patch.schema.json",
        ):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(_schema(name))

    def test_workflow_charter_settlement_schema_matches_runtime_bounds(self) -> None:
        validator = _workflow_contract_validator(
            "#/$defs/charterSettlementItem"
        )
        valid = {
            "target": "one_sentence_promise",
            "status": "kept",
            "rationale": "The established material fulfils the promise.",
            "basis_ids": ["evidence-" + "1" * 20],
            "event_ids": [],
        }
        validator.validate(valid)

        invalid_items = []
        for changes in (
            {"basis_ids": []},
            {
                "basis_ids": [
                    f"evidence-{index:020x}" for index in range(17)
                ]
            },
            {"basis_ids": ["not-a-workflow-basis-id"]},
            {
                "basis_ids": [
                    "evidence-" + "1" * 20,
                    "evidence-" + "1" * 20,
                ]
            },
            {"event_ids": [f"event-{index}" for index in range(33)]},
            {"event_ids": ["event-1", "event-1"]},
        ):
            item = dict(valid)
            item.update(changes)
            invalid_items.append(item)
        for item in invalid_items:
            with self.subTest(item=item):
                self.assertTrue(list(validator.iter_errors(item)))

    def test_workflow_fork_anchor_requires_events_or_complete_bar_range(self) -> None:
        validator = _workflow_contract_validator(
            "#/$defs/fork/properties/anchor"
        )
        base = {
            "authoring_revision": "1" * 64,
            "score_sha256": "2" * 64,
            "event_ids": ["event-1"],
            "part_ids": [],
            "start_bar": None,
            "start_beat": None,
            "end_bar": None,
            "end_beat": None,
        }
        validator.validate(base)
        validator.validate(
            {
                **base,
                "event_ids": [],
                "part_ids": ["lead"],
                "start_bar": 1,
                "start_beat": 1,
                "end_bar": 2,
                "end_beat": 1,
            }
        )

        invalid_anchors = [
            {**base, "event_ids": [], "part_ids": ["lead"]},
            {
                **base,
                "event_ids": [],
                "start_bar": 1,
                "start_beat": None,
                "end_bar": 2,
                "end_beat": 1,
            },
            {**base, "event_ids": ["event-1", "event-1"]},
            {
                **base,
                "event_ids": [f"event-{index}" for index in range(129)],
            },
            {**base, "part_ids": ["lead", "lead"]},
            {
                **base,
                "part_ids": [f"part-{index}" for index in range(65)],
            },
        ]
        for anchor in invalid_anchors:
            with self.subTest(anchor=anchor):
                self.assertTrue(list(validator.iter_errors(anchor)))

    def test_workflow_schema_accepts_compatible_policy_profiles(self) -> None:
        validator = _workflow_contract_validator(
            "#/$defs/state/properties/policy"
        )
        legacy = _legacy_workflow_policy()
        claim_lifecycle = {
            **legacy,
            "claim_lifecycle_profile": "explicit-v1",
        }
        acceptance_gate = {
            **claim_lifecycle,
            "acceptance_gate_profile": "recorded-hard-failure-recheck-v1",
        }
        charter_settlement = {
            **acceptance_gate,
            "charter_settlement_profile": "affirmative-promise-ledger-v1",
        }
        composition_governance = {
            **charter_settlement,
            "composition_governance_profile": (
                "whole-work-derivation-and-bounded-amendment-v1"
            ),
        }
        revision_contract = {
            **charter_settlement,
            "revision_contract_profile": (
                "bounded-change-and-explicit-challenger-settlement-v1"
            ),
        }
        governed_revision_contract = {
            **revision_contract,
            "composition_governance_profile": (
                "whole-work-derivation-and-bounded-amendment-v1"
            ),
        }
        for policy in (
            legacy,
            claim_lifecycle,
            acceptance_gate,
            charter_settlement,
            composition_governance,
            revision_contract,
            governed_revision_contract,
        ):
            with self.subTest(policy=policy):
                validator.validate(policy)

        for policy in (
            {
                **legacy,
                "acceptance_gate_profile": "recorded-hard-failure-recheck-v1",
            },
            {
                **claim_lifecycle,
                "charter_settlement_profile": "affirmative-promise-ledger-v1",
            },
            {
                **acceptance_gate,
                "revision_contract_profile": (
                    "bounded-change-and-explicit-challenger-settlement-v1"
                ),
            },
        ):
            with self.subTest(policy=policy):
                self.assertTrue(list(validator.iter_errors(policy)))

    def test_workflow_revision_scope_schema_requires_real_bounded_authority(self) -> None:
        validator = _workflow_contract_validator("#/$defs/revisionScope")
        bounded = {
            "change_scale": "bounded",
            "documents": ["score"],
            "allowed_document_paths": {"score": []},
            "score": {
                "part_ids": ["lead"],
                "event_ids": ["lead-1"],
                "bar_ranges": [{"start": 1, "end": 2}],
                "allowed_note_fields": ["pitch", "part_id"],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            },
            "whole_work_cost": None,
        }
        validator.validate(bounded)

        no_score_authority = json.loads(json.dumps(bounded))
        no_score_authority["score"]["allowed_note_fields"] = []
        no_score_authority["score"]["event_ids"] = []
        self.assertTrue(list(validator.iter_errors(no_score_authority)))

        score_not_declared = json.loads(json.dumps(bounded))
        score_not_declared["documents"] = ["render_profile"]
        score_not_declared["allowed_document_paths"] = {"score": ["/title"]}
        self.assertTrue(list(validator.iter_errors(score_not_declared)))

        invalid_pointer = json.loads(json.dumps(bounded))
        invalid_pointer["allowed_document_paths"] = {"score": ["title"]}
        self.assertTrue(list(validator.iter_errors(invalid_pointer)))

        note_pointer = json.loads(json.dumps(bounded))
        note_pointer["allowed_document_paths"] = {
            "score": ["/parts/0/notes/0/pitch"]
        }
        self.assertTrue(list(validator.iter_errors(note_pointer)))

        reversed_range = json.loads(json.dumps(bounded))
        reversed_range["score"]["bar_ranges"] = [{"start": 3, "end": 2}]
        # JSON Schema documents the cross-field relation; semantic core and
        # MCP typed validation enforce it.
        validator.validate(reversed_range)

        whole_work = json.loads(json.dumps(bounded))
        whole_work["change_scale"] = "whole_work"
        whole_work["allowed_document_paths"] = None
        whole_work["score"] = None
        whole_work["whole_work_cost"] = {
            "accepted_costs": [
                "expanded_change_surface",
                "downstream_compatibility_rework",
                "increased_topic_drift_risk",
            ],
            "rationale": "The broad rewrite cost is accepted before editing.",
        }
        validator.validate(whole_work)
        whole_work["whole_work_cost"]["accepted_costs"].pop()
        self.assertTrue(list(validator.iter_errors(whole_work)))

    def test_workflow_prior_revision_assessment_schema_is_non_aesthetic(self) -> None:
        validator = _workflow_contract_validator(
            "#/$defs/priorRevisionAssessment"
        )
        validator.validate(
            {
                "contract_sha256": "1" * 64,
                "outcome": "retain_baseline",
                "rationale": "The declared change was not established by the cited review.",
                "basis_ids": ["review-" + "2" * 20],
            }
        )
        invalid = {
            "contract_sha256": "1" * 64,
            "outcome": "sounds_better",
            "rationale": "A machine score cannot establish this.",
            "basis_ids": ["review-" + "2" * 20],
        }
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_render_profile_schema_accepts_public_default(self) -> None:
        validator = Draft202012Validator(_schema("render-profile.schema.json"))
        validator.validate(RenderProfile().to_dict())
        validator.validate({})
        self.assertTrue(
            list(validator.iter_errors({"write_stems": "yes"}))
        )

    def test_score_patch_schema_accepts_engine_patch_contract(self) -> None:
        score = _score()
        patch = {
            "kind": "tianlai.score_patch",
            "schema_version": 1,
            "base_score_sha256": canonical_score_sha256(score),
            "operations": [
                {
                    "op": "update_note",
                    "event_id": "lead-1",
                    "expect": {"pitch": "C4", "voice": None},
                    "changes": {"pitch": "D4", "voice": "1"},
                },
                {
                    "op": "add_note",
                    "part_id": "lead",
                    "note": {
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 1,
                        "pitch": "E4",
                        "staff": 1,
                    },
                },
            ],
        }
        validator = Draft202012Validator(_schema("score-patch.schema.json"))
        validator.validate(patch)
        invalid = json.loads(json.dumps(patch))
        invalid["operations"][1]["note"]["event_id"] = "caller-owned"
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_candidate_schema_accepts_published_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            score = _score()
            roster = {
                "assignments": [
                    {
                        "part": "lead",
                        "instrument": "测试工具/参考振荡器",
                    }
                ]
            }
            profile = RenderProfile().to_dict()
            target = prepare_candidate_target(
                Path(temporary),
                score["title"],
                output_id="protocol",
            )
            target.directory.mkdir(parents=True)
            receipt = target.directory / "渲染回执.json"
            receipt.write_text("{}\n", encoding="utf-8")
            manifest = publish_candidate_metadata(
                target,
                title=score["title"],
                score=score,
                roster=roster,
                render_profile=profile,
                receipt_path=receipt,
                plan_sha256=canonical_json_sha256({"parts": []}),
            )

            Draft202012Validator(
                _schema("candidate.schema.json"),
                format_checker=FormatChecker(),
            ).validate(manifest)

    def test_candidate_schema_accepts_v2_authoring_and_workflow_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            score = _score()
            roster = {
                "assignments": [
                    {
                        "part": "lead",
                        "instrument": "测试工具/参考振荡器",
                    }
                ]
            }
            authoring_roster = {
                "kind": "tianlai.authoring_roster",
                "schema_version": 1,
                "assignments": [
                    {
                        "part": "lead",
                        "instrument": "测试工具/参考振荡器",
                    }
                ],
            }
            project_id = "1" * 32
            roster_hash = canonical_json_sha256(authoring_roster)
            profile = RenderProfile().to_dict()
            revision = canonical_json_sha256(
                {
                    "kind": "tianlai.authoring_revision_binding",
                    "schema_version": 1,
                    "project_id": project_id,
                    "documents": {
                        "score": canonical_json_sha256(score),
                        "authoring_roster": roster_hash,
                        "render_profile": canonical_json_sha256(profile),
                    },
                }
            )
            target = prepare_candidate_target(
                Path(temporary), score["title"], output_id="authoring-protocol"
            )
            target.directory.mkdir(parents=True)
            workflow_authorization = {
                "workflow_id": "2" * 32,
                "project_id": project_id,
                "reservation_revision": "3" * 64,
                "iteration_number": 1,
                "operation_id": "4" * 32,
                "authoring_revision": revision,
                "candidate_work_id": target.work_id,
                "candidate_id": target.candidate_id,
                "parent_work_id": None,
                "parent_candidate_id": None,
                "parent_manifest_sha256": None,
            }
            plan = {
                "roster": "未命名编制",
                "parts": [
                    {
                        "executor_id": "lead",
                        "part_id": "lead",
                        "instrument": "测试工具/参考振荡器",
                        "gain_db": 0.0,
                        "pan": 0.0,
                        "seat": {
                            "azimuth_deg": 0.0,
                            "distance_m": 3.0,
                        },
                        "transpose": 0,
                        "duration_scale": 1.0,
                        "dynamic_compression": 0.0,
                        "articulation_auto": True,
                        "articulation_map": {},
                        "kit_pitch": None,
                    }
                ],
            }
            plan_path = target.directory / "演奏计划.json"
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            plan_sha256 = canonical_json_sha256(plan)
            receipt = target.directory / "渲染回执.json"
            receipt.write_text(
                json.dumps(
                    {
                        "authoring_project": {
                            "project_id": project_id,
                            "revision": revision,
                            "authoring_roster_canonical_sha256": roster_hash,
                        },
                        "authoring_workflow": workflow_authorization,
                        "performance_plan": {
                            "path": plan_path.name,
                            "file_sha256": sha256_file(plan_path),
                            "sha256": plan_sha256,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = publish_candidate_metadata(
                target,
                title=score["title"],
                score=score,
                roster=roster,
                render_profile=profile,
                receipt_path=receipt,
                plan_sha256=plan_sha256,
                authoring_project={
                    "project_id": project_id,
                    "revision": revision,
                    "authoring_roster": authoring_roster,
                },
                authoring_workflow=workflow_authorization,
            )

            Draft202012Validator(
                _schema("candidate.schema.json"),
                format_checker=FormatChecker(),
            ).validate(manifest)

    def test_timestamp_schema_patterns_reject_offset_and_controls(self) -> None:
        candidate_schema = _schema("candidate.schema.json")
        timestamp = candidate_schema["allOf"][0]["then"]["properties"][
            "created_at_utc"
        ]["pattern"]
        import re

        self.assertIsNotNone(re.fullmatch(timestamp, "2026-08-09T12:34:56.000Z"))
        self.assertIsNone(re.fullmatch(timestamp, "2026-08-09T12:34:56+00:00"))
        self.assertIsNone(re.fullmatch(timestamp, "2026-08-09T12:34:56.000Z\n"))


if __name__ == "__main__":
    unittest.main()
