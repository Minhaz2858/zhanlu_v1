"""Tests for domain signals A/B backtest (Phase F3 Task F3)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.domain_signals_ab import (
    ABResult, apply_overlay, ab_evaluate, decide,
    _IMPROVEMENT_THRESHOLD, _WIN_FRACTION_THRESHOLD,
)


def _trend_forecast(train_y: pd.Series, h: int) -> np.ndarray:
    """Naive linear-trend forecast for testing."""
    if len(train_y) < 2:
        return np.repeat(float(train_y.iloc[-1]), h)
    slope = (train_y.iloc[-1] - train_y.iloc[0]) / max(len(train_y) - 1, 1)
    last = float(train_y.iloc[-1])
    return np.array([last + slope * (i + 1) for i in range(h)])


def test_apply_overlay_zero_naphtha_is_seasonal_only():
    """F3: causal chain returns 0 when no naphtha delta; seasonal adds separately."""
    base = 1000.0
    as_of = pd.Timestamp("2026-08-04")
    out = apply_overlay(base, product_id="isoprene", as_of_date=as_of, naphtha_pct_change=None)
    # Aug → use seasonal rule. isoprene has no Aug rule (verified separately)
    # so total adjustment should be 0.
    # Just check the call doesn't crash and returns a sane value.
    assert isinstance(out, float)
    assert abs(out - 1000.0) < 1.0 or out == 1000.0


def test_apply_overlay_with_naphtha_delta_propagates():
    """F3: naphtha +10% → isoprene gets +7.48% (0.88 × 0.85)."""
    base = 1000.0
    as_of = pd.Timestamp("2026-08-04")
    out = apply_overlay(base, product_id="isoprene", as_of_date=as_of, naphtha_pct_change=10.0)
    # isoprene: (0.88, 0.85) → elasticity = 0.748
    # adjustment = +10% × 0.748 = +7.48%
    # total = +7.48% (no seasonal for Aug)
    expected = 1000.0 * (1 + 0.0748)
    assert abs(out - expected) < 0.1, f"Expected {expected}, got {out}"


def test_apply_overlay_negative_naphtha_reduces_forecast():
    """F3: naphtha -5% → isoprene gets -3.74% (negative adjustment)."""
    base = 1000.0
    as_of = pd.Timestamp("2026-08-04")
    out = apply_overlay(base, product_id="isoprene", as_of_date=as_of, naphtha_pct_change=-5.0)
    expected = 1000.0 * (1 - 0.0374)
    assert abs(out - expected) < 0.1


def test_apply_overlay_unknown_product_returns_base():
    """F3: unknown product has no elasticity → no causal adjustment."""
    base = 1000.0
    as_of = pd.Timestamp("2026-08-04")
    out = apply_overlay(base, product_id="nonexistent_product_xyz", as_of_date=as_of, naphtha_pct_change=10.0)
    assert abs(out - 1000.0) < 0.1


def test_ab_evaluate_runs_and_returns_metrics():
    """F3: A/B evaluation returns MAPE on/off for both branches."""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2025-06-01", periods=200, freq="D")
    y = pd.Series(1000 + np.cumsum(rng.normal(0, 5, 200)), index=dates)
    naphtha = pd.Series(500 + np.cumsum(rng.normal(0, 2, 200)), index=dates)
    result = ab_evaluate(y, naphtha, _trend_forecast, product_id="isoprene", horizons=(7,))
    assert isinstance(result, ABResult)
    assert result.n_origins > 0
    assert result.mape_off > 0
    assert np.isfinite(result.mape_on)


def test_decide_enable_when_most_products_improve():
    """F3: >= 60% win-rate + median improvement >= 5% → recommend enable."""
    results = []
    for i in range(10):
        # 8 winners, 2 losers
        if i < 8:
            results.append(ABResult(
                product_id=f"winner_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.18,  # 10% improvement
                improvement_pct=0.10,
                dir_acc_off=0.5, dir_acc_on=0.6,
            ))
        else:
            results.append(ABResult(
                product_id=f"loser_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.22,  # -10% (worse)
                improvement_pct=-0.10,
                dir_acc_off=0.5, dir_acc_on=0.4,
            ))
    decision = decide(results)
    assert decision["recommendation"] == "enable"
    assert decision["win_fraction"] == 0.8
    assert decision["median_improvement_pct"] >= _IMPROVEMENT_THRESHOLD


def test_decide_leave_off_when_neutral():
    """F3: median improvement at exactly threshold but tiny → leave off.

    True neutral: 5 products at +0.05, 5 at -0.05. Median = 0.0, well below
    the 5% threshold. Win fraction = 0.5, below the 60% threshold.
    """
    results = []
    for i in range(5):
        results.append(ABResult(
            product_id=f"winner_{i}", n_origins=5,
            mape_off=0.20, mape_on=0.19,
            improvement_pct=0.05,
            dir_acc_off=0.5, dir_acc_on=0.5,
        ))
    for i in range(5):
        results.append(ABResult(
            product_id=f"loser_{i}", n_origins=5,
            mape_off=0.20, mape_on=0.21,
            improvement_pct=-0.05,
            dir_acc_off=0.5, dir_acc_on=0.5,
        ))
    decision = decide(results)
    # Median = 0.0, win_fraction = 0.5 (in 0.40-0.60 "mixed" zone)
    assert decision["recommendation"] == "leave_off"
    assert decision["median_improvement_pct"] == 0.0


def test_decide_leave_off_when_harmful():
    """F3: most products lose → recommend leave off."""
    results = []
    for i in range(10):
        if i < 3:
            results.append(ABResult(
                product_id=f"winner_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.18,
                improvement_pct=0.10,
                dir_acc_off=0.5, dir_acc_on=0.5,
            ))
        else:
            results.append(ABResult(
                product_id=f"loser_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.22,
                improvement_pct=-0.10,
                dir_acc_off=0.5, dir_acc_on=0.4,
            ))
    decision = decide(results)
    assert decision["recommendation"] == "leave_off"
    assert decision["win_fraction"] < _WIN_FRACTION_THRESHOLD


def test_decide_leave_off_when_mixed():
    """F3: mixed results (40-60% wins) → leave off until per-product gating."""
    results = []
    for i in range(10):
        if i < 5:
            results.append(ABResult(
                product_id=f"winner_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.18,
                improvement_pct=0.10,
                dir_acc_off=0.5, dir_acc_on=0.5,
            ))
        else:
            results.append(ABResult(
                product_id=f"loser_{i}", n_origins=5,
                mape_off=0.20, mape_on=0.22,
                improvement_pct=-0.10,
                dir_acc_off=0.5, dir_acc_on=0.4,
            ))
    decision = decide(results)
    assert decision["recommendation"] == "leave_off"


def test_decide_empty_results():
    """F3: empty results → leave off (no data)."""
    decision = decide([])
    assert decision["recommendation"] == "leave_off"