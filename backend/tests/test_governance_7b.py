"""Phase 7B — complete enterprise knowledge governance.

Completes the two missing review lifecycles using EXISTING patterns:

- OntologyLink review (proposed/inferred → verified | rejected) via the
  generic enterprise confirm/reject endpoints (kind='link'), org-scoped,
  both endpoints validated (exist, same org, not deleted), review history
  appended to provenance_json (NO migration), every decision audited.
- Rediscovery hardening: stamp_link NEVER downgrades a verified or
  rejected relationship.
- Unified queue: document proposals (D6 document-access gated) and
  proposed links join the EXISTING review queue — no second queue.
- BCS lifecycle honesty: rejected links are EXCLUDED from context
  (previously leaked as hypotheses); approved document knowledge enters
  as VERIFIED objects and never creates BusinessRules.

DocumentProposal approve/reject/revise already existed (atomic service,
decision events, D6 gating) — exercised here for regression + BCS
integration proof, not reimplemented.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db, require_org_admin
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.enterprise_process import (
    BusinessCapability, BusinessProcess, OrgUnit, ProcessActivity,
    ProcessEvidence)
from app.models.knowledge_base import DocumentVersion, KnowledgeBase
from app.models.ontology import (
    DocumentDecisionEvent, DocumentProposal, OntologyDomain, OntologyLink,
    OntologyObject)
from app.models.ontology_revision import OntologyRevision
from app.models.resource_access_policy import ResourceAccessPolicy
from app.routers.ontology import router as ontology_router
from app.services.enterprise.business_context import build_business_context
from app.services.ontology.evidence import stamp_link

ORG = "org-acme"
ORG2 = "org-other"
ADMIN = SimpleNamespace(id="u-admin", org_id=ORG, role="admin",
                        is_super_admin=False, full_name="Ada Admin",
                        email="ada@acme.test")


@pytest.fixture()
def env():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        OntologyDomain.__table__, OntologyObject.__table__,
        OntologyLink.__table__, DocumentProposal.__table__,
        DocumentDecisionEvent.__table__,
        OntologyRevision.__table__, ResourceAccessPolicy.__table__,
        AuditEvent.__table__, KnowledgeBase.__table__,
        DocumentVersion.__table__,
        BusinessRule.__table__, OrgUnit.__table__,
        BusinessCapability.__table__, BusinessProcess.__table__,
        ProcessActivity.__table__, ProcessEvidence.__table__])
    s = sessionmaker(bind=eng)()
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    state = {"admin": True}

    def _get_db():
        yield s

    def _admin():
        if not state["admin"]:
            raise HTTPException(status_code=403, detail="org admin required")
        return ADMIN

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_org_admin] = _admin
    app.dependency_overrides[get_current_user_required] = lambda: ADMIN
    c = TestClient(app)
    c.db = s
    c.state = state
    yield c
    s.close()
    eng.dispose()


def _seed_pair(db, *, link_status="proposed", to_org=ORG,
               delete_to=False, org=ORG):
    db.add(OntologyDomain(id="d1", org_id=org, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=org, domain_id="d1", name="Order",
                          evidence_status="verified", backing_kind="manual"))
    db.add(OntologyObject(id="o2", org_id=to_org, domain_id="d1",
                          name="Customer", evidence_status="proposed",
                          backing_kind="manual",
                          is_deleted=delete_to))
    db.add(OntologyLink(id="l1", org_id=org, from_object_id="o1",
                        to_object_id="o2", link_type="belongs_to",
                        evidence_status=link_status, confidence=0.7))
    db.commit()


# 1. link review lifecycle ---------------------------------------------------------

def test_link_confirm_proposed_to_verified(env):
    _seed_pair(env.db)
    res = env.post("/api/ontology/enterprise/link/l1/confirm",
                   json={"reason": "checked both systems"})
    assert res.status_code == 200
    body = res.json()
    assert body["evidence_status"] == "verified"
    link = env.db.query(OntologyLink).get("l1")
    hist = (link.provenance_json or {}).get("review_history")
    assert hist and hist[-1]["decision"] == "confirmed"
    assert hist[-1]["reviewer_id"] == "u-admin"
    assert hist[-1]["reason"] == "checked both systems"
    # audited (event type follows the shared _record_review convention)
    ev = env.db.query(AuditEvent).filter_by(
        event_type="ontology_link_confirmed", resource_id="l1").all()
    assert len(ev) == 1


def test_link_reject_proposed_to_rejected(env):
    _seed_pair(env.db)
    res = env.post("/api/ontology/enterprise/link/l1/reject", json={})
    assert res.status_code == 200
    assert res.json()["evidence_status"] == "rejected"
    assert env.db.query(AuditEvent).filter_by(
        event_type="ontology_link_rejected", resource_id="l1").count() == 1


def test_duplicate_decision_conflicts_and_preserves_state(env):
    _seed_pair(env.db)
    env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    second = env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    assert second.status_code == 409
    assert env.db.query(OntologyLink).get("l1").evidence_status == "verified"
    rejected_twice = env.post("/api/ontology/enterprise/link/l1/reject",
                              json={})
    assert rejected_twice.status_code == 409  # verified can't be rejected


def test_link_cross_org_404(env):
    _seed_pair(env.db, org=ORG2)
    res = env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    assert res.status_code == 404


def test_link_missing_endpoint_object_refused(env):
    _seed_pair(env.db, delete_to=True)
    res = env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    assert res.status_code == 400  # endpoints must exist and remain valid
    assert env.db.query(OntologyLink).get("l1").evidence_status == "proposed"


def test_link_cross_org_endpoint_refused(env):
    _seed_pair(env.db, to_org=ORG2)  # to_object belongs to another org
    res = env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    assert res.status_code == 400  # no cross-org edge may be confirmed


def test_rediscovery_never_downgrades_terminal_links(env):
    _seed_pair(env.db, link_status="verified")
    link = env.db.query(OntologyLink).get("l1")
    stamp_link(link, by="rediscovery", status="inferred")
    assert link.evidence_status == "verified"  # verified survives rediscovery
    # a second, rejected relationship in the same org
    env.db.add(OntologyLink(id="l2", org_id=ORG, from_object_id="o2",
                            to_object_id="o1", link_type="fulfills",
                            evidence_status="rejected"))
    env.db.commit()
    link2 = env.db.query(OntologyLink).get("l2")
    stamp_link(link2, by="rediscovery", status="inferred")
    assert link2.evidence_status == "rejected"  # rejected never silently returns


def test_link_review_requires_admin(env):
    _seed_pair(env.db)
    env.state["admin"] = False
    res = env.post("/api/ontology/enterprise/link/l1/confirm", json={})
    assert res.status_code == 403
    assert env.db.query(OntologyLink).get("l1").evidence_status == "proposed"


# 2. unified queue includes links + document proposals -----------------------------

def _seed_docs(db):
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Contracts",
                         source_kind="document"))
    db.add(DocumentProposal(
        id="dp1", org_id=ORG, kb_id="kb-1", source_kb_id="kb-1",
        doc_version_id="dv1",
        chunk_key="c1", proposal_kind="object",
        payload_json={"name": "Penalty Clause",
                      "description": "Late-payment penalty"},
        quote="a 2% penalty applies", status="proposed",
        audience_json={"org_id": ORG}, method="llm_extraction"))
    db.add(DocumentProposal(
        id="dp2", org_id=ORG, kb_id="kb-x", source_kb_id="kb-x",
        doc_version_id="dv2",
        chunk_key="c2", proposal_kind="object",
        payload_json={"name": "Foreign Clause"}, quote="foreign terms",
        status="proposed", audience_json={"org_id": ORG},
        method="llm_extraction"))  # source KB cross-org -> D6-hidden
    db.commit()


def test_queue_includes_links_and_document_proposals(env):
    _seed_pair(env.db)
    _seed_docs(env.db)
    res = env.get("/api/ontology/enterprise/review-queue")
    assert res.status_code == 200
    body = res.json()
    links = body["links"]
    assert len(links) == 1
    assert links[0]["kind"] == "link"
    assert links[0]["label"] == "Order —belongs_to→ Customer"
    assert links[0]["confidence"] == 0.7
    docs = body["document_proposals"]
    # D6: the cross-org-source proposal is hidden but honestly counted
    assert [d["id"] for d in docs] == ["dp1"]
    assert body["hidden_document_proposals"] == 1
    assert body["counts"]["links"] == 1
    assert body["counts"]["document_proposals"] == 1


def test_queue_rejected_links_excluded(env):
    _seed_pair(env.db, link_status="rejected")
    body = env.get("/api/ontology/enterprise/review-queue").json()
    assert body["links"] == []
    assert body["counts"]["links"] == 0


# 3. BCS lifecycle honesty ---------------------------------------------------------

def test_bcs_verified_link_is_fact_proposed_is_hypothesis(env):
    _seed_pair(env.db, link_status="proposed")
    ctx = build_business_context(env.db, ADMIN, ORG, query="orders")
    links = ctx["ontology"]["links"]
    entry = next(l for l in links if l["type"] == "belongs_to")
    assert entry["hypothesis"] is True  # proposed stays explicitly labeled

    link = env.db.query(OntologyLink).get("l1")
    link.evidence_status = "verified"
    env.db.commit()
    ctx2 = build_business_context(env.db, ADMIN, ORG, query="orders")
    entry2 = next(l for l in ctx2["ontology"]["links"]
                  if l["type"] == "belongs_to")
    assert "hypothesis" not in entry2  # verified relationship is a fact


def test_bcs_excludes_rejected_links(env):
    _seed_pair(env.db, link_status="rejected")
    ctx = build_business_context(env.db, ADMIN, ORG, query="orders")
    types = [l["type"] for l in ctx["ontology"]["links"]]
    assert "belongs_to" not in types  # rejected knowledge is never included


def test_document_approval_enters_bcs_as_verified_object_never_rules(env):
    db = env.db
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(DocumentProposal(
        id="dp9", org_id=ORG, kb_id="kb-1", source_kb_id="kb-1",
        doc_version_id="dv9",
        chunk_key="c9", proposal_kind="object",
        payload_json={"name": "Penalty Clause",
                      "description": "Late-payment penalty",
                      "domain_id": "d1"},
        quote="a 2% penalty applies", status="proposed",
        audience_json={"org_id": ORG}, method="llm_extraction"))
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Contracts",
                         source_kind="document", created_by_id="u-admin"))
    db.commit()

    res = env.post("/api/ontology/document-evidence/dp9/approve", json={})
    assert res.status_code == 200
    obj = db.query(OntologyObject).filter_by(name="Penalty Clause").one()
    assert obj.evidence_status == "verified"  # D2 stamp — the only path
    # approved document knowledge NEVER becomes executable business rules
    assert db.query(BusinessRule).filter_by(org_id=ORG).count() == 0

    ctx = build_business_context(db, ADMIN, ORG, query="penalty")
    names = [o["name"] for o in ctx["ontology"]["objects"]]
    assert "Penalty Clause" in names  # informational knowledge, in context
    entry = next(o for o in ctx["ontology"]["objects"]
                 if o["name"] == "Penalty Clause")
    assert entry.get("evidence_status") == "verified"
