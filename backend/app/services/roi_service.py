"""ROI service — the business-value layer of the platform (IBM Gap 1).

Every enterprise-AI framework we researched (IBM Think, 2026-10) lands on
the same finding: the #1 reason AI initiatives fail to scale is that
nobody can MEASURE their business value (95% of GenAI pilots fail; only
~29% of executives can measure ROI confidently). The platform already
records rich operational data — this service links it into per-org /
per-agent value metrics so a customer's leadership can see what the
platform returns.

Hard ROI (measured, from real rows):
  - conversations, user turns, answers delivered
  - artifacts produced
  - cost total (from cost_ledger — llm_tokens, sandbox, artifacts…)
  - cost per answered turn

Estimated ROI (clearly labeled heuristic, tunable via settings):
  - est_hours_saved: user turns × ROI_MINUTES_PER_TURN + artifacts ×
    ROI_MINUTES_PER_ARTIFACT — "time an employee would have spent doing
    this manually". An ESTIMATE, not a measurement; the assumptions are
    returned alongside so the customer can adjust them.

Derived:
  - est_value_multiple: est_hours_saved × ROI_HOURLY_RATE / max(cost, ε)
    — "value returned per unit spent" (>1 = ROI positive under the
    stated assumptions).

Org-scoped by construction: every query filters by org_id — an Org Admin
can only ever see their own tenant's value (same wall as auth).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)


def _window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _iter_messages(raw) -> list[dict]:
    """Messages of a conversation row, tolerating legacy shapes."""
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    if isinstance(raw, str):
        import json as _json

        try:
            parsed = _json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
        return [m for m in parsed if isinstance(m, dict)] if isinstance(parsed, list) else []
    return []


def roi_summary(db: Session, org_id: str, days: int = 30, app_id: str | None = None) -> dict:
    """Business-value summary for one org over the trailing window.

    All queries are org-filtered (tenant isolation, same as auth).
    """
    from app.models.agent_conversation import AgentConversation
    from app.models.governance import CostLedger

    days = max(1, min(int(days), 365))
    start = _window_start(days)

    # ── Costs (clean SQL aggregate) ─────────────────────────────────────
    cost_q = (
        db.query(
            CostLedger.agent_name,
            CostLedger.cost_type,
            CostLedger.cost_amount,
        )
        .filter(
            CostLedger.org_id == org_id,
            CostLedger.is_deleted == False,  # noqa: E712
            CostLedger.recorded_at >= start,
        )
    )
    if app_id:
        cost_q = cost_q.filter(CostLedger.app_id == app_id)

    cost_total = 0.0
    cost_by_type: dict[str, float] = {}
    cost_by_agent: dict[str, float] = {}
    for agent_name, cost_type, amount in cost_q.all():
        amt = float(amount or 0.0)
        cost_total += amt
        cost_by_type[cost_type] = cost_by_type.get(cost_type, 0.0) + amt
        key = agent_name or "unknown"
        cost_by_agent[key] = cost_by_agent.get(key, 0.0) + amt

    # ── Conversations & turns (JSON parsed in Python — bounded by window) ─
    conv_q = (
        db.query(AgentConversation)
        .filter(
            AgentConversation.org_id == org_id,
            AgentConversation.is_deleted == False,  # noqa: E712
            AgentConversation.created_date >= start,
        )
    )
    if app_id:
        conv_q = conv_q.filter(AgentConversation.app_id == app_id)

    minutes_per_turn = float(getattr(settings, "ROI_MINUTES_PER_TURN", 10))
    minutes_per_artifact = float(getattr(settings, "ROI_MINUTES_PER_ARTIFACT", 30))
    hourly_rate = float(getattr(settings, "ROI_HOURLY_RATE", 40))

    totals = {
        "conversations": 0,
        "user_turns": 0,
        "assistant_answers": 0,
        "artifacts_created": 0,
        "cost_total": round(cost_total, 2),
        "est_hours_saved": 0.0,
    }
    by_agent: dict[str, dict] = {}

    for conv in conv_q.all():
        agent = conv.agent_name or "unknown"
        msgs = _iter_messages(conv.messages)
        user_turns = sum(1 for m in msgs if m.get("role") == "user")
        answers = sum(1 for m in msgs if m.get("role") == "assistant")
        artifacts = 0
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            artifacts += len(m.get("artifact_ids") or [])
            for a in m.get("artifacts") or []:
                if isinstance(a, dict):
                    artifacts += 1

        slot = by_agent.setdefault(agent, {
            "agent_name": agent,
            "conversations": 0,
            "user_turns": 0,
            "assistant_answers": 0,
            "artifacts_created": 0,
            "cost_total": cost_by_agent.get(agent, 0.0),
            "est_hours_saved": 0.0,
        })
        slot["conversations"] += 1
        slot["user_turns"] += user_turns
        slot["assistant_answers"] += answers
        slot["artifacts_created"] += artifacts
        slot["est_hours_saved"] = round(
            (
                slot["user_turns"] * minutes_per_turn
                + slot["artifacts_created"] * minutes_per_artifact
            ) / 60.0,
            2,
        )

        totals["conversations"] += 1
        totals["user_turns"] += user_turns
        totals["assistant_answers"] += answers
        totals["artifacts_created"] += artifacts

    totals["est_hours_saved"] = round(
        (
            totals["user_turns"] * minutes_per_turn
            + totals["artifacts_created"] * minutes_per_artifact
        ) / 60.0,
        2,
    )

    est_value_multiple = None
    if cost_total > 0 and totals["est_hours_saved"] > 0:
        est_value_multiple = round(
            (totals["est_hours_saved"] * hourly_rate) / cost_total, 2
        )

    return {
        "org_id": org_id,
        "window_days": days,
        "totals": {
            **totals,
            "cost_total": round(cost_total, 2),
            "cost_by_type": {k: round(v, 2) for k, v in sorted(
                cost_by_type.items(), key=lambda kv: -kv[1])},
            "cost_per_answered_turn": (
                round(cost_total / totals["assistant_answers"], 4)
                if totals["assistant_answers"] else None
            ),
            "est_value_multiple": est_value_multiple,
            "est_assumptions": {
                "minutes_per_turn": minutes_per_turn,
                "minutes_per_artifact": minutes_per_artifact,
                "hourly_rate": hourly_rate,
                "note": (
                    "est_hours_saved is an ESTIMATE: manual-time equivalent "
                    "of answered turns and produced artifacts, at the "
                    "assumptions above. Adjust ROI_* settings to your "
                    "workforce."
                ),
            },
        },
        "by_agent": sorted(by_agent.values(), key=lambda a: -a["cost_total"]),
    }
