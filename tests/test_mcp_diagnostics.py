"""Tests for MCP-safe runtime diagnosis and restore planning."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tianlai.mcp_diagnostics import (
    _safe_relative_path,
    build_safe_resource_restore_plan,
    collect_instrument_resource_readiness,
    collect_runtime_diagnosis,
)
from tianlai.runtime_layout import RuntimeLayout


def _layout(root: Path) -> RuntimeLayout:
    return RuntimeLayout(
        home=root,
        catalog=root / "乐器",
        allowlist=root / "可信乐器.json",
        schemas=root / "schemas",
        resources=root / "音源",
        output=root / "output",
        source="working_tree",
        catalog_ready=True,
    )


def _doctor_report() -> dict[str, object]:
    sentinel = r"C:\Users\Private Person\secret-token"
    return {
        "version": "0.8.0rc1",
        "distribution": {
            "name": "tianlai-audio",
            "version": "0.8.0rc1",
            "matches_imported_code": True,
        },
        "python": {
            "version": "3.14.1",
            "implementation": "CPython",
            "executable": sentinel + r"\python.exe",
            "bits": 64,
            "supported": True,
        },
        "platform": {
            "system": "Windows",
            "normalised_machine": "x86_64",
            "supported": True,
            "platform": sentinel,
            "rosetta": {
                "status": "not_applicable",
                "translated": None,
                "error": sentinel,
            },
        },
        "capabilities": {
            "resource_restore": {
                "status": "available",
                "manifest": sentinel + r"\resource_restore_manifest.json",
                "family_count": 2,
                "instrument_count": 3,
                "archive_formats": ["zip", "7z"],
                "seven_zip_extractor": sentinel + r"\bsdtar.exe",
                "error": sentinel,
            },
            "fluidsynth": {
                "status": "available",
                "ready": True,
                "required_for_core": False,
                "library": sentinel + r"\fluidsynth.dll",
                "native": {
                    "status": "available",
                    "library": sentinel + r"\fluidsynth.dll",
                    "directory": sentinel,
                    "source": "project_local",
                    "probe": "fluidsynth",
                    "load_verified": True,
                    "error": sentinel,
                },
                "python_binding": {
                    "status": "available",
                    "module_available": True,
                    "version": "1.4.0",
                    "required_version": "1.4.0",
                    "error": sentinel,
                },
            },
        },
        "layout": {
            "home": sentinel,
            "catalog": sentinel + r"\乐器",
            "resources": sentinel + r"\音源",
            "output": sentinel + r"\output",
            "source": "working_tree",
            "catalog_ready": True,
        },
        "writability": {
            name: {
                "path": sentinel + rf"\{name}",
                "probe_directory": sentinel,
                "exists": True,
                "writable": True,
                "error": sentinel,
            }
            for name in ("home", "resources", "output")
        },
        "catalog": {"status": "ready", "count": 3, "errors": []},
        "trusted": {"status": "ready", "count": 3, "error": sentinel},
        "instruments": [
            {
                "id": "乐器/Ready",
                "production": True,
                "manifest": sentinel + r"\ready.json",
                "resource": {
                    "status": "ready",
                    "check_level": "manifest_references",
                    "asset_backed": False,
                    "asset_root": sentinel,
                    "problems": [],
                    "installer": {
                        "resource_family": None,
                        "path": sentinel,
                    },
                },
            },
            {
                "id": "乐器/Missing",
                "production": True,
                "manifest": sentinel + r"\missing.json",
                "resource": {
                    "status": "missing",
                    "check_level": "sfz_references",
                    "asset_backed": True,
                    "asset_root": sentinel,
                    "problems": [
                        {
                            "kind": "resource_reference",
                            "path": sentinel + r"\tone.wav",
                            "message": "missing " + sentinel,
                        }
                    ],
                    "installer": {
                        "resource_family": "samples",
                        "path": sentinel,
                        "arguments": [sentinel],
                    },
                },
            },
            {
                "id": "乐器/Unused",
                "production": True,
                "resource": {
                    "status": "missing",
                    "check_level": "manifest_references",
                    "asset_backed": True,
                    "problems": [{"message": sentinel, "path": sentinel}],
                    "installer": {"resource_family": "unused"},
                },
            },
        ],
        "summary": {
            "catalog_count": 3,
            "production_count": 3,
            "test_utility_count": 0,
            "trusted_count": 3,
            "trusted_ready_count": 1,
            "resource_ready_count": 1,
            "resource_missing_count": 2,
            "resource_invalid_count": 0,
            "asset_backed_count": 2,
            "self_contained_count": 1,
        },
    }


def _family(
    family_id: str,
    group: str,
    instrument: str,
    *,
    download_bytes: int,
    install_bytes: int,
) -> dict[str, object]:
    return {
        "id": family_id,
        "group": group,
        "display_name": f"Family {family_id}",
        "instrument_ids": [instrument],
        "license": {
            "expression": "CC-BY-4.0",
            "status": "approved",
        },
        "source": {
            "repository": "https://private.invalid/source",
            "commit": "a" * 40,
        },
        "archive": {
            "url": "https://private.invalid/archive.zip",
            "filename": f"{family_id}.zip",
            "estimated_bytes": download_bytes,
        },
        "install": {
            "target": f"Libraries/{family_id}",
            "tree": {"bytes": install_bytes},
            "derived": [],
        },
    }


class RuntimeDiagnosisTests(unittest.TestCase):
    def test_embedded_windows_drive_is_not_a_safe_relative_path(self) -> None:
        self.assertIsNone(_safe_relative_path("safe/C:/Users/private/secret"))
        self.assertIsNone(_safe_relative_path("safe/C:Users/private/secret"))

    def test_absolute_paths_errors_and_urls_never_cross_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = _layout(Path(temporary))
            with mock.patch(
                "tianlai.mcp_diagnostics.collect_doctor_report",
                return_value=_doctor_report(),
            ) as collect:
                result = collect_runtime_diagnosis(
                    layout,
                    check_level="references",
                    max_issues=1,
                )

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Private Person", encoded)
        self.assertNotIn("C:\\\\Users", encoded)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("secret-token", encoded)
        self.assertFalse(result["network"])
        self.assertFalse(result["persistent_writes"])
        self.assertEqual(
            result["active_probes"],
            {
                "native_library_probe": False,
                "external_program_probe": False,
                "ephemeral_writability_probe": False,
            },
        )
        self.assertEqual(
            result["passive_checks"],
            {
                "filesystem_metadata": True,
                "instrument_reference_scan": True,
                "macos_translation_identity": False,
            },
        )
        self.assertTrue(result["core_ready"])
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["issues_truncated"])
        collect.assert_called_once_with(
            layout=layout,
            verify_references=True,
            active_probes=False,
        )

    def test_unknown_check_level_is_rejected_before_doctor(self) -> None:
        with (
            mock.patch("tianlai.mcp_diagnostics.collect_doctor_report") as collect,
            self.assertRaisesRegex(ValueError, "quick.*references"),
        ):
            collect_runtime_diagnosis(mock.Mock(), check_level="deep")
        collect.assert_not_called()


class InstrumentResourceReadinessTests(unittest.TestCase):
    def test_unreferenced_missing_resource_does_not_block_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = _layout(Path(temporary))
            with mock.patch(
                "tianlai.mcp_diagnostics.collect_doctor_report",
                return_value=_doctor_report(),
            ) as collect:
                result = collect_instrument_resource_readiness(
                    layout,
                    ["乐器/Ready"],
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["required_count"], 1)
        self.assertEqual(result["instruments"][0]["instrument_id"], "乐器/Ready")
        self.assertEqual(result["issues"], [])
        collect.assert_called_once_with(
            layout=layout,
            verify_references=True,
            active_probes=False,
            selected_instrument_ids=["乐器/Ready"],
        )

    def test_missing_resource_has_safe_restore_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = _layout(Path(temporary))
            with mock.patch(
                "tianlai.mcp_diagnostics.collect_doctor_report",
                return_value=_doctor_report(),
            ):
                result = collect_instrument_resource_readiness(
                    layout,
                    ["乐器/Missing", "乐器/Missing"],
                )

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["summary"]["required_count"], 1)
        self.assertEqual(
            result["restore_plan_handoff"],
            {"instrument_ids": ["乐器/Missing"]},
        )
        self.assertNotIn("Private Person", encoded)
        self.assertNotIn("secret-token", encoded)

    def test_passive_output_denial_blocks_environment_not_resource_contract(self) -> None:
        report = _doctor_report()

        with tempfile.TemporaryDirectory() as temporary:
            layout = _layout(Path(temporary))
            layout.output.mkdir()
            (layout.output / "mcp").write_text("not a directory", encoding="utf-8")
            with mock.patch(
                "tianlai.mcp_diagnostics.collect_doctor_report",
                return_value=report,
            ):
                result = collect_instrument_resource_readiness(
                    layout,
                    ["乐器/Ready"],
                )

        self.assertTrue(result["resource_references_ready"])
        self.assertFalse(result["render_environment_ready"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "environment_blocked")
        self.assertIn(
            "layout.output_not_writable",
            {item["code"] for item in result["issues"]},
        )


class SafeRestorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.layout = _layout(Path(self.temporary.name))
        self.manifest = {
            "totals": {},
            "families": [
                _family("alpha", "keys", "乐器/A", download_bytes=10, install_bytes=20),
                _family("beta", "strings", "乐器/B", download_bytes=30, install_bytes=40),
                _family("gamma", "winds", "乐器/C", download_bytes=50, install_bytes=60),
            ],
        }

    def test_selection_union_uses_manifest_order_and_keeps_license_only(self) -> None:
        with mock.patch(
            "tianlai.mcp_diagnostics.load_restore_manifest",
            return_value=self.manifest,
        ):
            result = build_safe_resource_restore_plan(
                self.layout,
                instrument_ids=["乐器/B"],
                family_ids=["alpha"],
                groups=["winds"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["selection"]["selected_family_ids"],
            ["alpha", "beta", "gamma"],
        )
        self.assertEqual(result["summary"]["estimated_download_bytes"], 90)
        self.assertEqual(result["summary"]["additional_installed_bytes"], 120)
        self.assertTrue(
            all(item["license"] == {"expression": "CC-BY-4.0", "status": "approved"} for item in result["families"])
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("a" * 40, encoded)

    def test_unknown_selectors_are_structured_and_known_subset_is_planned(self) -> None:
        with mock.patch(
            "tianlai.mcp_diagnostics.load_restore_manifest",
            return_value=self.manifest,
        ):
            result = build_safe_resource_restore_plan(
                self.layout,
                instrument_ids=["乐器/B", "乐器/Unknown"],
                family_ids=["missing-family"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["selection"]["selected_family_ids"], ["beta"])
        self.assertEqual(
            {item["code"] for item in result["issues"]},
            {"selection.unknown_instrument", "selection.unknown_family"},
        )

    def test_planning_never_downloads_restores_or_creates_resource_root(self) -> None:
        with (
            mock.patch(
                "tianlai.mcp_diagnostics.load_restore_manifest",
                return_value=self.manifest,
            ),
            mock.patch("tianlai.resource_restore.urlopen") as network,
            mock.patch("tianlai.resource_restore.download_archive") as download,
            mock.patch("tianlai.resource_restore.restore_family") as restore,
        ):
            result = build_safe_resource_restore_plan(self.layout)

        self.assertTrue(result["ok"])
        self.assertFalse(result["network"])
        self.assertFalse(result["persistent_writes"])
        self.assertFalse(result["downloads_started"])
        self.assertFalse(result["restore_started"])
        self.assertFalse(self.layout.resources.exists())
        network.assert_not_called()
        download.assert_not_called()
        restore.assert_not_called()

    def test_selector_count_is_bounded_before_manifest_work(self) -> None:
        with (
            mock.patch(
                "tianlai.mcp_diagnostics.load_restore_manifest",
            ) as load_manifest,
            self.assertRaisesRegex(ValueError, "at most 128"),
        ):
            build_safe_resource_restore_plan(
                self.layout,
                instrument_ids=[f"乐器/{index}" for index in range(129)],
            )

        load_manifest.assert_not_called()

    def test_selector_deduplication_preserves_first_seen_order(self) -> None:
        with mock.patch(
            "tianlai.mcp_diagnostics.load_restore_manifest",
            return_value=self.manifest,
        ):
            result = build_safe_resource_restore_plan(
                self.layout,
                instrument_ids=["乐器/B", "乐器/B", "乐器/A"],
            )

        self.assertEqual(
            result["selection"]["instrument_ids"],
            ["乐器/B", "乐器/A"],
        )
        self.assertEqual(
            result["selection"]["selected_family_ids"],
            ["alpha", "beta"],
        )


if __name__ == "__main__":
    unittest.main()
