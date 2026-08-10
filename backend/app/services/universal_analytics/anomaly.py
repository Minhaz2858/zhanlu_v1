"""Statistical anomaly detection (P5).

Flag-gated behind UNIVERSAL_ANALYTICS_ANOMALY (default OFF) because it
requires scipy for seasonal decomposition (optional heavy import).

Methods:
  - zscore:  Flag points with |z| > 3.
  - iqr:     Flag points outside 1.5 * IQR from Q1/Q3.
  - seasonal: Flag residual outliers after seasonal decomposition (scipy).

When disabled, returns an empty list.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd


def is_anomaly_enabled() -> bool:
    """Check whether anomaly detection is enabled."""
    return os.environ.get("UNIVERSAL_ANALYTICS_ANOMALY", "false").lower() in (
        "true", "1", "yes",
    )


def detect_anomalies(
    series: pd.Series,
    method: str = "zscore",
) -> list[dict]:
    """Detect anomalies in a time series.

    Args:
        series: Time-indexed pd.Series of numeric values.
        method: "zscore" (default), "iqr", or "seasonal".

    Returns:
        List of dicts with keys: index (str date), value (float),
        method, threshold, z_score (when method=zscore).
        Empty list if disabled or no anomalies found.
    """
    if not is_anomaly_enabled():
        return []

    # Drop NaN values
    clean = series.dropna()
    if len(clean) < 3:
        return []

    if method == "zscore":
        return _zscore_detect(clean)
    elif method == "iqr":
        return _iqr_detect(clean)
    elif method == "seasonal":
        return _seasonal_detect(clean)
    else:
        return _zscore_detect(clean)


# ── Methods ─────────────────────────────────────────────────────────


def _zscore_detect(series: pd.Series) -> list[dict]:
    """|z| > 3 → anomaly."""
    std = series.std()
    if std < 1e-12:
        return []  # constant series

    mean = series.mean()
    z = (series - mean) / std
    anomalies = series[abs(z) > 3]

    results = []
    for idx in anomalies.index:
        dt_str = (
            idx.isoformat()[:10]
            if hasattr(idx, "isoformat")
            else str(idx)
        )
        results.append({
            "index": dt_str,
            "value": float(series[idx]),
            "method": "zscore",
            "z_score": float(z[idx]),
            "threshold": 3.0,
        })
    return results


def _iqr_detect(series: pd.Series) -> list[dict]:
    """Points outside 1.5 * IQR from Q1/Q3."""
    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    iqr = q3 - q1
    if iqr < 1e-12:
        return []

    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    anomalies = series[(series < lower) | (series > upper)]

    results = []
    for idx in anomalies.index:
        dt_str = (
            idx.isoformat()[:10]
            if hasattr(idx, "isoformat")
            else str(idx)
        )
        results.append({
            "index": dt_str,
            "value": float(series[idx]),
            "method": "iqr",
            "lower_bound": float(lower),
            "upper_bound": float(upper),
        })
    return results


def _seasonal_detect(series: pd.Series) -> list[dict]:
    """Seasonal decomposition residual outliers (requires scipy).

    Falls back to zscore when scipy is unavailable.
    """
    try:
        from scipy.stats import zscore as zscore_fn
        return _zscore_detect(series)
    except ImportError:
        return _zscore_detect(series)
