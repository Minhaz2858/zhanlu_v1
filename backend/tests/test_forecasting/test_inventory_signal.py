"""Tests for Wave 3 T3.2 — compute_inventory_signal (pure function)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.forecasting.features.inventory_signal import (
    InventorySignal,
    compute_inventory_signal,
)


def _make_inventory_df(values, start_date=None, freq_days=1):
    """Build a daily inventory DataFrame. Default freq=1 (daily)."""
    start = start_date or datetime(2024, 1, 1)
    dates = [start + timedelta(days=i * freq_days) for i in range(len(values))]
    return pd.DataFrame({"date": dates, "inventory_t": values})


class TestComputeInventorySignal:

    def test_empty_df_returns_insufficient(self):
        df = pd.DataFrame(columns=["date", "inventory_t"])
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert isinstance(sig, InventorySignal)
        assert sig.has_sufficient_data is False
        assert sig.inventory_pressure == "normal"

    def test_none_df_returns_insufficient(self):
        sig = compute_inventory_signal(None, product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_insufficient_data(self):
        df = _make_inventory_df([100.0] * 5)
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_4wk_change_pct_correct(self):
        """Daily data: first 28 at 5000, next 28 at 5750 → +15%."""
        values = [5000.0] * 28 + [5750.0] * 28
        df = _make_inventory_df(values)
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert sig.has_sufficient_data is True
        # Latest 4wk avg ≈ 5750, prior 4wk avg ≈ 5000
        assert sig.inventory_4wk_change_pct == pytest.approx(15.0, abs=1.0)

    def test_pressure_high_above_threshold(self):
        """+20% change → high pressure (supply pressure)."""
        values = [5000.0] * 28 + [6000.0] * 28
        df = _make_inventory_df(values)
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert sig.inventory_pressure == "high"

    def test_pressure_low_below_threshold(self):
        """-20% change → low pressure (tight)."""
        values = [5000.0] * 28 + [4000.0] * 28
        df = _make_inventory_df(values)
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert sig.inventory_pressure == "low"

    def test_pressure_normal_in_band(self):
        """+5% change → normal."""
        values = [5000.0] * 28 + [5250.0] * 28
        df = _make_inventory_df(values)
        sig = compute_inventory_signal(df, product_id="isoprene")
        assert sig.inventory_pressure == "normal"

    def test_divergence_inventory_up_price_down(self):
        """Inventory rising + price falling → positive divergence (supply pressure)."""
        inv_values = [5000.0] * 28 + [6000.0] * 28
        inv_df = _make_inventory_df(inv_values)
        # Use a separate helper for the price DataFrame so it has the
        # expected 'price' column (not 'inventory_t').
        start = inv_df["date"].iloc[0]
        price_dates = [start + timedelta(days=i) for i in range(56)]
        price_values = [100.0] * 28 + [80.0] * 28
        price_df = pd.DataFrame({"date": price_dates, "price": price_values})
        sig = compute_inventory_signal(
            inv_df, price_df=price_df, product_id="isoprene",
        )
        # Inventory change = +20%, price change = -20% → divergence = +40
        assert sig.inventory_vs_price_divergence is not None
        assert sig.inventory_vs_price_divergence > 30.0

    def test_no_price_df_no_divergence(self):
        values = [5000.0] * 28 + [5500.0] * 28
        df = _make_inventory_df(values)
        sig = compute_inventory_signal(df, price_df=None, product_id="isoprene")
        assert sig.inventory_vs_price_divergence is None
        assert sig.has_sufficient_data is True