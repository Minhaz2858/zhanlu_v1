"""Native MCP server — exposes Zhanlu skills as MCP tools/resources via HTTP+SSE.

Implements a lightweight MCP server that wraps the existing ToolRegistry.
External MCP clients (Claude Desktop, Cursor, etc.) can connect to discover
and invoke skills as MCP tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


class ZhanluMCPServer:
    """Minimal MCP-compatible server that exposes the ToolRegistry as tools.

    This is NOT a full MCP server process — it's a library that can be
    embedded in the FastAPI app or run standalone. Use the router in
    ``app/routers/mcp.py`` to expose it over HTTP+SSE.
    """

    def __init__(self):
        self._server_name = "zhanlu-skills"
        self._server_version = "1.0.0"

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all registered tools in MCP-compatible format."""
        tools = []
        for name in sorted(registry.list_available()):
            entry = registry.get_entry(name)
            if not entry:
                continue
            schema = entry.schema or {}
            func = schema.get("function", {})
            tools.append({
                "name": name,
                "description": func.get("description", entry.description or name),
                "inputSchema": func.get("parameters", {
                    "type": "object",
                    "properties": {},
                }),
            })
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a tool by name and return MCP-compatible content."""
        entry = registry.get_entry(name)
        if not entry:
            return [{"type": "text", "text": f"Tool '{name}' not found"}]

        try:
            if entry.handler:
                result = await entry.handler(arguments or {}, context=context)
            else:
                result = {"error": f"Tool '{name}' has no handler"}
        except Exception as exc:
            logger.warning("MCP tool call failed for %s: %s", name, exc)
            result = {"error": str(exc)}

        return [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }
        ]

    def list_resources(self) -> list[dict[str, Any]]:
        """Return skills as MCP resources (resource://skill/<name>)."""
        resources = []
        for name in sorted(registry.list_available()):
            entry = registry.get_entry(name)
            if not entry:
                continue
            resources.append({
                "uri": f"skill://{name}",
                "name": name,
                "description": entry.description or name,
                "mimeType": "application/json",
            })
        return resources

    def read_resource(self, uri: str) -> list[dict[str, Any]]:
        """Read a skill resource by URI (skill://<name>)."""
        if not uri.startswith("skill://"):
            return [{"type": "text", "text": f"Unknown resource URI: {uri}"}]

        name = uri[len("skill://"):]
        entry = registry.get_entry(name)
        if not entry:
            return [{"type": "text", "text": f"Skill '{name}' not found"}]

        return [
            {
                "type": "text",
                "text": json.dumps({
                    "name": name,
                    "description": entry.description,
                    "schema": entry.schema,
                }, ensure_ascii=False, default=str),
            }
        ]


# Singleton
_zhanlu_mcp: ZhanluMCPServer | None = None


def get_zhanlu_mcp_server() -> ZhanluMCPServer:
    global _zhanlu_mcp
    if _zhanlu_mcp is None:
        _zhanlu_mcp = ZhanluMCPServer()
    return _zhanlu_mcp
