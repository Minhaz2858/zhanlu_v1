"""ChatTurnRecorder — put the chat turn path on the harness run spine.

Before this module, ``agent_runs`` / ``agent_run_steps`` were written only by the
*delegated* execution paths (``routers/agent_runs.py``, ``swarm/runtime.py``,
``research/loop.py``).  An interactive chat turn — the path the user actually
watches — persisted nothing per model call, so there was no per-call history to
read back and ``GET /agent-runs/conversations/{id}`` was empty for chat.

This joins the chat path to the spine without rebuilding anything: it creates one
``AgentRun`` per turn and hands events to the existing ``CheckpointSink``, which
writes one ``AgentRunStep`` per model call / tool call.  Nothing in the loop's
control flow depends on it.

Design rules:

* **Fail-open.** Recording must never change a turn.  Every public method swallows
  its own errors; nothing is raised into the agent loop.  Lost evidence is
  *counted* (``stats()["failures"]``) rather than silently dropped — the same
  discipline as ``rule_events.py``.
* **Flag-gated.** ``HARNESS_CHAT_RUNS_ENABLED`` (default ``1``) is the master
  switch; with it off, no rows are written and the loop behaves as before.
* **One run per turn, including across an approval pause.** The run_id is
  persisted into the conversation's ``_resume_state`` when a turn pauses, so
  resuming continues the same run instead of starting a second one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Event types CheckpointSink persists (it ignores everything else).
_EVENT_LLM = "llm_call"
_EVENT_TOOL = "tool_call"

# Preview caps — the sink truncates too, but keeping these small keeps the JSON
# we hand it cheap to build on the hot path.
_PREVIEW_CHARS = 2000


def chat_runs_enabled() -> bool:
    """Master switch. Any value other than an explicit falsy token means ON."""
    return os.getenv("HARNESS_CHAT_RUNS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


@dataclass
class RecorderStats:
    """Evidence accounting, so a broken recorder is visible rather than silent."""
    model_calls: int = 0
    tool_calls: int = 0
    failures: int = 0
    enabled: bool = True
    run_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "failures": self.failures,
            "enabled": self.enabled,
            "run_id": self.run_id,
        }


@dataclass
class ChatTurnRecorder:
    """Records one chat turn as an ``AgentRun`` + per-call ``AgentRunStep`` rows.

    Usage (all calls are best-effort and may be skipped):

        rec = ChatTurnRecorder(agent_name=..., conversation_id=..., task=user_text)
        rec.start()
        ... per model call:  rec.model_call(iteration=i, messages=llm_messages,
                                            response=llm_response, duration_ms=ms)
        ... per tool call:   rec.tool_call(iteration=i, tool_name=n, tool_args=a,
                                            result_preview=txt)
        rec.finish(status="completed", iterations=n, tool_call_count=m)
    """
    agent_name: str
    conversation_id: str
    task: str
    user_id: str | None = None
    org_id: str | None = None
    app_id: str | None = None
    run_id: str = ""
    enabled: bool | None = None
    _sink: Any = None
    _stats: RecorderStats = field(default_factory=RecorderStats)
    _finished: bool = False

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = chat_runs_enabled()
        self._stats.enabled = bool(self.enabled)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Create the run record. Returns the run_id ('' when disabled/failed)."""
        if not self.enabled or self.run_id:
            return self.run_id
        try:
            from app.services.harness.run_service import AgentRunService
            from app.services.harness.checkpoint_sink import CheckpointSink

            svc = AgentRunService()
            self.run_id = svc.create_run_record(
                agent_name=self.agent_name,
                task=self.task,
                mode="chat",
                caller_context={
                    # list_runs_by_conversation() matches on this key, which is
                    # what makes the chat turn show up in run history.
                    "conversation_id": self.conversation_id,
                    "user_id": self.user_id,
                    "org_id": self.org_id,
                    "app_id": self.app_id,
                },
            )
            # create_run_record leaves the row 'queued'; this turn is running now.
            svc._update_run_started(self.run_id)
            self._sink = CheckpointSink()
            self._stats.run_id = self.run_id
        except Exception as err:
            self._stats.failures += 1
            logger.warning("ChatTurnRecorder: could not start run for conv %s: %s",
                           self.conversation_id, err)
        return self.run_id

    def attach(self, run_id: str) -> None:
        """Continue an existing run (used when a paused turn is resumed)."""
        if not self.enabled or not run_id:
            return
        self.run_id = run_id
        self._stats.run_id = run_id
        if self._sink is None:
            try:
                from app.services.harness.checkpoint_sink import CheckpointSink
                self._sink = CheckpointSink()
            except Exception as err:
                self._stats.failures += 1
                logger.warning("ChatTurnRecorder: could not attach sink: %s", err)

    def finish(
        self,
        *,
        status: str = "completed",
        error: str | None = None,
        iterations: int | None = None,
        tool_call_count: int | None = None,
    ) -> None:
        """Mark the run terminal. Idempotent — a second call is a no-op."""
        if not self.enabled or not self.run_id or self._finished:
            return
        self._finished = True
        try:
            from app.services.harness.run_service import AgentRunService
            svc = AgentRunService()
            if error:
                self._store_error(error)
            svc.finalize_run(
                self.run_id,
                status=status,
                iterations=iterations,
                tool_call_count=tool_call_count,
            )
        except Exception as err:
            self._stats.failures += 1
            logger.warning("ChatTurnRecorder: finalize failed for run %s: %s",
                           self.run_id, err)

    # -- events ------------------------------------------------------------

    def model_call(
        self,
        *,
        iteration: int,
        messages: list | None = None,
        response: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one model call: the request messages and the model's reply."""
        if not self.enabled or not self.run_id:
            return
        try:
            response = response if isinstance(response, dict) else {}
            content = response.get("content") or ""
            tool_names = [
                (tc or {}).get("function", {}).get("name") or (tc or {}).get("name")
                for tc in (response.get("tool_calls") or [])
            ]
            preview = content[:_PREVIEW_CHARS]
            if tool_names:
                preview = f"{preview}\n[tool_calls: {', '.join(n for n in tool_names if n)}]"
            usage = response.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            self._emit({
                "type": _EVENT_LLM,
                "iteration": iteration,
                # Snapshot the request so the exact prompt for this call is
                # recoverable from the persisted row.
                "messages": messages,
                "result_preview": preview,
                "duration_ms": duration_ms,
                "prompt_tokens": usage.get("prompt_tokens") or response.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens") or response.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens") or response.get("total_tokens"),
                "status": "ok",
            })
            self._stats.model_calls += 1
        except Exception as err:
            self._stats.failures += 1
            logger.debug("ChatTurnRecorder.model_call failed (ignored): %s", err)

    def tool_call(
        self,
        *,
        iteration: int,
        tool_name: str,
        tool_args: Any = None,
        result_preview: str | None = None,
        status: str = "ok",
        duration_ms: int | None = None,
    ) -> None:
        """Record one tool call: what was called, with what, and what came back."""
        if not self.enabled or not self.run_id:
            return
        try:
            self._emit({
                "type": _EVENT_TOOL,
                "iteration": iteration,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "result_preview": (result_preview or "")[:_PREVIEW_CHARS],
                "status": status,
                "duration_ms": duration_ms,
            })
            self._stats.tool_calls += 1
        except Exception as err:
            self._stats.failures += 1
            logger.debug("ChatTurnRecorder.tool_call failed (ignored): %s", err)

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counters for evidence accounting (mirrors rule_events.summary())."""
        return self._stats.as_dict()

    # -- internals ---------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        if self._sink is None:
            return
        event.setdefault("run_id", self.run_id)
        self._sink(event)

    def _store_error(self, error: str) -> None:
        try:
            from app.database import SessionLocal
            from app.models.agent_run import AgentRun

            db = SessionLocal()
            try:
                row = db.query(AgentRun).filter(AgentRun.run_id == self.run_id).first()
                if row is not None:
                    row.error = error[:2000]
                    db.commit()
            finally:
                db.close()
        except Exception as err:
            self._stats.failures += 1
            logger.debug("ChatTurnRecorder: could not store error (ignored): %s", err)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def start_recorder(
    *,
    agent_name: str,
    conversation_id: str,
    task: str,
    user_id: str | None = None,
    org_id: str | None = None,
    app_id: str | None = None,
) -> ChatTurnRecorder:
    """Create + start a recorder for a turn. Never raises."""
    rec = ChatTurnRecorder(
        agent_name=agent_name,
        conversation_id=conversation_id,
        task=(task or "")[:4000],
        user_id=user_id,
        org_id=org_id,
        app_id=app_id,
    )
    try:
        rec.start()
    except Exception as err:  # pragma: no cover - start() already guards
        logger.warning("start_recorder failed (ignored): %s", err)
    return rec


def preview_text(value: Any, limit: int = 600) -> str:
    """Compact one-line preview of a tool result for the step row."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
    return text[:limit]


