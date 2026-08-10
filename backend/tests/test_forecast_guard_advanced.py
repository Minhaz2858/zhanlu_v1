"""P2-1: Advanced guard rails — monotonicity, change-rate clamp, regime blend, stale guard.

Tests verify each of the four advanced guard features independently and combined.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.services.forecasting.guard_advanced import (
    AdvancedGuardResult,
    evaluate_guard_advanced,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mk_series(vals: list[float], start_date: str = "2025-06-01") -> pd.Series:
    dates = pd.date_range(start=start_date, periods=len(vals), freq="D")
    return pd.Series(vals, index=dates, name="fc")


def _make_flat(h: int, val: float) -> pd.Series:
    return _mk_series([val] * h)


# ---------------------------------------------------------------------------
# Basic operation
# ---------------------------------------------------------------------------

def test_basic_guard_same_as_original():
    """When all advanced features are disabled, result matches original logic."""
    ensemble = _make_flat(7, 105.0)
    naive = _make_flat(7, 100.0)
    # ensemble has worse MAPE → should fall back to naive
    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=8.0,
        naive_mape=5.0,
    )
    assert result.below_naive_baseline is True
    assert (result.published_forecast.values == naive.values).all()
    assert result.monotonicity_violations == 0
    assert result.change_rate_clamped is False
    assert result.regime_blend_factor is None
    assert result.stale_data_detected is False


def test_ensemble_beats_naive_stays_unchanged():
    """When ensemble beats naive, published = ensemble."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)
    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=4.0,
        naive_mape=8.0,
    )
    assert result.below_naive_baseline is False
    assert (result.published_forecast.values == ensemble.values).all()


# ---------------------------------------------------------------------------
# Change-rate clamp
# ---------------------------------------------------------------------------

def test_change_rate_clamp_caps_step():
    """Forecast that jumps > max_change_pct from last actual gets pulled back."""
    last_actual = 100.0
    # Ensemble jumps 50% on day 1 (150 > 100*1.15)
    ensemble = _mk_series([150.0, 160.0, 170.0, 180.0, 190.0, 200.0, 210.0])
    naive = _make_flat(7, 110.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_actual=last_actual,
        max_change_pct=15.0,  # max 15% change per step
    )

    assert result.change_rate_clamped is True
    # Day 1 should be clamped to <= 115.0 (last_actual * 1.15)
    assert result.published_forecast.iloc[0] <= 115.0
    # Should still be >= last_actual (won't go below)
    assert result.published_forecast.iloc[0] >= last_actual


def test_change_rate_clamp_not_triggered_small_change():
    """Small step change should NOT trigger clamp."""
    last_actual = 100.0
    ensemble = _mk_series([105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0])
    naive = _make_flat(7, 104.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_actual=last_actual,
        max_change_pct=15.0,
    )

    assert result.change_rate_clamped is False
    assert (result.published_forecast.values == ensemble.values).all()


# ---------------------------------------------------------------------------
# Monotonicity enforcement
# ---------------------------------------------------------------------------

def test_monotonicity_fixes_non_monotonic():
    """When enforce_monotonicity=True and forecast goes down, clamp to last_actual."""
    last_actual = 100.0
    # Forecast goes below last_actual (violates upward expectation)
    ensemble = _mk_series([90.0, 85.0, 80.0, 95.0, 100.0, 105.0, 110.0])
    naive = _make_flat(7, 95.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_actual=last_actual,
        enforce_monotonicity=True,
    )

    assert result.monotonicity_violations > 0
    # No value should go below last_actual
    for i in range(len(result.published_forecast)):
        assert result.published_forecast.iloc[i] >= last_actual, \
            f"Step {i}: {result.published_forecast.iloc[i]} < {last_actual}"


def test_monotonicity_disabled_no_effect():
    """With enforce_monotonicity=False, non-monotonic is allowed."""
    last_actual = 100.0
    ensemble = _mk_series([90.0, 85.0, 80.0, 95.0, 100.0, 105.0, 110.0])
    naive = _make_flat(7, 95.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_actual=last_actual,
        enforce_monotonicity=False,
    )

    assert result.monotonicity_violations == 0
    # Published should be ensemble (no monotonicity correction)
    assert result.published_forecast.iloc[0] == 90.0


# ---------------------------------------------------------------------------
# Volatility-regime blend
# ---------------------------------------------------------------------------

def test_high_vol_blends_toward_naive():
    """When vol is high, blend pulls forecast toward naive."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        vol_regime_blend=True,
        daily_returns_std=8.0,  # High vol > 5% threshold
    )

    assert result.regime_blend_factor is not None
    assert 0.0 < result.regime_blend_factor < 1.0
    # Blended forecast should be between ensemble and naive
    for i in range(7):
        assert naive.iloc[i] <= result.published_forecast.iloc[i] <= ensemble.iloc[i], \
            f"Step {i}: {result.published_forecast.iloc[i]} not between {naive.iloc[i]} and {ensemble.iloc[i]}"


def test_low_vol_no_blend():
    """Low vol should NOT trigger blending."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        vol_regime_blend=True,
        daily_returns_std=2.0,  # Low vol below threshold
    )

    assert result.regime_blend_factor is None
    assert (result.published_forecast.values == ensemble.values).all()


