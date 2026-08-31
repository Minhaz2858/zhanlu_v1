"""T2.1 Decision-ROI loop closure: engine logging + scoring cron + nightly step."""
import datetime
import os

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastDecisionLog,
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
)

_NEEDED = [
    ForecastDecisionLog.__table__,
    ForecastTarget.__table__,
    ForecastRun.__table__,
    ForecastAccuracyLog.__table__,
]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture(autouse=True)
def _reset_env():
    yield
    for k in ("FORECAST_DECISION_LOGGING_ENABLED",
              "FORECAST_EVAL_JOB_ENABLED",
              "FORECAST_DRIFT_AUTO_ADJUST_ENABLED"):
        os.environ.pop(k, None)


@pytest.fixture
def db():
    s = SessionLocal()
    # ensure session.info has organization_id so log_decision() works
    s.info["organization_id"] = "default-org"
    yield s
    s.rollback()
    s.close()


# ---------------------------------------------------------------------------
#  tests: decision_logger integration — log_decision with actual_price_t
# ---------------------------------------------------------------------------

def test_log_decision_writes_row_with_actual_price_t(db):
    """log_decision() accepts actual_price_t and writes it to the DB row."""
    from app.services.forecasting.features.decision_logger import log_decision

    log_decision(
        session=db,
        product_id="异戊二烯",
        horizon_day=7,
        as_of_date=datetime.date(2026, 7, 30),
        action="buy",
        confidence="high",
        rationale="test rationale",
        forecast_run_id=None,
        predicted_p_rise=0.85,
        predicted_change_pct=0.065,
        decision_thresholds={"buy": 0.70, "sell": 0.30},
        actual_price_t=12345.67,
    )
    db.flush()

    rows = db.query(ForecastDecisionLog).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.product_id == "异戊二烯"
    assert r.horizon_day == 7
    assert r.action == "buy"
    assert r.confidence == "high"
    assert r.actual_price_t == 12345.67
    assert r.predicted_p_rise == 0.85
    assert r.predicted_change_pct == 0.065


def test_log_decision_actual_price_t_is_not_none(db):
    """actual_price_t must be written, otherwise get_pending_unrealized
    filters it out (it requires actual_price_t IS NOT NULL)."""
    from app.services.forecasting.features.decision_logger import (
        log_decision,
        get_pending_unrealized,
    )

    log_decision(
        session=db,
        product_id="异戊二烯",
        horizon_day=3,
        as_of_date=datetime.date(2026, 8, 1),
        action="sell",
        confidence="medium",
        rationale="declining trend",
        forecast_run_id=None,
        predicted_p_rise=0.20,
        predicted_change_pct=-0.04,
        decision_thresholds={"buy": 0.70, "sell": 0.30},
        actual_price_t=9800.0,
    )
    db.flush()

    pending = get_pending_unrealized(
        db, cutoff_date=datetime.date(2026, 8, 10), product_id=None
    )
    assert len(pending) == 1
    assert pending[0].actual_price_t == 9800.0


def test_log_decision_without_actual_price_t_not_pending(db):
    """If actual_price_t is NULL, get_pending_unrealized returns empty.
    Confirms the Wave 0 gap: without actual_price_t, the ROI loop stalls."""
    from app.services.forecasting.features.decision_logger import (
        log_decision,
        get_pending_unrealized,
    )

    # Use the log_decision() helper BUT pass actual_price_t=None
    # (must still reach the db row with NULL)
    log_decision(
        session=db,
        product_id="异戊二烯",
        horizon_day=7,
        as_of_date=datetime.date(2026, 7, 30),
        action="buy",
        confidence="high",
        rationale="old style",
        predicted_p_rise=0.85,
        predicted_change_pct=0.06,
        actual_price_t=None,
    )
    db.flush()

    pending = get_pending_unrealized(
        db, cutoff_date=datetime.date(2026, 8, 10), product_id=None
    )
    assert len(pending) == 0


# ---------------------------------------------------------------------------
#  tests: run_decision_scoring + _backfill_actual_price_t
# ---------------------------------------------------------------------------

def test_run_decision_scoring_no_pending_returns_empty(db, monkeypatch):
    """When no pending logs exist, run_decision_scoring returns zero counts."""
    monkeypatch.setattr(
        "app.services.forecasting.ops.decision_loop._resolve_mysql_engine",
        lambda: None,
    )

    from app.services.forecasting.ops.decision_loop import run_decision_scoring
    result = run_decision_scoring(db)
    # When the wrapper can't resolve MySQL, score_pending_decisions returns
    # scored_count=0 + errors list
    assert result["scored_count"] == 0


def test_run_decision_scoring_skips_when_no_mysql(db, monkeypatch):
    """When EDIA MySQL is unavailable AND there are pending logs,
    the result includes error note and zero scored."""
    monkeypatch.setattr(
        "app.services.forecasting.ops.decision_loop._resolve_mysql_engine",
        lambda: None,
    )

    from app.services.forecasting.features.decision_logger import log_decision
    log_decision(
        session=db,
        product_id="异戊二烯",
        horizon_day=7,
        as_of_date=datetime.date(2026, 7, 30),
        action="buy",
        confidence="high",
        rationale="test",
        predicted_p_rise=0.85,
        predicted_change_pct=0.06,
        actual_price_t=12345.0,
    )
    db.flush()

    from app.services.forecasting.ops.decision_loop import run_decision_scoring
    result = run_decision_scoring(db)
    assert result["scored_count"] == 0


