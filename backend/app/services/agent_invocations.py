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


def _current_trace_id() -> Optional[str]:
    """Read the active OpenTelemetry span's trace id (ADLC P1-6).

    Best-effort: returns None when the OTel SDK is absent or no span is
    active — the agent_invocations.trace_id column stays NULL instead of
    failing the record.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None
        tid = format(ctx.trace_id, "032x")
        return tid if tid != "0" * 32 else None
    except Exception:  # noqa: BLE001 — tracing must never break recording
        return None


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
    route: Optional[str] = None,
    outcome: Optional[str] = None,
    tools_selected: Optional[int] = None,
    delegation_mode: Optional[str] = None,
) -> Optional[str]:
    """Insert one agent_invocation row. Returns the new row id (or None).

    All fields optional except db. ``invocation_type`` should be one of
    ``conversation`` (main loop) or ``sub_agent`` (delegate_task).
    """
    try:
        from app.models.agent_invocation import AgentInvocation

        resolved_app_id = _resolve_agent_app_id(db, agent_app_id)
        if trace_id is None:
            trace_id = _current_trace_id()
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
            route=(route or "")[:32] or None,
            outcome=(outcome or "")[:32] or None,
            tools_selected=tools_selected,
            delegation_mode=(delegation_mode or "")[:16] or None,
        )
        db.add(row)
        db.commit()
        # RL Phase 0: persist the formalized reward for this trajectory
        # (bandit policy updates + SFT/GRPO datasets consume these).
        try:
            from app.services.rl.rewards import compute_reward
            from app.models.trajectory_reward import TrajectoryReward

            reward, components = compute_reward(
                status=status,
                duration_ms=duration_ms,
                tool_call_count=tool_call_count,
                # Wire the caller-supplied judge confidence (0-1) into the
                # quality-bonus component; previously it was always dropped.
                quality_score=(
                    confidence_score
                    if isinstance(confidence_score, (int, float)) else None
                ),
            )
            db.add(TrajectoryReward(
                invocation_id=row.id,
                agent_name=invocation_type,
                route=route,
                reward=reward,
                components=components,
                source="auto",
            ))
            db.commit()
            # RL Phase 1: feed the policy bandit (route arm).
            from app.services.rl.policy_bandit import record_reward

            record_reward(route or "", reward)
        except Exception as _rw_err:  # noqa: BLE001 — rewards are never fatal
            logger.debug("trajectory reward persist failed (non-fatal): %s", _rw_err)
        return row.id
    except Exception as exc:  # noqa: BLE001 — recording must never break the loop
        logger.warning("agent_invocations: failed to record (%s) — non-fatal", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def resolve_invocation_for_feedback(
    db: Session, conversation_id: str, assistant_content: str
) -> Optional[str]:
    """Find the invocation a thumbs rating applies to (best-effort).

    Matches the latest invocations of the conversation against the message
    content (loose prefix match, either direction — the stored
    ``assistant_content`` may be truncated or the message edited).
    """
    try:
        from app.models.agent_invocation import AgentInvocation

        rows = (
            db.query(AgentInvocation)
            .filter(AgentInvocation.conversation_id == conversation_id)
            .order_by(AgentInvocation.created_date.desc())
            .limit(25)
            .all()
        )
        if not rows:
            return None
        needle = (assistant_content or "").strip()[:120]
        if needle:
            for inv in rows:
                content = (inv.assistant_content or "").strip()[:120]
                if content and (
                    needle == content
                    or needle.startswith(content)
                    or content.startswith(needle)
                ):
                    return inv.id
        for inv in rows:  # fallback: the latest main-loop turn
            if inv.invocation_type == "conversation":
                return inv.id
        return rows[0].id
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("agent_invocations: feedback resolve failed: %s", exc)
        return None


def apply_human_feedback(
    db: Session,
    *,
    invocation_id: str,
    rating: int,
    comment: str = "",
) -> Optional[str]:
    """Record a human (HITL) reward override for one invocation.

    Upserts the ``source="human"`` TrajectoryReward (auto rows stay
    untouched) and feeds the route bandit, so explicit user feedback
    steers policy — the feedback loop the RL phases consumed but nothing
    wrote to. Best-effort; never raises.
    """
    try:
        from app.models.agent_invocation import AgentInvocation
        from app.models.trajectory_reward import TrajectoryReward
        from app.services.rl.policy_bandit import record_reward as _bandit_record

        inv = (
            db.query(AgentInvocation)
            .filter(AgentInvocation.id == invocation_id)
            .first()
        )
        if inv is None:
            return None

        reward = 1.0 if rating > 0 else -1.0
        components: dict = {"outcome": reward, "total": reward,
                            "origin": "human_thumbs"}
        if comment:
            components["comment"] = comment[:200]

        row = (
            db.query(TrajectoryReward)
            .filter(
                TrajectoryReward.invocation_id == invocation_id,
                TrajectoryReward.source == "human",
            )
            .first()
        )
        if row is None:
            row = TrajectoryReward(
                invocation_id=invocation_id,
                agent_name=inv.invocation_type,
                route=inv.route,
                reward=reward,
                components=components,
                source="human",
            )
            db.add(row)
        else:
            row.reward = reward
            row.components = components
        db.commit()
        # Human signal steers the bandit (same gate as auto rewards).
        _bandit_record(inv.route or "", reward)
        return row.id
    except Exception as exc:  # noqa: BLE001 — feedback must never 500
        logger.warning("agent_invocations: human feedback failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
