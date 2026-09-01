"""slash_confirm tool — request explicit user confirmation before running a command.

The agent builds its own confirm dialog (via ::options blocks). This tool
records the request so the UI can render it as a structured action that
the user must approve/reject, distinct from ordinary chat.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _slash_confirm(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    command = (args.get("command") or "").strip()
    description = (args.get("description") or "").strip()
    if not command:
        return {"success": False, "error": "command is required"}

    confirm_id = uuid.uuid4().hex[:8]
    payload = {
        "confirm_id": confirm_id,
        "command": command,
        "description": description,
        "issued_at": time.time(),
        "status": "pending",
    }
    conversation_id = (context or {}).get("conversation_id") if context else None
    if conversation_id:
        try:
            from app.services.tool_output import push_event
            push_event(conversation_id, {"type": "slash_confirm", **payload})
        except Exception as exc:
            logger.debug("Could not push slash_confirm event: %s", exc)

    return {
        "success": True,
        "instruction": (
            "Surface this confirmation to the user (queued for the chat "
            "UI). Do NOT run the command until the user approves. The next "
            "user message will be treated as approval/rejection if it "
            "matches one of: 'yes', 'no', 'approve', 'reject'."
        ),
        **payload,
    }


SLASH_CONFIRM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "slash_confirm",
        "description": (
            "Request explicit user confirmation before running a command. "
            "The chat UI will render a confirm dialog; the user's next "
            "message is interpreted as approve/reject. Use for risky "
            "actions the user must explicitly opt into."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command/operation to confirm."},
                "description": {"type": "string", "description": "What the command does, in plain language."},
            },
            "required": ["command"],
        },
    },
}

registry.register(
    name="slash_confirm",
    schema=SLASH_CONFIRM_SCHEMA,
    handler=_slash_confirm,
    category="ux",
    toolset="ux",
    description="Request explicit user confirmation before running a command.",
    emoji="⚠️",
)
