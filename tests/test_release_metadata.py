from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

import tianlai


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_and_project_versions_match(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["version"],
            tianlai.__version__,
        )

    def test_pep639_license_metadata_has_no_legacy_classifier(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["requires-python"], ">=3.11,<3.15")
        self.assertIn(
            "Programming Language :: Python :: Implementation :: CPython",
            project["classifiers"],
        )
        self.assertIn(
            "Operating System :: Microsoft :: Windows",
            project["classifiers"],
        )
        self.assertIn(
            "Operating System :: POSIX :: Linux",
            project["classifiers"],
        )
        self.assertFalse(
            any(
                item.startswith("License ::")
                for item in project.get("classifiers", [])
            )
        )

    def test_required_public_rights_documents_exist(self) -> None:
        for name in (
            "LICENSE",
            "NOTICE",
            "OUTPUT_RIGHTS.md",
            "TRADEMARKS.md",
        ):
            with self.subTest(name=name):
                path = ROOT / name
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

    def test_minimal_windows_entrypoints_exist(self) -> None:
        for name in (
            "安装运行环境.cmd",
            "天籁.cmd",
            "检查运行环境.cmd",
            "安装可恢复音源.cmd",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_minimal_linux_entrypoint_exists_and_uses_lf(self) -> None:
        bootstrap = ROOT / "bootstrap_linux.sh"
        self.assertTrue(bootstrap.is_file())
        raw = bootstrap.read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", raw)

    def test_hash_locked_python_sources_checkout_with_lf(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.py text eol=lf", attributes.splitlines())

    def test_repository_text_defaults_are_cross_platform_lf(self) -> None:
        attributes = (
            ROOT / ".gitattributes"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(attributes[0], "* text=auto eol=lf")
        self.assertIn("*.cmd binary", attributes)

        editor_config = (
            ROOT / ".editorconfig"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(editor_config[0], "root = true")
        self.assertIn("end_of_line = lf", editor_config)
        self.assertIn("[*.{bat,cmd}]", editor_config)
        self.assertEqual(editor_config[-1], "end_of_line = crlf")

    def test_windows_ci_doctor_uses_utf8_without_weakening_the_gate(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        doctor_step = workflow.split(
            "      - name: Source-release doctor\n",
            maxsplit=1,
        )[1].split("\n\n  linux-portable:", maxsplit=1)[0]
        self.assertIn('PYTHONUTF8: "1"', doctor_step)
        self.assertIn('PYTHONIOENCODING: "utf-8"', doctor_step)
        self.assertIn("python -m tianlai.doctor --quick", doctor_step)
        self.assertNotIn("continue-on-error", doctor_step)

    def test_ordinary_ci_does_not_duplicate_tagged_release_runs(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        trigger = workflow.split("permissions:", maxsplit=1)[0]
        self.assertIn('push:\n    branches:\n      - "**"', trigger)
        self.assertNotIn("tags:", trigger)

    def test_pypi_artifacts_are_declared_engine_only(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"]["tianlai-mcp"],
            "tianlai.mcp_entry:main",
        )
        self.assertFalse(project["tool"]["setuptools"]["include-package-data"])
        self.assertEqual(project["project"]["readme"], "README.pypi.md")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("exclude README.md", manifest)
        self.assertIn("prune tests", manifest)
        pypi_readme = (ROOT / "README.pypi.md").read_text(encoding="utf-8")
        self.assertNotIn("](docs/", pypi_readme)
        self.assertNotIn("](examples/", pypi_readme)
        engine_smoke = pypi_readme.split("## Installation", 1)[1].split(
            "`tianlai-doctor` performs", 1
        )[0]
        self.assertNotIn("tianlai-doctor", engine_smoke)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("正式产品是项目提供的轻量源码 ZIP", readme)
        self.assertIn("只提供可复用的 Python 引擎", readme)

    def test_mcp_docs_keep_the_passive_native_macos_gate_explicit(self) -> None:
        chinese = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in ("README.md", "docs/MCP.md")
        )
        english = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in ("README.en.md", "docs/MCP.en.md")
        )

        self.assertIn("只读 `sysctlbyname`", chinese)
        self.assertIn("readiness 授权继续渲染", chinese)
        self.assertIn("readiness 不授权客户端", chinese)
        self.assertIn("协议本身不能强迫", chinese)
        self.assertNotIn("无法通过被动检查确认 Rosetta", chinese)

        self.assertIn("read-only in-process `sysctlbyname`", english)
        self.assertIn("Only verified native Intel", english)
        self.assertIn("translation or an unverifiable", english)
        self.assertIn("client that bypasses readiness", english)
        self.assertNotIn("passive inspection cannot establish Rosetta", english)

    def test_default_pytest_collection_is_confined_to_the_test_suite(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["tool"]["pytest"]["ini_options"]["testpaths"],
            ["tests"],
        )

    def test_citation_names_the_original_author_and_version(self) -> None:
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn('family-names: "Nor.na"', citation)
        self.assertIn(f"version: {tianlai.__version__}", citation)
        self.assertIn("date-released: 2026-08-17", citation)

    def test_formal_release_surfaces_are_synchronised(self) -> None:
        self.assertEqual(tianlai.__version__, "0.9.0")
        current_files = (
            "README.md",
            "README.en.md",
            "docs/README.md",
            "docs/README.en.md",
            "docs/MCP.md",
            "docs/MCP.en.md",
            "docs/Linux快速开始.md",
            "docs/Linux快速开始.en.md",
            "docs/当前状态.md",
            "docs/当前状态.en.md",
        )
        for relative in current_files:
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("0.9.0", text)
                self.assertNotIn("0.8.0rc1", text)

        # The private construction repository keeps the historical changelog;
        # the deliberately minimal public source ZIP excludes it.  Validate
        # the release entry whenever this test runs in the construction tree.
        changelog_path = ROOT / "CHANGELOG.md"
        changelog_en_path = ROOT / "CHANGELOG.en.md"
        if changelog_path.is_file() or changelog_en_path.is_file():
            self.assertTrue(changelog_path.is_file())
            self.assertTrue(changelog_en_path.is_file())
            changelog = changelog_path.read_text(encoding="utf-8")
            changelog_en = changelog_en_path.read_text(encoding="utf-8")
            self.assertIn("## 0.9.0（2026-08-17）", changelog)
            self.assertIn("## 0.9.0 (2026-08-17)", changelog_en)


if __name__ == "__main__":
    unittest.main()
