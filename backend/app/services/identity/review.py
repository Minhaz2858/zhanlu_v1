"""T5 — identity review queue services.

The review queue is the human gate for ALL tiers: system matching lands
every candidate as `inferred` (tiers 1-2 with high confidence, tiers 3-4
requiring review) and only an org-admin confirm makes a crosswalk ACTIVE
(`confirmed`). Every decision is audited (`identity_review`) and
append-only recorded in identity_decision_events. Rejections are
remembered by the resolver's `_record_match` (never re-proposed).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.identity import IdentityDecisionEvent, IdentityMatch

_VALID_DECISIONS = ("confirm", "reject")

# Allowed status transitions. Same-status re-review is a no-op error;
# confirmed→rejected and rejected→confirmed are REVERSALS (recorded).
_ALLOWED_TRANSITIONS = {
    ("inferred", "confirmed"),
    ("inferred", "rejected"),
    ("confirmed", "rejected"),   # reversal
    ("rejected", "confirmed"),   # reversal
}


def append_decision_event(db: Session, org_id: str, match: IdentityMatch,
                          *, event_kind: str, previous_status: str | None,
                          new_status: str,
                          reviewed_by: str | None = None) -> IdentityDecisionEvent:
    """Append ONE decision-history row. Never updates or deletes —
    history is reconstructed by ordering on (event_seq, created_date)."""
    last = (
        db.query(IdentityDecisionEvent.event_seq)
        .filter(IdentityDecisionEvent.org_id == org_id,
                IdentityDecisionEvent.match_id == match.id)
        .order_by(IdentityDecisionEvent.event_seq.desc())
        .first()
    )
    ev = IdentityDecisionEvent(
        org_id=org_id, match_id=match.id, object_id=match.object_id,
        event_kind=event_kind,
        event_seq=(last[0] + 1) if last else 1,
        previous_status=previous_status, new_status=new_status,
        reviewed_by=reviewed_by, match_tier=match.match_tier,
        confidence=match.confidence, evidence_json=match.evidence_json,
        source_a_key=match.source_a_key, source_b_key=match.source_b_key,
        pk_a=match.pk_a, pk_b=match.pk_b,
    )
    db.add(ev)
    return ev


def _to_dict(m: IdentityMatch) -> dict:
    return {
        "id": m.id,
        "org_id": m.org_id,
        "object_id": m.object_id,
        "source_a": m.source_a,
        "source_b": m.source_b,
        "pk_a": m.pk_a,
        "pk_b": m.pk_b,
        "match_tier": m.match_tier,
        "status": m.status,
        "confidence": m.confidence,
        "normalization_rule_json": m.normalization_rule_json,
        "evidence_json": m.evidence_json,
        "reviewed_by": m.reviewed_by,
        "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
        "created_at": m.created_date.isoformat() if m.created_date else None,
    }


def list_matches(db: Session, org_id: str, *,
                 object_id: str | None = None,
                 status: str | None = None) -> list[dict]:
    """Org-scoped review queue, newest first."""
    q = db.query(IdentityMatch).filter(IdentityMatch.org_id == org_id)
    if object_id:
        q = q.filter(IdentityMatch.object_id == object_id)
    if status:
        q = q.filter(IdentityMatch.status == status)
    rows = q.order_by(IdentityMatch.created_date.desc()).all()
    return [_to_dict(m) for m in rows]


def review_match(db: Session, org_id: str, match_id: str, decision: str,
                 *, reviewed_by: str | None = None) -> dict:
    """Org-admin confirm/reject on ONE crosswalk. Audited. Cross-org
    access raises LookupError (a leak would be a 404, not a feature)."""
    if decision not in _VALID_DECISIONS:
        raise ValueError("decision must be confirm or reject")

    m = (
        db.query(IdentityMatch)
        .filter(IdentityMatch.id == match_id,
                IdentityMatch.org_id == org_id)
        .first()
    )
    if m is None:
        raise LookupError("match not found")

    new_status = "confirmed" if decision == "confirm" else "rejected"
    if (m.status, new_status) not in _ALLOWED_TRANSITIONS:
        raise ValueError(
            f"match is already {m.status!r} — {decision} is a no-op"
        )
    previous_status = m.status

    m.status = new_status
    m.reviewed_by = reviewed_by
    from datetime import datetime, timezone

    m.reviewed_at = datetime.now(timezone.utc)
    append_decision_event(
        db, org_id, m, event_kind=decision,
        previous_status=previous_status, new_status=new_status,
        reviewed_by=reviewed_by,
    )
    db.flush()

    from datetime import datetime, timezone

    from app.models.audit_event import AuditEvent

    db.add(AuditEvent(
        org_id=org_id,
        event_type="identity_review",
        event_source="ontology",
        actor_type="user",
        actor_id=reviewed_by,
        resource_type="identity_match",
        resource_id=m.id,
        outcome="success",
        occurred_at=datetime.now(timezone.utc),
        detail_json={
            "decision": decision,
            "match_id": m.id,
            "object_id": m.object_id,
            "match_tier": m.match_tier,
            "pk_a": m.pk_a,
            "pk_b": m.pk_b,
            "source_a_key": m.source_a_key,
            "source_b_key": m.source_b_key,
        },
    ))
    db.commit()
    return _to_dict(m)


def decision_history(db: Session, org_id: str, match_id: str) -> list[dict]:
    """Reconstructable append-only history for ONE match, org-scoped.
    Cross-org access raises LookupError (a leak would be a 404)."""
    m = (
        db.query(IdentityMatch)
        .filter(IdentityMatch.id == match_id,
                IdentityMatch.org_id == org_id)
        .first()
    )
    if m is None:
        raise LookupError("match not found")
    events = (
        db.query(IdentityDecisionEvent)
        .filter(IdentityDecisionEvent.org_id == org_id,
                IdentityDecisionEvent.match_id == match_id)
        .order_by(IdentityDecisionEvent.event_seq.asc(),
                  IdentityDecisionEvent.created_date.asc())
        .all()
    )
    return [{
        "event_kind": e.event_kind,
        "event_seq": e.event_seq,
        "previous_status": e.previous_status,
        "new_status": e.new_status,
        "reviewed_by": e.reviewed_by,
        "match_tier": e.match_tier,
        "confidence": e.confidence,
        "evidence": e.evidence_json,
        "source_a_key": e.source_a_key,
        "source_b_key": e.source_b_key,
        "pk_a": e.pk_a,
        "pk_b": e.pk_b,
        "timestamp": e.created_date.isoformat() if e.created_date else None,
    } for e in events]


def crosswalk_summary(db: Session, org_id: str, object_id: str,
                      *, sample_cap: int = 3) -> dict:
    """Honest summary of the CONFIRMED crosswalks for an object — what
    the ontology runtime may surface on grounded reads. Inferred and
    rejected matches are NOT active and never appear. No per-row joins:
    provenance only (which sources, how many pairs, a small sample)."""
    rows = (
        db.query(IdentityMatch)
        .filter(IdentityMatch.org_id == org_id,
                IdentityMatch.object_id == object_id,
                IdentityMatch.status == "confirmed",
                IdentityMatch.is_deleted == False)  # noqa: E712
        .all()
    )
    grouped: dict[tuple[str, str], list[IdentityMatch]] = {}
    for m in rows:
        grouped.setdefault((m.source_a_key, m.source_b_key), []).append(m)

    sources = []
    for (key_a, key_b), ms in grouped.items():
        first = ms[0]
        sources.append({
            "source_a": first.source_a,
            "source_b": first.source_b,
            "source_a_key": key_a,
            "source_b_key": key_b,
            "confirmed_pairs": len(ms),
            "sample": [{"pk_a": m.pk_a, "pk_b": m.pk_b}
                       for m in ms[:sample_cap]],
        })
    return {"object_id": object_id, "sources": sources}
