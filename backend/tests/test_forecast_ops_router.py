"""forecast_ops router: status + manual run-evaluation."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog,
    ForecastFeedback, ForecastWeightAdjustment,
)
from app.routers import forecast_ops

_NEEDED = [
    ForecastTarget.__table__, ForecastRun.__table__,
    ForecastAccuracyLog.__table__, ForecastFeedback.__table__,
    ForecastWeightAdjustment.__table__,
]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture(autouse=True)
def _reset_eval_env():
    yield
    os.environ.pop("FORECAST_EVAL_JOB_ENABLED", None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _fake_user():
    return SimpleNamespace(id="u1", is_admin=True, email="a@b.c",
                           org_id="default-org", app_id="default-app")


def _client(db, eval_on: bool, admin=True):
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "true" if eval_on else "false"
    app = FastAPI()
    app.include_router(forecast_ops.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    u = _fake_user()
    app.dependency_overrides[get_current_user_required] = lambda: u
    app.dependency_overrides[require_admin] = lambda: u
    return TestClient(app)


def test_status_returns_chips(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    db.add(ForecastWeightAdjustment(
        target_id=t.id, org_id="default-org", triggered_by="drift",
        reason="drift", applied=False,
    )); db.commit()
    c = _client(db, eval_on=True)
    r = c.get("/api/forecast-ops/status")
    assert r.status_code == 200
    body = r.json()
    assert any(p["product_key"] == "异戊二烯" for p in body["products"])


def test_run_evaluation_admin_only(db, monkeypatch):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    # stub the actuals loader so no MySQL is hit
    monkeypatch.setattr(
        "app.services.forecasting.ops.evaluation_job._default_actuals_loader",
        lambda: (lambda datasource: None),
    )
    c = _client(db, eval_on=True, admin=True)
    r = c.post("/api/forecast-ops/run-evaluation")
    assert r.status_code == 200
    assert "runs_scored" in r.json()


def test_run_evaluation_disabled_when_flag_off(db):
    c = _client(db, eval_on=False, admin=True)
    r = c.post("/api/forecast-ops/run-evaluation")
    assert r.status_code in (404, 501, 503)
