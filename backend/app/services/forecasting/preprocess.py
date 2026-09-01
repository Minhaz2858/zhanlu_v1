"""Anomaly-robust time-series preprocessing and data-quality guard.

Provides:
- STL-residual spike detection with configurable cap
- Stale-data tail guard (signals if last observation is too old)
- Level-shift detection (marks but does NOT mutate)
- Winsorization (1st/99th percentile clip)
- Auditable CleaningReport per series

Never mutates y_raw — all cleaning is done on a copied y_work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CleaningReport:
    """Audit trail for preprocessing decisions on a single series."""

    series_id: str
    n_points: int
    n_spikes_detected: int = 0
    spike_dates: list[str] = field(default_factory=list)
    n_level_shifts: int = 0
    level_shift_dates: list[str] = field(default_factory=list)
    stale_tail_days: int | None = None
    is_stale: bool = False
    winsorization_applied: bool = False
    notes: str = ""


@dataclass
class PreprocessResult:
    y_clean: pd.Series    # cleaned series (dropped NaN, spikes replaced, possibly winsorized)
    y_raw: pd.Series      # original (unaltered) series
    report: CleaningReport


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def preprocess_series(
    y: pd.Series,
    series_id: str,
    seasonal_period: int = 7,
    spike_k: float = 6.0,
    max_spike_frac: float = 0.05,
    stale_threshold_days: int = 14,
    winsorize: bool = True,
) -> PreprocessResult:
    """Clean a single time-series with anomaly detection and data-quality checks.

    Parameters
    ----------
    y: pd.Series
        Input time-series (index must be datetime-like).
    series_id: str
        Human-readable identifier used in the report.
    seasonal_period: int
        Period for STL decomposition (default 7 for daily data).
    spike_k: float
        Multiplier for MAD-based spike threshold. Higher = less sensitive.
    max_spike_frac: float
        Maximum fraction of observations that can be flagged as spikes.
    stale_threshold_days: int
        If the latest data point is older than this many days, set the stale flag.
    winsorize: bool
        Whether to clip tails at [1st, 99th] percentiles.

    Returns
    -------
    PreprocessResult
        Contains cleaned series, raw series, and an audit report.
    """
    y_raw = y.copy()
    y_work = y.dropna().copy()
    n_points = len(y_work)
    report = CleaningReport(series_id=series_id, n_points=n_points)

    if n_points < seasonal_period * 2:
        report.notes = "Series too short for STL-based cleaning; returned unchanged."
        return PreprocessResult(y_clean=y_work, y_raw=y_raw, report=report)

    # ---- 1. Stale-data guard -----------------------------------------------
    last_date = y_work.index.max()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(last_date, "tzinfo") and last_date.tzinfo is not None:
        last_date = last_date.replace(tzinfo=None)
    tail_gap_days = (now - last_date).days if last_date < now else 0
    if tail_gap_days > stale_threshold_days:
        report.is_stale = True
        report.stale_tail_days = tail_gap_days

    # ---- 2. Spike detection (STL-residual based) ---------------------------
    try:
        from statsmodels.tsa.seasonal import STL
        stl = STL(y_work.values.astype(float), period=seasonal_period, robust=True)
        res = stl.fit()
        residuals = res.resid
        mad = np.median(np.abs(residuals - np.median(residuals)))
        if mad > 1e-10:
            spike_mask = np.abs(residuals) > spike_k * mad
            spike_indices = np.where(spike_mask)[0]
        else:
            spike_indices = np.array([], dtype=int)
    except Exception as exc:
        logger.warning("STL failed for %s: %s", series_id, exc)
        spike_indices = np.array([], dtype=int)

    # Cap spikes to max_spike_frac * n_points (keep largest residuals)
    max_spikes = max(1, int(n_points * max_spike_frac))
    if len(spike_indices) > max_spikes:
        residuals_abs = np.abs(residuals[spike_indices]) if len(spike_indices) > 0 else np.array([])
        top_order = np.argsort(residuals_abs)[::-1][:max_spikes]
        spike_indices = spike_indices[top_order]

    report.n_spikes_detected = len(spike_indices)
    for idx in spike_indices:
        date = y_work.index[idx]
        report.spike_dates.append(str(date.date()) if hasattr(date, "date") else str(date))

    # Replace spikes with lag-period value (or previous value if too early)
    y_cleaned = y_work.copy()
    for idx in spike_indices:
        lag = seasonal_period
        if idx >= lag:
            y_cleaned.iloc[idx] = y_work.iloc[idx - lag]
        elif idx > 0:
            y_cleaned.iloc[idx] = y_work.iloc[idx - 1]

    # ---- 2b. Ensemble anomaly detection (flag-gated) --------------------------
    anomaly_count = 0
    anomaly_detection_enabled = settings.FORECAST_ANOMALY_DETECTION_ENABLED
    if anomaly_detection_enabled:
        try:
            from app.services.forecasting.anomaly_detector import detect_anomalies
            anom_result = detect_anomalies(y_cleaned, seasonal_period=seasonal_period)
            if anom_result.anomaly_indices:
                for idx in anom_result.anomaly_indices:
                    # Use median of surrounding window for replacement
                    half_win = max(1, seasonal_period // 2)
                    w_start = max(0, idx - half_win)
                    w_end = min(len(y_cleaned), idx + half_win + 1)
                    window_vals = y_cleaned.iloc[w_start:w_end].values
                    replacement = np.median(window_vals[np.abs(window_vals - np.median(window_vals)) < 2 * np.std(window_vals)] if len(window_vals) > 2 else window_vals)
                    if np.isnan(replacement):
                        replacement = y_cleaned.iloc[idx]
                    y_cleaned.iloc[idx] = replacement
                    date = y_cleaned.index[idx]
                    report.spike_dates.append(
                        f"anomaly:{str(date.date()) if hasattr(date, 'date') else str(date)}"
                    )
                anomaly_count = len(anom_result.anomaly_indices)
                report.n_spikes_detected += anomaly_count
                logger.info(
                    "Anomaly detection for %s: %d flagged (STL=%d, ensemble=%d)",
                    series_id, report.n_spikes_detected,
                    len(spike_indices), anomaly_count,
                )
        except Exception as exc:
            logger.warning("Ensemble anomaly detection failed for %s: %s", series_id, exc)

    # ---- 3. Level-shift detection (mark only — do NOT remove) --------------
    try:
        rolling_mean = y_cleaned.rolling(window=seasonal_period, center=True).mean()
        diff = rolling_mean.diff(seasonal_period)
        diff_clean = diff.dropna()
        if len(diff_clean) > 0:
            # Use std-based threshold (MAD fails when most diffs are zero)
            diff_std = float(np.nanstd(diff_clean.values))
            if diff_std > 1e-10:
                shift_mask = np.abs(diff) > 3 * diff_std
                shift_indices = np.where(shift_mask.values)[0]
            else:
                shift_indices = np.array([], dtype=int)
        else:
            shift_indices = np.array([], dtype=int)
    except Exception:
        shift_indices = np.array([], dtype=int)

    report.n_level_shifts = len(shift_indices)
    for idx in shift_indices:
        date = y_cleaned.index[idx]
        report.level_shift_dates.append(str(date.date()) if hasattr(date, "date") else str(date))

    # ---- 4. Winsorization (1st/99th percentile clip) -----------------------
    if winsorize and n_points >= 20:
        p1, p99 = np.percentile(y_cleaned.values, [1, 99])
        y_before = y_cleaned.copy()
        y_cleaned = y_cleaned.clip(lower=p1, upper=p99)
        if not y_cleaned.equals(y_before):
            report.winsorization_applied = True

    report.notes = (
        f"Preprocessed {n_points} points: {report.n_spikes_detected} spikes, "
        f"{report.n_level_shifts} shifts, stale={'yes' if report.is_stale else 'no'}, "
        f"winsorized={'yes' if report.winsorization_applied else 'no'}."
    )
    return PreprocessResult(y_clean=y_cleaned, y_raw=y_raw, report=report)


# ---------------------------------------------------------------------------
# ERP transaction-price smoothing (Phase 2A)
# ---------------------------------------------------------------------------

def smooth_erp_prices(
    y: pd.Series,
    window: int = 7,
    method: str = "median",
) -> pd.Series:
    """Apply rolling smoothing to ERP transaction prices.

    ERP prices have deal-to-deal noise that market quotes don't (bulk
    discounts, grade premiums, supplier-negotiated terms).  A rolling
    median preserves the underlying price trend while being robust to
    single-transaction outliers.

    Parameters
    ----------
    y : pd.Series
        Raw price series (datetime index).
    window : int
        Rolling window size in observations (default 7 = weekly).
    method : str
        "median" (default) or "mean".

    Returns
    -------
    pd.Series
        Smoothed series (same length as input, NA at the leading edge).
    """
    if len(y.dropna()) < window:
        logger.debug("smooth_erp_prices: series too short (%d < %d), skipping", len(y.dropna()), window)
        return y

    if method == "median":
        smoothed = y.rolling(window=window, center=False, min_periods=1).median()
    else:
        smoothed = y.rolling(window=window, center=False, min_periods=1).mean()

    # Drop leading/trailing NaN from rolling (fill with original values)
    result = smoothed.combine_first(y)
    logger.info(
        "Applied ERP smoothing (window=%d, method=%s): %d points → %d non-NA",
        window, method, len(y), result.notna().sum(),
    )
    return result
