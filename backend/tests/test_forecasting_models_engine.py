"""Tests for forecast model base class and individual model implementations."""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.base import ForecastModel, ModelFitError
from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive
from app.services.forecasting.models.mean_reversion import MeanReversion
from app.services.forecasting.models.ets import ETSModel
from app.services.forecasting.models.arima import ARIMAModel
from app.services.forecasting.models.stl import STLModel
from app.services.forecasting.models.xgboost_reg import XGBoostReg
from app.services.forecasting.models import build_model_pool


# ── Synthetic data helpers ────────────────────────────────────────────

def _make_sine(n: int = 200, noise: float = 0.1) -> pd.Series:
    """Sine wave with trend + noise."""
    np.random.seed(42)
    vals = np.sin(2 * np.pi * np.arange(n) / 7) * 5 + np.arange(n) * 0.03 + np.random.normal(0, noise, n)
    return pd.Series(vals, name="y")


def _make_flat(n: int = 50) -> pd.Series:
    """Constant series with tiny noise."""
    np.random.seed(42)
    return pd.Series(np.ones(n) + np.random.normal(0, 0.001, n), name="y")


def _make_short() -> pd.Series:
    return pd.Series([1.0, 2.0], name="y")


# ── Base class tests ──────────────────────────────────────────────────

class TestBaseModel:
    def test_model_is_abstract(self):
        with pytest.raises(TypeError):
            ForecastModel()  # type: ignore[abstract]

    def test_model_fit_error_is_exception(self):
        err = ModelFitError("something went wrong")
        assert isinstance(err, Exception)
        assert str(err) == "something went wrong"


# ── NaiveLast tests ───────────────────────────────────────────────────

class TestNaiveLast:
    def test_repeats_last_value(self):
        y = pd.Series([10.0, 20.0, 30.0])
        model = NaiveLast()
        model.fit(y)
        pred = model.forecast(5)
        assert len(pred) == 5
        assert np.allclose(pred.values, 30.0)

    def test_rejects_short_series(self):
        model = NaiveLast()
        with pytest.raises(ModelFitError, match="requires at least"):
            model.fit(pd.Series([1.0]))

    def test_forecast_before_fit_raises(self):
        with pytest.raises(ModelFitError, match="before fit"):
            NaiveLast().forecast(3)

    def test_min_history(self):
        assert NaiveLast.min_history == 2
        assert NaiveLast().min_history == 2  # instance also has it


# ── SeasonalNaive tests ────────────────────────────────────────────────

class TestSeasonalNaive:
    def test_repeats_seasonal_pattern(self):
        y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        model = SeasonalNaive(seasonal_period=3)
        model.fit(y)
        pred = model.forecast(6)
        # pattern repeats from end: 8,9,10,8,9,10
        expected = [8.0, 9.0, 10.0, 8.0, 9.0, 10.0]
        assert len(pred) == 6
        assert np.allclose(pred.values, expected)

    def test_auto_period_from_fit(self):
        y = pd.Series(range(1, 20), dtype=float)
        model = SeasonalNaive(seasonal_period=2)
        model.fit(y, seasonal_period=5)
        pred = model.forecast(5)
        assert len(pred) == 5

    def test_rejects_too_short(self):
        model = SeasonalNaive(seasonal_period=7)
        y = pd.Series([1.0, 2.0, 3.0])
        with pytest.raises(ModelFitError, match="requires at least"):
            model.fit(y)


# ── MeanReversion tests ───────────────────────────────────────────────

class TestMeanReversion:
    def test_returns_mean(self):
        y = pd.Series([10.0, 20.0, 30.0]*4)  # 12 points, > min_history=10
        model = MeanReversion()
        model.fit(y)
        pred = model.forecast(4)
        assert len(pred) == 4
        assert np.allclose(pred.values, 20.0)

    def test_min_history(self):
        assert MeanReversion.min_history == 10
        model = MeanReversion()
        with pytest.raises(ModelFitError):
            model.fit(pd.Series([1.0, 2.0, 3.0]))


