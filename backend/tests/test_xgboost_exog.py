"""Tests for XGBoost exogenous feature support + xgboost_exog registration."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.xgboost_reg import XGBoostReg
from app.services.forecasting.models import build_model_pool


def _make_y(n: int = 80) -> pd.Series:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    values = 5000 + np.sin(np.arange(n) * 0.1) * 100 + np.random.rand(n) * 20
    return pd.Series(values, index=dates, name="y")


def _make_exog(n: int = 80, n_features: int = 3) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    data = {f"exog_{i}": np.random.rand(n) * 100 for i in range(n_features)}
    return pd.DataFrame(data, index=dates)


class TestXGBoostExogFit:
    def test_fit_without_exog_works(self):
        model = XGBoostReg()
        y = _make_y(80)
        model.fit(y, seasonal_period=7)
        assert model._model is not None
        assert not model.uses_exog

    def test_fit_with_exog_sets_uses_exog_flag(self):
        model = XGBoostReg()
        y = _make_y(80)
        exog = _make_exog(80, 3)
        model.fit(y, seasonal_period=7, exog=exog)
        assert model.uses_exog

    def test_fit_with_exog_stores_feature_names(self):
        model = XGBoostReg()
        y = _make_y(80)
        exog = _make_exog(80, 3)
        model.fit(y, seasonal_period=7, exog=exog)
        assert len(model._exog_feature_names) == 3
        assert model._exog_feature_names == ["exog_0", "exog_1", "exog_2"]

    def test_fit_with_exog_no_nan_y_alignment(self):
        model = XGBoostReg()
        y = _make_y(80)
        exog = _make_exog(80, 3)
        model.fit(y, seasonal_period=7, exog=exog)
        assert model._last_values is not None
        assert len(model._last_values) <= 80

    def test_fit_with_empty_exog(self):
        model = XGBoostReg()
        y = _make_y(80)
        model.fit(y, seasonal_period=7, exog=pd.DataFrame())
        assert not model.uses_exog


class TestXGBoostExogForecast:
    def test_forecast_without_exog_works(self):
        model = XGBoostReg()
        y = _make_y(80)
        model.fit(y, seasonal_period=7)
        pred = model.forecast(7)
        assert len(pred) == 7

    def test_forecast_with_exog_future(self):
        model = XGBoostReg()
        y = _make_y(80)
        exog = _make_exog(80, 3)
        model.fit(y, seasonal_period=7, exog=exog)
        exog_future = _make_exog(7, 3)
        pred = model.forecast(7, exog_future=exog_future)
        assert len(pred) == 7

    def test_forecast_falls_back_to_last_exog_when_no_future(self):
        # With no exog_future passed, should fall back to last known exog row
        model = XGBoostReg()
        y = _make_y(80)
        exog = _make_exog(80, 3)
        model.fit(y, seasonal_period=7, exog=exog)
        pred = model.forecast(7)  # No exog_future
        assert len(pred) == 7


class TestBuildModelPool:
    def test_xgboost_reg_is_registered(self):
        pool = build_model_pool()
        assert "xgboost_reg" in pool

    def test_xgboost_exog_is_registered(self):
        pool = build_model_pool()
        assert "xgboost_exog" in pool
        assert pool["xgboost_exog"].name == "xgboost_exog"

    def test_xgboost_exog_is_different_entry(self):
        pool = build_model_pool()
        assert pool["xgboost_reg"] is not pool["xgboost_exog"]


class TestClassAttribute:
    def test_uses_exog_has_correct_type(self):
        model = XGBoostReg()
        assert isinstance(model.uses_exog, bool)
