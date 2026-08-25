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

    def test_authoring_project_one_way_upgrade_is_explicit_and_bilingual(
        self,
    ) -> None:
        chinese_markers = (
            "仅打开或保存完全相同的文档也不会触发迁移",
            "save_sequence",
            "current_save_event_sha256",
            "first_save_sequence",
            "parent_revision",
            ".tianlai/save-events/",
            "单向的因果记录升级",
            "0.9.x` 无法再打开保存后的工程",
            "首次修改保存前",
            "复制完整工程目录",
        )
        english_markers = (
            "saving identical documents",
            "save_sequence",
            "current_save_event_sha256",
            "first_save_sequence",
            "parent_revision",
            ".tianlai/save-events/",
            "one-way causal-provenance upgrade",
            "0.9.x` cannot reopen the project",
            "before the first changed save",
            "Copy the complete project directory",
        )
        for relative in ("README.md", "docs/MCP.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for marker in chinese_markers:
                with self.subTest(path=relative, marker=marker):
                    self.assertIn(marker, text)
        for relative in ("README.en.md", "docs/MCP.en.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for marker in english_markers:
                with self.subTest(path=relative, marker=marker):
                    self.assertIn(marker, text)

    def test_external_constitution_boundary_is_explicit_and_bilingual(
        self,
    ) -> None:
        chinese = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README.md",
                "docs/MCP.md",
                "docs/创作工作流.md",
                "docs/当前状态.md",
            )
        )
        english = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README.en.md",
                "README.pypi.md",
                "docs/MCP.en.md",
                "docs/创作工作流.en.md",
                "docs/当前状态.en.md",
            )
        )

        for marker in (
            "无状态可选",
            "前者只接受 `null`",
            "constitution_context",
            "material_relationship",
            "whole_work_necessity",
            "next_action=null",
        ):
            with self.subTest(language="zh-CN", marker=marker):
                self.assertIn(marker, chinese)
        self.assertNotIn("工作流法源", chinese)
        self.assertNotIn("官方 activation 所需", chinese)

        for marker in (
            "stateless, optional source of ideas",
            "former accepts only `null`",
            "constitution_context",
            "material_relationship",
            "whole_work_necessity",
            "next_action=null",
        ):
            with self.subTest(language="en", marker=marker):
                self.assertIn(marker, english)

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
        self.assertIn("date-released: 2026-08-25", citation)

    def test_formal_release_surfaces_are_synchronised(self) -> None:
        self.assertEqual(tianlai.__version__, "1.1.0")
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
                self.assertIn("1.1.0", text)
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
            self.assertIn("## 1.1.0（2026-08-25）", changelog)
            self.assertIn("## 1.1.0 (2026-08-25)", changelog_en)
            self.assertIn("## 1.0.0（2026-08-24）", changelog)
            self.assertIn("## 1.0.0 (2026-08-24)", changelog_en)
            chinese_unreleased, chinese_releases = changelog.split(
                "## 1.1.0（2026-08-25）",
                maxsplit=1,
            )
            english_unreleased, english_releases = changelog_en.split(
                "## 1.1.0 (2026-08-25)",
                maxsplit=1,
            )
            chinese_release, chinese_prior_releases = chinese_releases.split(
                "## 1.0.0（2026-08-24）",
                maxsplit=1,
            )
            english_release, english_prior_releases = english_releases.split(
                "## 1.0.0 (2026-08-24)",
                maxsplit=1,
            )
            self.assertIn("- 暂无。", chinese_unreleased)
            self.assertIn("- None.", english_unreleased)
            self.assertNotIn("天籁音乐宪法 v0.2", chinese_unreleased)
            self.assertNotIn("Constitution v0.2", english_unreleased)
            self.assertIn("天籁音乐宪法 v0.2", chinese_prior_releases)
            self.assertIn("Constitution v0.2", english_prior_releases)

            for marker in (
                "无状态可选",
                "material_relationship",
                "whole_work_necessity",
                "constitution_context",
            ):
                with self.subTest(section="chinese_release", marker=marker):
                    self.assertIn(marker, chinese_release)
            for marker in (
                "stateless, optional source of ideas",
                "material_relationship",
                "whole_work_necessity",
                "constitution_context",
            ):
                with self.subTest(section="english_release", marker=marker):
                    self.assertIn(marker, english_release)

            for marker in (
                "作品展开图",
                "prior_revision_assessment",
                "save_sequence",
                "work_id",
                "collaboration",
            ):
                with self.subTest(section="chinese_release", marker=marker):
                    self.assertIn(marker, chinese_prior_releases)
            for marker in (
                "composition map",
                "prior_revision_assessment",
                "save_sequence",
                "work_id",
                "collaboration",
            ):
                with self.subTest(section="english_release", marker=marker):
                    self.assertIn(marker, english_prior_releases)

        self.assertIn(
            "更新日期：2026-08-25",
            (ROOT / "docs" / "当前状态.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Updated: 2026-08-25",
            (ROOT / "docs" / "当前状态.en.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
