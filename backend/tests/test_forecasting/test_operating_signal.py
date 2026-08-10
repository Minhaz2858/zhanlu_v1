"""Tests for Wave 3 T3.1 — compute_operating_signal (pure function)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.forecasting.features.operating_signal import (
    OperatingSignal,
    compute_operating_signal,
)


def _make_op_rate_df(rates, start_date=None, freq_days=1):
    """Build a daily op-rate DataFrame mimicking OperatingRateLoader output.

    Default ``freq_days=1`` matches daily data — same convention as
    ``demand_signal.py``. Tests that need weekly cadence should use 7.
    """
    start = start_date or (datetime(2024, 1, 1))
    dates = [start + timedelta(days=i * freq_days) for i in range(len(rates))]
    return pd.DataFrame({"date": dates, "op_rate": rates})


class TestComputeOperatingSignal:

    def test_empty_df_returns_insufficient(self):
        df = pd.DataFrame(columns=["date", "op_rate"])
        sig = compute_operating_signal(df, product_id="isoprene")
        assert isinstance(sig, OperatingSignal)
        assert sig.product_id == "isoprene"
        assert sig.has_sufficient_data is False
        assert sig.utilization_regime == "normal"

    def test_none_df_returns_insufficient(self):
        sig = compute_operating_signal(None, product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_insufficient_data_returns_normal(self):
        """len(df) < 8 → has_sufficient_data=False."""
        df = _make_op_rate_df([70.0] * 5)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_rolling_4wk_mean_correct(self):
        # Daily: last 28 days are all 80.0
        rates = [70.0] * 28 + [80.0] * 28
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.has_sufficient_data is True
        assert sig.rolling_4wk_op_rate == pytest.approx(80.0, abs=0.01)

    def test_yoy_change_pct_correct(self):
        # Daily: 400 rows total, first 372 at 70%, last 28 at 77%
        rates = [70.0] * 372 + [77.0] * 28
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.yoy_change_pct is not None
        # (77 - 70) / 70 * 100 ≈ 10.0%
        assert sig.yoy_change_pct == pytest.approx(10.0, abs=0.5)

    def test_regime_tight_at_80(self):
        rates = [82.0] * 100
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.utilization_regime == "tight"

    def test_regime_loose_below_55(self):
        rates = [50.0] * 100
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.utilization_regime == "loose"

    def test_regime_normal_between(self):
        rates = [65.0] * 100
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert sig.utilization_regime == "normal"

    def test_divergence_op_flat_price_rising(self):
        """Op-rate flat but price rising → high negative divergence."""
        # 56 days: op-rate constant, price transitions from 100 to 200 at day 28
        op_rates = [75.0] * 56
        op_df = _make_op_rate_df(op_rates)
        prices = [100.0] * 28 + [200.0] * 28
        price_df = pd.DataFrame({
            "date": op_df["date"].tolist(),
            "price": prices,
        })
        sig = compute_operating_signal(
            op_df, price_df=price_df, product_id="isoprene",
        )
        # Op-rate change = 0%, price change = +100% → divergence = -100
        assert sig.op_rate_vs_price_divergence is not None
        assert sig.op_rate_vs_price_divergence < -50.0

    def test_divergence_op_rising_price_flat(self):
        """Op-rate rising but price flat → positive divergence."""
        # 56 days: op-rate transitions from 60 to 80 at day 28, price constant
        op_rates = [60.0] * 28 + [80.0] * 28
        op_df = _make_op_rate_df(op_rates)
        prices = [100.0] * 56
        price_df = pd.DataFrame({
            "date": op_df["date"].tolist(),
            "price": prices,
        })
        sig = compute_operating_signal(
            op_df, price_df=price_df, product_id="isoprene",
        )
        # Op-rate change = +33.33%, price change = 0% → divergence = +33.33
        assert sig.op_rate_vs_price_divergence is not None
        assert sig.op_rate_vs_price_divergence > 30.0

    def test_no_price_df_no_divergence(self):
        rates = [75.0] * 100
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, price_df=None, product_id="isoprene")
        assert sig.op_rate_vs_price_divergence is None
        assert sig.has_sufficient_data is True

    def test_returns_operating_signal_dataclass(self):
        rates = [75.0] * 100
        df = _make_op_rate_df(rates)
        sig = compute_operating_signal(df, product_id="isoprene")
        assert isinstance(sig, OperatingSignal)
        assert hasattr(sig, "rolling_4wk_op_rate")
        assert hasattr(sig, "yoy_change_pct")
        assert hasattr(sig, "utilization_regime")