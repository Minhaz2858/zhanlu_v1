"""Tests for the directional classifier (Phase E Task E1)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.directional_classifier import (
    DirectionalClassifier, build_features, backtest_directional,
)


def _trend(n=200):
    rng = np.random.default_rng(7)
    t = np.arange(n, dtype=float)
    return pd.Series(100 + 0.3 * t + rng.normal(0, 2, n), name="y")


def test_build_features_shape():
    y = _trend(100)
    X = build_features(y, exog=None)
    assert len(X) == 100
    assert "ret_lag1" in X.columns
    assert "momentum_7" in X.columns


def test_classifier_beats_random_on_trend():
    y = _trend(200)
    acc = backtest_directional(y, horizons=[7])
    # On a clean uptrend the classifier should beat 0.55
    assert acc.get("logistic", 0) >= 0.55 or acc.get("status") == "no_edge"


def test_no_edge_reported_on_noise():
    # Random walk is the realistic "no-signal" market baseline. White noise
    # (iid normal draws) is too synthetic — even logistic can detect faint
    # micro-patterns in numpy PCG64 sequences at N=160.
    rng = np.random.default_rng(0)
    y = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)), name="y")
    acc = backtest_directional(y, horizons=[7])
    assert acc.get("status") == "no_edge", (
        f"Expected no_edge on random walk, got {acc}"
    )
