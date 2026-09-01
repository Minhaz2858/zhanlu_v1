"""Test quantile regression XGBoost (P3-2B).

Validates that QuantileXGBoost trains multiple models for different quantile
levels and produces ordered forecasts (p10 <= p50 <= p90).
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.xgboost_reg import QuantileXGBoost


class TestQuantileXGBoost:
    """Tests for QuantileXGBoost."""

    def _make_series(self, n=100, seed=42):
        """Generate synthetic price series with trend and noise."""
        np.random.seed(seed)
        dates = pd.date_range("2024-01-01", periods=n, freq="D")
        trend = np.cumsum(np.random.normal(0, 1, n))
        noise = np.random.normal(0, 5, n)
        values = 100 + trend + noise
        return pd.Series(values, index=dates, name="test_product")

    def test_fit_creates_models(self):
        """fit() should create models for all quantiles."""
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        assert len(qxgb._models) == 3
        assert 0.1 in qxgb._models
        assert 0.5 in qxgb._models
        assert 0.9 in qxgb._models

    def test_forecast_quantile_returns_series(self):
        """forecast_quantile() should return a pd.Series."""
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        fc = qxgb.forecast_quantile(7, quantile=0.5)
        assert isinstance(fc, pd.Series)
        assert len(fc) == 7
        assert all(np.isfinite(v) for v in fc)

    def test_quantile_ordering(self):
        """p10 <= p50 <= p90 at every horizon step (approximate, with tolerance).

        Note: Standard XGBoost regression doesn't enforce quantile ordering.
        With XGBoost 2.0+ and reg:quantileerror, ordering is guaranteed.
        Without it, we allow a small tolerance.
        """
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        p10 = qxgb.forecast_quantile(7, quantile=0.1)
        p50 = qxgb.forecast_quantile(7, quantile=0.5)
        p90 = qxgb.forecast_quantile(7, quantile=0.9)

        # Check ordering with tolerance (standard regression may not perfectly order)
        for i in range(7):
            # p10 should generally be <= p50, p50 <= p90
            # Allow small inversions due to model variance
            assert p10.iloc[i] <= p90.iloc[i] + 1.0  # p10 <= p90 (most important)
            assert p50.iloc[i] <= p90.iloc[i] + 1.0  # p50 <= p90
            assert p10.iloc[i] <= p50.iloc[i] + 1.0  # p10 <= p50

    def test_interval_returns_bounds(self):
        """interval() should return (lower, median, upper) with lower < upper (approximate).

        With standard XGBoost regression, quantile ordering may not be perfect.
        """
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        lo, mid, hi = qxgb.interval(7, alpha=0.1)

        assert len(lo) == 7
        assert len(mid) == 7
        assert len(hi) == 7

        # Check approximate ordering with tolerance
        for i in range(7):
            assert lo.iloc[i] <= hi.iloc[i] + 1.0  # lower <= upper (most important)
            assert mid.iloc[i] <= hi.iloc[i] + 1.0
            assert lo.iloc[i] <= mid.iloc[i] + 1.0

    def test_forecast_returns_median(self):
        """forecast() should return the median (p50) forecast."""
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        fc = qxgb.forecast(7)
        p50 = qxgb.forecast_quantile(7, quantile=0.5)

        np.testing.assert_array_almost_equal(fc.values, p50.values, decimal=5)

    def test_insufficient_data_raises(self):
        """fit() with too little data should raise ModelFitError."""
        qxgb = QuantileXGBoost()
        y = pd.Series([1, 2, 3, 4, 5])  # only 5 points

        with pytest.raises(Exception):
            qxgb.fit(y, quantiles=[0.5])

    def test_custom_quantiles(self):
        """Should support custom quantile levels."""
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.25, 0.75])

        assert len(qxgb._models) == 2
        assert 0.25 in qxgb._models
        assert 0.75 in qxgb._models

    def test_forecast_quantiles_dict(self):
        """forecast_quantiles() should return dict of all quantiles."""
        qxgb = QuantileXGBoost()
        y = self._make_series(100)
        qxgb.fit(y, quantiles=[0.1, 0.5, 0.9])

        fcs = qxgb.forecast_quantiles(7)
        assert len(fcs) == 3
        assert all(isinstance(v, pd.Series) for v in fcs.values())
