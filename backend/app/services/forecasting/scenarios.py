"""Scenario generation — base / bull / bear for 3 horizons.

Takes the published point forecast (from ensemble or naive fallback)
and constructs optimistic (bull) and pessimistic (bear) bounds using
the holdout residual distribution.  Each horizon also gets a confidence
label based on the backtest MAPE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from app.services.forecasting.conformal import ConformalCalibration

logger = logging.getLogger(__name__)

# Standard horizons for business reporting
DEFAULT_HORIZONS = [3, 7, 30]

# Confidence thresholds (MAPE-based)
CONFIDENCE_HIGH = 0.10  # MAPE < 10%
CONFIDENCE_LOW = 0.25   # MAPE > 25%


@dataclass
class ScenarioResult:
    """Scenarios for all configured horizons."""

    horizons: dict[int, dict[str, pd.Series]]  # {h: {"base": s, "bull": s, "bear": s}}
    confidence: str  # "High" / "Medium" / "Low"
    bounds_source: str = "residuals"  # "residuals" or "sigma"


def generate(
    point_forecast: pd.Series,
    residuals: list[float] | None = None,
    mape: float | None = None,
    horizons: list[int] | None = None,
    fallback_std: float | None = None,
    calibration: "ConformalCalibration | None" = None,
) -> ScenarioResult:
    """Generate base/bull/bear for specified horizons.

    Parameters
    ----------
    point_forecast : pd.Series
        The published point forecast (ensemble or naive fallback).
        Must be at least ``max(horizons)`` steps long.
    residuals : list[float] or None
        Holdout residuals (actual - predicted) from backtest.  Used to
        compute scenario bounds via percentiles.
    mape : float or None
        Backtest MAPE.  Used for confidence label.  If None, defaults
        to "Medium".
    horizons : list[int] or None
        Horizons to generate scenarios for.  Default: [3, 7, 30].
    fallback_std : float or None
        If *residuals* is None or empty, use this standard deviation
        for ±1-sigma bands.  If also None, default is 10% of the mean
        absolute forecast value.
    calibration : ConformalCalibration or None
        When provided, bull/bear bounds use per-horizon conformal
        intervals (calibrated coverage ~1-alpha) instead of the
        25th/75th residual percentile.  Preferred over *residuals*.

    Returns
    -------
    ScenarioResult
    """
    if horizons is None:
        horizons = DEFAULT_HORIZONS

    h_max = max(horizons)
    if len(point_forecast) < h_max:
        # Pad forecast — extrastep is repeat of last value
        values = list(point_forecast.values)
        pad_val = values[-1] if values else 0.0
        padded = np.full(h_max, pad_val, dtype=float)
        padded[:len(values)] = values
        point_forecast = pd.Series(padded, name=point_forecast.name)

    use_conformal = calibration is not None

    # Determine bounds (non-conformal path: single quantile for all horizons)
    if not use_conformal:
        if residuals and len(residuals) >= 5:
            lower_quantile = float(np.percentile(residuals, 25))
            upper_quantile = float(np.percentile(residuals, 75))
            bounds_source = "residuals"
        else:
            # Fallback: ±1 sigma around zero
            if fallback_std is not None:
                sigma = fallback_std
            else:
                sigma = 0.1 * float(np.abs(point_forecast.values[:h_max]).mean() + 1e-10)
            lower_quantile = -sigma
            upper_quantile = sigma
            bounds_source = "sigma"
    else:
        bounds_source = "conformal"

    # Confidence label
    if mape is not None and mape < float("inf"):
        if mape < CONFIDENCE_HIGH:
            confidence = "High"
        elif mape < CONFIDENCE_LOW:
            confidence = "Medium"
        else:
            confidence = "Low"
    else:
        confidence = "Medium"

    # Build per-horizon scenario dicts
    horizon_scenarios: dict[int, dict[str, pd.Series]] = {}

    for h in horizons:
        base = pd.Series(point_forecast.values[:h], name="base")
        if use_conformal:
            lo, hi = calibration.interval(point_forecast, horizon=h)
            bull = pd.Series(hi.values[:h], name="bull")
            bear = pd.Series(lo.values[:h], name="bear")
        else:
            bull = pd.Series(base.values + upper_quantile, name="bull")
            bear = pd.Series(base.values + lower_quantile, name="bear")
        horizon_scenarios[h] = {"base": base, "bull": bull, "bear": bear}

    return ScenarioResult(
        horizons=horizon_scenarios,
        confidence=confidence,
        bounds_source=bounds_source,
    )
