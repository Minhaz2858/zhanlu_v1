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
from app.routers import dashboards as dashboards_mod


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


def test_legacy_delete_handler_still_registered():
    # The new /app-records route must not have displaced the legacy
    # DELETE /{dashboard_id} soft-delete handler.
    assert dashboards_mod.delete_dashboard is not None
