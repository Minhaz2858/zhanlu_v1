"""Router tests: hard-delete endpoint for full-stack dashboard apps.

Covers DELETE /api/dashboards/app-records/{slug_or_id}: creator-only personal
scope, company scope (admin gate for non-creators), org wall, unknown slug
404, cascade of bound conversations, and that the legacy ``/{dashboard_id}``
delete handler is untouched.

The handler is called DIRECTLY (plain Python function, no TestClient) with
``db=`` and ``user=`` keyword args — FastAPI's Depends() defaults only resolve
when the framework invokes the function, so this exercises the exact logic.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import Base, get_db  # noqa: F401  (get_db for pattern parity)
import app.models  # noqa: F401  register all models
from app.models.dashboard_app import DashboardApp
from app.models.agent_conversation import AgentConversation
from app.routers import dashboards as dashboards_mod
from app.services.dashboard_app.cascade import delete_bound_conversations


# Isolated in-memory SQLite engine for this test module (same pattern as
# test_dashboards_catch_all.py): StaticPool so every session shares one
# connection and sees the same tables.
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _mk_record(db, *, slug, org_id="org-1", created_by_id="user-1", scope="personal"):
    rec = DashboardApp(
        id=str(uuid.uuid4()),
        slug=slug,
        name=slug,
        description="test app",
        datasource_kb_id="kb1",
        app_id="default-app",
        spec={},
        status="running",
        org_id=org_id,
        created_by_id=created_by_id,
        scope=scope,
    )
    db.add(rec)
    db.commit()
    return rec


def _mk_user(user_id="user-1", org_id="org-1", role="user"):
    u = MagicMock()
    u.id = user_id
    u.org_id = org_id
    u.role = role
    return u


def test_creator_deletes_personal_app_204_and_row_gone(db):
    rec = _mk_record(db, slug="my-app", created_by_id="user-1", scope="personal")
    user = _mk_user("user-1")

    result = dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert result is None
    assert db.query(DashboardApp).filter(DashboardApp.id == rec.id).count() == 0


def test_non_creator_personal_404_row_intact(db):
    rec = _mk_record(db, slug="private-app", created_by_id="user-1", scope="personal")
    user = _mk_user("user-2")

    with pytest.raises(HTTPException) as ei:
        dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert ei.value.status_code == 404
    assert db.query(DashboardApp).filter(DashboardApp.id == rec.id).count() == 1


def test_company_app_creator_deletes_204(db):
    rec = _mk_record(db, slug="company-app", created_by_id="user-1", scope="company")
    user = _mk_user("user-1")

    result = dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert result is None
    assert db.query(DashboardApp).count() == 0


def test_company_app_admin_can_delete_204(db):
    rec = _mk_record(db, slug="company-app", created_by_id="user-1", scope="company")
    admin = _mk_user("user-2", role="admin")

    result = dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=admin)

    assert result is None
    assert db.query(DashboardApp).count() == 0


def test_company_app_non_admin_404_row_intact(db):
    rec = _mk_record(db, slug="company-app", created_by_id="user-1", scope="company")
    user = _mk_user("user-2", role="user")

    with pytest.raises(HTTPException) as ei:
        dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert ei.value.status_code == 404
    assert db.query(DashboardApp).filter(DashboardApp.id == rec.id).count() == 1


def test_unknown_slug_404(db):
    user = _mk_user("user-1")

    with pytest.raises(HTTPException) as ei:
        dashboards_mod.delete_dashboard_app_record("no-such-app", db=db, user=user)

    assert ei.value.status_code == 404


def test_cascade_delete_bound_conversations_called(db, monkeypatch):
    rec = _mk_record(db, slug="cascade-app", created_by_id="user-1", scope="personal")
    user = _mk_user("user-1")

    calls = {}

    def _fake_cascade(session, slug, dash_id):
        calls["slug"] = slug
        calls["dash_id"] = dash_id
        return 1

    monkeypatch.setattr(
        "app.services.dashboard_app.cascade.delete_bound_conversations",
        _fake_cascade,
    )

    result = dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert result is None
    assert calls.get("slug") == "cascade-app"
    assert calls.get("dash_id") == rec.id
    assert db.query(DashboardApp).count() == 0


def test_legacy_and_app_records_delete_routes_registered():
    # Both DELETE routes must be registered: the new /app-records hard-delete
    # AND the legacy /{dashboard_id} soft-delete handler (the new route must
    # not have displaced the old one).
    delete_paths = {
        r.path
        for r in dashboards_mod.router.routes
        if "DELETE" in (getattr(r, "methods", None) or set())
    }
    assert "/dashboards/app-records/{slug_or_id}" in delete_paths
    assert "/dashboards/{dashboard_id}" in delete_paths


def test_cross_org_404_row_intact(db):
    # Org wall: a record in another org is invisible — 404, row untouched.
    rec = _mk_record(db, slug="other-org-app", org_id="org-2", created_by_id="user-9")
    user = _mk_user("user-1", org_id="org-1")

    with pytest.raises(HTTPException) as ei:
        dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert ei.value.status_code == 404
    assert db.query(DashboardApp).filter(DashboardApp.id == rec.id).count() == 1


def test_delete_by_slug_as_creator_204(db):
    # Positive slug resolution: the handler accepts the SLUG (not just the id).
    rec = _mk_record(db, slug="slug-resolved-app", created_by_id="user-1", scope="personal")
    user = _mk_user("user-1")

    result = dashboards_mod.delete_dashboard_app_record(rec.slug, db=db, user=user)

    assert result is None
    assert db.query(DashboardApp).count() == 0


def test_company_app_missing_role_attribute_404(db):
    # getattr(user, "role", "user") default branch: a user object WITHOUT a
    # ``role`` attribute must fail closed (404), not 500. spec=[...] makes
    # attribute access raise AttributeError for unset attrs, so the getattr
    # default is genuinely exercised (a bare MagicMock would auto-create
    # user.role and never reach the default).
    rec = _mk_record(db, slug="company-app", created_by_id="user-1", scope="company")
    user = MagicMock(spec=["id", "org_id"])
    user.id = "user-2"
    user.org_id = "org-1"

    with pytest.raises(HTTPException) as ei:
        dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert ei.value.status_code == 404
    assert db.query(DashboardApp).filter(DashboardApp.id == rec.id).count() == 1


def test_delete_survives_poller_and_rmtree_failures(db, monkeypatch):
    # Best-effort artifact cleanup: a raising stop_poller and a missing app
    # dir must NOT crash the delete — 204 and the row still goes away.
    rec = _mk_record(db, slug="resilient-app", created_by_id="user-1", scope="personal")
    user = _mk_user("user-1")

    def _boom(slug):
        raise RuntimeError("poller exploded")

    monkeypatch.setattr(dashboards_mod.dashboard_app_manager, "stop_poller", _boom)

    class _FakeGen:
        def app_dir(self, slug):
            return Path("/nonexistent/app/dir")  # does not exist → rmtree skipped

    monkeypatch.setattr(
        "app.services.dashboard_app.generator.get_generator",
        lambda: _FakeGen(),
    )

    result = dashboards_mod.delete_dashboard_app_record(rec.id, db=db, user=user)

    assert result is None
    assert db.query(DashboardApp).count() == 0


def test_cascade_dedupes_slug_and_id_match(db):
    # A conversation matching BOTH slug and id is deleted exactly once —
    # delete_bound_conversations must return 1, not 2.
    rec = _mk_record(db, slug="dedupe-app", created_by_id="user-1", scope="personal")
    conv = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name="dashboard_builder",
        title="dedupe chat",
        status="active",
        metadata_={
            "mode": "dashboard",
            "dashboard_slug": rec.slug,
            "dashboard_id": rec.id,
        },
    )
    db.add(conv)
    db.commit()

    count = delete_bound_conversations(db, rec.slug, rec.id)

    assert count == 1
    assert db.query(AgentConversation).count() == 0
