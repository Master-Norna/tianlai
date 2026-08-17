"""Dependency-light console entry point for the optional MCP service."""

from __future__ import annotations

import importlib
import sys
from typing import Any

from ._console_encoding import configure_utf8_standard_streams


_INSTALL_HINT = (
    '天籁 MCP 依赖未安装；请运行: python -m pip install "tianlai-audio[mcp]"'
)


def _load_server() -> Any:
    return importlib.import_module(".mcp_server", __package__)


def main() -> int:
    """Start the real service, or explain how to install its optional extra."""

    # stdout belongs to the MCP stdio transport.  Only the human-facing
    # installation diagnostic on stderr is configured here.
    configure_utf8_standard_streams(stdout=False)
    try:
        server = _load_server()
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == "mcp" or missing.startswith("mcp."):
            print(_INSTALL_HINT, file=sys.stderr)
            return 2
        raise
    server.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
