"""Test regime-adaptive ensemble weights.

Validates that the regime-aware weight multipliers in ensemble.blend()
shift model weights based on the detected regime label. The regime detector
outputs "bull", "bear", "volatile", "sideways" — and the ensemble applies
different weight multipliers for each.

Key behavior:
  - volatile: naive_last gets 1.5x boost, xgboost models get 0.6x dampened
  - bull: xgboost models get 1.3x boost, mean_reversion gets 0.7x
  - bear: mean_reversion gets 1.3x boost, xgboost gets 0.8x
  - sideways: no adjustment (equal opportunity)
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ensemble import (
    blend,
    EnsembleResult,
    _REGIME_WEIGHT_MULT,
    _apply_regime_multipliers,
)


class TestApplyRegimeMultipliers:
    """Unit tests for _apply_regime_multipliers()."""

    def test_no_regime_returns_unchanged(self):
        """When regime is None, weights should be unchanged."""
        names = ["xgboost_reg", "naive_last", "seasonal_naive"]
        weights = np.array([0.5, 0.3, 0.2])
        result = _apply_regime_multipliers(names, weights, None)
        np.testing.assert_array_almost_equal(result, weights)

    def test_unknown_regime_returns_unchanged(self):
        """When regime is not in the table, weights should be unchanged."""
        names = ["xgboost_reg", "naive_last"]
        weights = np.array([0.6, 0.4])
        result = _apply_regime_multipliers(names, weights, "unknown_regime")
        np.testing.assert_array_almost_equal(result, weights)

    def test_volatile_boosts_naive_dampens_xgboost(self):
        """In volatile regime, naive_last should get more weight, xgboost less."""
        names = ["xgboost_reg", "naive_last", "seasonal_naive", "mean_reversion"]
        weights = np.array([0.4, 0.3, 0.2, 0.1])
        result = _apply_regime_multipliers(names, weights, "volatile")

        # naive_last should have higher weight than before
        naive_idx = names.index("naive_last")
        xgb_idx = names.index("xgboost_reg")
        assert result[naive_idx] > weights[naive_idx]
        assert result[xgb_idx] < weights[xgb_idx]
        # Should still sum to 1
        assert abs(result.sum() - 1.0) < 1e-10

    def test_bull_boosts_xgboost_dampens_mean_reversion(self):
        """In bull regime, xgboost should get more weight, mean_reversion less."""
        names = ["xgboost_reg", "naive_last", "mean_reversion"]
        weights = np.array([0.3, 0.4, 0.3])
        result = _apply_regime_multipliers(names, weights, "bull")

        xgb_idx = names.index("xgboost_reg")
        mr_idx = names.index("mean_reversion")
        assert result[xgb_idx] > weights[xgb_idx]
        assert result[mr_idx] < weights[mr_idx]
        assert abs(result.sum() - 1.0) < 1e-10

    def test_bear_boosts_mean_reversion_dampens_xgboost(self):
        """In bear regime, mean_reversion should get more weight, xgboost less."""
        names = ["xgboost_reg", "naive_last", "mean_reversion"]
        weights = np.array([0.4, 0.3, 0.3])
        result = _apply_regime_multipliers(names, weights, "bear")

        mr_idx = names.index("mean_reversion")
        xgb_idx = names.index("xgboost_reg")
        assert result[mr_idx] > weights[mr_idx]
        assert result[xgb_idx] < weights[xgb_idx]
        assert abs(result.sum() - 1.0) < 1e-10

    def test_sideways_no_adjustment(self):
        """In sideways regime, weights should be unchanged (empty multiplier table)."""
        names = ["xgboost_reg", "naive_last", "seasonal_naive"]
        weights = np.array([0.5, 0.3, 0.2])
        result = _apply_regime_multipliers(names, weights, "sideways")
        np.testing.assert_array_almost_equal(result, weights)

    def test_pattern_matching_in_name(self):
        """Multipliers should match partial names (e.g., 'xgboost_exog' matches 'xgboost')."""
        names = ["xgboost_exog", "naive_last"]
        weights = np.array([0.5, 0.5])
        result = _apply_regime_multipliers(names, weights, "volatile")

        # xgboost_exog should be dampened (matches "xgboost" pattern)
        xgb_idx = names.index("xgboost_exog")
        assert result[xgb_idx] < weights[xgb_idx]
        assert abs(result.sum() - 1.0) < 1e-10


class TestBlendWithRegime:
    """Integration tests for blend() with regime parameter."""

    def _make_forecasts(self, h=7):
        """Create synthetic forecasts with different characteristics."""
        np.random.seed(42)
        base = 100.0
        return {
            "xgboost_reg": pd.Series(base + np.cumsum(np.random.normal(0, 1, h)), name="xgboost_reg"),
            "naive_last": pd.Series([base] * h, name="naive_last"),
            "seasonal_naive": pd.Series(base + np.sin(np.linspace(0, 2 * np.pi, h)) * 5, name="seasonal_naive"),
        }

    def test_blend_without_regime(self):
        """blend() without regime should use standard softmax weights."""
        forecasts = self._make_forecasts()
        errors = {"xgboost_reg": 5.0, "naive_last": 8.0, "seasonal_naive": 10.0}
        result = blend(forecasts, errors)

        assert isinstance(result, EnsembleResult)
        assert len(result.point_forecast) == 7
        assert result.weights["xgboost_reg"] > result.weights["naive_last"]  # lower error = higher weight

    def test_blend_with_volatile_regime(self):
        """blend() with volatile regime should boost naive_last weight."""
        forecasts = self._make_forecasts()
        errors = {"xgboost_reg": 5.0, "naive_last": 8.0, "seasonal_naive": 10.0}

        result_no_regime = blend(forecasts, errors)
        result_volatile = blend(forecasts, errors, regime="volatile")

        # In volatile regime, naive_last should get more weight than without regime
        assert result_volatile.weights["naive_last"] > result_no_regime.weights["naive_last"]

    def test_blend_with_bull_regime(self):
        """blend() with bull regime should boost xgboost weight."""
        forecasts = self._make_forecasts()
        errors = {"xgboost_reg": 5.0, "naive_last": 8.0, "seasonal_naive": 10.0}

        result_no_regime = blend(forecasts, errors)
        result_bull = blend(forecasts, errors, regime="bull")

        # In bull regime, xgboost should get more weight than without regime
        assert result_bull.weights["xgboost_reg"] > result_no_regime.weights["xgboost_reg"]

    def test_blend_with_bear_regime(self):
        """blend() with bear regime should boost mean_reversion weight.

        Note: mean_reversion must have error <= 2x best_error to survive gating.
        """
        forecasts = self._make_forecasts()
        forecasts["mean_reversion"] = pd.Series([100.0] * 7, name="mean_reversion")
        # mean_reversion error = 7.0 (between xgboost 5.0 and naive 8.0)
        # 7.0 <= 2*5.0 = 10.0 → survives gating
        errors = {"xgboost_reg": 5.0, "naive_last": 8.0, "seasonal_naive": 10.0, "mean_reversion": 7.0}

        result_no_regime = blend(forecasts, errors)
        result_bear = blend(forecasts, errors, regime="bear")

        # In bear regime, mean_reversion should get more weight than without regime
        assert result_bear.weights["mean_reversion"] > result_no_regime.weights["mean_reversion"]

    def test_different_regimes_produce_different_forecasts(self):
        """Different regimes should produce different blended forecasts."""
        forecasts = self._make_forecasts()
        errors = {"xgboost_reg": 5.0, "naive_last": 8.0, "seasonal_naive": 10.0}

        result_bull = blend(forecasts, errors, regime="bull")
        result_bear = blend(forecasts, errors, regime="bear")
        result_volatile = blend(forecasts, errors, regime="volatile")

        # At least one step should differ between regimes
        bull_vals = result_bull.point_forecast.values
        bear_vals = result_bear.point_forecast.values
        volatile_vals = result_volatile.point_forecast.values

        assert not np.allclose(bull_vals, bear_vals, atol=0.1)
        assert not np.allclose(bull_vals, volatile_vals, atol=0.1)


class TestRegimeDetectorLabels:
    """Test that regime detector labels match ensemble multiplier keys."""

    def test_all_detector_labels_in_multiplier_table(self):
        """Every regime label the detector can output must have a multiplier entry."""
        from app.services.forecasting.regime_detector import detect_regime

        # The detector outputs: "volatile", "bull", "bear", "sideways"
        detector_labels = {"volatile", "bull", "bear", "sideways"}
        multiplier_keys = set(_REGIME_WEIGHT_MULT.keys())

        missing = detector_labels - multiplier_keys
        assert not missing, f"Regime labels missing from _REGIME_WEIGHT_MULT: {missing}"
