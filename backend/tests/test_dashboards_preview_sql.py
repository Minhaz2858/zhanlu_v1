import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import sqlite3
import tempfile
import os as _os
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


def _sqlite_kb(db):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE metrics (n INTEGER, label TEXT)")
    conn.execute("INSERT INTO metrics VALUES (1, 'a')")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(name="t", source_kind="database", db_type="sqlite",
                       api_url=f.name, org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    return kb, f.name


def _make(db, client, kb_id):
    r = client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": kb_id,
                    "definition": {"widgets": [{"id": "w1", "type": "kpi", "title": "A",
                    "sql": "SELECT 1 AS n", "options": {}}]}})
    return r.json()["id"]


def test_preview_returns_rows(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT n, label FROM metrics"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["n", "label"]
    assert body["rows"] == [{"n": 1, "label": "a"}]
    assert body["error"] is None
    _os.unlink(path)


def test_preview_rejects_non_readonly(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "DELETE FROM metrics"})
    assert r.status_code == 400
    _os.unlink(path)


def test_preview_rejects_multi_statement(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT 1; SELECT 2"})
    assert r.status_code == 400
    _os.unlink(path)


def test_preview_non_creator_forbidden(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    other = _user(db)
    c2 = _client(db, other)
    r = c2.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT 1"})
    assert r.status_code == 403
    _os.unlink(path)


def test_preview_db_error_is_200_with_error_field(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    # valid read-only SQL against a table that does not exist -> execution error
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT * FROM nope"})
    assert r.status_code == 200
    assert r.json()["error"]  # execution error reported, not 5xx
    _os.unlink(path)
