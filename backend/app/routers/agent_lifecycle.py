"""Agent lifecycle + governed catalog routes (ADLC P0-3 / P1-5 / P2-7).

  POST /api/agents/{id}/publish            — snapshot + bump version + publish
  POST /api/agents/{id}/unpublish          — pull from the catalog (draft)
  POST /api/agents/{id}/rollback           — {to_version}; restore snapshot
  GET  /api/agents/{id}/revisions          — immutable revision history
  GET  /api/agents-catalog                 — governed catalog view
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_current_user_required, get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-lifecycle"])


class PublishBody(BaseModel):
    note: str | None = None


class RollbackBody(BaseModel):
    to_version: int


@router.post("/agents/{agent_id}/publish")
def publish_agent(
    agent_id: str,
    body: PublishBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    from app.services import agent_lifecycle

    try:
        return agent_lifecycle.publish_agent(
            db, agent_id, user_id=getattr(user, "id", None),
            note=(body.note if body else None),
        )
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.post("/agents/{agent_id}/unpublish")
def unpublish_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    from app.services import agent_lifecycle

    try:
        return agent_lifecycle.unpublish_agent(
            db, agent_id, user_id=getattr(user, "id", None))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.post("/agents/{agent_id}/rollback")
def rollback_agent(
    agent_id: str,
    body: RollbackBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    from app.services import agent_lifecycle

    try:
        return agent_lifecycle.rollback_agent(
            db, agent_id, body.to_version, user_id=getattr(user, "id", None))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.get("/agents/{agent_id}/revisions")
def list_revisions(
    agent_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    from app.services import agent_lifecycle

    return agent_lifecycle.list_revisions(db, agent_id)


@router.get("/agents-catalog")
def agents_catalog(
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Governed catalog (IBM ADLC Monitor): version, owner, risk posture,
    rollout, environments, red-team posture — one row per agent."""
    from app.models.agent_app import AgentApp
    from app.models.red_team_result import RedTeamResult

    agents = (
        db.query(AgentApp)
        .filter(AgentApp.is_deleted == False)  # noqa: E712
        .order_by(AgentApp.name)
        .all()
    )
    catalog = []
    for a in agents:
        last_rt = (
            db.query(RedTeamResult)
            .filter(RedTeamResult.agent_name == a.name)
            .order_by(RedTeamResult.created_date.desc())
            .first()
        )
        policy = a.policy_profile or {}
        catalog.append({
            "id": a.id,
            "name": a.name,
            "description": (a.description or "")[:200],
            "status": a.status or "draft",
            "version": a.version or 1,
            "owner_id": getattr(a, "created_by_id", None),
            "resource_type": a.resource_type,
            "risk_tier": policy.get("risk_tier"),
            "rollout_percent": a.rollout_percent or 100,
            "environments": ["production"] if a.status == "published" else [],
            "is_system": a.is_system,
            "red_team": {
                "last_run": last_rt.created_date.isoformat() if last_rt else None,
                "last_passed": last_rt.passed if last_rt else None,
                "probes_run": last_rt.total_probes if last_rt else None,
            } if last_rt else None,
        })
    return catalog
