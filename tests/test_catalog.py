from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tianlai.catalog import discover_instruments


ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_catalog_preserves_existing_formal_instruments(self) -> None:
        entries = discover_instruments(ROOT / "乐器")
        names = {entry.name for entry in entries}
        self.assertTrue({"钢琴", "小提琴", "大提琴", "长笛"}.issubset(names))
        # Five original formal instruments plus the 98-entry first expansion.
        # Individual expansion entries may graduate from SoundFont to sampled
        # implementations without changing catalog completeness.
        self.assertGreaterEqual(len(entries), 104)

        banjo = next(entry for entry in entries if entry.name == "班卓琴")
        self.assertEqual(banjo.license_status, "approved")

    def test_discovery_is_sorted_and_reports_soundfont_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifests = {
                "管弦乐/木管组/长笛": {
                    "name": "flute",
                    "type": "soundfont",
                    "program": 73,
                    "note_min": 60,
                    "note_max": 96,
                    "quality_tier": "candidate",
                    "collaboration_review_status": "in_progress",
                    "upgrade_status": "awaiting dedicated samples",
                    "license_status": "quarantined",
                },
                "键盘乐器/钢琴": {"name": "piano", "type": "piano"},
            }
            for relative, manifest in manifests.items():
                directory = root / relative
                directory.mkdir(parents=True)
                (directory / "乐器.json").write_text(
                    json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                )

            entries = discover_instruments(root)
            self.assertEqual([entry.name for entry in entries], ["长笛", "钢琴"])
            flute = entries[0]
            self.assertEqual(flute.category, "管弦乐 / 木管组")
            self.assertEqual(flute.implementation_type, "soundfont")
            self.assertEqual(flute.program, 73)
            self.assertEqual(flute.quality_tier, "candidate")
            self.assertEqual(
                flute.collaboration_review_status, "in_progress"
            )
            self.assertEqual(flute.upgrade_status, "awaiting dedicated samples")
            self.assertEqual(flute.license_status, "quarantined")

    def test_missing_catalog_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "catalog does not exist"):
                discover_instruments(Path(temporary_directory) / "missing")


if __name__ == "__main__":
    unittest.main()
