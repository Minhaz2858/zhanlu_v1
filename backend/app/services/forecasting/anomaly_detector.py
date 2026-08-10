"""Ensemble anomaly detection for price time-series.

Combines three methods with majority-vote decision:
1. Isolation Forest (multivariate, sklearn)
2. STL residual Z-score (univariate, statsmodels)
3. Rolling IQR (univariate, no dependencies)

Flag-gated via FORECAST_ANOMALY_DETECTION_ENABLED (default false).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_SEASONAL = 7
_IF_CONTAMINATION = 0.05      # expected outlier fraction for IsolationForest
_STL_SIGMA = 3.0               # Z-score threshold for STL residuals
_IQR_MULTIPLIER = 2.5          # multiplier for rolling IQR fence


@dataclass
class AnomalyResult:
    anomaly_indices: list[int]   # positions in the cleaned series
    anomaly_scores: list[float]  # 0.0-1.0 per point (higher = more anomalous)
    method_votes: list[list[str]]  # which methods flagged each point


def detect_anomalies(
    y: pd.Series,
    seasonal_period: int = _DEFAULT_SEASONAL,
) -> AnomalyResult:
    """Detect anomalous price observations using a 3-method ensemble.

    Args:
        y: Price series (pandas Series with datetime index).
        seasonal_period: Expected season length for STL decomposition.

    Returns:
        AnomalyResult with flagged indices, scores, and vote details.
    """
    y_work = y.dropna()
    if len(y_work) < 2 * seasonal_period:
        return AnomalyResult(
            anomaly_indices=[], anomaly_scores=[], method_votes=[],
        )

    values = y_work.values.astype(float)
    n = len(values)
    votes = np.zeros(n, dtype=int)
    score_accum = np.zeros(n, dtype=float)
    method_hits: list[list[str]] = [[] for _ in range(n)]

    # --- Method 1: Isolation Forest -------------------------------------------
    try:
        from sklearn.ensemble import IsolationForest

        X_if = _build_if_features(values, seasonal_period)
        if_clf = IsolationForest(
            contamination=_IF_CONTAMINATION,
            random_state=42,
            n_estimators=100,
        )
        preds = if_clf.fit_predict(X_if)  # -1 = outlier, 1 = inlier
        scores = if_clf.score_samples(X_if)
        # Normalize scores to 0-1 range (higher = more anomalous)
        s_min, s_max = scores.min(), scores.max()
        if s_max > s_min:
            norm_scores = (scores - s_min) / (s_max - s_min)
        else:
            norm_scores = np.zeros_like(scores)

        for i in range(n):
            if preds[i] == -1:
                votes[i] += 1
                method_hits[i].append("isolation_forest")
            score_accum[i] += norm_scores[i]
    except ImportError:
        logger.debug("sklearn not installed — Isolation Forest skipped")
    except Exception as exc:
        logger.warning("Isolation Forest anomaly detection failed: %s", exc)

    # --- Method 2: STL residual Z-score ---------------------------------------
    try:
        from statsmodels.tsa.seasonal import STL

        # Use STL robust to avoid influence of potential outliers
        stl = STL(values, period=max(3, seasonal_period), robust=True)
        res = stl.fit()
        residuals = res.resid
        sigma = np.std(residuals) if np.std(residuals) > 0 else 1.0
        residual_z = np.abs(residuals) / sigma

        for i in range(n):
            if residual_z[i] > _STL_SIGMA:
                votes[i] += 1
                method_hits[i].append("stl_zscore")
            score_accum[i] += min(1.0, residual_z[i] / (_STL_SIGMA * 2.0))
    except ImportError:
        logger.debug("statsmodels not installed — STL Z-score skipped")
    except Exception as exc:
        logger.warning("STL residual anomaly detection failed: %s", exc)

    # --- Method 3: Rolling IQR -------------------------------------------------
    window = max(3, min(seasonal_period * 2, n // 4))
    for i in range(window, n - window):
        w_start = max(0, i - window)
        w_end = min(n, i + window + 1)
        local_vals = values[w_start:w_end]
        q1, q3 = np.percentile(local_vals, [25, 75])
        iqr = q3 - q1
        if iqr < 1e-10:
            continue
        lower = q1 - _IQR_MULTIPLIER * iqr
        upper = q3 + _IQR_MULTIPLIER * iqr
        if values[i] < lower or values[i] > upper:
            votes[i] += 1
            method_hits[i].append("rolling_iqr")
            # Score: how many IQR-multiples beyond the fence
            excess = max(abs(values[i] - lower), abs(values[i] - upper)) / (iqr + 1e-10)
            score_accum[i] += min(1.0, excess / (_IQR_MULTIPLIER * 2.0))

    # --- Majority vote: require at least 2 out of 3 methods -------------------
    anomaly_indices: list[int] = []
    anomaly_scores: list[float] = []
    for i in range(n):
        if votes[i] >= 2:
            anomaly_indices.append(i)
            anomaly_scores.append(round(float(score_accum[i] / max(1, votes[i])), 3))

    if anomaly_indices:
        logger.info(
            "Anomaly detection: %d/%d points flagged (%.1f%%)",
            len(anomaly_indices), n, 100 * len(anomaly_indices) / n,
        )

    return AnomalyResult(
        anomaly_indices=anomaly_indices,
        anomaly_scores=anomaly_scores,
        method_votes=method_hits,
    )


def _build_if_features(values: np.ndarray, period: int) -> np.ndarray:
    """Build feature matrix for Isolation Forest from lagged values."""
    n = len(values)
    features = []
    # Rolling mean and std
    for w in [3, 5, min(period, n // 3)]:
        if w < 3:
            continue
        roll_mean = np.full(n, np.nan)
        roll_std = np.full(n, np.nan)
        for i in range(n):
            start = max(0, i - w + 1)
            roll_mean[i] = np.mean(values[start:i + 1])
            if i > start:
                roll_std[i] = np.std(values[start:i + 1])
        features.append(roll_mean)
        features.append(roll_std)

    # Lagged returns
    for lag in [1, 3, min(period, n // 2)]:
        if lag < 1:
            continue
        ret = np.full(n, np.nan)
        ret[lag:] = (values[lag:] - values[:-lag]) / (np.abs(values[:-lag]) + 1e-10)
        features.append(ret)

    X = np.column_stack(features)
    # Replace NaN with column mean
    for j in range(X.shape[1]):
        col_mean = np.nanmean(X[:, j])
        X[np.isnan(X[:, j]), j] = col_mean if not np.isnan(col_mean) else 0.0

    return X
