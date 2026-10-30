"""P4 T7 — human review of document-derived ontology proposals.

The approval is an ATOMIC unit (decision record D4): create-or-attach
element + document provenance entry + stamp_human_document_approval (D2) +
record_revision (P2) + document decision event (IdentityDecisionEvent
pattern) + AuditEvent + proposal status update — one transaction, ONE
commit at the end. Any failure rolls back EVERY mutation; a partially
applied approval is impossible.

Refusals (explicit reasons, the proposal stays in review):
- audience-unresolvable proposals are NOT promotable (D3);
- a `verified` existing definition is NEVER overwritten — conflicting
  proposals stay in review requiring explicit human conflict resolution;
- creating a new object requires a resolvable org domain.

Rejections are remembered (the extraction dedup checks ALL statuses, so a
rejected quote is never re-proposed). Revisions and decision events are
append-only; superseded proposals are history, never erased.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.knowledge_base import DocumentVersion
from app.models.ontology import (
    DocumentDecisionEvent,
    DocumentProposal,
    OntologyDomain,
    OntologyObject,
)
from app.services.ontology.evidence import stamp_human_document_approval
from app.services.ontology.revisions import record_revision

_VALID_DECISIONS = ("approve", "reject")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def append_decision_event(db: Session, org_id: str, proposal: DocumentProposal,
                          *, event_kind: str, previous_status: str | None,
                          new_status: str, reviewed_by: str | None = None,
                          detail: dict | None = None) -> DocumentDecisionEvent:
    """Append ONE decision-history row. Never updates or deletes — history
    is reconstructed by ordering on (event_seq, created_date)."""
    last = (
        db.query(DocumentDecisionEvent.event_seq)
        .filter(DocumentDecisionEvent.org_id == org_id,
                DocumentDecisionEvent.proposal_id == proposal.id)
        .order_by(DocumentDecisionEvent.event_seq.desc())
        .first()
    )
    ev = DocumentDecisionEvent(
        org_id=org_id, proposal_id=proposal.id,
        object_id=proposal.object_id, event_kind=event_kind,
        event_seq=(last[0] + 1) if last else 1,
        previous_status=previous_status, new_status=new_status,
        reviewed_by=reviewed_by, payload_json=proposal.payload_json,
        detail_json=detail,
    )
    db.add(ev)
    return ev


def _to_dict(p: DocumentProposal) -> dict:
    return {
        "id": p.id,
        "org_id": p.org_id,
        "kb_id": p.kb_id,
        "doc_version_id": p.doc_version_id,
        "source_kb_id": p.source_kb_id,
        "chunk_key": p.chunk_key,
        "proposal_kind": p.proposal_kind,
        "payload_json": p.payload_json,
        "quote": p.quote,
        "location_json": p.location_json,
        "method": p.method,
        "model_version": p.model_version,
        "audience_json": p.audience_json,
        "status": p.status,
        "object_id": p.object_id,
        "conflict_json": p.conflict_json,
        "reviewed_by": p.reviewed_by,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "created_date": p.created_date.isoformat() if p.created_date else None,
    }


def _get_proposal(db: Session, org_id: str, proposal_id: str) -> DocumentProposal:
    """Org-scoped fetch — cross-org access is a 404-style failure. The row
    is locked FOR UPDATE (no-op on SQLite) so two concurrent reviews cannot
    interleave the event_seq allocation."""
    p = (
        db.query(DocumentProposal)
        .filter(DocumentProposal.id == proposal_id,
                DocumentProposal.org_id == org_id)
        .with_for_update()
        .first()
    )
    if p is None:
        raise LookupError("proposal not found")
    return p


def _check_reviewable(p: DocumentProposal) -> str:
    """Only a `proposed` proposal can be reviewed (approve/reject/revise)."""
    if p.status != "proposed":
        raise ValueError(
            f"proposal is {p.status!r} — only a `proposed` proposal can "
            f"be reviewed"
        )
    return p.status


def _audience_resolvable(p: DocumentProposal) -> bool:
    """D3: a proposal is promotable only when its captured audience
    snapshot resolved to the proposal's OWN org. Extraction stores
    {"org_id": ...} for resolvable documents; anything else — including a
    snapshot pointing at a different org — is unresolvable."""
    aud = p.audience_json or {}
    return (bool((aud.get("org_id") or "").strip())
            and aud.get("org_id") == p.org_id)


def _content_hash(db: Session, p: DocumentProposal) -> str | None:
    row = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.id == p.doc_version_id,
                DocumentVersion.org_id == p.org_id)
        .first()
    )
    return row.content_hash if row else None


def _resolve_target(db: Session, p: DocumentProposal, *, domain_id: str | None,
                    reviewer_id: str | None) -> tuple[OntologyObject, str]:
    """Create-or-attach: a conflict with an existing object attaches (the
    admin's approve IS the explicit human conflict resolution — extraction
    never auto-merges). The DEFINITION of a `verified` object is never
    overwritten (description untouched); its evidence entries still append
    — per-entry provenance (D7) is exactly how two documents with
    different audiences attach to one object independently. Returns
    (object, change_kind)."""
    conflict = p.conflict_json or {}
    existing_id = conflict.get("existing_object_id")
    if existing_id:
        existing = (
            db.query(OntologyObject)
            .filter(OntologyObject.id == existing_id,
                    OntologyObject.org_id == p.org_id,
                    OntologyObject.is_deleted == False)  # noqa: E712
            .first()
        )
        if existing is None:
            raise ValueError(
                "conflict references a missing object — re-run extraction"
            )
        if existing.evidence_status == "verified":
            # Corroborating document: append entry only — definition stays.
            return existing, "attach_verified"
        if p.payload_json and (p.payload_json.get("description") or "").strip():
            existing.description = p.payload_json["description"]
        return existing, "update"

    # Create path: a new object needs a resolvable org domain.
    payload = p.payload_json or {}
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("proposal payload has no name — revise it first")
    dom_id = domain_id or payload.get("domain_id")
    if not dom_id:
        raise ValueError(
            "creating a new ontology object requires a domain — pass "
            "domain_id or revise the payload with domain_id"
        )
    domain = (
        db.query(OntologyDomain)
        .filter(OntologyDomain.id == dom_id, OntologyDomain.org_id == p.org_id)
        .first()
    )
    if domain is None:
        raise ValueError(
            f"domain {dom_id!r} not found in org — refusal, never guessed"
        )
    obj = OntologyObject(
        org_id=p.org_id, domain_id=domain.id, name=name,
        display_name=payload.get("display_name") or name,
        description=payload.get("description"),
        backing_kind="manual",  # document knowledge, not a live data binding
        is_active=True, evidence_status="proposed",
    )
    # Explicit refusal for a name that already exists in the org (the
    # unique constraint would otherwise surface as a raw IntegrityError).
    clash = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == p.org_id,
                OntologyObject.name == name,
                OntologyObject.is_deleted == False)  # noqa: E712
        .first()
    )
    if clash is not None:
        raise ValueError(
            f"an object named {name!r} already exists (id {clash.id}) — "
            f"re-run extraction to record the conflict for review"
        )
    db.add(obj)
    db.flush()  # need the id for revision + provenance
    return obj, "create"


def _write_audit(db: Session, org_id: str, p: DocumentProposal, *,
                 decision: str, reviewer_id: str | None,
                 detail: dict) -> None:
    db.add(AuditEvent(
        org_id=org_id,
        event_type="ontology_document_review",
        event_source="ontology",
        actor_type="user",
        actor_id=reviewer_id,
        resource_type="ontology_document_proposal",
        resource_id=p.id,
        outcome="success",
        occurred_at=_now(),
        detail_json=detail,
    ))


def review_proposal(db: Session, org_id: str, proposal_id: str, decision: str,
                    *, reviewed_by: str | None = None,
                    domain_id: str | None = None,
                    payload_override: dict | None = None) -> dict:
    """Org-admin approve/reject on ONE document proposal — the ATOMIC unit.
    Any failure rolls back every mutation (scenarios 6/7)."""
    if decision not in _VALID_DECISIONS:
        raise ValueError("decision must be approve or reject")
    if not (reviewed_by or "").strip():
        # Audit-trail integrity: EVERY recorded decision carries a human
        # actor — the API layer (T8) passes the authenticated principal,
        # never a request body.
        raise ValueError(
            "document review requires an authenticated human reviewer"
        )

    p = _get_proposal(db, org_id, proposal_id)
    previous_status = _check_reviewable(p)

    if payload_override is not None:
        p.payload_json = dict(payload_override)

    try:
        if decision == "reject":
            p.status = "rejected"
            p.reviewed_by = reviewed_by
            p.reviewed_at = _now()
            append_decision_event(
                db, org_id, p, event_kind="reject",
                previous_status=previous_status, new_status="rejected",
                reviewed_by=reviewed_by,
            )
            _write_audit(
                db, org_id, p, decision=decision, reviewer_id=reviewed_by,
                detail={"decision": decision, "proposal_id": p.id,
                        "kb_id": p.kb_id, "doc_version_id": p.doc_version_id},
            )
            db.commit()
            return _to_dict(p)

        # ── approve ──
        if not _audience_resolvable(p):
            raise ValueError(
                "proposal is not promotable: the source document's audience "
                "could not be resolved (D3) — extraction must re-capture it"
            )

        target, change_kind = _resolve_target(db, p, domain_id=domain_id,
                                              reviewer_id=reviewed_by)

        # D2 stamp (the only document-approval entry point to `verified`).
        stamp_human_document_approval(
            target, reviewer_id=reviewed_by or "", proposal=p,
            content_hash=_content_hash(db, p), decision="approved",
        )

        record_revision(
            db, "object", target, change_kind=change_kind,
            changed_by=reviewed_by,
            summary=f"document-grounded {change_kind} from proposal "
                    f"{p.id[:8]} (doc {p.doc_version_id[:8]})",
        )
        p.status = "approved"
        p.object_id = target.id
        p.reviewed_by = reviewed_by
        p.reviewed_at = _now()
        append_decision_event(
            db, org_id, p, event_kind="approve",
            previous_status=previous_status, new_status="approved",
            reviewed_by=reviewed_by,
            detail={"content_hash": _content_hash(db, p),
                    "location": p.location_json, "quote": p.quote},
        )
        _write_audit(
            db, org_id, p, decision=decision, reviewer_id=reviewed_by,
            detail={"decision": decision, "proposal_id": p.id,
                    "object_id": target.id, "kb_id": p.kb_id,
                    "doc_version_id": p.doc_version_id,
                    "change_kind": change_kind},
        )
        db.commit()  # the ONE commit — everything above is one transaction
    except Exception:
        db.rollback()  # scenario 6/7: a partial approval is impossible
        raise
    return _to_dict(p)


def revise_proposal(db: Session, org_id: str, proposal_id: str,
                    payload: dict, *, reviewed_by: str | None = None) -> dict:
    """Admin edits the proposal payload while it is still `proposed`; the
    subsequent approve uses the revised payload. Revise never promotes."""
    if not (reviewed_by or "").strip():
        raise ValueError(
            "document review requires an authenticated human reviewer"
        )
    p = _get_proposal(db, org_id, proposal_id)
    _check_reviewable(p)
    if not isinstance(payload, dict) or not (payload.get("name") or "").strip():
        raise ValueError("revised payload must carry a name")
    try:
        p.payload_json = dict(payload)
        append_decision_event(
            db, org_id, p, event_kind="revise", previous_status="proposed",
            new_status="proposed", reviewed_by=reviewed_by,
            detail={"revised_payload": payload},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _to_dict(p)


def decision_history(db: Session, org_id: str, proposal_id: str) -> list[dict]:
    """Append-only decision history for ONE proposal, org-scoped. Cross-org
    access raises LookupError (a leak would be a 404, not a feature)."""
    _get_proposal(db, org_id, proposal_id)
    events = (
        db.query(DocumentDecisionEvent)
        .filter(DocumentDecisionEvent.org_id == org_id,
                DocumentDecisionEvent.proposal_id == proposal_id)
        .order_by(DocumentDecisionEvent.event_seq.asc(),
                  DocumentDecisionEvent.created_date.asc())
        .all()
    )
    return [{
        "event_kind": e.event_kind,
        "event_seq": e.event_seq,
        "previous_status": e.previous_status,
        "new_status": e.new_status,
        "reviewed_by": e.reviewed_by,
        "payload": e.payload_json,
        "detail": e.detail_json,
        "timestamp": e.created_date.isoformat() if e.created_date else None,
    } for e in events]
