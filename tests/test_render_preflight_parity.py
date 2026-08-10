from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tianlai.cli import main as cli_main
from tianlai.resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    validate_render_request_resource_limits,
)
from tianlai.space import SpaceConfig


PROJECT = Path(__file__).resolve().parents[1]
_HAS_MCP = importlib.util.find_spec("mcp") is not None


def _long_score() -> dict:
    return {
        "schema_version": 1,
        "title": "resource-preflight-parity",
        "sample_rate": 48_000,
        "tail_seconds": 1.0,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 120.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "Tone",
                "name": "Tone",
                "notes": [
                    {
                        "event_id": "tone-0001",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 600.0,
                        "pitch": "C4",
                        "velocity": 0.5,
                    }
                ],
            }
        ],
    }


def _roster() -> dict:
    return {
        "name": "resource-preflight-parity",
        "collaboration": {"mode": "analyze"},
        "assignments": [
            {
                "part": "Tone",
                "executor_id": "tone",
                "instrument": "电子乐器/合成器主音",
            }
        ],
    }


class RenderResourceRequestTests(unittest.TestCase):
    class _Plan:
        duration_seconds = 10.0
        sample_rate = 8_000
        parts = (object(), object())
        collaboration = type("_Collaboration", (), {"mode": "analyze"})()

    def test_stems_change_disk_estimate_but_not_peak_memory(self) -> None:
        without_stems = validate_render_request_resource_limits(
            self._Plan(),
            write_stems=False,
            space=None,
            collaboration_mode=None,
            stem_cache_enabled=False,
        )
        with_stems = validate_render_request_resource_limits(
            self._Plan(),
            write_stems=True,
            space=None,
            collaboration_mode=None,
            stem_cache_enabled=True,
        )

        self.assertEqual(
            without_stems["estimated_audio_memory_bytes"],
            with_stems["estimated_audio_memory_bytes"],
        )
        self.assertGreater(
            with_stems["estimated_primary_output_bytes"],
            without_stems["estimated_primary_output_bytes"],
        )
        self.assertEqual(
            with_stems["render_parameters"]["collaboration_mode"],
            "analyze",
        )
        self.assertTrue(
            with_stems["render_parameters"]["analysis_enabled"]
        )
        self.assertIn(
            "sequentially",
            with_stems["memory_model"]["write_stems"],
        )
        self.assertIn(
            "memmap",
            with_stems["memory_model"]["collaboration_analysis"],
        )

    def test_failed_gate_retains_the_complete_current_request_report(
        self,
    ) -> None:
        with self.assertRaises(ResourceLimitError) as raised:
            validate_render_request_resource_limits(
                self._Plan(),
                write_stems=True,
                space=SpaceConfig(),
                collaboration_mode="suggest",
                stem_cache_enabled=True,
                limits=ProjectLimits(max_audio_memory_bytes=1),
            )

        report = raised.exception.preflight
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["render_parameters"]["space_enabled"])
        self.assertEqual(
            report["render_parameters"]["collaboration_mode"],
            "suggest",
        )
        self.assertEqual(
            report["gates"]["estimated_audio_memory_bytes"]["status"],
            "failed",
        )


