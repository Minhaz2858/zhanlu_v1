"""Phase 1B.2 — semantic integration.

Bridge: BusinessRule → SemanticGuidelines (authoritative → compatibility
layer). Covers: enabled SQL rule materialization, proposed/rejected/
non-SQL/verified-only exclusion, idempotency, disable retracting
influence (lifecycle hook), manual guideline preservation, provenance
footer. Terminology: ontology link/unlink/inspect with org-consistency
validation and preserved existing behavior.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import OntologyObject
from app.models.semantic_guideline import SemanticGuideline
from app.models.terminology import Terminology
from app.models.user import User
from app.services.ontology.business_rule_bridge import (
    MARKER_PREFIX, marker_for, sync_business_rules_to_guidelines)
from app.services.ontology.business_rules import review_business_rule

ORG = "org-acme"


def _user(uid, role="user", org=ORG):
    return User(id=uid, email=f"{uid}@t.dev", full_name=uid.title(),
                role=role, password_hash="x", org_id=org)


class _Reviewer:
    id = "u-admin"
    full_name = "Admin"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        BusinessRule.__table__, SemanticGuideline.__table__,
        KnowledgeBase.__table__, Terminology.__table__,
        OntologyObject.__table__, AuditEvent.__table__,
    ])
    s = sessionmaker(bind=eng)()
    # one KB so org-wide rules have a materialization target
    s.add(KnowledgeBase(id="kb-1", name="Sales DB", org_id=ORG,
                        source_kind="database"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _rule(db, **kw):
    rule = dict(org_id=ORG, scope="org", scope_ref_id=None,
                statement="Exclude cancelled orders from revenue",
                status="proposed", applies_to="sql",
                source_evidence_ids=["pe-1"])
    rule.update(kw)
    rid = rule.pop("id", f"br-{len(rule['statement'])%97}")
    row = BusinessRule(id=rid, **rule)
    db.add(row)
    db.commit()
    return row


def _derived(db, org=ORG):
    return db.query(SemanticGuideline).filter(
        SemanticGuideline.org_id == org,
        SemanticGuideline.title.like(f"{MARKER_PREFIX}%"),
        SemanticGuideline.is_deleted == False,  # noqa: E712
    ).all()


# 1. Enabled SQL rule synchronization ---------------------------------------

def test_enabled_sql_rule_materializes(db):
    rule = _rule(db, id="br-ok", status="enabled")
    out = sync_business_rules_to_guidelines(db, ORG)
    assert out["created"] == 1
    rows = _derived(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.datasource_id == "kb-1"  # org-wide → enumerated target
    assert row.is_enabled is True
    assert row.title.startswith(marker_for("br-ok"))
    assert "Exclude cancelled orders" in row.content
    # provenance + evidence preserved in the derived content
    assert f"business_rule:{rule.id}" in row.content
    assert "scope: org:-" in row.content
    assert "evidence: pe-1" in row.content
    assert row.org_id == ORG


def test_targeted_rule_materializes_per_datasource(db):
    _rule(db, id="br-tgt", status="enabled", datasource_ids=["kb-a", "kb-b"])
    sync_business_rules_to_guidelines(db, ORG)
    rows = sorted(r.datasource_id for r in _derived(db))
    assert rows == ["kb-a", "kb-b"]


# 2-4. Exclusions + idempotency ----------------------------------------------

def test_proposed_rejected_verified_nonsql_never_materialize(db):
    _rule(db, id="br-prop", status="proposed")
    _rule(db, id="br-rej", status="rejected", statement="r2")
    _rule(db, id="br-ver", status="verified", statement="r3")
    _rule(db, id="br-act", status="enabled", applies_to="action",
          statement="r4")
    sync_business_rules_to_guidelines(db, ORG)
    assert _derived(db) == []
    # same on the per-rule path: any state change retracts previous rows
    ver = db.get(BusinessRule, "br-ver")
    db.add(SemanticGuideline(org_id=ORG, datasource_id="kb-1", kind="other",
                             title=f"{marker_for('br-ver')} org",
                             content="stale", is_enabled=True))
    db.commit()
    from app.services.ontology.business_rule_bridge import sync_single_rule
    sync_single_rule(db, ver)
    assert _derived(db) == []


def test_sync_is_idempotent(db):
    _rule(db, id="br-idem", status="enabled")
    first = sync_business_rules_to_guidelines(db, ORG)
    assert first["created"] == 1
    second = sync_business_rules_to_guidelines(db, ORG)
    assert (second["created"], second["updated"], second["removed"]) == \
        (0, 0, 0)
    assert len(_derived(db)) == 1


# 5. Disabled rule stops influencing retrieval --------------------------------

def test_disable_retracts_influence(db):
    rule = _rule(db, id="br-dis", status="enabled")
    sync_business_rules_to_guidelines(db, ORG)
    assert len(_derived(db)) == 1
    review_business_rule(db, ORG, "br-dis", "disable", _Reviewer(),
                         reason="paused")
    rows = _derived(db)
    assert rows == []
    # soft-deleted derived rows are invisible to the retrieval filter
    dead = db.query(SemanticGuideline).filter(
        SemanticGuideline.title.like(f"{marker_for('br-dis')}%")).all()
    assert dead and all(r.is_deleted for r in dead)
    # re-enable materializes again (disable left it verified → enable is valid)
    review_business_rule(db, ORG, "br-dis", "enable", _Reviewer())
    assert len(_derived(db)) == 1
    assert db.get(BusinessRule, "br-dis").statement == rule.statement


def test_lifecycle_hook_retracts_on_reject(db):
    # NOTE: rejected is only reachable from proposed (verified rules can
    # never be rejected — by design). The reject hook must still retract
    # any stale derived rows left behind by out-of-band edits.
    _rule(db, id="br-rj", status="proposed")
    db.add(SemanticGuideline(
        org_id=ORG, datasource_id="kb-1", kind="other",
        title=f"{marker_for('br-rj')} org", content="stale",
        is_enabled=True))
    db.commit()
    review_business_rule(db, ORG, "br-rj", "reject", _Reviewer(),
                         reason="obsolete")
    assert _derived(db) == []


# 6. Manual guidelines preserved ----------------------------------------------

def test_manual_guidelines_untouched(db):
    db.add(SemanticGuideline(
        org_id=ORG, datasource_id="kb-1", kind="policy",
        title="Fiscal year starts July", content="FY = Jul..Jun",
        is_enabled=True))
    db.commit()
    manual_before = db.query(SemanticGuideline).filter(
        SemanticGuideline.title == "Fiscal year starts July").first()
    _rule(db, id="br-man", status="enabled")
    sync_business_rules_to_guidelines(db, ORG)
    db.expire_all()
    manual = db.query(SemanticGuideline).filter(
        SemanticGuideline.title == "Fiscal year starts July").first()
    assert manual.id == manual_before.id
    assert manual.is_deleted is False and manual.is_enabled is True
    assert manual.content == "FY = Jul..Jun"
    assert len(_derived(db)) == 1  # derived row coexists


# Terminology → Ontology link --------------------------------------------------

@pytest.fixture()
def client(db):
    from app.database import get_db as get_db_database
    from app.routers.terminologies import register_terminologies_router

    app = FastAPI()
    app.include_router(register_terminologies_router(), prefix="/api")

    def _db():
        yield db

    # terminologies.py uses app.database.get_db (a DIFFERENT function
    # object from app.deps.get_db) — override both
    app.dependency_overrides[get_db_database] = _db
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_required] = \
        lambda: _user("u-any")
    return TestClient(app, raise_server_exceptions=False)


def _term(db, **kw):
    row = dict(word="ARR", description="Annual recurring revenue",
               datasource_ids='["kb-1"]', org_id=ORG)
    row.update(kw)
    obj = Terminology(id=kw.get("id", "t-1"), **row)
    db.add(obj)
    db.commit()
    return obj


def _object(db, **kw):
    row = dict(name="Customer", domain_id="dom-1")
    row.update(kw)
    oid = row.pop("id", "oo-1")
    org = row.pop("org_id", ORG)
    obj = OntologyObject(id=oid, org_id=org, **row)
    db.add(obj)
    db.commit()
    return obj


def test_terminology_ontology_link_works(client, db):
    _term(db)
    _object(db)
    r = client.post(
        "/api/datasources/kb-1/terminologies/t-1/ontology-link",
        json={"ontology_object_id": "oo-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ontology_object_id"] == "oo-1"
    assert body["ontology_object"]["name"] == "Customer"
    # inspect path
    g = client.get(
        "/api/datasources/kb-1/terminologies/t-1/ontology-link").json()
    assert g["ontology_object"]["id"] == "oo-1"
    # list serialization exposes the link id
    listed = client.get(
        "/api/datasources/kb-1/terminologies").json()
    assert listed[0]["ontology_object_id"] == "oo-1"
    # unlink restores the unlinked state
    u = client.delete(
        "/api/datasources/kb-1/terminologies/t-1/ontology-link")
    assert u.json()["ontology_object_id"] is None


def test_cross_org_link_rejected(client, db):
    _term(db)
    _object(db, org_id="org-evil")
    r = client.post(
        "/api/datasources/kb-1/terminologies/t-1/ontology-link",
        json={"ontology_object_id": "oo-1"})
    assert r.status_code == 404  # fail closed: missing == cross-org
    assert db.get(Terminology, "t-1").ontology_object_id is None
    # missing object also 404
    r2 = client.post(
        "/api/datasources/kb-1/terminologies/t-1/ontology-link",
        json={"ontology_object_id": "oo-missing"})
    assert r2.status_code == 404


def test_existing_terminology_behavior_preserved(client, db):
    _term(db)
    _object(db)
    # upsert / list / search still behave (unlinked rows fully functional)
    r = client.post("/api/datasources/kb-1/terminologies",
                    json={"word": "MRR", "description": "Monthly"})
    assert r.status_code == 200
    assert r.json()["ontology_object_id"] is None
    listed = client.get("/api/datasources/kb-1/terminologies").json()
    assert {t["word"] for t in listed} == {"ARR", "MRR"}
    s = client.get("/api/datasources/kb-1/terminologies/search",
                   params={"q": "ARR"}).json()
    assert "ARR" in {hit["word"] for hit in s}
