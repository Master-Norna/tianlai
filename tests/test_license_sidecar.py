from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tianlai.license_sidecar import (
    AudioArtifact,
    InstrumentUse,
    build_license_sidecar_document,
    render_human_attribution,
    write_license_sidecars,
)

ROOT = Path(__file__).resolve().parents[1]


class LicenseSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.instrument = self.root / "乐器" / "键盘乐器" / "测试琴"
        self.instrument.mkdir(parents=True)
        self.manifest = self.instrument / "乐器.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "测试琴",
                    "upstream": "Example Library",
                    "origin": "https://example.invalid/library",
                    "upstream_version": "1.2.3",
                    "license": "CC-BY-4.0",
                    "license_status": "approved",
                    "evidence_files": ["LICENSE", "README.md"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.audio = self.root / "result.wav"
        self.audio.write_bytes(b"not-a-real-wave-but-hashable")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_records_only_used_manifests_and_never_invents_creator(self) -> None:
        unused = self.root / "乐器" / "未使用" / "乐器.json"
        unused.parent.mkdir()
        unused.write_text(
            json.dumps(
                {
                    "name": "绝不应出现",
                    "creator": "Unused Creator",
                    "license": "CC0-1.0",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        document = build_license_sidecar_document(
            (
                InstrumentUse(
                    self.manifest,
                    used_by=("piano",),
                ),
            ),
            (
                AudioArtifact(
                    role="mix",
                    path=self.audio,
                    label="合奏.wav",
                ),
            ),
        )

        self.assertEqual(
            document["scope"],
            {
                "rule": "actual_render_inputs_only",
                "instrument_count": 1,
                "audio_artifact_count": 1,
            },
        )
        record = document["instruments"][0]
        self.assertEqual(record["instrument"], "测试琴")
        self.assertEqual(record["manifest"]["path"], "键盘乐器/测试琴/乐器.json")
        self.assertEqual(record["upstream"], "Example Library")
        self.assertIsNone(record["creator"])
        self.assertIn("creator_missing_in_manifest", record["warnings"])
        self.assertNotIn("Unused Creator", json.dumps(document, ensure_ascii=False))
        self.assertEqual(
            document["audio_artifacts"][0]["sha256"],
            hashlib.sha256(self.audio.read_bytes()).hexdigest(),
        )

    def test_duplicate_manifest_is_deduplicated_but_usage_is_preserved(self) -> None:
        document = build_license_sidecar_document(
            (
                InstrumentUse(self.manifest, used_by=("piano_2",)),
                InstrumentUse(self.manifest, used_by=("piano_1",)),
            ),
            (),
        )

        self.assertEqual(document["scope"]["instrument_count"], 1)
        self.assertEqual(
            document["instruments"][0]["used_by"],
            ["piano_1", "piano_2"],
        )

    def test_released_attribution_records_keep_first_party_credit(self) -> None:
        cases = (
            (
                "乐器/键盘乐器/电钢琴/乐器.json",
                "Greg Sullivan",
                (
                    "kinwie",
                    "https://creativecommons.org/licenses/by/3.0/",
                    "remain unmodified",
                    "band-limited",
                ),
            ),
            (
                "乐器/键盘乐器/合唱电钢琴/乐器.json",
                "Greg Sullivan",
                (
                    "kinwie",
                    "https://creativecommons.org/licenses/by/3.0/",
                    "remain unmodified",
                    "stereo chorus",
                ),
            ),
            (
                "乐器/键盘乐器/钢琴/乐器.json",
                "Alexander Holm",
                ("kinwie", "CC BY 3.0"),
            ),
            (
                "乐器/键盘乐器/击弦古钢琴/乐器.json",
                "Staatliches Institut für Musikforschung (SIMPK)",
                ("CC BY 4.0", "original WAV bytes are unchanged"),
            ),
            (
                "乐器/管弦乐/弦乐组/弦乐合奏/乐器.json",
                "Paul Battersby",
                ("Sonatina Symphonic Orchestra", "Documentation/license.htm"),
            ),
            (
                "乐器/世界乐器/班卓琴/乐器.json",
                "itsclipping",
                ("provenance", "does not imply endorsement"),
            ),
            (
                "乐器/现代管乐/高音萨克斯/乐器.json",
                "Music Technology Group (MTG)",
                ("kinwie", "CC BY 4.0"),
            ),
        )

        for relative, creator_fragment, attribution_fragments in cases:
            with self.subTest(manifest=relative):
                record = build_license_sidecar_document(
                    (
                        InstrumentUse(
                            ROOT / Path(relative),
                            used_by=("licence-test",),
                        ),
                    ),
                    (),
                )["instruments"][0]
                self.assertIn(creator_fragment, record["creator"])
                for fragment in attribution_fragments:
                    self.assertIn(fragment, record["attribution"])
                self.assertNotIn(
                    "creator_missing_in_manifest",
                    record["warnings"],
                )

    def test_project_authored_dsp_is_reported_without_missing_license_noise(
        self,
    ) -> None:
        manifest = self.root / "乐器" / "世界乐器" / "程序钟" / "乐器.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "程序钟",
                    "provenance_kind": "project_authored_dsp",
                    "implementation_license": "Apache-2.0",
                    "external_audio_assets": [],
                    "audio_asset_license": "not_applicable",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        document = build_license_sidecar_document(
            (InstrumentUse(manifest, used_by=("bell",)),),
            (),
        )
        record = document["instruments"][0]

        self.assertEqual(record["provenance_kind"], "project_authored_dsp")
        self.assertEqual(record["implementation_license"], "Apache-2.0")
        self.assertEqual(record["external_audio_assets"], [])
        self.assertEqual(record["audio_asset_license"], "not_applicable")
        self.assertEqual(
            record["upstream_terms_action"],
            "not_applicable_no_third_party_audio_assets",
        )
        self.assertEqual(record["warnings"], [])
        self.assertEqual(document["warnings"], [])

        human = render_human_attribution(document)
        self.assertIn("来源类型：自研 DSP", human)
        self.assertIn("实现许可：Apache-2.0", human)
        self.assertIn("第三方采样：无", human)
        self.assertNotIn("许可：（清单未声明）", human)
        self.assertNotIn("上游：（清单未声明）", human)
        self.assertNotIn("上游许可证原文", human)
        self.assertNotIn("字段为空只表示", human)

    def test_incomplete_project_authored_declaration_stays_conservative(
        self,
    ) -> None:
        manifest = self.root / "乐器" / "世界乐器" / "不完整程序钟" / "乐器.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "不完整程序钟",
                    "provenance_kind": "project_authored_dsp",
                    "implementation_license": "Apache-2.0",
                    "external_audio_assets": ["unexpected.wav"],
                    "audio_asset_license": "not_applicable",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        record = build_license_sidecar_document(
            (InstrumentUse(manifest, used_by=("bell",)),),
            (),
        )["instruments"][0]

        self.assertNotIn("provenance_kind", record)
        self.assertIn("upstream_missing_in_manifest", record["warnings"])
        self.assertIn("license_missing_in_manifest", record["warnings"])

    def test_expected_manifest_hash_detects_a_changed_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "渲染期间发生变化"):
            build_license_sidecar_document(
                (
                    InstrumentUse(
                        self.manifest,
                        used_by=("piano",),
                        expected_sha256="0" * 64,
                    ),
                ),
                (),
            )

    def test_machine_and_human_sidecars_are_deterministic(self) -> None:
        first_json = self.root / "first" / "许可与署名.json"
        first_text = self.root / "first" / "许可与署名.txt"
        second_json = self.root / "second" / "许可与署名.json"
        second_text = self.root / "second" / "许可与署名.txt"
        kwargs = {
            "instrument_uses": (
                InstrumentUse(self.manifest, used_by=("piano",)),
            ),
            "audio_artifacts": (
                AudioArtifact("mix", self.audio, "合奏.wav"),
            ),
        }

        first = write_license_sidecars(
            first_json,
            first_text,
            **kwargs,
        )
        second = write_license_sidecars(
            second_json,
            second_text,
            **kwargs,
        )

        self.assertEqual(first_json.read_bytes(), second_json.read_bytes())
        self.assertEqual(first_text.read_bytes(), second_text.read_bytes())
        self.assertEqual(first.json_sha256, second.json_sha256)
        human = first_text.read_text(encoding="utf-8")
        self.assertIn("只列本次渲染实际使用的乐器", human)
        self.assertIn("创作者/录音者：（清单未声明）", human)
        self.assertIn("CC-BY-4.0", human)
        self.assertIn("上游许可证原文", human)


if __name__ == "__main__":
    unittest.main()
