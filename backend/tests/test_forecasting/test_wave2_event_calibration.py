"""T2.2 Event-Impact Calibration — event studies + data-backed overlay."""
import datetime
import os

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import ForecastEventImpact, ForecastTarget

_NEEDED = [
    ForecastEventImpact.__table__,
    ForecastTarget.__table__,
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
    for k in ("FORECAST_EVENT_CALIBRATION_ENABLED",):
        os.environ.pop(k, None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def price_history():
    """Fake price series: 14 days baseline + 7 days post-event = 21+ entries."""
    base_date = datetime.date(2026, 6, 17)  # 14 days before event
    prices = {}
    # 14-day baseline: stable around 10,000
    for i in range(14):
        d = base_date + datetime.timedelta(days=i)
        prices[d.isoformat()] = 10000.0 + i * 10
    # Event at day 14: price jumps
    event_day = base_date + datetime.timedelta(days=14)
    prices[event_day.isoformat()] = 10600.0  # +6% jump
    for i in range(1, 7):
        d = event_day + datetime.timedelta(days=i)
        prices[d.isoformat()] = 10600.0 + i * 5
    return prices


# ---------------------------------------------------------------------------
#  tests: event calibration core logic
# ---------------------------------------------------------------------------

def test_compute_price_impact_pct_over_event_window(price_history):
    """Event window cumulative return vs pre-event baseline."""
    from app.services.forecasting.ops.event_calibration import (
        _compute_event_impact,
    )

    event_date = datetime.date(2026, 7, 1)  # day 14 from base
    impact = _compute_event_impact(
        price_history=price_history,
        event_date=event_date,
        window_days=7,
        baseline_days=14,
    )
    # Price went from baseline_mean ~10,065 to post-event ~10,600+
    assert impact["price_impact_pct"] is not None
    assert impact["price_impact_pct"] > 2.0  # meaningful positive shift


def test_compute_price_impact_no_data_returns_none():
    """Empty price history returns None for impact."""
    from app.services.forecasting.ops.event_calibration import (
        _compute_event_impact,
    )

    impact = _compute_event_impact(
        price_history={},
        event_date=datetime.date(2026, 7, 11),
        window_days=7,
        baseline_days=10,
    )
    assert impact["price_impact_pct"] is None


# ---------------------------------------------------------------------------
#  tests: run_event_calibration()
# ---------------------------------------------------------------------------

def test_run_event_calibration_empty_writes_zero(db, monkeypatch):
    """No events to process → zero impacts written."""
    # Patch _get_closed_events to return empty
    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration._get_closed_events",
        lambda db, window_days: [],
    )

    from app.services.forecasting.ops.event_calibration import (
        run_event_calibration,
    )
    result = run_event_calibration(db, lookback_days=180, window_days=7)
    assert result["events_processed"] == 0
    assert result["impacts_written"] == 0


def test_run_event_calibration_writes_one_impact(db, monkeypatch, price_history):
    """One closed event → one ForecastEventImpact row written."""
    from datetime import date, datetime, timedelta

    event_date = date(2026, 7, 1)

    # Patch _get_closed_events
    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration._get_closed_events",
        lambda db, window_days: [
            {"id": 1, "event_type": "maintenance", "event_date": event_date,
             "headline": "Planned shutdown", "product_key": "异戊二烯",
             "direction": "up", "magnitude_estimate": "moderate"},
        ],
    )
    # Patch price history lookup
    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration._get_price_history_for_event",
        lambda db, product_key: price_history,
    )

    from app.services.forecasting.ops.event_calibration import (
        run_event_calibration,
    )
    result = run_event_calibration(db, lookback_days=180, window_days=7)
    assert result["events_processed"] == 1
    assert result["impacts_written"] == 1

    rows = db.query(ForecastEventImpact).all()
    assert len(rows) == 1
    assert rows[0].event_type == "maintenance"
    assert rows[0].product_id == "异戊二烯"
    assert rows[0].price_impact_pct is not None


def test_run_event_calibration_idempotent(db, monkeypatch, price_history):
    """Duplicate (product_id, event_type, event_date) is skipped."""
    from datetime import date, datetime, timedelta

    event_date = date(2026, 7, 1)

    # Pre-populate one row
    existing = ForecastEventImpact(
        product_id="异戊二烯",
        event_type="maintenance",
        event_date=event_date,
        price_impact_pct=5.0,
        source="manual",
        org_id="default-org",
    )
    db.add(existing)
    db.flush()

    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration._get_closed_events",
        lambda db, window_days: [
            {"id": 1, "event_type": "maintenance", "event_date": event_date,
             "headline": "Same event again", "product_key": "异戊二烯",
             "direction": "up", "magnitude_estimate": "moderate"},
        ],
    )
    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration._get_price_history_for_event",
        lambda db, product_key: price_history,
    )

    from app.services.forecasting.ops.event_calibration import (
        run_event_calibration,
    )
    result = run_event_calibration(db, lookback_days=180, window_days=7)
    assert result["events_processed"] == 1
    assert result["impacts_written"] == 0  # idempotent skip


# ---------------------------------------------------------------------------
#  tests: nightly step
# ---------------------------------------------------------------------------

def test_event_calibration_step_skips_when_disabled(db):
    """When flag OFF, return {skipped: True}."""
    os.environ["FORECAST_EVENT_CALIBRATION_ENABLED"] = "false"

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_event_calibration_step(db)
    assert result.get("skipped") is True


def test_event_calibration_step_runs_when_enabled(db, monkeypatch):
    """When flag ON, calls run_event_calibration."""
    os.environ["FORECAST_EVENT_CALIBRATION_ENABLED"] = "true"

    monkeypatch.setattr(
        "app.services.forecasting.ops.event_calibration.run_event_calibration",
        lambda db, lookback_days, window_days: {"events_processed": 0, "impacts_written": 0},
    )

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_event_calibration_step(db)
    assert result["events_processed"] == 0
