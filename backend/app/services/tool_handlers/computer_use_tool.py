"""computer_use tool — screenshot + click + type at coordinates.

A simpler alternative to the full browser tool: takes a screenshot of
the desktop (or a window) and lets the agent click/type at screen
coordinates. Useful for controlling native apps.

This is a stub for now — full implementation requires platform-specific
desktop capture (X11/Wayland on Linux, Quartz on macOS, Win32 on
Windows). Returns a structured "platform not configured" error.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _computer_use(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "screenshot").lower()
    if action == "screenshot":
        return {
            "success": False,
            "error": "computer_use is not yet implemented for this platform. Use the 'browser' tool for headless web pages, or implement a platform-specific screencapture helper and register it.",
        }
    return {
        "success": False,
        "error": f"computer_use.{action} is not yet implemented. Use 'browser' for web tasks.",
    }


COMPUTER_USE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": (
            "Desktop screenshot + click + type (computer-use / OSWorld). "
            "Currently returns a structured not-implemented error — use "
            "the 'browser' tool for web tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["screenshot", "click", "type"]},
                "x": {"type": "integer", "description": "X coordinate (for click)."},
                "y": {"type": "integer", "description": "Y coordinate (for click)."},
                "text": {"type": "string", "description": "Text to type (for type)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="computer_use",
    schema=COMPUTER_USE_SCHEMA,
    handler=_computer_use,
    category="browser",
    toolset="browser",
    description="Desktop screenshot + click + type (computer-use).",
    emoji="🖥️",
)
