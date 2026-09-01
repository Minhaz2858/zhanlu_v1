"""interrupt tool — set/clear a per-conversation interrupt flag.

The agent runtime can poll this flag between tool calls to allow the
user to abort a long-running task. The chat UI sets the flag via a
SSE-side command; the agent checks it before continuing.

The flag is held in an in-process dict keyed by conversation_id.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_flags: dict[str, bool] = {}


def is_interrupted(conversation_id: str) -> bool:
    with _lock:
        return _flags.get(conversation_id, False)


def clear_interrupt(conversation_id: str) -> None:
    with _lock:
        _flags.pop(conversation_id, None)


async def _interrupt(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "set").lower()
    conversation_id = (args.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = (context or {}).get("conversation_id", "") if context else ""
    if not conversation_id:
        return {"success": False, "error": "conversation_id is required (or pass via context)"}
    if action == "set":
        with _lock:
            _flags[conversation_id] = True
        return {"success": True, "conversation_id": conversation_id, "interrupted": True}
    if action == "clear":
        clear_interrupt(conversation_id)
        return {"success": True, "conversation_id": conversation_id, "interrupted": False}
    if action == "check":
        return {"success": True, "conversation_id": conversation_id, "interrupted": is_interrupted(conversation_id)}
    return {"success": False, "error": f"Unknown action: {action!r}"}


INTERRUPT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "interrupt",
        "description": (
            "Set, clear, or check the per-conversation interrupt flag. "
            "The agent runtime checks this flag between tool calls; when "
            "set, the agent should abort the current task and return a "
            "summary of progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "clear", "check"]},
                "conversation_id": {"type": "string", "description": "Conversation id (defaults to current context)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="interrupt",
    schema=INTERRUPT_SCHEMA,
    handler=_interrupt,
    category="ux",
    toolset="ux",
    description="Set/clear/check the per-conversation interrupt flag.",
    emoji="⏸️",
    # The interrupt flag is set by the UI via an SSE-side command and polled
    # by the runtime between tool calls (`is_interrupted`). Exposing the tool
    # to the LLM is pure waste: weak function-calling models poll it every
    # step ("interrupt(action=check)") and burn the whole tool budget. The
    # tool stays registered/callable for agents that explicitly enable it via
    # tool_config, but is off the default LLM tool list.
    enabled_by_default=False,
)
