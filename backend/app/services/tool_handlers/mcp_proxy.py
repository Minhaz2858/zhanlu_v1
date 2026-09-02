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


def _split_command(server_url: str) -> tuple[str, list[str]]:
    """'npx -y @scope/server' -> ('npx', ['-y', '@scope/server'])."""
    import shlex

    parts = shlex.split(server_url or "")
    if not parts:
        raise ValueError("MCP server has an empty command")
    return parts[0], parts[1:]


def list_tools_for_server(server_url: str) -> list[dict[str, Any]]:
    """Live tools/list for a server_url (spawns the process)."""
    command, args = _split_command(server_url)
    result = probe_mcp_server(command, args, {})
    return (result or {}).get("tools") or []


def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: int = 60,
) -> dict[str, Any]:
    """Call a tool on an MCP server via stdio JSON-RPC (tools/call).

    Spawns the server subprocess per call (same lifecycle as the probe).
    Returns {"success": True, "result": <tool result>} or
    {"success": False, "error": ...}.
    """
    import subprocess
    import shlex

    command, args = _split_command(server_url)
    try:
        proc = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        })
        try:
            out, err = proc.communicate(input=request + "\n", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"success": False, "error": f"MCP tool {tool_name!r} timed out after {timeout_s}s"}
        proc.wait()
        if proc.returncode != 0 and not out.strip():
            return {"success": False, "error": f"MCP server exited {proc.returncode}: {err.strip()[:500]}"}
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") != 1:
                continue
            if "error" in resp:
                return {"success": False, "error": str(resp["error"])}
            result = resp.get("result") or {}
            if result.get("isError"):
                return {"success": False, "error": json.dumps(result, ensure_ascii=False)[:500]}
            content = result.get("content") or []
            text_parts = [
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ]
            if text_parts:
                return {"success": True, "result": "\n".join(text_parts), "raw": result}
            if result.get("structuredContent") is not None:
                return {"success": True, "result": json.dumps(result["structuredContent"], ensure_ascii=False), "raw": result}
            return {"success": True, "result": json.dumps(result, ensure_ascii=False), "raw": result}
        return {"success": False, "error": f"No JSON-RPC response from MCP server for tool {tool_name!r}"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("MCP call error %s/%s: %s", server_url, tool_name, exc)
        return {"success": False, "error": str(exc)}


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
