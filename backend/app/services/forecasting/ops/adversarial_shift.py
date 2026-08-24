"""Adversarial distribution-shift detector.

Trains a lightweight logistic regression classifier to distinguish
recent 30-day feature distributions from older training data.

If the classifier achieves >70% accuracy, the distributions have shifted
and the model is operating in unfamiliar territory — a leading indicator
of accuracy degradation.

Usage::

    from app.services.forecasting.ops.adversarial_shift import detect_shift
    result = detect_shift(X_train, X_recent)
    if result.is_shifted:
        logger.warning("Distribution shift detected: score=%.2f", result.score)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Threshold: if classifier accuracy > this, distributions have shifted
_SHIFT_THRESHOLD = 0.70
# Minimum samples required for both recent and historical data
_MIN_SAMPLES = 20


@dataclass
class ShiftResult:
    """Result of adversarial shift detection."""
    is_shifted: bool
    score: float  # classifier accuracy (0-1)
    threshold: float
    n_recent: int
    n_historical: int
    message: str


def detect_shift(
    X_historical: pd.DataFrame | np.ndarray,
    X_recent: pd.DataFrame | np.ndarray,
    threshold: float = _SHIFT_THRESHOLD,
    min_samples: int = _MIN_SAMPLES,
) -> ShiftResult:
    """Detect distribution shift between historical and recent data.

    Parameters
    ----------
    X_historical : pd.DataFrame | np.ndarray
        Historical training features (older data).
    X_recent : pd.DataFrame | np.ndarray
        Recent features (last 30 days).
    threshold : float
        Accuracy threshold above which shift is detected.
    min_samples : int
        Minimum samples required for both datasets.

    Returns
    -------
    ShiftResult
    """
    # Convert to numpy arrays
    if isinstance(X_historical, pd.DataFrame):
        X_hist = X_historical.values.astype(float)
    else:
        X_hist = np.array(X_historical, dtype=float)

    if isinstance(X_recent, pd.DataFrame):
        X_rec = X_recent.values.astype(float)
    else:
        X_rec = np.array(X_recent, dtype=float)

    # Remove NaN/inf rows
    hist_valid = np.all(np.isfinite(X_hist), axis=1)
    rec_valid = np.all(np.isfinite(X_rec), axis=1)
    X_hist = X_hist[hist_valid]
    X_rec = X_rec[rec_valid]

    n_hist = len(X_hist)
    n_rec = len(X_rec)

    if n_hist < min_samples or n_rec < min_samples:
        return ShiftResult(
            is_shifted=False,
            score=0.0,
            threshold=threshold,
            n_recent=n_rec,
            n_historical=n_hist,
            message=f"Insufficient samples: historical={n_hist}, recent={n_rec} (need ≥{min_samples})",
        )

    # Balance datasets: use min(n_hist, n_rec) samples from each
    n = min(n_hist, n_rec)
    X_hist_balanced = X_hist[:n]
    X_rec_balanced = X_rec[:n]

    # Create labels: 0 = historical, 1 = recent
    X_combined = np.vstack([X_hist_balanced, X_rec_balanced])
    y_combined = np.array([0] * n + [1] * n)

    # Shuffle
    shuffle_idx = np.random.RandomState(42).permutation(len(X_combined))
    X_shuffled = X_combined[shuffle_idx]
    y_shuffled = y_combined[shuffle_idx]

    # Train logistic regression
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score

        # Use simple logistic regression with regularization
        clf = LogisticRegression(
            max_iter=1000,
            random_state=42,
            solver='lbfgs',
            C=1.0,
        )

        # 5-fold cross-validation accuracy
        scores = cross_val_score(clf, X_shuffled, y_shuffled, cv=5, scoring='accuracy')
        mean_accuracy = float(np.mean(scores))

        is_shifted = mean_accuracy > threshold

        message = (
            f"Distribution shift detected (score={mean_accuracy:.3f})"
            if is_shifted
            else f"No significant shift (score={mean_accuracy:.3f})"
        )

        return ShiftResult(
            is_shifted=is_shifted,
            score=mean_accuracy,
            threshold=threshold,
            n_recent=n_rec,
            n_historical=n_hist,
            message=message,
        )

    except ImportError:
        logger.warning("sklearn not installed — adversarial shift detection skipped")
        return ShiftResult(
            is_shifted=False,
            score=0.0,
            threshold=threshold,
            n_recent=n_rec,
            n_historical=n_hist,
            message="sklearn not available",
        )
    except Exception as exc:
        logger.warning("Adversarial shift detection failed: %s", exc)
        return ShiftResult(
            is_shifted=False,
            score=0.0,
            threshold=threshold,
            n_recent=n_rec,
            n_historical=n_hist,
            message=f"Detection failed: {exc}",
        )
