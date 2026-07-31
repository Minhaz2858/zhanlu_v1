"""discord tool — post messages to a Discord channel via webhook.

Uses Discord's incoming webhook API — no bot token required, just a
webhook URL stored in DISCORD_WEBHOOK_URL. For full bot functionality
(token-based), the lazy_deps installer pulls in discord.py on first
use.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _discord(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["DISCORD_WEBHOOK_URL"])
    if missing:
        return missing_config_response("discord", missing_env=missing)

    content = (args.get("content") or "").strip()
    if not content:
        return {"success": False, "error": "content is required"}
    username = (args.get("username") or "Zhanlu Agent").strip()
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    payload = {"content": content, "username": username}
    if args.get("avatar_url"):
        payload["avatar_url"] = args["avatar_url"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        return {"success": False, "error": f"Discord post failed: {exc}"}
    return {"success": True, "channel": "webhook", "content_length": len(content)}


DISCORD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord",
        "description": (
            "Post a message to a Discord channel via webhook. Requires "
            "DISCORD_WEBHOOK_URL. For full bot features (token-based), "
            "configure DISCORD_BOT_TOKEN instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The message content."},
                "username": {"type": "string", "description": "Display name.", "default": "Zhanlu Agent"},
                "avatar_url": {"type": "string", "description": "Optional avatar URL."},
            },
            "required": ["content"],
        },
    },
}

registry.register(
    name="discord",
    schema=DISCORD_SCHEMA,
    handler=_discord,
    category="communication",
    toolset="communication",
    description="Post a message to a Discord channel via webhook.",
    emoji="💬",
    requires_env=["DISCORD_WEBHOOK_URL"],
    max_result_size_chars=2_000,
)
