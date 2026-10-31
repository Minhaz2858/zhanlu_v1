"""EBPM Stage 4 increment 1 — deterministic enterprise resource discovery.

The inventory is assembled ONLY from the org's own authorized rows; a second
org sees nothing; discovery persistence is an upsert (idempotent refresh).
"""

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.automation_task import AutomationTask
from app.models.agent_run import AgentRun
from app.models.enterprise_process import EnterpriseResource
from app.models.knowledge_catalog import KBTableRelation
from app.routers.ontology import router as ontology_router
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin")


@pytest.fixture()
def client():
    eng = make_engine()
    Base.metadata.create_all(eng, tables=[EnterpriseResource.__table__,
                                          AutomationTask.__table__,
                                          AgentRun.__table__,
                                          KBTableRelation.__table__])
    s = make_session(eng)
    data_path = tempfile.mktemp(suffix=".db")
    create_data_db(data_path)
    build_sandbox(s, data_path)
    s.commit()
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_required] = lambda: _admin()
    yield TestClient(app), s
    s.close()
    eng.dispose()


def test_inventory_lists_only_own_org_resources(client):
    c, s = client
    r = c.get("/api/ontology/enterprise/inventory")
    assert r.status_code == 200
    inv = r.json()
    # The automotive sandbox: one sqlite datasource, one file KB, one doc KB.
    assert inv["summary"]["datasources"] >= 1
    assert inv["summary"]["documents"] >= 2
    assert inv["ontology"]["objects"] > 0
    # schema catalog is attached to the datasource (deterministic, no LLM)
    ds = inv["datasources"][0]
    assert ds["tables"] >= 1 and ds["columns"] >= 1

    # Cross-org isolation: a different org sees empty everything.
    c.app.dependency_overrides[get_current_user_required] = \
        lambda: _admin("other-org")
    inv2 = c.get("/api/ontology/enterprise/inventory").json()
    assert inv2["summary"]["datasources"] == 0
    assert inv2["summary"]["documents"] == 0
    assert inv2["ontology"]["objects"] == 0


def test_discover_persists_inventory_idempotently(client):
    c, s = client
    r = c.post("/api/ontology/enterprise/discover")
    assert r.status_code == 200
    rows = s.query(EnterpriseResource).filter(
        EnterpriseResource.org_id == ORG).all()
    kinds = {r_.resource_kind for r_ in rows}
    assert {"datasource", "document"} <= kinds
    n_first = len(rows)

    # Second pass: refresh, not duplicate.
    c.post("/api/ontology/enterprise/discover")
    rows2 = s.query(EnterpriseResource).filter(
        EnterpriseResource.org_id == ORG).all()
    assert len(rows2) == n_first
    assert all(r_.discovered_at for r_ in rows2)

    # Resources endpoint requires auth + org scope.
    listed = c.get("/api/ontology/enterprise/resources").json()
    assert any(x["resource_kind"] == "datasource" for x in listed)


def test_discover_requires_org_admin(client):
    c, _s = client
    c.app.dependency_overrides[get_current_user_required] = \
        lambda: SimpleNamespace(id="u-v", org_id=ORG, role="user")
    assert c.post("/api/ontology/enterprise/discover").status_code == 403
