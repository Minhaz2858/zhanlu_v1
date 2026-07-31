import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import uuid

import pytest

from app.database import Base, engine, SessionLocal
from app.models.dashboard import Dashboard
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.tool_handlers.dashboard_tools import _update_dashboard
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


def _user(db, uid=None):
    u = User(id=uid or str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
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


def _dashboard(db, user, kb):
    d = Dashboard(name="orig", datasource_kb_id=kb.id, project="global",
                  definition={"widgets": [{"id": "w1", "type": "kpi", "title": "A",
                  "sql": "SELECT 1 AS n", "options": {}}]},
                  refresh_interval_seconds=30, org_id="default-org", app_id="default-app",
                  created_by_id=user.id)
    db.add(d)
    db.commit()
    return d


def test_creator_updates_title_and_widgets(db):
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    args = {"dashboard_id": d.id, "title": "new", "widgets": [
        {"id": "w2", "type": "line", "title": "B", "sql": "SELECT 2 AS n", "options": {}}]}
    r = asyncio.run(_update_dashboard(args, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is True
    assert r["dashboard"]["name"] == "new"
    assert r["dashboard"]["definition"]["widgets"][0]["id"] == "w2"
    assert r["artifact"]["widget_count"] == 1


def test_non_creator_denied(db):
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    other = _user(db)
    r = asyncio.run(_update_dashboard({"dashboard_id": d.id, "title": "x"}, db, other.id, {"org_id": "default-org"}))
    assert r["success"] is False
    assert "creator" in r["error"].lower()


def test_invalid_widget_sql_rejected(db):
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    args = {"dashboard_id": d.id, "widgets": [
        {"id": "w", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}
    r = asyncio.run(_update_dashboard(args, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is False
    assert "invalid" in r["error"].lower() or "sql" in r["error"].lower()


def test_not_found(db):
    u = _user(db)
    r = asyncio.run(_update_dashboard({"dashboard_id": str(uuid.uuid4()), "title": "x"}, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is False


def test_refresh_interval_clamped(db):
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    r = asyncio.run(_update_dashboard({"dashboard_id": d.id, "refresh_interval_seconds": 5}, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is True
    assert r["dashboard"]["refresh_interval_seconds"] == 10


def test_partial_update_keeps_widgets(db):
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    # only title — widgets untouched
    r = asyncio.run(_update_dashboard({"dashboard_id": d.id, "title": "renamed"}, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is True
    assert r["dashboard"]["name"] == "renamed"
    assert r["dashboard"]["definition"]["widgets"][0]["id"] == "w1"  # unchanged
