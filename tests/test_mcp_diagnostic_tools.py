"""MCP-facing runtime, project-readiness and restore-planning tools."""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from unittest import mock


_HAS_MCP = importlib.util.find_spec("mcp") is not None


def _resource_result(
    instrument_ids: list[str],
    *,
    ready: bool,
) -> dict[str, object]:
    instruments = [
        {
            "instrument_id": instrument_id,
            "status": "ready" if ready else "missing",
            "check_level": "sfz_references",
            "asset_backed": True,
            "resource_family": "test-family",
        }
        for instrument_id in instrument_ids
    ]
    issues = (
        []
        if ready
        else [
            {
                "severity": "error",
                "code": "resource.missing",
                "stage": "resources",
                "message": "Required instrument resources are missing.",
                "instrument_id": instrument_ids[0],
                "resource_family": "test-family",
            }
        ]
    )
    return {
        "kind": "tianlai.instrument_resource_readiness_result",
        "schema_version": 1,
        "ok": ready,
        "status": "ready" if ready else "missing",
        "resource_references_ready": ready,
        "render_environment_ready": True,
        "verify_references": True,
        "network": False,
        "persistent_writes": False,
        "summary": {
            "required_count": len(instruments),
            "ready_count": len(instruments) if ready else 0,
            "missing_count": 0 if ready else len(instruments),
            "invalid_count": 0,
            "unlisted_count": 0,
        },
        "instruments": instruments,
        "render_environment": {
            "ready_for_render_attempt": True,
            "python_supported": True,
            "platform_supported": True,
            "macos_translation_identity_check_performed": False,
            "output": {
                "status": "estimated_ready",
                "writable": None,
                "writable_estimate": True,
                "probe_performed": False,
                "verification": "passive_estimate",
            },
            "active_write_probe_performed": False,
        },
        "restore_plan_handoff": {
            "instrument_ids": [] if ready else list(instrument_ids),
        },
        "issues": issues,
        "issue_counts": {} if ready else {"error": 1},
        "issues_truncated": False,
    }


