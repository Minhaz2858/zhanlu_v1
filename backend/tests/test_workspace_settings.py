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
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
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


@pytest.fixture()
def client(db_session):
    """Provide a TestClient backed by the real zhanlu.db. The router
    under test reads/writes ``workspace_settings`` rows; the table is
    created in the real DB by the initial create_all sweep. We clean
    the rows we create in each test by calling ``db_session.query(...).
    delete()`` on teardown (the same session, different connection).
    """
    from app.deps import get_db as real_get_db
    from app.database import SessionLocal

    def _override():
        # Use a fresh SessionLocal session per request so the test
        # sees writes from the service layer immediately.
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[real_get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(real_get_db, None)
        # Clean up any rows this test created.
        try:
            db_session.query(WorkspaceSetting).delete()
            db_session.commit()
        except Exception:
            db_session.rollback()


def test_router_get_returns_default(client):
    """GET /api/workspace-settings returns the default flag values
    (auto_bind_all_datasources=False) when no row exists yet.
    """
    resp = client.get("/api/workspace-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"auto_bind_all_datasources": False}


def test_router_put_round_trip(client):
    """PUT flips the flag, GET reads it back."""
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
