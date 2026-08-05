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
    """Compute realized MAPE if actuals are available."""
    if actual_values is None or len(actual_values) == 0:
        return {"mape": None, "mae": None, "n_matched": 0}

    errors = []
    for i, fdate in enumerate(forecast_dates):
        if fdate in actual_values.index:
            actual = actual_values.loc[fdate]
            if actual > 0:
                errors.append(abs(forecast_values[i] - actual) / actual * 100)
    if not errors:
        return {"mape": None, "mae": None, "n_matched": 0}

    return {"mape": float(np.mean(errors)), "n_matched": len(errors)}


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

    cutoff = datetime.utcnow() - timedelta(days=window_days)
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
    now = datetime.utcnow()
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
