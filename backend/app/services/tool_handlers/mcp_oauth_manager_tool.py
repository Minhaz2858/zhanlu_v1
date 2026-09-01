"""mcp_oauth_manager tool — manage OAuth tokens for multiple MCP servers.

Skeleton — would persist tokens to disk and refresh on expiry.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _mcp_oauth_manager(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    return {
        "success": True,
        "action": action,
        "note": "mcp_oauth_manager skeleton — list/refresh/clear per-server tokens.",
    }


MCP_OAUTH_MANAGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_oauth_manager",
        "description": "Manage per-MCP-server OAuth tokens. Skeleton.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "refresh", "clear"]},
                "server": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="mcp_oauth_manager",
    schema=MCP_OAUTH_MANAGER_SCHEMA,
    handler=_mcp_oauth_manager,
    category="mcp",
    toolset="mcp",
    description="MCP OAuth token manager.",
    emoji="🗝️",
)
