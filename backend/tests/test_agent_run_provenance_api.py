"""Phase 8 T8-9 — GET /api/agent-runs/{run_id}/provenance (org-scoped)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.user import User
from app.deps import get_current_user_required, get_db
from app.routers.agent_runs import router
from app.services.harness.run_service import AgentRunService

svc = AgentRunService()
ORG = "org-acme"
OTHER = "org-other"


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[AgentRun.__table__, User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u1", org_id=ORG, email="a@t", full_name="A",
               role="admin", password_hash="x"))
    s.add(User(id="u2", org_id=OTHER, email="b@t", full_name="B",
               role="admin", password_hash="x"))
    s.commit()

    run_id = svc.create_run_record(
        agent_name="analyst", task="t",
        caller_context={"org_id": ORG, "user_id": "u1"}, db=s)
    svc.finalize_run(run_id, status="completed", db=s)

    app = FastAPI()
    app.include_router(router)

    state = {"user": "u1"}

    def _get_db():
        yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = lambda: s.get(
        User, state["user"])
    c = TestClient(app)
    c.state_db = s
    c.state_user = state
    c.run_id = run_id
    yield c
    s.close()
    eng.dispose()


def test_provenance_endpoint_returns_envelope(client):
    res = client.get(f"/agent-runs/{client.run_id}/provenance")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["version"] == "prov-v1"
    assert body["run"]["run_id"] == client.run_id
    assert body["run"]["org_id"] == ORG


def test_provenance_cross_org_hidden(client):
    client.state_user["user"] = "u2"
    res = client.get(f"/agent-runs/{client.run_id}/provenance")
    assert res.status_code in (403, 404), res.text


def test_provenance_missing_run_404(client):
    res = client.get("/agent-runs/nope/provenance")
    assert res.status_code == 404
