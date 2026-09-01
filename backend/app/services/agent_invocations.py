"""agent_invocations recorder — per-run trace + cost for every agent execution.

The ``agent_invocations`` table has existed with a full schema since the
harness scaffolding, but nothing ever wrote to it (live count was 0 rows).
This module is the single writer: the main chat loop records one row per
conversation turn; the delegate_task tool records one row per sub-agent run
(with parent linkage via ``execution_id``/``span_id``).

Columns mapped (see app/models/agent_invocation.py):
  agent_app_id, conversation_id, execution_id, user_id, invocation_type,
  trigger, input_message, input_json, status, output_json,
  assistant_content, error_message, confidence_score, started_at,
  completed_at, duration_ms, token_usage, cost_amount, trace_id, span_id

Best-effort by design: recording must NEVER crash the chat loop — every
failure is caught and logged (non-fatal).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_NAME = "general_assistant"


def _resolve_agent_app_id(db: Session, agent_app_id: Optional[str]) -> Optional[str]:
    """Resolve a missing app id by name lookup (no hardcoded UUIDs)."""
    if agent_app_id:
        return agent_app_id
    try:
        from app.models.agent_app import AgentApp

        app = db.query(AgentApp).filter(AgentApp.name == _DEFAULT_AGENT_NAME).first()
        return app.id if app else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_invocations: app resolution failed: %s", exc)
        return None


def record_invocation(
    db: Session,
    *,
    agent_app_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    invocation_type: str = "conversation",
    trigger: str = "user",
    input_message: Optional[str] = None,
    input_json: Optional[dict] = None,
    status: str = "completed",
    output_json: Optional[dict] = None,
    assistant_content: Optional[str] = None,
    error_message: Optional[str] = None,
    confidence_score: Optional[float] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    token_usage: Optional[dict] = None,
    cost_amount: Optional[float] = None,
    trace_id: Optional[str] = None,
    parent_invocation_id: Optional[str] = None,
    model_name: Optional[str] = None,
    tool_call_count: Optional[int] = None,
) -> Optional[str]:
    """Insert one agent_invocation row. Returns the new row id (or None).

    All fields optional except db. ``invocation_type`` should be one of
    ``conversation`` (main loop) or ``sub_agent`` (delegate_task).
    """
    try:
        from app.models.agent_invocation import AgentInvocation

        resolved_app_id = _resolve_agent_app_id(db, agent_app_id)
        if not resolved_app_id:
            logger.warning("agent_invocations: no agent app to bind — skipping row")
            return None

        now = datetime.now(timezone.utc)
        row = AgentInvocation(
            agent_app_id=resolved_app_id,
            conversation_id=conversation_id,
            execution_id=execution_id or str(uuid.uuid4()),
            user_id=user_id,
            invocation_type=invocation_type,
            trigger=trigger,
            input_message=(input_message or "")[:2000] or None,
            input_json=input_json,
            status=status,
            output_json=output_json,
            assistant_content=(assistant_content or "")[:10000] or None,
            error_message=(error_message or "")[:2000] or None,
            confidence_score=confidence_score,
            started_at=started_at or now,
            completed_at=completed_at or now,
            duration_ms=duration_ms,
            token_usage=token_usage,
            cost_amount=cost_amount,
            trace_id=trace_id,
            span_id=parent_invocation_id,
            model_name=(model_name or "")[:128] or None,
            tool_call_count=tool_call_count,
        )
        db.add(row)
        db.commit()
        return row.id
    except Exception as exc:  # noqa: BLE001 — recording must never break the loop
        logger.warning("agent_invocations: failed to record (%s) — non-fatal", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
