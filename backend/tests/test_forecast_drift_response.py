"""Drift response: writes audit row on drift, no-op when stable."""
from datetime import datetime, timedelta, timezone
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastWeightAdjustment,
)
from app.services.forecasting.ops import drift_response

_NEEDED = [ForecastTarget.__table__, ForecastRun.__table__,
           ForecastAccuracyLog.__table__, ForecastWeightAdjustment.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _seed_realized(db, target, baseline_mapes, recent_mapes):
    """Insert ForecastAccuracyLog rows with realized_mape at given times."""
    now = datetime.now(timezone.utc)
    for m in baseline_mapes:
        db.add(ForecastAccuracyLog(
            target_id=target.id, horizon_days=7, realized_mape=m,
            evaluated_at=now - timedelta(days=60), org_id="default-org",
        ))
    for m in recent_mapes:
        db.add(ForecastAccuracyLog(
            target_id=target.id, horizon_days=7, realized_mape=m,
            evaluated_at=now - timedelta(days=10), org_id="default-org",
        ))
    db.commit()


def test_writes_audit_row_on_drift(db):
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.commit()
    # baseline ~12%, recent ~25% -> 25 > 12*1.2 -> drift
    _seed_realized(db, target, [0.12, 0.11, 0.12], [0.25, 0.26, 0.24])

    result = drift_response.check_drift_and_audit(db, target)
    assert result["is_drifting"] is True
    rows = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
    ).all()
    assert len(rows) == 1
    assert rows[0].applied is False
    assert "recent" in (rows[0].reason or "")


def test_no_audit_when_stable(db):
    target = ForecastTarget(product_key="苯乙烯", name="苯乙烯", org_id="default-org")
    db.add(target); db.commit()
    _seed_realized(db, target, [0.10, 0.10, 0.10], [0.11, 0.10, 0.10])

    result = drift_response.check_drift_and_audit(db, target)
    assert result["is_drifting"] is False
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
    ).count()
    assert n == 0


def test_no_duplicate_audit_within_7_days(db):
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.commit()
    _seed_realized(db, target, [0.12, 0.11, 0.12], [0.25, 0.26, 0.24])
    drift_response.check_drift_and_audit(db, target)
    drift_response.check_drift_and_audit(db, target)  # second call same window
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
    ).count()
    assert n == 1
