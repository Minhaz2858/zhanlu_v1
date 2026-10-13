"""Use-case portfolio service — the AI-CoE board (IBM Gap 2).

IBM's AI Center-of-Excellence framework (Function 1 + 8): maintain a
portfolio of AI use cases with owners and KPIs, and measure business
impact. This service joins three things Zhanlu already had:

  - the agent catalog (what exists, versions, risk tier)
  - the portfolio registry (business owner, KPI, lifecycle stage)
  - the ROI layer (conversations, artifacts, cost, est. hours saved)

into ONE board per org: each use case = ownership + stage + measured
value. The stage comes from the registry; agents without an entry show
as "idea" — which is itself the CoE signal (governed but not yet
productized).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.agent_portfolio import LIFECYCLE_STAGES, AgentPortfolioEntry
from app.services.roi_service import roi_summary

logger = logging.getLogger(__name__)


def get_portfolio(
    db: Session,
    org_id: str,
    days: int = 30,
    app_id: str | None = None,
) -> dict:
    """The CoE portfolio board: every agent in the org with its business
    metadata (if any) and its measured value for the window."""
    from app.models.agent_app import AgentApp

    roi = roi_summary(db, org_id=org_id, days=days, app_id=app_id)
    roi_by_agent = {a["agent_name"]: a for a in roi["by_agent"]}

    entries = (
        db.query(AgentPortfolioEntry)
        .filter(
            AgentPortfolioEntry.org_id == org_id,
            AgentPortfolioEntry.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    entry_by_agent_id = {e.agent_id: e for e in entries}

    agents = (
        db.query(AgentApp)
        .filter(
            AgentApp.org_id == org_id,
            AgentApp.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    items = []
    seen_agent_ids = set()
    for a in agents:
        seen_agent_ids.add(a.id)
        entry = entry_by_agent_id.get(a.id)
        agent_name = a.name or (entry.agent_name if entry else None) or a.id
        roi_agent = roi_by_agent.get(agent_name, {})
        items.append({
            "agent_id": a.id,
            "agent_name": agent_name,
            "catalog_status": a.status or "draft",
            "business_owner": entry.business_owner if entry else None,
            "business_kpi": entry.business_kpi if entry else None,
            "lifecycle_stage": (entry.lifecycle_stage if entry else None) or "idea",
            "value_notes": entry.value_notes if entry else None,
            "roi": {
                "conversations": roi_agent.get("conversations", 0),
                "user_turns": roi_agent.get("user_turns", 0),
                "artifacts_created": roi_agent.get("artifacts_created", 0),
                "cost_total": roi_agent.get("cost_total", 0.0),
                "est_hours_saved": roi_agent.get("est_hours_saved", 0.0),
            },
        })

    # Portfolio entries pointing at agents that no longer exist (deleted
    # agent, other app) — still listed so ownership doesn't vanish silently.
    for e in entries:
        if e.agent_id not in seen_agent_ids:
            agent_name = e.agent_name or e.agent_id
            roi_agent = roi_by_agent.get(agent_name, {})
            items.append({
                "agent_id": e.agent_id,
                "agent_name": agent_name,
                "catalog_status": "removed",
                "business_owner": e.business_owner,
                "business_kpi": e.business_kpi,
                "lifecycle_stage": e.lifecycle_stage,
                "value_notes": e.value_notes,
                "roi": {
                    "conversations": roi_agent.get("conversations", 0),
                    "user_turns": roi_agent.get("user_turns", 0),
                    "artifacts_created": roi_agent.get("artifacts_created", 0),
                    "cost_total": roi_agent.get("cost_total", 0.0),
                    "est_hours_saved": roi_agent.get("est_hours_saved", 0.0),
                },
            })

    # CoE board order: production first, then by measured value.
    stage_rank = {s: i for i, s in enumerate(LIFECYCLE_STAGES)}
    items.sort(key=lambda i: (
        stage_rank.get(i["lifecycle_stage"], 99),
        -(i["roi"]["est_hours_saved"]),
    ))

    return {
        "org_id": org_id,
        "window_days": days,
        "stages": list(LIFECYCLE_STAGES),
        "items": items,
        "summary": {
            k: sum(1 for i in items if i["lifecycle_stage"] == k)
            for k in LIFECYCLE_STAGES
        },
    }


def upsert_entry(
    db: Session,
    org_id: str,
    agent_id: str,
    *,
    agent_name: str | None = None,
    business_owner: str | None = None,
    business_kpi: str | None = None,
    lifecycle_stage: str | None = None,
    value_notes: str | None = None,
) -> AgentPortfolioEntry:
    """Create or update the portfolio entry for one agent (org-scoped)."""
    if lifecycle_stage is not None and lifecycle_stage not in LIFECYCLE_STAGES:
        raise ValueError(
            f"lifecycle_stage must be one of {', '.join(LIFECYCLE_STAGES)}"
        )

    entry = (
        db.query(AgentPortfolioEntry)
        .filter(
            AgentPortfolioEntry.org_id == org_id,
            AgentPortfolioEntry.agent_id == agent_id,
            AgentPortfolioEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if entry is None:
        entry = AgentPortfolioEntry(org_id=org_id, agent_id=agent_id)
        db.add(entry)
    if agent_name is not None:
        entry.agent_name = agent_name
    if business_owner is not None:
        entry.business_owner = business_owner
    if business_kpi is not None:
        entry.business_kpi = business_kpi
    if lifecycle_stage is not None:
        entry.lifecycle_stage = lifecycle_stage
    if value_notes is not None:
        entry.value_notes = value_notes
    db.commit()
    db.refresh(entry)
    logger.info(
        "portfolio: entry upserted (org=%s agent=%s stage=%s)",
        org_id, agent_id, entry.lifecycle_stage,
    )
    return entry
