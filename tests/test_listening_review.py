"""人工听审批次、响应、Hash 失效与汇总规则的回归测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import zipfile

from jsonschema import Draft202012Validator

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "人工听审.py"
SCHEMA_PATHS = {
    "batch": ROOT / "schemas" / "listening-review-batch.schema.json",
    "response": ROOT / "schemas" / "listening-review-response.schema.json",
    "assets": ROOT / "schemas" / "listening-review-assets.schema.json",
    "summary": ROOT / "schemas" / "listening-review-summary.schema.json",
}


def _load_tool():
    name = "tianlai_test_listening_review"
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ListeningReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        (self.project / "乐器").mkdir()
        (self.project / "examples").mkdir()
        (self.project / "output" / "试听").mkdir(parents=True)
        self.instrument_paths: list[str] = []
        self.wav_paths: dict[str, Path] = {}

        families = [
            "键盘乐器",
            "环境与拟音",
            "管弦乐/弦乐组",
        ]
        for index in range(12):
            relative = f"{families[index % len(families)]}/测试乐器{index:02d}"
            self._make_instrument(relative, index)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_instrument(self, relative: str, index: int) -> None:
        directory = self.project / "乐器" / Path(relative)
        directory.mkdir(parents=True)
        manifest = directory / "乐器.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": f"{directory.name} candidate",
                    "type": "modeled_instrument",
                    "quality_tier": "candidate",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        events = self.project / "examples" / f"{directory.name}_奏法.events.json"
        events.write_text(
            json.dumps({"events": [], "index": index}, ensure_ascii=False),
            encoding="utf-8",
        )
        wav = self.project / "output" / "试听" / Path(relative).with_suffix(".wav")
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(f"test wav {index:02d}".encode("ascii"))
        report = {
            "status": "machine_pass_human_pending",
            "rendered_at": "2099-01-01",
            "platform": "test",
            "sample_rate": 48000,
            "channels": 2,
            "subtype": "PCM_24",
            "frame_count": 480000,
            "duration_seconds": 10.0,
            "peak_active_voices": 1,
            "peak": 0.25,
            "rms": 0.05,
            "clipped_samples": 0,
            "wav": wav.relative_to(self.project).as_posix(),
            "wav_sha256": _sha256(wav),
            "hash_algorithm": HASH_ALGORITHM,
            "canonicalization": CANONICALIZATION,
            "manifest_canonical_sha256": canonical_json_file_sha256(
                manifest
            ),
            "events": events.relative_to(self.project).as_posix(),
            "events_canonical_sha256": canonical_json_file_sha256(events),
            "coverage": ["低中高音域", "弱强与释放"],
            "human_review": "pending",
        }
        (directory / "试听核验.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.instrument_paths.append(relative)
        self.wav_paths[relative] = wav

    def _create_technical(self, name: str = "review") -> tuple[Path, dict]:
        output = self.project / name
        plan = self.tool.create_review_plan(
            self.project,
            output,
            layer="technical",
            seed=42,
            minimum_batch_size=6,
            maximum_batch_size=8,
            grouping="stratified_mixed",
            materialize="none",
            created_at="2099-01-01T00:00:00Z",
        )
        return output, plan

    def _batch_paths(self, review_root: Path) -> list[Path]:
        return sorted(review_root.rglob("batch.json"))

    def _complete_response(
        self,
        batch_path: Path,
        response_path: Path,
        reviewer_id: str,
        *,
        status: str = "pass",
        role: str = "general_listener",
        expertise: list[str] | None = None,
    ) -> dict:
        response = self.tool.start_response(
            batch_path,
            response_path,
            self.project,
            reviewer_id=reviewer_id,
            role=role,
            expertise=expertise or [],
            listening_environment="headphones",
            device="test headphones",
            started_at="2099-01-01T00:00:00Z",
        )
        batch = self.tool.load_batch(batch_path)
        comment = "" if status == "pass" else "测试说明"
        answers = []
        for item in batch["items"]:
            for question in batch["questions"]:
                answers.append(
                    {
                        "item_id": item["item_id"],
                        "wav_sha256": item["wav_sha256"],
                        "question_id": question["question_id"],
                        "status": status,
                        "comment": comment,
                        "answered_at": "2099-01-01T00:10:00Z",
                    }
                )
        response["answers"] = answers
        response["completion_status"] = "complete"
        response["session"]["completed_at"] = "2099-01-01T00:10:00Z"
        self.tool.write_json_atomic(response_path, response)
        return response

    def test_all_new_schemas_are_valid_draft_2020_12(self) -> None:
        for path in SCHEMA_PATHS.values():
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)

    def test_103_style_partition_uses_only_six_to_eight_items(self) -> None:
        sizes = self.tool.balanced_sizes(103, 6, 8)
        self.assertEqual(sum(sizes), 103)
        self.assertEqual(len(sizes), 13)
        self.assertTrue(all(6 <= value <= 8 for value in sizes))
        self.assertEqual(sizes.count(8), 12)
        self.assertEqual(sizes.count(7), 1)

    def test_create_is_randomized_deterministic_and_schema_valid(self) -> None:
        first_root, first_plan = self._create_technical("review-a")
        second_root, second_plan = self._create_technical("review-b")
        self.assertEqual(first_plan["batch_count"], 2)
        self.assertEqual(
            [entry["batch_id"] for entry in first_plan["batches"]],
            [entry["batch_id"] for entry in second_plan["batches"]],
        )
        self.assertEqual(
            sorted(entry["item_count"] for entry in first_plan["batches"]),
            [6, 6],
        )

        schema = json.loads(SCHEMA_PATHS["batch"].read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        first_orders = []
        second_orders = []
        for first, second in zip(
            self._batch_paths(first_root),
            self._batch_paths(second_root),
            strict=True,
        ):
            first_batch = json.loads(first.read_text(encoding="utf-8"))
            second_batch = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(first_batch["schema_version"], 2)
            self.assertFalse(
                first_batch["review_policy"][
                    "automatic_collaboration_promotion"
                ]
            )
            self.assertNotIn(
                "automatic_formal_promotion",
                first_batch["review_policy"],
            )
            self.assertEqual(list(validator.iter_errors(first_batch)), [])
            self.assertEqual(self.tool.validate_batch(first, self.project), [])
            for item in first_batch["items"]:
                self.assertEqual(item["hash_algorithm"], HASH_ALGORITHM)
                self.assertEqual(
                    item["canonicalization"],
                    CANONICALIZATION,
                )
                self.assertIn("manifest_canonical_sha256", item)
                self.assertIn("events_canonical_sha256", item)
                self.assertNotIn("manifest_sha256", item)
                self.assertNotIn("events_sha256", item)
            self.assertTrue(first_batch["randomization"]["blind_order"])
            self.assertFalse(
                first_batch["review_policy"]["target_identity_visible"]
            )
            first_orders.extend(
                item["instrument_path"] for item in first_batch["items"]
            )
            second_orders.extend(
                item["instrument_path"] for item in second_batch["items"]
            )
        self.assertEqual(first_orders, second_orders)
        self.assertNotEqual(first_orders, sorted(first_orders))

    def test_archived_v1_batch_is_read_only_compatible(self) -> None:
        review_root, _plan = self._create_technical("review-v1")
        batch_path = self._batch_paths(review_root)[0]
        archived = json.loads(batch_path.read_text(encoding="utf-8"))
        for item in archived["items"]:
            instrument = (
                self.project / "乐器" / Path(item["instrument_path"])
            )
            manifest = instrument / "乐器.json"
            report_path = self.project / item["report_path"]
            report = json.loads(report_path.read_text(encoding="utf-8"))
            events = self.project / report["events"]
            manifest_raw = _sha256(manifest)
            events_raw = _sha256(events)
            report["identity_migration"] = {
                "status": "superseded_by_canonical_json_v1",
                "hash_algorithm": HASH_ALGORITHM,
                "hash_semantics": "source-file-bytes",
                "manifest_sha256": manifest_raw,
                "events_sha256": events_raw,
            }
            self.tool.write_json_atomic(report_path, report)
            for field in (
                "hash_algorithm",
                "canonicalization",
                "manifest_canonical_sha256",
                "events_canonical_sha256",
            ):
                item.pop(field)
            item["manifest_sha256"] = manifest_raw
            item["events_sha256"] = events_raw
        archived["schema_version"] = 1
        policy = archived["review_policy"]
        policy["automatic_formal_promotion"] = policy.pop(
            "automatic_collaboration_promotion"
        )
        archived["batch_sha256"] = self.tool.canonical_sha256(
            archived,
            omit="batch_sha256",
        )
        self.tool.write_json_atomic(batch_path, archived)
        before = batch_path.read_bytes()

        self.assertEqual(
            self.tool.validate_batch(batch_path, self.project),
            [],
        )
        self.assertEqual(batch_path.read_bytes(), before)
        loaded = self.tool.load_batch(batch_path)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertIn(
            "automatic_formal_promotion",
            loaded["review_policy"],
        )

    def test_hardlink_materialization_uses_generic_names(self) -> None:
        output = self.project / "hardlink-review"
        self.tool.create_review_plan(
            self.project,
            output,
            layer="technical",
            seed=7,
            minimum_batch_size=6,
            maximum_batch_size=8,
            grouping="stratified_mixed",
            materialize="hardlink",
            created_at="2099-01-01T00:00:00Z",
        )
        batch_path = self._batch_paths(output)[0]
        batch = self.tool.load_batch(batch_path)
        for item in batch["items"]:
            playback = batch_path.parent / item["playback_wav"]
            self.assertRegex(playback.name, r"^[0-9]{2}\.wav$")
            source = self.project / item["source_wav"]
            self.assertEqual(_sha256(playback), _sha256(source))

    def test_record_requires_comment_and_completes_atomically(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        response_path = review_root / "responses" / "listener.json"
        self.tool.start_response(
            batch_path,
            response_path,
            self.project,
            reviewer_id="listener-one",
            role="general_listener",
            listening_environment="headphones",
            device="test headphones",
            started_at="2099-01-01T00:00:00Z",
        )
        batch = self.tool.load_batch(batch_path)
        first_item = batch["items"][0]
        first_question = batch["questions"][0]
        with self.assertRaisesRegex(self.tool.ReviewError, "必须写评论"):
            self.tool.record_answer(
                batch_path,
                response_path,
                self.project,
                item_label=first_item["blind_label"],
                question_id=first_question["question_id"],
                status="reject",
                comment="",
            )
        updated = self.tool.record_answer(
            batch_path,
            response_path,
            self.project,
            item_label=first_item["blind_label"],
            question_id=first_question["question_id"],
            status="unsure",
            comment="需要重听",
            answered_at="2099-01-01T00:01:00Z",
        )
        self.assertEqual(updated["completion_status"], "draft")
        self.assertEqual(len(updated["answers"]), 1)
        self.assertEqual(
            self.tool.validate_response(
                batch_path,
                response_path,
                self.project,
                require_complete=False,
            ),
            [],
        )

        completed = self._complete_response(
            batch_path,
            review_root / "responses" / "listener-two.json",
            "listener-two",
        )
        self.assertEqual(completed["completion_status"], "complete")
        self.assertEqual(
            self.tool.validate_response(
                batch_path,
                review_root / "responses" / "listener-two.json",
                self.project,
            ),
            [],
        )
        summary = self.tool.summarize_reviews(
            review_root,
            review_root / "responses",
            self.project,
            generated_at="2099-01-01T01:00:00Z",
        )
        self.assertEqual(summary["response_states"]["accepted"], 1)
        self.assertEqual(summary["response_states"]["draft"], 1)

    def test_changed_wav_makes_old_response_stale(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        response_path = review_root / "responses" / "complete.json"
        self._complete_response(batch_path, response_path, "listener-one")
        changed = self.project / batch["items"][0]["source_wav"]
        changed.write_bytes(b"new render with a different hash")

        issues = self.tool.validate_response(
            batch_path,
            response_path,
            self.project,
        )
        self.assertTrue(
            any(
                issue.startswith("stale:") and "当前源WAV Hash已变化" in issue
                for issue in issues
            ),
            issues,
        )

    def test_changed_manifest_or_events_makes_old_response_stale(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        response_path = review_root / "responses" / "complete.json"
        self._complete_response(batch_path, response_path, "listener-one")
        first = batch["items"][0]
        instrument_dir = self.project / "乐器" / first["instrument_path"]
        manifest = instrument_dir / "乐器.json"
        old_manifest = manifest.read_bytes()
        changed_manifest = json.loads(old_manifest)
        changed_manifest["semantic_change"] = True
        manifest.write_text(
            json.dumps(changed_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest_issues = self.tool.validate_response(
            batch_path,
            response_path,
            self.project,
        )
        self.assertTrue(
            any(
                issue.startswith("stale:")
                and "当前manifest Hash已变化" in issue
                for issue in manifest_issues
            ),
            manifest_issues,
        )

        manifest.write_bytes(old_manifest)
        events = (
            self.project
            / "examples"
            / f"{instrument_dir.name}_奏法.events.json"
        )
        changed_events = json.loads(events.read_text(encoding="utf-8"))
        changed_events["semantic_change"] = True
        events.write_text(
            json.dumps(changed_events, ensure_ascii=False),
            encoding="utf-8",
        )
        event_issues = self.tool.validate_response(
            batch_path,
            response_path,
            self.project,
        )
        self.assertTrue(
            any(
                issue.startswith("stale:")
                and "当前events Hash已变化" in issue
                for issue in event_issues
            ),
            event_issues,
        )

    def test_json_layout_only_change_does_not_make_batch_stale(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        response_path = review_root / "responses" / "complete.json"
        self._complete_response(batch_path, response_path, "listener-one")
        first = batch["items"][0]
        instrument_dir = self.project / "乐器" / first["instrument_path"]
        manifest = instrument_dir / "乐器.json"
        events = (
            self.project
            / "examples"
            / f"{instrument_dir.name}_奏法.events.json"
        )
        for path in (manifest, events):
            document = json.loads(path.read_text(encoding="utf-8"))
            formatted = (
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=4,
                )
                + "\n"
            )
            path.write_bytes(formatted.replace("\n", "\r\n").encode("utf-8"))

        self.assertEqual(
            self.tool.validate_response(
                batch_path,
                response_path,
                self.project,
            ),
            [],
        )

    def test_source_discovery_accepts_hash_bound_event_in_protocol_subdir(
        self,
    ) -> None:
        relative = self.instrument_paths[0]
        directory = self.project / "乐器" / relative
        root_event = (
            self.project
            / "examples"
            / f"{directory.name}_奏法.events.json"
        )
        nested_event = (
            self.project
            / "examples"
            / "全音域上行"
            / f"{directory.name}_全音域上行.events.json"
        )
        nested_event.parent.mkdir(parents=True)
        root_event.replace(nested_event)
        report_path = directory / "试听核验.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["events"] = nested_event.relative_to(self.project).as_posix()
        report["events_canonical_sha256"] = canonical_json_file_sha256(
            nested_event
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        sources = self.tool.discover_review_sources(
            self.project,
            only=(relative,),
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["instrument_path"], relative)
        self.assertEqual(
            sources[0]["events_canonical_sha256"],
            canonical_json_file_sha256(nested_event),
        )

    def test_legacy_report_is_read_but_new_batch_uses_canonical_identity(
        self,
    ) -> None:
        relative = self.instrument_paths[0]
        directory = self.project / "乐器" / relative
        manifest = directory / "乐器.json"
        report_path = directory / "试听核验.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        events = self.project / report["events"]
        for field in (
            "hash_algorithm",
            "canonicalization",
            "manifest_canonical_sha256",
            "events_canonical_sha256",
        ):
            report.pop(field)
        report["manifest_sha256"] = _sha256(manifest)
        report["events_sha256"] = _sha256(events)
        self.tool.write_json_atomic(report_path, report)

        output = self.project / "legacy-source"
        self.tool.create_review_plan(
            self.project,
            output,
            layer="technical",
            seed=1,
            minimum_batch_size=1,
            maximum_batch_size=1,
            grouping="family",
            materialize="none",
            only=[relative],
            created_at="2099-01-01T00:00:00Z",
        )
        batch = self.tool.load_batch(self._batch_paths(output)[0])
        item = batch["items"][0]
        self.assertEqual(item["hash_algorithm"], HASH_ALGORITHM)
        self.assertEqual(item["canonicalization"], CANONICALIZATION)
        self.assertEqual(
            item["manifest_canonical_sha256"],
            canonical_json_file_sha256(manifest),
        )
        self.assertEqual(
            item["events_canonical_sha256"],
            canonical_json_file_sha256(events),
        )
        self.assertNotIn("manifest_sha256", item)
        self.assertNotIn("events_sha256", item)

    def test_report_annotation_alone_does_not_invalidate_bound_evidence(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        response_path = review_root / "responses" / "complete.json"
        self._complete_response(batch_path, response_path, "listener-one")
        report_path = self.project / batch["items"][0]["report_path"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["manual_review_note"] = "coordinator annotation"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.tool.validate_response(
                batch_path,
                response_path,
                self.project,
            ),
            [],
        )

    def test_identity_requires_whitelisted_hashed_reference_and_role(self) -> None:
        reference = self.project / "音源" / "听审参考" / "ref.wav"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"reference audio")
        first = self.instrument_paths[0]
        asset_map_path = self.project / "assets.json"
        asset_map_path.write_text(
            json.dumps(
                {
                    "$schema": "https://tianlai.local/schemas/listening-review-assets.schema.json",
                    "schema_version": 1,
                    "items": {
                        first: [
                            {
                                "kind": "reference",
                                "label": "strict reference",
                                "path": reference.relative_to(self.project).as_posix(),
                                "source": "test provenance",
                                "license": "CC0-1.0",
                                "notes": "",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        output = self.project / "identity-review"
        plan = self.tool.create_review_plan(
            self.project,
            output,
            layer="identity",
            seed=3,
            minimum_batch_size=6,
            maximum_batch_size=8,
            grouping="family",
            materialize="none",
            asset_map_path=asset_map_path,
            created_at="2099-01-01T00:00:00Z",
        )
        self.assertEqual(plan["included_instrument_count"], 1)
        self.assertEqual(len(plan["skipped_without_required_assets"]), 11)
        batch_path = self._batch_paths(output)[0]
        batch = self.tool.load_batch(batch_path)
        self.assertEqual(batch["items"][0]["references"][0]["sha256"], _sha256(reference))
        with self.assertRaisesRegex(self.tool.ReviewError, "不接受"):
            self.tool.start_response(
                batch_path,
                output / "responses" / "bad-role.json",
                self.project,
                reviewer_id="ordinary",
                role="general_listener",
                listening_environment="headphones",
                device="test",
            )

        bad_map = json.loads(asset_map_path.read_text(encoding="utf-8"))
        bad_map["items"][first][0]["license"] = "CC-BY-SA-4.0"
        bad_path = self.project / "bad-assets.json"
        bad_path.write_text(
            json.dumps(bad_map, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.tool.ReviewError, "不符合 Schema|不在白名单"
        ):
            self.tool.load_asset_map(bad_path, self.project)

    def test_expert_layer_requires_declared_expertise(self) -> None:
        output = self.project / "expert-review"
        self.tool.create_review_plan(
            self.project,
            output,
            layer="expert",
            seed=5,
            minimum_batch_size=6,
            maximum_batch_size=8,
            grouping="family",
            materialize="none",
            only=["管弦乐/弦乐组"],
            created_at="2099-01-01T00:00:00Z",
        )
        batch_path = self._batch_paths(output)[0]
        with self.assertRaisesRegex(self.tool.ReviewError, "expertise"):
            self.tool.start_response(
                batch_path,
                output / "responses" / "expert.json",
                self.project,
                reviewer_id="expert-one",
                role="instrument_expert",
                expertise=[],
                listening_environment="speakers",
                device="nearfield monitors",
            )
        response = self.tool.start_response(
            batch_path,
            output / "responses" / "expert.json",
            self.project,
            reviewer_id="expert-one",
            role="instrument_expert",
            expertise=["西洋弦乐"],
            listening_environment="speakers",
            device="nearfield monitors",
        )
        self.assertEqual(response["reviewer"]["expertise"], ["西洋弦乐"])

    def test_two_complete_reviewers_never_auto_change_collaboration_status(
        self,
    ) -> None:
        review_root, _plan = self._create_technical()
        first_batch = self._batch_paths(review_root)[0]
        self._complete_response(
            first_batch,
            review_root / "responses" / "listener-a.json",
            "listener-a",
        )
        self._complete_response(
            first_batch,
            review_root / "responses" / "listener-b.json",
            "listener-b",
        )
        summary = self.tool.summarize_reviews(
            review_root,
            review_root / "responses",
            self.project,
            generated_at="2099-01-01T01:00:00Z",
        )
        self.assertEqual(summary["response_states"]["accepted"], 2)
        passed = [
            item
            for item in summary["items"]
            if item["batch_id"] == self.tool.load_batch(first_batch)["batch_id"]
        ]
        self.assertTrue(passed)
        self.assertTrue(all(item["disposition"] == "pass" for item in passed))
        self.assertEqual(summary["schema_version"], 2)
        gate = summary["collaboration_review_gate"]
        self.assertEqual(gate["status"], "blocked")
        self.assertFalse(gate["automatic_status_change"])
        self.assertNotIn("formal_gate", summary)
        self.assertIn(
            "没有有效的乐器族专家响应",
            gate["reasons"],
        )
        self.assertIn("quality_tier", gate["notice"])
        schema = json.loads(SCHEMA_PATHS["summary"].read_text(encoding="utf-8"))
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(summary)),
            [],
        )

    def test_pass_reject_disagreement_is_conflict(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        self._complete_response(
            batch_path,
            review_root / "responses" / "pass.json",
            "listener-pass",
            status="pass",
        )
        self._complete_response(
            batch_path,
            review_root / "responses" / "reject.json",
            "listener-reject",
            status="reject",
        )
        summary = self.tool.summarize_reviews(
            review_root,
            review_root / "responses",
            self.project,
            generated_at="2099-01-01T01:00:00Z",
        )
        batch_id = self.tool.load_batch(batch_path)["batch_id"]
        results = [
            item for item in summary["items"] if item["batch_id"] == batch_id
        ]
        self.assertTrue(results)
        self.assertTrue(
            all(item["disposition"] == "conflict" for item in results)
        )

    def test_duplicate_reviewer_responses_are_excluded(self) -> None:
        review_root, _plan = self._create_technical()
        batch_path = self._batch_paths(review_root)[0]
        self._complete_response(
            batch_path,
            review_root / "responses" / "one.json",
            "same-listener",
        )
        self._complete_response(
            batch_path,
            review_root / "responses" / "two.json",
            "same-listener",
        )
        summary = self.tool.summarize_reviews(
            review_root,
            review_root / "responses",
            self.project,
            generated_at="2099-01-01T01:00:00Z",
        )
        self.assertEqual(summary["response_states"]["accepted"], 0)
        self.assertEqual(summary["response_states"]["invalid"], 2)

    def test_offline_zip_is_self_contained_blinded_and_ordered(self) -> None:
        review_root, _plan = self._create_technical("中文 听审根")
        batch_path = self._batch_paths(review_root)[0]
        hidden_reference = self.project / "音源" / "内部 揭盲参考.wav"
        hidden_reference.parent.mkdir(parents=True)
        hidden_reference.write_bytes(b"must never enter a technical package")
        batch_document = json.loads(batch_path.read_text(encoding="utf-8"))
        batch_document["items"][0]["references"].append(
            {
                "kind": "reference",
                "label": "测试乐器绝密身份",
                "path": hidden_reference.relative_to(self.project).as_posix(),
                "sha256": _sha256(hidden_reference),
                "source": "内部揭盲来源",
                "license": "CC0-1.0",
                "notes": "技术层禁止外发",
            }
        )
        batch_document["batch_sha256"] = self.tool.canonical_sha256(
            batch_document,
            omit="batch_sha256",
        )
        self.tool.write_json_atomic(batch_path, batch_document)
        batch = self.tool.load_batch(batch_path)
        output = self.project / "给朋友" / "技术听审 第01批.zip"
        result = self.tool.export_offline_package(
            batch_path,
            output,
            self.project,
            exported_at="2099-01-01T00:00:00Z",
        )
        self.assertTrue(result["archive"])
        self.assertTrue(output.is_file())

        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertTrue(names)
            self.assertTrue(all("\\" not in name for name in names))
            self.assertTrue(
                all(
                    not Path(name).is_absolute()
                    and ".." not in Path(name).parts
                    for name in names
                )
            )
            self.assertFalse(any(name.endswith("batch.json") for name in names))
            root_name = "技术听审 第01批"
            html_name = f"{root_name}/天籁听审问卷.html"
            guide_name = f"{root_name}/使用说明.txt"
            checksum_name = f"{root_name}/SHA256SUMS.txt"
            attribution_name = f"{root_name}/许可与署名.txt"
            self.assertIn(html_name, names)
            self.assertIn(guide_name, names)
            self.assertIn(checksum_name, names)
            self.assertIn(attribution_name, names)
            audio_names = [
                name
                for name in names
                if name.startswith(f"{root_name}/audio/")
            ]
            self.assertEqual(
                audio_names,
                [
                    f"{root_name}/audio/{index:02d}.wav"
                    for index in range(1, len(batch["items"]) + 1)
                ],
            )

            html = archive.read(html_name).decode("utf-8")
            guide = archive.read(guide_name).decode("utf-8")
            checksums = archive.read(checksum_name).decode("utf-8")
            attribution = archive.read(attribution_name).decode("utf-8")
            self.assertIn("所有技术层听审包使用同一份", attribution)
            self.assertIn("不表示当前批次使用了它", attribution)
            self.assertIn("统一修改说明", attribution)
            blinded_playback_text = "\n".join((html, guide, checksums))
            self.assertNotIn("__TIANLAI_OFFLINE_REVIEW_DATA__", html)
            self.assertNotIn("绝密身份", blinded_playback_text)
            self.assertNotIn("内部揭盲来源", blinded_playback_text)
            self.assertNotIn("<script src=", html)
            self.assertNotRegex(html, r"<link[^>]+href=")
            self.assertIn("connect-src 'none'", html)
            for item in batch["items"]:
                self.assertNotIn(
                    item["instrument_path"],
                    blinded_playback_text,
                )
                self.assertNotIn(
                    item["instrument_name"],
                    blinded_playback_text,
                )
                self.assertNotIn(
                    item["source_wav"],
                    blinded_playback_text,
                )
                member = (
                    f"{root_name}/audio/{int(item['order']):02d}.wav"
                )
                self.assertEqual(
                    hashlib.sha256(archive.read(member)).hexdigest(),
                    item["wav_sha256"],
                )

            match = re.search(
                r'<script id="review-data" type="application/json">'
                r"(.*?)</script>",
                html,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            public = json.loads(match.group(1))
            self.assertEqual(public["batch_id"], batch["batch_id"])
            self.assertEqual(public["batch_sha256"], batch["batch_sha256"])
            self.assertEqual(
                [item["playback_wav"] for item in public["items"]],
                [
                    f"audio/{index:02d}.wav"
                    for index in range(1, len(batch["items"]) + 1)
                ],
            )
            self.assertTrue(
                all("display_identity" not in item for item in public["items"])
            )
            self.assertTrue(
                all(not item["auxiliary_audio"] for item in public["items"])
            )

    def test_offline_export_refuses_stale_and_escaping_playback(self) -> None:
        review_root, _plan = self._create_technical("export-guards")
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        first_source = self.project / batch["items"][0]["source_wav"]
        original = first_source.read_bytes()
        first_source.write_bytes(original + b"tampered")
        stale_output = self.project / "stale.zip"
        with self.assertRaisesRegex(self.tool.ReviewError, "不可导出"):
            self.tool.export_offline_package(
                batch_path,
                stale_output,
                self.project,
            )
        self.assertFalse(stale_output.exists())

        first_source.write_bytes(original)
        escaping = json.loads(batch_path.read_text(encoding="utf-8"))
        outside = review_root / "outside.wav"
        outside.write_bytes(original)
        escaping["items"][0]["playback_origin"] = "batch"
        escaping["items"][0]["playback_wav"] = "../../outside.wav"
        escaping["batch_sha256"] = self.tool.canonical_sha256(
            escaping,
            omit="batch_sha256",
        )
        self.tool.write_json_atomic(batch_path, escaping)
        escape_output = self.project / "escape.zip"
        with self.assertRaisesRegex(self.tool.ReviewError, "越出批次目录"):
            self.tool.export_offline_package(
                batch_path,
                escape_output,
                self.project,
            )
        self.assertFalse(escape_output.exists())

    def test_technical_packages_share_one_global_attribution_pool(self) -> None:
        review_root, _plan = self._create_technical("global-attribution")
        batches = [
            self.tool.load_batch(path)
            for path in self._batch_paths(review_root)
        ]
        self.assertGreaterEqual(len(batches), 2)
        first = self.tool._offline_attribution_notice(
            batches[0],
            self.project,
        )
        second = self.tool._offline_attribution_notice(
            batches[1],
            self.project,
        )
        self.assertEqual(first, second)

    def test_private_review_package_includes_quarantined_render(self) -> None:
        restricted_path = self.instrument_paths[0]
        instrument_dir = self.project / "乐器" / Path(restricted_path)
        manifest_path = instrument_dir / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["license_status"] = "quarantined"
        manifest["upstream"] = "Mixed open sample library"
        manifest["license"] = "CC-BY-SA-4.0 mixed attribution pending"
        self.tool.write_json_atomic(manifest_path, manifest)
        report_path = instrument_dir / "试听核验.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["manifest_canonical_sha256"] = canonical_json_file_sha256(
            manifest_path
        )
        self.tool.write_json_atomic(report_path, report)

        review_root, plan = self._create_technical(
            "private-includes-quarantine"
        )
        restricted_batch = next(
            path
            for path in self._batch_paths(review_root)
            if restricted_path
            in {
                item["instrument_path"]
                for item in self.tool.load_batch(path)["items"]
            }
        )
        self.assertEqual(plan["included_instrument_count"], 12)
        output = self.project / "private-review.zip"
        self.tool.export_offline_package(
            restricted_batch,
            output,
            self.project,
        )
        self.assertTrue(output.is_file())
        with zipfile.ZipFile(output) as archive:
            notice_name = next(
                name
                for name in archive.namelist()
                if name.endswith("/许可与署名.txt")
            )
            notice = archive.read(notice_name).decode("utf-8")
        self.assertIn("普通听众或大众质量审核", notice)
        self.assertIn("quarantined", notice)
        self.assertIn("Mixed open sample library", notice)

    def test_offline_embedded_json_escapes_script_termination(self) -> None:
        review_root, _plan = self._create_technical("html-escaping")
        batch_path = self._batch_paths(review_root)[0]
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        attack = '</script><script id="injected">throw 1</script>'
        batch["description"] = attack
        batch["batch_sha256"] = self.tool.canonical_sha256(
            batch,
            omit="batch_sha256",
        )
        self.tool.write_json_atomic(batch_path, batch)
        output = self.project / "escaped-package"
        self.tool.export_offline_package(
            batch_path,
            output,
            self.project,
        )
        html = (output / "天籁听审问卷.html").read_text(encoding="utf-8")
        self.assertNotIn(attack, html)
        self.assertNotIn('<script id="injected">', html)
        match = re.search(
            r'<script id="review-data" type="application/json">'
            r"(.*?)</script>",
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        public = json.loads(match.group(1))
        self.assertEqual(public["description"], attack)

    def test_offline_response_import_is_valid_idempotent_and_unique(self) -> None:
        review_root, _plan = self._create_technical("import-roundtrip")
        batch_path = self._batch_paths(review_root)[0]
        returned = self.project / "朋友返回" / "张三 完整.json"
        response = self._complete_response(
            batch_path,
            returned,
            "listener-zhang",
        )
        self.assertEqual(
            self.tool.locate_batch_for_offline_response(
                review_root,
                returned,
            ),
            batch_path.resolve(),
        )
        responses_root = review_root / "responses"
        imported = self.tool.import_offline_response(
            batch_path,
            returned,
            responses_root,
            self.project,
        )
        self.assertTrue(imported.is_file())
        self.assertEqual(
            self.tool.validate_response(
                batch_path,
                imported,
                self.project,
            ),
            [],
        )
        self.assertEqual(
            self.tool.import_offline_response(
                batch_path,
                returned,
                responses_root,
                self.project,
            ),
            imported.resolve(),
        )
        nested = responses_root / "归档"
        nested.mkdir()
        nested_imported = nested / imported.name
        imported.replace(nested_imported)
        self.assertEqual(
            self.tool.import_offline_response(
                batch_path,
                returned,
                responses_root,
                self.project,
            ),
            nested_imported.resolve(),
        )

        duplicate = json.loads(json.dumps(response))
        duplicate["response_id"] = duplicate["response_id"] + "-new"
        duplicate["session"]["notes"] = "第二份"
        duplicate_path = self.project / "朋友返回" / "张三 第二份.json"
        self.tool.write_json_atomic(duplicate_path, duplicate)
        with self.assertRaisesRegex(
            self.tool.ReviewError,
            "同一批次已导入",
        ):
            self.tool.import_offline_response(
                batch_path,
                duplicate_path,
                responses_root,
                self.project,
            )

    def test_offline_auxiliary_kind_cannot_escape_package_root(self) -> None:
        reference = self.project / "音源" / "reference.wav"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"reference audio")
        instrument_path = self.instrument_paths[0]
        asset_map = self.project / "assets.json"
        asset_map.write_text(
            json.dumps(
                {
                    "$schema": (
                        "https://tianlai.local/schemas/"
                        "listening-review-assets.schema.json"
                    ),
                    "schema_version": 1,
                    "items": {
                        instrument_path: [
                            {
                                "kind": "reference",
                                "label": "reference",
                                "path": reference.relative_to(
                                    self.project
                                ).as_posix(),
                                "source": "test",
                                "license": "CC0-1.0",
                                "notes": "",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        review_root = self.project / "identity-path-guard"
        self.tool.create_review_plan(
            self.project,
            review_root,
            layer="identity",
            seed=1,
            minimum_batch_size=1,
            maximum_batch_size=1,
            grouping="family",
            materialize="none",
            asset_map_path=asset_map,
            only=[instrument_path],
            created_at="2099-01-01T00:00:00Z",
        )
        batch_path = self._batch_paths(review_root)[0]
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["items"][0]["references"][0][
            "kind"
        ] = "x/../../../../../escaped"
        batch["batch_sha256"] = self.tool.canonical_sha256(
            batch,
            omit="batch_sha256",
        )
        self.tool.write_json_atomic(batch_path, batch)

        original_schema_errors = self.tool._schema_errors
        self.tool._schema_errors = lambda *_args, **_kwargs: []
        try:
            output = self.project / "identity-path-guard.zip"
            with self.assertRaisesRegex(self.tool.ReviewError, "kind 无效"):
                self.tool.export_offline_package(
                    batch_path,
                    output,
                    self.project,
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(self.project.rglob("escaped*")), [])
        finally:
            self.tool._schema_errors = original_schema_errors

    def test_private_context_asset_is_complete_and_explicit(self) -> None:
        context = self.project / "作品" / "内部测试曲.wav"
        context.parent.mkdir(parents=True)
        context.write_bytes(b"private rendered work")
        instrument_path = self.instrument_paths[0]
        asset_document = {
            "$schema": (
                "https://tianlai.local/schemas/"
                "listening-review-assets.schema.json"
            ),
            "schema_version": 1,
            "items": {
                instrument_path: [
                    {
                        "kind": "context",
                        "label": "内部测试曲场景",
                        "path": context.relative_to(
                            self.project
                        ).as_posix(),
                        "source": "受邀私下复核用内部测试渲染",
                        "license": "private-review-authorized",
                        "notes": "仅限指定听审者，不公开转发",
                    }
                ]
            },
        }
        asset_map = self.project / "private-context.json"
        asset_map.write_text(
            json.dumps(asset_document, ensure_ascii=False),
            encoding="utf-8",
        )
        review_root = self.project / "private-context-review"
        self.tool.create_review_plan(
            self.project,
            review_root,
            layer="context",
            seed=2,
            minimum_batch_size=1,
            maximum_batch_size=1,
            grouping="family",
            materialize="none",
            asset_map_path=asset_map,
            only=[instrument_path],
            created_at="2099-01-01T00:00:00Z",
        )
        batch_path = self._batch_paths(review_root)[0]
        output = self.project / "private-context-package"
        result = self.tool.export_offline_package(
            batch_path,
            output,
            self.project,
            exported_at="2099-01-01T00:00:00Z",
        )
        self.assertEqual(result["audio_file_count"], 2)
        notice = (output / "许可与署名.txt").read_text(encoding="utf-8")
        self.assertIn("内部测试曲场景", notice)
        self.assertIn("private-review-authorized", notice)

    def test_old_source_timestamp_does_not_break_zip_export(self) -> None:
        review_root, _plan = self._create_technical("old-mtime")
        batch_path = self._batch_paths(review_root)[0]
        batch = self.tool.load_batch(batch_path)
        source = self.project / batch["items"][0]["source_wav"]
        os.utime(source, (0, 0))
        output = self.project / "old-mtime.zip"
        self.tool.export_offline_package(
            batch_path,
            output,
            self.project,
        )
        self.assertTrue(zipfile.is_zipfile(output))

    def test_read_json_wraps_invalid_utf8(self) -> None:
        broken = self.project / "invalid-utf8.json"
        broken.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(self.tool.ReviewError, "无法读取 JSON"):
            self.tool.read_json(broken)

    def test_response_timestamps_must_be_valid_utc(self) -> None:
        review_root, _plan = self._create_technical("timestamp-validation")
        batch_path = self._batch_paths(review_root)[0]
        response_path = self.project / "valid-response.json"
        valid = self._complete_response(
            batch_path,
            response_path,
            "listener-time",
        )
        batch = self.tool.load_batch(batch_path)
        cases = {}
        invalid_started = json.loads(json.dumps(valid))
        invalid_started["session"]["started_at"] = "2099-99-01T00:00:00Z"
        cases["started"] = invalid_started
        invalid_completed = json.loads(json.dumps(valid))
        invalid_completed["session"]["completed_at"] = ""
        cases["completed"] = invalid_completed
        invalid_answered = json.loads(json.dumps(valid))
        invalid_answered["answers"][0]["answered_at"] = (
            "2099-01-01T08:10:00+08:00"
        )
        cases["answered"] = invalid_answered
        invalid_draft = json.loads(json.dumps(valid))
        invalid_draft["completion_status"] = "draft"
        cases["draft-completed"] = invalid_draft
        for name, response in cases.items():
            with self.subTest(name=name):
                issues = self.tool.validate_response_document(
                    batch,
                    response,
                    require_complete=False,
                    check_schema=False,
                )
                self.assertTrue(issues)

    def test_batch_autolocation_uses_id_and_hash(self) -> None:
        common = self.project / "duplicate-batches"
        first_root = common / "first"
        second_root = common / "second"
        for output, created_at in (
            (first_root, "2099-01-01T00:00:00Z"),
            (second_root, "2099-01-02T00:00:00Z"),
        ):
            self.tool.create_review_plan(
                self.project,
                output,
                layer="technical",
                seed=42,
                minimum_batch_size=6,
                maximum_batch_size=8,
                grouping="stratified_mixed",
                materialize="none",
                created_at=created_at,
            )
        first_batch = self._batch_paths(first_root)[0]
        second_batch = self._batch_paths(second_root)[0]
        self.assertEqual(
            self.tool.load_batch(first_batch)["batch_id"],
            self.tool.load_batch(second_batch)["batch_id"],
        )
        self.assertNotEqual(
            self.tool.load_batch(first_batch)["batch_sha256"],
            self.tool.load_batch(second_batch)["batch_sha256"],
        )
        response_path = self.project / "bound-response.json"
        self._complete_response(
            first_batch,
            response_path,
            "listener-bound",
        )
        self.assertEqual(
            self.tool.locate_batch_for_offline_response(
                common,
                response_path,
            ),
            first_batch.resolve(),
        )

    def test_summary_handles_malformed_reviewer_without_crashing(self) -> None:
        review_root, _plan = self._create_technical("malformed-summary")
        batch_path = self._batch_paths(review_root)[0]
        response_path = review_root / "responses" / "malformed.json"
        response = self._complete_response(
            batch_path,
            response_path,
            "listener-malformed",
        )
        response["reviewer"] = []
        self.tool.write_json_atomic(response_path, response)
        summary = self.tool.summarize_reviews(
            review_root,
            review_root / "responses",
            self.project,
            generated_at="2099-01-01T01:00:00Z",
        )
        self.assertEqual(summary["response_states"]["accepted"], 0)
        self.assertEqual(summary["response_states"]["invalid"], 1)

    def test_offline_response_import_rejects_invalid_external_shapes(self) -> None:
        review_root, _plan = self._create_technical("invalid-imports")
        batch_path = self._batch_paths(review_root)[0]
        valid_path = self.project / "returned" / "valid.json"
        valid = self._complete_response(
            batch_path,
            valid_path,
            "listener-valid",
        )

        cases = {
            "draft": {
                **valid,
                "completion_status": "draft",
            },
            "missing": {
                **valid,
                "answers": valid["answers"][:-1],
            },
            "wrong-batch": {
                **valid,
                "batch_id": "technical-999-0000000000",
            },
            "bad-reviewer-shape": {
                **valid,
                "reviewer": [],
            },
        }
        nonpass = json.loads(json.dumps(valid))
        nonpass["answers"][0]["status"] = "reject"
        nonpass["answers"][0]["comment"] = ""
        cases["nonpass-without-comment"] = nonpass

        for name, document in cases.items():
            with self.subTest(name=name):
                path = self.project / "returned" / f"{name}.json"
                self.tool.write_json_atomic(path, document)
                responses_root = self.project / f"responses-{name}"
                with self.assertRaises(self.tool.ReviewError):
                    self.tool.import_offline_response(
                        batch_path,
                        path,
                        responses_root,
                        self.project,
                    )
                self.assertFalse(responses_root.exists())


if __name__ == "__main__":
    unittest.main()
