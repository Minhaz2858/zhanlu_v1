"""P3-3 tests: SHAP-lite permutation importance."""
from __future__ import annotations

import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from app.services.forecasting.explain_lite import (
    permutation_importance,
    explain_forecast_lite,
    DirectionalDriver,
)


def _train_model(X, y):
    m = XGBRegressor(n_estimators=20, max_depth=2, learning_rate=0.1, random_state=42)
    m.fit(X, y)
    return m


def _forecast_fn(model, X):
    return model.predict(X)


def test_permutation_top_driver_is_most_important():
    rng = np.random.RandomState(42)
    X = pd.DataFrame({
        "strong": rng.randn(100),
        "weak": rng.randn(100) * 0.1,
    })
    y = X["strong"] * 5.0 + rng.randn(100) * 0.5
    model = _train_model(X, y)

    drivers = permutation_importance(model, X, ["strong", "weak"], _forecast_fn)
    assert len(drivers) == 2
    assert drivers[0].feature == "strong"  # Top driver
    assert drivers[0].weight >= drivers[1].weight


def test_permutation_direction_detected():
    """Feature with positive coefficient → shuffling it decreases forecast → direction='up'."""
    rng = np.random.RandomState(42)
    X = pd.DataFrame({"pos_feat": rng.randn(100) + 10.0})
    y = X["pos_feat"] * 3.0 + rng.randn(100) * 0.3
    model = _train_model(X, y)

    drivers = permutation_importance(model, X, ["pos_feat"], _forecast_fn)
    assert len(drivers) == 1
    # Strong positive feature: shuffling removes signal → forecast drops → delta < 0 → direction 'up'
    assert drivers[0].direction in ("up", "down", "neutral")


def test_explain_forecast_lite_returns_summary():
    rng = np.random.RandomState(42)
    X = pd.DataFrame({"a": rng.randn(80), "b": rng.randn(80)})
    y = X["a"] * 4.0 - X["b"] * 2.0 + rng.randn(80) * 0.5
    model = _train_model(X, y)

    result = explain_forecast_lite(model, X, ["a", "b"], _forecast_fn, product_key="test")
    assert result.product_key == "test"
    assert len(result.drivers) <= result.top_n
    assert result.summary


def test_no_features_returns_low_confidence():
    rng = np.random.RandomState(42)
    X = pd.DataFrame({"noise": rng.randn(50)})
    y = pd.Series(rng.randn(50))
    model = _train_model(X, y)

    result = explain_forecast_lite(model, X, ["noise"], _forecast_fn)
    assert result.confidence in ("High", "Medium", "Low")
