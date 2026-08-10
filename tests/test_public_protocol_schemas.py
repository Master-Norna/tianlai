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
            "render-profile.schema.json",
            "score-patch.schema.json",
        ):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(_schema(name))

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
