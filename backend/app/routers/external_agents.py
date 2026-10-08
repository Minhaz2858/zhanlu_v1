"""External-agent registry CRUD — remote A2A agents Zhanlu can delegate to.

IBM agent-protocols workstream #1 (2026-10-08). Companion to the
``invoke_external_agent`` tool: the UI/API manages the directory here; the
tool reads it at delegation time.

Surface (mounted under ``/api``):
  GET    /external-agents            — list
  POST   /external-agents            — create
  PATCH  /external-agents/{id}       — update
  DELETE /external-agents/{id}       — soft delete
  POST   /external-agents/{id}/probe — fetch the remote agent card (discovery)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_current_user_required, get_db
from app.models.external_agent import ExternalAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/external-agents", tags=["external-agents"])


class ExternalAgentCreate(BaseModel):
    name: str
    endpoint_url: str
    description: str | None = None
    card_url: str | None = None
    auth_token: str | None = None
    capabilities: list[str] | None = None
    timeout_s: int | None = 60
    enabled: bool = True
    granted_agents: list[str] | None = None


class ExternalAgentUpdate(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    description: str | None = None
    card_url: str | None = None
    auth_token: str | None = None
    capabilities: list[str] | None = None
    timeout_s: int | None = None
    enabled: bool | None = None
    granted_agents: list[str] | None = None


@router.get("")
def list_external_agents(
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    rows = (
        db.query(ExternalAgent)
        .filter(ExternalAgent.is_deleted == False)  # noqa: E712
        .order_by(ExternalAgent.name)
        .all()
    )
    return [r.to_dict() for r in rows]


@router.post("")
def create_external_agent(
    body: ExternalAgentCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    if not body.endpoint_url.startswith(("http://", "https://")):
        raise HTTPException(400, "endpoint_url must be an http(s) URL")
    from app.services.crypto import encrypt_value

    row = ExternalAgent(
        name=body.name.strip(),
        description=body.description,
        endpoint_url=body.endpoint_url.strip(),
        card_url=body.card_url,
        # IBM framework-audit #3: credentials encrypted at rest
        auth_token=encrypt_value(body.auth_token),
        capabilities=body.capabilities or [],
        timeout_s=body.timeout_s or 60,
        enabled=body.enabled,
        granted_agents=body.granted_agents,
    )
    db.add(row)
    db.commit()
    return row.to_dict()


@router.patch("/{agent_id}")
def update_external_agent(
    agent_id: str,
    body: ExternalAgentUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    row = (
        db.query(ExternalAgent)
        .filter(
            ExternalAgent.id == agent_id,
            ExternalAgent.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(404, "External agent not found")
    from app.services.crypto import encrypt_value

    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "auth_token" and value:
            value = encrypt_value(value)  # credentials encrypted at rest
        setattr(row, field, value)
    db.commit()
    return row.to_dict()


@router.delete("/{agent_id}")
def delete_external_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    row = (
        db.query(ExternalAgent)
        .filter(
            ExternalAgent.id == agent_id,
            ExternalAgent.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(404, "External agent not found")
    row.is_deleted = True
    db.commit()
    return {"ok": True}


@router.post("/{agent_id}/probe")
async def probe_external_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Fetch the remote agent's discovery card (A2A step 1) and store
    its capabilities. Non-destructive: a failed probe only sets status."""
    row = (
        db.query(ExternalAgent)
        .filter(
            ExternalAgent.id == agent_id,
            ExternalAgent.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(404, "External agent not found")

    base = row.endpoint_url.rstrip("/")
    card_candidates = [
        row.card_url,
        # conventional well-known paths relative to the endpoint origin
        base.split("/a2a")[0] + "/.well-known/agent.json" if "/a2a" in base else None,
    ]
    card: dict[str, Any] | None = None
    last_err: str | None = None
    headers = (
        {"Authorization": f"Bearer {row.auth_token}"} if row.auth_token else {}
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in card_candidates:
            if not url:
                continue
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and ("name" in data or "skills" in data
                                               or "capabilities" in data):
                    card = data
                    break
            except Exception as e:  # noqa: BLE001 — probing is best-effort
                last_err = str(e)

    if card is not None:
        skills = card.get("skills") or card.get("capabilities") or []
        if isinstance(skills, list):
            row.capabilities = [
                s.get("name") if isinstance(s, dict) else str(s) for s in skills
            ][:20]
        row.description = row.description or card.get("description")
        row.status = "ok"
    else:
        row.status = "unreachable"
    row.last_probed_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "status": row.status,
        "card": card,
        "error": last_err if card is None else None,
        "capabilities": row.capabilities or [],
    }
