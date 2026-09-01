"""Model + table-creation tests for forecast_feedback, forecast_weight_adjustments,
and the run_id column on forecast_accuracy_log."""
from datetime import datetime
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog,
    ForecastFeedback, ForecastWeightAdjustment,
)

_NEEDED_TABLES = [
    ForecastTarget.__table__, ForecastRun.__table__,
    ForecastAccuracyLog.__table__, ForecastFeedback.__table__,
    ForecastWeightAdjustment.__table__,
]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)
    Base.metadata.create_all(engine, tables=_NEEDED_TABLES)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_forecast_feedback_roundtrip(db):
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.flush()
    fb = ForecastFeedback(
        target_id=target.id, product_id="异戊二烯", ai_price=10388.0,
        user_price=11000.0, reason="supply tightening", author_id="u1",
        author_name="analyst", target_date=datetime(2026, 8, 10), status="pending",
        org_id="default-org",
    )
    db.add(fb); db.commit()
    got = db.query(ForecastFeedback).first()
    assert got.status == "pending"
    assert got.beat is None
    assert got.user_price == 11000.0


def test_forecast_weight_adjustment_roundtrip(db):
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.flush()
    adj = ForecastWeightAdjustment(
        target_id=target.id, triggered_by="drift",
        reason="recent=18.3 vs baseline=12.1", old_weights={"ets": 0.4},
        new_weights={"ets": 0.3}, delta_ratio=None, applied=False,
        org_id="default-org",
    )
    db.add(adj); db.commit()
    got = db.query(ForecastWeightAdjustment).first()
    assert got.triggered_by == "drift"
    assert got.applied is False


def test_accuracy_log_run_id_nullable(db):
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.flush()
    log = ForecastAccuracyLog(
        target_id=target.id, horizon_days=7, mape=0.12, naive_mape=0.10,
        run_id="run-abc", realized_mape=0.15, org_id="default-org",
    )
    db.add(log); db.commit()
    got = db.query(ForecastAccuracyLog).first()
    assert got.run_id == "run-abc"
    assert got.realized_mape == 0.15
