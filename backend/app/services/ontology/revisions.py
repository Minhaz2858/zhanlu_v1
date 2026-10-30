"""Versioned ontology registry (PRD P2, gap G4).

Contract:
- every ontology mutation produces exactly ONE meaningful revision,
  transactionally atomic with the mutation (same session/commit)
- idempotent re-runs produce ZERO revisions (no-change detection: the new
  snapshot must differ from the latest revision's after_json)
- history is append-only and immutable; rollback restores a snapshot and
  appends a NEW `rollback` revision — never erases
- concurrent mutations of one entity cannot duplicate or gap revision_no:
  the entity row is locked FOR UPDATE (no-op on SQLite) before allocation,
  and the insert runs in a SAVEPOINT with a bounded retry
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ontology import (
    OntologyAction,
    OntologyDomain,
    OntologyLink,
    OntologyObject,
)
from app.models.ontology_revision import OntologyRevision

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

# Mutable-state snapshot fields per entity type (evidence fields INCLUDED —
# the history must be able to reconstruct any prior evidence state).
_SNAPSHOT_FIELDS = {
    "object": ("domain_id", "name", "display_name", "description",
               "backing_kind", "backing_ref", "properties_json", "is_active",
               "is_deleted", "evidence_status", "confidence",
               "provenance_json"),
    "link": ("from_object_id", "to_object_id", "link_type", "cardinality",
             "description", "is_deleted", "evidence_status", "confidence",
             "provenance_json"),
    "action": ("object_id", "name", "description", "tool_name", "risk_level",
               "requires_confirmation", "params_json", "is_active",
               "is_deleted", "evidence_status", "confidence",
               "provenance_json"),
    "domain": ("code", "name", "description", "sort_order", "is_deleted"),
}

_ENTITY_MODEL = {
    "object": OntologyObject,
    "link": OntologyLink,
    "action": OntologyAction,
    "domain": OntologyDomain,
}


def snapshot(entity_type: str, entity) -> dict:
    return {f: getattr(entity, f) for f in _SNAPSHOT_FIELDS[entity_type]}


def _latest_after(db: Session, entity_type: str, entity_id: str) -> dict | None:
    row = (
        db.query(OntologyRevision)
        .filter(
            OntologyRevision.entity_type == entity_type,
            OntologyRevision.entity_id == entity_id,
        )
        .order_by(OntologyRevision.revision_no.desc())
        .first()
    )
    return row.after_json if row else None


def record_revision(
    db: Session, entity_type: str, entity, change_kind: str | None = None,
    changed_by: str | None = None, summary: str | None = None,
):
    """Record ONE revision for `entity`, atomic with the caller's
    transaction. Returns the OntologyRevision, or None when the snapshot
    is unchanged (idempotent re-run → zero revisions)."""
    after = snapshot(entity_type, entity)
    before = _latest_after(db, entity_type, entity.id)
    if before is not None and before == after:
        return None  # idempotent: nothing meaningfully changed

    org_id = getattr(entity, "org_id", None)
    kind = change_kind or ("create" if before is None else "update")

    # Serialize revision_no allocation per entity (row lock; no-op on SQLite).
    model = _ENTITY_MODEL[entity_type]
    db.execute(
        select(model.id).where(model.id == entity.id).with_for_update()
    )

    last_attempt = None
    for attempt in range(_MAX_RETRIES):
        current_max = (
            db.query(func.max(OntologyRevision.revision_no))
            .filter(
                OntologyRevision.entity_type == entity_type,
                OntologyRevision.entity_id == entity.id,
            )
            .scalar()
            or 0
        )
        rev = OntologyRevision(
            org_id=org_id, entity_type=entity_type, entity_id=entity.id,
            revision_no=current_max + 1, change_kind=kind,
            before_json=before, after_json=after,
            changed_by=changed_by, change_summary=summary,
        )
        try:
            nested = db.begin_nested()
            db.add(rev)
            db.flush()
            nested.commit()  # release savepoint; still atomic with outer txn
            return rev
        except Exception as exc:  # noqa: BLE001 — concurrent race → retry
            last_attempt = exc
            logger.warning(
                "revision_no race (%s/%s) attempt %d: %s",
                entity_type, entity.id[:8], attempt + 1, exc,
            )
    raise last_attempt


def list_revisions(db: Session, org_id: str, entity_type: str, entity_id: str):
    return (
        db.query(OntologyRevision)
        .filter(
            OntologyRevision.org_id == org_id,
            OntologyRevision.entity_type == entity_type,
            OntologyRevision.entity_id == entity_id,
        )
        .order_by(OntologyRevision.revision_no.desc())
        .all()
    )


def revisions_since(db: Session, org_id: str, since_revision_id: str | None,
                    entity_type: str | None = None, limit: int = 200):
    q = db.query(OntologyRevision).filter(OntologyRevision.org_id == org_id)
    if entity_type:
        q = q.filter(OntologyRevision.entity_type == entity_type)
    if since_revision_id:
        anchor = db.query(OntologyRevision).filter(
            OntologyRevision.id == since_revision_id,
            OntologyRevision.org_id == org_id,
        ).first()
        if anchor is not None:
            q = q.filter(OntologyRevision.created_date > anchor.created_date)
    return q.order_by(OntologyRevision.created_date.desc()).limit(limit).all()


_RESTORE_FIELDS = _SNAPSHOT_FIELDS  # rollback restores the full snapshot


def rollback_to_revision(db: Session, entity_type: str, entity,
                         revision_no: int, changed_by: str | None = None):
    """Restore the entity to `revision_no`'s BEFORE state and append a NEW
    `rollback` revision. History is never erased or rewritten."""
    target = (
        db.query(OntologyRevision)
        .filter(
            OntologyRevision.org_id == entity.org_id,
            OntologyRevision.entity_type == entity_type,
            OntologyRevision.entity_id == entity.id,
            OntologyRevision.revision_no == revision_no,
        )
        .first()
    )
    if target is None:
        raise ValueError(
            f"revision {revision_no} not found for {entity_type} {entity.id}"
        )
    restore = dict(target.after_json or {})
    if not restore:
        raise ValueError(
            f"revision {revision_no} has no restorable state"
        )
    for f in _RESTORE_FIELDS[entity_type]:
        if f in restore:
            setattr(entity, f, restore[f])
    return record_revision(
        db, entity_type, entity, change_kind="rollback",
        changed_by=changed_by,
        summary=f"rollback to revision {revision_no}",
    )


def revision_to_dict(r: OntologyRevision) -> dict:
    return {
        "id": r.id, "entity_type": r.entity_type, "entity_id": r.entity_id,
        "revision_no": r.revision_no, "change_kind": r.change_kind,
        "before": r.before_json, "after": r.after_json,
        "changed_by": r.changed_by, "summary": r.change_summary,
        "created_date": r.created_date.isoformat() if r.created_date else None,
    }
