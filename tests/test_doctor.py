from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tianlai.doctor as doctor_module
from tianlai import __version__
from tianlai.doctor import (
    REPORT_SCHEMA_VERSION,
    _platform_runtime_supported,
    _python_runtime_supported,
    _relative_or_absolute,
    collect_doctor_report,
    doctor_report_json,
    main,
)
from tianlai.runtime_layout import RuntimeLayout


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class DoctorTests(unittest.TestCase):
    def test_relative_path_display_survives_identity_resolution_errors(
        self,
    ) -> None:
        path = Path("unresolvable")
        with mock.patch.object(
            Path,
            "resolve",
            side_effect=OSError("simulated identity failure"),
        ):
            self.assertEqual(
                _relative_or_absolute(path, Path("root")),
                str(path),
            )

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

    def test_supported_platforms_match_the_public_native_architecture_contract(
        self,
    ) -> None:
        for system, machine in (
            ("Windows", "AMD64"),
            ("Linux", "x86_64"),
            ("Darwin", "arm64"),
            ("Darwin", "aarch64"),
            ("Darwin", "x86_64"),
        ):
            with self.subTest(system=system, machine=machine):
                self.assertTrue(
                    _platform_runtime_supported(
                        system=system,
                        machine=machine,
                        bits=64,
                    )
                )
        for system, machine, bits in (
            ("Windows", "ARM64", 64),
            ("Linux", "aarch64", 64),
            ("Darwin", "i386", 32),
            ("FreeBSD", "x86_64", 64),
        ):
            with self.subTest(system=system, machine=machine, bits=bits):
                self.assertFalse(
                    _platform_runtime_supported(
                        system=system,
                        machine=machine,
                        bits=bits,
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

    def test_fluidsynth_capability_reports_homebrew_native_and_binding_layers(
        self,
    ) -> None:
        prefix = self.root / "Homebrew prefix"
        runtime = prefix / "opt" / "fluid-synth" / "lib"
        runtime.mkdir(parents=True)
        library = runtime / "libfluidsynth.dylib"
        library.write_bytes(b"test dylib placeholder")

        with (
            mock.patch(
                "tianlai.soundfont._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.soundfont._is_macos_runtime",
                return_value=True,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "HOMEBREW_PREFIX": str(prefix),
                    "TIANLAI_FLUIDSYNTH_DIR": "",
                },
                clear=False,
            ),
            mock.patch(
                "tianlai.doctor.importlib.util.find_spec",
                return_value=object(),
            ),
            mock.patch(
                "tianlai.doctor.importlib.metadata.version",
                return_value="1.4.0",
            ),
            mock.patch(
                "tianlai.doctor.ctypes.util.find_library",
                return_value=None,
            ) as system_lookup,
            mock.patch(
                "tianlai.doctor._load_native_fluidsynth_library",
            ) as native_load,
        ):
            capability = doctor_module._fluidsynth_capability(self.layout)

        self.assertEqual(capability["status"], "available")
        self.assertTrue(capability["ready"])
        self.assertFalse(capability["required_for_core"])
        self.assertEqual(capability["library"], str(library.resolve()))
        self.assertEqual(capability["native"]["status"], "available")
        self.assertTrue(capability["native"]["load_verified"])
        self.assertEqual(capability["native"]["source"], "homebrew")
        self.assertEqual(capability["native"]["directory"], str(runtime.resolve()))
        self.assertEqual(capability["python_binding"]["status"], "available")
        self.assertEqual(capability["python_binding"]["version"], "1.4.0")
        native_load.assert_called_once_with(library.resolve())
        system_lookup.assert_not_called()

    def test_fluidsynth_report_labels_project_and_environment_directories(
        self,
    ) -> None:
        project_runtime = self.root / "音源" / "通用" / "fluidsynth" / "lib"
        override = self.root / "external FluidSynth"
        with mock.patch.dict(
            os.environ,
            {"TIANLAI_FLUIDSYNTH_DIR": str(override)},
            clear=False,
        ):
            self.assertEqual(
                doctor_module._fluidsynth_directory_source(
                    project_runtime,
                    self.root,
                ),
                "project_local",
            )
            self.assertEqual(
                doctor_module._fluidsynth_directory_source(
                    override,
                    self.root,
                ),
                "environment_override",
            )

    def test_fluidsynth_capability_keeps_native_and_missing_binding_distinct(
        self,
    ) -> None:
        def locate(name: str) -> str | None:
            if name == "libfluidsynth-3":
                return "libfluidsynth.3.dylib"
            return None

        with (
            mock.patch(
                "tianlai.doctor._find_project_fluidsynth_directory",
                return_value=None,
            ),
            mock.patch(
                "tianlai.doctor.ctypes.util.find_library",
                side_effect=locate,
            ),
            mock.patch(
                "tianlai.doctor._load_native_fluidsynth_library",
            ) as native_load,
            mock.patch.dict(doctor_module.sys.modules) as modules,
            mock.patch(
                "tianlai.doctor.importlib.util.find_spec",
                return_value=None,
            ),
            mock.patch(
                "tianlai.doctor.importlib.metadata.version",
                side_effect=doctor_module.importlib.metadata.PackageNotFoundError,
            ),
        ):
            modules.pop("fluidsynth", None)
            capability = doctor_module._fluidsynth_capability(self.layout)

        self.assertEqual(capability["status"], "optional_missing")
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["native"]["status"], "available")
        self.assertTrue(capability["native"]["load_verified"])
        self.assertEqual(capability["native"]["source"], "system_lookup")
        self.assertEqual(capability["native"]["probe"], "libfluidsynth-3")
        self.assertEqual(
            capability["native"]["library"],
            "libfluidsynth.3.dylib",
        )
        self.assertEqual(
            capability["python_binding"]["status"],
            "optional_missing",
        )
        self.assertIsNone(capability["python_binding"]["version"])
        native_load.assert_called_once_with("libfluidsynth.3.dylib")

    def test_damaged_configured_dylib_is_error_not_available(self) -> None:
        runtime = self.root / "external FluidSynth"
        runtime.mkdir()
        library = runtime / "libfluidsynth.dylib"
        library.write_bytes(b"not a Mach-O library")

        with (
            mock.patch(
                "tianlai.soundfont._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.soundfont._is_macos_runtime",
                return_value=True,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "TIANLAI_FLUIDSYNTH_DIR": str(runtime),
                    "HOMEBREW_PREFIX": "",
                },
                clear=False,
            ),
            mock.patch(
                "tianlai.doctor._load_native_fluidsynth_library",
                side_effect=OSError("mach-o file, but is an incompatible architecture"),
            ),
            mock.patch(
                "tianlai.doctor.importlib.util.find_spec",
                return_value=object(),
            ),
            mock.patch(
                "tianlai.doctor.importlib.metadata.version",
                return_value="1.4.0",
            ),
        ):
            capability = doctor_module._fluidsynth_capability(self.layout)

        self.assertEqual(capability["status"], "error")
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["native"]["status"], "error")
        self.assertFalse(capability["native"]["load_verified"])
        self.assertIn("incompatible architecture", capability["native"]["error"])

    def test_native_loader_rejects_a_file_that_is_not_a_dynamic_library(self) -> None:
        placeholder = self.root / "libfluidsynth.dylib"
        placeholder.write_bytes(b"not a dynamic library")

        with self.assertRaises(OSError):
            doctor_module._load_native_fluidsynth_library(placeholder)

    def test_broken_optional_fluidsynth_degrades_otherwise_ready_report(self) -> None:
        self._write_self_contained_test_utility()
        _write_json(
            self.root / "可信乐器.json",
            {"trusted": ["测试工具/参考振荡器"]},
        )
        with mock.patch(
            "tianlai.doctor._platform_capabilities",
            return_value={"fluidsynth": {"status": "error"}},
        ):
            report = collect_doctor_report(layout=self.layout)

        self.assertEqual(report["summary"]["status"], "degraded")

    def test_rosetta_diagnostic_is_not_probed_or_reported_off_macos(self) -> None:
        with mock.patch(
            "tianlai.doctor._probe_macos_rosetta_translation",
        ) as probe:
            capability = doctor_module._macos_rosetta_capability(
                system="Linux",
                machine="x86_64",
                bits=64,
            )

        self.assertEqual(capability["status"], "not_applicable")
        self.assertIsNone(capability["translated"])
        self.assertIsNone(capability["supported"])
        probe.assert_not_called()

    def test_rosetta_diagnostic_distinguishes_native_arm_and_translation(self) -> None:
        with mock.patch(
            "tianlai.doctor._probe_macos_rosetta_translation",
        ) as probe:
            native = doctor_module._macos_rosetta_capability(
                system="Darwin",
                machine="arm64",
                bits=64,
            )
        self.assertEqual(native["status"], "native")
        self.assertFalse(native["translated"])
        self.assertEqual(native["host_architecture"], "arm64")
        self.assertTrue(native["supported"])
        probe.assert_not_called()

        with mock.patch(
            "tianlai.doctor._probe_macos_rosetta_translation",
            return_value=True,
        ) as probe:
            translated = doctor_module._macos_rosetta_capability(
                system="Darwin",
                machine="x86_64",
                bits=64,
            )
        self.assertEqual(translated["status"], "translated")
        self.assertTrue(translated["translated"])
        self.assertEqual(translated["host_architecture"], "arm64")
        self.assertFalse(translated["supported"])
        probe.assert_called_once_with()

        with mock.patch(
            "tianlai.doctor._probe_macos_rosetta_translation",
            side_effect=OSError("sysctl unavailable"),
        ):
            unknown = doctor_module._macos_rosetta_capability(
                system="Darwin",
                machine="x86_64",
                bits=64,
            )
        self.assertEqual(unknown["status"], "unknown")
        self.assertFalse(unknown["supported"])

    def test_rosetta_translation_makes_the_report_runtime_unsupported(self) -> None:
        self._write_self_contained_test_utility()
        _write_json(
            self.root / "可信乐器.json",
            {"trusted": ["测试工具/参考振荡器"]},
        )
        with (
            mock.patch("tianlai.doctor.platform.system", return_value="Darwin"),
            mock.patch("tianlai.doctor.platform.machine", return_value="x86_64"),
            mock.patch(
                "tianlai.doctor._probe_macos_rosetta_translation",
                return_value=True,
            ),
        ):
            report = collect_doctor_report(layout=self.layout)

        self.assertFalse(report["platform"]["supported"])
        self.assertEqual(report["platform"]["rosetta"]["status"], "translated")
        self.assertEqual(report["summary"]["status"], "error")

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
        self.assertEqual(
            json.loads(encoded)["schema_version"],
            REPORT_SCHEMA_VERSION,
        )
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

    def test_json_cli_explicitly_requests_utf8_for_redirected_stdout(self) -> None:
        self._write_sample_manifest()
        stdout = io.StringIO()
        stdout.reconfigure = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {"TIANLAI_HOME": str(self.root)},
            clear=False,
        ), mock.patch("sys.stdout", stdout):
            self.assertEqual(main(["--json", "--quick"]), 0)

        stdout.reconfigure.assert_called_once_with(
            encoding="utf-8",
            errors="strict",
        )
        self.assertEqual(
            json.loads(stdout.getvalue())["schema_version"],
            REPORT_SCHEMA_VERSION,
        )

    def test_json_cli_subprocess_emits_utf8_bytes_in_unicode_layout(self) -> None:
        self._write_sample_manifest()
        environment = os.environ.copy()
        environment["TIANLAI_HOME"] = str(self.root)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tianlai.doctor",
                "--json",
                "--quick",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        decoded = completed.stdout.decode("utf-8", errors="strict")
        document = json.loads(decoded)
        self.assertEqual(document["schema_version"], REPORT_SCHEMA_VERSION)
        self.assertIn("乐器", decoded)

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
        self.assertTrue(report["platform"]["supported"])
        self.assertIn(report["platform"]["normalised_machine"], {"x86_64", "arm64"})
        restore_capability = report["capabilities"]["resource_restore"]
        self.assertEqual(restore_capability["family_count"], 15)
        self.assertEqual(restore_capability["instrument_count"], 74)
        self.assertEqual(
            set(restore_capability["archive_formats"]),
            {"7z", "tar.xz", "zip"},
        )
        self.assertFalse(report["capabilities"]["fluidsynth"]["required_for_core"])
        self.assertEqual(
            report["summary"]["installer_available_count"],
            74,
        )
        self.assertEqual(
            report["summary"]["installer_unavailable_on_platform_count"],
            0,
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

        self.assertEqual(report["summary"]["installer_available_count"], 74)
        self.assertEqual(
            report["summary"]["installer_unavailable_on_platform_count"],
            0,
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

        vpo = by_id["管弦乐/弦乐组/小提琴"]["resource"]["installer"]
        self.assertEqual(vpo["status"], "available")
        self.assertEqual(vpo["resource_family"], "virtual-playing-orchestra")
        self.assertEqual(vpo["module"], "tianlai.resource_restore")


if __name__ == "__main__":
    unittest.main()
