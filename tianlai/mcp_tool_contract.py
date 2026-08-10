"""Strict MCP v2 tool registration without a core-package MCP import.

The Tianlai engine keeps MCP optional.  Consequently this module deliberately uses
duck typing and the standard library only; :mod:`tianlai.mcp_server` is the sole
module that imports the optional ``mcp`` package.

MCP SDK 2.0.0 does not expose a public hook for changing the Pydantic argument
model created by ``MCPServer.add_tool``.  The small, guarded private-API adapter
below is therefore kept in one place.  It makes top-level tool arguments strict
both on the wire and at execution time.  Every private attribute is checked before
use so an SDK upgrade fails during server construction instead of silently
weakening the contract.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from typing import Any, TypeVar, cast


PINNED_MCP_VERSION = "2.0.0"

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])
_ToolDecorator = Callable[[_CallableT], _CallableT]


class MCPToolContractError(RuntimeError):
    """The pinned MCP SDK cannot provide Tianlai's strict tool contract."""


def bind_strict_mcp_tool(
    server: Any,
) -> Callable[..., _ToolDecorator]:
    """Return a ``@mcp_tool(...)`` decorator bound to *server*.

    The returned decorator mirrors the public MCPServer decorator fields Tianlai
    uses, forwards ``ToolAnnotations`` unchanged, and always requests structured
    output.  ``ToolAnnotations`` is intentionally not imported here: callers in
    ``mcp_server`` construct the SDK object and this optional-dependency boundary
    merely transports it.

    Example::

        mcp_tool = bind_strict_mcp_tool(mcp)

        @mcp_tool(annotations=annotations)
        def inspect(path: str) -> dict[str, object]:
            ...
    """

    def mcp_tool(
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Any | None = None,
        icons: list[Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> _ToolDecorator:
        if callable(name):
            raise TypeError(
                "Use @mcp_tool() rather than @mcp_tool; Tianlai tool "
                "registration requires an explicit decorator call."
            )

        def decorator(fn: _CallableT) -> _CallableT:
            register_strict_mcp_tool(
                server,
                fn,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                icons=icons,
                meta=meta,
            )
            return fn

        return cast(_ToolDecorator, decorator)

    return mcp_tool


def register_strict_mcp_tool(
    server: Any,
    fn: _CallableT,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: Any | None = None,
    icons: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> _CallableT:
    """Register one structured tool and enforce strict top-level arguments.

    This adapter is intentionally pinned to MCP SDK 2.0.0.  The SDK's public
    ``MCPServer.add_tool`` currently returns ``None``, so locating and tightening
    the generated argument model requires ``_tool_manager`` and ``fn_metadata``.
    Those private seams are isolated and defended here.
    """

    _require_pinned_mcp_sdk()
    tool_name = name or getattr(fn, "__name__", None)
    if not isinstance(tool_name, str) or not tool_name:
        raise MCPToolContractError("MCP tools require a non-empty name")

    add_tool = getattr(server, "add_tool", None)
    if not callable(add_tool):
        raise _private_api_error("MCPServer.add_tool is missing")

    try:
        add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=True,
        )
    except TypeError as exc:
        raise _private_api_error(
            "MCPServer.add_tool no longer accepts the pinned registration fields"
        ) from exc

    # Pinned MCP 2.0.0 private seam.  Keep this lookup close to all validation so
    # future SDK drift cannot leave a successfully registered but permissive tool.
    manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(manager, "get_tool", None)
    if not callable(get_tool):
        raise _private_api_error("MCPServer._tool_manager.get_tool is missing")

    tool = get_tool(tool_name)
    if tool is None:
        raise _private_api_error(
            f"registered tool {tool_name!r} is not visible through _tool_manager"
        )
    if getattr(tool, "fn", None) is not fn:
        raise MCPToolContractError(
            f"MCP tool name {tool_name!r} is already registered; refusing to "
            "tighten a different callable"
        )

    fn_metadata = getattr(tool, "fn_metadata", None)
    arg_model = getattr(fn_metadata, "arg_model", None)
    model_config = getattr(arg_model, "model_config", None)
    model_rebuild = getattr(arg_model, "model_rebuild", None)
    model_json_schema = getattr(arg_model, "model_json_schema", None)
    if not isinstance(model_config, dict):
        raise _private_api_error("tool.fn_metadata.arg_model.model_config is missing")
    if not callable(model_rebuild) or not callable(model_json_schema):
        raise _private_api_error(
            "tool.fn_metadata.arg_model no longer supports rebuild/schema generation"
        )

    # Do not mutate an inherited ConfigDict in place.  Each SDK-generated argument
    # model receives its own strict config, then rebuilds both the validator and its
    # advertised JSON Schema from that same source of truth.
    arg_model.model_config = {**model_config, "extra": "forbid"}
    try:
        model_rebuild(force=True)
        input_schema = model_json_schema(by_alias=True)
    except (TypeError, ValueError) as exc:
        raise _private_api_error("strict argument model rebuild failed") from exc

    if not isinstance(input_schema, dict):
        raise _private_api_error("argument model returned a non-object JSON Schema")
    if input_schema.get("additionalProperties") is not False:
        raise MCPToolContractError(
            f"MCP tool {tool_name!r} did not produce additionalProperties=false"
        )

    try:
        tool.parameters = input_schema
    except (AttributeError, TypeError, ValueError) as exc:
        raise _private_api_error("registered tool parameters are no longer writable") from exc

    output_schema = getattr(fn_metadata, "output_schema", None)
    if not isinstance(output_schema, dict):
        raise MCPToolContractError(
            f"MCP tool {tool_name!r} has no structured output schema; add a "
            "serializable return annotation"
        )
    return fn


def _require_pinned_mcp_sdk() -> None:
    try:
        installed = metadata.version("mcp")
    except metadata.PackageNotFoundError as exc:
        raise MCPToolContractError(
            'MCP support requires pip install "tianlai-audio[mcp]"'
        ) from exc
    if installed != PINNED_MCP_VERSION:
        raise MCPToolContractError(
            "Tianlai's strict MCP adapter is pinned to mcp=="
            f"{PINNED_MCP_VERSION}, but {installed!r} is installed"
        )


def _private_api_error(detail: str) -> MCPToolContractError:
    return MCPToolContractError(
        f"mcp=={PINNED_MCP_VERSION} private API contract changed: {detail}"
    )
