"""T5 — identity review queue services.

The review queue is the human gate for tiers 3-4: `inferred` matches
wait here until an org-admin confirms or rejects them. Every decision
is audited (`identity_review`). Rejections are remembered by the
resolver's `_record_match` (never re-proposed); confirmations activate
the crosswalk for the ontology runtime.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.identity import IdentityMatch

_VALID_DECISIONS = ("confirm", "reject")


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

    m.status = "confirmed" if decision == "confirm" else "rejected"
    m.reviewed_by = reviewed_by
    from datetime import datetime, timezone

    m.reviewed_at = datetime.now(timezone.utc)
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
