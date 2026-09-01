"""Unit tests for per-horizon ensemble weighting (Phase C Task C2).

Verifies: per-step weights vary by horizon, floor removed (bad models get
~0 weight via ratio gating), and flat-weight backward compatibility.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.ensemble import blend, EnsembleResult


def _fc(name, vals):
    return pd.Series(vals, name=name)


def test_per_horizon_weights_vary_by_step():
    """ARIMA best at h=1..7, ETS best at h=20..30 -> weights shift along horizon."""
    h = 30
    forecasts = {
        "arima": _fc("arima", np.linspace(100, 115, h)),
        "ets": _fc("ets", np.linspace(100, 118, h)),
    }
    # ARIMA low error at 7d, high at 30d; ETS opposite
    per_h = {
        7: {"arima": 0.03, "ets": 0.12},
        30: {"arima": 0.20, "ets": 0.05},
    }
    result = blend(
        forecasts,
        per_model_error={"arima": 0.10, "ets": 0.10},
        per_model_error_by_horizon=per_h,
    )
    assert isinstance(result, EnsembleResult)
    # weights is now per-step list
    assert isinstance(result.weights["arima"], list)
    assert len(result.weights["arima"]) == h
    # At step 7 ARIMA should outweigh ETS; at step 30 vice-versa
    assert result.weights["arima"][6] > result.weights["ets"][6]
    assert result.weights["arima"][29] < result.weights["ets"][29]


def test_floor_removed_bad_model_gets_zero():
    """A model 3x worse than best gets ~0 weight (no floor rescue)."""
    forecasts = {"good": _fc("good", [100] * 7), "bad": _fc("bad", [100] * 7)}
    result = blend(forecasts, per_model_error={"good": 0.05, "bad": 0.20})
    assert result.weights["bad"] < 0.01
    assert result.weights["good"] > 0.99


def test_flat_weights_backward_compat():
    """No per-horizon dict -> flat weights (old behavior)."""
    forecasts = {"a": _fc("a", [100] * 7), "b": _fc("b", [101] * 7)}
    result = blend(forecasts, per_model_error={"a": 0.05, "b": 0.06})
    assert isinstance(result.weights["a"], float)
    assert abs(sum(result.weights.values()) - 1.0) < 1e-9


def test_per_horizon_point_forecast_is_weighted_blend():
    """The blended point forecast should reflect per-step weights."""
    h = 7
    forecasts = {
        "a": _fc("a", [100.0] * h),
        "b": _fc("b", [200.0] * h),
    }
    per_h = {7: {"a": 0.01, "b": 0.02}}  # b is worse
    result = blend(
        forecasts,
        per_model_error={"a": 0.05, "b": 0.10},
        per_model_error_by_horizon=per_h,
    )
    # a has lower error -> point forecast closer to 100 than 200
    assert result.point_forecast.iloc[0] < 150


def test_per_horizon_all_inf_errors_falls_back_equal():
    """If all per-horizon errors are inf, fall back to equal weights."""
    forecasts = {"a": _fc("a", [100] * 7), "b": _fc("b", [101] * 7)}
    per_h = {7: {"a": float("inf"), "b": float("inf")}}
    result = blend(
        forecasts,
        per_model_error={"a": float("inf"), "b": float("inf")},
        per_model_error_by_horizon=per_h,
    )
    # Equal weights
    assert abs(result.weights["a"][0] - result.weights["b"][0]) < 1e-9
