"""Phase 3E — observations endpoint (HTTP-level, org-scoped, fail-closed)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.agent_run import AgentRun
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.enterprise_process import (
    BusinessProcess, ProcessActivity, ProcessEvidence, ProcessLink)
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyAction, OntologyObject
from app.routers.ontology import router as ontology_router

ORG = "org-acme"
ORG2 = "org-other"


def _user(org=ORG):
    return SimpleNamespace(id="u-1", org_id=org, role="user",
                           full_name="V", email="v@t.dev")


@pytest.fixture()
def env():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        Nl2sqlQueryLog.__table__, AgentRun.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__, ProcessLink.__table__,
        OntologyObject.__table__, OntologyAction.__table__,
        BusinessRule.__table__, AuditEvent.__table__])
    s = sessionmaker(bind=eng)()
    db = s
    db.add(BusinessProcess(id="proc-1", org_id=ORG, name="P",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(Nl2sqlQueryLog(id="l1", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1",
                          activity_seq=1, match_confidence=1.0,
                          match_method="explicit"))
    db.commit()

    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = lambda: _user()
    client = TestClient(app)
    yield client, db
    s.close()
    eng.dispose()


def test_observations_endpoint_summary(env):
    client, _ = env
    r = client.get("/api/ontology/enterprise/processes/proc-1/observations")
    assert r.status_code == 200
    body = r.json()
    assert body["execution_count"] == 1
    assert body["activity_counts"] == {"1": 1}
    assert body["confidence_distribution"]["high"] == 1
    assert body["references"][0]["record_id"] == "l1"
    # sensitive payloads never leak
    assert "question" not in body["references"][0]


def test_observations_endpoint_empty_history(env):
    client, db = env
    db.add(BusinessProcess(id="proc-empty", org_id=ORG, name="E",
                           discovery_status="proposed",
                           evidence_status="proposed"))
    db.commit()
    r = client.get(
        "/api/ontology/enterprise/processes/proc-empty/observations")
    assert r.status_code == 200
    body = r.json()
    assert body["execution_count"] == 0 and body["references"] == []
    assert body["time_range"] is None


def test_observations_endpoint_cross_org_fail_closed(env):
    client, _ = env
    app_user = _user(ORG2)
    client.app.dependency_overrides[get_current_user_required] = (
        lambda: app_user)
    r = client.get("/api/ontology/enterprise/processes/proc-1/observations")
    assert r.status_code == 404


def test_observations_endpoint_missing_process_404(env):
    client, _ = env
    r = client.get("/api/ontology/enterprise/processes/proc-x/observations")
    assert r.status_code == 404
