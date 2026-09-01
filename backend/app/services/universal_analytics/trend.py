"""Trend analysis engine.

Given a time series (pd.Series), computes:
- direction: "up", "down", or "flat"
- slope: linear trend coefficient
- strength: weak / moderate / strong (based on R²)
- moving_average: rolling window list (when window > 0)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def analyze_trend(
    series: pd.Series,
    window: int = 0,
) -> dict:
    """Analyze trend direction, slope, and strength for a time series.

    Args:
        series: Time-indexed pd.Series of numeric values.
        window: Moving-average window size (0 = skip MA).

    Returns:
        dict with keys: direction, slope, strength, moving_average,
                        series_length, mean, volatility.
    """
    if len(series) < 2:
        return {
            "direction": "flat",
            "slope": 0.0,
            "strength": "insufficient_data",
            "moving_average": [],
            "series_length": len(series),
            "mean": float(series.mean()) if len(series) > 0 else 0.0,
            "volatility": 0.0,
        }

    # Drop NaN values
    clean = series.dropna()
    if len(clean) < 2:
        return {
            "direction": "flat",
            "slope": 0.0,
            "strength": "insufficient_data",
            "moving_average": [],
            "series_length": len(series),
            "mean": float(series.mean()) if len(series) > 0 else 0.0,
            "volatility": 0.0,
        }

    # Linear regression slope
    x = np.arange(len(clean)).astype(float)
    y = clean.values.astype(float)
    slope, intercept = np.polyfit(x, y, 1)

    # R² for strength classification
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    if abs(slope) < 1e-9:
        direction = "flat"
    elif slope > 0:
        direction = "up"
    else:
        direction = "down"

    if r_squared >= 0.7:
        strength = "strong"
    elif r_squared >= 0.3:
        strength = "moderate"
    else:
        strength = "weak"

    # Moving average
    ma = []
    if window > 0 and len(clean) >= window:
        ma = clean.rolling(window=window).mean().dropna().tolist()

    return {
        "direction": direction,
        "slope": float(slope),
        "strength": strength,
        "moving_average": [float(v) for v in ma],
        "series_length": len(series),
        "mean": float(clean.mean()),
        "volatility": float(clean.std()) if len(clean) > 1 else 0.0,
    }
