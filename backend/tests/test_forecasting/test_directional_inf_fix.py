"""Test Bug 1 fix: inf values from pct_change on near-zero prices.

Before the fix, build_features() on series with zero or near-zero values
produced inf/-inf that killed every LogisticRegression.fit(), leaving
n_test=0 and making backtest_directional always return "no_edge".
"""

import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.directional_classifier import (
    DirectionalClassifier, backtest_directional, build_features,
)


class TestInfFix:
    """Verify build_features handles zero/near-zero price series safely."""

    def test_build_features_no_inf_on_zero_price(self):
        """A series containing zero values must not produce inf or -inf."""
        # 100 points: 20 zeros followed by 80 positive values
        rng = np.random.RandomState(42)
        vals = np.concatenate([np.zeros(20), rng.uniform(0.1, 1.0, 80)])
        y = pd.Series(vals, index=pd.date_range("2024-01-01", periods=100, freq="D"))
        X = build_features(y)
        assert X is not None
        assert not np.isinf(X.values).any(), "build_features produced inf values"
        assert not np.isinf(-X.values).any(), "build_features produced -inf values"

    def test_build_features_no_inf_on_near_zero_price(self):
        """Near-zero values (1e-12) from pct_change must not produce inf."""
        y = pd.Series(
            [100.0, 1e-12, 100.0] * 30,
            index=pd.date_range("2024-01-01", periods=90, freq="D"),
        )
        X = build_features(y)
        assert X is not None
        assert not np.isinf(X.values).any(), "near-zero pct_change produced inf"

    def test_backtest_with_zeros_returns_no_edge_not_crash(self):
        """backtest_directional on zero-containing series must not crash."""
        rng = np.random.RandomState(42)
        vals = np.concatenate([np.zeros(5), rng.uniform(0.5, 2.0, 200)])
        y = pd.Series(vals, index=pd.date_range("2024-01-01", periods=205, freq="D"))
        result = backtest_directional(y, horizons=(7,))
        assert isinstance(result, dict)
        # Must return a valid status — never crash
        assert result["status"] in ("edge", "no_edge")

    def test_cv_eval_logistic_fits_with_zeros(self):
        """The classifier must fit without exception on zero-containing series.
        build_features drops date index (uses .values internally), so we
        align on row positions: features start at row `first_valid`, labels
        end at `len(y) - horizon`. Overlap must be ≥ 30 rows."""
        rng = np.random.RandomState(42)
        vals = np.concatenate([np.zeros(5), rng.uniform(0.5, 2.0, 250)])
        y = pd.Series(vals, index=pd.date_range("2024-01-01", periods=255, freq="D"))
        X = build_features(y)
        horizon = 7
        # build_features uses .values → returns default RangeIndex
        # Lag features have NaN for first ~14 rows; future_sign has NaN for last 7
        first_valid = X.first_valid_index() if hasattr(X, "first_valid_index") else 14
        if first_valid is None or first_valid == 0:
            # Determine empirically: first row where all columns are non-NaN
            first_valid = int(X.notna().all(axis=1).idxmax())
        X_valid = X.iloc[first_valid:-horizon].reset_index(drop=True)
        future_sign = (y.shift(-horizon) > y).astype(int)
        y_valid = future_sign.iloc[first_valid:-horizon].astype(int).reset_index(drop=True)
        assert len(X_valid) >= 30, f"Need ≥30 aligned rows, got {len(X_valid)}"
        clf = DirectionalClassifier()
        clf.fit(X_valid, y_valid)
        prob = clf.predict_proba(X_valid.iloc[[-1]])
        assert prob is not None
        assert 0.0 <= float(prob[0]) <= 1.0
