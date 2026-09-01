"""Test data freshness detection, gap interpolation, and outage alerts."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ops.freshness_guard import check_freshness


def _make_series(dates, prices):
    return pd.DataFrame({"date": dates, "price": prices})


def test_fresh_data_no_gaps():
    """Recent data with no gaps → not stale, no interpolation."""
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=30, freq="D")
    df = _make_series(dates, np.linspace(1000, 1100, 30))
    report = check_freshness("c5", df)
    assert not report.is_stale
    assert report.gap_count == 0
    assert report.interpolated_count == 0
    assert not report.outage_alert


def test_stale_data():
    """Data older than 3 days → stale flag."""
    dates = pd.date_range(end="2026-01-01", periods=10, freq="D")
    df = _make_series(dates, [1000.0] * 10)
    now = pd.Timestamp("2026-01-10", tz="UTC")
    report = check_freshness("c5", df, now=now)
    assert report.is_stale
    assert report.days_since_latest == 9


def test_short_gap_interpolated():
    """1-3 day gaps are linearly interpolated."""
    dates = [
        pd.Timestamp("2026-01-01", tz="UTC"),
        pd.Timestamp("2026-01-02", tz="UTC"),
        pd.Timestamp("2026-01-05", tz="UTC"),  # 3-day gap
        pd.Timestamp("2026-01-06", tz="UTC"),
    ]
    df = _make_series(dates, [1000.0, 1010.0, 1040.0, 1050.0])
    report = check_freshness("c5", df)
    assert report.gap_count == 1
    assert report.interpolated_count == 2  # Jan 3, Jan 4
    assert not report.outage_alert
    interp = report.interpolated_series
    assert len(interp) == 6  # Jan 1-6


def test_long_gap_outage_alert():
    """Gaps >3 days trigger outage alert and skip interpolation."""
    dates = [
        pd.Timestamp("2026-01-01", tz="UTC"),
        pd.Timestamp("2026-01-02", tz="UTC"),
        pd.Timestamp("2026-01-10", tz="UTC"),  # 8-day gap
        pd.Timestamp("2026-01-11", tz="UTC"),
    ]
    df = _make_series(dates, [1000.0, 1010.0, 1090.0, 1100.0])
    now = pd.Timestamp("2026-01-12", tz="UTC")
    report = check_freshness("c5", df, now=now)
    assert report.outage_alert
    assert report.interpolated_count == 0
    assert any("outage detected" in n for n in report.notes)


def test_empty_series():
    """Empty DataFrame is flagged stale with a note."""
    df = pd.DataFrame(columns=["date", "price"])
    report = check_freshness("c5", df)
    assert report.is_stale
    assert "empty series" in report.notes
