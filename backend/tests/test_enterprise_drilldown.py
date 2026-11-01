"""Phase 6C — enterprise business drilldown APIs.

Two org-scoped, read-only endpoints completing the navigation chain
Process → Activity → Business Object → Resource → Evidence:

- ``GET /api/ontology/enterprise/business-objects/{id}`` — authoritative
  OntologyObject summary: domain, relationships (links with peer names),
  associated processes (via ProcessEvidence), backing resource.
- ``GET /api/ontology/enterprise/resources/{ref_id}`` — resource identity
  + onboarding status (latest run) + related business concepts.

NO credentials, SQL, or execution payloads are ever returned. Cross-org
and missing ids fail closed (404).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from types import SimpleNamespace

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessProcess, EnterpriseResource, OrgUnit, ProcessEvidence)
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import OntologyDomain, OntologyLink, OntologyObject
from app.routers.ontology import router as ontology_router

ORG = "org-acme"
ORG2 = "org-other"


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        OntologyDomain.__table__, OntologyObject.__table__,
        OntologyLink.__table__, BusinessProcess.__table__,
        ProcessEvidence.__table__, EnterpriseResource.__table__,
        EnterpriseOnboardingRun.__table__, KnowledgeBase.__table__,
        OrgUnit.__table__])
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
    yield c
    s.close()
    eng.dispose()


def _seed_object(db):
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Sales DW"))
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order",
                          description="A customer order",
                          evidence_status="verified", backing_kind="table",
                          backing_ref="kb:kb-1:sales_order"))
    db.add(OntologyObject(id="o2", org_id=ORG, domain_id="d1", name="Customer",
                          evidence_status="proposed"))
    db.add(OntologyLink(id="l1", org_id=ORG, from_object_id="o1",
                        to_object_id="o2", link_type="belongs_to"))
    db.add(BusinessProcess(id="p1", org_id=ORG, name="Order to Cash",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(ProcessEvidence(id="ev-1", org_id=ORG, process_id="p1",
                           evidence_kind="ontology_object",
                           source_ref="ontology:o1",
                           evidence_status="verified"))
    db.add(OrgUnit(id="u1", org_id=ORG, name="Operations",
                   unit_type="department"))
    db.commit()


# 1. business object summary -------------------------------------------------------

def test_object_summary_full_shape(client):
    _seed_object(client.db)
    res = client.get("/api/ontology/enterprise/business-objects/o1")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "o1" and body["name"] == "Order"
    assert body["evidence_status"] == "verified"
    assert body["domain"]["name"] == "Sales"
    # relationships with peer names
    assert body["links"][0]["peer_name"] == "Customer"
    assert body["links"][0]["link_type"] == "belongs_to"
    # associated processes via evidence
    assert body["processes"][0]["id"] == "p1"
    assert body["processes"][0]["name"] == "Order to Cash"
    # backing resource resolved from backing_ref (kb:<id>:<table>)
    assert body["backing"]["ref"] == "kb:kb-1:sales_order"
    assert body["backing"]["resource"]["id"] == "kb-1"
    assert body["backing"]["resource"]["kind"] == "knowledge_base"


def test_object_summary_unwired_object_has_no_resource(client):
    _seed_object(client.db)
    body = client.get("/api/ontology/enterprise/business-objects/o2").json()
    assert body["backing"]["resource"] is None  # missing stays missing


def test_object_summary_cross_org_and_missing_fail_closed(client):
    _seed_object(client.db)
    assert client.get(
        "/api/ontology/enterprise/business-objects/ghost").status_code == 404
    client.db.add(OntologyObject(id="ox", org_id=ORG2, domain_id="d-foreign",
                                 name="Foreign", evidence_status="verified"))
    client.db.commit()
    assert client.get(
        "/api/ontology/enterprise/business-objects/ox").status_code == 404


# 2. resource summary ------------------------------------------------------------------

def test_resource_summary_with_onboarding_and_related_concepts(client):
    _seed_object(client.db)
    db = client.db
    db.add(EnterpriseResource(org_id=ORG, name="Sales DW",
                              resource_kind="datasource", ref_id="kb-1"))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="Sales DW", status="completed", steps_json=[],
        attempt=1))
    db.commit()
    res = client.get("/api/ontology/enterprise/resources/kb-1")
    assert res.status_code == 200
    body = res.json()
    assert body["identity"]["name"] == "Sales DW"
    assert body["identity"]["kind"] == "datasource"
    assert body["onboarding"]["status"] == "completed"
    assert body["onboarding"]["attempt"] == 1
    # related concepts: objects backed by this resource
    assert body["related_objects"][0]["id"] == "o1"
    assert body["related_objects"][0]["name"] == "Order"


def test_resource_summary_latest_onboarding_run_wins(client):
    db = client.db
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="DW"))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="DW", status="partial_failed", steps_json=[],
        attempt=1, error="schema mismatch"))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="DW", status="completed", steps_json=[], attempt=2))
    db.commit()
    body = client.get("/api/ontology/enterprise/resources/kb-1").json()
    assert body["onboarding"]["status"] == "completed"  # LATEST run
    assert body["onboarding"]["attempt"] == 2


def test_resource_summary_never_exposes_credentials(client):
    db = client.db
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Sales DW",
                         password="s3cr3t-hunter2", username="etl_user"))
    db.commit()
    body = client.get("/api/ontology/enterprise/resources/kb-1").json()
    assert "s3cr3t-hunter2" not in json.dumps(body)
    assert "etl_user" not in json.dumps(body)


def test_resource_summary_untracked_ref_falls_back_to_kb(client):
    db = client.db
    db.add(KnowledgeBase(id="kb-9", org_id=ORG, name="Docs KB"))
    db.commit()
    body = client.get("/api/ontology/enterprise/resources/kb-9").json()
    assert body["identity"]["name"] == "Docs KB"
    assert body["onboarding"]["status"] is None  # never onboarded — honest


def test_resource_summary_cross_org_fail_closed(client):
    db = client.db
    db.add(KnowledgeBase(id="kb-x", org_id=ORG2, name="Foreign DW"))
    db.commit()
    assert client.get(
        "/api/ontology/enterprise/resources/kb-x").status_code == 404
