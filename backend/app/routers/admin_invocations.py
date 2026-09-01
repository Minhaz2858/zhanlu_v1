"""Admin agent-observability endpoints (2026-08-29).

Read-only surface over ``agent_invocations`` (one row per conversation turn /
sub-agent run):

- GET /api/admin/invocations                  — paginated, filterable list
- GET /api/admin/invocations/summary          — aggregates (cost, latency, status)
- GET /api/admin/invocations/{invocation_id}  — full detail + parent/children
- GET /api/admin/invocations/conversations/{conversation_id} — per-conversation turns

All endpoints require the ``admin`` role (``require_admin``), mirroring
admin_users.py / admin_evals.py. Null metrics (token_usage, cost_amount,
confidence_score) are returned as None — the UI renders "—".
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.models.agent_invocation import AgentInvocation
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/invocations", tags=["admin-invocations"])

_ACCEPT_STATUSES = {"completed"}


def _row_to_dict(r: AgentInvocation) -> dict:
    return {
        "id": r.id,
        "agent_app_id": r.agent_app_id,
        "conversation_id": r.conversation_id,
        "execution_id": r.execution_id,
        "user_id": r.user_id,
        "invocation_type": r.invocation_type,
        "trigger": r.trigger,
        "status": r.status,
        "input_excerpt": (r.input_message or "")[:200],
        "assistant_excerpt": (r.assistant_content or "")[:200],
        "error_message": r.error_message,
        "confidence_score": r.confidence_score,
        "duration_ms": r.duration_ms,
        "token_usage": r.token_usage,
        "cost_amount": r.cost_amount,
        "trace_id": r.trace_id,
        "span_id": r.span_id,
        "model_name": r.model_name,
        "tool_call_count": r.tool_call_count,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "created_date": r.created_date.isoformat() if r.created_date else None,
    }


@router.get("")
def list_invocations(
    conversation_id: str | None = None,
    agent_app_id: str | None = None,
    invocation_type: str | None = None,
    status: str | None = None,
    user_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    q = db.query(AgentInvocation)
    if conversation_id:
        q = q.filter(AgentInvocation.conversation_id == conversation_id)
    if agent_app_id:
        q = q.filter(AgentInvocation.agent_app_id == agent_app_id)
    if invocation_type:
        q = q.filter(AgentInvocation.invocation_type == invocation_type)
    if status:
        q = q.filter(AgentInvocation.status == status)
    if user_id:
        q = q.filter(AgentInvocation.user_id == user_id)
    if date_from:
        try:
            q = q.filter(AgentInvocation.created_date >= datetime.fromisoformat(date_from))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid date_from: {date_from}")
    if date_to:
        try:
            q = q.filter(AgentInvocation.created_date <= datetime.fromisoformat(date_to))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid date_to: {date_to}")

    total = q.count()
    rows = (
        q.order_by(AgentInvocation.created_date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "count": len(rows),
        "items": [_row_to_dict(r) for r in rows],
    }


@router.get("/summary")
def invocation_summary(
    date_from: str | None = None,
    date_to: str | None = None,
    agent_app_id: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    q = db.query(AgentInvocation)
    if date_from:
        try:
            q = q.filter(AgentInvocation.created_date >= datetime.fromisoformat(date_from))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid date_from: {date_from}")
    if date_to:
        try:
            q = q.filter(AgentInvocation.created_date <= datetime.fromisoformat(date_to))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid date_to: {date_to}")
    if agent_app_id:
        q = q.filter(AgentInvocation.agent_app_id == agent_app_id)

    rows = q.all()
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "by_status": {}, "by_type": {}, "by_agent": {},
            "totals": {"sum_cost": 0.0, "avg_duration_ms": 0.0, "avg_confidence": None,
                       "success_rate": 0.0, "turns_with_cost": 0, "turns_with_model": 0},
            "daily": [],
        }

    by_status: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    agent_stats: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "success": 0, "sum_cost": 0.0, "cost_count": 0,
                 "sum_duration": 0, "duration_count": 0, "sum_conf": 0.0, "conf_count": 0,
                 "sum_tools": 0, "tool_count": 0}
    )
    daily: dict[str, dict] = defaultdict(lambda: {"count": 0, "sum_cost": 0.0, "cost_count": 0})

    for r in rows:
        by_status[r.status or "unknown"] += 1
        by_type[r.invocation_type or "unknown"] += 1
        st = agent_stats[r.agent_app_id or "unknown"]
        st["count"] += 1
        if r.status in _ACCEPT_STATUSES:
            st["success"] += 1
        if r.cost_amount is not None:
            st["sum_cost"] += r.cost_amount
            st["cost_count"] += 1
        if r.duration_ms is not None:
            st["sum_duration"] += r.duration_ms
            st["duration_count"] += 1
        if r.confidence_score is not None:
            st["sum_conf"] += r.confidence_score
            st["conf_count"] += 1
        if r.tool_call_count is not None:
            st["sum_tools"] += r.tool_call_count
            st["tool_count"] += 1
        d = r.created_date.date().isoformat() if r.created_date else None
        if d:
            daily[d]["count"] += 1
            if r.cost_amount is not None:
                daily[d]["sum_cost"] += r.cost_amount
                daily[d]["cost_count"] += 1

    total_cost = sum(s["sum_cost"] for s in agent_stats.values())
    cost_rows = sum(s["cost_count"] for s in agent_stats.values())
    dur_rows = sum(s["duration_count"] for s in agent_stats.values())
    conf_rows = sum(s["conf_count"] for s in agent_stats.values())
    model_rows = sum(1 for r in rows if r.model_name)
    success = sum(1 for r in rows if r.status in _ACCEPT_STATUSES)

    return {
        "total": total,
        "by_status": dict(by_status),
        "by_type": dict(by_type),
        "by_agent": {
            agent: {
                "count": s["count"],
                "success_rate": round(s["success"] / max(s["count"], 1), 3),
                "sum_cost": round(s["sum_cost"], 4),
                "avg_duration_ms": round(s["sum_duration"] / max(s["duration_count"], 1), 1)
                if s["duration_count"] else None,
                "avg_confidence": round(s["sum_conf"] / max(s["conf_count"], 1), 3)
                if s["conf_count"] else None,
                "avg_tool_calls": round(s["sum_tools"] / max(s["tool_count"], 1), 2)
                if s["tool_count"] else None,
            }
            for agent, s in sorted(agent_stats.items())
        },
        "totals": {
            "sum_cost": round(total_cost, 4),
            "turns_with_cost": cost_rows,
            "avg_duration_ms": round(sum(s["sum_duration"] for s in agent_stats.values()) / max(dur_rows, 1), 1)
            if dur_rows else 0.0,
            "avg_confidence": round(sum(s["sum_conf"] for s in agent_stats.values()) / max(conf_rows, 1), 3)
            if conf_rows else None,
            "success_rate": round(success / max(total, 1), 3),
            "turns_with_model": model_rows,
        },
        "daily": [
            {"date": d, "count": v["count"], "sum_cost": round(v["sum_cost"], 4)}
            for d, v in sorted(daily.items())
        ],
    }


@router.get("/{invocation_id}")
def invocation_detail(
    invocation_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    row = db.query(AgentInvocation).filter(
        AgentInvocation.id == invocation_id,
        AgentInvocation.is_deleted == False,  # noqa: E712
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Invocation not found")

    detail = _row_to_dict(row)
    detail["input_message"] = row.input_message
    detail["assistant_content"] = row.assistant_content
    detail["output_json"] = row.output_json
    detail["input_json"] = row.input_json

    # Children (sub-agents spawned from this turn) link via span_id == this id.
    children = db.query(AgentInvocation).filter(
        AgentInvocation.span_id == row.id,
        AgentInvocation.is_deleted == False,  # noqa: E712
    ).all()
    detail["children"] = [_row_to_dict(c) for c in children]

    # Parent (the turn that spawned this sub-agent) links via this.span_id.
    if row.span_id:
        parent = db.query(AgentInvocation).filter(
            AgentInvocation.id == row.span_id,
            AgentInvocation.is_deleted == False,  # noqa: E712
        ).first()
        detail["parent"] = _row_to_dict(parent) if parent else None
    else:
        detail["parent"] = None

    return detail


@router.get("/conversations/{conversation_id}")
def conversation_invocations(
    conversation_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    rows = (
        db.query(AgentInvocation)
        .filter(
            AgentInvocation.conversation_id == conversation_id,
            AgentInvocation.is_deleted == False,  # noqa: E712
        )
        .order_by(AgentInvocation.created_date.desc())
        .limit(limit)
        .all()
    )
    return {"count": len(rows), "items": [_row_to_dict(r) for r in rows]}
