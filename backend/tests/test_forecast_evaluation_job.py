"""Evaluation job: populates realized_mape + scores HITL feedback against actuals."""
from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastFeedback,
)
from app.services.forecasting.ops.evaluation_job import run_evaluation

_NEEDED = [ForecastTarget.__table__, ForecastRun.__table__,
           ForecastAccuracyLog.__table__, ForecastFeedback.__table__]


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


def _make_run(db, as_of, base_values_by_h):
    """Seed a target + a ForecastRun with results[str(h)]["base"] = list."""
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.flush()
    results = {str(h): {"base": vals, "bull": vals, "bear": vals}
               for h, vals in base_values_by_h.items()}
    run = ForecastRun(
        target_id=target.id, org_id="default-org", app_id="default-app",
        results=results, below_naive_baseline=False, confidence="Medium",
        as_of_date=as_of, model_detail={"ensemble_mape": 0.10, "naive_mape": 0.12},
    )
    db.add(run); db.commit()
    return target, run


def test_writes_realized_mape_for_past_run(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # 7-day horizon: forecast 100 for Jul 2..Jul 8; actuals all 110 -> MAPE = 10/110 each
    target, run = _make_run(db, as_of, {7: [100.0] * 7})
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    actuals = pd.Series([110.0] * 7, index=idx)
    loader = lambda datasource: actuals  # noqa: E731

    summary = run_evaluation(db, actuals_loader=loader, product_key="异戊二烯")

    logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()
    assert len(logs) == 1
    assert logs[0].realized_mape is not None
    assert abs(logs[0].realized_mape - (10.0 / 110.0 * 100)) < 0.01
    assert logs[0].evaluated_at is not None
    assert summary["runs_scored"] >= 1


def test_idempotent_rerun_no_duplicate(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    target, run = _make_run(db, as_of, {7: [100.0] * 7})
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    loader = lambda datasource: pd.Series([110.0] * 7, index=idx)  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="异戊二烯")
    run_evaluation(db, actuals_loader=loader, product_key="异戊二烯")  # re-run
    n = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).count()
    assert n == 1


def test_skips_run_whose_horizon_not_yet_arrived(db):
    # as_of = yesterday -> 30-day horizon not passed -> not scored
    as_of = datetime.now(timezone.utc) - timedelta(days=1)
    target, run = _make_run(db, as_of, {30: [100.0] * 30})
    loader = lambda datasource: pd.Series([110.0], index=pd.date_range("2026-01-01", periods=1))  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="异戊二烯")
    n = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).count()
    assert n == 0


def test_scores_pending_feedback(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    target, run = _make_run(db, as_of, {7: [100.0] * 7})
    # AI said 100; user said 109; actual = 110 -> user_error < ai_error -> beat
    fb = ForecastFeedback(
        target_id=target.id, product_id="异戊二烯", ai_price=100.0,
        user_price=109.0, reason="tightening", author_id="u1", author_name="a1",
        target_date=datetime(2026, 7, 5), status="pending", org_id="default-org",
    )
    db.add(fb); db.commit()
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    loader = lambda datasource: pd.Series([110.0] * 7, index=idx)  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="异戊二烯")
    db.refresh(fb)
    assert fb.status == "scored"
    assert fb.beat is True
    assert fb.scored_at is not None
    assert fb.ai_error > fb.user_error
