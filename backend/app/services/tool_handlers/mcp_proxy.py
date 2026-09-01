"""MCP proxy handler — forwards tool calls to connected MCP servers.

Each connected MCP server is spawned as a subprocess (stdio transport).
The proxy dispatches tool calls to the appropriate server and returns
results in the same format as native tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# In-memory cache of connected MCP server processes and their tool lists.
# Keyed by server name. Production would move this to a connection pool.
_mcp_clients: dict[str, dict[str, Any]] = {}
_mcp_tools_cache: dict[str, list[dict[str, Any]]] = {}


def probe_mcp_server(
    command: str, args: list[str], env: dict[str, str]
) -> dict[str, Any] | None:
    """Probe an MCP server's capabilities by spawning it and calling tools/list.

    This is a best-effort synchronous probe. Production would use the MCP
    SDK's client to negotiate the protocol.

    Returns a dict with 'tools' and 'resources' lists, or None on failure.
    """
    try:
        full_env = os.environ.copy()
        full_env.update(env)

        # Try to spawn and get tools via stdio JSON-RPC
        cmd = [command] + args
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            text=True,
        )

        # Send tools/list request
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        })
        try:
            out, err = proc.communicate(input=request + "\n", timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None

        proc.wait()
        if proc.returncode != 0 and not out.strip():
            logger.debug("MCP probe failed for %s: %s", command, err.strip())
            return None

        # Parse JSON-RPC response
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
                if resp.get("result"):
                    return resp["result"]
            except json.JSONDecodeError:
                continue

        return None
    except Exception as exc:
        logger.debug("MCP probe error for %s: %s", command, exc)
        return None


def register_mcp_tools(server_name: str, tools: list[dict[str, Any]]):
    """Register tools from a connected MCP server in the proxy cache."""
    _mcp_tools_cache[server_name] = tools


def get_mcp_tools_for_server(server_name: str) -> list[dict[str, Any]]:
    """Get cached tools for a connected MCP server."""
    return _mcp_tools_cache.get(server_name, [])


async def _mcp_proxy_handler(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Proxy handler that dispatches to a connected MCP server's tool.

    The tool name is expected to match the MCP tool name registered
    from the connected server.
    """
    tool_name = context.get("tool_name") if context else None
    if not tool_name:
        return {"success": False, "error": "MCP proxy: no tool_name in context"}

    server_name = context.get("mcp_server") if context else None
    if not server_name:
        return {"success": False, "error": "MCP proxy: no mcp_server in context"}

    # For now, return a placeholder — real implementation would
    # spawn the MCP subprocess and make the call via stdio JSON-RPC
    logger.info(
        "MCP proxy: %s → %s (not yet implemented)", server_name, tool_name
    )
    return {
        "success": False,
        "error": f"MCP proxy forwarding not yet implemented for {server_name}/{tool_name}",
    }
