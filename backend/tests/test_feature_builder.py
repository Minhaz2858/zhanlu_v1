"""Tests for exogenous feature matrix builder."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.features.feature_registry import FeatureSpec
from app.services.forecasting.features.feature_builder import (
    FeatureMatrix, build_features,
)


def _make_y(n: int = 60) -> pd.Series:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.Series(np.sin(np.arange(n) * 0.1) * 100 + 5000.0, index=dates, name="y")


class TestBuildFeaturesTraining:
    def test_x_train_has_feedstock_lag_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(
            product_key="isoprene", feedstock_keys=["cracked_c5", "naphtha"],
            feedstock_lags=[1, 2], spread_pairs=[("cracked_c5", "naphtha")],
            use_fx=True, use_event_flags=True, calendar_features=True,
        )
        loader = MagicMock()
        loader.read_actuals.side_effect = [
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "cracked_c5": [800.0 + i for i in range(60)]}),
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "naphtha": [600.0 + i for i in range(60)]}),
        ]
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})

        result = build_features("isoprene", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is not None
        lag_cols = [c for c in result.X_train.columns if "lag" in c]
        assert len(lag_cols) >= 2

    def test_x_train_has_spread_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="isoprene", feedstock_keys=["cracked_c5", "naphtha"],
                           feedstock_lags=[1], spread_pairs=[("cracked_c5", "naphtha")])
        loader = MagicMock()
        loader.read_actuals.side_effect = [
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "cracked_c5": [800.0 + i for i in range(60)]}),
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "naphtha": [600.0 + i for i in range(60)]}),
        ]
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("isoprene", y, spec, loader, fx_loader, event_loader, 7, None)
        spread_cols = [c for c in result.X_train.columns if "spread" in c]
        assert len(spread_cols) >= 1

    def test_x_train_has_calendar_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="test", feedstock_keys=["feed"], calendar_features=True)
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"),
            "feed": [700.0 + i for i in range(60)]})
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("test", y, spec, loader, fx_loader, event_loader, 7, None)
        assert any("sin" in c or "cos" in c for c in result.X_train.columns)


class TestBuildFeaturesFuture:
    def test_x_future_uses_cascade_values(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="isoprene", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"),
            "cracked_c5": [800.0 + i for i in range(60)]})
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        cascade = {"cracked_c5": [810.0, 815.0, 820.0, 825.0, 830.0, 835.0, 840.0]}
        result = build_features("isoprene", y, spec, loader, fx_loader, event_loader, 7, cascade)
        assert result.X_future is not None
        assert len(result.X_future) == 7

    def test_x_future_none_when_no_cascade(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="isoprene", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"),
            "cracked_c5": [800.0 + i for i in range(60)]})
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("isoprene", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_future is None

    def test_upstream_product_returns_none(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="crude_oil", feedstock_keys=[])
        loader = MagicMock()
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("crude_oil", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is None
        assert result.X_future is None


class TestNoLeakage:
    def test_x_train_for_date_t_uses_only_past_feedstock(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="isoprene", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        dates = pd.date_range("2026-01-01", periods=60, freq="D")
        feedstock_values = [100.0 * i for i in range(60)]
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({"ds": dates, "cracked_c5": feedstock_values})
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({"ds": dates, "event_flag": [0.0] * 60})
        result = build_features("isoprene", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is not None
        lag_col = [c for c in result.X_train.columns if "cracked_c5" in c and "lag1" in c]
        assert len(lag_col) == 1
        # First training row's cracked_c5_lag1 = feedstock_values[0] (the day before)
        assert result.X_train[lag_col[0]].iloc[0] == pytest.approx(feedstock_values[0])
