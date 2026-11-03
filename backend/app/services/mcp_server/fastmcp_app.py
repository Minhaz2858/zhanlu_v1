"""Spec-compliant MCP server transport — Zhanlu skills over streamable HTTP.

Builds a ``FastMCP`` server (official SDK) that exposes every registered
ToolRegistry handler as an MCP tool, mounted at ``/api/ext-mcp``. This
gives external MCP clients (Claude Desktop, Cursor, n8n, …)
protocol-version negotiation, capabilities, sessions and both JSON and
SSE responses for free — the 2026-spec server surface. External clients
point their base URL at ``https://<host>/api/ext-mcp/mcp``.
"""
import json
import logging
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.services.mcp_server import get_zhanlu_mcp_server
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_fastmcp: FastMCP | None = None

# Phase 8 T8-7: the ONLY context keys an MCP caller may propagate. Identity
# and trace references that the tool handlers' org/binding scoping already
# re-validates server-side — never permission flags or trust overrides.
_ALLOWED_CONTEXT_KEYS = frozenset({
    "org_id", "app_id", "user_id", "agent_app_id", "agent_name",
    "run_id", "conversation_id",
})
_RESERVED_CONTEXT_ARG = "_zhanlu_context"


def extract_zhanlu_context(arguments: dict | None) -> tuple[dict | None, dict]:
    """Split ``_zhanlu_context`` out of MCP tool arguments.

    Returns ``(context_or_None, remaining_arguments)``. A missing or
    non-dict context yields ``(None, arguments_without_key)`` — anonymous
    MCP calls keep the no-context fallback; garbage context is DROPPED,
    never half-trusted. Unknown keys inside a dict context are stripped
    (allow-list, so permission flags cannot ride along)."""
    if not isinstance(arguments, dict):
        return None, {}
    rest = {k: v for k, v in arguments.items()
            if k != _RESERVED_CONTEXT_ARG}
    raw = arguments.get(_RESERVED_CONTEXT_ARG)
    if not isinstance(raw, dict):
        return None, rest
    return {k: v for k, v in raw.items() if k in _ALLOWED_CONTEXT_KEYS}, rest


def build_fastmcp_server() -> FastMCP:
    """Create a FastMCP server wrapping the ToolRegistry (idempotent)."""
    global _fastmcp
    if _fastmcp is not None:
        return _fastmcp

    server = FastMCP(
        "zhanlu-skills",
        instructions=(
            "Zhanlu Enterprise AI OS — governed skills exposed as MCP tools. "
            "Each tool takes a single JSON `arguments` object."
        ),
        # Served behind the main Zhanlu ASGI app on the deployment's real
        # Host header; disable FastMCP's localhost-only DNS-rebinding guard
        # (it would 421 every request with a non-localhost Host).
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    zhanlu = get_zhanlu_mcp_server()

    def _make_tool(tool_name: str, entry) -> Callable:
        async def _tool(arguments: dict | None = None) -> str:
            # Phase 8 T8-7: optional verified context pass-through for
            # internal consumers; anonymous external calls stay context=None.
            context, tool_arguments = extract_zhanlu_context(arguments)
            content = await zhanlu.call_tool(
                tool_name, tool_arguments, context=context)
            texts = [
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            return "\n".join(t for t in texts if t) or json.dumps(
                content, ensure_ascii=False, default=str,
            )

        _tool.__name__ = tool_name
        _tool.__doc__ = entry.description or tool_name
        return _tool

    for name in sorted(registry.list_available()):
        entry = registry.get_entry(name)
        if not entry or not entry.handler:
            continue
        server.tool()(_make_tool(name, entry))

    _fastmcp = server
    logger.info("FastMCP server built with %d tools", len(registry.list_available()))
    return server


def get_mcp_http_app() -> Any:
    """ASGI app for streamable HTTP transport (mount at /api/ext-mcp)."""
    return build_fastmcp_server().streamable_http_app()


def get_fastmcp_lifespan() -> Any:
    """Async context manager driving the FastMCP session-manager lifecycle.

    Starlette ``Mount`` does not run a child app's lifespan, so the host app
    must enter ``session_manager.run()`` itself (uvicorn does this for the
    standalone app; this hook is for the mounted-in-main case).
    """
    return build_fastmcp_server().session_manager.run()


__all__ = ["build_fastmcp_server", "get_mcp_http_app", "get_fastmcp_lifespan"]
