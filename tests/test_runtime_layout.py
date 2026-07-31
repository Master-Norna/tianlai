from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tianlai.runtime_layout import (
    RuntimeLayoutError,
    discover_runtime_layout,
)


class RuntimeLayoutTests(unittest.TestCase):
    def test_discovers_catalogue_from_nested_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "乐器").mkdir()
            (root / "可信乐器.json").write_text(
                '{"trusted":[]}',
                encoding="utf-8",
            )
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            with patch.dict(os.environ, {}, clear=True):
                layout = discover_runtime_layout(start=nested)
        self.assertEqual(layout.home, root.resolve())
        self.assertEqual(layout.source, "working_tree")
        self.assertTrue(layout.catalog_ready)

    def test_explicit_incomplete_home_fails_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {"TIANLAI_HOME": temporary},
                clear=True,
            ):
                with self.assertRaisesRegex(
                    RuntimeLayoutError,
                    "not a complete",
                ):
                    discover_runtime_layout()

    def test_engine_only_layout_keeps_output_out_of_package_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            start = Path(temporary)
            installed_module = (
                start
                / "site-packages"
                / "tianlai"
                / "runtime_layout.py"
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "tianlai.runtime_layout.__file__",
                    str(installed_module),
                ),
            ):
                layout = discover_runtime_layout(start=start)
        self.assertFalse(layout.catalog_ready)
        self.assertEqual(layout.home, start.resolve())
        self.assertEqual(layout.output, start.resolve() / "output")
        with self.assertRaisesRegex(RuntimeLayoutError, "TIANLAI_HOME"):
            layout.require_catalog()

    def test_resource_and_output_overrides_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "乐器").mkdir()
            (root / "可信乐器.json").write_text(
                '{"trusted":[]}',
                encoding="utf-8",
            )
            resources = root / "external"
            resources.mkdir()
            output = root / "renders"
            with patch.dict(
                os.environ,
                {
                    "TIANLAI_HOME": str(root),
                    "TIANLAI_RESOURCE_DIR": str(resources),
                    "TIANLAI_OUTPUT_DIR": str(output),
                },
                clear=True,
            ):
                layout = discover_runtime_layout()
        self.assertEqual(layout.resources, resources.resolve())
        self.assertEqual(layout.output, output.resolve())


if __name__ == "__main__":
    unittest.main()