class CliRenderPreflightParityTests(unittest.TestCase):
    def test_plan_only_uses_the_same_default_hall_gate_as_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            score_path = base / "score.json"
            roster_path = base / "roster.json"
            output = base / "plan"
            score_path.write_text(
                json.dumps(_long_score(), ensure_ascii=False),
                encoding="utf-8",
            )
            roster_path.write_text(
                json.dumps(_roster(), ensure_ascii=False),
                encoding="utf-8",
            )
            common = [
                "ensemble",
                "--score",
                str(score_path),
                "--roster",
                str(roster_path),
                "--root",
                str(PROJECT / "乐器"),
                "--output",
                str(output),
                "--plan-only",
            ]

            failed_stderr = io.StringIO()
            with contextlib.redirect_stderr(failed_stderr):
                failed = cli_main(common)

            self.assertEqual(failed, 2)
            self.assertFalse(output.exists())
            self.assertIn("render_preflight", failed_stderr.getvalue())
            self.assertIn('"space_enabled": true', failed_stderr.getvalue())

            passed_stdout = io.StringIO()
            with contextlib.redirect_stdout(passed_stdout):
                passed = cli_main([*common, "--dry", "--no-stems"])

            self.assertEqual(passed, 0)
            report = json.loads(
                (output / "资源预检.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["render_parameters"]["space_enabled"])
            self.assertFalse(report["render_parameters"]["write_stems"])
            review = json.loads(
                (output / "创作自检.json").read_text(encoding="utf-8")
            )
            self.assertTrue(review["continuation_allowed"])
            self.assertEqual(review["blocking_count"], 0)
            self.assertIn("performance_plan_sha256", review["binding"])
            self.assertIn("资源预检:", passed_stdout.getvalue())
            self.assertIn("创作自检:", passed_stdout.getvalue())


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class McpRenderPreflightParityTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server

    def test_validate_and_render_reject_the_same_default_profile(self) -> None:
        score = _long_score()
        roster = _roster()

        validated = self.m.validate_project(
            copy.deepcopy(score),
            copy.deepcopy(roster),
            trusted_only=False,
        )

        self.assertFalse(validated["ok"])
        self.assertEqual(validated["render_preflight"]["status"], "failed")
        self.assertTrue(
            validated["render_preflight"]["render_parameters"][
                "space_enabled"
            ]
        )
        self.assertEqual(
            validated["render_preflight"]["render_parameters"][
                "collaboration_mode"
            ],
            "analyze",
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "mcp"
            with patch.object(self.m, "OUTPUT_DIR", output):
                rendered = self.m.render(
                    copy.deepcopy(score),
                    copy.deepcopy(roster),
                    title="must-not-render",
                    trusted_only=False,
                    render_profile=validated["render_handoff"][
                        "render_profile"
                    ],
                    expected_render_profile_sha256=validated[
                        "render_handoff"
                    ]["expected_render_profile_sha256"],
                )

            self.assertFalse(output.exists())

        self.assertFalse(rendered["ok"])
        self.assertEqual(
            rendered["render_preflight"][
                "estimated_audio_memory_bytes"
            ],
            validated["render_preflight"][
                "estimated_audio_memory_bytes"
            ],
        )
        self.assertEqual(
            rendered["render_preflight"]["render_parameters"],
            validated["render_preflight"]["render_parameters"],
        )
        self.assertEqual(
            rendered["render_preflight"],
            validated["render_preflight"],
        )
        self.assertFalse(rendered["render_preflight"]["passed"])
        self.assertEqual(
            rendered["render_profile_sha256"],
            validated["settings"]["render_profile_canonical_sha256"],
        )

    def test_same_dry_profile_passes_and_reports_its_exact_options(
        self,
    ) -> None:
        result = self.m.validate_project(
            _long_score(),
            _roster(),
            trusted_only=False,
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["render_preflight"]["passed"])
        self.assertEqual(
            result["render_preflight"]["render_parameters"],
            {
                "write_stems": False,
                "space_enabled": False,
                "hall_tail_seconds": 0.0,
                "collaboration_mode": "manual",
                "analysis_enabled": False,
                "stem_cache_enabled": False,
            },
        )
        self.assertLess(
            result["render_preflight"]["estimated_audio_memory_bytes"],
            2 * 1024 * 1024 * 1024,
        )
        self.assertEqual(
            result["render_handoff"]["render_profile"],
            result["settings"]["render_profile"],
        )
        self.assertEqual(
            result["render_handoff"][
                "expected_render_profile_sha256"
            ],
            result["settings"]["render_profile_canonical_sha256"],
        )

    def test_render_handoff_rejects_a_changed_profile_before_output(
        self,
    ) -> None:
        validated = self.m.validate_project(
            _long_score(),
            _roster(),
            trusted_only=False,
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )
        handoff = validated["render_handoff"]

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "mcp"
            with patch.object(self.m, "OUTPUT_DIR", output):
                rendered = self.m.render(
                    copy.deepcopy(_long_score()),
                    copy.deepcopy(_roster()),
                    title="profile-mismatch",
                    trusted_only=False,
                    render_profile=handoff["render_profile"],
                    expected_render_profile_sha256=handoff[
                        "expected_render_profile_sha256"
                    ],
                    hall=True,
                )

            self.assertFalse(output.exists())

        self.assertFalse(rendered["ok"])
        self.assertEqual(
            rendered["code"],
            "render_profile.preflight_mismatch",
        )
        self.assertNotEqual(
            rendered["render_profile_sha256"],
            rendered["expected_render_profile_sha256"],
        )

    def test_render_handoff_rejects_a_malformed_expected_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "mcp"
            with patch.object(self.m, "OUTPUT_DIR", output):
                rendered = self.m.render(
                    _long_score(),
                    _roster(),
                    trusted_only=False,
                    expected_render_profile_sha256="NOT-A-HASH",
                )

            self.assertFalse(output.exists())

        self.assertEqual(
            rendered["code"],
            "render_profile.invalid_expected_sha256",
        )

    def test_invalid_issue_limit_keeps_the_stable_error_code(self) -> None:
        result = self.m.validate_project(
            _long_score(),
            _roster(),
            trusted_only=False,
            max_issues=0,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["code"], "query.invalid_limit")


if __name__ == "__main__":
    unittest.main()
