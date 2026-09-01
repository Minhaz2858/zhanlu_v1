"""Test directional classifier integration."""

import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.directional_classifier import (
    DirectionalClassifier, backtest_directional, build_features,
)


@pytest.fixture
def trending_series():
    rng = np.random.RandomState(42)
    trend = np.linspace(90, 110, 200)
    noise = rng.normal(0, 1.5, 200)
    return pd.Series(
        trend + noise,
        index=pd.date_range("2024-01-01", periods=200, freq="D"),
        name="price",
    )


class TestDirectionalClassifier:
    def test_fit_predict_proba(self, trending_series):
        clf = DirectionalClassifier()
        X = build_features(trending_series)
        assert X is not None
        y_sign = (trending_series.shift(-1) > trending_series).astype(int).dropna()
        min_len = min(len(X), len(y_sign))
        clf.fit(X.iloc[:min_len], y_sign.iloc[:min_len])
        X_last = X.iloc[[-1]]; prob = clf.predict_proba(X_last)
        assert prob is not None
        assert 0.0 <= prob <= 1.0

    def test_predict_proba_trending_up(self, trending_series):
        clf = DirectionalClassifier()
        X = build_features(trending_series)
        y_sign = (trending_series.shift(-1) > trending_series).astype(int).dropna()
        min_len = min(len(X), len(y_sign))
        clf.fit(X.iloc[:min_len], y_sign.iloc[:min_len])
        X_last = X.iloc[[-1]]; prob = clf.predict_proba(X_last)
        assert prob is not None

    def test_backtest_directional_structure(self, trending_series):
        result = backtest_directional(trending_series, horizons=(7,))
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("edge", "no_edge")

    def test_build_features_output(self, trending_series):
        X = build_features(trending_series)
        assert X is not None
        assert len(X) > 50

    def test_short_series_handling(self):
        short = pd.Series([100.0, 101.0, 100.5], name="short")
        result = backtest_directional(short, horizons=(7,))
        assert result["status"] == "no_edge"
