"""Test cross-product lag features (P1-2A).

Validates that build_features() adds upstream_{product}_lag1/2/3 columns
when cross_product_lags_enabled=True and upstream_series_map is provided.
"""
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta

from app.services.forecasting.features.feature_builder import build_features
from app.services.forecasting.features.feature_registry import FeatureSpec


class MockLoader:
    """Minimal mock for feedstock/FX/event loaders."""
    def read_actuals(self, key, start, end):
        return pd.DataFrame({"ds": pd.date_range(start, end), key: np.random.uniform(80, 120, len(pd.date_range(start, end)))})

    def read_usd_cny(self, start, end):
        return pd.DataFrame({"ds": pd.date_range(start, end), "usd_cny": [7.2] * len(pd.date_range(start, end))})

    def read_flags(self, key, start, end):
        return pd.DataFrame({"ds": pd.date_range(start, end), "event_flag": [0.0] * len(pd.date_range(start, end))})


def _make_series(n=30):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.Series(np.random.uniform(100, 150, n), index=dates, name="c5_cracked")


class TestCrossProductLagFeatures:
    """Tests for cross-product upstream lag features."""

    def test_disabled_returns_no_upstream_cols(self):
        """When disabled, no upstream_* columns should appear."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        upstream = {"naphtha": pd.Series(np.random.uniform(80, 120, 30), index=y.index)}

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            upstream_series_map=upstream,
            cross_product_lags_enabled=False,
        )
        assert result.X_train is not None
        assert not any("upstream_" in c for c in result.feature_names)

    def test_enabled_adds_upstream_lag_cols(self):
        """When enabled, upstream_{key}_lag1/2/3 columns should appear."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        upstream = {"naphtha": pd.Series(np.random.uniform(80, 120, 30), index=y.index)}

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            upstream_series_map=upstream,
            cross_product_lags_enabled=True,
        )
        assert result.X_train is not None
        assert "upstream_naphtha_lag1" in result.feature_names
        assert "upstream_naphtha_lag2" in result.feature_names
        assert "upstream_naphtha_lag3" in result.feature_names

    def test_multiple_upstream_products(self):
        """Multiple upstream products should each get lag columns."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        upstream = {
            "naphtha": pd.Series(np.random.uniform(80, 120, 30), index=y.index),
            "brent": pd.Series(np.random.uniform(60, 90, 30), index=y.index),
        }

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            upstream_series_map=upstream,
            cross_product_lags_enabled=True,
        )
        assert "upstream_naphtha_lag1" in result.feature_names
        assert "upstream_brent_lag1" in result.feature_names

    def test_lag_values_are_shifted(self):
        """Lag values should be from previous days, not current day."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        # Create upstream series with known values
        upstream_vals = list(range(30))
        upstream = {"naphtha": pd.Series(upstream_vals, index=y.index)}

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            upstream_series_map=upstream,
            cross_product_lags_enabled=True,
        )
        # For date 2024-01-05 (index 4), lag1 should be value from 2024-01-04 (index 3)
        date_5 = y.index[4]
        lag1_val = result.X_train.loc[date_5, "upstream_naphtha_lag1"]
        assert lag1_val == 3.0  # upstream_vals[3]

    def test_future_rows_have_upstream_cols(self):
        """Future X_future should also have upstream lag columns."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        upstream = {"naphtha": pd.Series(np.random.uniform(80, 120, 30), index=y.index)}
        # Need cascade_forecasts for X_future to be built
        cascade = {"naphtha": [100.0] * 7}

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            cascade_forecasts=cascade,
            upstream_series_map=upstream,
            cross_product_lags_enabled=True,
        )
        assert result.X_future is not None
        assert "upstream_naphtha_lag1" in result.X_future.columns
