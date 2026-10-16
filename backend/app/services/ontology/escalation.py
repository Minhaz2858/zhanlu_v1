"""Escalation service (P3-6) — the structured human handoff.

Create: packages summary + context into an escalation record, audit-
logs it, and (when an ontology object is given) fans out through the
ontology links so the domain team's agents also see the handoff.
Resolve: a human closes the loop with a note — the audit trail keeps
both sides.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def create_escalation(
    db: Session,
    org_id: str,
    summary: str,
    *,
    context: dict | list | None = None,
    conversation_id: str | None = None,
    agent_name: str | None = None,
    object_name: str | None = None,
    sentiment: str | None = None,
    created_by: str | None = None,
) -> dict:
    import uuid as _uuid

    from app.models.audit_event import AuditEvent
    from app.models.escalation import Escalation

    esc = Escalation(
        id=str(_uuid.uuid4()), org_id=org_id,
        summary=summary,
        context_json=json.dumps(context, ensure_ascii=False, default=str)
        if context is not None else None,
        conversation_id=conversation_id, agent_name=agent_name,
        object_name=object_name, sentiment=sentiment,
        status="open", created_by_id=created_by,
    )
    db.add(esc)
    db.flush()

    try:
        db.add(AuditEvent(
            org_id=org_id,
            event_type="escalation_created",
            event_source="ontology",
            actor_type="agent" if agent_name else "user",
            actor_id=created_by,
            resource_type="escalation",
            resource_id=esc.id,
            outcome="success",
            detail_json={
                "summary": summary[:300], "sentiment": sentiment,
                "agent_name": agent_name, "object_name": object_name,
            },
            occurred_at=datetime.now(timezone.utc),
        ))
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning("escalation: audit write failed: %s", exc)

    db.commit()

    # optional: domain fan-out (same channel as sensors)
    fanout = None
    if object_name:
        try:
            from app.services.ontology.fanout import notify_object_change

            fanout = notify_object_change(
                db, org_id, object_name,
                f"[ESCALATED] {summary[:200]}",
                changed_by=agent_name,
            )
        except Exception as exc:  # noqa: BLE001 — handoff must not fail on fan-out
            logger.warning("escalation: fan-out failed (non-fatal): %s", exc)

    return {"id": esc.id, "status": "open", "fanout": fanout}


def list_escalations(
    db: Session, org_id: str, *, status: str | None = None, limit: int = 50,
) -> list[dict]:
    from app.models.escalation import Escalation

    q = db.query(Escalation).filter(
        Escalation.org_id == org_id,
        Escalation.is_deleted == False,  # noqa: E712
    )
    if status:
        q = q.filter(Escalation.status == status)
    rows = q.order_by(Escalation.created_date.desc()).limit(min(limit, 200)).all()
    out = []
    for e in rows:
        d = e.to_dict()
        try:
            d["context"] = json.loads(e.context_json) if e.context_json else None
        except (TypeError, ValueError):
            d["context"] = None
        d.pop("context_json", None)
        out.append(d)
    return out


def resolve_escalation(
    db: Session, org_id: str, escalation_id: str,
    *, resolved_by: str | None, note: str | None,
) -> dict:
    from app.models.audit_event import AuditEvent
    from app.models.escalation import Escalation

    esc = db.query(Escalation).filter(
        Escalation.id == escalation_id,
        Escalation.org_id == org_id,
        Escalation.is_deleted == False,  # noqa: E712
    ).first()
    if not esc:
        raise LookupError(f"escalation not found: {escalation_id}")
    esc.status = "resolved"
    esc.resolution_note = note
    esc.resolved_by = resolved_by
    try:
        db.add(AuditEvent(
            org_id=org_id,
            event_type="escalation_resolved",
            event_source="ontology",
            actor_type="user",
            actor_id=resolved_by,
            resource_type="escalation",
            resource_id=esc.id,
            outcome="success",
            detail_json={"note": (note or "")[:300]},
            occurred_at=datetime.now(timezone.utc),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("escalation: resolve audit failed: %s", exc)
    db.commit()
    return {"id": esc.id, "status": "resolved"}
