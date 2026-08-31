"""T10 router tests: personal/company scope visibility for dashboard apps.

Covers:
- ``GET /app-records`` — company apps visible to the whole org; personal apps
  only to their creator.
- ``GET /app-records/{slug}`` — 404 for another user's personal app; 200 for
  a company app.
- The ``scope`` field is serialized on the record payload (drives the
  From Personal / From Company tabs in My Files).
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_dashboard_scope.db")
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


def _seed_dashboard(db, slug, org_id="default-org", created_by_id="u",
                    scope="personal"):
    kb = _seed_kb(db, org_id=org_id)
    rec = DashboardApp(
        slug=slug, name=f"Dashboard {slug}", description=None,
        datasource_kb_id=kb.id, spec={"name": slug}, status="running",
        refresh_interval_seconds=30, org_id=org_id,
        created_by_id=created_by_id, project_id=None, chat_thread_id=None,
        scope=scope,
    )
    db.add(rec)
    db.commit()
    return rec


def test_list_apps_shows_personal_only_to_creator(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "alice-secret", created_by_id=alice.id, scope="personal")
    # Bob lists the org's apps: Alice's personal dashboard must be absent.
    client = _make_client(db, bob)
    r = client.get("/api/dashboards/app-records")
    assert r.status_code == 200, r.text
    slugs = [x["slug"] for x in r.json()]
    assert "alice-secret" not in slugs


def test_list_apps_shows_personal_to_creator(db):
    alice = _seed_user(db)
    _seed_dashboard(db, "alice-own", created_by_id=alice.id, scope="personal")
    client = _make_client(db, alice)
    r = client.get("/api/dashboards/app-records")
    slugs = [x["slug"] for x in r.json()]
    assert "alice-own" in slugs


def test_list_apps_shows_company_to_whole_org(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "org-dashboard", created_by_id=alice.id, scope="company")
    client = _make_client(db, bob)
    r = client.get("/api/dashboards/app-records")
    slugs = [x["slug"] for x in r.json()]
    assert "org-dashboard" in slugs


def test_single_record_404_for_other_users_personal(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "alice-only", created_by_id=alice.id, scope="personal")
    client = _make_client(db, bob)
    r = client.get("/api/dashboards/app-records/alice-only")
    assert r.status_code == 404
    assert r.json()["detail"] == "Dashboard app not found"


def test_single_record_200_for_company(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "team-dash", created_by_id=alice.id, scope="company")
    client = _make_client(db, bob)
    r = client.get("/api/dashboards/app-records/team-dash")
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "company"


def test_single_record_200_for_creator_personal(db):
    alice = _seed_user(db)
    _seed_dashboard(db, "alice-own2", created_by_id=alice.id, scope="personal")
    client = _make_client(db, alice)
    r = client.get("/api/dashboards/app-records/alice-own2")
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "personal"


def test_single_record_by_uuid_200(db):
    """Regression: the frontend navigates to /dashboard/:id and passes the
    record UUID to /app-records/{id}. The endpoint previously filtered by
    slug only, so every UUID lookup 404'd and the dashboard page rendered
    blank ("Error: dashboards API 404")."""
    alice = _seed_user(db)
    rec = _seed_dashboard(db, "uuid-lookup", created_by_id=alice.id, scope="personal")
    client = _make_client(db, alice)
    r = client.get(f"/api/dashboards/app-records/{rec.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(rec.id)
    assert body["slug"] == "uuid-lookup"


def test_single_record_by_uuid_still_scoped(db):
    """UUID lookup must keep T10 scope: another user's personal app is 404
    even when resolved by id, not slug."""
    alice = _seed_user(db)
    bob = _seed_user(db)
    rec = _seed_dashboard(db, "uuid-scope", created_by_id=alice.id, scope="personal")
    client = _make_client(db, bob)
    r = client.get(f"/api/dashboards/app-records/{rec.id}")
    assert r.status_code == 404


def test_mark_viewed_404_for_other_users_personal(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "alice-view", created_by_id=alice.id, scope="personal")
    client = _make_client(db, bob)
    r = client.post("/api/dashboards/app-records/alice-view/mark-viewed")
    assert r.status_code == 404


def test_chat_thread_404_for_other_users_personal(db):
    alice = _seed_user(db)
    bob = _seed_user(db)
    _seed_dashboard(db, "alice-thread", created_by_id=alice.id, scope="personal")
    client = _make_client(db, bob)
    r = client.get("/api/dashboards/app-records/alice-thread/chat-thread")
    assert r.status_code == 404
