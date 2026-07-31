from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ROOT = ROOT / "乐器"
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
