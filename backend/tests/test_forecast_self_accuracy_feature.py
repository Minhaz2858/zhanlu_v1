"""Test self-accuracy feature (P3-2C).

Validates that build_features() adds recent_mape_7d column when
self_accuracy_feature_enabled=True.
"""
import numpy as np
import pandas as pd
import pytest

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


class TestSelfAccuracyFeature:
    """Tests for self-accuracy feature in build_features()."""

    def test_disabled_returns_no_mape_col(self):
        """When disabled, no recent_mape_7d column should appear."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            recent_mape_7d=12.5,
            self_accuracy_feature_enabled=False,
        )
        assert "recent_mape_7d" not in result.feature_names

    def test_enabled_adds_mape_col(self):
        """When enabled, recent_mape_7d column should appear."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            recent_mape_7d=12.5,
            self_accuracy_feature_enabled=True,
        )
        assert "recent_mape_7d" in result.feature_names
        assert "recent_mape_7d" in result.X_train.columns

    def test_mape_value_correct(self):
        """The mape value should match the input parameter."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            recent_mape_7d=8.3,
            self_accuracy_feature_enabled=True,
        )
        # All rows should have the same mape value
        assert all(result.X_train["recent_mape_7d"] == 8.3)

    def test_none_mape_no_col(self):
        """When mape is None, column should not be added even if enabled."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            recent_mape_7d=None,
            self_accuracy_feature_enabled=True,
        )
        assert "recent_mape_7d" not in result.feature_names

    def test_future_rows_have_mape_col(self):
        """Future X_future should also have recent_mape_7d column."""
        y = _make_series(30)
        spec = FeatureSpec(product_key="c5_cracked", feedstock_keys=["naphtha"])
        cascade = {"naphtha": [100.0] * 7}

        result = build_features(
            "c5_cracked", y, spec,
            MockLoader(), MockLoader(), MockLoader(),
            horizon=7,
            cascade_forecasts=cascade,
            recent_mape_7d=15.0,
            self_accuracy_feature_enabled=True,
        )
        assert result.X_future is not None
        assert "recent_mape_7d" in result.X_future.columns
