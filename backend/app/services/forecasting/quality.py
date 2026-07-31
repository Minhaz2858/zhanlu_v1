"""Series quality scoring — 6 factors → 0-100 score → A/B/C/D grade.

Measures forecastability, not data quality in the general sense.
A high score means the series is likely to produce useful forecasts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

logger = logging.getLogger(__name__)

# ── factor weights (sum = 100) ────────────────────────────────────────

FACTOR_WEIGHTS = {
    "history_length": 25,
    "missing_ratio": 20,
    "outlier_ratio": 15,
    "stationarity": 15,
    "seasonality_strength": 15,
    "frequency_regularity": 10,
}

# ── grade thresholds ──────────────────────────────────────────────────

GRADE_THRESHOLDS = [
    (80, "A"),
    (60, "B"),
    (40, "C"),
    (0, "D"),
]


@dataclass
class QualityResult:
    """Immutable result of a quality evaluation."""

    grade: str  # A / B / C / D
    score: float  # 0-100
    stats: dict[str, Any] = field(default_factory=dict)


def score_series(
    y: pd.Series,
    weights: dict[str, float] | None = None,
) -> QualityResult:
    """Score a single time series for forecastability.

    Parameters
    ----------
    y : pd.Series
        The time series to evaluate.  May contain NaN (counted as missing).
    weights : dict or None
        If provided, override the default factor weights.  Keys must be
        a subset of ``FACTOR_WEIGHTS``; unspecified keys keep defaults.

    Returns
    -------
    QualityResult
    """
    w = dict(FACTOR_WEIGHTS)
    if weights:
        w.update(weights)

    y_clean = y.dropna()
    n = len(y)
    n_clean = len(y_clean)

    stats: dict[str, Any] = {
        "history_length": n,
        "clean_length": n_clean,
        "missing_count": n - n_clean,
    }

    # Short-circuit: not enough data for any assessment
    if n_clean < 2:
        return QualityResult(grade="D", score=0.0, stats=stats)

    total_score = 0.0

    # --- 1. History length (exponential saturation) -------------------
    # Score ramps quickly then saturates; 365+ days = full credit
    history_factor = min(1.0, np.log1p(n_clean) / np.log1p(365))
    stats["history_length_score"] = round(history_factor, 4)
    total_score += w["history_length"] * history_factor

    # --- 2. Missing ratio (linear penalty) ----------------------------
    missing_ratio = float((n - n_clean) / max(n, 1))
    missing_factor = max(0.0, 1.0 - missing_ratio)
    stats["missing_ratio"] = round(missing_ratio, 4)
    total_score += w["missing_ratio"] * missing_factor

    # --- 3. Outlier ratio (IQR method) --------------------------------
    q1 = float(y_clean.quantile(0.25))
    q3 = float(y_clean.quantile(0.75))
    iqr = q3 - q1
    if iqr > 0:
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        outlier_ratio = float(
            ((y_clean < lower) | (y_clean > upper)).sum() / n_clean
        )
    else:
        outlier_ratio = 0.0
    outlier_factor = max(0.0, 1.0 - outlier_ratio * 5)  # 20% outliers = 0 score
    stats["outlier_ratio"] = round(outlier_ratio, 4)
    total_score += w["outlier_ratio"] * outlier_factor

    # --- 4. Stationarity (ADF test p-value) ---------------------------
    try:
        adf_result = adfuller(y_clean.values, maxlag=min(12, n_clean // 2 - 1), autolag="AIC")
        adf_pvalue = float(adf_result[1])
    except Exception as exc:
        logger.debug("ADF test failed: %s", exc)
        adf_pvalue = 1.0
    stationarity_factor = 1.0 - min(1.0, adf_pvalue)
    stats["adf_pvalue"] = round(adf_pvalue, 4)
    total_score += w["stationarity"] * stationarity_factor

    # --- 5. Seasonality strength (STL variance ratio) -----------------
    seasonal_period = _guess_period(n_clean)
    seasonality_factor = _compute_seasonality(y_clean, seasonal_period)
    stats["seasonality_strength"] = round(seasonality_factor, 4)
    stats["detected_period"] = seasonal_period
    total_score += w["seasonality_strength"] * seasonality_factor

    # --- 6. Frequency regularity (CV of inter-arrival intervals) ------
    if hasattr(y.index, "to_series"):
        freq_factor = _compute_frequency_regularity(y.index)
    else:
        freq_factor = 1.0  # assume perfect if no datetime index
    stats["frequency_regularity"] = round(freq_factor, 4)
    total_score += w["frequency_regularity"] * freq_factor

    # Normalize to 0-100
    stats["total_score"] = round(total_score, 2)

    # --- Grade assignment ---------------------------------------------
    grade = "D"
    for threshold, label in GRADE_THRESHOLDS:
        if total_score >= threshold:
            grade = label
            break

    return QualityResult(grade=grade, score=round(total_score, 2), stats=stats)


# ── helpers ────────────────────────────────────────────────────────────

def _guess_period(n: int) -> int:
    """Guess a likely seasonal period based on series length."""
    if n >= 365:
        return 7  # weekly pattern dominates for long daily data
    if n >= 90:
        return 7
    if n >= 30:
        return 4  # approximate weekly
    return max(2, n // 4)  # short series — use a quarter


def _compute_seasonality(y: pd.Series, period: int) -> float:
    """Estimate seasonality strength via STL decomposition variance ratio.

    If the seasonal component accounts for a large share of total
    variance, the series is strongly seasonal → high score.
    """
    if len(y) < 2 * period:
        return 0.0

    try:
        from statsmodels.tsa.seasonal import STL, seasonal_decompose
    except ImportError:
        return 0.0

    try:
        stl = STL(y.values, period=period, robust=True)
        result = stl.fit()
        seasonal = result.seasonal
        resid = result.resid
    except Exception:
        try:
            result = seasonal_decompose(
                y.values, model="additive", period=period
            )
            seasonal = result.seasonal
            resid = result.resid
        except Exception as exc:
            logger.debug("STL/seasonal_decompose failed: %s", exc)
            return 0.0

    mask = ~np.isnan(seasonal) & ~np.isnan(resid)
    if mask.sum() < period:
        return 0.0

    var_seasonal = float(np.var(seasonal[mask]))
    var_residual = float(np.var(resid[mask]))
    total_var = var_seasonal + var_residual
    if total_var < 1e-10:
        return 0.0

    ratio = var_seasonal / total_var
    return max(0.0, min(1.0, float(ratio)))


def _compute_frequency_regularity(index: pd.Index) -> float:
    """Score how regular the time intervals are.

    CV (std / mean) of inter-arrival times.  Perfectly regular = 1.0.
    """
    try:
        if not isinstance(index, pd.DatetimeIndex):
            index = pd.to_datetime(index, errors="coerce")
        diffs = index.to_series().diff().dropna()
        if diffs.nunique() <= 1:
            return 1.0
        # Convert to seconds for meaningful CV
        seconds = diffs.dt.total_seconds()
        cv = abs(float(seconds.std() / max(seconds.mean(), 1.0)))
        # Exponential decay: CV=0.0 → 1.0, CV=1.0 → 0.37
        return max(0.0, float(np.exp(-cv)))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug("Frequency regularity failed: %s", exc)
        return 0.5  # neutral score when we can't assess
