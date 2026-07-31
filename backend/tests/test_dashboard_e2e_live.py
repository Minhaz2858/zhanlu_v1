import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import time
import uuid
import pytest

from app.database import Base, engine, SessionLocal
from app.models.dashboard import Dashboard
from app.models.user import User
import app.models  # noqa: F401
from app.services.tool_handlers.dashboard_tools import _create_dashboard
from app.services.dashboard_query import run_dashboard_query
from tests._dashboard_e2e_helpers import (
    require_live_or_skip, make_scratch_kb, NOW_WIDGET, VERSION_WIDGET, BAD_HOST,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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


@pytest.fixture
def user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _make_live_dashboard(db, user, cfg, host_override=None):
    kb = make_scratch_kb(db, cfg, org_id=user.org_id, host_override=host_override)
    res = _run(_create_dashboard(
        {"datasource_id": kb.id, "title": "E2E Live",
         "widgets": [NOW_WIDGET, VERSION_WIDGET], "refresh_interval_seconds": 10},
        db, user.id, context={"org_id": user.org_id}))
    assert res["success"] is True, res.get("error")
    return db.get(Dashboard, res["dashboard"]["id"])


def test_init_and_stable_connection(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg)
    assert dash.datasource_kb_id  # persisted, bound to the live KB
    out = _run(run_dashboard_query(db, dash))
    ts = out["results"]["w_ts"]
    assert ts["error"] is None and ts["rows"], ts
    ver = out["results"]["w_ver"]
    assert ver["error"] is None and ver["rows"] and ver["rows"][0].get("v"), ver


def test_polling_streams_fresh_data(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg)
    t1 = _run(run_dashboard_query(db, dash))["results"]["w_ts"]["rows"][0]["ts"]
    time.sleep(0.2)
    t2 = _run(run_dashboard_query(db, dash))["results"]["w_ts"]["rows"][0]["ts"]
    assert t1 != t2  # NOW(6) differs across polls => fresh live data each time


def test_connection_interruption_isolated_gracefully(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg, host_override=BAD_HOST)
    out = _run(run_dashboard_query(db, dash))
    # Structure intact even though the host is unreachable
    assert out["dashboard_id"] == dash.id and "refreshed_at" in out
    ts = out["results"]["w_ts"]
    assert ts["error"], "expected a per-widget error for unreachable host"
    assert ts["rows"] == []
