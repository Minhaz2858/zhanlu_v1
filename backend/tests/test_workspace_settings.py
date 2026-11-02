"""Tests for the workspace_settings_service + the /api/workspace-settings
router. These are pure backend tests — no frontend, no LLM, no live DB.

The service is the source of truth for the ``auto_bind_all_datasources``
flag; the router is a thin typed wrapper around it. We test both.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Use the package-level main re-export pattern. The root main.py is a
# module; import it via importlib so the test doesn't need to be run
# from the repo root.
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "backend_main",
    os.path.join(os.path.dirname(__file__), "..", "main.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
app = _mod.app
from app.models.workspace_settings import WorkspaceSetting
from app.services.workspace_settings_service import (
    KEY_AUTO_BIND_ALL_DATASOURCES,
    get_bool,
    get_str,
    set_value,
    clear_cache,
)


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[WorkspaceSetting.__table__])
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    clear_cache()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        clear_cache()


def test_get_bool_returns_default_when_no_row(db_session):
    """The default value for the flag is False (DATA-CORE-3)."""
    assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES) is False


def test_set_value_then_get_bool_round_trip(db_session):
    """Set true, get true. Set false, get false. The contract is symmetric."""
    set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, "true",
              org_id="o1", app_id="a1")
    db_session.commit()
    assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES,
                    org_id="o1", app_id="a1") is True

    set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, "false",
              org_id="o1", app_id="a1")
    db_session.commit()
    assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES,
                    org_id="o1", app_id="a1") is False


def test_falsy_string_variants(db_session):
    """``"0"``, ``"no"``, ``"off"``, ``""`` all read as False."""
    for falsy in ("0", "no", "off", "", "False", "FALSE"):
        set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, falsy,
                  org_id="o1", app_id="a1")
        db_session.commit()
        assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES,
                        org_id="o1", app_id="a1") is False, f"value={falsy!r}"


def test_get_str_returns_none_when_not_set(db_session):
    """A non-existent key returns None (not empty string)."""
    assert get_str(db_session, "totally_made_up_key") is None


def test_scopes_are_isolated(db_session):
    """Setting a value in scope (o1, a1) does not leak into (o2, a2)."""
    set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, "true",
              org_id="o1", app_id="a1")
    db_session.commit()
    assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES,
                    org_id="o1", app_id="a1") is True
    # Different scope: should be default.
    assert get_bool(db_session, KEY_AUTO_BIND_ALL_DATASOURCES,
                    org_id="o2", app_id="a2") is False


def test_set_value_upserts(db_session):
    """Calling set_value twice on the same (org, app, key) updates the
    existing row in place rather than creating a duplicate.
    """
    set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, "true",
              org_id="o1", app_id="a1")
    db_session.commit()
    set_value(db_session, KEY_AUTO_BIND_ALL_DATASOURCES, "false",
              org_id="o1", app_id="a1")
    db_session.commit()

    rows = (
        db_session.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.org_id == "o1",
            WorkspaceSetting.app_id == "a1",
            WorkspaceSetting.key == KEY_AUTO_BIND_ALL_DATASOURCES,
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].value == "false"


# ---------------------------------------------------------------------------
# Router-level tests (integration)
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, org_id="org-acme", role="user"):
        self.id = "u-test"
        self.org_id = org_id
        self.role = role
        self.email = "tester@test"
        self.full_name = "Tester"
        self.is_anonymous = False


@pytest.fixture()
def client(db_session):
    """TestClient with the router's DB session AND auth overridden.

    Phase 7C (B2): the org is resolved from the VERIFIED user, and writes
    require an org admin. ``state['role']`` toggles the authenticated
    principal between org member and org admin.
    """
    from app.deps import get_current_user_required, get_db

    state = {"role": "admin"}
    db_session.add.__self__  # keep linters honest about the session use

    def _get_db():
        yield db_session

    def _user():
        return _FakeUser(role=state["role"])

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = _user
    try:
        with TestClient(app) as c:
            c.state = state
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_required, None)
        try:
            db_session.query(WorkspaceSetting).delete()
            db_session.commit()
        except Exception:
            db_session.rollback()


def test_router_get_returns_default(client):
    """GET /api/workspace-settings returns the default flag values
    (auto_bind_all_datasources=False) when no row exists yet — resolved
    under the verified user's org.
    """
    resp = client.get("/api/workspace-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"auto_bind_all_datasources": False}


def test_router_put_round_trip(client):
    """PUT flips the flag (org admin), GET reads it back."""
    resp = client.put(
        "/api/workspace-settings",
        json={"auto_bind_all_datasources": True},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_bind_all_datasources"] is True

    resp = client.get("/api/workspace-settings")
    assert resp.json()["auto_bind_all_datasources"] is True

    resp = client.put(
        "/api/workspace-settings",
        json={"auto_bind_all_datasources": False},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_bind_all_datasources"] is False


def test_router_rejects_cross_org_request(client):
    """Phase 7C (B2): a client-supplied org_id that differs from the
    verified user's org is refused — no tenant may read or flip another
    tenant's workspace settings."""
    resp = client.get("/api/workspace-settings",
                      params={"org_id": "org-evil"})
    assert resp.status_code == 403
    resp = client.put("/api/workspace-settings",
                      params={"org_id": "org-evil"},
                      json={"auto_bind_all_datasources": True})
    assert resp.status_code == 403


def test_router_put_requires_org_admin(client):
    """Phase 7C (B2): a plain org member can READ but not WRITE the
    workspace flag (org-wide blast radius)."""
    client.state["role"] = "user"
    resp = client.put("/api/workspace-settings",
                      json={"auto_bind_all_datasources": True})
    assert resp.status_code == 403
    # reads stay available to org members
    assert client.get("/api/workspace-settings").status_code == 200
