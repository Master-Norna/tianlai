from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ROOT = ROOT / "乐器"
MUSIC_REFERENCE_ROOT = ROOT / "docs" / "音乐创作参考笔记"
CC_BY_4_URL = "https://creativecommons.org/licenses/by/4.0/"
DECLARATION = {
    "provenance_kind": "project_authored_dsp",
    "implementation_license": "Apache-2.0",
    "external_audio_assets": [],
    "audio_asset_license": "not_applicable",
    "license_status": "approved",
}
EXPECTED_TYPES = {
    "modeled_instrument": 10,
    "modeled_bianzhong": 1,
    "synthesizer": 10,
    "procedural_sfx": 8,
}


class ProjectLicensePolicyTests(unittest.TestCase):
    def test_root_license_notice_and_output_rights_are_consistent(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        output_rights = (ROOT / "OUTPUT_RIGHTS.md").read_text(
            encoding="utf-8"
        )
        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("9. Accepting Warranty or Additional Liability", license_text)
        self.assertIn("Copyright 2026 Nor.na", notice)
        self.assertIn("originally conceived, architected, and developed", notice)
        self.assertIn("Tianlai Music Constitution v0.2", notice)
        self.assertIn("CC BY 4.0", notice)
        self.assertIn("does not alter the Apache-2.0 license", notice)
        self.assertEqual(
            pyproject["project"]["authors"],
            [{"name": "Nor.na"}],
        )
        self.assertEqual(
            pyproject["project"]["license"],
            "Apache-2.0",
        )
        self.assertEqual(
            set(pyproject["project"]["license-files"]),
            {"LICENSE", "NOTICE", "OUTPUT_RIGHTS.md", "TRADEMARKS.md"},
        )
        for phrase in (
            "代码许可证不会自动附着到音乐输出",
            "项目自研 DSP",
            "第三方采样乐器",
            "输入作品与使用者责任",
        ):
            self.assertIn(phrase, output_rights)

    def test_music_constitution_has_a_separate_cc_by_4_boundary(self) -> None:
        chinese_readme = (MUSIC_REFERENCE_ROOT / "README.md").read_text(
            encoding="utf-8"
        )
        english_readme = (MUSIC_REFERENCE_ROOT / "README.en.md").read_text(
            encoding="utf-8"
        )
        chinese_constitution = (
            MUSIC_REFERENCE_ROOT / "天籁音乐宪法-v0.2.md"
        ).read_text(encoding="utf-8")
        english_constitution = (
            MUSIC_REFERENCE_ROOT / "天籁音乐宪法-v0.2.en.md"
        ).read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        root_readme_en = (ROOT / "README.en.md").read_text(encoding="utf-8")

        self.assertTrue(chinese_readme.startswith("**简体中文** | [English]"))
        self.assertTrue(english_readme.startswith("[简体中文]"))
        self.assertTrue(
            chinese_constitution.startswith("**简体中文** | [English]")
        )
        self.assertTrue(english_constitution.startswith("[简体中文]"))
        for text in (
            chinese_readme,
            english_readme,
            chinese_constitution,
            english_constitution,
        ):
            self.assertIn("CC BY 4.0", text)
            self.assertIn(CC_BY_4_URL, text)
        for readme in (chinese_readme, english_readme):
            self.assertIn("Apache-2.0", readme)
            self.assertNotIn("音乐的“好听”能否被量化.pdf", readme)
        self.assertIn("不适用于天籁软件代码", chinese_readme)
        self.assertIn("does not apply to Tianlai software", english_readme)
        self.assertIn("不会触发项目处罚", chinese_readme)
        self.assertIn("triggers no", english_readme)
        self.assertIn("project penalty", english_readme)
        self.assertIn("仅仅使用这份创作指导不会", chinese_readme)
        self.assertIn("音乐自动适用 CC BY 4.0", chinese_readme)
        self.assertIn("Merely using", english_readme)
        self.assertIn("music created or modified with it subject", english_readme)
        self.assertIn("不遵守不会触发项目处罚", chinese_constitution)
        self.assertIn("音乐不会让音乐自动适用 CC BY 4.0", chinese_constitution)
        self.assertRegex(
            english_constitution.lower(),
            r"noncompliance does not trigger project\s*>\s*penalties",
        )
        self.assertRegex(
            english_constitution.lower(),
            r"does not automatically place that music under\s*>\s*cc by 4\.0",
        )
        self.assertIn(
            "docs/音乐创作参考笔记/天籁音乐宪法-v0.2.md",
            root_readme,
        )
        self.assertIn(
            "docs/音乐创作参考笔记/天籁音乐宪法-v0.2.en.md",
            root_readme_en,
        )

    def test_ignored_lifecycle_directories_unignore_only_public_readmes(
        self,
    ) -> None:
        lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        }
        for path in (
            "!output/README.md",
            "!output/README.en.md",
            "!音源/README.md",
            "!音源/README.en.md",
            "!乐谱/README.md",
            "!乐谱/README.en.md",
        ):
            self.assertIn(path, lines)
        for broad_exception in ("!output/*", "!音源/*", "!乐谱/*"):
            self.assertNotIn(broad_exception, lines)

    def test_29_project_authored_dsp_entries_have_exact_dual_evidence(
        self,
    ) -> None:
        entries: list[tuple[Path, dict, dict]] = []
        for manifest_path in sorted(INSTRUMENT_ROOT.rglob("乐器.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("provenance_kind") != "project_authored_dsp":
                continue
            resource_path = manifest_path.with_name("资源核验.json")
            resource = json.loads(resource_path.read_text(encoding="utf-8"))
            entries.append((manifest_path, manifest, resource))

        self.assertEqual(len(entries), 29)
        self.assertEqual(
            Counter(str(manifest["type"]) for _, manifest, _ in entries),
            Counter(EXPECTED_TYPES),
        )
        for manifest_path, manifest, resource in entries:
            with self.subTest(manifest=manifest_path.relative_to(ROOT)):
                for field, expected in DECLARATION.items():
                    self.assertEqual(manifest.get(field), expected)
                    self.assertEqual(resource.get(field), expected)
                self.assertEqual(resource.get("external_assets"), [])
                self.assertRegex(
                    str(resource.get("engine_sha256", "")),
                    re.compile(r"^[0-9a-fA-F]{64}$"),
                )
                source = manifest_path.with_name("来源.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("Apache-2.0", source)
                self.assertNotRegex(
                    source,
                    r"根级源码许可待|根许可待|根许可证待",
                )

    def test_no_other_manifest_claims_partial_project_dsp_provenance(
        self,
    ) -> None:
        provenance_fields = set(DECLARATION)
        self_authored_only_fields = provenance_fields - {"license_status"}
        for manifest_path in sorted(INSTRUMENT_ROOT.rglob("乐器.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            present = provenance_fields & set(manifest)
            self_authored_markers = self_authored_only_fields & set(manifest)
            with self.subTest(manifest=manifest_path.relative_to(ROOT)):
                if self_authored_markers:
                    self.assertEqual(
                        present,
                        provenance_fields,
                        f"不允许部分自研 DSP 声明：{sorted(present)}",
                    )

    def test_current_production_license_status_totals_are_explicit(self) -> None:
        statuses = Counter()
        for manifest_path in INSTRUMENT_ROOT.rglob("乐器.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("quality_tier") is None:
                continue
            statuses[str(manifest.get("license_status"))] += 1
        self.assertEqual(
            statuses,
            Counter({"approved": 72, "grandfathered": 31}),
        )


if __name__ == "__main__":
    unittest.main()
