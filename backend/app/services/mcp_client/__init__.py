"""MCP client (2026 spec) — connect Zhanlu to external MCP servers.

Built on the official ``mcp`` SDK (v1.x): stdio and streamable-HTTP
transports, protocol-version negotiation, capabilities, and sessions.
A :class:`McpConnection` wraps one connected server and exposes the
tool/resource/prompt surfaces to the agent stack.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, ListToolsResult

logger = logging.getLogger(__name__)


@dataclass
class McpToolInfo:
    """One tool exposed by a connected MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sdk(cls, tool: Any) -> "McpToolInfo":
        return cls(
            name=tool.name,
            description=getattr(tool, "description", "") or "",
            input_schema=(
                tool.inputSchema.model_dump()
                if hasattr(tool.inputSchema, "model_dump")
                else dict(tool.inputSchema or {})
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class McpConnection:
    """A live connection to one MCP server (SDK session + transports).

    Owns the AsyncExitStack so ``close()`` tears down stdio processes and
    HTTP sessions cleanly.
    """

    server_name: str
    transport: str  # "stdio" | "http"
    _session: ClientSession
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    server_info: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)

    async def list_tools(self) -> list[McpToolInfo]:
        result: ListToolsResult = await self._session.list_tools()
        return [McpToolInfo.from_sdk(t) for t in result.tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a tool and normalize the result for the Zhanlu tool shape."""
        result: CallToolResult = await self._session.call_tool(
            name, arguments or {},
        )
        text_parts = [
            c.text for c in (result.content or [])
            if getattr(c, "type", None) == "text" and getattr(c, "text", None)
        ]
        if getattr(result, "isError", False):
            return {
                "success": False,
                "error": "\n".join(text_parts) or json_dumps(result.model_dump()),
            }
        if text_parts:
            return {"success": True, "result": "\n".join(text_parts)}
        if result.structuredContent is not None:
            return {
                "success": True,
                "result": json_dumps(result.structuredContent),
                "structured": True,
            }
        return {"success": True, "result": json_dumps(result.model_dump())}

    async def list_resources(self) -> list[dict[str, Any]]:
        result = await self._session.list_resources()
        return [r.model_dump() for r in result.resources]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._session.read_resource(uri)
        return result.model_dump()

    async def list_prompts(self) -> list[dict[str, Any]]:
        result = await self._session.list_prompts()
        return [p.model_dump() for p in result.prompts]

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP connection close failed for %s: %s", self.server_name, exc)


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


async def connect_stdio(
    server_name: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> McpConnection:
    """Connect to a stdio MCP server (spawned subprocess, JSON-RPC over pipes)."""
    stack = AsyncExitStack()
    params = StdioServerParameters(
        command=command,
        args=args or [],
        env=env or None,
    )
    try:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
    except Exception:
        await stack.aclose()
        raise

    conn = McpConnection(
        server_name=server_name,
        transport="stdio",
        _session=session,
        _stack=stack,
        server_info=(
            init.serverInfo.model_dump() if hasattr(init.serverInfo, "model_dump") else {}
        ),
        capabilities=(
            init.capabilities.model_dump()
            if hasattr(init.capabilities, "model_dump")
            else {}
        ),
    )
    logger.info(
        "MCP stdio connection established: %s (server=%s, protocol=%s)",
        server_name, conn.server_info.get("name"), init.protocolVersion,
    )
    return conn


async def connect_http(
    server_name: str,
    base_url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> McpConnection:
    """Connect to a streamable-HTTP MCP server (2026 spec transport)."""
    stack = AsyncExitStack()
    url = base_url.rstrip("/") + "/mcp" if not base_url.rstrip("/").endswith("/mcp") else base_url.rstrip("/")
    try:
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(url, headers=headers or {}, timeout=timeout_s)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
    except Exception:
        await stack.aclose()
        raise

    conn = McpConnection(
        server_name=server_name,
        transport="http",
        _session=session,
        _stack=stack,
        server_info=(
            init.serverInfo.model_dump() if hasattr(init.serverInfo, "model_dump") else {}
        ),
        capabilities=(
            init.capabilities.model_dump()
            if hasattr(init.capabilities, "model_dump")
            else {}
        ),
    )
    logger.info(
        "MCP HTTP connection established: %s (url=%s, server=%s)",
        server_name, url, conn.server_info.get("name"),
    )
    return conn


__all__ = [
    "McpConnection",
    "McpToolInfo",
    "connect_stdio",
    "connect_http",
]
