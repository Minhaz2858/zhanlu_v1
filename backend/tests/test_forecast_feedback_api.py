"""Feedback API: capture + list, gated by FORECAST_HITL_ENABLED."""
import os
from datetime import datetime
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastFeedback
from app.routers import market_dashboard as md_router

_NEEDED = [ForecastTarget.__table__, ForecastFeedback.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture(autouse=True)
def _reset_hitl_env():
    yield
    os.environ.pop("FORECAST_HITL_ENABLED", None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _fake_user():
    return SimpleNamespace(id="u1", name="analyst", email="a@b.c",
                           org_id="default-org", app_id="default-app")


def _client(db, hitl_on: bool, user=None):
    os.environ["FORECAST_HITL_ENABLED"] = "true" if hitl_on else "false"
    app = FastAPI()
    app.include_router(md_router.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: (user or _fake_user())
    return TestClient(app)


def test_post_feedback_creates_pending(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=True)
    r = c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "异戊二烯", "ai_price": 10388.0, "user_price": 11000.0,
        "reason": "tightening", "target_date": "2026-08-10T00:00:00",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["user_price"] == 11000.0


def test_post_feedback_disabled_when_flag_off(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=False)
    r = c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "异戊二烯", "ai_price": 100.0, "user_price": 110.0, "reason": "x",
    })
    assert r.status_code in (404, 501, 503)


def test_get_feedback_history(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=True)
    c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "异戊二烯", "ai_price": 100.0, "user_price": 109.0, "reason": "r",
    })
    r = c.get("/api/market-dashboard/forecast-feedback?product_id=异戊二烯")
    assert r.status_code == 200
    assert len(r.json()) == 1
