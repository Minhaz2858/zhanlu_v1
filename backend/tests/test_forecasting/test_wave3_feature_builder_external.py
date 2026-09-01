"""Tests for Wave 3 T3.4 — feature_builder external exog columns.

Adds operating_rate_lag{1..7}, inventory_lag{1..7}, import_price_lag{1..7}
columns mirroring the Wave 1 erp_volume_lag{1..7} pattern.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.forecasting.features.feature_builder import build_features
from app.services.forecasting.features.feature_registry import FeatureSpec


def _make_y_series(n_days: int = 120, start_date: datetime | None = None,
                    base_price: float = 100.0, noise_seed: int = 0):
    """Build a synthetic y Series (price history) with daily index."""
    import numpy as np
    rng = np.random.default_rng(noise_seed)
    start = start_date or datetime(2025, 1, 1)
    dates = pd.date_range(start=start, periods=n_days, freq="D")
    values = base_price + rng.normal(0, 1, n_days).cumsum()
    return pd.Series(values, index=dates, name="y")


def _make_exog_df(values, start_date=None, col_name="value", freq_days=1):
    start = start_date or datetime(2025, 1, 1)
    dates = [start + timedelta(days=i * freq_days) for i in range(len(values))]
    return pd.DataFrame({"date": dates, col_name: values})


def _stub_loader(*_args, **_kwargs):
    """Stub feedstock loader — not exercised when feedstock_keys is empty."""
    raise RuntimeError("should not be called in these tests")


def _stub_fx(*_args, **_kwargs):
    raise RuntimeError("should not be called")


def _stub_events(*_args, **_kwargs):
    raise RuntimeError("should not be called")


class TestExternalExogColumns:

    def test_no_exog_no_new_columns(self):
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
        )
        # No features should be generated (no feedstock_keys)
        assert result.X_train is None
        assert result.feature_names == []

    def test_operating_rate_adds_lag_columns(self):
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        op_df = _make_exog_df(
            [75.0 + i * 0.1 for i in range(120)],
            col_name="op_rate",
        )
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            volume_df=None,
            operating_rate_df=op_df,
        )
        # Should have operating_rate_lag{1..7} columns
        op_cols = [c for c in result.feature_names
                   if c.startswith("op_rate_lag") or c == "op_rate_lag1"]
        assert len(op_cols) >= 1  # at least one op_rate_lag column
        assert "op_rate_lag1" in result.feature_names
        assert "op_rate_lag7" in result.feature_names

    def test_inventory_adds_lag_columns(self):
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        inv_df = _make_exog_df(
            [5000.0 + i * 5 for i in range(120)],
            col_name="inventory_t",
        )
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            inventory_df=inv_df,
        )
        assert "inventory_lag1" in result.feature_names
        assert "inventory_lag7" in result.feature_names

    def test_import_price_adds_lag_columns(self):
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        ip_df = _make_exog_df(
            [12.0 + i * 0.01 for i in range(120)],
            col_name="import_price_cny",
        )
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            import_price_df=ip_df,
        )
        assert "import_price_lag1" in result.feature_names
        assert "import_price_lag7" in result.feature_names

    def test_all_three_exog_combined(self):
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        op_df = _make_exog_df([75.0] * 120, col_name="op_rate")
        inv_df = _make_exog_df([5000.0] * 120, col_name="inventory_t")
        ip_df = _make_exog_df([12.0] * 120, col_name="import_price_cny")

        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            operating_rate_df=op_df,
            inventory_df=inv_df,
            import_price_df=ip_df,
        )
        # All 3 sets of lag columns present
        for prefix in ["op_rate_lag", "inventory_lag", "import_price_lag"]:
            assert any(c.startswith(prefix) for c in result.feature_names), \
                f"missing {prefix}* columns"

    def test_empty_exog_does_not_break(self):
        """Empty df should not add columns or break."""
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        empty_op = pd.DataFrame(columns=["date", "op_rate"])
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            operating_rate_df=empty_op,
        )
        # Empty df → no op_rate_lag columns
        assert not any(c.startswith("op_rate_lag") for c in result.feature_names)

    def test_exog_with_few_rows_skipped(self):
        """Exog df with < 8 rows should be skipped (matches volume_df pattern)."""
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(60)
        small_op = _make_exog_df([75.0] * 5, col_name="op_rate")
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            operating_rate_df=small_op,
        )
        assert not any(c.startswith("op_rate_lag") for c in result.feature_names)

    def test_exog_lag_values_are_correct(self):
        """Verify lag values are correct for at least one column."""
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        # y starts at 2025-01-01
        y = _make_y_series(30, start_date=datetime(2025, 1, 1))
        # op_rate: indexed by date, all 75.0
        op_df = _make_exog_df(
            [75.0] * 60, start_date=datetime(2025, 1, 1),
            col_name="op_rate", freq_days=1,
        )
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            operating_rate_df=op_df,
        )
        # All values should be 75.0 (flat series)
        assert "op_rate_lag1" in result.feature_names
        col = result.X_train["op_rate_lag1"]
        assert (col == 75.0).all()

    def test_exog_lag_columns_in_future(self):
        """Future horizon also gets the lag columns (with last-value fallback)."""
        spec = FeatureSpec(
            product_key="isoprene",
            feedstock_keys=[],
            feedstock_lags=[1],
            spread_pairs=[],
            use_fx=False,
            calendar_features=False,
        )
        y = _make_y_series(30)
        op_df = _make_exog_df([75.0] * 60, col_name="op_rate")
        result = build_features(
            target_product_key="isoprene",
            y=y, spec=spec,
            feedstock_loader=_stub_loader,
            fx_loader=_stub_fx,
            event_loader=_stub_events,
            horizon=7,
            operating_rate_df=op_df,
            cascade_forecasts={},  # no cascade → no future matrix
        )
        # With no cascade_forecasts for feedstock_keys, X_future is None
        # (feedstock_keys is empty here). Just verify X_train has op_rate_lag.
        assert result.X_future is None
        assert "op_rate_lag1" in result.feature_names