@unittest.skipUnless(_HAS_MCP, "optional mcp package is not installed")
class ProjectReadinessToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from tianlai import mcp_server

        self.m = mcp_server
        published = self.m.score_and_roster_format()
        self.score = copy.deepcopy(published["example_score"])
        self.roster = copy.deepcopy(published["example_roster"])

    def test_ready_project_matches_validation_without_rendering(self) -> None:
        observed_ids: list[str] = []

        def ready_resources(_layout, instrument_ids, **_kwargs):
            observed_ids.extend(instrument_ids)
            return _resource_result(list(instrument_ids), ready=True)

        with (
            mock.patch.object(
                self.m,
                "collect_instrument_resource_readiness",
                side_effect=ready_resources,
            ),
            mock.patch.object(self.m, "render_plan") as render_plan,
        ):
            readiness = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )
            validation = self.m.validate_project(
                self.score,
                self.roster,
                trusted_only=False,
            )

        self.assertTrue(readiness["ok"], readiness)
        self.assertTrue(readiness["validation_ok"])
        self.assertTrue(readiness["resource_references_ready"])
        self.assertTrue(readiness["ready_for_render_attempt"])
        self.assertFalse(readiness["audio_probe_performed"])
        self.assertFalse(readiness["audio_rendered"])
        self.assertEqual(readiness["project"], validation["project"])
        self.assertEqual(
            readiness["project_review"],
            validation["project_review"],
        )
        self.assertTrue(readiness["self_check"]["can_proceed"])
        self.assertEqual(readiness["self_check"]["blocking_count"], 0)
        self.assertEqual(
            readiness["render_preflight"],
            validation["render_preflight"],
        )
        self.assertEqual(
            readiness["render_handoff"],
            validation["render_handoff"],
        )
        self.assertEqual(observed_ids, sorted(set(observed_ids)))
        self.assertEqual(
            set(observed_ids),
            {"管弦乐/木管组/长笛", "键盘乐器/钢琴"},
        )
        render_plan.assert_not_called()

    def test_runtime_and_project_diagnosis_never_start_active_probes(self) -> None:
        forbidden = AssertionError("strictly passive MCP diagnosis")
        with (
            mock.patch(
                "tianlai.doctor._directory_writability",
                side_effect=forbidden,
            ) as write_probe,
            mock.patch(
                "tianlai.doctor._find_bsdtar_executable",
                side_effect=forbidden,
            ) as archive_probe,
            mock.patch(
                "tianlai.doctor._load_native_fluidsynth_library",
                side_effect=forbidden,
            ) as native_probe,
            mock.patch(
                "tianlai.doctor._passive_platform_identity",
                return_value=(
                    "Linux",
                    "6.0.0",
                    "x86_64",
                    "Linux-6.0.0-x86_64",
                ),
            ),
            mock.patch(
                "tianlai.doctor._probe_macos_rosetta_translation",
                side_effect=forbidden,
            ) as rosetta_probe,
            mock.patch("subprocess.Popen", side_effect=forbidden) as popen,
            mock.patch("subprocess.run", side_effect=forbidden) as run,
            mock.patch(
                "subprocess.check_output",
                side_effect=forbidden,
            ) as check_output,
            mock.patch(
                "subprocess.check_call",
                side_effect=forbidden,
            ) as check_call,
            mock.patch("subprocess.call", side_effect=forbidden) as call,
        ):
            runtime = self.m.diagnose_runtime()
            readiness = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        self.assertTrue(runtime["ok"], runtime)
        # The source archive intentionally contains no multi-gigabyte audio
        # libraries.  This test owns the passive-probe contract, not the local
        # resource-installation state, so compilation must pass while readiness
        # may truthfully remain blocked until the selected resources are present.
        self.assertTrue(readiness["validation_ok"], readiness)
        self.assertTrue(readiness["render_environment_ready"], readiness)
        self.assertTrue(readiness["project_review"]["continuation_allowed"])
        for probe in (
            write_probe,
            archive_probe,
            native_probe,
            rosetta_probe,
            popen,
            run,
            check_output,
            check_call,
            call,
        ):
            probe.assert_not_called()
        self.assertFalse(any(runtime["active_probes"].values()))
        self.assertFalse(any(readiness["active_probes"].values()))

    def test_passive_intel_macos_diagnosis_verifies_native_process(self) -> None:
        with (
            mock.patch(
                "tianlai.doctor._passive_platform_identity",
                return_value=(
                    "Darwin",
                    "25.0.0",
                    "x86_64",
                    "Darwin-25.0.0-x86_64",
                ),
            ),
            mock.patch(
                "tianlai.doctor._probe_macos_rosetta_translation",
                return_value=False,
            ) as rosetta_probe,
        ):
            runtime = self.m.diagnose_runtime()
            readiness = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        platform_check = runtime["checks"]["platform"]
        self.assertTrue(platform_check["supported"], runtime)
        self.assertEqual(platform_check["status"], "ready")
        self.assertEqual(platform_check["machine"], "x86_64")
        self.assertEqual(platform_check["rosetta"]["status"], "native")
        self.assertFalse(platform_check["rosetta"]["translated"])
        self.assertTrue(
            platform_check["rosetta"]["identity_check_performed"]
        )
        self.assertTrue(runtime["core_ready"], runtime)
        self.assertTrue(readiness["render_environment_ready"], readiness)
        self.assertTrue(runtime["passive_checks"]["macos_translation_identity"])
        self.assertTrue(
            readiness["passive_checks"]["macos_translation_identity"]
        )
        self.assertFalse(any(runtime["active_probes"].values()))
        self.assertFalse(any(readiness["active_probes"].values()))
        self.assertEqual(rosetta_probe.call_count, 2)

    def test_passive_rosetta_process_is_blocked_before_render(self) -> None:
        with (
            mock.patch(
                "tianlai.doctor._passive_platform_identity",
                return_value=(
                    "Darwin",
                    "25.0.0",
                    "x86_64",
                    "Darwin-25.0.0-x86_64",
                ),
            ),
            mock.patch(
                "tianlai.doctor._probe_macos_rosetta_translation",
                return_value=True,
            ) as rosetta_probe,
        ):
            runtime = self.m.diagnose_runtime()
            readiness = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        platform_check = runtime["checks"]["platform"]
        self.assertFalse(platform_check["supported"], runtime)
        self.assertEqual(platform_check["status"], "unsupported")
        self.assertEqual(platform_check["rosetta"]["status"], "translated")
        self.assertTrue(platform_check["rosetta"]["translated"])
        self.assertFalse(runtime["core_ready"], runtime)
        self.assertFalse(readiness["render_environment_ready"], readiness)
        self.assertFalse(readiness["ready_for_render_attempt"], readiness)
        self.assertIn(
            "runtime.platform_unsupported_or_unverified",
            {issue["code"] for issue in readiness["issues"]},
        )
        self.assertEqual(rosetta_probe.call_count, 2)

    def test_unverifiable_intel_macos_identity_fails_closed(self) -> None:
        with (
            mock.patch(
                "tianlai.doctor._passive_platform_identity",
                return_value=(
                    "Darwin",
                    "25.0.0",
                    "x86_64",
                    "Darwin-25.0.0-x86_64",
                ),
            ),
            mock.patch(
                "tianlai.doctor._probe_macos_rosetta_translation",
                side_effect=OSError("sysctl unavailable"),
            ) as rosetta_probe,
        ):
            runtime = self.m.diagnose_runtime()
            readiness = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        platform_check = runtime["checks"]["platform"]
        self.assertFalse(platform_check["supported"], runtime)
        self.assertEqual(platform_check["rosetta"]["status"], "unknown")
        self.assertFalse(runtime["core_ready"], runtime)
        self.assertFalse(readiness["render_environment_ready"], readiness)
        self.assertFalse(readiness["ready_for_render_attempt"], readiness)
        self.assertEqual(rosetta_probe.call_count, 2)

    def test_missing_project_resource_blocks_attempt_and_hands_off_ids(self) -> None:
        with mock.patch.object(
            self.m,
            "collect_instrument_resource_readiness",
            side_effect=lambda _layout, ids, **_kwargs: _resource_result(
                list(ids),
                ready=False,
            ),
        ):
            result = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        self.assertTrue(result["validation_ok"], result)
        self.assertFalse(result["resource_references_ready"])
        self.assertFalse(result["ready_for_render_attempt"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["restore_plan_handoff"]["instrument_ids"])
        self.assertIn("resource.missing", {item["code"] for item in result["issues"]})

    def test_unwritable_output_estimate_blocks_render_attempt(self) -> None:
        resource_result = _resource_result(
            ["管弦乐/木管组/长笛", "键盘乐器/钢琴"],
            ready=True,
        )
        resource_result["render_environment_ready"] = False
        resource_result["render_environment"]["ready_for_render_attempt"] = False
        resource_result["render_environment"]["output"].update(
            {
                "status": "unavailable",
                "writable_estimate": False,
            }
        )
        resource_result["issues"] = [
            {
                "severity": "error",
                "code": "layout.output_not_writable",
                "stage": "render_environment",
                "message": "The configured Tianlai output location is not writable.",
            }
        ]
        resource_result["issue_counts"] = {"error": 1}

        with mock.patch.object(
            self.m,
            "collect_instrument_resource_readiness",
            return_value=resource_result,
        ):
            result = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        self.assertTrue(result["validation_ok"])
        self.assertTrue(result["resource_references_ready"])
        self.assertFalse(result["render_environment_ready"])
        self.assertFalse(result["ready_for_render_attempt"])
        self.assertEqual(result["checks"]["render_environment"]["status"], "failed")

    def test_invalid_roster_blocks_resource_scan(self) -> None:
        with mock.patch.object(
            self.m,
            "collect_instrument_resource_readiness",
        ) as resource_check:
            result = self.m.check_project_readiness(
                self.score,
                {"name": "invalid", "assignments": []},
                trusted_only=False,
            )

        self.assertFalse(result["validation_ok"])
        self.assertFalse(result["ready_for_render_attempt"])
        self.assertEqual(result["resources"]["status"], "blocked")
        self.assertEqual(
            result["resources"]["blocked_by"],
            ["roster_document"],
        )
        resource_check.assert_not_called()

    def test_project_documents_and_private_names_are_not_echoed(self) -> None:
        self.score["title"] = "PRIVATE_SCORE_SENTINEL"
        self.roster["name"] = "PRIVATE_ROSTER_SENTINEL"
        with mock.patch.object(
            self.m,
            "collect_instrument_resource_readiness",
            side_effect=lambda _layout, ids, **_kwargs: _resource_result(
                list(ids),
                ready=True,
            ),
        ):
            result = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
            )

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE_SCORE_SENTINEL", encoded)
        self.assertNotIn("PRIVATE_ROSTER_SENTINEL", encoded)
        self.assertNotIn('"parts"', encoded)
        self.assertNotIn('"assignments"', encoded)

    def test_issue_limit_bounds_the_complete_response_without_duplication(self) -> None:
        resource_result = _resource_result(
            ["管弦乐/木管组/长笛", "键盘乐器/钢琴"],
            ready=False,
        )
        resource_result["issues"] = [
            {
                "severity": "error",
                "code": f"resource.missing.{index}",
                "stage": "resources",
                "message": "Required instrument resources are missing.",
            }
            for index in range(2)
        ]
        resource_result["issue_counts"] = {"error": 2}
        resource_result["issues_truncated"] = True

        with mock.patch.object(
            self.m,
            "collect_instrument_resource_readiness",
            return_value=resource_result,
        ) as resource_check:
            result = self.m.check_project_readiness(
                self.score,
                self.roster,
                trusted_only=False,
                max_issues=1,
            )

        self.assertLessEqual(len(result["issues"]), 1)
        self.assertTrue(result["issues_truncated"])
        self.assertEqual(result["issue_counts"]["error"], 2)
        self.assertNotIn("issues", result["resources"])
        self.assertEqual(result["resources"]["issues_reported_at"], "$.issues")
        self.assertEqual(resource_check.call_args.kwargs["max_issues"], 1)


if __name__ == "__main__":
    unittest.main()
