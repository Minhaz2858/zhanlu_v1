"""P0-2: Domain signal calibration — A/B backtest + JSON elasticity override.

Provides:
- run_calibration_backtest(): measures MAPE with/without domain signals
- load_elasticity_overrides(): reads JSON override table
- get_calibrated_elasticity(): resolves product elasticity (override > static)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.forecasting.domain_signals import (
    _ELASTICITIES,
    compute_domain_signal_adjustment,
)

logger = logging.getLogger(__name__)

# Default calibration file location (relative to backend/ or absolute)
_DEFAULT_CALIBRATION_PATH = "data/domain_signal_calibration.json"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class CalibrationResult:
    """Result of an A/B backtest comparing with/without domain signals."""

    product_id: str
    baseline_mape: float          # MAPE without domain signals
    with_signals_mape: float      # MAPE with domain signals
    improvement_pct: float        # (baseline - with_signals) / baseline * 100
    recommended_elasticity: float | None = None  # tuned elasticity, or None
    applied: bool = False         # True if domain signals improved the forecast
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# A/B backtest
# ---------------------------------------------------------------------------

def run_calibration_backtest(
    y: pd.Series,
    product_id: str,
    naphtha_pct_change: float | None,
    as_of_date: datetime,
    horizons: list[int] | None = None,
    min_train: int = 30,
    min_holdout: int = 3,
    improvement_threshold_pct: float = 0.5,
) -> CalibrationResult:
    """Run an A/B backtest comparing seasonal-naive with vs without domain signals.

    Uses a walk-forward approach: for each fold, produces a base forecast
    (seasonal naive) and a domain-adjusted version, then computes MAPE
    to determine whether the domain overlay improves accuracy.

    Args:
        y: Target time series.
        product_id: The dashboard product_id.
        feedstock_pct_change: Recent feedstock % change (or None).
        as_of_date: Anchor date (determines seasonal month).
        horizons: List of forecast horizons to test. Default [7].
        min_train: Minimum training window size.
        min_holdout: Minimum holdout window size.
        improvement_threshold_pct: Min MAPE improvement (%) to set applied=True.

    Returns:
        CalibrationResult with baseline/w_signals MAPE and recommendation.
    """
    if horizons is None:
        horizons = [7]

    y = y.dropna().sort_index()
    n = len(y)

    if n < min_train + min_holdout:
        return CalibrationResult(
            product_id=product_id,
            baseline_mape=float("nan"),
            with_signals_mape=float("nan"),
            improvement_pct=0.0,
            recommended_elasticity=None,
            applied=False,
            notes=["series too short for backtest"],
        )

    # Walk-forward folds
    base_errors: list[float] = []
    sig_errors: list[float] = []

    # Choose a practical horizon for fold sizing
    test_horizon = max(horizons)

    # Build folds: sliding training window, test window = test_horizon
    fold_start = min_train
    step = max(7, test_horizon // 2)

    while fold_start + test_horizon <= n:
        y_train = y.iloc[:fold_start]
        y_test = y.iloc[fold_start:fold_start + test_horizon]

        if len(y_test) < min_holdout:
            fold_start += step
            continue

        # Baseline: seasonal naive forecast (values from same period)
        seasonal_period = _guess_seasonal_period(y_train)
        baseline_fc = _seasonal_naive_forecast(y_train, test_horizon, seasonal_period)

        # Domain-adjusted: apply domain signal overlay to baseline
        adj_result = compute_domain_signal_adjustment(
            product_id, as_of_date, naphtha_pct_change,
        )
        total_pct = adj_result["total_pct"]
        domain_fc = baseline_fc * (1.0 + total_pct / 100.0)

        # Compute errors for each horizon
        for h in horizons:
            if h > test_horizon:
                continue
            actual_h = y_test.iloc[:h]
            base_h = baseline_fc.iloc[:h]
            sig_h = domain_fc.iloc[:h]
            if len(actual_h) >= min_holdout:
                base_mape_h = np.mean(np.abs((actual_h.values - base_h.values) / np.maximum(np.abs(actual_h.values), 0.01))) * 100
                sig_mape_h = np.mean(np.abs((actual_h.values - sig_h.values) / np.maximum(np.abs(actual_h.values), 0.01))) * 100
                base_errors.append(base_mape_h)
                sig_errors.append(sig_mape_h)

        fold_start += step

    if not base_errors:
        return CalibrationResult(
            product_id=product_id,
            baseline_mape=float("nan"),
            with_signals_mape=float("nan"),
            improvement_pct=0.0,
            recommended_elasticity=None,
            applied=False,
            notes=["no valid folds"],
        )

    baseline_mape = float(np.mean(base_errors))
    with_signals_mape = float(np.mean(sig_errors))

    if baseline_mape == 0 or np.isnan(baseline_mape):
        improvement_pct = 0.0
    else:
        improvement_pct = (baseline_mape - with_signals_mape) / baseline_mape * 100.0

    applied = improvement_pct > improvement_threshold_pct

    # Determine recommended elasticity (current static value)
    static_elasticity = _ELASTICITIES.get(product_id)
    recommended = static_elasticity if applied else None

    notes: list[str] = []
    if applied:
        notes.append(f"domain signals improved MAPE by {improvement_pct:.1f}%")
        notes.append(f"recommended_elasticity={recommended}")
    else:
        notes.append("domain signals did not significantly improve accuracy")

    return CalibrationResult(
        product_id=product_id,
        baseline_mape=round(baseline_mape, 4),
        with_signals_mape=round(with_signals_mape, 4),
        improvement_pct=round(improvement_pct, 2),
        recommended_elasticity=recommended,
        applied=applied,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# JSON override loading
# ---------------------------------------------------------------------------

def load_elasticity_overrides(
    path: str | None = None,
) -> dict[str, float]:
    """Load elasticity override map from JSON file.

    File format:
        {"product_id": effective_elasticity, ...}

    Args:
        path: Path to the JSON file. Defaults to data/domain_signal_calibration.json
              resolved relative to the backend package root.

    Returns:
        Dict of product_id -> effective elasticity. Empty dict if file not found
        or invalid.
    """
    if path is None:
        # Resolve default path relative to the backend app directory
        backend_root = Path(__file__).resolve().parent.parent.parent.parent
        path = str(backend_root / _DEFAULT_CALIBRATION_PATH)

    if not os.path.isfile(path):
        logger.debug("[domain-calibration] no override file at %s", path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[domain-calibration] failed to load %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        return {}

    # Validate: keys must be strings, values must be numeric
    overrides: dict[str, float] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, (int, float)):
            overrides[k] = float(v)
        else:
            logger.warning(
                "[domain-calibration] skipping invalid entry: %s=%s (type %s)",
                k, v, type(v).__name__,
            )

    return overrides


# ---------------------------------------------------------------------------
# Calibrated elasticity resolver
# ---------------------------------------------------------------------------

def get_calibrated_elasticity(
    product_id: str,
    overrides: dict[str, float] | None = None,
) -> float | None:
    """Resolve the effective elasticity for a product.

    Precedence:
    1. Override dict (from JSON calibration file)
    2. Static _ELASTICITIES table

    Args:
        product_id: Dashboard product_id.
        overrides: Optional dict of product_id -> elasticity from JSON file.

    Returns:
        Effective elasticity (float), or None if product is unknown.
    """
    if overrides and product_id in overrides:
        return float(overrides[product_id])
    return _ELASTICITIES.get(product_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _guess_seasonal_period(y: pd.Series) -> int:
    """Heuristic: use 7 for daily data with >= 28 obs, else 1."""
    if len(y) >= 28 and isinstance(y.index, pd.DatetimeIndex):
        if (y.index[1] - y.index[0]).days <= 2:
            return 7
    return 1


def _seasonal_naive_forecast(
    y: pd.Series, horizon: int, seasonal_period: int,
) -> pd.Series:
    """Simple seasonal naive: last observed values offset by seasonal_period."""
    if seasonal_period <= 1 or len(y) < seasonal_period:
        # Fall back to naive (last value)
        last_val = y.iloc[-1]
        fc_vals = [last_val] * horizon
    else:
        fc_vals = []
        for i in range(horizon):
            idx = len(y) - seasonal_period + (i % seasonal_period)
            if idx >= 0 and idx < len(y):
                fc_vals.append(y.iloc[idx])
            else:
                fc_vals.append(y.iloc[-1])

    future_dates = pd.date_range(
        start=y.index[-1] + pd.Timedelta(days=1), periods=horizon, freq="D",
    )
    return pd.Series(fc_vals, index=future_dates, name="fc")
