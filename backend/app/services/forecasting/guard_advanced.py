"""P2-1: Advanced guard rails extending the honesty gate.

Adds four layers on top of the original binary ensemble-vs-naive check:
1. Monotonicity enforcement — prevents forecast from going below last actual
2. Change-rate clamp — caps step-to-step change to max_change_pct
3. Volatility-regime blend — blends toward naive when vol is high
4. Stale-data guard — flags when last observed data is too old

All features are opt-in; with defaults off, behavior matches evaluate_guard().

P2.14: Volatility thresholds imported from forecast_policy_service (single source of truth).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

# P2.14: canonical volatility thresholds (single source of truth)
from app.services.forecasting.forecast_policy_service import (
    HIGH_VOL_THRESHOLD,
    MODERATE_VOL_THRESHOLD,
)

import numpy as np
import pandas as pd

from app.services.forecasting.guard import GuardResult, evaluate_guard

logger = logging.getLogger(__name__)


@dataclass
class AdvancedGuardResult(GuardResult):
    """Extended guard result with per-feature diagnostics."""

    monotonicity_violations: int = 0
    change_rate_clamped: bool = False
    regime_blend_factor: float | None = None  # None = no blend
    stale_data_detected: bool = False


def evaluate_guard_advanced(
    ensemble_forecast: pd.Series,
    naive_forecast: pd.Series,
    ensemble_mape: float,
    naive_mape: float,
    last_actual: float | None = None,
    max_change_pct: float = 15.0,
    enforce_monotonicity: bool = False,
    vol_regime_blend: bool = False,
    daily_returns_std: float | None = None,
    stale_threshold_days: int = 14,
    last_data_date: datetime | None = None,
    soft_blend_enabled: bool = False,
    soft_blend_margin_pct: float = 2.0,
) -> AdvancedGuardResult:
    """Evaluate guard with advanced safety layers.

    Processing order:
    1. Original honesty gate (ensemble vs naive error)
    2. Stale-data detection
    3. Volatility-regime blending
    4. Monotonicity enforcement
    5. Change-rate clamp

    Args:
        ensemble_forecast: Ensemble point forecast (h steps).
        naive_forecast: Seasonal-naive point forecast (h steps).
        ensemble_mape: Ensemble backtest MAPE.
        naive_mape: Naive backtest MAPE.
        last_actual: Last observed actual value (for monotonicity + clamp).
        max_change_pct: Maximum allowed % change from one step to the next.
        enforce_monotonicity: If True, ensure forecast does not go below last_actual.
        vol_regime_blend: If True, blend toward naive when daily_returns_std is high.
        daily_returns_std: Standard deviation of daily returns (for regime detection).
        stale_threshold_days: Max days since last_data_date before flagging stale.
        last_data_date: Date of last observed data point.
    """
    # Step 1: Original binary gate
    base = evaluate_guard(ensemble_forecast, naive_forecast, ensemble_mape, naive_mape,
                          soft_blend_enabled=soft_blend_enabled,
                          soft_blend_margin_pct=soft_blend_margin_pct)
    published = base.published_forecast.copy()

    # Step 2: Stale-data detection
    stale = False
    if last_data_date is not None:
        days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_data_date).days
        if days_since > stale_threshold_days:
            stale = True
            logger.warning(
                "[guard-advanced] stale data detected: last_data=%s, %d days ago",
                last_data_date.date(), days_since,
            )

    # Step 3: Volatility-regime blending
    blend_factor: float | None = None
    if vol_regime_blend and daily_returns_std is not None and not base.below_naive_baseline:
        # P2.14: Use canonical thresholds from forecast_policy_service
        if daily_returns_std >= HIGH_VOL_THRESHOLD:
            blend_factor = min(daily_returns_std / 20.0, 0.7)  # Blend up to 70% toward naive
        elif daily_returns_std >= MODERATE_VOL_THRESHOLD:
            blend_factor = 0.2  # Mild blend

        if blend_factor is not None:
            published = published * (1 - blend_factor) + naive_forecast * blend_factor
            logger.info(
                "[guard-advanced] vol-regime blend: factor=%.2f, daily_std=%.2f",
                blend_factor, daily_returns_std,
            )

    # Step 4: Monotonicity enforcement
    monotonicity_violations = 0
    if enforce_monotonicity and last_actual is not None:
        published_vals = published.values.astype(float)
        for i in range(len(published_vals)):
            if published_vals[i] < last_actual:
                published_vals[i] = last_actual
                monotonicity_violations += 1
        published = pd.Series(published_vals, index=published.index, name=published.name)
        if monotonicity_violations > 0:
            logger.warning(
                "[guard-advanced] monotonicity: %d violations corrected (below %.2f)",
                monotonicity_violations, last_actual,
            )

    # Step 5: Change-rate clamp
    clamped = False
    if last_actual is not None and max_change_pct < 1000.0:
        published_vals = published.values.astype(float)
        prev = last_actual
        for i in range(len(published_vals)):
            allowed_max = prev * (1.0 + max_change_pct / 100.0)
            allowed_min = prev * (1.0 - max_change_pct / 100.0)
            if published_vals[i] > allowed_max:
                published_vals[i] = allowed_max
                clamped = True
            elif published_vals[i] < allowed_min:
                published_vals[i] = allowed_min
                clamped = True
            prev = published_vals[i]
        published = pd.Series(published_vals, index=published.index, name=published.name)
        if clamped:
            logger.warning(
                "[guard-advanced] change-rate clamp applied (max_change_pct=%.1f%%)",
                max_change_pct,
            )

    return AdvancedGuardResult(
        below_naive_baseline=base.below_naive_baseline,
        published_forecast=published,
        ensemble_mape=base.ensemble_mape,
        naive_mape=base.naive_mape,
        blend_ratio=base.blend_ratio,
        monotonicity_violations=monotonicity_violations,
        change_rate_clamped=clamped,
        regime_blend_factor=blend_factor,
        stale_data_detected=stale,
    )
