"""Usage analytics API (Phase 3.5) — org-level operational dashboard.

Admin-only, org-scoped to the caller's own organization (isolation wall).
Read-only aggregation over EXISTING tables — no second telemetry system.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.models.user import User
from app.services.usage_analytics import agent_stats, org_overview

router = APIRouter()


@router.get("/usage/overview")
def usage_overview(
    days: Optional[int] = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Org-level operational overview: executions, reliability, failures,
    tokens, honest costs, approvals, unauthorized-attempt counts."""
    return org_overview(db, org_id=user.org_id, days=days)


@router.get("/usage/agents")
def usage_by_agent(
    days: Optional[int] = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Per-agent rollup: invocation counts, success/failure rates, latency,
    token usage, cost honesty (known vs unknown pricing)."""
    return {"agents": agent_stats(db, org_id=user.org_id, days=days)}
