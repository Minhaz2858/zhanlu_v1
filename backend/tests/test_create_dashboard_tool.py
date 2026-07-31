import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import uuid
import pytest

from app.database import Base, engine, SessionLocal
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
import app.models  # noqa: F401
from app.services.tool_handlers.dashboard_tools import _create_dashboard


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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _setup(db):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
                full_name="t", role="user", password_hash="x",
                org_id="default-org", app_id="default-app")
    db.add(user)
    proj = Project(name="sales-proj", org_id="default-org", app_id="default-app")
    db.add(proj)
    kb = KnowledgeBase(name="sales_db", source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app", project_id=None)
    db.add(kb)
    db.commit()
    # bind kb to project via FK now that proj has an id
    kb.project_id = proj.id
    db.commit()
    return user, proj, kb


def test_create_dashboard_success(db):
    user, proj, kb = _setup(db)
    args = {
        "datasource_id": kb.id,
        "title": "Revenue",
        "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS n", "options": {}}],
        "refresh_interval_seconds": 20,
    }
    res = _run(_create_dashboard(args, db, user.id,
               context={"project_id": proj.id, "org_id": user.org_id}))
    assert res["success"] is True
    assert res["artifact"]["source"] == "dashboard"
    assert res["artifact"]["widget_count"] == 1
    assert res["dashboard"]["datasource_kb_id"] == kb.id


def test_create_dashboard_rejects_ddl(db):
    user, proj, kb = _setup(db)
    args = {"datasource_id": kb.id, "title": "bad",
            "widgets": [{"id": "w1", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}
    res = _run(_create_dashboard(args, db, user.id,
               context={"project_id": proj.id, "org_id": user.org_id}))
    assert res["success"] is False
    assert "read-only" in res.get("error", "").lower() or "select" in res.get("error", "").lower()


def test_create_dashboard_rejects_foreign_datasource(db):
    user, proj, kb = _setup(db)
    other = KnowledgeBase(name="other", source_kind="database", db_type="sqlite",
                          org_id="other-org", app_id="default-app")
    db.add(other)
    db.commit()
    args = {"datasource_id": other.id, "title": "x",
            "widgets": [{"id": "w1", "type": "kpi", "title": "x", "sql": "SELECT 1", "options": {}}]}
    res = _run(_create_dashboard(args, db, user.id,
               context={"project_id": proj.id, "org_id": user.org_id}))
    assert res["success"] is False  # cross-org rejected (no IDOR)
