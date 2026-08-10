"""Test XGBoostReg tuning integration."""

import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.models.xgboost_reg import XGBoostReg
from app.services.forecasting.models.base import ModelFitError


@pytest.fixture(scope="class")
def synthetic_series():
    rng = np.random.RandomState(42)
    trend = np.linspace(100, 120, 200)
    noise = rng.normal(0, 3, 200)
    season = 5 * np.sin(2 * np.pi * np.arange(200) / 7)
    return pd.Series(
        trend + noise + season,
        index=pd.date_range("2024-01-01", periods=200, freq="D"),
        name="price",
    )


class TestXGBoostReg:
    def test_fit_default_params(self, synthetic_series):
        m = XGBoostReg()
        m.fit(synthetic_series, seasonal_period=7)
        assert m._model is not None
        assert m._model.n_estimators == 100
        assert m._model.max_depth == 3

    def test_product_key_accepted(self, synthetic_series):
        m = XGBoostReg()
        m.fit(synthetic_series, seasonal_period=7, product_key="test_product")
        assert m._product_key == "test_product"

    def test_forecast_shape(self, synthetic_series):
        m = XGBoostReg()
        m.fit(synthetic_series, seasonal_period=7)
        pred = m.forecast(7)
        assert len(pred) == 7

    def test_min_history_enforced(self):
        m = XGBoostReg()
        short = pd.Series([100.0, 101.0], name="short")
        with pytest.raises(ModelFitError):
            m.fit(short, seasonal_period=7)

    def test_tuner_importable(self):
        from app.services.forecasting.models.xgboost_tuner import tune_xgboost_params
        assert callable(tune_xgboost_params)

    def test_flag_defaults_off(self, synthetic_series):
        m = XGBoostReg()
        m.fit(synthetic_series, seasonal_period=7, product_key="test_none")
        assert m._tuned_params is None
