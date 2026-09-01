"""mcp_oauth tool — OAuth flow for MCP servers that require user auth.

Skeleton — full flow uses the mcp SDK's auth helpers.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _mcp_oauth(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "status").lower()
    return {
        "success": True,
        "action": action,
        "note": "mcp_oauth skeleton — handles MCP server OAuth (authorize / callback / refresh / revoke).",
    }


MCP_OAUTH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_oauth",
        "description": "OAuth flow for MCP servers. Skeleton.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "authorize", "callback", "refresh", "revoke"]},
                "server": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="mcp_oauth",
    schema=MCP_OAUTH_SCHEMA,
    handler=_mcp_oauth,
    category="mcp",
    toolset="mcp",
    description="MCP server OAuth flow.",
    emoji="🔐",
)
