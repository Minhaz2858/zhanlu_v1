"""§5 Agent Invocation Contract (R6 Phase 2) — the SINGLE structured entry
point between the automation runner and the production v3 agent harness.

Spec: docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md §5.

NOTHING in this module exposes SSE text or regex to callers: the v3 stream is
consumed here exactly once, its frames parsed by ``parse_sse_frame`` (the one
centralized parser — the executor's ``_parse_sse_chunk`` delegates here), and
translated to typed ``AgentEvent`` objects.

Naming: this is the SINGULAR ``agent_invocation`` contract module. The user's
plural ``agent_invocations.py`` (trace/cost recorder) is a different module
and must never be modified from here.

Phase-1 scope (spec §5): adapter over ``add_message_stream`` /
``resume_conversation`` with NO chat-path changes. Both historical call
shapes of the v3 handler are supported because the handler is in flux:

* legacy — ``await handler(...)`` → ``StreamingResponse`` → ``.body_iterator``
* current — the handler is itself an async generator yielding SSE frames

Shape detection is structural (``hasattr``), never exception-driven.

Cancellation & deadline (normative per §5): ``ctx.cancellation`` is checked
at every chunk boundary; the wall-clock deadline is enforced at THIS layer —
an expired deadline fails fast before the harness is even invoked, and a
mid-stream deadline stops consumption at the next chunk — both surface as
``run_failed(classification="timeout"|"cancelled")`` followed by a final
``stream_ended``. ``stream_ended`` is ALWAYS the last event on every path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import AsyncIterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

AgentEventType = Literal[
    "phase", "plan_step", "tool_call_started", "tool_call_finished",
    "approval_requested", "delta", "artifact_created", "run_finished",
    "run_failed", "stream_ended",
]

_TERMINAL = ("run_finished", "run_failed")


# ---------------------------------------------------------------------------
# §5 types
# ---------------------------------------------------------------------------

class AutomationRef(BaseModel):
    definition_id: str
    execution_id: str
    attempt: int = 0
    spec_version: str = "v1"
    trigger: str = "schedule"  # schedule | manual | retry | api


class CancellationSignal:
    """asyncio.Event + wall-clock deadline, settable from the (sync) runner.

    ``request_cancel()`` is thread-safe: the runner runs the event loop in a
    dedicated thread and may cancel from another thread, so a threading.Event
    bridge is checked alongside the in-loop asyncio.Event.
    """

    def __init__(self, deadline: Optional[datetime] = None):
        self.deadline = deadline
        self._async_event = asyncio.Event()
        self._thread_event = threading.Event()

    def request_cancel(self) -> None:
        """Settable from the runner — any thread, any time."""
        self._thread_event.set()
        try:
            self._async_event.set()
        except RuntimeError:
            pass  # no running loop in this thread — the bridge covers it

    @property
    def cancelled(self) -> bool:
        return (self._async_event.is_set() or self._thread_event.is_set()
                or self.timed_out)

    @property
    def timed_out(self) -> bool:
        if self.deadline is None:
            return False
        return datetime.now(timezone.utc) >= self.deadline


class ExecutionBudgets(BaseModel):
    """Advisory budgets carried into the harness (deadline is enforced at
    the contract layer; the others are enforced by the harness itself)."""
    max_tool_calls: Optional[int] = None
    max_llm_calls: Optional[int] = None
    max_seconds: Optional[float] = None


class AgentInvocationContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    db: object  # sqlalchemy.orm.Session
    user: object
    org_id: str
    app_id: str
    project_id: Optional[str] = None
    conversation_id: str
    automation: AutomationRef
    cancellation: CancellationSignal
    budgets: ExecutionBudgets = Field(default_factory=ExecutionBudgets)
    trace_id: str


class AgentInvocationRequest(BaseModel):
    message: str
    force_planning: bool = True
    resume: bool = False           # True → wraps resume path
    approval_id: Optional[str] = None


class AgentEvent(BaseModel):
    type: AgentEventType
    seq: int
    occurred_at: datetime
    payload: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Centralized SSE frame parser (the executor's _parse_sse_chunk delegates here)
# ---------------------------------------------------------------------------

def parse_sse_frame(chunk) -> Optional[dict]:
    """Parse one SSE chunk ('data: {json}\\n\\n') into a dict, or None."""
    try:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        # SSE frames may carry multiple fields (event:, data:); the JSON
        # payload lives on the data: field(s) — joined if split across lines.
        data_lines = [ln[len("data:"):].strip()
                      for ln in str(chunk).splitlines()
                      if ln.strip().startswith("data:")]
        if not data_lines:
            return None
        payload = "\n".join(data_lines).strip()
        if not payload:
            return None
        return json.loads(payload)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SSE event → AgentEvent translation (single mapping point, typed output)
# ---------------------------------------------------------------------------

_PHASE_EVENTS = {"phase", "fsm_state"}
_PLAN_EVENTS = {"plan_summary", "plan_step_created", "plan_step_started",
                "plan_step_finished"}
_TOOL_START_EVENTS = {"tool_call_started", "tool_progress"}
_TOOL_FINISH_EVENTS = {"tool_call_finished", "activity_step"}
_APPROVAL_EVENTS = {"approval_requested", "approval_request"}
_ARTIFACT_EVENTS = {"artifact_created"}


def _classify(etype: str) -> Optional[str]:
    if etype in _PHASE_EVENTS:
        return "phase"
    if etype in _PLAN_EVENTS:
        return "plan_step"
    if etype in _TOOL_START_EVENTS:
        return "tool_call_started"
    if etype in _TOOL_FINISH_EVENTS:
        return "tool_call_finished"
    if etype in _APPROVAL_EVENTS:
        return "approval_requested"
    if etype in _ARTIFACT_EVENTS:
        return "artifact_created"
    if etype == "delta":
        return "delta"
    return None


def _step_to_tool_event(step: dict) -> dict:
    """activity_step payload → tool_call_started/finished payload."""
    return {
        "tool_name": step.get("tool") or step.get("name") or "",
        "status": step.get("status") or "running",
        "description": step.get("description"),
        "number": step.get("number"),
    }


# ---------------------------------------------------------------------------
# invoke_agent
# ---------------------------------------------------------------------------

async def invoke_agent(ctx: AgentInvocationContext,
                       req: AgentInvocationRequest,
                       ) -> AsyncIterator[AgentEvent]:
    """Invoke the production v3 agent harness and yield structured events.

    Contract guarantees:
    * ``stream_ended`` is ALWAYS the final event (success, failure, cancel,
      timeout, upstream error — every path).
    * ``run_finished``/``run_failed`` carry provenance
      (status/content_hash/usage/agent_run_id/conversation_id) regardless of
      terminal status — closing audit S5 (conversation id on failure too).
    * Cancellation is honored at every chunk boundary; the wall-clock
      deadline is enforced HERE (expired ⇒ fail-fast ``run_failed(
      classification="timeout")`` without invoking the harness).
    * Partial content is preserved in delta events up to the failure point.
    """
    seq = 0

    async def _emit(etype: str, payload: dict) -> AgentEvent:
        nonlocal seq
        seq += 1
        return AgentEvent(seq=seq, occurred_at=datetime.now(timezone.utc),
                          type=etype, payload=payload)

    # Deadline enforcement BEFORE the harness is invoked (contract layer).
    if ctx.cancellation.timed_out:
        yield await _emit("run_failed", {
            "classification": "timeout",
            "error": "wall-clock deadline exceeded before invocation",
            "agent_run_id": None,
            "conversation_id": ctx.conversation_id,
            "automation": ctx.automation.model_dump(),
        })
        yield await _emit("stream_ended", {})
        return

    # Lazy import (circular-import guard, mirrors automation_executor).
    import app.routers.agents as agents_mod

    if req.resume:
        stream = agents_mod.resume_conversation(
            app_id=ctx.app_id,
            conversation_id=ctx.conversation_id,
            approval_id=req.approval_id,
            db=ctx.db,
            user=ctx.user,
        )
    else:
        stream = agents_mod.add_message_stream(
            app_id=ctx.app_id,
            conversation_id=ctx.conversation_id,
            body={
                "role": "user",
                "content": req.message,
                "force_planning": req.force_planning,
                "phase": "automation",
                "automation_task_id": ctx.automation.definition_id,
                "automation_execution_id": ctx.automation.execution_id,
            },
            db=ctx.db,
            user=ctx.user,
        )

    # Structural shape detection (never exception-driven): legacy handlers
    # return a coroutine → StreamingResponse; the current handler is an
    # async generator yielding SSE frames directly.
    if asyncio.iscoroutine(stream):
        stream = await stream
    if hasattr(stream, "body_iterator"):
        stream = stream.body_iterator

    run_payload: dict = {
        "agent_run_id": None,
        "conversation_id": ctx.conversation_id,
        "status": None,
        "content_hash": None,
        "usage": None,
    }
    terminal_emitted = False

    try:
        async for chunk in stream:
            # Cancellation + deadline at every chunk boundary (§5 normative).
            if ctx.cancellation.cancelled:
                classification = ("timeout" if ctx.cancellation.timed_out
                                  else "cancelled")
                yield await _emit("run_failed", {
                    "classification": classification,
                    "error": "cancelled at chunk boundary",
                    **run_payload,
                    "automation": ctx.automation.model_dump(),
                })
                terminal_emitted = True
                break

            evt = parse_sse_frame(chunk)
            if evt is None:
                continue
            etype = evt.get("type")
            mapped = _classify(etype) if etype else None

            if etype == "message_start":
                run_payload["agent_run_id"] = (
                    evt.get("run_id") or run_payload["agent_run_id"])
                continue
            if etype in ("error", "interrupted", "paused_error"):
                # Legacy failure frames → typed run_failed (terminal).
                yield await _emit("run_failed", {
                    "classification": "error",
                    "error": evt.get("error") or json.dumps(evt),
                    **run_payload,
                    "automation": ctx.automation.model_dump(),
                })
                terminal_emitted = True
                break
            if etype == "done":
                run_payload["status"] = evt.get("completion_status") or "succeeded"
                run_payload["content_hash"] = evt.get("content_hash")
                run_payload["usage"] = evt.get("usage")
                if evt.get("message_id") and not run_payload["agent_run_id"]:
                    run_payload["agent_run_id"] = evt["message_id"]
                continue
            if etype == "run_id":
                run_payload["agent_run_id"] = evt.get("run_id")
                continue

            if mapped is None:
                continue  # unknown/irrelevant frame — never re-emitted raw

            payload = {k: v for k, v in evt.items() if k != "type"}
            if etype == "activity_step":
                payload = _step_to_tool_event(evt.get("step") or {})
            if etype == "paused" or etype == "approval_requested":
                payload.setdefault("approval_id", evt.get("approval_id"))

            yield await _emit(mapped, payload)
            if mapped == "delta":
                # (deadline re-check happens on the NEXT chunk boundary)
                continue
        else:
            pass
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — harness errors are terminal, not silent
        if not terminal_emitted:
            yield await _emit("run_failed", {
                "classification": "error",
                "error": f"{type(e).__name__}: {e}",
                **run_payload,
                "automation": ctx.automation.model_dump(),
            })
            terminal_emitted = True

    if not terminal_emitted and run_payload["status"] is not None:
        yield await _emit("run_finished", {**run_payload})
    elif not terminal_emitted:
        # Stream ended without a terminal frame (silent close) — honest
        # failure, never a fabricated success.
        yield await _emit("run_failed", {
            "classification": "stream_closed",
            "error": "v3 stream ended without a terminal event",
            **run_payload,
            "automation": ctx.automation.model_dump(),
        })

    yield await _emit("stream_ended", {})
