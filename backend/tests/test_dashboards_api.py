import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.routers.dashboards import router as dashboards_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_client(db, user, auth=True):
    app = FastAPI()
    app.include_router(dashboards_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    if auth:
        app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def _seed_user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_kb(db, name="sales_db"):
    kb = KnowledgeBase(name=name, source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    return kb


def _good_widget():
    return {"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}}


def test_create_and_get_dashboard(db):
    user = _seed_user(db)
    kb = _seed_kb(db)
    proj = Project(name="p1", org_id="default-org", app_id="default-app")
    db.add(proj)
    db.commit()
    client = _make_client(db, user)
    body = {"name": "Sales", "datasource_kb_id": kb.id, "project_id": proj.id,
            "definition": {"widgets": [_good_widget()]}, "refresh_interval_seconds": 45}
    r = client.post("/api/dashboards", json=body)
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    g = client.get(f"/api/dashboards/{did}")
    assert g.status_code == 200
    assert g.json()["name"] == "Sales"
    assert g.json()["refresh_interval_seconds"] == 45


def test_list_scoped_to_project(db):
    user = _seed_user(db)
    kb = _seed_kb(db)
    proj = Project(name="p2", org_id="default-org", app_id="default-app")
    db.add(proj)
    db.commit()
    client = _make_client(db, user)
    client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": kb.id,
                 "project_id": proj.id, "definition": {"widgets": [_good_widget()]}})
    r = client.get(f"/api/dashboards?project_id={proj.id}")
    assert r.status_code == 200
    assert any(d["name"] == "d" for d in r.json())


def test_create_rejects_non_readonly_sql(db):
    user = _seed_user(db)
    kb = _seed_kb(db)
    client = _make_client(db, user)
    body = {"name": "bad", "datasource_kb_id": kb.id,
            "definition": {"widgets": [{"id": "w1", "type": "kpi", "title": "A",
                             "sql": "DROP TABLE t", "options": {}}]}}
    r = client.post("/api/dashboards", json=body)
    assert r.status_code == 400


def test_unauth_rejected(db):
    client = _make_client(db, None, auth=False)
    assert client.get("/api/dashboards").status_code in (401, 403)


def test_delete(db):
    user = _seed_user(db)
    kb = _seed_kb(db)
    client = _make_client(db, user)
    r = client.post("/api/dashboards", json={"name": "del", "datasource_kb_id": kb.id,
                     "definition": {"widgets": [_good_widget()]}})
    did = r.json()["id"]
    assert client.delete(f"/api/dashboards/{did}").status_code in (200, 204)
    assert client.get(f"/api/dashboards/{did}").status_code == 404


def test_query_returns_per_widget_structure(db):
    user = _seed_user(db)
    kb = _seed_kb(db)
    client = _make_client(db, user)
    r = client.post("/api/dashboards", json={"name": "q", "datasource_kb_id": kb.id,
                     "definition": {"widgets": [_good_widget()]}})
    did = r.json()["id"]
    q = client.post(f"/api/dashboards/{did}/query")
    assert q.status_code == 200
    body = q.json()
    assert body["dashboard_id"] == did
    assert "w1" in body["results"]
    assert "error" in body["results"]["w1"]  # present whether or not KB connects
