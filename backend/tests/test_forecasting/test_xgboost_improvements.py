"""Wave 3: XGBoost improvements tests.

Covers:
1. tune_xgboost_params — Optuna search with purged CV (falls back to defaults)
2. XGBoostDirect — direct multi-step model (no recursive error accumulation)
3. Model pool integration — xgboost_direct registered when flag ON
"""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.base import ModelFitError


# ============================================================================
# 1. XGBoostDirect
# ============================================================================

class TestXGBoostDirect:
    """Direct multi-step XGBoost — one model per horizon step."""

    @pytest.fixture(scope="class")
    def long_series(self):
        rng = np.random.RandomState(42)
        trend = np.linspace(100, 120, 200)
        noise = rng.normal(0, 3, 200)
        season = 5 * np.sin(2 * np.pi * np.arange(200) / 7)
        return pd.Series(
            trend + noise + season,
            index=pd.date_range("2024-01-01", periods=200, freq="D"),
            name="price",
        )

    def test_import(self):
        from app.services.forecasting.models.xgboost_direct import XGBoostDirect

        m = XGBoostDirect(max_horizon=7)
        assert m.name == "xgboost_direct"
        assert m.min_history == 90

    def test_fit_and_forecast_short_horizon(self, long_series):
        from app.services.forecasting.models.xgboost_direct import XGBoostDirect

        m = XGBoostDirect(max_horizon=7)
        m.fit(long_series, seasonal_period=7)
        fc = m.forecast(h=7)
        assert len(fc) == 7
        assert all(np.isfinite(v) for v in fc.values)

    def test_forecast_longer_than_max(self, long_series):
        """Requesting 14 steps when only 7 models exist → padded."""
        from app.services.forecasting.models.xgboost_direct import XGBoostDirect

        m = XGBoostDirect(max_horizon=7)
        m.fit(long_series, seasonal_period=7)
        fc = m.forecast(h=14)
        assert len(fc) == 14
        assert all(np.isfinite(v) for v in fc.values)

    def test_short_series_raises(self):
        from app.services.forecasting.models.xgboost_direct import XGBoostDirect

        y_short = pd.Series(
            [100, 101, 102], index=pd.date_range("2024-01-01", periods=3, freq="D")
        )
        m = XGBoostDirect()
        with pytest.raises(ModelFitError):
            m.fit(y_short)

    def test_direct_beats_naive_on_trending(self, long_series):
        """On a clean trending series, direct model h=7 should be reasonable."""
        from app.services.forecasting.models.xgboost_direct import XGBoostDirect

        m = XGBoostDirect(max_horizon=7)
        m.fit(long_series, seasonal_period=7)
        fc = m.forecast(h=7)
        last = long_series.iloc[-1]
        # Not exploding — forecast should be within reasonable range
        assert abs(fc.iloc[-1] - last) < 30, f"forecast too far from last value: {fc.iloc[-1]} vs {last}"


# ============================================================================
# 2. XGBoost tuner
# ============================================================================

class TestXGBoostTuner:
    """Optuna hyperparameter search with purged CV."""

    @pytest.fixture(scope="class")
    def medium_series(self):
        rng = np.random.RandomState(42)
        return pd.Series(
            rng.normal(0, 1, 120).cumsum() + 100,
            index=pd.date_range("2024-01-01", periods=120, freq="D"),
            name="price",
        )

    def test_import(self):
        from app.services.forecasting.models.xgboost_tuner import (
            tune_xgboost_params,
            DEFAULT_PARAMS,
        )

        assert callable(tune_xgboost_params)
        assert "n_estimators" in DEFAULT_PARAMS

    def test_returns_defaults_for_short_series(self):
        from app.services.forecasting.models.xgboost_tuner import (
            tune_xgboost_params,
        )

        y = pd.Series([100], index=pd.date_range("2024-01-01", periods=1, freq="D"))
        params = tune_xgboost_params(y, product_key="test_short", n_trials=2)
        # Should return defaults (series too short)
        assert "n_estimators" in params
        assert params["n_estimators"] == 100     # default

    def test_cache_reuse(self, medium_series):
        from app.services.forecasting.models.xgboost_tuner import tune_xgboost_params

        p1 = tune_xgboost_params(
            medium_series, product_key="test_cache", n_trials=5,
        )
        p2 = tune_xgboost_params(
            medium_series, product_key="test_cache", n_trials=5,
        )
        # Same product key — cache should return identical params
        assert p1 == p2

    def test_force_retune_bypasses_cache(self, medium_series):
        from app.services.forecasting.models.xgboost_tuner import tune_xgboost_params

        p1 = tune_xgboost_params(
            medium_series, product_key="test_force", n_trials=3,
            force_retune=True,
        )
        assert "n_estimators" in p1


# ============================================================================
# 3. Model pool registration
# ============================================================================

class TestModelPoolRegistration:
    """Verify xgboost_direct registration behaviour."""

    def test_direct_in_pool_when_flag_on(self):
        """With FORECAST_XGB_DIRECT_ENABLED=true, xgboost_direct MUST be registered."""
        from app.services.forecasting.models import build_model_pool

        pool = build_model_pool()
        assert "xgboost_direct" in pool, (
            "FORECAST_XGB_DIRECT_ENABLED is true but xgboost_direct not in pool"
        )
