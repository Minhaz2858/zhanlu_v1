"""Agent operating modes — plan vs execute.

The migration spec (§3, Phase 1d) installs this as a *primitive* so that
``turn_planner``'s server-authored plan can be deleted later.  The separation is
deliberate:

* ``turn_planner`` reads the user's text and authors a plan server-side from
  keyword tables (``_KEYWORD_SCHEMA`` / ``_KEYWORD_DATA`` / …).  That is the
  server deciding for the model.
* This module does not author a plan at all.  It only controls **whether
  side-effecting tool calls run** in a turn, and points the model at the
  existing ``todo`` tool (``AgentTodo``) as the place to write its own plan.

Mode resolution mirrors the existing conversation-level permission override
(``conv.metadata_["permission_mode"]``), so there is one convention for
"a setting that applies to this conversation":

    execute (default) — everything runs as it does today.
    plan              — reads run; writes / destructive / externally-visible
                        calls are NOT executed.  Each one comes back as a
                        ``plan_mode`` proposal the model can fold into its plan.

A proposal is not a failure and is never retryable — retrying it would burn the
turn's iteration budget for a decision the model cannot change.

Agent scratch state (``todo``, ``memory``) stays executable in plan mode: the
plan itself lives in the todo list, so blocking it would make the mode unusable.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

AgentMode = Literal["execute", "plan"]

# Same metadata-key convention as permission_mode.
AGENT_MODE_KEY = "agent_mode"

VALID_MODES: tuple[str, ...] = ("execute", "plan")
DEFAULT_MODE: AgentMode = "execute"

# Risk levels that a plan-mode turn may not act on.
_SIDE_EFFECTING_RISKS = frozenset({"write", "destructive", "external"})

# Fallback for tools that are externally visible but declare no risk yet.
# Registry declarations (Phase 1b) take precedence once a tool declares
# ``risk="external"``; this set exists so plan mode is not silently blind to
# message-sending while registrations are migrated.
FALLBACK_EXTERNAL_TOOLS: frozenset[str] = frozenset({
    "send_message",
    "discord",
    "feishu_doc",
    "feishu_drive",
    "x_search",
    "xurl",
    "email",
    "courier",
})


def normalize_mode(value: Any) -> AgentMode:
    """Coerce anything into a valid mode. Unknown → the default."""
    if isinstance(value, str) and value.strip().lower() in VALID_MODES:
        return value.strip().lower()  # type: ignore[return-value]
    return DEFAULT_MODE


def get_mode(conversation_metadata: dict[str, Any] | None) -> AgentMode:
    """The mode in force for a conversation. Never raises."""
    if not isinstance(conversation_metadata, dict):
        return DEFAULT_MODE
    return normalize_mode(conversation_metadata.get(AGENT_MODE_KEY))


def is_plan_mode(conversation_metadata: dict[str, Any] | None) -> bool:
    return get_mode(conversation_metadata) == "plan"


def make_mode_patch(mode: Any) -> dict[str, Any]:
    """The metadata patch that sets the mode (mirrors make_grant in approval.py)."""
    return {AGENT_MODE_KEY: normalize_mode(mode)}


def set_mode_on(conversation_metadata: dict[str, Any] | None, mode: Any) -> dict[str, Any]:
    """Return a NEW metadata dict with the mode applied.

    Returns a copy because SQLAlchemy does not track in-place mutation of a JSON
    column — callers must reassign ``conv.metadata_`` for the change to persist.
    """
    merged = dict(conversation_metadata or {})
    merged.update(make_mode_patch(mode))
    return merged


def tool_needs_execution(
    *,
    tool_name: str,
    risk: str,
    never_gate: bool = False,
) -> bool:
    """True when a call of this risk may run in plan mode.

    Reads and unknown risks run.  Writes / destructive / externally-visible
    calls do not, except agent scratch state (``never_gate``), which the mode
    depends on to hold the plan.
    """
    if never_gate:
        return True
    level = (risk or "").lower()
    if level in _SIDE_EFFECTING_RISKS:
        return False
    if not level and tool_name in FALLBACK_EXTERNAL_TOOLS:
        return False
    return True


def plan_mode_proposal(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    risk: str = "",
    action: str = "",
) -> dict[str, Any]:
    """The tool result returned instead of executing a blocked call.

    Shaped like the approval pause (``success: False`` + a marker the loop
    understands) so the chat loop and the retry classifier treat it as a
    deliberate non-execution rather than an error.
    """
    what = f"{tool_name} (action '{action}')" if action else tool_name
    return {
        "success": False,
        "plan_mode": True,
        # Never retryable — see _is_retryable in agent_tools.
        "retryable": False,
        "tool_name": tool_name,
        "arguments": arguments or {},
        "risk": risk,
        "action": action,
        "message": (
            f"NOT EXECUTED: {what} does not run while the agent is in plan mode. "
            "Record what you intend to do in your task list with the 'todo' tool "
            "(this is your plan), then present it. Call mode_set with mode='execute' "
            "once the user has approved the plan."
        ),
    }


PLAN_MODE_SYSTEM_BLOCK = """## Operating mode: PLAN