def now_ms() -> int:
    """Monotonic-ish wall clock for duration_ms."""
    return int(time.time() * 1000)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ChatTurnRecorder",
    "RecorderStats",
    "chat_runs_enabled",
    "start_recorder",
    "preview_text",
    "now_ms",
    "utc_stamp",
]


# ── Phase 8.1 Stage 1: AgentRun-to-message linkage for the v3 stream ───────

def record_chat_turn_run(
    db,
    *,
    org_id: str,
    user_id: str | None,
    conversation_id: str,
    agent_name: str,
    task: str,
    tool_calls: list[dict] | None,
    status: str,
) -> str | None:
    """Give a v3 chat turn a real AgentRun WITHOUT changing stream behavior.

    Creates the run (org from the VERIFIED conversation/user context — never
    caller-supplied), appends one ``tool_call`` step per EXECUTED tool call
    (``blocked`` entries are authorization DENIALS, not executions — they are
    never recorded as such), and finalizes with the honest terminal status.
    The provenance envelope (T8-8) is then written by ``finalize_run``.

    Fail-open: any failure returns None and never raises into the stream.
    Flag-gated by the same ``HARNESS_CHAT_RUNS_ENABLED`` switch.
    """
    if not chat_runs_enabled():
        return None
    try:
        from app.services.harness.run_service import AgentRunService

        svc = AgentRunService()
        run_id = svc.create_run_record(
            agent_name=agent_name or "general_assistant",
            task=task or "",
            mode="chat",
            caller_context={
                "org_id": org_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
            },
            db=db,
        )
        executed = [
            tc for tc in (tool_calls or [])
            if isinstance(tc, dict)
            and tc.get("status") not in (None, "blocked", "awaiting_approval",
                                         "awaiting_decision_summary")
        ]
        for index, tc in enumerate(executed, start=1):
            step_status = "error" if tc.get("status") == "error" else "ok"
            svc.append_step(
                run_id, "tool_call", step_index=index,
                tool_name=tc.get("name"), db=db,
            )
            # status correction: append_step writes the model default; the
            # honest per-call status is set explicitly below
            try:
                from app.models.agent_run_step import AgentRunStep
                step = db.query(AgentRunStep).filter_by(
                    run_id=run_id, step_index=index).first()
                if step is not None:
                    step.status = step_status
                    db.commit()
            except Exception:  # noqa: BLE001 — status refinement is best-effort
                pass
        svc.finalize_run(
            run_id, status=status,
            tool_call_count=len(executed), db=db,
        )
        return run_id
    except Exception:  # noqa: BLE001 — fail-open by contract
        return None