# ── ETS tests ─────────────────────────────────────────────────────────

class TestETSModel:
    def test_fit_forecast_on_sine(self):
        y = _make_sine()
        model = ETSModel()
        model.fit(y, seasonal_period=7)
        pred = model.forecast(14)
        assert len(pred) == 14
        assert pred.notna().all()

    def test_rejects_short_series(self):
        model = ETSModel()
        with pytest.raises(ModelFitError):
            model.fit(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]))

    def test_falls_back_to_simple(self):
        # Flat series — ETS can't find seasonality but should fall back
        y = _make_flat(30)
        model = ETSModel()
        model.fit(y, seasonal_period=7)
        pred = model.forecast(5)
        assert len(pred) == 5
        assert pred.notna().all()


# ── ARIMA tests ───────────────────────────────────────────────────────

class TestARIMAModel:
    def test_fit_forecast_on_sine(self):
        y = _make_sine()
        model = ARIMAModel()
        model.fit(y)
        pred = model.forecast(10)
        assert len(pred) == 10
        assert pred.notna().all()

    def test_grid_search_finds_order(self):
        y = _make_sine(60)
        model = ARIMAModel()
        model.fit(y)
        # Should have found some (p,d,q) order
        assert hasattr(model, "_best_order")
        assert model._best_order is not None

    def test_rejects_short(self):
        with pytest.raises(ModelFitError):
            ARIMAModel().fit(pd.Series([1.0, 2.0, 3.0, 4.0]))


# ── STL tests ─────────────────────────────────────────────────────────

class TestSTLModel:
    def test_fit_forecast_on_sine(self):
        y = _make_sine()
        model = STLModel(seasonal_period=7)
        model.fit(y)
        pred = model.forecast(14)
        assert len(pred) == 14
        assert pred.notna().all()

    def test_rejects_short_series(self):
        model = STLModel(seasonal_period=7)
        with pytest.raises(ModelFitError):
            model.fit(pd.Series([1.0, 2.0, 3.0]))

    def test_forecast_before_fit(self):
        with pytest.raises(ModelFitError):
            STLModel().forecast(5)


# ── XGBoostReg tests ─────────────────────────────────────────────────

class TestXGBoostReg:
    def test_fit_forecast_on_sine(self):
        y = _make_sine(200)
        model = XGBoostReg()
        model.fit(y, seasonal_period=7)
        pred = model.forecast(10)
        assert len(pred) == 10
        assert pred.notna().all()

    def test_min_history(self):
        assert XGBoostReg.min_history == 60
        model = XGBoostReg()
        with pytest.raises(ModelFitError):
            model.fit(pd.Series(range(30), dtype=float))


# ── Model pool / registry tests ──────────────────────────────────────

class TestBuildModelPool:
    def test_returns_all_models(self):
        pool = build_model_pool(seasonal_period=7)
        assert "naive_last" in pool
        assert "seasonal_naive" in pool
        assert "ets" in pool
        assert "arima" in pool
        assert "stl" in pool
        assert "mean_reversion" in pool
        # xgboost_reg may be present if xgboost installed
        assert len(pool) >= 6

    def test_each_model_has_name(self):
        pool = build_model_pool()
        for name, model in pool.items():
            assert model.name == name

    def test_each_model_is_forecast_model(self):
        pool = build_model_pool()
        for model in pool.values():
            assert isinstance(model, ForecastModel)


# ── Model crash isolation ─────────────────────────────────────────────

class TestModelCrashIsolation:
    def test_model_exception_does_not_crash_pool(self):
        """If one model crashes, others in the pool still work."""
        y = _make_sine(100)
        pool = build_model_pool(seasonal_period=7)
        results = {}
        failures = []
        for name, model in pool.items():
            try:
                model.fit(y, seasonal_period=7)
                pred = model.forecast(7)
                results[name] = pred
            except Exception as e:
                failures.append(name)

        # Most models should succeed (at least 4 of 7)
        assert len(results) >= 4, f"Only {len(results)} models succeeded: {failures}"
