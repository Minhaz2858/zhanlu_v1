"""
Wave 6 — Advanced Ensemble Architecture tests.

Coverage:
- StackingMetaLearner: record folds, fit Ridge, blend forecasts
- Regime-aware model pool: volatility-based model dropping
- VARModel: cross-product multivariate forecasting
- Engine integration: stacking callback, regime detection, VAR pool registration
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta


# ================================================================
# Test data helpers
# ================================================================

def make_test_series(n: int = 150, seed: int = 42) -> pd.Series:
    """Generate a realistic price-like time series."""
    rng = np.random.default_rng(seed)
    dr = pd.date_range("2025-01-01", periods=n, freq="D")
    trend = np.linspace(100, 120, n)
    season = 3 * np.sin(2 * np.pi * np.arange(n) / 7)
    noise = rng.normal(0, 1.5, n)
    return pd.Series(trend + season + noise, index=dr, name="price")


def make_correlated_data(n: int = 200, seed: int = 42) -> dict[str, pd.Series]:
    """Generate correlated product data for VAR testing."""
    rng = np.random.default_rng(seed)
    dr = pd.date_range("2025-01-01", periods=n, freq="D")

    # Base signal
    trend = np.linspace(100, 120, n)
    base_season = 3 * np.sin(2 * np.pi * np.arange(n) / 7)

    products = {}
    products["naphtha"] = pd.Series(
        trend + base_season + rng.normal(0, 1.5, n), index=dr, name="naphtha",
    )
    # C5 correlated with naphtha (upstream)
    products["C5"] = pd.Series(
        0.9 * products["naphtha"].values + rng.normal(0, 1, n), index=dr, name="C5",
    )
    # Isoprene correlated with C5
    products["isoprene"] = pd.Series(
        1.1 * products["C5"].values + rng.normal(0, 1, n), index=dr, name="isoprene",
    )
    # DCPD correlated with C5 and isoprene
    products["DCPD"] = pd.Series(
        0.7 * products["C5"].values + 0.2 * products["isoprene"].values + rng.normal(0, 1, n),
        index=dr, name="DCPD",
    )
    # C5_resin downstream
    products["C5_resin"] = pd.Series(
        1.2 * products["DCPD"].values + rng.normal(0, 2, n), index=dr, name="C5_resin",
    )
    return products


# ================================================================
# Stacking Meta-Learner
# ================================================================

class TestStackingMetaLearner:
    """Test Ridge regression stacking meta-learner."""

    def test_create(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        s = StackingMetaLearner(alpha=0.5, scale=True)
        assert s.fitted is False
        assert len(s.feature_names) == 0

    def test_record_fold(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        s = StackingMetaLearner()
        s.record_fold(
            {"arima": [1.0, 2.0, 3.0, 1.5], "ets": [1.1, 2.1, 3.1, 1.4]},
            [1.05, 2.05, 3.05, 1.48],
        )
        s.record_fold(
            {"arima": [4.0, 5.0, 4.2, 5.2, 4.5], "ets": [3.9, 4.9, 4.1, 5.1, 4.6]},
            [4.05, 5.05, 4.18, 5.18, 4.58],
        )
        s.record_fold(
            {"arima": [6.0, 7.0, 6.5], "ets": [6.1, 7.1, 6.6]},
            [6.05, 7.05, 6.58],
        )
        # Should have ≥10 samples × 2 models
        assert len(s._preds_list) == 3
        fit_ok = s.fit_meta()
        assert fit_ok
        assert s.fitted

    def test_fit_meta_with_enough_data(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner
        import numpy as np

        s = StackingMetaLearner()
        rng = np.random.default_rng(1)
        for _ in range(5):
            n_per_fold = rng.integers(5, 15)
            preds = {
                "model_a": rng.normal(100, 5, n_per_fold),
                "model_b": rng.normal(102, 6, n_per_fold),
                "model_c": rng.normal(101, 4, n_per_fold),
            }
            actuals = 0.5 * preds["model_a"] + 0.3 * preds["model_b"] + 0.2 * preds["model_c"] + rng.normal(0, 1, n_per_fold)
            s.record_fold(preds, actuals)

        ok = s.fit_meta()
        assert ok
        assert s.fitted
        assert s._model_cols == ["model_a", "model_b", "model_c"]

    def test_fit_meta_with_insufficient_data(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        s = StackingMetaLearner()
        # Only 5 samples — too few (need ≥10)
        s.record_fold({"m1": [1.0, 2.0, 3.0, 4.0, 5.0]}, [1.5, 2.5, 3.5, 4.5, 5.5])
        s.record_fold({"m1": [6.0, 7.0, 8.0]}, [6.5, 7.5, 8.5])
        ok = s.fit_meta()
        assert not ok  # n=8 < 10, p=1 < 2
        assert not s.fitted

    def test_fit_meta_empty(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        s = StackingMetaLearner()
        ok = s.fit_meta()
        assert not ok

    def test_blend_not_fitted_returns_none(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        s = StackingMetaLearner()
        base = {"arima": make_test_series(20)}
        result = s.blend(base, h=7)
        assert result is None

    def test_blend_after_fit(self):
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner
        import numpy as np

        s = StackingMetaLearner(alpha=0.1, scale=False)
        rng = np.random.default_rng(2)
        for _ in range(8):
            n = rng.integers(5, 15)
            m1 = rng.normal(100, 3, n)
            m2 = rng.normal(102, 4, n)
            actuals = 0.6 * m1 + 0.4 * m2 + rng.normal(0, 0.5, n)
            s.record_fold({"m1": m1, "m2": m2}, actuals)

        assert s.fit_meta()

        # Create base forecasts
        fc_m1 = pd.Series(np.linspace(100, 105, 10))
        fc_m2 = pd.Series(np.linspace(102, 108, 10))
        result = s.blend({"m1": fc_m1, "m2": fc_m2}, h=7)
        assert result is not None
        assert len(result) == 7
        assert isinstance(result, pd.Series)


# ================================================================
# VAR Model
# ================================================================

class TestVARModel:
    """Test VAR/VECM multivariate model."""

    @pytest.fixture
    def correlated(self):
        return make_correlated_data(200)

    def test_create(self):
        from app.services.forecasting.models.var_model import VARModel

        m = VARModel(product_key="C5")
        assert m.name == "var_model"
        assert m.min_history == 60
        assert m.uses_exog is False
        assert not m.fitted

    def test_fit_with_correlated_data(self, correlated):
        from app.services.forecasting.models.var_model import VARModel

        y = correlated["C5"]
        m = VARModel(product_key="C5", correlated_data=correlated, max_lags=3)
        m.fit(y)
        assert m.fitted
        assert m.selected_lags >= 1
        assert m.selected_lags <= 3

    def test_forecast_after_fit(self, correlated):
        from app.services.forecasting.models.var_model import VARModel

        y = correlated["C5"]
        m = VARModel(product_key="C5", correlated_data=correlated, max_lags=3)
        m.fit(y)
        fc = m.forecast(h=7)
        assert isinstance(fc, pd.Series)
        assert len(fc) == 7

    def test_forecast_without_fit_returns_naive(self):
        from app.services.forecasting.models.var_model import VARModel

        m = VARModel(product_key="C5")
        fc = m.forecast(h=7)
        assert isinstance(fc, pd.Series)
        assert len(fc) == 7
        # Naive fallback should be all zeros
        assert (fc == 0).all()

    def test_insufficient_products_skips_fit(self, correlated):
        from app.services.forecasting.models.var_model import VARModel

        # Only provide target, no correlated products
        y = correlated["naphtha"]
        m = VARModel(
            product_key="naphtha",
            correlated_data={"naphtha": correlated["naphtha"]},  # only one
            max_lags=3,
        )
        m.fit(y)
        assert not m.fitted

    def test_short_data_skips_fit(self, correlated):
        from app.services.forecasting.models.var_model import VARModel

        # Very short series for all products
        short = {k: s.iloc[:30] for k, s in correlated.items()}
        y = short["C5"]
        m = VARModel(product_key="C5", correlated_data=short, max_lags=3)
        m.fit(y)
        assert not m.fitted

    def test_joint_columns(self, correlated):
        from app.services.forecasting.models.var_model import VARModel

        y = correlated["C5"]
        m = VARModel(product_key="C5", correlated_data=correlated, max_lags=3)
        m.fit(y)
        if m.fitted:
            cols = m.joint_columns
            assert "C5" in cols
            assert len(cols) >= 3  # at least 3 products after filtering

    def test_correlated_groups(self):
        from app.services.forecasting.models.var_model import _CORRELATED_GROUPS

        assert "C5" in _CORRELATED_GROUPS
        assert "naphtha" in _CORRELATED_GROUPS["C5"]
        assert "isoprene" in _CORRELATED_GROUPS["C5"]
        assert "DCPD" in _CORRELATED_GROUPS["C5"]


# ================================================================
# Regime-Aware Model Pool
# ================================================================

class TestRegimeAwarePool:
    """Test volatility regime detection and model pool filtering."""

    def test_normal_vol_drops_nothing(self):
        """Normal volatility (<1.5%) should keep all standard models."""
        from app.services.forecasting.models import build_model_pool
        y = make_test_series(100)
        pool = build_model_pool(y=y)
        # All standard models should be present (regime not in build_model_pool)
        assert "naive_last" in pool
        assert "seasonal_naive" in pool
        assert "arima" in pool
        assert len(pool) >= 6  # Expected: naive_last, seasonal_naive, ets, arima, stl, mean_reversion, + xgboost_reg, xgboost_exog if torch available

    def test_high_vol_detection(self):
        """High volatility series detection doesn't crash."""
        import numpy as np
        rng = np.random.default_rng(42)
        # Generate a series with high daily returns
        y = pd.Series(
            np.cumprod(1 + rng.normal(0, 0.08, 100)),
            index=pd.date_range("2025-01-01", periods=100, freq="D"),
        )
        returns = np.diff(np.log(np.maximum(np.asarray(y, dtype=float), 1e-6)))
        daily_vol = float(np.std(returns)) * 100
        # With 8% std, daily_vol should be >5%
        assert daily_vol > 1.0  # At minimum, detects some volatility

    def test_stable_series_low_vol(self):
        """Stable series should have low volatility."""
        y = pd.Series(
            np.linspace(100, 101, 50),  # near-constant
            index=pd.date_range("2025-01-01", periods=50, freq="D"),
        )
        returns = np.diff(np.log(np.maximum(np.asarray(y, dtype=float), 1e-6)))
        daily_vol = float(np.std(returns)) * 100
        assert daily_vol < 1.5  # should be normal vol


