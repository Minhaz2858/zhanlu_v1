"""Tests for Wave 3 T3.3 — compute_import_parity_signal (pure function)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.services.forecasting.features.import_parity_signal import (
    ImportParitySignal,
    compute_import_parity_signal,
)


def _make_price_df(values, col="price", start_date=None, freq_days=1):
    start = start_date or datetime(2024, 1, 1)
    dates = [start + timedelta(days=i * freq_days) for i in range(len(values))]
    return pd.DataFrame({"date": dates, col: values})


def _make_import_df(values, start_date=None, freq_days=1):
    return _make_price_df(values, col="import_price_cny",
                          start_date=start_date, freq_days=freq_days)


class TestComputeImportParitySignal:

    def test_empty_import_df_returns_insufficient(self):
        df = pd.DataFrame(columns=["date", "import_price_cny"])
        domestic_df = _make_price_df([100.0] * 10)
        sig = compute_import_parity_signal(df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert isinstance(sig, ImportParitySignal)
        assert sig.has_sufficient_data is False

    def test_insufficient_data_no_domestic(self):
        """< 4 import rows AND no domestic → insufficient."""
        df = _make_import_df([12.0] * 3)
        sig = compute_import_parity_signal(df, product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_insufficient_data_no_import(self):
        df = _make_import_df([12.0] * 3)
        domestic_df = _make_price_df([100.0] * 10)
        sig = compute_import_parity_signal(df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert sig.has_sufficient_data is False

    def test_positive_gap_ceiling_pressure_true(self):
        """Import 12, domestic 15 → gap = +20% → ceiling_pressure=True."""
        import_df = _make_import_df([12.0] * 10)
        domestic_df = _make_price_df([15.0] * 10)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert sig.has_sufficient_data is True
        assert sig.import_parity_gap is not None
        assert sig.import_parity_gap == pytest.approx(0.20, abs=0.01)
        assert sig.ceiling_pressure is True

    def test_negative_gap_no_ceiling_pressure(self):
        """Import 18, domestic 15 → gap = -20% → no ceiling pressure."""
        import_df = _make_import_df([18.0] * 10)
        domestic_df = _make_price_df([15.0] * 10)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert sig.import_parity_gap is not None
        assert sig.import_parity_gap == pytest.approx(-0.20, abs=0.01)
        assert sig.ceiling_pressure is False

    def test_import_window_open_at_5pct(self):
        """Gap = +5% (import = 14.25, domestic = 15) → window_open=True."""
        import_df = _make_import_df([14.25] * 10)
        domestic_df = _make_price_df([15.0] * 10)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert sig.import_window_open is True

    def test_import_window_closed_below_5pct(self):
        """Gap = +3% → window_open=False."""
        import_df = _make_import_df([14.55] * 10)
        domestic_df = _make_price_df([15.0] * 10)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert sig.import_window_open is False

    def test_no_domestic_price_no_gap(self):
        """Without domestic_price_df → gap is None, both flags False."""
        import_df = _make_import_df([12.0] * 10)
        sig = compute_import_parity_signal(import_df, product_id="isoprene")
        assert sig.has_sufficient_data is True
        assert sig.import_parity_gap is None
        assert sig.ceiling_pressure is False
        assert sig.import_window_open is False

    def test_uses_latest_values_from_each_df(self):
        """Gap uses the last row of each DataFrame."""
        import_df = _make_import_df([20.0, 18.0, 15.0, 12.0, 10.0])
        domestic_df = _make_price_df([15.0] * 5)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        # Latest import = 10, domestic = 15 → gap = (15-10)/15 = +0.33
        assert sig.import_parity_gap == pytest.approx(0.333, abs=0.01)
        assert sig.ceiling_pressure is True

    def test_returns_import_parity_signal_dataclass(self):
        import_df = _make_import_df([12.0] * 10)
        domestic_df = _make_price_df([15.0] * 10)
        sig = compute_import_parity_signal(import_df, domestic_price_df=domestic_df,
                                            product_id="isoprene")
        assert isinstance(sig, ImportParitySignal)
        assert hasattr(sig, "import_parity_gap")
        assert hasattr(sig, "import_window_open")
        assert hasattr(sig, "ceiling_pressure")