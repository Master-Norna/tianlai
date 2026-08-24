"""MCP editable-loop tools stay read-only, bounded and event-addressable."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tianlai.score_ops import (
    SCORE_PATCH_KIND,
    SCORE_SLICE_QUERY_KIND,
    canonical_score_sha256,
)


_HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class UpgradeScoreToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server

    def test_upgrade_is_pure_and_returns_stable_v1_ids(self) -> None:
        published = self.m.score_and_roster_format()["example_score"]
        legacy = copy.deepcopy(published)
        legacy.pop("schema_version")
        for part in legacy["parts"]:
            for note in part["notes"]:
                note.pop("event_id")
        original = copy.deepcopy(legacy)

        first = self.m.upgrade_score(legacy)
        second = self.m.upgrade_score(legacy)

        self.assertTrue(first["ok"])
        self.assertTrue(first["changed"])
        self.assertEqual(legacy, original)
        self.assertEqual(first["score"], second["score"])
        self.assertEqual(first["score"]["schema_version"], 1)
        self.assertEqual(
            [
                note["event_id"]
                for part in first["score"]["parts"]
                for note in part["notes"]
            ],
            [
                "event-000001",
                "event-000002",
                "event-000003",
                "event-000004",
            ],
        )
        self.assertTrue(first["warnings"])

    def test_upgrade_rejects_ambiguous_bar_end(self) -> None:
        legacy = copy.deepcopy(
            self.m.score_and_roster_format()["example_score"]
        )
        legacy.pop("schema_version")
        for part in legacy["parts"]:
            for note in part["notes"]:
                note.pop("event_id")
        legacy["parts"][0]["notes"][0]["beat"] = 5

        result = self.m.upgrade_score(legacy)

        self.assertFalse(result["ok"])
        self.assertIn("outside [1, 5)", result["issues"][0]["message"])


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class ValidateProjectToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server
        published = self.m.score_and_roster_format()
        self.score = published["example_score"]
        self.roster = published["example_roster"]

    def test_valid_project_compiles_without_audio_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-exist"
            with (
                patch.object(self.m, "OUTPUT_DIR", output),
                patch.object(
                    self.m,
                    "render_plan",
                    side_effect=AssertionError("validate called render_plan"),
                ),
            ):
                result = self.m.validate_project(
                    self.score,
                    self.roster,
                )

            self.assertFalse(output.exists())

        self.assertTrue(result["ok"])
        self.assertFalse(result["audio_rendered"])
        self.assertEqual(
            result["checks"]["resources"],
            {
                "status": "not_run",
                "level": "catalog_only",
                "ready_to_render": None,
                "reason_code": "audio_assets_not_opened",
            },
        )
        self.assertEqual(result["summary"]["source_event_count"], 4)
        self.assertEqual(result["summary"]["planned_note_count"], 4)
        self.assertNotIn("trace", result)

    def test_score_and_roster_failures_are_both_reported(self) -> None:
        result = self.m.validate_project(
            {"tempo_map": [], "parts": []},
            {"assignments": "not-an-array"},
        )

        self.assertFalse(result["ok"])
        stages = {issue["stage"] for issue in result["issues"]}
        self.assertIn("score_document", stages)
        self.assertIn("roster_document", stages)
        self.assertEqual(
            result["checks"]["performance_plan"]["status"],
            "skipped",
        )

    def test_invalid_bar_end_is_caught_before_planning(self) -> None:
        score = copy.deepcopy(self.score)
        score["parts"][0]["notes"][0]["beat"] = 5

        result = self.m.validate_project(score, self.roster)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["checks"]["score_time_coordinates"]["status"],
            "failed",
        )
        self.assertIn(
            "score.parts[0].notes[0].beat=5",
            "\n".join(issue["message"] for issue in result["issues"]),
        )
        self.assertEqual(
            result["checks"]["performance_plan"]["status"],
            "skipped",
        )

    def test_issue_page_is_bounded(self) -> None:
        result = self.m.validate_project(
            {"tempo_map": [], "parts": []},
            {"assignments": "not-an-array"},
            max_issues=1,
        )

        self.assertEqual(len(result["issues"]), 1)
        self.assertTrue(result["issues_truncated"])
        self.assertGreaterEqual(
            sum(result["issue_counts"].values()),
            2,
        )


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class PreciseScoreEditingToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server
        self.score = copy.deepcopy(
            self.m.score_and_roster_format()["example_score"]
        )

    def test_slice_and_patch_are_pure_and_event_addressable(self) -> None:
        sliced = self.m.get_score_slice(
            self.score,
            {
                "kind": SCORE_SLICE_QUERY_KIND,
                "schema_version": 1,
                "event_ids": ["piano-0002"],
                "max_notes": 8,
            },
        )
        self.assertTrue(sliced["ok"])
        self.assertEqual(sliced["matched_note_count"], 1)

        original = copy.deepcopy(self.score)
        patched = self.m.patch_score(
            self.score,
            {
                "kind": SCORE_PATCH_KIND,
                "schema_version": 1,
                "base_score_sha256": canonical_score_sha256(self.score),
                "operations": [
                    {
                        "op": "update_note",
                        "event_id": "piano-0002",
                        "expect": {"pitch": "E4"},
                        "changes": {"pitch": "F4"},
                    }
                ],
            },
        )
        self.assertTrue(patched["ok"])
        self.assertEqual(self.score, original)
        compared = self.m.compare_score_versions(
            self.score,
            patched["score"],
        )
        self.assertTrue(compared["ok"])
        self.assertEqual(compared["counts"]["updated"], 1)

    def test_patch_conflict_returns_stable_error_object(self) -> None:
        result = self.m.patch_score(
            self.score,
            {
                "kind": SCORE_PATCH_KIND,
                "schema_version": 1,
                "base_score_sha256": "0" * 64,
                "operations": [
                    {
                        "op": "delete_note",
                        "event_id": "piano-0001",
                    }
                ],
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "base_score_hash_mismatch")

    def test_patch_rejects_boolean_as_numeric_expectation(self) -> None:
        original = copy.deepcopy(self.score)
        result = self.m.patch_score(
            self.score,
            {
                "kind": SCORE_PATCH_KIND,
                "schema_version": 1,
                "base_score_sha256": canonical_score_sha256(self.score),
                "operations": [
                    {
                        "op": "delete_note",
                        "event_id": "piano-0001",
                        "expect": {"bar": True},
                    }
                ],
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "expectation_failed")
        self.assertEqual(result["details"]["field"], "bar")
        self.assertEqual(self.score, original)


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class LocateToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server
        published = self.m.score_and_roster_format()
        self.score = published["example_score"]
        self.roster = published["example_roster"]

    def _event(self, result: dict, event_id: str) -> dict:
        return next(
            event
            for event in result["events"]
            if event["source"]["event_id"] == event_id
        )

    def test_locate_returns_source_ids_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-exist"
            with (
                patch.object(self.m, "OUTPUT_DIR", output),
                patch.object(
                    self.m,
                    "render_plan",
                    side_effect=AssertionError("locate called render_plan"),
                ),
            ):
                result = self.m.locate(
                    self.score,
                    self.roster,
                    at_seconds=0.5,
                    before_seconds=0.5,
                    after_seconds=0.5,
                )
            self.assertFalse(output.exists())

        self.assertTrue(result["ok"])
        self.assertEqual(result["instrument_scope"], "formal")
        event = self._event(result, "piano-0001")
        self.assertTrue(event["source"]["stable_identity"])
        self.assertEqual(event["logical"]["bar"], 1)
        self.assertEqual(event["logical"]["beat"], 1.0)
        self.assertTrue(event["relation"]["active_at_anchor"])
        self.assertFalse(
            result["time_semantics"]["audible_tail_included"]
        )

    def test_logical_position_is_seed_independent_but_schedule_can_move(self) -> None:
        first = self.m.locate(
            self.score,
            self.roster,
            at_seconds=1.0,
            before_seconds=2.0,
            after_seconds=2.0,
            seed=1,
        )
        second = self.m.locate(
            self.score,
            self.roster,
            at_seconds=1.0,
            before_seconds=2.0,
            after_seconds=2.0,
            seed=2,
        )
        a = self._event(first, "piano-0002")
        b = self._event(second, "piano-0002")

        self.assertEqual(a["logical"], b["logical"])
        self.assertNotEqual(
            a["scheduled"]["start_seconds"],
            b["scheduled"]["start_seconds"],
        )

    def test_part_filter_and_event_limit_are_enforced(self) -> None:
        result = self.m.locate(
            self.score,
            self.roster,
            at_seconds=1.0,
            before_seconds=2.0,
            after_seconds=2.0,
            part_ids=["Piano"],
            max_events=1,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(result["summary"]["truncated"])
        self.assertTrue(
            all(
                event["source"]["part_id"] == "Piano"
                for event in result["events"]
            )
        )

    def test_time_beyond_plan_is_rejected(self) -> None:
        result = self.m.locate(
            self.score,
            self.roster,
            at_seconds=999,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["issues"][0]["code"],
            "query.time_out_of_range",
        )


if __name__ == "__main__":
    unittest.main()