# ---------------------------------------------------------------------------
# Stale-data guard
# ---------------------------------------------------------------------------

def test_stale_data_detected():
    """When last_data_date is too old, stale flag is set."""
    ensemble = _make_flat(7, 105.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_data_date=datetime(2025, 1, 1),
        stale_threshold_days=14,
    )

    assert result.stale_data_detected is True


def test_fresh_data_not_stale():
    """Recent data should NOT trigger stale flag."""
    ensemble = _make_flat(7, 105.0)
    naive = _make_flat(7, 100.0)
    yesterday = datetime.now() - timedelta(days=1)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_data_date=yesterday,
        stale_threshold_days=14,
    )

    assert result.stale_data_detected is False


def test_no_last_data_date_no_stale_flag():
    """Without last_data_date, stale should be False."""
    ensemble = _make_flat(7, 105.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
    )

    assert result.stale_data_detected is False


# ---------------------------------------------------------------------------
# Combined — all features active
# ---------------------------------------------------------------------------

def test_all_features_combined():
    """Monotonicity, change-rate clamp, regime blend, and stale guard all work together."""
    last_actual = 100.0
    # Ensemble makes an unrealistic jump
    ensemble = _mk_series([130.0, 125.0, 95.0, 140.0, 150.0, 160.0, 170.0])
    naive = _make_flat(7, 105.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=8.0,
        last_actual=last_actual,
        max_change_pct=15.0,
        enforce_monotonicity=True,
        vol_regime_blend=True,
        daily_returns_std=7.0,
        last_data_date=datetime(2025, 1, 1),
        stale_threshold_days=14,
    )

    assert result.change_rate_clamped is True
    assert result.monotonicity_violations > 0
    assert result.regime_blend_factor is not None
    assert result.stale_data_detected is True

    # No value should be below last_actual
    for i in range(len(result.published_forecast)):
        assert result.published_forecast.iloc[i] >= last_actual, \
            f"Step {i}: {result.published_forecast.iloc[i]} < {last_actual}"

    # Day 1 should be clamped
    assert result.published_forecast.iloc[0] <= last_actual * 1.15


def test_naive_fallback_with_advanced_features():
    """When ensemble is worse than naive, naive is published even with advanced features."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=10.0,  # Worse
        naive_mape=5.0,
        last_actual=95.0,
        max_change_pct=15.0,
        enforce_monotonicity=True,
    )

    assert result.below_naive_baseline is True
    # Published should be naive (or close to it)
    assert (result.published_forecast.values == naive.values).all()


# ---------------------------------------------------------------------------
# Soft-blend passthrough (Bug #1 fix)
# ---------------------------------------------------------------------------

def test_soft_blend_passthrough_marginal_mape():
    """When soft_blend is enabled and MAPE is within margin, ensemble is not hard-discarded."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)
    # Ensemble slightly worse but within 5% margin of naive (5% × 5.0 = 0.25 margin_abs)
    # excess = 5.2 - 5.0 = 0.2 ≤ 0.25 → soft-blend triggers
    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.2,
        naive_mape=5.0,
        soft_blend_enabled=True,
        soft_blend_margin_pct=5.0,
    )

    # With soft-blend, should NOT fall back to pure naive
    # blend_ratio should be between 0 and 1 (partial blend)
    assert 0.0 < result.blend_ratio < 1.0
    # Published should NOT be pure naive
    assert not (result.published_forecast.values == naive.values).all()


def test_soft_blend_passthrough_disabled_uses_hard_gate():
    """When soft_blend is disabled, the hard honesty gate fires normally."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)

    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=7.0,
        naive_mape=6.0,
        soft_blend_enabled=False,
    )

    # Without soft-blend, ensemble worse than naive → fall back to naive
    assert result.blend_ratio == 0.0
    assert result.below_naive_baseline is True
    assert (result.published_forecast.values == naive.values).all()


# ---------------------------------------------------------------------------
# Blend ratio propagation (Bug #7 fix)
# ---------------------------------------------------------------------------

def test_blend_ratio_propagated_from_base_guard():
    """blend_ratio should reflect what the base guard computed, not always 1.0."""
    ensemble = _make_flat(7, 110.0)
    naive = _make_flat(7, 100.0)

    # When ensemble is worse → base guard sets blend_ratio=0.0 (pure naive)
    result = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=10.0,
        naive_mape=5.0,
    )
    assert result.blend_ratio == 0.0
    assert result.below_naive_baseline is True

    # When ensemble is better → base guard sets blend_ratio=1.0 (pure ensemble)
    result2 = evaluate_guard_advanced(
        ensemble_forecast=ensemble,
        naive_forecast=naive,
        ensemble_mape=5.0,
        naive_mape=10.0,
    )
    assert result2.blend_ratio == 1.0
    assert result2.below_naive_baseline is False
