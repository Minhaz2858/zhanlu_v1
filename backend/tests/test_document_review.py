"""P4 T7 — human review of document-derived proposals.

The approval is an ATOMIC unit (plan Part B / decision record D4):
create-or-attach element + document provenance entry (D7 fields) +
stamp_human_document_approval (D2 — the only document-approval entry
point, refuses machine callers) + record_revision (P2) + decision event
(IdentityDecisionEvent pattern) + AuditEvent + proposal status update.
Any failure rolls back EVERY mutation — a partially approved proposal is
impossible. `verified` definitions are NEVER overwritten.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import DocumentVersion, KnowledgeBase
from app.models.ontology import DocumentProposal, OntologyDomain, OntologyObject
import app.models.ontology_revision  # noqa: F401 — register in metadata

ORG = "org-r"
OTHER = "org-s"
REVIEWER = "user-admin-1"

QUOTE = "Customer means any entity that buys goods from the company."


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture()
def domain(db):
    d = OntologyDomain(org_id=ORG, code="customer", name="Customer",
                       sort_order=10)
    db.add(d)
    db.commit()
    return d


@pytest.fixture()
def proposal(db, domain):
    """One `proposed` definition proposal from a document version."""
    kb = KnowledgeBase(org_id=ORG, name="handbook", source_kind="file",
                       file_type="txt", file_url="/api/uploads/handbook.txt")
    db.add(kb)
    db.flush()
    ver = DocumentVersion(
        org_id=ORG, kb_id=kb.id, content_hash="a" * 64,
        file_url_at_ingest=kb.file_url,
        first_indexed_at=datetime.now(timezone.utc),
    )
    db.add(ver)
    db.flush()
    kb.content_hash = "a" * 64
    kb.content_version_id = ver.id
    p = DocumentProposal(
        org_id=ORG,
        kb_id=kb.id,
        doc_version_id=ver.id,
        source_kb_id=kb.id,
        chunk_key=f"{kb.id}_{ver.id[:8]}_0",
        proposal_kind="definition",
        payload_json={"name": "Customer",
                      "description": "An entity that buys goods."},
        quote=QUOTE,
        location_json={"page": 1, "section": "Definitions"},
        method="llm_extraction",
        model_version="deepseek-chat",
        audience_json={"org_id": ORG, "project_ids": []},
        status="proposed",
    )
    db.add(p)
    db.commit()
    return p


def _approve(db, p, **kw):
    from app.services.ontology.document_review import review_proposal

    kw.setdefault("domain_id", None)
    return review_proposal(db, ORG, p.id, "approve", reviewed_by=REVIEWER,
                           **kw)


# ── happy path: create → verified + full D7 provenance ──────────────────

def test_approve_creates_verified_object_with_provenance(db, proposal,
                                                         domain):
    result = _approve(db, proposal, domain_id=domain.id)

    assert result["status"] == "approved"
    obj = db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).one()
    assert result["object_id"] == obj.id
    # P1 semantics: human approval → verified; confidence is NOT a score.
    assert obj.evidence_status == "verified"
    assert obj.confidence is None
    # Proposal closure
    assert proposal.status == "approved"
    assert proposal.object_id == obj.id
    assert proposal.reviewed_by == REVIEWER
    assert proposal.reviewed_at is not None


def test_provenance_entry_carries_full_d7_fields(db, proposal, domain):
    _approve(db, proposal, domain_id=domain.id)

    obj = db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).one()
    entries = (obj.provenance_json or {}).get("document_provenance") or []
    assert len(entries) == 1
    e = entries[0]
    assert e["source_kb_id"] == proposal.source_kb_id
    assert e["doc_version_id"] == proposal.doc_version_id
    assert e["content_hash"] == "a" * 64
    assert e["location"] == {"page": 1, "section": "Definitions"}
    assert e["quote"] == QUOTE
    assert e["reviewer"] == REVIEWER
    assert e["decision"] == "approved"
    assert e["audience_snapshot"] == {"org_id": ORG, "project_ids": []}


def test_decision_event_and_audit_written_on_approve(db, proposal, domain):
    from app.models.audit_event import AuditEvent
    from app.models.ontology import DocumentDecisionEvent

    _approve(db, proposal, domain_id=domain.id)

    events = db.query(DocumentDecisionEvent).filter(
        DocumentDecisionEvent.org_id == ORG,
        DocumentDecisionEvent.proposal_id == proposal.id,
    ).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_kind == "approve"
    assert ev.event_seq == 1
    assert ev.previous_status == "proposed"
    assert ev.new_status == "approved"
    assert ev.reviewed_by == REVIEWER

    audits = db.query(AuditEvent).filter(
        AuditEvent.org_id == ORG,
        AuditEvent.event_type == "ontology_document_review",
        AuditEvent.resource_id == proposal.id,
    ).all()
    assert len(audits) == 1
    assert audits[0].actor_id == REVIEWER
    assert audits[0].detail_json["decision"] == "approve"


def test_revision_recorded_on_approve(db, proposal, domain):
    from app.models.ontology_revision import OntologyRevision

    _approve(db, proposal, domain_id=domain.id)

    obj = db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).one()
    revs = db.query(OntologyRevision).filter(
        OntologyRevision.org_id == ORG,
        OntologyRevision.entity_type == "object",
        OntologyRevision.entity_id == obj.id,
    ).all()
    assert len(revs) == 1
    assert revs[0].after_json["evidence_status"] == "verified"


# ── attach-to-existing (conflict resolution) ─────────────────────────────

def test_attach_to_existing_inferred_object(db, proposal, domain):
    existing = OntologyObject(
        org_id=ORG, domain_id=domain.id, name="Customer",
        description="Old description", backing_kind="manual",
        evidence_status="inferred",
    )
    db.add(existing)
    db.commit()
    proposal.conflict_json = {"existing_object_id": existing.id,
                              "existing_name": existing.name,
                              "existing_is_active": existing.is_active,
                              "existing_evidence_status": "inferred"}
    db.commit()

    result = _approve(db, proposal)

    # No NEW object; the existing one is attached and authorized.
    assert db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).count() == 1
    assert result["object_id"] == existing.id
    assert existing.evidence_status == "verified"
    entries = (existing.provenance_json or {}).get("document_provenance")
    assert entries and entries[0]["quote"] == QUOTE
    assert proposal.object_id == existing.id


def test_two_documents_on_one_object_append_independent_entries(
        db, proposal, domain):
    existing = OntologyObject(
        org_id=ORG, domain_id=domain.id, name="Customer",
        backing_kind="manual", evidence_status="inferred",
    )
    db.add(existing)
    db.commit()
    proposal.conflict_json = {"existing_object_id": existing.id,
                              "existing_name": existing.name}
    _approve(db, proposal)

    # A second proposal from a DIFFERENT document (different audience).
    kb2 = KnowledgeBase(org_id=ORG, name="sales-playbook",
                        source_kind="file", file_type="txt",
                        file_url="/api/uploads/playbook.txt")
    db.add(kb2)
    db.flush()
    ver2 = DocumentVersion(
        org_id=ORG, kb_id=kb2.id, content_hash="b" * 64,
        file_url_at_ingest=kb2.file_url,
        first_indexed_at=datetime.now(timezone.utc),
    )
    db.add(ver2)
    db.flush()
    kb2.content_hash = "b" * 64
    kb2.content_version_id = ver2.id
    p2 = DocumentProposal(
        org_id=ORG, kb_id=kb2.id, doc_version_id=ver2.id,
        source_kb_id=kb2.id, chunk_key=f"{kb2.id}_{ver2.id[:8]}_0",
        proposal_kind="definition",
        payload_json={"name": "Customer", "description": "v2"},
        quote="Second document span.",
        location_json={"page": 3}, method="llm_extraction",
        model_version="deepseek-chat",
        audience_json={"org_id": ORG, "project_ids": ["proj-finance"]},
        status="proposed",
        conflict_json={"existing_object_id": existing.id,
                       "existing_name": existing.name},
    )
    db.add(p2)
    db.commit()

    _approve(db, p2)

    entries = (existing.provenance_json or {}).get("document_provenance")
    assert len(entries) == 2
    assert entries[0]["doc_version_id"] != entries[1]["doc_version_id"]
    assert entries[0]["quote"] != entries[1]["quote"]
    # Independent audiences ride on each entry (D7 per-entry authorization).
    assert entries[0]["audience_snapshot"]["project_ids"] == []
    assert entries[1]["audience_snapshot"]["project_ids"] == ["proj-finance"]


# ── refusal paths (explicit reasons, proposal stays in review) ───────────

def test_verified_definition_never_overwritten_but_entry_appends(
        db, proposal, domain):
    verified = OntologyObject(
        org_id=ORG, domain_id=domain.id, name="Customer",
        description="THE verified definition", backing_kind="manual",
        evidence_status="verified",
    )
    db.add(verified)
    db.commit()
    proposal.conflict_json = {"existing_object_id": verified.id,
                              "existing_name": verified.name,
                              "existing_evidence_status": "verified"}
    db.commit()

    result = _approve(db, proposal)

    # Definition untouched; the corroborating document appends its entry
    # (per-entry provenance D7 — binding scenario 5 groundwork).
    db.expire_all()
    obj = db.query(OntologyObject).filter(
        OntologyObject.id == verified.id).one()
    assert obj.description == "THE verified definition"
    assert obj.evidence_status == "verified"
    assert result["object_id"] == verified.id
    entries = (obj.provenance_json or {}).get("document_provenance")
    assert entries and entries[0]["quote"] == QUOTE


def test_audience_unresolvable_is_not_promotable(db, proposal, domain):
    proposal.audience_json = None
    db.commit()

    with pytest.raises(ValueError, match="audience"):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"


def test_create_requires_resolvable_domain(db, proposal):
    with pytest.raises(ValueError, match="domain"):
        _approve(db, proposal, domain_id=None)


# ── atomicity (scenarios 6 & 7) ──────────────────────────────────────────

def test_stamp_failure_rolls_back_every_mutation(db, proposal, domain,
                                                 monkeypatch):
    import app.services.ontology.document_review as dr

    def _boom(*a, **k):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(dr, "stamp_human_document_approval", _boom)

    with pytest.raises(RuntimeError):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"
    assert db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).count() == 0
    from app.models.ontology import DocumentDecisionEvent
    assert db.query(DocumentDecisionEvent).count() == 0


def test_audit_failure_rolls_back_every_mutation(db, proposal, domain,
                                                 monkeypatch):
    import app.services.ontology.document_review as dr

    def _boom(*a, **k):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(dr, "_write_audit", _boom)

    with pytest.raises(RuntimeError):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"
    assert proposal.reviewed_by is None
    assert db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).count() == 0
    from app.models.ontology import DocumentDecisionEvent
    assert db.query(DocumentDecisionEvent).count() == 0


# ── machine evidence can never become verified (scenario 8) ──────────────

def test_machine_caller_cannot_set_verified(db, proposal, domain):
    from app.services.ontology.evidence import (
        stamp_human_document_approval,
    )

    obj = OntologyObject(org_id=ORG, domain_id=domain.id, name="X",
                         backing_kind="manual", evidence_status="proposed")
    db.add(obj)
    db.commit()

    for bad in (None, "", "  "):
        with pytest.raises(ValueError, match="human"):
            stamp_human_document_approval(obj, reviewer_id=bad,
                                          proposal=proposal)
    assert obj.evidence_status == "proposed"


def test_stamp_preserves_existing_provenance_entries(db, proposal, domain):
    from app.services.ontology.evidence import (
        stamp_human_document_approval,
    )

    obj = OntologyObject(
        org_id=ORG, domain_id=domain.id, name="X", backing_kind="manual",
        evidence_status="inferred",
        provenance_json={"method": "discovery", "kb_id": "kb-old",
                         "document_provenance": [
                             {"doc_version_id": "older-version",
                              "quote": "an older span"}]},
    )
    db.add(obj)
    db.commit()

    stamp_human_document_approval(obj, reviewer_id=REVIEWER,
                                  proposal=proposal)
    entries = obj.provenance_json["document_provenance"]
    assert len(entries) == 2  # appended, never replaced
    assert obj.provenance_json["method"] == "discovery"  # keys preserved


# ── reject / revise / history ────────────────────────────────────────────

def test_reject_records_decision(db, proposal):
    from app.services.ontology.document_review import review_proposal

    result = review_proposal(db, ORG, proposal.id, "reject",
                             reviewed_by=REVIEWER)
    assert result["status"] == "rejected"
    assert proposal.reviewed_by == REVIEWER
    db.expire_all()
    # No object was ever created by a rejection.
    assert db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).count() == 0


def test_same_status_review_is_noop_error(db, proposal):
    from app.services.ontology.document_review import review_proposal

    review_proposal(db, ORG, proposal.id, "reject", reviewed_by=REVIEWER)
    with pytest.raises(ValueError, match="rejected"):
        review_proposal(db, ORG, proposal.id, "reject", reviewed_by=REVIEWER)
    with pytest.raises(ValueError, match="rejected"):
        review_proposal(db, ORG, proposal.id, "approve", reviewed_by=REVIEWER)


def test_revise_updates_payload_and_records_event(db, proposal):
    from app.services.ontology.document_review import revise_proposal

    out = revise_proposal(db, ORG, proposal.id,
                          {"name": "Customer",
                           "description": "Revised by the admin."},
                          reviewed_by=REVIEWER)
    assert out["payload_json"]["description"] == "Revised by the admin."
    assert proposal.payload_json["description"] == "Revised by the admin."
    assert proposal.status == "proposed"  # revise never promotes

    from app.models.ontology import DocumentDecisionEvent
    kinds = [e.event_kind for e in db.query(DocumentDecisionEvent).filter(
        DocumentDecisionEvent.proposal_id == proposal.id).all()]
    assert kinds == ["revise"]


def test_revise_refused_after_decision(db, proposal):
    from app.services.ontology.document_review import (
        review_proposal, revise_proposal,
    )

    review_proposal(db, ORG, proposal.id, "reject", reviewed_by=REVIEWER)
    with pytest.raises(ValueError, match="rejected"):
        revise_proposal(db, ORG, proposal.id, {"name": "X"},
                        reviewed_by=REVIEWER)


def test_decision_history_is_ordered_and_scoped(db, proposal, domain):
    from app.services.ontology.document_review import (
        decision_history, revise_proposal, review_proposal,
    )

    revise_proposal(db, ORG, proposal.id, {"name": "Customer", "v": 2},
                    reviewed_by=REVIEWER)
    review_proposal(db, ORG, proposal.id, "approve",
                    reviewed_by=REVIEWER, domain_id=domain.id)

    hist = decision_history(db, ORG, proposal.id)
    assert [e["event_kind"] for e in hist] == ["revise", "approve"]
    seqs = [e["event_seq"] for e in hist]
    assert seqs == sorted(seqs)
    # Cross-org lookups are a 404-style failure, never a leak.
    with pytest.raises(LookupError):
        decision_history(db, OTHER, proposal.id)


def test_cross_org_review_is_lookup_error(db, proposal):
    from app.services.ontology.document_review import review_proposal

    with pytest.raises(LookupError):
        review_proposal(db, OTHER, proposal.id, "approve",
                        reviewed_by=REVIEWER)


# ── review-hardening pins (independent review, T7 round 1) ───────────────

def test_end_to_end_anonymous_approve_refuses_and_rolls_back(
        db, proposal, domain):
    """Scenario 8 at the production entry point: review_proposal with no
    reviewer identity never reaches `verified` and leaves nothing behind."""
    from app.services.ontology.document_review import review_proposal

    with pytest.raises(ValueError, match="reviewer"):
        review_proposal(db, ORG, proposal.id, "approve", reviewed_by=None,
                        domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"
    assert db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer",
    ).count() == 0
    from app.models.ontology import DocumentDecisionEvent
    assert db.query(DocumentDecisionEvent).count() == 0


def test_rollback_clears_revision_row(db, proposal, domain, monkeypatch):
    """Scenario 6 extended: the savepoint-released revision row is also
    gone after a failed approval."""
    import app.services.ontology.document_review as dr
    from app.models.ontology_revision import OntologyRevision

    monkeypatch.setattr(dr, "_write_audit",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("audit exploded")))

    with pytest.raises(RuntimeError):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert db.query(OntologyRevision).count() == 0


def test_create_path_name_clash_is_explicit_refusal(db, proposal, domain):
    """F11: a same-name object that extraction missed (no conflict_json)
    refuses with a reason, not a raw IntegrityError."""
    db.add(OntologyObject(org_id=ORG, domain_id=domain.id, name="Customer",
                          backing_kind="manual", evidence_status="inferred"))
    db.commit()

    with pytest.raises(ValueError, match="already exists"):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"


def test_audience_org_mismatch_is_not_promotable(db, proposal, domain):
    """F4: a snapshot pointing at a DIFFERENT org does not satisfy D3."""
    proposal.audience_json = {"org_id": OTHER, "project_ids": []}
    db.commit()

    with pytest.raises(ValueError, match="audience"):
        _approve(db, proposal, domain_id=domain.id)

    db.expire_all()
    assert proposal.status == "proposed"
