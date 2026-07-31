"""代表性试听集输出边界的轻量回归测试；不会渲染音频。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "生成试听包.py"


def _load_tool():
    name = "tianlai_test_generate_demo_collection"
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DemoCollectionOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        (self.project / "output").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_is_a_strict_output_child(self) -> None:
        resolved = self.tool.resolve_output_directory(
            self.tool.DEFAULT_OUTPUT,
            project_root=self.project,
        )
        self.assertEqual(
            resolved,
            (self.project / "output" / "代表性试听集").resolve(),
        )

    def test_relative_and_absolute_output_children_are_allowed(self) -> None:
        relative = self.tool.resolve_output_directory(
            "output/试听/跨族群",
            project_root=self.project,
        )
        absolute = self.tool.resolve_output_directory(
            self.project / "output" / "另一份",
            project_root=self.project,
        )
        self.assertEqual(
            relative,
            (self.project / "output" / "试听" / "跨族群").resolve(),
        )
        self.assertEqual(
            absolute,
            (self.project / "output" / "另一份").resolve(),
        )

    def test_output_root_and_paths_outside_it_are_rejected(self) -> None:
        rejected = (
            "output",
            ".",
            "试听包",
            "output/..",
            "../outside",
            self.project.parent / "outside",
        )
        for requested in rejected:
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(ValueError, "output/ 下的非根子目录"):
                    self.tool.resolve_output_directory(
                        requested,
                        project_root=self.project,
                    )

    def test_prepare_replaces_only_the_validated_target(self) -> None:
        sibling = self.project / "output" / "作品" / "原创.wav"
        sibling.parent.mkdir(parents=True)
        sibling.write_bytes(b"keep")
        source = self.project / "README.md"
        source.write_text("keep", encoding="utf-8")

        target = self.project / "output" / "代表性试听集"
        target.mkdir()
        stale = target / "旧试听.wav"
        stale.write_bytes(b"stale")

        prepared = self.tool.prepare_output_directory(
            "output/代表性试听集",
            project_root=self.project,
        )

        self.assertEqual(prepared, target.resolve())
        self.assertTrue(prepared.is_dir())
        self.assertFalse(stale.exists())
        self.assertEqual(sibling.read_bytes(), b"keep")
        self.assertEqual(source.read_text(encoding="utf-8"), "keep")

    def test_existing_file_is_rejected_without_removal(self) -> None:
        target = self.project / "output" / "代表性试听集"
        target.write_bytes(b"not-a-directory")

        with self.assertRaisesRegex(ValueError, "不是目录"):
            self.tool.prepare_output_directory(
                target,
                project_root=self.project,
            )

        self.assertEqual(target.read_bytes(), b"not-a-directory")

    def test_link_redirecting_outside_output_is_rejected_when_supported(self) -> None:
        outside = self.project / "outside"
        outside.mkdir()
        link = self.project / "output" / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"当前系统不允许创建目录符号链接：{exc}")

        with self.assertRaisesRegex(ValueError, "非根子目录|链接或联接点"):
            self.tool.resolve_output_directory(
                link / "代表性试听集",
                project_root=self.project,
            )

    def test_demo_roster_is_explicitly_eighteen_items(self) -> None:
        self.assertEqual(len(self.tool.DEMO_NAMES), 18)

    def test_events_for_finds_hash_bound_score_in_protocol_subdirectory(
        self,
    ) -> None:
        instrument = self.project / "乐器" / "测试琴"
        instrument.mkdir(parents=True)
        examples = self.project / "examples"
        old_score = examples / "测试琴_旧谱.events.json"
        current_score = (
            examples / "全音域上行" / "测试琴_全音域上行.events.json"
        )
        old_score.parent.mkdir(parents=True)
        current_score.parent.mkdir(parents=True)
        old_score.write_text('{"events":[]}\n', encoding="utf-8")
        current_score.write_text(
            '{"events":[{"type":"note_on"}]}\n',
            encoding="utf-8",
        )
        (instrument / "试听核验.json").write_text(
            json.dumps(
                {
                    "events": (
                        "examples/全音域上行/"
                        "测试琴_全音域上行.events.json"
                    ),
                    "hash_algorithm": HASH_ALGORITHM,
                    "canonicalization": CANONICALIZATION,
                    "events_canonical_sha256": (
                        canonical_json_file_sha256(current_score)
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.tool.ROOT = self.project
        self.tool.EXAMPLES = examples

        self.assertEqual(self.tool.events_for(instrument), current_score)

    def test_events_for_ignores_json_layout_changes(self) -> None:
        instrument = self.project / "乐器" / "测试琴"
        instrument.mkdir(parents=True)
        score = self.project / "examples" / "测试琴_试听.events.json"
        score.parent.mkdir(parents=True)
        score.write_text('{"events":[],"version":1}\n', encoding="utf-8")
        (instrument / "试听核验.json").write_text(
            json.dumps(
                {
                    "events": score.relative_to(self.project).as_posix(),
                    "hash_algorithm": HASH_ALGORITHM,
                    "canonicalization": CANONICALIZATION,
                    "events_canonical_sha256": (
                        canonical_json_file_sha256(score)
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        score.write_bytes(
            b'{\r\n  "version": 1,\r\n  "events": []\r\n}\r\n'
        )
        self.tool.ROOT = self.project
        self.tool.EXAMPLES = self.project / "examples"

        self.assertEqual(self.tool.events_for(instrument), score)

    def test_events_for_keeps_explicit_legacy_byte_hash_compatibility(
        self,
    ) -> None:
        instrument = self.project / "乐器" / "测试琴"
        instrument.mkdir(parents=True)
        score = self.project / "examples" / "测试琴_旧谱.events.json"
        score.parent.mkdir(parents=True)
        score.write_text('{"events":[]}\n', encoding="utf-8")
        (instrument / "试听核验.json").write_text(
            json.dumps(
                {
                    "events": score.relative_to(self.project).as_posix(),
                    "events_sha256": hashlib.sha256(
                        score.read_bytes()
                    ).hexdigest(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.tool.ROOT = self.project
        self.tool.EXAMPLES = self.project / "examples"

        self.assertEqual(self.tool.events_for(instrument), score)


if __name__ == "__main__":
    unittest.main()
