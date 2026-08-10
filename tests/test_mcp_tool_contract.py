"""Contract tests for Tianlai's isolated MCP SDK 2.0.0 adapter."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from tianlai.mcp_tool_contract import (
    MCPToolContractError,
    PINNED_MCP_VERSION,
    bind_strict_mcp_tool,
)


_HAS_MCP = importlib.util.find_spec("mcp") is not None


class OptionalDependencyBoundaryTests(unittest.TestCase):
    def test_contract_module_does_not_import_optional_mcp_package(self) -> None:
        source = Path(__file__).parents[1] / "tianlai" / "mcp_tool_contract.py"
        text = source.read_text(encoding="utf-8")

        self.assertNotIn("from mcp", text)
        self.assertNotIn("import mcp", text)

    def test_wrong_sdk_version_fails_before_registration(self) -> None:
        server = mock.Mock()
        decorator = bind_strict_mcp_tool(server)

        with (
            mock.patch(
                "tianlai.mcp_tool_contract.metadata.version",
                return_value="2.1.0",
            ),
            self.assertRaisesRegex(MCPToolContractError, "pinned to mcp==2.0.0"),
        ):

            @decorator()
            def incompatible(value: int) -> dict[str, int]:
                return {"value": value}

        server.add_tool.assert_not_called()

    def test_missing_private_manager_fails_closed(self) -> None:
        class ServerWithoutManager:
            def add_tool(self, fn, **kwargs) -> None:
                self.last_registration = (fn, kwargs)

        server = ServerWithoutManager()
        decorator = bind_strict_mcp_tool(server)

        with (
            mock.patch(
                "tianlai.mcp_tool_contract.metadata.version",
                return_value=PINNED_MCP_VERSION,
            ),
            self.assertRaisesRegex(
                MCPToolContractError,
                "_tool_manager.get_tool is missing",
            ),
        ):

            @decorator()
            def guarded(value: int) -> dict[str, int]:
                return {"value": value}

        self.assertTrue(server.last_registration[1]["structured_output"])


@unittest.skipUnless(_HAS_MCP, "optional mcp package is not installed")
class RealMCPServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        from mcp.server import MCPServer

        self.server = MCPServer("tianlai-contract-test")
        self.mcp_tool = bind_strict_mcp_tool(self.server)

    def test_tools_list_is_strict_structured_and_annotated(self) -> None:
        from mcp.types import ToolAnnotations

        annotations = ToolAnnotations(
            title="Inspect project",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

        @self.mcp_tool(annotations=annotations)
        def inspect_project(path: str, depth: int = 1) -> dict[str, object]:
            """Inspect one project."""

            return {"path": path, "depth": depth}

        listed = asyncio.run(self.server.list_tools())

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].name, "inspect_project")
        self.assertIs(listed[0].annotations, annotations)
        self.assertIs(listed[0].input_schema["additionalProperties"], False)
        self.assertIsInstance(listed[0].output_schema, dict)
        self.assertIn("additionalProperties", listed[0].output_schema)

    def test_unknown_top_level_argument_is_rejected_at_execution_boundary(self) -> None:
        @self.mcp_tool()
        def echo(value: int) -> dict[str, int]:
            return {"value": value}

        tool = self.server._tool_manager.get_tool("echo")
        self.assertIsNotNone(tool)

        with self.assertRaisesRegex(Exception, "Extra inputs are not permitted"):
            tool.fn_metadata.validate_arguments({"value": 7, "surprise": True})

        self.assertEqual(
            tool.fn_metadata.validate_arguments({"value": 7}),
            {"value": 7},
        )

    def test_duplicate_name_does_not_mutate_existing_tool(self) -> None:
        @self.mcp_tool(name="same_name")
        def original(value: int) -> dict[str, int]:
            return {"value": value}

        with self.assertRaisesRegex(MCPToolContractError, "already registered"):

            @self.mcp_tool(name="same_name")
            def replacement(value: str) -> dict[str, str]:
                return {"value": value}

        tool = self.server._tool_manager.get_tool("same_name")
        self.assertIs(tool.fn, original)


if __name__ == "__main__":
    unittest.main()
