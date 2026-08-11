from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tianlai import mcp_entry


ROOT = Path(__file__).resolve().parents[1]


class McpEntryTests(unittest.TestCase):
    def test_public_client_examples_use_dependency_light_entry(self) -> None:
        for relative_path in (
            ".mcp.json.example",
            "docs/MCP.md",
            "docs/MCP.en.md",
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn("tianlai.mcp_entry", text)
                self.assertNotIn("tianlai.mcp_server", text)

    def test_missing_optional_dependency_has_one_line_install_hint(self) -> None:
        missing = ModuleNotFoundError("No module named 'mcp'", name="mcp")
        stderr = io.StringIO()
        with (
            mock.patch.object(mcp_entry, "_load_server", side_effect=missing),
            redirect_stderr(stderr),
        ):
            status = mcp_entry.main()

        lines = stderr.getvalue().splitlines()
        self.assertEqual(status, 2)
        self.assertEqual(len(lines), 1)
        self.assertIn('pip install "tianlai-audio[mcp]"', lines[0])
        self.assertNotIn("Traceback", lines[0])

    def test_unrelated_import_failure_is_not_hidden(self) -> None:
        missing = ModuleNotFoundError(
            "No module named 'unexpected_dependency'",
            name="unexpected_dependency",
        )
        with (
            mock.patch.object(mcp_entry, "_load_server", side_effect=missing),
            self.assertRaises(ModuleNotFoundError),
        ):
            mcp_entry.main()

    def test_installed_service_is_delegated_to(self) -> None:
        run = mock.Mock()
        server = SimpleNamespace(main=run)
        with mock.patch.object(mcp_entry, "_load_server", return_value=server):
            status = mcp_entry.main()

        self.assertEqual(status, 0)
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
