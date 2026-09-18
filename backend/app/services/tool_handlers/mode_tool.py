"""mode_get / mode_set — the agent reads and changes its own operating mode.

Phase 1d.  The mode lives in ``conversation.metadata_["agent_mode"]``, the same
convention as ``permission_mode``, and is enforced in ``execute_tool`` (see
``app/services/harness/modes.py``).

These two tools are agent scratch state, not user-facing resources, so they are
in ``NEVER_GATE`` and stay callable in plan mode — otherwise the model could
enter plan mode and never get out of it.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.harness.modes import (
    AGENT_MODE_KEY,
    VALID_MODES,
    get_mode,
    make_mode_patch,
    plan_from_todos,
    plan_progress,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

MODE_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mode_get",
        "description": (
            "Read the current operating mode (execute or plan) for this "
            "conversation, plus the current task list that holds your plan."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

MODE_SET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mode_set",
        "description": (
            "Switch this conversation between 'plan' and 'execute'. In plan mode "
            "read-only tool calls run normally but write, destructive and "
            "externally-visible calls are NOT executed - they come back marked "
            "plan_mode with a message. Use plan mode to investigate and write a "
            "plan with the todo tool, then switch to execute once the user has "
            "approved it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(VALID_MODES),
                    "description": "'plan' to propose only, 'execute' to act.",
                },
            },
            "required": ["mode"],
        },
    },
}


def _conversation_id(context: Optional[dict]) -> str:
    if isinstance(context, dict):
        return str(context.get("conversation_id") or "")
    return ""


async def _mode_get(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    conv_id = _conversation_id(context)
    metadata = context.get("conversation_metadata") if isinstance(context, dict) else None
    steps = plan_from_todos(db, conv_id) if (db is not None and conv_id) else []
    return {
        "success": True,
        "mode": get_mode(metadata),
        "available_modes": list(VALID_MODES),
        "plan_source": "todo list (AgentTodo) - your own task list is the plan",
        "plan": steps,
        "plan_progress": plan_progress(steps),
    }


async def _mode_set(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    requested = args.get("mode")
    if not isinstance(requested, str) or requested.strip().lower() not in VALID_MODES:
        return {
            "success": False,
            "error": f"mode must be one of: {', '.join(VALID_MODES)}",
        }
    mode = requested.strip().lower()

    conv_id = _conversation_id(context)
    if db is None or not conv_id:
        return {
            "success": False,
            "error": "No conversation in context; the mode is stored per conversation.",
        }

    try:
        from app.models.agent_conversation import AgentConversation

        conv = (
            db.query(AgentConversation)
            .filter(
                AgentConversation.id == conv_id,
                AgentConversation.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if conv is None:
            return {"success": False, "error": f"Conversation {conv_id} not found"}
        # Reassign (not in-place) so SQLAlchemy sees the JSON column change.
        merged = dict(conv.metadata_ or {})
        merged.update(make_mode_patch(mode))
        conv.metadata_ = merged
        db.commit()
    except Exception as err:
        logger.warning("mode_set failed for conversation %s: %s", conv_id, err)
        try:
            db.rollback()
        except Exception:
            pass
        return {"success": False, "error": f"Could not set mode: {err}"}

    logger.info("Conversation %s agent mode set to '%s' by tool", conv_id, mode)
    return {
        "success": True,
        "mode": mode,
        "key": AGENT_MODE_KEY,
        "conversation_id": conv_id,
        "note": (
            "Writes, destructive and externally-visible tool calls will be "
            "proposed instead of executed."
            if mode == "plan"
            else "Tools now execute normally (destructive actions still need approval)."
        ),
    }


registry.register(
    name="mode_get",
    schema=MODE_GET_SCHEMA,
    handler=_mode_get,
    category="planning",
    toolset="planning",
    description="Read the current operating mode and the plan held in the task list.",
    emoji="🧭",
    max_result_size_chars=8_000,
    # Agent scratch state: must stay callable in plan mode or the model cannot
    # read the mode it is in.
    risk="read",
)

registry.register(
    name="mode_set",
    schema=MODE_SET_SCHEMA,
    handler=_mode_set,
    category="planning",
    toolset="planning",
    description="Switch between plan mode (propose only) and execute mode.",
    emoji="🔀",
    max_result_size_chars=4_000,
    # A write, but agent scratch state — see NEVER_GATE in
    # app/services/permissions/approval.py. In plan mode the model must be able
    # to switch back to execute.
    risk="write",
)