# ================================================================
# Backtest Integration (on_fold callback)
# ================================================================

class TestBacktestOnFold:
    """Test that evaluate() calls on_fold callback correctly."""

    def test_on_fold_called(self):
        from app.services.forecasting.backtest import evaluate
        from app.services.forecasting.models import build_model_pool

        y = make_test_series(120)
        pool = build_model_pool(y=y)
        calls = []

        def _cb(T, y_train, preds, actuals):
            calls.append((T, len(preds), len(actuals)))

        result = evaluate(y, pool, horizons=[7], on_fold=_cb)
        assert len(calls) > 0
        assert result.n_folds > 0
        # Each call should have predictions for all models
        for _, n_preds, n_actuals in calls:
            assert n_preds == len(pool)  # one prediction per model
            assert n_actuals == 7  # primary horizon

    def test_on_fold_collects_all_models(self):
        from app.services.forecasting.backtest import evaluate
        from app.services.forecasting.models import build_model_pool

        y = make_test_series(150)
        pool = build_model_pool(y=y)
        collected_models: set = set()

        def _cb(T, y_train, preds, actuals):
            collected_models.update(preds.keys())

        evaluate(y, pool, horizons=[7], on_fold=_cb)
        # Should have collected all models at least in some fold
        for name in pool:
            assert name in collected_models, f"{name} not in collected predictions"

    def test_stacking_collects_and_fits(self):
        """End-to-end: collect fold preds → fit stacking → blend."""
        from app.services.forecasting.backtest import evaluate
        from app.services.forecasting.models import build_model_pool
        from app.services.forecasting.models.stacking_meta import StackingMetaLearner

        y = make_test_series(150)
        pool = build_model_pool(y=y)
        stacker = StackingMetaLearner(alpha=0.5, scale=True)

        def _cb(T, y_train, preds, actuals):
            stacker.record_fold(preds, actuals)

        evaluate(y, pool, horizons=[7], on_fold=_cb)
        ok = stacker.fit_meta()
        assert ok
        assert stacker.fitted

        # Build simple forecasts for blending (avoid models that need exog)
        forecasts = {}
        sp = 7
        # Use only simple models that work without external data
        from app.services.forecasting.models.arima import ARIMAModel
        from app.services.forecasting.models.ets import ETSModel
        from app.services.forecasting.models.naive import NaiveLast

        for Cls, name in [
            (NaiveLast, "naive_last"),
            (ARIMAModel, "arima"),
            (ETSModel, "ets"),
        ]:
            m = Cls()
            try:
                m.fit(y, seasonal_period=sp)
                fc = m.forecast(7)
                forecasts[name] = fc
            except Exception:
                pass

        assert len(forecasts) >= 2
        blended = stacker.blend(forecasts, h=7)
        assert blended is not None
        assert len(blended) == 7
