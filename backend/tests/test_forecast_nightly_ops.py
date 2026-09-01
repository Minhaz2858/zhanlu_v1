"""Nightly loop calls eval + drift after the compute loop, gated by flags."""
import os
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import ForecastTarget
from app.services import scheduled_tasks

_NEEDED = [ForecastTarget.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture(autouse=True)
def _reset_env():
    yield
    for k in ("NIGHTLY_FORECAST_ENABLED", "FORECAST_EVAL_JOB_ENABLED",
              "FORECAST_DRIFT_AUTO_ADJUST_ENABLED"):
        os.environ.pop(k, None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _patch_deps(monkeypatch):
    """Avoid MySQL: stub the engine class + seed functions at their source."""
    monkeypatch.setattr(
        "app.services.forecasting.engine.ForecastEngine",
        lambda d: type("E", (), {"compute_target_anchored": lambda self, tid: None})(),
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.seed_forecast_targets",
        lambda db: 0,
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.discover_and_seed_sku_targets",
        lambda db: 0,
    )


def test_nightly_runs_eval_and_drift_when_enabled(db, monkeypatch):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org",
                       status="active")
    db.add(t); db.commit()

    calls = {"eval": 0, "drift": 0}
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: calls.__setitem__("eval", calls["eval"] + 1) or {"runs_scored": 0},
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_drift_step",
        lambda db: calls.__setitem__("drift", calls["drift"] + 1) or {"checked": 0},
    )
    _patch_deps(monkeypatch)
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "true"
    os.environ["FORECAST_DRIFT_AUTO_ADJUST_ENABLED"] = "true"

    summary = scheduled_tasks._run_nightly_forecast_sync()
    assert calls["eval"] == 1
    assert calls["drift"] == 1
    assert "eval" in summary and "drift" in summary


def test_nightly_skips_eval_when_disabled(db, monkeypatch):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org",
                       status="active")
    db.add(t); db.commit()
    calls = {"eval": 0}
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: calls.__setitem__("eval", calls["eval"] + 1) or {"runs_scored": 0},
    )
    _patch_deps(monkeypatch)
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "false"
    scheduled_tasks._run_nightly_forecast_sync()
    assert calls["eval"] == 0
