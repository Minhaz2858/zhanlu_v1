"""P2-3 tests: Enhanced preprocessing — imputation, anomaly score, holidays, short-series fallback."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.preprocess_enhanced import (
    preprocess_enhanced,
    EnhancedCleaningReport,
)


def _daily_series(n: int = 60, seed: int = 42, base: float = 100.0) -> pd.Series:
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    vals = base + rng.randn(n).cumsum() * 0.5
    return pd.Series(vals, index=dates, name="y")


# ---------------------------------------------------------------------------
# Imputation
# ---------------------------------------------------------------------------

def test_impute_missing_dates():
    """Gaps in daily series are filled via linear interpolation."""
    dates = pd.date_range("2025-01-01", "2025-02-01", freq="D").tolist()
    # Remove two dates from middle
    dates_skip = [d for d in dates if d not in [pd.Timestamp("2025-01-15"), pd.Timestamp("2025-01-16")]]
    vals = np.arange(len(dates_skip), dtype=float) * 0.5 + 100.0
    # Inject a small gap in the range
    y = pd.Series(vals, index=dates_skip, name="y")

    result, report, _ = preprocess_enhanced(y, "test_impute", impute_missing=True)
    assert report.n_missing_imputed == 2
    assert len(result.y_clean) >= len(dates_skip)  # At least as many as input


def test_no_impute_when_no_gaps():
    """Series without gaps has n_missing_imputed = 0."""
    y = _daily_series(30)
    result, report, _ = preprocess_enhanced(y, "test_no_gap", impute_missing=True)
    assert report.n_missing_imputed == 0


# ---------------------------------------------------------------------------
# Anomaly score
# ---------------------------------------------------------------------------

def test_anomaly_score_range():
    """Anomaly scores are in [0, 1]."""
    y = _daily_series(60)
    result, report, _ = preprocess_enhanced(y, "test_score", compute_anomaly_score=True)
    assert 0.0 <= report.max_anomaly_score <= 1.0


def test_anomaly_score_with_extreme_outlier():
    """Outlier pushes max_anomaly_score toward 1."""
    dates = pd.date_range("2025-01-01", periods=40, freq="D")
    vals = np.ones(40) * 100.0
    vals[20] = 500.0  # Extreme outlier
    y = pd.Series(vals, index=dates, name="y")

    result, report, _ = preprocess_enhanced(y, "test_outlier", compute_anomaly_score=True)
    assert report.max_anomaly_score > 0.5, f"Expected high anomaly score, got {report.max_anomaly_score}"


# ---------------------------------------------------------------------------
# Holiday features
# ---------------------------------------------------------------------------

def test_holiday_features_columns():
    """Check holiday feature columns exist."""
    y = _daily_series(60)
    result, report, holiday_df = preprocess_enhanced(y, "test_holiday", add_holiday_features=True)

    assert holiday_df is not None
    for col in ("is_holiday", "is_spring_festival", "day_of_week", "is_weekend", "is_month_end"):
        assert col in holiday_df.columns, f"Missing holiday column: {col}"


def test_holiday_flags_correct():
    """National Day (Oct 1-7) sets is_holiday=1."""
    y = _daily_series(30)
    # Shift dates to include Oct
    rng = np.random.RandomState(42)
    dates = pd.date_range("2025-09-20", periods=30, freq="D")
    vals = 100.0 + rng.randn(30).cumsum() * 0.5
    y = pd.Series(vals, index=dates, name="y")

    result, report, holiday_df = preprocess_enhanced(y, "test_nd", add_holiday_features=True)
    assert holiday_df is not None
    # Oct 1 should be a holiday
    oct1 = pd.Timestamp("2025-10-01")
    if oct1 in holiday_df.index:
        assert holiday_df.loc[oct1, "is_holiday"] == 1


def test_weekend_flag():
    """Weekend dates have is_weekend=1."""
    y = _daily_series(30)
    result, report, holiday_df = preprocess_enhanced(y, "test_wknd", add_holiday_features=True)

    assert holiday_df is not None
    # Find a Sunday (dayofweek=6)
    sundays = holiday_df[holiday_df.index.dayofweek == 6]
    if len(sundays) > 0:
        assert (sundays["is_weekend"] == 1).all()


# ---------------------------------------------------------------------------
# Short-series fallback
# ---------------------------------------------------------------------------

def test_short_series_fallback():
    """Series with < threshold points uses fallback."""
    y = _daily_series(5)
    result, report, holiday_df = preprocess_enhanced(
        y, "short", short_series_threshold=12, impute_missing=True,
    )

    assert isinstance(report, EnhancedCleaningReport)
    assert holiday_df is None  # No holiday features for fallback
    assert len(result.y_clean) >= 1  # At least has something


# ---------------------------------------------------------------------------
# No mutations
# ---------------------------------------------------------------------------

def test_y_raw_not_mutated():
    """Original y_raw is unchanged."""
    y = _daily_series(30)
    y_orig = y.copy()
    preprocess_enhanced(y, "no_mutate")
    pd.testing.assert_series_equal(y, y_orig)
