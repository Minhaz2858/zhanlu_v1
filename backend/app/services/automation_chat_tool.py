"""Chat-agent tool: run an AutomationTask now and return its output.

Used by the chat agent when the user types something like
"Run my Daily Sales Data Sync now". Validates ownership, enqueues the
run via the existing dispatcher, polls briefly, and returns the output.

Handler signature matches what the tool registry / tool_retry dispatcher
expects: ``async def handler(args, db, user_id, *, context=None) -> dict``
with the result carrying a ``success`` flag.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.user import User

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5
# Poll window for the final run state. Must cover typical run durations
# (~95s observed) so the chat turn can confirm the DELIVERED result, not
# just "Running". Safe to block this long: the v3 SSE stream emits 5s
# heartbeat pings (see agents._sse_with_heartbeat) and nginx allows 120s
# per read gap. Override via ZHANLU_EXECUTE_AUTOMATION_POLL_S.
_MAX_POLL_S = max(1.0, float(os.environ.get("ZHANLU_EXECUTE_AUTOMATION_POLL_S", "90")))


def _user_can_run(db: Session, user: User, task: AutomationTask) -> bool:
    """Owner check. Team-share is not implemented yet."""
    return task.created_by_id == user.id


def _resolve_task(db: Session, args: dict, user: User) -> Optional[AutomationTask]:
    if args.get("task_id"):
        return db.query(AutomationTask).filter(
            AutomationTask.id == args["task_id"],
            AutomationTask.is_deleted == False,  # noqa: E712
        ).first()
    if args.get("name"):
        like = args["name"].strip().lower()
        candidates = db.query(AutomationTask).filter(
            AutomationTask.created_by_id == user.id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).all()
        for t in candidates:
            if like in (t.name or "").lower():
                return t
    return None


def _find_in_flight_execution(db: Session, task_id: str):
    """Return the task's currently queued/running execution, if any.

    Idempotency guard: the chat LLM sometimes re-calls execute_automation
    in the same turn (e.g. when the first result came back "running").
    Without this check every call spawned a NEW execution — 5 duplicate
    runs were observed in a single chat turn. Attaching to the in-flight
    run gives the re-call a meaningful answer without duplicating work.
    """
    from datetime import datetime, timezone
    from app.models.automation_execution import AutomationExecution
    return db.query(AutomationExecution).filter(
        AutomationExecution.automation_task_id == task_id,
        AutomationExecution.status.in_(("queued", "running")),
        AutomationExecution.is_deleted == False,  # noqa: E712
        # Skip expired/stale rows — the janitor reaps those; a fresh
        # trigger is more useful than attaching to a zombie.
        (AutomationExecution.timeout_at.is_(None))
        | (AutomationExecution.timeout_at > datetime.now(timezone.utc)),
    ).order_by(AutomationExecution.created_date.desc()).first()


def _poll_execution_status(db: Session, execution_id: str, timeout: float = _MAX_POLL_S):
    """Poll the execution row until status is final or timeout.

    Reads through a FRESH session every iteration. Reusing the caller's
    request-scoped session was a live bug: SQLAlchemy's identity map
    cached the AutomationExecution row on the first SELECT, so every
    later poll returned the stale "running" object and the agent always
    reported "Running" even when the run had completed inside the poll
    window. (The ``db`` argument is kept for signature compatibility but
    is deliberately not used for reads — this function also runs in a
    worker thread via ``asyncio.to_thread``, where a request-scoped
    session is not safe to share.)
    """
    from app.models.automation_execution import AutomationExecution
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with SessionLocal() as poll_db:
            ex = poll_db.query(AutomationExecution).filter(
                AutomationExecution.id == execution_id,
            ).first()
            # Read attributes BEFORE the session closes (objects expire
            # on close by default).
            status = ex.status if ex else None
            output_text = (ex.output_text or "")[:2000] if ex else ""
            error = ex.error if ex else None
        if status in ("completed", "failed"):
            return {
                "status": status,
                "output_text": output_text,
                "error": error,
            }
        time.sleep(_POLL_INTERVAL_S)
    return {
        "status": "running",
        # LLM-facing guidance: the dispatcher posts the completion (or
        # failure) to this same chat session via _notify_chat once the run
        # finishes, so the agent must say the result will arrive here
        # automatically — never tell the user to check back manually or
        # offer to "pull the results later" (there is no such mechanism).
        "note": (
            "The run is still in progress. Its completion result will be "
            "posted to this chat automatically when it finishes. Tell the "
            "user exactly that; do not ask them to check back manually."
        ),
    }


async def execute_automation_tool(
    args: dict,
    db: Session,
    user_id: Optional[str],
    *,
    context: dict | None = None,
) -> dict:
    """Tool handler. ``user_id`` is optional for service-mode; the tool
    refuses to run when missing."""
    if not user_id:
        return {"success": False, "error": "Authentication required."}

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return {"success": False, "error": "User not found."}

    task = _resolve_task(db, args, user)
    if task is None:
        return {
            "success": False,
            "error": f"Task {args.get('task_id') or args.get('name') or ''} not found",
        }
    if not _user_can_run(db, user, task):
        return {"success": False, "error": "That task isn't yours."}

    try:
        # Idempotency: attach to an in-flight run instead of duplicating it.
        in_flight = _find_in_flight_execution(db, task.id)
        if in_flight is not None:
            execution_id = in_flight.id
            attached = True
        else:
            from app.services.automation_dispatcher import (
                trigger_now,
                AUTOMATION_MAX_RECURSION_DEPTH,
                compute_execution_depth,
            )
            from app.services.automation_executor import get_current_execution_id
            # Recursion cap. parent_exec_id is the automation run currently
            # executing in this thread (set by _run_agent_in_conversation), or
            # None when this tool is called from interactive chat. None →
            # top-level spawn (depth 1) — always allowed. When non-None we walk
            # its parent_execution_id chain to find the nesting depth and
            # refuse a child that would exceed the cap.
            parent_exec_id = get_current_execution_id()
            parent_depth = compute_execution_depth(db, parent_exec_id)
            new_depth = parent_depth + 1
            if new_depth > AUTOMATION_MAX_RECURSION_DEPTH:
                logger.warning(
                    "execute_automation_tool: refusing nested spawn (depth %d > cap %d) for task %s",
                    new_depth, AUTOMATION_MAX_RECURSION_DEPTH, task.id,
                )
                return {
                    "success": False,
                    "error": (
                        f"Refusing to run task {task.name!r}: it would exceed "
                        f"the automation recursion cap (depth {new_depth} > "
                        f"{AUTOMATION_MAX_RECURSION_DEPTH}). Break the cycle."
                    ),
                }
            execution_id = await trigger_now(
                task.id, parent_execution_id=parent_exec_id
            )
            attached = False
        if not execution_id:
            return {
                "success": False,
                "error": "Failed to start run: dispatcher returned no execution id",
            }
    except Exception as e:  # pragma: no cover - dispatcher exercised in prod
        logger.exception("execute_automation_tool: dispatcher failed: %s", e)
        return {"success": False, "error": f"Failed to start run: {e}"}

    # Poll on a worker thread so the async event loop stays free for the
    # chat to stream its response.
    result = await asyncio.to_thread(_poll_execution_status, db, execution_id)
    # LLM-facing directive: whatever the outcome, this tool must be called
    # AT MOST ONCE per task per turn. Without this the model re-called it
    # in a loop when the first result was "running" (duplicate runs) or
    # when it found the delivered output unsatisfying.
    single_call_note = (
        "This is the definitive result for this run request — report it to "
        "the user as-is. Do NOT call execute_automation again for this task "
        "in this turn."
    )
    note = result.get("note")
    result["note"] = f"{note} {single_call_note}" if note else single_call_note
    return {"success": True, "execution_id": execution_id, "attached": attached, **result}


# ---------------------------------------------------------------------------
# Tool registry registration (auto-registers on import)
# ---------------------------------------------------------------------------
EXECUTE_AUTOMATION_SCHEMA: dict[str, Any] = {
    # P3-bis: the OpenAI tool-call API requires the
    # ``{"type": "function", "function": {...}}`` wrapper around every
    # tool schema. Without ``type`` the request fails with
    # "tools[N]: missing field `type`" (we hit this on the first
    # /automation "Run Now" smoke test). The CRUD tool lists in
    # ``agent_prompts.py`` use the same wrapper, so this is the
    # canonical shape for any tool that goes through ``get_tools()``.
    "type": "function",
    "function": {
        "name": "execute_automation",
        "description": (
            "Run an existing automation task now and return its output. "
            "Use when the user asks to run / trigger / execute / fire a "
            "named scheduled task. Resolves by task_id or case-insensitive "
            "name match on the user's own tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Exact AutomationTask id (preferred).",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring of the task name. "
                        "Only matches the calling user's own tasks."
                    ),
                },
            },
        },
    },
}


def _register() -> None:
    try:
        from app.services.tool_registry import registry
        # P3-bis: after wrapping the schema in ``{"type": "function",
        # "function": {...}}`` the top-level description is gone — read
        # it from the inner ``function`` block to keep the registry
        # entry populated (registry.register falls back to
        # ``schema.get("function", {}).get("description", "")`` if
        # we pass an empty string here, so this is mostly cosmetic,
        # but explicit > implicit).
        fn_block = EXECUTE_AUTOMATION_SCHEMA.get("function", {})
        registry.register(
            name="execute_automation",
            schema=EXECUTE_AUTOMATION_SCHEMA,
            handler=execute_automation_tool,
            category="automation",
            toolset="automation",
            is_async=True,
            description=fn_block.get("description", ""),
            emoji="⏯",
        )
        logger.info("execute_automation tool registered")
    except Exception:
        logger.exception("execute_automation: registry.register failed")


_register()
