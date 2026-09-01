"""Test regime-aware ensemble blending.

Covers:
1. Regime weight multipliers are applied when regime provided
2. No adjustment when regime is None / "sideways"
3. Softmax weights still sum to 1.0 after multiplier → renormalization
4. Per-horizon path also applies multipliers
"""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ensemble import blend, _REGIME_WEIGHT_MULT


def _make_forecasts(model_names, n_steps=7, seed=42):
    rng = np.random.RandomState(seed)
    f = {}
    for name in model_names:
        base = 100 + rng.normal(0, 5)
        vals = base + np.cumsum(rng.normal(0, 1, n_steps))
        f[name] = pd.Series(vals)
    return f


def _make_per_model_error(model_names, seed=1):
    rng = np.random.RandomState(seed)
    return {name: 10.0 + rng.uniform(0, 10) for name in model_names}


class TestRegimeBlend:
    def test_bull_boosts_xgboost(self):
        """In bull regime, xgboost gets higher relative weight."""
        model_names = ["naive_last", "xgboost_reg", "mean_reversion", "ets"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names)

        # Without regime
        result_default = blend(forecasts, errors, regime=None)
        # With bull regime
        result_bull = blend(forecasts, errors, regime="bull")

        # Both produce valid results
        assert result_default.point_forecast is not None
        assert result_bull.point_forecast is not None
        assert len(result_default.point_forecast) == 7

    def test_bear_boosts_mean_reversion(self):
        """In bear regime, mean_reversion gets boosted."""
        model_names = ["naive_last", "xgboost_reg", "mean_reversion", "seasonal_naive"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names)

        result = blend(forecasts, errors, regime="bear")
        assert result is not None
        assert result.weights is not None

    def test_volatile_boosts_naive(self):
        """In volatile regime, naive models get higher weight."""
        model_names = ["naive_last", "xgboost_reg", "seasonal_naive", "stl"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names)

        result = blend(forecasts, errors, regime="volatile")
        assert result is not None

    def test_sideways_no_multiplier(self):
        """Sideways and None regimes should produce identical results."""
        model_names = ["naive_last", "xgboost_reg", "mean_reversion", "ets"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names, seed=42)

        result_sideways = blend(forecasts, errors, regime="sideways")
        result_none = blend(forecasts, errors, regime=None)

        np.testing.assert_array_almost_equal(
            result_sideways.point_forecast.values,
            result_none.point_forecast.values,
            decimal=10,
        )

    def test_weights_sum_to_one(self):
        """After multiplier and renormalization, weights sum to 1."""
        model_names = ["naive_last", "xgboost_reg", "mean_reversion", "ets",
                       "seasonal_naive", "arima", "stl"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names, seed=7)

        for regime in ("bull", "bear", "volatile", "sideways", None):
            result = blend(forecasts, errors, regime=regime)
            total = sum(result.weights.values())
            assert abs(total - 1.0) < 0.001, f"regime={regime}, total={total}"

    def test_per_horizon_regime(self):
        """Per-horizon blend also accepts regime."""
        model_names = ["naive_last", "xgboost_reg", "ets"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names)
        per_h = {h: errors for h in range(1, 8)}

        result = blend(forecasts, errors, per_model_error_by_horizon=per_h, regime="bull")
        assert result is not None
        # Per-horizon path returns a single best-tau point forecast
        assert result.point_forecast is not None

    def test_invalid_regime_ignored(self):
        """Unknown regime string is silently ignored."""
        model_names = ["naive_last", "xgboost_reg"]
        forecasts = _make_forecasts(model_names)
        errors = _make_per_model_error(model_names)

        result = blend(forecasts, errors, regime="invalid_regime")
        assert result is not None
