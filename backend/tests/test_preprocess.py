"""Tests for anomaly-robust preprocessing module."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.preprocess import (
    CleaningReport,
    PreprocessResult,
    preprocess_series,
    smooth_erp_prices,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_series(
    n: int = 100,
    spike_idx: list[int] | None = None,
    stale_tail_gap: int = 0,
) -> pd.Series:
    """Build a synthetic daily series with optional spikes (sine base, deterministic)."""
    dates = pd.date_range("2026-06-01", periods=n, freq="D")
    # Deterministic sine wave base — avoids random walk triggering spike detector
    t = np.arange(n)
    values = 5000.0 + 80.0 * np.sin(2 * np.pi * t / 30.0)
    if spike_idx:
        for i in spike_idx:
            if i < len(values):
                values[i] *= 3.0
    if stale_tail_gap > 0:
        values[n:] = np.nan
        dates = dates[:n]
        values = values[:n]
    return pd.Series(values, index=dates, name="y")


# ---------------------------------------------------------------------------
# Spike Detection
# ---------------------------------------------------------------------------

class TestSpikeDetection:
    def test_detects_and_corrects_spike(self):
        y = _make_series(n=100, spike_idx=[50])
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected >= 1
        assert abs(result.y_clean.iloc[50] - y.iloc[49]) < abs(y.iloc[50] - y.iloc[49])

    def test_cap_at_5_percent(self):
        y = _make_series(n=100, spike_idx=list(range(0, 20)))
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected <= 5

    def test_no_spike_on_clean_series(self):
        y = _make_series(n=100)
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected == 0


# ---------------------------------------------------------------------------
# Stale Guard
# ---------------------------------------------------------------------------

class TestStaleGuard:
    def test_stale_detected_when_tail_gap_large(self):
        old_dates = pd.date_range("2026-01-01", periods=50, freq="D")
        old_values = 5000 + np.random.rand(50) * 50
        y_old = pd.Series(old_values, index=old_dates)
        result = preprocess_series(y_old, "test_product", stale_threshold_days=14)
        assert isinstance(result.report.is_stale, bool)

    def test_not_stale_when_recent(self):
        recent_dates = pd.date_range(end=pd.Timestamp.now(), periods=50, freq="D")
        values = 5000 + np.random.rand(50) * 50
        y = pd.Series(values, index=recent_dates)
        result = preprocess_series(y, "test_product", stale_threshold_days=14)
        assert result.report.is_stale is False


# ---------------------------------------------------------------------------
# Level Shift
# ---------------------------------------------------------------------------

class TestLevelShift:
    def test_level_shift_marked_not_removed(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D")
        values = np.concatenate([np.full(50, 5000.0), np.full(50, 5500.0)])
        y = pd.Series(values, index=dates)
        result = preprocess_series(y, "test_product")
        assert result.report.n_level_shifts >= 1
        assert result.y_clean.iloc[60] == pytest.approx(5500.0)


# ---------------------------------------------------------------------------
# Raw Preserved
# ---------------------------------------------------------------------------

class TestRawPreserved:
    def test_y_raw_never_mutated(self):
        y = _make_series(n=100, spike_idx=[50])
        original = y.copy()
        result = preprocess_series(y, "test_product")
        pd.testing.assert_series_equal(y, original)
        pd.testing.assert_series_equal(result.y_raw, original)


# ---------------------------------------------------------------------------
# Winsorization
# ---------------------------------------------------------------------------

class TestWinsorization:
    def test_extremes_capped_in_clean(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D")
        values = np.full(100, 5000.0)
        values[50] = 50000.0
        y = pd.Series(values, index=dates)
        result = preprocess_series(y, "test_product", spike_k=100.0)
        assert result.y_clean.max() < 50000.0


# ---------------------------------------------------------------------------
# ERP Smoothing — center=False (Bug #6 fix)
# ---------------------------------------------------------------------------

class TestErpSmoothingNoFutureLeakage:
    """Verify that smooth_erp_prices uses trailing-only windows (center=False)."""

    def test_trailing_window_does_not_use_future(self):
        """The last smoothed value must only depend on past observations."""
        dates = pd.date_range("2026-07-01", periods=20, freq="D")
        values = np.concatenate([np.full(10, 100.0), np.full(10, 200.0)])
        y = pd.Series(values, index=dates)

        smoothed = smooth_erp_prices(y, window=7, method="median")

        # With center=False, the trailing window at the step point (index 9→10)
        # should still show the step — it won't be pre-smoothed by future data
        # With center=True, index 9 would have been pulled toward 200 by future data
        assert smoothed.iloc[9] == pytest.approx(100.0)  # Still in past-only window
        # By index 13 (7 steps after step), the window is fully in 200 territory
        assert smoothed.iloc[13] == pytest.approx(200.0)

    def test_trailing_window_preserves_lag(self):
        """Trailing smooth should introduce a ~window/2 lag vs future-leaking center."""
        dates = pd.date_range("2026-07-01", periods=30, freq="D")
        values = np.arange(30, dtype=float)
        y = pd.Series(values, index=dates)

        smoothed = smooth_erp_prices(y, window=7, method="median")

        # Trailing window for index 29 uses indices 23-29 → median should be ~26
        assert smoothed.iloc[-1] == pytest.approx(26.0)
