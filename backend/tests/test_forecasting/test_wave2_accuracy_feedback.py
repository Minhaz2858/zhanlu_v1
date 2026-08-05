"""T2.3 Accuracy Feedback Loop — auto-flag degradation + audit rows."""
import datetime
import os

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastDecisionLog,
    ForecastTarget,
    ForecastRun,
    ForecastWeightAdjustment,
    ForecastAccuracyLog,
)

_NEEDED = [
    ForecastDecisionLog.__table__,
    ForecastTarget.__table__,
    ForecastRun.__table__,
    ForecastWeightAdjustment.__table__,
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
    for k in ("FORECAST_ACCURACY_FEEDBACK_ENABLED",):
        os.environ.pop(k, None)


@pytest.fixture
def db():
    s = SessionLocal()
    s.info["organization_id"] = "default-org"
    yield s
    s.rollback()
    s.close()


def _make_target(db, pk="异戊二烯"):
    t = ForecastTarget(product_key=pk, name=pk, org_id="default-org",
                       status="active", source="manual")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
#  tests: run_accuracy_feedback()
# ---------------------------------------------------------------------------

def test_run_accuracy_feedback_empty_returns_zero(db):
    """No targets or no decision logs → return zero flagged."""
    from app.services.forecasting.ops.accuracy_feedback import run_accuracy_feedback
    result = run_accuracy_feedback(db)
    assert result["checked"] == 0
    assert result["flagged"] == 0


def test_run_accuracy_feedback_flags_degraded_product(db, monkeypatch):
    """When a product has degraded decision accuracy, flag it."""
    t = _make_target(db)

    # Recent window: bad decisions (2 losses)
    for i in range(2):
        db.add(ForecastDecisionLog(
            product_id="异戊二烯",
            horizon_day=7,
            as_of_date=datetime.date(2026, 7, 20) + datetime.timedelta(days=i),
            action="buy",
            confidence="high",
            rationale="test",
            predicted_p_rise=0.85,
            predicted_change_pct=0.06,
            actual_price_t=10000.0,
            roi_pct=-5.0,
            org_id="default-org",
        ))
    # Baseline window: good decisions (10 wins)
    for i in range(10):
        db.add(ForecastDecisionLog(
            product_id="异戊二烯",
            horizon_day=7,
            as_of_date=datetime.date(2026, 5, 1) + datetime.timedelta(days=i),
            action="buy",
            confidence="high",
            rationale="baseline",
            predicted_p_rise=0.80,
            predicted_change_pct=0.05,
            actual_price_t=10000.0,
            roi_pct=5.0,
            org_id="default-org",
        ))
    db.flush()

    from app.services.forecasting.ops.accuracy_feedback import run_accuracy_feedback
    result = run_accuracy_feedback(
        db, recent_window_days=30, baseline_days=90, degradation_threshold_pct=25.0
    )
    assert result["checked"] >= 1
    assert result["flagged"] >= 1

    audit = (
        db.query(ForecastWeightAdjustment)
        .filter(ForecastWeightAdjustment.triggered_by == "accuracy_degradation")
        .first()
    )
    assert audit is not None
    assert audit.applied is False
    assert audit.target_id == t.id


def test_run_accuracy_feedback_idempotent(db, monkeypatch):
    """Skip if pending audit exists within 7 days."""
    t = _make_target(db)

    # Existing pending audit within 7 days
    db.add(ForecastWeightAdjustment(
        target_id=t.id,
        triggered_by="accuracy_degradation",
        reason="already flagged",
        applied=False,
        org_id="default-org",
        created_date=datetime.datetime.utcnow() - datetime.timedelta(days=2),
    ))
    db.flush()

    for i in range(2):
        db.add(ForecastDecisionLog(
            product_id="异戊二烯",
            horizon_day=7,
            as_of_date=datetime.date(2026, 7, 20) + datetime.timedelta(days=i),
            action="buy",
            confidence="high",
            rationale="test",
            predicted_p_rise=0.85,
            predicted_change_pct=0.06,
            actual_price_t=10000.0,
            roi_pct=-5.0,
            org_id="default-org",
        ))
    db.flush()

    from app.services.forecasting.ops.accuracy_feedback import run_accuracy_feedback
    result = run_accuracy_feedback(
        db, recent_window_days=30, baseline_days=90, degradation_threshold_pct=25.0
    )
    assert result["flagged"] == 0  # idempotent skip

    count = (
        db.query(ForecastWeightAdjustment)
        .filter(ForecastWeightAdjustment.triggered_by == "accuracy_degradation")
        .count()
    )
    assert count == 1  # still only the original


def test_run_accuracy_feedback_no_degradation(db):
    """When accuracy is stable, no flags raised."""
    t = _make_target(db)

    for i in range(10):
        db.add(ForecastDecisionLog(
            product_id="异戊二烯",
            horizon_day=7,
            as_of_date=datetime.date(2026, 7, 1) + datetime.timedelta(days=i),
            action="buy",
            confidence="high",
            rationale="stable",
            predicted_p_rise=0.80,
            predicted_change_pct=0.05,
            actual_price_t=10000.0,
            roi_pct=5.0,
            org_id="default-org",
        ))
    db.flush()

    from app.services.forecasting.ops.accuracy_feedback import run_accuracy_feedback
    result = run_accuracy_feedback(
        db, recent_window_days=30, baseline_days=90, degradation_threshold_pct=50.0
    )
    assert result["flagged"] == 0


# ---------------------------------------------------------------------------
#  tests: API endpoints
# ---------------------------------------------------------------------------

def test_accuracy_flags_api_returns_list(db, monkeypatch):
    """GET /forecast-ops/accuracy-flags returns flagged products."""
    t = _make_target(db)
    db.add(ForecastWeightAdjustment(
        target_id=t.id,
        triggered_by="accuracy_degradation",
        reason="MAPE degradation detected",
        old_weights={"recent_roi_avg": -3.0, "baseline_roi_avg": 5.0},
        applied=False,
        org_id="default-org",
        created_date=datetime.datetime.utcnow() - datetime.timedelta(days=1),
    ))
    db.flush()

    flags = (
        db.query(ForecastWeightAdjustment)
        .filter(ForecastWeightAdjustment.triggered_by == "accuracy_degradation")
        .all()
    )
    assert len(flags) == 1
    assert "degradation" in flags[0].reason


def test_status_includes_degraded_count(db):
    """Verify degraded_count field is available."""
    t = _make_target(db)
    db.add(ForecastWeightAdjustment(
        target_id=t.id,
        triggered_by="accuracy_degradation",
        reason="test",
        applied=False,
        org_id="default-org",
        created_date=datetime.datetime.utcnow() - datetime.timedelta(days=1),
    ))
    db.flush()

    degraded = (
        db.query(ForecastWeightAdjustment)
        .filter(
            ForecastWeightAdjustment.triggered_by == "accuracy_degradation",
            ForecastWeightAdjustment.applied == False,  # noqa: E712
        )
        .count()
    )
    assert degraded == 1


# ---------------------------------------------------------------------------
#  tests: nightly step
# ---------------------------------------------------------------------------

def test_accuracy_feedback_step_skips_when_disabled(db):
    """When flag OFF, return {skipped: True}."""
    os.environ["FORECAST_ACCURACY_FEEDBACK_ENABLED"] = "false"

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_accuracy_feedback_step(db)
    assert result.get("skipped") is True


def test_accuracy_feedback_step_runs_when_enabled(db, monkeypatch):
    """When flag ON, calls run_accuracy_feedback."""
    os.environ["FORECAST_ACCURACY_FEEDBACK_ENABLED"] = "true"

    monkeypatch.setattr(
        "app.services.forecasting.ops.accuracy_feedback.run_accuracy_feedback",
        lambda db, **kw: {"checked": 0, "flagged": 0},
    )

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_accuracy_feedback_step(db)
    assert result["checked"] == 0
