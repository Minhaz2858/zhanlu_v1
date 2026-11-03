"""Phase 8 T8-3 — binding CRUD + source-descriptor API (agent-studio routes).

The legacy studio endpoints inserted AgentDataBinding rows directly with no
org validation and no projection update; they now route through the binding
service. Adds PATCH/DELETE and the source-descriptors contract.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.agent_skill_binding import AgentSkillBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.deps import get_current_user_required, get_db
from app.routers.agent_studio import router

ORG = "org-acme"
OTHER = "org-other"


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentApp.__table__, AgentDataBinding.__table__,
        AgentSkillBinding.__table__, KnowledgeBase.__table__,
        User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u-admin", org_id=ORG, email="a@t", full_name="A",
               role="admin", password_hash="x"))
    s.add(User(id="u-other", org_id=OTHER, email="b@t", full_name="B",
               role="admin", password_hash="x"))
    s.add(AgentApp(id="app-1", name="Analyst", org_id=ORG,
                   created_by_id="u-admin"))
    s.add(KnowledgeBase(id="ds-1", name="Sales DW", org_id=ORG,
                        source_kind="database", db_type="mysql"))
    s.add(KnowledgeBase(id="ds-2", name="Contracts", org_id=ORG,
                        source_kind="document"))
    s.add(KnowledgeBase(id="ds-x", name="Foreign", org_id=OTHER,
                        source_kind="database"))
    s.commit()

    app = FastAPI()
    app.include_router(router)

    state = {"user": "u-admin"}

    def _get_db():
        yield s

    def _current_user():
        return s.get(User, state["user"])

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = _current_user
    c = TestClient(app)
    c.state_db = s
    c.state_user = state
    yield c
    s.close()
    eng.dispose()


def test_create_and_list_bindings(client):
    res = client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-1", "access_mode": "read_only"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_enabled"] is True
    # projection updated
    assert client.state_db.get(AgentApp, "app-1").knowledge_bases == ["ds-1"]
    lst = client.get("/agent-studio/app-1/bindings").json()
    assert [b["datasource_id"] for b in lst["data_bindings"]] == ["ds-1"]


def test_create_cross_org_datasource_rejected(client):
    res = client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-x"})
    assert res.status_code in (400, 403), res.text


def test_duplicate_create_returns_conflict_or_updates(client):
    client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-1"})
    res = client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-1", "access_mode": "read_write"})
    assert res.status_code == 200
    live = client.state_db.query(AgentDataBinding).filter_by(
        agent_app_id="app-1", datasource_id="ds-1", is_deleted=False).all()
    assert len(live) == 1 and live[0].access_mode == "read_write"


def test_patch_enable_toggle_and_delete_revokes(client):
    bid = client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-1"}).json()["id"]
    res = client.patch(f"/agent-studio/app-1/bindings/data/{bid}",
                       json={"is_enabled": False})
    assert res.status_code == 200
    assert client.state_db.get(AgentApp, "app-1").knowledge_bases == []
    res = client.delete(f"/agent-studio/app-1/bindings/data/{bid}")
    assert res.status_code == 200
    assert client.state_db.get(AgentDataBinding, bid).is_deleted is True


def test_patch_and_delete_cross_org_rejected(client):
    client.state_user["user"] = "u-other"
    client.state_db.add(AgentApp(id="app-f", name="F", org_id=OTHER,
                                 created_by_id="u-other"))
    client.state_db.commit()
    bid = client.post("/agent-studio/app-f/bindings/data", json={
        "datasource_id": "ds-x"}).json()["id"]
    client.state_user["user"] = "u-admin"
    res = client.patch(f"/agent-studio/app-f/bindings/data/{bid}",
                       json={"is_enabled": False})
    assert res.status_code in (400, 403)
    res = client.delete(f"/agent-studio/app-f/bindings/data/{bid}")
    assert res.status_code in (400, 403)
    assert client.state_db.get(AgentDataBinding, bid).is_deleted is False


def test_source_descriptors_contract(client):
    client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-1"})
    client.post("/agent-studio/app-1/bindings/data", json={
        "datasource_id": "ds-2"})
    res = client.get("/agent-studio/app-1/source-descriptors")
    assert res.status_code == 200
    items = res.json()["descriptors"]
    by_id = {d["id"]: d for d in items}
    assert by_id["ds-1"]["resource_type"] == "database"
    assert by_id["ds-2"]["resource_type"] == "knowledge_base"
    assert by_id["ds-1"]["binding_id"] and by_id["ds-1"]["is_enabled"] is True
    assert by_id["ds-1"]["name"] == "Sales DW"
    assert {"id", "name", "resource_type", "source_kind", "binding_id",
            "access_mode", "is_enabled", "readiness",
            "queryable"} <= set(by_id["ds-1"].keys())


def test_source_descriptors_cross_org_agent_hidden(client):
    client.state_user["user"] = "u-other"
    client.state_db.add(AgentApp(id="app-f", name="F", org_id=OTHER))
    client.state_db.commit()
    res = client.get("/agent-studio/app-f/source-descriptors")
    assert res.status_code in (200, 403)
    if res.status_code == 200:
        assert res.json()["descriptors"] == []
