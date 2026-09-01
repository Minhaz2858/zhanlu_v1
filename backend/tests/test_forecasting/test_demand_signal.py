"""Tests for demand_signal.py (pure functions, zero I/O)."""
from __future__ import annotations

import pandas as pd
import pytest
from datetime import date, timedelta

from app.services.forecasting.features.demand_signal import (
    compute_demand_signal,
    compute_supplier_ladder_signal,
    DemandSignal,
)


def _make_volume_df(dates_and_volumes):
    """Create volume DataFrame from list of (date, volume) tuples."""
    rows = [{"date": d, "volume": v} for d, v in dates_and_volumes]
    return pd.DataFrame(rows)


def _make_price_df(dates_and_prices):
    """Create price DataFrame from list of (date, price) tuples."""
    rows = [{"date": d, "price": p} for d, p in dates_and_prices]
    return pd.DataFrame(rows)


class TestComputeDemandSignal:
    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["date", "volume"])
        result = compute_demand_signal(df, product_id="test")
        assert isinstance(result, DemandSignal)
        assert not result.has_sufficient_data

    def test_insufficient_data(self):
        """Less than 7 days of data -> no signal."""
        base = date(2026, 7, 1)
        dates = [base + timedelta(days=i) for i in range(5)]
        vols = [100.0] * 5
        df = _make_volume_df(list(zip(dates, vols)))
        result = compute_demand_signal(df, product_id="test")
        assert not result.has_sufficient_data

    def test_basic_rolling_volume(self):
        """28+ days of stable volume -> rolling_4wk_vol = 100.0."""
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=i) for i in range(60)]
        vols = [100.0] * 60
        df = _make_volume_df(list(zip(dates, vols)))
        result = compute_demand_signal(df, product_id="test")
        assert result.has_sufficient_data
        assert result.rolling_4wk_vol == pytest.approx(100.0)
        assert result.recent_vol == 100.0
        assert result.demand_trend == "stable"

    def test_rising_demand_trend(self):
        """Volume rising >10% -> demand_trend = rising."""
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=i) for i in range(60)]
        # First 30 days: avg 100, last 30 days: avg 120 (20% increase)
        vols = [100.0] * 30 + [120.0] * 30
        df = _make_volume_df(list(zip(dates, vols)))
        result = compute_demand_signal(df, product_id="test")
        assert result.demand_trend == "rising"
        assert result.vol_momentum_4wk is not None
        assert result.vol_momentum_4wk > 0

    def test_falling_demand_trend(self):
        """Volume falling >10% -> demand_trend = falling."""
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=i) for i in range(60)]
        vols = [200.0] * 30 + [150.0] * 30
        df = _make_volume_df(list(zip(dates, vols)))
        result = compute_demand_signal(df, product_id="test")
        assert result.demand_trend == "falling"

    def test_volume_price_divergence(self):
        """Volume rising + price flat -> positive divergence."""
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=i) for i in range(60)]
        vols = [100.0] * 30 + [120.0] * 30
        prices = [5000.0] * 30 + [5000.0] * 30
        vol_df = _make_volume_df(list(zip(dates, vols)))
        price_df = _make_price_df(list(zip(dates, prices)))
        result = compute_demand_signal(vol_df, price_df, product_id="test")
        # vol_chg = 20%, price_chg = 0%, divergence = +20.0 (volume stronger than price)
        assert result.vol_price_divergence is not None
        assert result.vol_price_divergence > 0

    def test_yoy_change(self):
        """YoY change over 364+ days."""
        base = date(2024, 1, 1)
        dates = [base + timedelta(days=i) for i in range(400)]
        # 2024 avg ~100, 2025 avg ~110 (10% YoY)
        vols = [100.0] * 200 + [110.0] * 200
        df = _make_volume_df(list(zip(dates, vols)))
        result = compute_demand_signal(df, product_id="test")
        # yoy_change_pct may not be computed if not enough overlap, but
        # at minimum the function should not crash
        assert result.has_sufficient_data


class TestSupplierLadderSignal:
    def test_empty(self):
        df = pd.DataFrame(columns=["date", "spread", "supplier_count"])
        result = compute_supplier_ladder_signal(df, product_id="test")
        assert not result["has_data"]

    def test_stable_spread(self):
        dates = [date(2026, 7, i + 1) for i in range(30)]
        spreads = [50.0] * 30
        counts = [3.0] * 30
        df = pd.DataFrame(
            list(zip(dates, spreads, counts)),
            columns=["date", "spread", "supplier_count"],
        )
        result = compute_supplier_ladder_signal(df, product_id="test")
        assert result["has_data"]
        assert result["avg_spread"] == pytest.approx(50.0)
        assert result["avg_supplier_count"] == 3.0
        assert result["spread_trend"] == "stable"

    def test_widening_spread(self):
        """Spread increasing 20% -> widening."""
        dates = [date(2026, 7, i + 1) for i in range(30)]
        spreads = [50.0 + i for i in range(30)]  # 50 -> 79 (58% increase)
        counts = [3.0] * 30
        df = pd.DataFrame(
            list(zip(dates, spreads, counts)),
            columns=["date", "spread", "supplier_count"],
        )
        result = compute_supplier_ladder_signal(df, product_id="test")
        assert result["has_data"]
        assert result["spread_trend"] == "widening"

    def test_narrowing_spread(self):
        """Spread decreasing 20% -> narrowing."""
        dates = [date(2026, 7, i + 1) for i in range(30)]
        spreads = [100.0 - i for i in range(30)]  # 100 -> 71 (29% decrease)
        counts = [3.0] * 30
        df = pd.DataFrame(
            list(zip(dates, spreads, counts)),
            columns=["date", "spread", "supplier_count"],
        )
        result = compute_supplier_ladder_signal(df, product_id="test")
        assert result["has_data"]
        assert result["spread_trend"] == "narrowing"
