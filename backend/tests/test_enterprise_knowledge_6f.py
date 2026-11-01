"""Phase 6F — Enterprise Intelligence final UX & integration (backend).

Four additive, read-only enrichments completing the investigation chain —
NO new stores, NO migrations, NO second context system:

- BusinessRule detail gains ``effect`` (marker protocol: block | approval |
  informational) and ``resolved_evidence`` (source_evidence_ids resolved to
  org ProcessEvidence rows; missing ids honestly marked unavailable);
- Business object detail gains ``related_rules`` — ONLY rules whose
  persisted scope references this object or its domain (never invented);
- ``GET /ontology/enterprise/resources/{ref_id}/onboarding-history`` —
  bounded run history for the Resource → onboarding-history link;
- ``GET /ontology/enterprise/assistant-context`` — AI Workforce
  explainability by calling the EXISTING Business Context Service builder
  (no duplication); never exposes backing refs; authorization stays with
  the BCS/project-access contract (cross-org project → 404).
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.business_rule import BusinessRule
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    EnterpriseResource, ProcessEvidence)
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import OntologyDomain, OntologyObject
from app.models.project import Project
from app.routers.ontology import router as ontology_router

ORG = "org-acme"
ORG2 = "org-other"


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    from app.models.ontology import OntologyLink
    tables = [BusinessRule.__table__, OntologyDomain.__table__,
              OntologyObject.__table__, OntologyLink.__table__,
              ProcessEvidence.__table__,
              EnterpriseOnboardingRun.__table__, KnowledgeBase.__table__,
              EnterpriseResource.__table__, Project.__table__]
    # BCS touches more tables — create the full registry where available so
    # sections are real rather than fail-open warnings (fail-open still
    # covered by the no-seed test).
    try:
        from app.models.enterprise_process import (
            BusinessCapability, BusinessProcess, OrgUnit, ProcessActivity)
        from app.models.metric_definition import MetricDefinition
        from app.models.terminology import Terminology
        tables += [BusinessProcess.__table__, OrgUnit.__table__,
                   ProcessActivity.__table__, BusinessCapability.__table__,
                   Terminology.__table__, MetricDefinition.__table__]
    except Exception:  # noqa: BLE001 — optional tables
        pass
    Base.metadata.create_all(eng, tables=tables)
    s = sessionmaker(bind=eng)()
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    user = SimpleNamespace(id="u-owner", org_id=ORG, role="admin",
                           is_super_admin=False)

    def _get_db():
        yield s

    def _user():
        return user

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = _user
    c = TestClient(app)
    c.db = s
    c.user = user
    yield c
    s.close()
    eng.dispose()


def _seed_rule_with_evidence(db):
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order",
                          description="A customer order",
                          evidence_status="verified",
                          backing_kind="table",
                          backing_ref="kb:kb-1:sales_order"))
    db.add(ProcessEvidence(id="ev-1", org_id=ORG, process_id="p1",
                           evidence_kind="ontology_object",
                           source_ref="ontology:o1",
                           evidence_status="verified"))
    db.add(BusinessRule(
        id="br1", org_id=ORG, scope="object", scope_ref_id="o1",
        statement="PROHIBIT: selling below cost", status="enabled",
        applies_to="action", source_evidence_ids=["ev-1", "ev-gone"]))
    db.add(BusinessRule(
        id="br2", org_id=ORG, scope="object", scope_ref_id="o1",
        statement="Review bulk discounts before approval", status="verified",
        applies_to="action"))
    db.add(BusinessRule(
        id="br3", org_id=ORG, scope="object", scope_ref_id="o-other",
        statement="PROHIBIT: unrelated", status="enabled"))
    db.add(BusinessRule(
        id="br4", org_id=ORG2, scope="object", scope_ref_id="o1",
        statement="PROHIBIT: foreign", status="enabled"))
    db.commit()


# 1. rule detail: effect + resolved evidence ---------------------------------------

def test_rule_detail_effect_and_resolved_evidence(client):
    _seed_rule_with_evidence(client.db)
    res = client.get("/api/ontology/enterprise/business-rules/br1")
    assert res.status_code == 200
    body = res.json()
    # marker protocol effect — informational vs executable distinction
    assert body["effect"] == "block"
    # evidence ids resolved against org ProcessEvidence rows
    ev = {e["id"]: e for e in body["resolved_evidence"]}
    assert ev["ev-1"]["available"] is True
    assert ev["ev-1"]["source_ref"] == "ontology:o1"
    assert ev["ev-1"]["evidence_status"] == "verified"
    # missing evidence id honestly marked, never dropped
    assert ev["ev-gone"]["available"] is False


def test_rule_detail_informational_effect(client):
    _seed_rule_with_evidence(client.db)
    body = client.get(
        "/api/ontology/enterprise/business-rules/br2").json()
    assert body["effect"] == "informational"


# 2. object detail: related rules (persisted scope references only) ---------------

def test_object_related_rules(client):
    _seed_rule_with_evidence(client.db)
    res = client.get("/api/ontology/enterprise/business-objects/o1")
    assert res.status_code == 200
    rules = {r["id"]: r for r in res.json()["related_rules"]}
    assert "br1" in rules and "br2" in rules
    assert rules["br1"]["effect"] == "block"
    assert rules["br1"]["status"] == "enabled"
    # other objects' rules and cross-org rules are never included
    assert "br3" not in rules and "br4" not in rules


def test_object_related_rules_empty_honest(client):
    db = client.db
    db.add(OntologyDomain(id="d1", org_id=ORG, code="x", name="X"))
    db.add(OntologyObject(id="o9", org_id=ORG, domain_id="d1", name="Lonely"))
    db.commit()
    body = client.get(
        "/api/ontology/enterprise/business-objects/o9").json()
    assert body["related_rules"] == []


def test_object_related_rules_via_domain_scope(client):
    db = client.db
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order"))
    db.add(BusinessRule(id="brd", org_id=ORG, scope="domain",
                        scope_ref_id="d1", statement="REQUIRE_APPROVAL: exports",
                        status="enabled", applies_to="action"))
    db.commit()
    rules = client.get(
        "/api/ontology/enterprise/business-objects/o1").json()["related_rules"]
    assert [r["id"] for r in rules] == ["brd"]
    assert rules[0]["effect"] == "approval"


# 3. resource onboarding history ---------------------------------------------------

def _seed_resource(db):
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Sales DW"))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="knowledge_base", resource_id="kb-1",
        resource_name="Sales DW", status="completed", attempt=1,
        steps_json=[{"step": "refresh_inventory", "status": "ok"},
                    {"step": "extract_evidence", "status": "ok"}]))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="knowledge_base", resource_id="kb-1",
        resource_name="Sales DW", status="partial_failed", attempt=2,
        error="evidence step failed",
        steps_json=[{"step": "refresh_inventory", "status": "ok"},
                    {"step": "extract_evidence", "status": "failed"}]))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG2, resource_type="knowledge_base", resource_id="kb-1",
        resource_name="Foreign", status="failed", attempt=1))
    db.commit()


def test_onboarding_history_bounded_org_scoped(client):
    _seed_resource(client.db)
    res = client.get(
        "/api/ontology/enterprise/resources/kb-1/onboarding-history")
    assert res.status_code == 200
    body = res.json()
    assert body["identity"]["id"] == "kb-1"
    assert body["total"] == 2  # cross-org run excluded
    assert len(body["runs"]) == 2
    assert body["runs"][0]["status"] in ("completed", "partial_failed")
    assert all("steps" in r for r in body["runs"])
    steps = body["runs"][0]["steps"]
    assert steps[0] == {"step": "refresh_inventory", "status": "ok"}
    # never dumps full steps_json payloads or credentials
    assert all("detail" not in s for r in body["runs"] for s in r["steps"])


def test_onboarding_history_unknown_resource_404(client):
    res = client.get(
        "/api/ontology/enterprise/resources/kb-nope/onboarding-history")
    assert res.status_code == 404


def test_onboarding_history_never_onboarded_empty(client):
    db = client.db
    db.add(KnowledgeBase(id="kb-2", org_id=ORG, name="Fresh KB"))
    db.commit()
    body = client.get(
        "/api/ontology/enterprise/resources/kb-2/onboarding-history").json()
    assert body["total"] == 0 and body["runs"] == []


# 4. assistant-context explainability (reuses BCS builder) --------------------------

def test_assistant_context_shape_and_no_backing_ref_leak(client):
    db = client.db
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order",
                          description="A customer order",
                          evidence_status="verified",
                          backing_kind="table",
                          backing_ref="kb:kb-1:sales_order"))
    db.commit()
    res = client.get("/api/ontology/enterprise/assistant-context",
                     params={"query": "customer orders"})
    assert res.status_code == 200
    body = res.json()
    # BCS contract keys — explainability, not a second context system
    assert "status" in body and "warnings" in body
    assert "token_budget" in body and "evidence_coverage" in body
    # no backing refs, SQL, or credentials ever leak through explainability
    assert "sales_order" not in res.text
    assert "kb:kb-1" not in res.text


def test_assistant_context_cross_org_project_visible_boundary(client):
    # BCS contract (reused verbatim): an unauthorized EXISTING project is
    # honestly OMITTED with authorized:false + reason — never guessed, and
    # the explainability endpoint must make that boundary VISIBLE.
    db = client.db
    db.add(Project(id="prj-f", org_id=ORG2, name="Foreign",
                   created_by_id="someone"))
    db.commit()
    res = client.get("/api/ontology/enterprise/assistant-context",
                     params={"project_id": "prj-f"})
    assert res.status_code == 200
    body = res.json()
    assert body["project"]["authorized"] is False
    assert body["project"]["unavailable_reason"]
    # cross-org project name never leaks through explainability
    assert "Foreign" not in res.text


def test_assistant_context_empty_org_honest(client):
    res = client.get("/api/ontology/enterprise/assistant-context")
    assert res.status_code == 200
    body = res.json()
    # fail-open sections surface warnings; status is honest, never "complete"
    assert body["status"] in ("partial", "unavailable")
    assert isinstance(body["warnings"], list)
