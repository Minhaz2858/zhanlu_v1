"""T5 router tests: chat-thread <-> dashboard-app bidirectional linking.

Covers the ``GET /app-records/{slug}/chat-thread`` endpoint that backs the
My Files "Open in chat" action: org scoping, 404 on missing slug, and 404
when the dashboard was built outside a chat (no thread bound).
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_dashboard_app_link.db")
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

# DashboardApp.spec is PostgreSQL JSONB; SQLite cannot compile it natively.
# Render it as a plain JSON/TEXT column so the sqlite test DB can create_all.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.dashboard_app import DashboardApp
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.routers.dashboards import router as dashboards_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


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


def _seed_user(db, org_id="default-org"):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id=org_id, app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_kb(db, org_id="default-org"):
    kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", source_kind="database",
                       db_type="sqlite", org_id=org_id, app_id="default-app")
    db.add(kb)
    db.commit()
    return kb


def _seed_dashboard(db, slug, org_id="default-org", chat_thread_id=None, created_by_id="u"):
    kb = _seed_kb(db, org_id=org_id)
    rec = DashboardApp(
        slug=slug, name=f"Dashboard {slug}", description=None,
        datasource_kb_id=kb.id, spec={"name": slug}, status="running",
        refresh_interval_seconds=30, org_id=org_id,
        created_by_id=created_by_id, project_id=None, chat_thread_id=chat_thread_id,
        scope="personal",
    )
    db.add(rec)
    db.commit()
    return rec


def test_chat_thread_returns_bound_thread(db):
    user = _seed_user(db)
    _seed_dashboard(db, "sales-dash", chat_thread_id="conv-sales", created_by_id=user.id)
    client = _make_client(db, user)
    r = client.get("/api/dashboards/app-records/sales-dash/chat-thread")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "sales-dash"
    assert body["chat_thread_id"] == "conv-sales"


def test_chat_thread_404_when_no_thread_bound(db):
    user = _seed_user(db)
    _seed_dashboard(db, "headless-dash", created_by_id=user.id)  # built outside a chat
    client = _make_client(db, user)
    r = client.get("/api/dashboards/app-records/headless-dash/chat-thread")
    assert r.status_code == 404
    assert "chat thread" in r.json()["detail"].lower()


def test_chat_thread_404_when_slug_missing(db):
    user = _seed_user(db)
    client = _make_client(db, user)
    r = client.get("/api/dashboards/app-records/ghost/chat-thread")
    assert r.status_code == 404
    assert r.json()["detail"] == "Dashboard app not found"


def test_chat_thread_scoped_to_org(db):
    user = _seed_user(db, org_id="org-a")
    # Dashboard owned by another org must not leak.
    _seed_dashboard(db, "other-org-dash", org_id="org-b", chat_thread_id="conv-secret")
    client = _make_client(db, user)
    r = client.get("/api/dashboards/app-records/other-org-dash/chat-thread")
    assert r.status_code == 404


def test_chat_thread_requires_auth(db):
    _seed_dashboard(db, "auth-dash", chat_thread_id="conv-auth")
    client = _make_client(db, None, auth=False)
    r = client.get("/api/dashboards/app-records/auth-dash/chat-thread")
    assert r.status_code in (401, 403)


def test_app_record_serializes_chat_thread_id(db):
    """The My Files list needs chat_thread_id on the record payload itself."""
    user = _seed_user(db)
    _seed_dashboard(db, "linked-dash", chat_thread_id="conv-link", created_by_id=user.id)
    client = _make_client(db, user)
    r = client.get("/api/dashboards/app-records/linked-dash")
    assert r.status_code == 200, r.text
    assert r.json()["chat_thread_id"] == "conv-link"