# ---------------------------------------------------------------------------
#  tests: _backfill_actual_price_t
# ---------------------------------------------------------------------------

def test_backfill_actual_price_t_fills_null_price(db, monkeypatch):
    """_backfill_actual_price_t queries MySQL and fills actual_price_t
    for logs where it is NULL."""
    from app.services.forecasting.features.decision_logger import log_decision

    log_decision(
        session=db,
        product_id="异戊二烯",
        horizon_day=7,
        as_of_date=datetime.date(2026, 7, 30),
        action="buy",
        confidence="high",
        rationale="needs backfill",
        predicted_p_rise=0.80,
        predicted_change_pct=0.05,
        actual_price_t=None,
    )
    db.flush()

    # Mock MySQL engine returning a price
    class FakeRow:
        def __init__(self, vals):
            self._vals = vals
        def __getitem__(self, idx):
            return self._vals[idx]

    class FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def execute(self, stmt, params=None):
            return self
        def fetchone(self):
            return FakeRow(["2026-07-30", 10200.0])
        def close(self):
            pass

    class FakeEngine:
        def connect(self):
            return FakeConn()

    monkeypatch.setattr(
        "app.services.forecasting.ops.decision_loop._resolve_mysql_engine",
        lambda: FakeEngine(),
    )

    from app.services.forecasting.ops.decision_loop import _backfill_actual_price_t
    count = _backfill_actual_price_t(db)
    assert count >= 1

    # Re-read — should now have actual_price_t
    updated = (
        db.query(ForecastDecisionLog)
        .filter(ForecastDecisionLog.product_id == "异戊二烯")
        .first()
    )
    assert updated is not None
    assert updated.actual_price_t == 10200.0


# ---------------------------------------------------------------------------
#  tests: nightly step — _run_decision_scoring_step
# ---------------------------------------------------------------------------

def test_decision_scoring_step_runs_when_enabled(db, monkeypatch):
    """When flag ON, the nightly step calls run_decision_scoring."""
    os.environ["FORECAST_DECISION_LOGGING_ENABLED"] = "true"

    calls = {"scoring": 0}
    monkeypatch.setattr(
        "app.services.forecasting.ops.decision_loop.run_decision_scoring",
        lambda db: calls.__setitem__("scoring", calls["scoring"] + 1)
        or {"scored_count": 0},
    )

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_decision_scoring_step(db)
    assert calls["scoring"] == 1
    assert result["scored_count"] == 0


def test_decision_scoring_step_skips_when_disabled(db, monkeypatch):
    """When flag OFF, the nightly step returns {skipped: True}."""
    os.environ["FORECAST_DECISION_LOGGING_ENABLED"] = "false"

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_decision_scoring_step(db)
    # When the function doesn't exist yet, this will raise AttributeError
    assert result.get("skipped") is True


def test_decision_scoring_step_in_nightly_sync(db, monkeypatch):
    """Verify _run_decision_scoring_step is called in nightly sync when ON."""
    t = ForecastTarget(
        product_key="异戊二烯", name="异戊二烯", org_id="default-org",
        status="active",
    )
    db.add(t)
    db.commit()

    calls = {"decision": 0}

    monkeypatch.setattr(
        "app.services.forecasting.engine.ForecastEngine",
        lambda d: type("E", (), {
            "compute_target_anchored": lambda self, tid: None,
        })(),
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.seed_forecast_targets",
        lambda db: 0,
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.discover_and_seed_sku_targets",
        lambda db: 0,
    )
    # Avoid colliding with real cron env — patch steps inside scheduled_tasks
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_decision_scoring_step",
        lambda db: calls.__setitem__("decision", calls["decision"] + 1)
        or {"scored_count": 0},
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: {"skipped": True},
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_drift_step",
        lambda db: {"skipped": True},
    )

    os.environ["FORECAST_DECISION_LOGGING_ENABLED"] = "true"
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "false"
    os.environ["FORECAST_DRIFT_AUTO_ADJUST_ENABLED"] = "false"

    from app.services import scheduled_tasks
    summary = scheduled_tasks._run_nightly_forecast_sync()
    assert calls["decision"] == 1
    assert "decision_scoring" in summary


def test_decision_scoring_step_skipped_in_nightly_when_disabled(db, monkeypatch):
    """When flag OFF, nightly sync does NOT call decision scoring."""
    t = ForecastTarget(
        product_key="异戊二烯", name="异戊二烯", org_id="default-org",
        status="active",
    )
    db.add(t)
    db.commit()

    calls = {"decision": 0}

    monkeypatch.setattr(
        "app.services.forecasting.engine.ForecastEngine",
        lambda d: type("E", (), {
            "compute_target_anchored": lambda self, tid: None,
        })(),
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.seed_forecast_targets",
        lambda db: 0,
    )
    monkeypatch.setattr(
        "app.services.forecasting.seed_targets.discover_and_seed_sku_targets",
        lambda db: 0,
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_decision_scoring_step",
        lambda db: calls.__setitem__("decision", calls["decision"] + 1)
        or {"scored_count": 0},
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: {"skipped": True},
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_drift_step",
        lambda db: {"skipped": True},
    )

    os.environ["FORECAST_DECISION_LOGGING_ENABLED"] = "false"
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "false"
    os.environ["FORECAST_DRIFT_AUTO_ADJUST_ENABLED"] = "false"

    from app.services import scheduled_tasks
    scheduled_tasks._run_nightly_forecast_sync()
    assert calls["decision"] == 0
