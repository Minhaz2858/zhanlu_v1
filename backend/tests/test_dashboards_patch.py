import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
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


def _client(db, user):
    app = FastAPI()
    app.include_router(dashboards_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def _user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _kb(db):
    kb = KnowledgeBase(name="db", source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    return kb


def _good():
    return {"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}}


def _make(db, client):
    r = client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": _kb(db).id,
                    "definition": {"widgets": [_good()]}})
    return r.json()["id"]


def test_creator_can_rename(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"name": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"


def test_creator_can_replace_definition(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    new_w = {"id": "w2", "type": "line", "title": "B", "sql": "SELECT 2 AS n", "options": {}}
    r = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": [new_w]}})
    assert r.status_code == 200
    assert r.json()["definition"]["widgets"][0]["id"] == "w2"


def test_patch_rejects_invalid_definition(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": []}})
    assert r.status_code == 400
    r2 = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": [
        {"id": "w", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}})
    assert r2.status_code == 400


def test_refresh_interval_clamped(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"refresh_interval_seconds": 5})
    assert r.json()["refresh_interval_seconds"] == 10
    r2 = c.patch(f"/api/dashboards/{did}", json={"refresh_interval_seconds": 999})
    assert r2.json()["refresh_interval_seconds"] == 300


def test_non_creator_forbidden(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other = _user(db)  # different user, same org
    c2 = _client(db, other)
    r = c2.patch(f"/api/dashboards/{did}", json={"name": "hacked"})
    assert r.status_code == 403


def test_wrong_org_404(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other = User(id=str(uuid.uuid4()), email="o@t.io", full_name="o", role="user",
                 password_hash="x", org_id="other-org", app_id="default-app")
    db.add(other); db.commit()
    c2 = _client(db, other)
    assert c2.patch(f"/api/dashboards/{did}", json={"name": "x"}).status_code == 404


def test_datasource_immutable(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other_kb = _kb(db)
    r = c.patch(f"/api/dashboards/{did}", json={"datasource_kb_id": other_kb.id})
    assert r.status_code == 200  # ignored (not in body schema), not an error
    assert r.json()["datasource_kb_id"] != other_kb.id  # unchanged


def test_can_edit_flag(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    # creator sees can_edit=True
    assert c.get(f"/api/dashboards/{did}").json()["can_edit"] is True
    # non-creator (same org) sees can_edit=False
    other = _user(db)
    c2 = _client(db, other)
    assert c2.get(f"/api/dashboards/{did}").json()["can_edit"] is False
