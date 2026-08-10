"""P2-3: Enhanced preprocessing — imputation, anomaly score, holiday features, short-series fallback.

Adds capabilities beyond the base preprocess_series():
- Missing-date imputation (linear interpolation)
- Per-point anomaly score (0-1)
- Chinese holiday / calendar features
- Short-series robust fallback
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from datetime import datetime

from app.services.forecasting.preprocess import (
    CleaningReport,
    PreprocessResult,
    preprocess_series,
)

logger = logging.getLogger(__name__)


# Chinese holidays (fixed solar dates only; lunar holidays approximated)
_SOLAR_HOLIDAYS = {
    1: {1},           # New Year
    5: {1, 2, 3},     # Labor Day
    10: {1, 2, 3, 4, 5, 6, 7},  # National Day
}
# Approximate lunar NY (simplified: Jan 20 - Feb 15)
_SPRING_FESTIVAL_RANGE = (
    pd.Timestamp("1900-01-20").dayofyear,
    pd.Timestamp("1900-02-15").dayofyear,
)

# Gregorian holidays
_GREGORIAN_HOLIDAYS_MD = {
    (1, 1),   # New Year
    (2, 14),  # Valentine (sentiment)
    (3, 8),   # Women's Day
    (4, 5),   # Qingming approx
    (5, 1), (5, 2), (5, 3),  # Labor
    (6, 1),   # Children's Day
    (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7),
    (12, 25),  # Christmas
}


@dataclass
class EnhancedCleaningReport(CleaningReport):
    """Extended cleaning report with imputation and anomaly scoring."""

    n_missing_imputed: int = 0
    imputed_dates: list[str] = None  # type: ignore
    max_anomaly_score: float = 0.0
    holiday_feature_count: int = 0

    def __post_init__(self):
        if self.imputed_dates is None:
            self.imputed_dates = []


def preprocess_enhanced(
    y: pd.Series,
    series_id: str = "unknown",
    seasonal_period: int = 7,
    spike_k: float = 6.0,
    max_spike_frac: float = 0.05,
    stale_threshold_days: int = 14,
    winsorize: bool = True,
    impute_missing: bool = True,
    compute_anomaly_score: bool = True,
    add_holiday_features: bool = True,
    short_series_threshold: int = 12,
) -> tuple[PreprocessResult, EnhancedCleaningReport, pd.DataFrame | None]:
    """Enhanced preprocessing with imputation, anomaly scoring, and holiday features.

    Args:
        y: Input time series.
        series_id: Identifier for logging.
        seasonal_period: Period for STL decomposition.
        spike_k: MAD multiplier for spike detection.
        max_spike_frac: Max fraction of points to mark as spikes.
        stale_threshold_days: Days after last observation to mark stale.
        winsorize: Apply winsorization.
        impute_missing: Fill gaps with linear interpolation.
        compute_anomaly_score: Compute per-point 0-1 anomaly likelihood.
        add_holiday_features: Append binary holiday/calendar columns.
        short_series_threshold: Below this, use robust fallback.

    Returns:
        (PreprocessResult, EnhancedCleaningReport, holiday_df | None)
    """
    y_raw = y.copy().sort_index()
    holiday_df: pd.DataFrame | None = None

    # --- Short-series fallback ---
    if len(y_raw.dropna()) <= short_series_threshold:
        logger.warning(
            "[preprocess-enhanced] series %s has only %d non-null points, using fallback",
            series_id, len(y_raw.dropna()),
        )
        return _short_series_fallback(y_raw, series_id, stale_threshold_days)

    # --- Impute missing dates ---
    report = EnhancedCleaningReport(
        series_id=series_id,
        n_points=len(y_raw),
    )

    if impute_missing and isinstance(y_raw.index, pd.DatetimeIndex):
        y_work, imputed = _impute_missing_dates(y_raw)
        report.n_missing_imputed = len(imputed)
        report.imputed_dates = sorted(str(d.date()) for d in imputed)
        if imputed:
            logger.info(
                "[preprocess-enhanced] %s: imputed %d missing dates",
                series_id, len(imputed),
            )
    else:
        y_work = y_raw.dropna()

    # --- Base preprocessing (STL spike + level shift + winsorize) ---
    base_result = preprocess_series(
        y_work,
        series_id=series_id,
        seasonal_period=seasonal_period,
        spike_k=spike_k,
        max_spike_frac=max_spike_frac,
        stale_threshold_days=stale_threshold_days,
        winsorize=winsorize,
    )

    # Carry forward base cleaning report fields
    report.n_spikes_detected = base_result.report.n_spikes_detected
    report.spike_dates = base_result.report.spike_dates
    report.n_level_shifts = base_result.report.n_level_shifts
    report.level_shift_dates = base_result.report.level_shift_dates
    report.stale_tail_days = base_result.report.stale_tail_days
    report.is_stale = base_result.report.is_stale

    # --- Anomaly score (computed on raw data before winsorization) ---
    if compute_anomaly_score and len(y_work) >= 5:
        scores = _anomaly_score(y_work)
        report.max_anomaly_score = float(np.max(scores)) if len(scores) > 0 else 0.0
    else:
        scores = np.zeros(len(y_work))

    # --- Holiday features ---
    if add_holiday_features and isinstance(y_raw.index, pd.DatetimeIndex):
        holiday_df = _build_holiday_features(base_result.y_clean.index)
        report.holiday_feature_count = len(holiday_df.columns)

    return base_result, report, holiday_df


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _impute_missing_dates(y: pd.Series) -> tuple[pd.Series, list[pd.Timestamp]]:
    """Linear interpolation over missing dates in a daily DatetimeIndex."""
    full_range = pd.date_range(start=y.index.min(), end=y.index.max(), freq="D")
    missing = sorted(set(full_range) - set(y.index))
    y_full = y.reindex(full_range).interpolate(method="linear", limit_direction="both")
    return y_full, missing


def _anomaly_score(y_clean: pd.Series) -> np.ndarray:
    """Compute a 0-1 anomaly score based on robust z-score.

    0 = normal, 1 = extreme outlier.
    """
    vals = y_clean.values.astype(float)
    med = np.median(vals)
    mad = np.median(np.abs(vals - med))
    # If MAD is zero (all values same or single outlier), fall back to IQR or std
    if mad == 0 or np.isnan(mad):
        iqr = np.percentile(vals, 75) - np.percentile(vals, 25)
        if iqr > 0:
            mad = iqr / 1.349  # IQR -> MAD equivalent for normal dist
        else:
            std = np.std(vals)
            mad = std / 1.4826 if std > 0 else 1.0  # std -> MAD equivalent
    z = 0.6745 * (vals - med) / mad
    # Sigmoid mapping: center at z=3, steepness 1
    return 1.0 / (1.0 + np.exp(-(np.abs(z) - 3.0)))


def _build_holiday_features(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Build binary holiday and calendar feature columns.

    Returns DataFrame indexed by dates with columns:
        is_holiday, is_spring_festival, day_of_week, is_weekend, is_month_end.
    """
    rows = {}
    for d in dates:
        features = {}
        m, dy = d.month, d.day

        # Solar holiday
        features["is_holiday"] = int(m in _SOLAR_HOLIDAYS and dy in _SOLAR_HOLIDAYS[m])
        # Approx Spring Festival
        doy = d.dayofyear
        features["is_spring_festival"] = int(
            _SPRING_FESTIVAL_RANGE[0] <= doy <= _SPRING_FESTIVAL_RANGE[1]
        )
        # Day of week and weekend
        features["day_of_week"] = d.dayofweek  # 0=Mon, 6=Sun
        features["is_weekend"] = int(d.dayofweek >= 5)
        # Month end
        next_d = d + pd.Timedelta(days=1)
        features["is_month_end"] = int(d.month != next_d.month)

        rows[d] = features

    df = pd.DataFrame(rows).T
    df.index.name = "date"
    return df


def _short_series_fallback(
    y_raw: pd.Series, series_id: str, stale_threshold_days: int,
) -> tuple[PreprocessResult, EnhancedCleaningReport, None]:
    """Robust fallback for series too short for STL."""
    y_clean = y_raw.dropna().copy()
    if len(y_clean) == 0:
        # Create a single-point placeholder
        y_clean = pd.Series([0.0], name=y_raw.name)

    is_stale = False
    stale_days = None
    if isinstance(y_raw.index, pd.DatetimeIndex) and len(y_raw) > 0:
        days_since = (datetime.now() - y_raw.index[-1].to_pydatetime()).days
        is_stale = days_since > stale_threshold_days
        stale_days = days_since

    report = EnhancedCleaningReport(
        series_id=series_id,
        n_points=len(y_raw),
        is_stale=is_stale,
        stale_tail_days=stale_days,
    )

    result = PreprocessResult(
        y_clean=y_clean,
        y_raw=y_raw,
        report=report,
    )
    return result, report, None
