"""Cross-domain event fan-out on ontology links (P1-3, IBM gap G6).

IBM supply-chain prescription: agents act as a connected system — when a
business object changes (a Signal hits, a Supplier stalls), the agents
bound to LINKED objects' domains must hear about it through business
semantics, not ad-hoc plumbing.

Honesty rules:
- traversal is over REAL ontology links only — no implicit guesses
- notification is recorded as an AuditEvent per notified agent; when a
  swarm ``team_id`` is provided the message also goes to the team
  mailbox (best-effort — a missing team never fails the fan-out)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.ontology import AgentDomainBinding, OntologyLink
from app.services.ontology.queries import (
    get_object_by_name,
    list_links,
    list_objects,
)

logger = logging.getLogger(__name__)


def plan_fanout(db: Session, org_id: str, object_name: str) -> dict:
    """Compute which agents should hear about a change to ``object_name``:
    traverse ontology links (both directions) → neighbor objects → their
    domains → agent bindings. Pure read; never dispatches."""
    source = get_object_by_name(db, org_id, object_name)
    if not source:
        return {"success": False, "reason": f"object {object_name!r} not found"}

    objects = list_objects(db, org_id)
    by_id = {o.id: o for o in objects}
    neighbors: dict[str, list[dict]] = {}
    for link in list_links(db, org_id):
        other_id = None
        if link.from_object_id == source.id:
            other_id = link.to_object_id
        elif link.to_object_id == source.id:
            other_id = link.from_object_id
        if not other_id or other_id == source.id:
            continue
        other = by_id.get(other_id)
        if not other:
            continue
        neighbors.setdefault(other.domain_id, []).append({
            "object": other.name, "link_type": link.link_type,
        })

    if not neighbors:
        return {
            "success": True, "source_object": source.name,
            "links_traversed": 0, "domains": [], "agents": [],
            "note": "no ontology links from this object — nothing to fan out",
        }

    domain_ids = list(neighbors.keys())
    bindings = (
        db.query(AgentDomainBinding)
        .filter(
            AgentDomainBinding.org_id == org_id,
            AgentDomainBinding.domain_id.in_(domain_ids),
        )
        .all()
    )
    agents = sorted({b.agent_name for b in bindings})
    return {
        "success": True,
        "source_object": source.name,
        "links_traversed": sum(len(v) for v in neighbors.values()),
        "domains": [
            {"domain_id": d, "linked_objects": objs}
            for d, objs in neighbors.items()
        ],
        "agents": agents,
    }


def notify_object_change(
    db: Session,
    org_id: str,
    object_name: str,
    change_summary: str,
    changed_by: str | None = None,
    team_id: str | None = None,
) -> dict:
    """Fan out a business-object change to agents bound to linked domains.

    Every notification is audit-logged (who/what/whom). With ``team_id``
    the message is also delivered to the swarm team mailbox (best effort).
    """
    from app.models.audit_event import AuditEvent

    plan = plan_fanout(db, org_id, object_name)
    if not plan.get("success"):
        return plan

    notified: list[str] = []
    for agent_name in plan.get("agents", []):
        detail = {
            "source_object": object_name,
            "change_summary": change_summary,
            "notified_agent": agent_name,
            "changed_by": changed_by,
        }
        try:
            db.add(AuditEvent(
                org_id=org_id,
                event_type="ontology_fanout",
                event_source="ontology",
                actor_type="agent" if changed_by else "system",
                actor_id=changed_by,
                resource_type="ontology_object",
                resource_id=plan.get("source_object"),
                outcome="success",
                detail_json=detail,
                occurred_at=datetime.now(timezone.utc),
            ))
            notified.append(agent_name)
        except Exception as exc:  # noqa: BLE001 — one bad row can't stop fan-out
            logger.warning("fanout: audit write failed for %s: %s", agent_name, exc)

    if notified:
        db.commit()

    # Best-effort swarm delivery: only when the caller names a team.
    swarm_delivered = 0
    if team_id and notified:
        try:
            from app.services.swarm.team_registry import get_team_registry

            registry_ = get_team_registry()
            for agent_name in notified:
                ok = registry_.send_message(
                    team_id=team_id,
                    sender="ontology",
                    recipient=agent_name,
                    content=(
                        f"[ontology] {object_name} changed: {change_summary}. "
                        "Review linked objects in your domain and act within "
                        "your guardrails (writes still require approval)."
                    ),
                    summary=f"{object_name} changed",
                )
                if ok:
                    swarm_delivered += 1
        except Exception as exc:  # noqa: BLE001 — fan-out is advisory
            logger.warning("fanout: swarm delivery failed: %s", exc)

    return {
        **plan,
        "notified": notified,
        "audit_logged": len(notified),
        "swarm_delivered": swarm_delivered,
    }
