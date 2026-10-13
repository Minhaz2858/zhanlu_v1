"""LLM cost recorder — wires token usage into the cost ledger (IBM Gap 1
completion, 2026-10-13).

The governance layer always had ``CostService.record_llm_cost`` and the
ROI layer aggregates ``cost_ledger`` — but NOTHING called the recorder,
so hard-ROI cost numbers read 0 forever. This module closes the loop:

  1. Request ingest sets the cost context (org, agent, conversation) —
     the tenant context comes from the verified JWT, never the client.
  2. ``llm_service.call_llm`` calls ``record_llm_usage_cost`` on every
     successful completion (usage is already captured there).
  3. The write is fire-and-forget (executor thread, own DB session,
     total try/except) — a cost-bookkeeping failure can never break an
     answer.

``asyncio.to_thread`` propagates contextvars, so the context set in the
v3 handler is visible inside the FSM's thread-pool execution too.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from app.config import settings
from app.database import SessionLocal  # noqa: F401 — patchable in tests

logger = logging.getLogger(__name__)

#: Set at request ingest; read inside the LLM call path. asyncio.to_thread
#: propagates it into thread-pool code (FSM execution) automatically.
llm_cost_context: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("llm_cost_context", default=None)
)


def set_llm_cost_context(
    *,
    org_id: str | None = None,
    agent_name: str | None = None,
    conversation_id: str | None = None,
    execution_id: str | None = None,
    app_id: str | None = None,
) -> None:
    """Set the tenant context the next LLM cost rows will be booked to."""
    llm_cost_context.set({
        "org_id": org_id,
        "agent_name": agent_name,
        "conversation_id": conversation_id,
        "execution_id": execution_id,
        "app_id": app_id,
    })


def _write_cost_row(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Insert one CostLedger row on a dedicated session (sync, executor)."""
    from app.services.governance.cost_service import CostService

    ctx = llm_cost_context.get() or {}
    db = SessionLocal()
    try:
        CostService(db).record_llm_cost(
            model_name=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            execution_id=ctx.get("execution_id"),
            conversation_id=ctx.get("conversation_id"),
            agent_name=ctx.get("agent_name"),
            org_id=ctx.get("org_id") or "default-org",
            app_id=ctx.get("app_id") or "default-app",
        )
    finally:
        db.close()


def record_llm_usage_cost(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Fire-and-forget cost booking for one successful LLM completion.

    Never raises. No-ops when disabled or when the call produced no usage.
    """
    if not getattr(settings, "COST_LEDGER_ENABLED", True):
        return
    if not model or prompt_tokens <= 0 and completion_tokens <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
        # Off the event loop — a single-row insert must never add latency
        # to the answer path.
        loop.run_in_executor(
            None, _safe_write, str(model), int(prompt_tokens), int(completion_tokens)
        )
    except RuntimeError:
        # No running loop (sync callers) — write inline; still guarded.
        _safe_write(str(model), int(prompt_tokens), int(completion_tokens))
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break answers
        logger.debug("llm cost record skipped: %s", exc)


def _safe_write(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    try:
        _write_cost_row(model, prompt_tokens, completion_tokens)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break answers
        logger.debug("llm cost record failed (non-fatal): %s", exc)
