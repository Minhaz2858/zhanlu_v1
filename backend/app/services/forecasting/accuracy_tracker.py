"""Realized accuracy tracking, drift detection, and adaptive model weights."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_WINDOW_DAYS_DEFAULT = 30
_DRIFT_THRESHOLD_PCT = 20.0
_ADAPTIVE_FACTOR = 0.3


# ---------------------------------------------------------------------------
# Rolling accuracy
# ---------------------------------------------------------------------------

def compute_realized_error(
    forecast_values: list[float],
    forecast_dates: list[pd.Timestamp],
    actual_values: pd.Series | None,
) -> dict:
    """Compute realized MAPE, MAE, and RMSE if actuals are available."""
    if actual_values is None or len(actual_values) == 0:
        return {"mape": None, "mae": None, "rmse": None, "signed_error": None, "n_matched": 0}

    pct_errors = []
    signed_errors = []
    abs_errors = []  # in price units
    for i, fdate in enumerate(forecast_dates):
        if fdate in actual_values.index:
            actual = actual_values.loc[fdate]
            if actual > 0:
                diff = forecast_values[i] - actual
                abs_errors.append(abs(diff))
                pct_errors.append(abs(diff) / actual * 100)
                signed_errors.append(diff / actual)
    if not pct_errors:
        return {"mape": None, "mae": None, "rmse": None, "signed_error": None, "n_matched": 0}

    return {
        "mape": float(np.mean(pct_errors)),
        "mae": float(np.mean(abs_errors)),
        "rmse": float(np.sqrt(np.mean(np.square(abs_errors)))),
        "signed_error": float(np.mean(signed_errors)),
        "n_matched": len(pct_errors),
    }


def rolling_accuracy(
    db,
    product_key: str,
    model_name: str | None = None,
    window_days: int = _WINDOW_DAYS_DEFAULT,
) -> dict:
    """Returns {'mean_mape': float, 'n_evaluations': int, 'trend': str}."""
    from app.models.forecasting import ForecastTarget, ForecastAccuracyLog

    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False  # noqa: E712
    ).first()
    if not target:
        return {"mean_mape": None, "n_evaluations": 0, "trend": "unknown"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.target_id == target.id,
        ForecastAccuracyLog.evaluated_at >= cutoff,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()

    mapes = [float(log.realized_mape) for log in logs]
    n = len(mapes)
    if n == 0:
        return {"mean_mape": None, "n_evaluations": 0, "trend": "unknown"}
    mean_mape = float(np.mean(mapes))
    # Simple trend: compare first half vs second half
    if n >= 6:
        first_half = np.mean(mapes[: n // 2])
        second_half = np.mean(mapes[n // 2:])
        if second_half < first_half * 0.9:
            trend = "improving"
        elif second_half > first_half * 1.1:
            trend = "degrading"
        else:
            trend = "stable"
    else:
        trend = "unknown"
    return {"mean_mape": mean_mape, "n_evaluations": n, "trend": trend}


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def detect_drift(
    db,
    product_key: str,
    baseline_window_days: int = 90,
    recent_window_days: int = 30,
    drift_threshold_pct: float = _DRIFT_THRESHOLD_PCT,
) -> dict:
    """Recent MAPE > baseline × (1 + threshold/100) → drift."""
    from app.models.forecasting import ForecastTarget, ForecastAccuracyLog

    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False  # noqa: E712
    ).first()
    if not target:
        return {"is_drifting": False, "reason": "no target"}

    all_logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.target_id == target.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()

    baseline_mapes = []
    recent_mapes = []
    now = datetime.now(timezone.utc)
    baseline_cutoff = now - timedelta(days=baseline_window_days)
    recent_cutoff = now - timedelta(days=recent_window_days)
    for log in all_logs:
        if log.evaluated_at is None:
            continue
        ev = log.evaluated_at
        if ev.tzinfo is not None:
            ev = ev.astimezone(timezone.utc).replace(tzinfo=None)
        mape = float(log.realized_mape)
        if ev >= recent_cutoff:
            recent_mapes.append(mape)
        elif ev >= baseline_cutoff:
            baseline_mapes.append(mape)

    if len(baseline_mapes) < 3 or len(recent_mapes) < 3:
        return {"is_drifting": False, "reason": "insufficient data"}

    baseline_mape = float(np.mean(baseline_mapes))
    recent_mape = float(np.mean(recent_mapes))
    is_drifting = recent_mape > baseline_mape * (1 + drift_threshold_pct / 100)
    return {
        "is_drifting": is_drifting,
        "baseline_mape": baseline_mape,
        "recent_mape": recent_mape,
        "reason": f"recent={recent_mape:.3f} vs baseline={baseline_mape:.3f}",
    }


# ---------------------------------------------------------------------------
# Adaptive weights
# ---------------------------------------------------------------------------

def adaptive_weights(
    backtest_weights: dict[str, float],
    realized_weights: dict[str, float] | None,
    realized_weight_factor: float = _ADAPTIVE_FACTOR,
) -> dict[str, float]:
    """Blend backtest weights with recent realized accuracy.

    Cold start (realized_weights=None) → returns backtest_weights unchanged.
    """
    if realized_weights is None or realized_weight_factor == 0.0:
        return dict(backtest_weights)

    blended: dict[str, float] = {}
    for model, bw in backtest_weights.items():
        rw = realized_weights.get(model, bw)
        blended[model] = (1 - realized_weight_factor) * bw + realized_weight_factor * rw

    # Renormalize to sum to 1.0
    total = sum(blended.values())
    if total > 0:
        blended = {k: v / total for k, v in blended.items()}
    return blended


# ---------------------------------------------------------------------------
# Decision ROI scoring (Phase F2)
# ---------------------------------------------------------------------------

def score_pending_decisions(db) -> dict:
    """Fetch pending decision logs whose horizon has closed, compute ROI, and fill.

    Called by the nightly compute pipeline or on-demand from the decision board.

    Returns summary: {scored_count, total_roi_avg, buy_count, sell_count, errors}
    """
    from app.services.forecasting.features.decision_logger import (
        get_pending_unrealized,
        fill_realized_outcomes,
    )
    from app.services.forecasting.features.decision_roi import (
        score_decision,
        aggregate_roi,
    )
    from app.services.forecasting.mysql_data_source import _resolve_mysql_engine

    pending = get_pending_unrealized(db)
    if not pending:
        return {"scored_count": 0, "total_roi_avg": None, "buy_count": 0,
                "sell_count": 0, "errors": []}

    engine = _resolve_mysql_engine()
    if engine is None:
        return {"scored_count": 0, "total_roi_avg": None, "buy_count": 0,
                "sell_count": 0, "errors": ["No external MySQL engine"]}

    scored = 0
    errors = []

    for log in pending:
        try:
            # Fetch actual price at as_of_date + horizon_day from market data
            import datetime
            from sqlalchemy import text
            from app.services.forecasting.features.exogenous_loaders import _ERP_TABLE_MAP

            target_date = log.as_of_date + datetime.timedelta(days=log.horizon_day)
            # Extract product_id from the full product_key (e.g. "<product>")
            product_id = log.product_id.split(".")[-1] if "." in log.product_id else log.product_id
            table = _ERP_TABLE_MAP.get(product_id, f"sale_erp_v_{product_id}_data")

            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        f"SELECT `date`, `price` FROM `{table}` "
                        f"WHERE `date` <= :target_date "
                        f"AND `date` >= :min_date "
                        f"ORDER BY `date` DESC LIMIT 1"
                    ),
                    {"target_date": target_date.isoformat(),
                     "min_date": (target_date - datetime.timedelta(days=7)).isoformat()},
                ).fetchone()

            if row is None:
                continue

            actual_price_th = float(row[1]) if row[1] is not None else None
            if actual_price_th is None:
                continue

            # We already have actual_price_t from the log
            actual_price_t = float(log.actual_price_t)
            if actual_price_t <= 0:
                continue

            roi = score_decision(
                action=log.action,
                actual_price_t=actual_price_t,
                actual_price_th=actual_price_th,
            )

            fill_realized_outcomes(
                session=db,
                log_id=log.id,
                actual_price_t=actual_price_t,
                actual_price_th=actual_price_th,
                roi_pct=roi,
            )
            scored += 1
        except Exception as exc:
            logger.warning("score_pending_decisions failed for log %s: %s", log.id, exc)
            errors.append(str(exc))

    # Re-fetch for aggregate summary
    if scored > 0:
        from app.models.forecasting import ForecastDecisionLog
        scored_logs = db.query(ForecastDecisionLog).filter(
            ForecastDecisionLog.roi_pct.isnot(None)
        ).order_by(ForecastDecisionLog.realized_at.desc()).limit(scored).all()
        summary = aggregate_roi(scored_logs)
        return {
            "scored_count": scored,
            "total_roi_avg": summary.weighted_roi,
            "buy_count": summary.buy_count,
            "sell_count": summary.sell_count,
            "accuracy_pct": summary.accuracy_pct,
            "errors": errors,
        }

    return {"scored_count": scored, "total_roi_avg": None, "buy_count": 0,
            "sell_count": 0, "errors": errors}


# ---------------------------------------------------------------------------
# Realized-price backfill (self-learning loop)
# ---------------------------------------------------------------------------

def backfill_realized_prices(db) -> dict:
    """Backfill actual_price_t and actual_price_th from ERP execution prices.

    Step 1: Fill actual_price_t for decision logs where it's NULL.
    Step 2: Fill actual_price_th for decision logs where the horizon has passed.

    Returns {backfilled_t, backfilled_th, skipped, errors}.
    """
    from app.models.forecasting import ForecastDecisionLog
    from app.services.forecasting.features.exogenous_loaders import _resolve_mysql_engine

    engine = _resolve_mysql_engine()
    if engine is None:
        logger.warning("[backfill] No external MySQL engine — skipping")
        return {"backfilled_t": 0, "backfilled_th": 0, "skipped": 0, "errors": ["No MySQL engine"]}

    now = datetime.now(timezone.utc)
    backfilled_t = 0
    backfilled_th = 0
    skipped = 0
    errors = []

    # Step 1: Fill actual_price_t where NULL
    null_t_logs = db.query(ForecastDecisionLog).filter(
        ForecastDecisionLog.actual_price_t.is_(None),
    ).limit(200).all()

    for log in null_t_logs:
        try:
            # Use md_t_lz_price (main price table) for all products
            # The product_id in decision logs maps to FMATERIAL_NAME
            target_date = log.as_of_date
            product_name = log.product_id

            with engine.connect() as conn:
                from sqlalchemy import text
                row = conn.execute(
                    text(
                        "SELECT `FDATE`, `FTAXPRICE` FROM `md_t_lz_price` "
                        "WHERE `FMATERIAL_NAME` = :product "
                        "AND `FDATE` <= :target_date "
                        "AND `FDATE` >= :min_date "
                        "AND `FTAXPRICE` NOT IN ('F7', 'NaN', '', 'null', 'NULL') "
                        "ORDER BY `FDATE` DESC LIMIT 1"
                    ),
                    {
                        "product": product_name,
                        "target_date": target_date.isoformat(),
                        "min_date": (target_date - timedelta(days=7)).isoformat(),
                    },
                ).fetchone()

            if row and row[1] is not None:
                log.actual_price_t = float(row[1])
                backfilled_t += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"log {log.id}: {exc}")
            logger.warning("[backfill] actual_price_t failed for log %s: %s", log.id, exc)

    if backfilled_t > 0:
        db.flush()

    # Step 2: Fill actual_price_th where NULL but horizon has passed and actual_price_t is set
    null_th_logs = db.query(ForecastDecisionLog).filter(
        ForecastDecisionLog.actual_price_t.isnot(None),
        ForecastDecisionLog.actual_price_th.is_(None),
        ForecastDecisionLog.roi_pct.is_(None),
    ).limit(200).all()

    for log in null_th_logs:
        try:
            horizon_end = log.as_of_date + timedelta(days=log.horizon_day)
            if horizon_end > now:
                skipped += 1
                continue

            product_name = log.product_id

            with engine.connect() as conn:
                from sqlalchemy import text
                row = conn.execute(
                    text(
                        "SELECT `FDATE`, `FTAXPRICE` FROM `md_t_lz_price` "
                        "WHERE `FMATERIAL_NAME` = :product "
                        "AND `FDATE` <= :target_date "
                        "AND `FDATE` >= :min_date "
                        "AND `FTAXPRICE` NOT IN ('F7', 'NaN', '', 'null', 'NULL') "
                        "ORDER BY `FDATE` DESC LIMIT 1"
                    ),
                    {
                        "product": product_name,
                        "target_date": horizon_end.isoformat(),
                        "min_date": (horizon_end - timedelta(days=7)).isoformat(),
                    },
                ).fetchone()

            if row and row[1] is not None:
                log.actual_price_th = float(row[1])
                # Compute ROI immediately
                actual_price_t = float(log.actual_price_t)
                if actual_price_t > 0:
                    from app.services.forecasting.features.decision_roi import score_decision
                    log.roi_pct = score_decision(
                        action=log.action,
                        actual_price_t=actual_price_t,
                        actual_price_th=float(row[1]),
                    )
                    log.realized_at = now
                backfilled_th += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"log {log.id}: {exc}")
            logger.warning("[backfill] actual_price_th failed for log %s: %s", log.id, exc)

    if backfilled_th > 0:
        db.flush()

    logger.info(
        "[backfill] Done: %d price_t, %d price_th, %d skipped, %d errors",
        backfilled_t, backfilled_th, skipped, len(errors),
    )
    return {
        "backfilled_t": backfilled_t,
        "backfilled_th": backfilled_th,
        "skipped": skipped,
        "errors": errors[:10],  # cap for log safety
    }
