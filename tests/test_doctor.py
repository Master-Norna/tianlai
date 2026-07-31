from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tianlai import __version__
from tianlai.doctor import (
    _python_runtime_supported,
    collect_doctor_report,
    doctor_report_json,
    main,
)
from tianlai.runtime_layout import RuntimeLayout


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class DoctorTests(unittest.TestCase):
    def test_supported_python_runtime_matches_bootstrap_contract(self) -> None:
        for version in ((3, 11), (3, 12), (3, 13), (3, 14)):
            with self.subTest(version=version):
                self.assertTrue(
                    _python_runtime_supported(
                        implementation="CPython",
                        version=version,
                        bits=64,
                    )
                )
        for implementation, version, bits in (
            ("CPython", (3, 10), 64),
            ("CPython", (3, 15), 64),
            ("CPython", (3, 14), 32),
            ("PyPy", (3, 11), 64),
        ):
            with self.subTest(
                implementation=implementation,
                version=version,
                bits=bits,
            ):
                self.assertFalse(
                    _python_runtime_supported(
                        implementation=implementation,
                        version=version,
                        bits=bits,
                    )
                )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tianlai_doctor_")
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "乐器"
        self.catalog.mkdir()
        _write_json(
            self.root / "可信乐器.json",
            {"trusted": ["管弦乐/样本乐器"]},
        )
        self.layout = RuntimeLayout(
            home=self.root,
            catalog=self.catalog,
            allowlist=self.root / "可信乐器.json",
            schemas=self.root / "schemas",
            resources=self.root / "音源",
            output=self.root / "output",
            source="test",
            catalog_ready=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_self_contained_test_utility(self) -> None:
        _write_json(
            self.catalog / "测试工具" / "参考振荡器" / "乐器.json",
            {
                "name": "参考振荡器",
                "type": "oscillator",
                "note_min": 0,
                "note_max": 127,
            },
        )

    def _write_sample_manifest(self, asset_root: str = "../../../音源/库") -> Path:
        path = self.catalog / "管弦乐" / "样本乐器" / "乐器.json"
        _write_json(
            path,
            {
                "name": "测试样本乐器",
                "type": "dedicated_sfz",
                "asset_root": asset_root,
                "evidence_files": ["LICENSE"],
                "articulations": {"normal": "instrument.sfz"},
                "default_articulation": "normal",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            },
        )
        return path

    def test_clean_source_without_audio_resources_remains_diagnosable(self) -> None:
        self._write_self_contained_test_utility()
        self._write_sample_manifest()

        report = collect_doctor_report(layout=self.layout)

        self.assertEqual(report["catalog"]["status"], "ready")
        self.assertEqual(report["summary"]["catalog_count"], 2)
        self.assertEqual(report["summary"]["production_count"], 1)
        self.assertEqual(report["summary"]["resource_ready_count"], 0)
        self.assertEqual(report["summary"]["resource_missing_count"], 1)
        self.assertEqual(report["summary"]["test_utility_count"], 1)
        self.assertEqual(report["summary"]["status"], "degraded")
        by_id = {item["id"]: item for item in report["instruments"]}
        self.assertEqual(
            by_id["测试工具/参考振荡器"]["resource"]["status"],
            "ready",
        )
        sample = by_id["管弦乐/样本乐器"]
        self.assertEqual(sample["resource"]["status"], "missing")
        self.assertEqual(
            sample["resource"]["problems"][0]["kind"],
            "asset_root",
        )
        self.assertEqual(sample["resource"]["installer"]["status"], "unavailable")
        self.assertTrue(report["writability"]["resources"]["writable"])
        self.assertFalse((self.root / "音源").exists())
        self.assertEqual(list(self.root.glob(".tianlai-write-probe-*")), [])

    def test_dedicated_check_expands_sfz_without_constructing_instrument(self) -> None:
        manifest = self._write_sample_manifest()
        asset_root = self.root / "音源" / "库"
        asset_root.mkdir(parents=True)
        (asset_root / "LICENSE").write_text("test evidence\n", encoding="utf-8")
        (asset_root / "tone.wav").write_bytes(b"not decoded by doctor")
        (asset_root / "instrument.sfz").write_text(
            "<region> sample=tone.wav key=60\n",
            encoding="utf-8",
        )

        with mock.patch(
            "tianlai.instrument.create_instrument",
            side_effect=AssertionError("doctor must not construct instruments"),
        ):
            report = collect_doctor_report(layout=self.layout)

        item = report["instruments"][0]
        self.assertEqual(Path(item["manifest"]).name, manifest.name)
        self.assertEqual(item["resource"]["status"], "ready")
        self.assertEqual(item["resource"]["check_level"], "sfz_references")

        (asset_root / "tone.wav").unlink()
        report = collect_doctor_report(layout=self.layout)
        item = report["instruments"][0]
        self.assertEqual(item["resource"]["status"], "missing")
        self.assertIn(
            "does not exist",
            item["resource"]["problems"][0]["message"],
        )

    def test_known_resource_family_reports_available_installer(self) -> None:
        self._write_sample_manifest(
            "../../../音源/VirtualPlayingOrchestra/"
            "Virtual-Playing-Orchestra3"
        )
        (self.root / "安装VPO音源.ps1").write_text(
            "# test installer\n",
            encoding="utf-8",
        )

        with mock.patch(
            "tianlai.doctor._is_windows_runtime",
            return_value=True,
        ):
            report = collect_doctor_report(layout=self.layout)

        installer = report["instruments"][0]["resource"]["installer"]
        self.assertEqual(installer["status"], "available")
        self.assertEqual(installer["installer_id"], "virtual-playing-orchestra")
        self.assertEqual(installer["path"], "安装VPO音源.ps1")
        self.assertEqual(installer["required_platform"], "Windows")

    def test_windows_only_installer_reports_platform_boundary(self) -> None:
        self._write_sample_manifest(
            "../../../音源/VirtualPlayingOrchestra/"
            "Virtual-Playing-Orchestra3"
        )
        (self.root / "安装VPO音源.ps1").write_text(
            "# test installer\n",
            encoding="utf-8",
        )

        with mock.patch(
            "tianlai.doctor._is_windows_runtime",
            return_value=False,
        ):
            report = collect_doctor_report(layout=self.layout)

        installer = report["instruments"][0]["resource"]["installer"]
        self.assertEqual(installer["status"], "unavailable_on_platform")
        self.assertEqual(installer["installer_id"], "virtual-playing-orchestra")
        self.assertEqual(installer["path"], "安装VPO音源.ps1")
        self.assertEqual(installer["required_platform"], "Windows")

    def test_json_api_is_portable_and_strict_mode_is_opt_in(self) -> None:
        self._write_sample_manifest()
        report = collect_doctor_report(layout=self.layout, verify_references=False)
        encoded = doctor_report_json(report)
        self.assertEqual(json.loads(encoded)["schema_version"], 2)
        self.assertNotIn("\\u4e50", encoded)

        with mock.patch.dict(
            os.environ,
            {"TIANLAI_HOME": str(self.root)},
            clear=False,
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(["--json", "--quick"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["summary"]["status"], "degraded")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                strict_result = main(
                    ["--json", "--quick", "--require-all-resources"]
                )
            self.assertEqual(strict_result, 2)

    def test_imported_code_version_wins_over_stale_distribution_metadata(self) -> None:
        self._write_self_contained_test_utility()

        with mock.patch(
            "tianlai.doctor.importlib.metadata.version",
            return_value="0.4.0",
        ):
            report = collect_doctor_report(layout=self.layout)

        self.assertEqual(report["version"], __version__)
        self.assertEqual(report["distribution"]["version"], "0.4.0")
        self.assertFalse(report["distribution"]["matches_imported_code"])

    def test_trusted_allowlist_reuses_fail_closed_policy(self) -> None:
        self._write_self_contained_test_utility()
        _write_json(
            self.root / "可信乐器.json",
            {"trusted": ["目录中不存在的乐器"]},
        )

        report = collect_doctor_report(layout=self.layout)

        self.assertEqual(report["trusted"]["status"], "invalid")
        self.assertIn("目录中不存在", report["trusted"]["error"])
        self.assertEqual(report["summary"]["status"], "error")
        with mock.patch.dict(
            os.environ,
            {"TIANLAI_HOME": str(self.root)},
            clear=False,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--quick"]), 1)


class ShippedDoctorTests(unittest.TestCase):
    def test_project_catalog_counts_and_lightweight_resource_split(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = collect_doctor_report(start=root, verify_references=False)
        self.assertEqual(report["catalog"]["status"], "ready")
        self.assertEqual(report["summary"]["catalog_count"], 104)
        self.assertEqual(report["summary"]["production_count"], 103)
        self.assertEqual(report["summary"]["test_utility_count"], 1)
        self.assertEqual(report["summary"]["trusted_count"], 25)
        self.assertEqual(report["summary"]["asset_backed_count"], 74)
        self.assertEqual(report["summary"]["self_contained_count"], 29)
        expected_available = 74 if os.name == "nt" else 38
        expected_platform_unavailable = 0 if os.name == "nt" else 36
        self.assertEqual(
            report["summary"]["installer_available_count"],
            expected_available,
        )
        self.assertEqual(
            report["summary"]["installer_unavailable_on_platform_count"],
            expected_platform_unavailable,
        )
        self.assertEqual(report["summary"]["installer_unavailable_count"], 0)
        self.assertEqual(report["summary"]["installer_missing_count"], 0)
        by_id = {item["id"]: item for item in report["instruments"]}
        installer = by_id["管弦乐/木管组/竖笛"]["resource"]["installer"]
        self.assertEqual(installer["status"], "available")
        self.assertEqual(installer["resource_family"], "vcsl")
        if os.name == "nt":
            self.assertEqual(
                installer["arguments"],
                ["-ResourceFamily", "vcsl"],
            )
        else:
            self.assertEqual(installer["module"], "tianlai.resource_restore")
            self.assertEqual(installer["arguments"][-2:], ["--family", "vcsl"])

    def test_non_windows_catalog_uses_python_restore_entrypoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with mock.patch(
            "tianlai.doctor._is_windows_runtime",
            return_value=False,
        ):
            report = collect_doctor_report(
                start=root,
                verify_references=False,
            )

        self.assertEqual(report["summary"]["installer_available_count"], 38)
        self.assertEqual(
            report["summary"]["installer_unavailable_on_platform_count"],
            36,
        )
        by_id = {item["id"]: item for item in report["instruments"]}
        tracked = by_id["管弦乐/木管组/竖笛"]["resource"]["installer"]
        self.assertEqual(tracked["status"], "available")
        self.assertEqual(tracked["module"], "tianlai.resource_restore")
        self.assertEqual(
            tracked["arguments"][:2],
            ["-m", "tianlai.resource_restore"],
        )
        self.assertEqual(tracked["arguments"][-2:], ["--family", "vcsl"])

        legacy = next(
            item["resource"]["installer"]
            for item in report["instruments"]
            if item["resource"]["installer"].get("installer_id")
            == "virtual-playing-orchestra"
        )
        self.assertEqual(legacy["status"], "unavailable_on_platform")
        self.assertEqual(legacy["required_platform"], "Windows")


if __name__ == "__main__":
    unittest.main()
