"""Unit tests for the walk-forward backtest rewrite (Phase C Task C1).

Verifies: per-horizon MAPE, residual collection (fixes the dead-code bug
where all_residuals was never populated), sliding-origin walk-forward,
directional accuracy, and short-series handling.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.backtest import evaluate, BacktestResult
from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive
from app.services.forecasting.models.arima import ARIMAModel


def _trend_series(n=120):
    """Upward-trending series with noise — directional signal present."""
    rng = np.random.default_rng(42)
    t = np.arange(n, dtype=float)
    return pd.Series(100 + 0.5 * t + rng.normal(0, 2, n), name="y")


def test_backtest_returns_per_horizon_mape():
    y = _trend_series(120)
    models = {"naive_last": NaiveLast(), "arima": ARIMAModel()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7, 30])
    assert isinstance(result, BacktestResult)
    # Per-horizon MAPE populated for each requested horizon
    assert set(result.per_horizon_mape.keys()) == {7, 30}
    for h, model_map in result.per_horizon_mape.items():
        assert "arima" in model_map
        assert model_map["arima"] < float("inf")


def test_backtest_collects_residuals_by_horizon():
    """The old backtest left residuals empty (bug). New one must populate."""
    y = _trend_series(120)
    models = {"naive_last": NaiveLast()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7])
    assert 7 in result.residuals_by_horizon
    assert len(result.residuals_by_horizon[7]) >= 5  # enough for percentiles


def test_backtest_walk_forward_no_train_test_overlap():
    """Origin slides forward; each fold's test window must not overlap its train."""
    y = _trend_series(60)
    models = {"naive_last": NaiveLast()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7], step=7)
    assert result.n_folds >= 2
    # If overlap existed, residuals would be suspiciously small; sanity-check mape > 0
    assert result.per_model_mape["naive_last"] > 0


def test_backtest_directional_accuracy_populated():
    y = _trend_series(120)
    models = {"naive_last": NaiveLast(), "arima": ARIMAModel()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7])
    assert "arima" in result.directional_accuracy
    # On a clean uptrend, ARIMA should beat 50% directional
    assert result.directional_accuracy["arima"] >= 0.5


def test_backtest_short_series_returns_empty():
    y = pd.Series([100, 101, 102], name="y")
    result = evaluate(y, {"naive_last": NaiveLast()}, horizons=[7])
    assert result.n_folds == 0
    assert result.per_horizon_mape == {}


def test_backward_compat_per_model_mape_still_present():
    """Old callers read per_model_mape (averaged across horizons)."""
    y = _trend_series(120)
    models = {"naive_last": NaiveLast()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7, 30])
    assert "naive_last" in result.per_model_mape
    assert result.ensemble_mape is not None
    assert result.naive_mape is not None