You are in plan mode. Read-only work runs normally; write, destructive and
externally-visible tool calls do NOT execute — they come back marked
`plan_mode: true` with a message saying so.

In this mode:
1. Do the reading you need (schema, catalog, data queries, files).
2. Write your plan into your task list with the `todo` tool — that list IS the
   plan; there is no separately generated plan document.
3. Present the plan and wait for the user. Do not claim any blocked action was
   performed.
4. When the user approves, call `mode_set` with mode="execute" and carry the
   plan out."""

EXECUTE_MODE_SYSTEM_BLOCK = """## Operating mode: EXECUTE

You are in execute mode: tools run normally, subject to the usual approval gate
for destructive actions."""


def mode_to_system_block(mode: Any) -> str:
    """Prompt guidance for a mode.

    Exposed for the phase that replaces ``turn_planner.plan_to_system_block``;
    plan mode is already self-teaching through the per-call proposal result, so
    nothing is required to inject this today.
    """
    return PLAN_MODE_SYSTEM_BLOCK if normalize_mode(mode) == "plan" else EXECUTE_MODE_SYSTEM_BLOCK


# ---------------------------------------------------------------------------
# The model's plan lives in AgentTodo — read it back as a plan
# ---------------------------------------------------------------------------

def plan_from_todos(db: Any, conversation_id: str) -> list[dict[str, Any]]:
    """Read the conversation's task list as the current plan.

    This is the replacement path for consumers of ``turn_planner``'s
    server-authored ``TurnPlan``: same information, but authored by the model.
    Returns ``[]`` on any problem — a plan read must never break a turn.
    """
    if not conversation_id:
        return []
    try:
        from app.models.agent_todo import AgentTodo

        row = (
            db.query(AgentTodo)
            .filter(AgentTodo.conversation_id == conversation_id)
            .order_by(AgentTodo.created_date.desc())
            .first()
        )
        if row is None or not isinstance(row.items, list):
            return []
        steps: list[dict[str, Any]] = []
        for item in row.items:
            if not isinstance(item, dict):
                continue
            steps.append({
                "id": item.get("id") or "",
                "title": item.get("content") or item.get("title") or "",
                "status": item.get("status") or "pending",
            })
        return steps
    except Exception as err:
        logger.debug("plan_from_todos failed (ignored): %s", err)
        return []


def plan_progress(steps: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by status, for a 'plan complete?' check."""
    out = {"total": len(steps), "completed": 0, "in_progress": 0, "pending": 0}
    for step in steps:
        status = (step.get("status") or "pending").lower()
        if status in out:
            out[status] += 1
    return out


__all__ = [
    "AgentMode",
    "AGENT_MODE_KEY",
    "VALID_MODES",
    "DEFAULT_MODE",
    "FALLBACK_EXTERNAL_TOOLS",
    "normalize_mode",
    "get_mode",
    "is_plan_mode",
    "make_mode_patch",
    "set_mode_on",
    "tool_needs_execution",
    "plan_mode_proposal",
    "mode_to_system_block",
    "PLAN_MODE_SYSTEM_BLOCK",
    "EXECUTE_MODE_SYSTEM_BLOCK",
    "plan_from_todos",
    "plan_progress",
]
