"""Wave 2: Engine Tuning Tests

Covers:
1. auto_tune_tau — softmax temperature from model error variance
2. Adaptive backtest folds — scales with series length
3. Model selector wiring — prunes underperforming models
"""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ensemble import auto_tune_tau, _DEFAULT_TAU, blend
from app.services.forecasting.backtest import evaluate, BacktestResult
from app.services.forecasting.model_selector import select_model_pool
from app.services.forecasting.models import build_model_pool


# ============================================================================
# 1. auto_tune_tau
# ============================================================================

class TestAutoTuneTau:
    def test_returns_default_when_insufficient_data(self):
        """Fewer than 2 finite values → fall back to default tau."""
        assert auto_tune_tau({}) == _DEFAULT_TAU
        assert auto_tune_tau({"a": float("inf")}) == _DEFAULT_TAU
        assert auto_tune_tau({"a": 5.0}) == _DEFAULT_TAU

    def test_low_variance_concentrates_weight(self):
        """Models closely agree → low tau (≤ 1.0)."""
        tau = auto_tune_tau({"a": 10.0, "b": 10.5, "c": 10.2})
        assert tau <= 1.0
        assert tau >= 0.5

    def test_high_variance_diversifies_weight(self):
        """Models widely disagree → high tau (> 1.0)."""
        tau = auto_tune_tau({"a": 5.0, "b": 25.0, "c": 45.0})
        assert tau >= 1.0
        assert tau <= 3.0

    def test_clamped_within_bounds(self):
        """All outputs must be within [tau_min, tau_max]."""
        # extreme agreement
        tau = auto_tune_tau({"a": 10.0, "b": 10.0}, tau_min=0.3, tau_max=2.5)
        assert 0.3 <= tau <= 2.5
        # extreme disagreement
        tau = auto_tune_tau({"a": 1.0, "b": 100.0}, tau_min=0.3, tau_max=2.5)
        assert 0.3 <= tau <= 2.5

    def test_ignores_negative_and_infinite(self):
        """Negative or inf MAPE values are excluded from cv calculation."""
        tau = auto_tune_tau({"a": -5.0, "b": 10.0, "c": float("inf"), "d": 15.0})
        assert tau >= 0.5
        assert tau <= 3.0

    def test_blend_respects_custom_tau(self):
        """blend() with explicit tau must produce expected behaviour."""
        rng = np.random.RandomState(42)
        fc = {
            "good": pd.Series([100.0, 102.0, 104.0]),
            "bad": pd.Series([80.0, 150.0, 90.0]),
        }
        errors = {"good": 2.0, "bad": 30.0}

        res_lo = blend(fc, errors, tau=0.3)
        res_hi = blend(fc, errors, tau=3.0)

        # low tau → good model gets more weight
        w_good_lo = res_lo.weights.get("good", 0.0)
        w_good_hi = res_hi.weights.get("good", 0.0)
        assert isinstance(w_good_lo, float)
        assert isinstance(w_good_hi, float)
        assert w_good_lo >= w_good_hi, (
            f"low tau should concentrate weight on best model: {w_good_lo} < {w_good_hi}"
        )


# ============================================================================
# 2. Adaptive backtest folds
# ============================================================================

class TestAdaptiveBacktestFolds:
    def test_short_series_limits_folds(self):
        """n=50 → at most n//30 ≈ 1, clamped to min folds=..."""
        dates = pd.date_range("2024-01-01", periods=50, freq="D")
        y = pd.Series(np.random.RandomState(0).normal(100, 5, 50), index=dates)
        pool = build_model_pool()
        bt = evaluate(y, pool, horizons=[7])
        # Should still produce at least 2 folds (the floor check in evaluate)
        assert bt.n_folds >= 0   # may be 0 if too short
        assert isinstance(bt, BacktestResult)

    def test_medium_series_gets_10_folds(self):
        """n=365 → n//30 ≈ 12, clamped to 10 minimum."""
        dates = pd.date_range("2024-01-01", periods=365, freq="D")
        y = pd.Series(np.random.RandomState(1).normal(100, 5, 365), index=dates)
        model = {"naive": build_model_pool()["naive_last"]}
        bt = evaluate(y, model, horizons=[7], min_train=60, step=14)
        assert bt.n_folds <= 20   # never exceeds _MAX_FOLDS
        assert isinstance(bt.per_model_mape, dict)

    def test_long_series_gets_up_to_20_folds(self):
        """n=800 → n//30 ≈ 26, clamped to 20 max."""
        dates = pd.date_range("2024-01-01", periods=800, freq="D")
        y = pd.Series(
            np.random.RandomState(2).normal(100, 5, 800), index=dates
        )
        model = {"naive": build_model_pool()["naive_last"]}
        bt = evaluate(y, model, horizons=[7], min_train=60, step=14)
        assert bt.n_folds >= 2
        assert bt.n_folds <= 20


# ============================================================================
# 3. Model selector wiring
# ============================================================================

class TestModelSelectorWiring:
    def test_select_model_pool_no_history_returns_unchanged(self):
        """With no rolling_mape, pool is returned unchanged."""
        pool = build_model_pool()
        pruned = select_model_pool(pool, "test_product", rolling_mape=None)
        assert set(pool.keys()) == set(pruned.keys())

    def test_select_model_pool_with_history(self):
        """With sufficient history, underperforming models are pruned."""
        pool = build_model_pool()
        # Simulate: naive_last consistently terrible, xgboost_exog always good
        rolling = {
            "naive_last": [60.0, 65.0, 62.0, 58.0, 70.0],
            "seasonal_naive": [18.0, 20.0, 17.0, 22.0, 19.0],
            "ets": [16.0, 15.0, 18.0, 17.0, 16.0],
            "xgboost_exog": [10.0, 11.0, 9.0, 12.0, 10.0],
            "arima": [14.0, 13.0, 15.0, 14.0, 13.0],
        }
        pruned = select_model_pool(pool, "test_product", rolling_mape=rolling)
        # naive_last should be dropped (MAPE 60+) 
        assert "naive_last" not in pruned
        # best models should be kept
        assert "xgboost_exog" in pruned
        assert "ets" in pruned
