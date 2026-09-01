"""Close the realized-accuracy loop.

Matches past ForecastRun rows (their stored published `results[str(h)]["base"]`)
against newly-arrived actuals, writes realized_mape onto ForecastAccuracyLog
rows, and scores pending HITL feedback. Idempotent: a run already scored is
never re-scored; a feedback already scored keeps its result.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastFeedback,
)
from app.services.forecasting.accuracy_tracker import compute_realized_error

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS = [3, 7, 30]


def _default_actuals_loader():
    """Build the default loader that reads external MySQL actuals."""
    from app.services.forecasting.mysql_data_source import MysqlDataSource
    src = MysqlDataSource()
    return lambda datasource: src.read_history(datasource)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC.

    SQLite returns datetimes without tzinfo, but in-memory test objects may
    carry tzinfo. Normalizing to naive UTC makes every comparison and pandas
    index lookup safe (naive vs naive) and matches the codebase convention
    (TimestampedBase defaults use ``datetime.now(timezone.utc)``).
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _reconstruct_forecast_dates(as_of: datetime, horizon: int) -> list[pd.Timestamp]:
    """Daily cadence: forecast step i -> as_of + (i+1) days."""
    return [pd.Timestamp(as_of + timedelta(days=i)) for i in range(1, horizon + 1)]


def _actuals_series(actuals_loader, datasource) -> Optional[pd.Series]:
    try:
        df = actuals_loader(datasource)
    except Exception:
        logger.exception("[eval-job] actuals load failed")
        return None
    if df is None:
        return None
    # Loader may return a ready Series (tests) or a raw DataFrame (prod).
    if isinstance(df, pd.Series):
        s = df.astype(float).copy()
        s.index = pd.to_datetime(s.index)
    else:
        if len(df) == 0:
            return None
        time_col = "FDATE" if "FDATE" in df.columns else df.columns[0]
        measure = "FTAXPRICE" if "FTAXPRICE" in df.columns else df.columns[-1]
        s = pd.Series(df[measure].astype(float).values,
                      index=pd.to_datetime(df[time_col]))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    # Normalize index to naive so it matches naive forecast dates.
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_convert(None)
    return s


def run_evaluation(
    db,
    actuals_loader: Optional[Callable] = None,
    product_key: Optional[str] = None,
) -> dict:
    """Score past forecasts + pending feedback against newly-arrived actuals.

    Returns: {"runs_scored": int, "runs_skipped": int, "feedback_scored": int}.
    """
    if actuals_loader is None:
        actuals_loader = _default_actuals_loader()

    now = datetime.now(timezone.utc)
    runs_scored = 0
    runs_skipped = 0
    feedback_scored = 0

    q = db.query(ForecastRun)
    if product_key:
        q = q.join(ForecastTarget, ForecastTarget.id == ForecastRun.target_id).filter(
            ForecastTarget.product_key == product_key
        )
    runs = q.order_by(ForecastRun.as_of_date.desc()).limit(200).all()

    for run in runs:
        target = db.query(ForecastTarget).filter(ForecastTarget.id == run.target_id).first()
        if target is None:
            continue
        actuals = _actuals_series(actuals_loader, target.datasource)
        if actuals is None:
            continue
        results = run.results or {}
        for h in DEFAULT_HORIZONS:
            key = str(h)
            if key not in results:
                continue
            base_vals = results[key].get("base") if isinstance(results[key], dict) else None
            if not base_vals:
                continue
            as_of = run.as_of_date or run.created_date
            if as_of is None:
                continue
            as_of = _to_naive_utc(as_of)
            # Skip if the horizon hasn't fully arrived.
            horizon_end = as_of + timedelta(days=h)
            if horizon_end > now:
                continue
            # Idempotency: already scored for this run+horizon?
            exists = db.query(ForecastAccuracyLog).filter(
                ForecastAccuracyLog.run_id == run.id,
                ForecastAccuracyLog.horizon_days == h,
                ForecastAccuracyLog.realized_mape.isnot(None),
            ).first()
            if exists:
                runs_skipped += 1
                continue
            fc_dates = _reconstruct_forecast_dates(as_of, h)
            fc_values = [float(v) for v in base_vals[:h]]
            res = compute_realized_error(fc_values, fc_dates, actuals)
            if res["n_matched"] == 0:
                continue
            md = run.model_detail or {}
            log = ForecastAccuracyLog(
                target_id=target.id, org_id=target.org_id, app_id=target.app_id,
                run_id=run.id, horizon_days=h,
                mape=md.get("ensemble_mape"),
                naive_mape=md.get("naive_mape"),
                realized_mape=res["mape"],
                realized_error=res.get("signed_error"),
                mae=res.get("mae"),
                rmse=res.get("rmse"),
                evaluated_at=now,
                below_naive_baseline=run.below_naive_baseline,
                per_model=None,  # marker: realized-eval row (backtest rows always set per_model)
            )
            db.add(log)
            runs_scored += 1

        feedback_scored += _score_pending_feedback(db, target, actuals, now)

    db.commit()
    summary = {
        "runs_scored": runs_scored,
        "runs_skipped": runs_skipped,
        "feedback_scored": feedback_scored,
    }
    logger.info("[eval-job] %s", summary)
    return summary


def _score_pending_feedback(db, target, actuals: pd.Series, now: datetime) -> int:
    pending = db.query(ForecastFeedback).filter(
        ForecastFeedback.target_id == target.id,
        ForecastFeedback.status == "pending",
        ForecastFeedback.target_date.isnot(None),
    ).all()
    scored = 0
    for fb in pending:
        td = pd.Timestamp(fb.target_date)
        if getattr(td, "tz", None) is not None:
            td = td.tz_convert(None)
        if td not in actuals.index:
            # nearest available actual on/before target_date
            prior = actuals[actuals.index <= td]
            if prior.empty:
                continue
            actual = float(prior.iloc[-1])
        else:
            actual = float(actuals.loc[td])
        if actual <= 0:
            continue
        fb.ai_error = abs(fb.ai_price - actual) / actual
        fb.user_error = abs(fb.user_price - actual) / actual
        fb.beat = fb.user_error < fb.ai_error
        fb.status = "scored"
        fb.scored_at = now
        scored += 1
    return scored


def get_realized_weights(db, product_key: str) -> Optional[dict]:
    """Return a realized-weights map for adaptive_weights().

    Per-model realized accuracy is NOT reconstructable (only the ensemble
    published forecast is stored), so this returns None -- drift_response uses
    a naive-blend strategy instead. Kept for API symmetry with adaptive_weights.
    """
    return None
