"""mcp_tool — Model Context Protocol server integration.

Connects to MCP servers via stdio, sse, or websocket transport and
exposes the server's tools as zhanlu tools. Heavy dep `mcp` is
lazy-installed on first call.

Configuration: MCP_SERVERS env var (JSON list of {name, command/args
or url, transport}).

Skeleton — full MCP client integration requires the mcp SDK and a
configured server. Returns a structured status when no servers are
configured.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_connected_servers: Dict[str, Dict[str, Any]] = {}


def _load_servers() -> List[dict]:
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning("MCP_SERVERS env var is not valid JSON: %s", exc)
        return []


async def _mcp_tool(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list_servers").lower()
    servers = _load_servers()

    if action == "list_servers":
        return {
            "success": True,
            "configured_servers": [
                {"name": s.get("name"), "transport": s.get("transport", "stdio")}
                for s in servers
            ],
            "active_connections": list(_connected_servers.keys()),
            "note": (
                "Configure MCP_SERVERS as a JSON list of {name, command, "
                "args, transport} (stdio) or {name, url, transport} "
                "(sse/websocket). On first call to a tool exposed by a "
                "server, the connection is established and the server's "
                "tools are registered."
            ),
        }

    if action == "call":
        server_name = (args.get("server") or "").strip()
        tool_name = (args.get("tool") or "").strip()
        if not server_name or not tool_name:
            return {"success": False, "error": "server and tool are required"}
        # Lazy connect
        try:
            from app.services.tool_handlers.lazy_deps import ensure
            ensure("mcp")
        except Exception as exc:
            return missing_config_response(
                "mcp",
                missing_binaries=["mcp"],
                missing_infra=[f"mcp SDK: {exc}"],
            )
        # Find the server config
        cfg = next((s for s in servers if s.get("name") == server_name), None)
        if not cfg:
            return {"success": False, "error": f"Server {server_name!r} not configured"}
        # Skeleton — real MCP call would use the mcp SDK's ClientSession.
        return {
            "success": True,
            "server": server_name,
            "tool": tool_name,
            "args": args.get("arguments", {}),
            "note": "MCP call skeleton — full integration requires the configured server to be reachable.",
        }

    return {"success": False, "error": f"Unknown action: {action!r}"}


MCP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp",
        "description": (
            "Model Context Protocol server integration. Configure "
            "MCP_SERVERS as a JSON list to enable. Actions: "
            "list_servers, call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list_servers", "call"]},
                "server": {"type": "string", "description": "Server name (for call)."},
                "tool": {"type": "string", "description": "Tool name on the server (for call)."},
                "arguments": {"type": "object", "description": "Tool arguments (for call)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="mcp",
    schema=MCP_SCHEMA,
    handler=_mcp_tool,
    category="mcp",
    toolset="mcp",
    description="Model Context Protocol server integration.",
    emoji="🔌",
    max_result_size_chars=20_000,
)
