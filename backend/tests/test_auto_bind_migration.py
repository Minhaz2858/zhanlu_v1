"""Phase 8 T8-11 — POST /workspace-settings/migrate-auto-bind.

Materializes the auto-bind effective KB set into explicit AgentDataBinding
rows for every agent in the (org, app), then stamps auto_bind_migrated_at —
after which the runtime stops widening scope (T8-5 gate). Idempotent:
a second run changes nothing. Org-admin only; org from the verified user.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.workspace_settings import WorkspaceSetting
from app.deps import get_current_user_required, get_db, require_org_admin
from app.routers.workspace_settings import router
from app.services import workspace_settings_service as wss

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
        KnowledgeBase.__table__, User.__table__,
        WorkspaceSetting.__table__])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u-admin", org_id=ORG, email="a@t", full_name="A",
               role="admin", password_hash="x"))
    s.add(User(id="u-mem", org_id=ORG, email="m@t", full_name="M",
               role="user", password_hash="x"))
    s.add(AgentApp(id="app-1", name="Analyst", org_id=ORG))
    s.add(AgentApp(id="app-2", name="Writer", org_id=ORG))
    s.add(KnowledgeBase(id="ds-1", name="DW", org_id=ORG, app_id="default-app",
                        source_kind="database"))
    s.add(KnowledgeBase(id="ds-2", name="Docs", org_id=ORG, app_id="default-app",
                        source_kind="file"))
    s.add(KnowledgeBase(id="ds-x", name="Foreign", org_id=OTHER,
                        app_id="default-app", source_kind="database"))
    s.add(wss_row := WorkspaceSetting(
        org_id=ORG, app_id="default-app",
        key=wss.KEY_AUTO_BIND_ALL_DATASOURCES, value="true"))
    s.commit()

    app = FastAPI()
    app.include_router(router)
    state = {"user": "u-admin"}

    def _get_db():
        yield s

    app.dependency_overrides[get_db] = _get_db
    # dynamic user; require_org_admin runs its REAL role check against it
    app.dependency_overrides[get_current_user_required] = lambda: s.get(
        User, state["user"])
    c = TestClient(app)
    c.s = s
    c.state = state
    yield c
    s.close()
    eng.dispose()


def test_migration_materializes_effective_set_and_stamps_state(client):
    res = client.post("/workspace-settings/migrate-auto-bind")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agents_materialized"] == 2
    assert body["bindings_created"] == 4  # 2 agents x 2 org KBs
    assert body["migrated_at"]
    live = client.s.query(AgentDataBinding).filter_by(
        is_deleted=False).all()
    assert len(live) == 4
    app1 = client.s.get(AgentApp, "app-1")
    assert sorted(app1.knowledge_bases) == ["ds-1", "ds-2"]
    # no cross-org leakage
    assert all(b.org_id != OTHER for b in live)
    assert not any(b.datasource_id == "ds-x" for b in live)


def test_migration_is_idempotent(client):
    first = client.post("/workspace-settings/migrate-auto-bind").json()
    second = client.post("/workspace-settings/migrate-auto-bind").json()
    assert second["bindings_created"] == 0
    assert second["agents_materialized"] == 2  # re-stamped / confirmed
    assert client.s.query(AgentDataBinding).filter_by(
        is_deleted=False).count() == 4


def test_migration_preserves_existing_manual_bindings(client):
    s = client.s
    from app.services.agent_binding_service import upsert_binding
    upsert_binding(s, ORG, "app-1", "ds-1", actor_id="u-admin",
                   access_mode="read_write")
    client.post("/workspace-settings/migrate-auto-bind")
    b = s.query(AgentDataBinding).filter_by(
        agent_app_id="app-1", datasource_id="ds-1",
        is_deleted=False).one()
    assert b.access_mode == "read_write"  # manual binding not clobbered


def test_migration_requires_org_admin(client):
    client.state["user"] = "u-mem"
    res = client.post("/workspace-settings/migrate-auto-bind")
    assert res.status_code in (401, 403)
