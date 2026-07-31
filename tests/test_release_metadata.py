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

    def test_pypi_artifacts_are_declared_engine_only(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"]["tianlai-mcp"],
            "tianlai.mcp_entry:main",
        )
        self.assertFalse(project["tool"]["setuptools"]["include-package-data"])
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("prune tests", manifest)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("正式产品是项目提供的轻量源码 ZIP", readme)
        self.assertIn("只提供可复用的 Python 引擎", readme)

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


if __name__ == "__main__":
    unittest.main()
