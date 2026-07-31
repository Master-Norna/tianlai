from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tianlai.quality import load_upgrade_progress
from tianlai.upgrade_registry import HISTORICAL_UPGRADE_REGISTRY


ROOT = Path(__file__).resolve().parents[1]


class UpgradeProgressTests(unittest.TestCase):
    def test_real_ledger_has_98_unique_existing_entries(self) -> None:
        progress = load_upgrade_progress(ROOT / "乐器")
        self.assertEqual(progress.total, 98)
        self.assertEqual(sum(progress.counts.values()), 98)
        self.assertEqual(progress.counts["formal"], 98)
        self.assertEqual(progress.collaboration_counts["untested"], 98)
        self.assertEqual(len({entry.upgrade_id for entry in progress.entries}), 98)
        self.assertEqual(len({entry.relative_path for entry in progress.entries}), 98)
        self.assertTrue(all(Path(entry.manifest_path).is_file() for entry in progress.entries))
        payload = progress.to_dict()
        self.assertEqual(
            payload["kind"],
            "tianlai.historical_upgrade_ledger",
        )
        self.assertIn("不是当前声音入口总数", payload["scope_note"])

    def test_explicit_markdown_registry_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger = Path(temporary_directory) / "ledger.md"
            ledger.write_text(
                "\n".join(
                    f"| {upgrade_id} | `{relative_path}` | formal |"
                    for upgrade_id, relative_path in HISTORICAL_UPGRADE_REGISTRY
                ),
                encoding="utf-8",
            )
            packaged = load_upgrade_progress(ROOT / "乐器")
            markdown = load_upgrade_progress(ROOT / "乐器", ledger)
            self.assertEqual(
                [
                    (entry.upgrade_id, entry.relative_path)
                    for entry in markdown.entries
                ],
                [
                    (entry.upgrade_id, entry.relative_path)
                    for entry in packaged.entries
                ],
            )

    def test_registry_rejects_duplicate_rows_before_loading_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ledger = root / "ledger.md"
            rows = [
                f"| SAM-{index:02d} | `分类/乐器{index}` | fallback |"
                for index in range(1, 98)
            ]
            rows.append("| SAM-01 | `分类/重复` | fallback |")
            ledger.write_text("\n".join(rows), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate upgrade IDs"):
                load_upgrade_progress(root / "乐器", ledger)

    def test_registry_rejects_nonportable_and_case_colliding_paths(self) -> None:
        variants = (
            (
                [
                    "../escape" if index == 0 else path
                    for index, (_, path) in enumerate(
                        HISTORICAL_UPGRADE_REGISTRY
                    )
                ],
                "portable POSIX relative path",
            ),
            (
                [
                    (
                        "Group/Instrument"
                        if index == 0
                        else "group/instrument"
                        if index == 1
                        else path
                    )
                    for index, (_, path) in enumerate(HISTORICAL_UPGRADE_REGISTRY)
                ],
                "case-insensitive filesystem",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for variant_index, (paths, message) in enumerate(variants):
                ledger = root / f"ledger-{variant_index}.md"
                ledger.write_text(
                    "\n".join(
                        f"| {upgrade_id} | `{relative_path}` | formal |"
                        for (upgrade_id, _), relative_path in zip(
                            HISTORICAL_UPGRADE_REGISTRY,
                            paths,
                            strict=True,
                        )
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, message):
                    load_upgrade_progress(root / "乐器", ledger)


if __name__ == "__main__":
    unittest.main()
