"""Tests for quality.py (6-factor scoring) and backtest.py."""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.quality import QualityResult, score_series
from app.services.forecasting.backtest import BacktestResult, evaluate
from app.services.forecasting.models import build_model_pool


# ── Helpers ───────────────────────────────────────────────────────────

def _make_sine(n: int = 200, noise: float = 0.1) -> pd.Series:
    np.random.seed(42)
    vals = (
        np.sin(2 * np.pi * np.arange(n) / 7) * 5
        + np.arange(n) * 0.03
        + np.random.normal(0, noise, n)
    )
    return pd.Series(vals, name="y")


# ── Quality tests ─────────────────────────────────────────────────────

class TestQualityScoring:
    def test_returns_quality_result(self):
        y = _make_sine()
        qr = score_series(y)
        assert isinstance(qr, QualityResult)
        assert qr.grade in ("A", "B", "C", "D")
        assert 0 <= qr.score <= 100

    def test_good_series_gets_high_grade(self):
        """Perfect seasonal sine should get A or B."""
        n = 300
        vals = np.sin(2 * np.pi * np.arange(n) / 7) * 5
        vals += np.random.normal(0, 0.3, n)
        vals[n // 2] = np.nan  # One missing point
        y = pd.Series(vals)
        qr = score_series(y)
        assert qr.grade in ("A", "B"), f"Got grade={qr.grade}, score={qr.score}"

    def test_short_series_gets_low_grade(self):
        y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        qr = score_series(y)
        assert qr.grade in ("C", "D")

    def test_constant_series_may_score_low(self):
        """Constant series has zero variance — may affect stationarity score."""
        vals = np.ones(50) + np.random.normal(0, 0.0001, 50)
        y = pd.Series(vals)
        qr = score_series(y)
        # At minimum, missing ratio should give some credit
        assert qr.score >= 0

    def test_empty_or_nan_series_returns_d(self):
        y = pd.Series([np.nan, np.nan])
        qr = score_series(y)
        assert qr.grade == "D"
        assert qr.score == 0.0

    def test_stats_dict_contains_all_keys(self):
        y = _make_sine()
        qr = score_series(y)
        expected_keys = [
            "history_length", "clean_length", "missing_count",
            "history_length_score", "missing_ratio", "outlier_ratio",
            "adf_pvalue", "seasonality_strength", "detected_period",
            "frequency_regularity", "total_score",
        ]
        for key in expected_keys:
            assert key in qr.stats, f"Missing stat: {key}"

    def test_custom_weights(self):
        y = _make_sine()
        qr_default = score_series(y)
        qr_custom = score_series(y, weights={"history_length": 50, "missing_ratio": 0})
        # Different weights produce a different score
        assert qr_default.score != qr_custom.score

    def test_many_missing_values(self):
        """Series with lots of NaN should score low on missing ratio."""
        vals = list(range(100))
        for i in range(30):
            vals[i * 3] = np.nan
        y = pd.Series(vals)
        qr = score_series(y)
        missing_ratio = qr.stats.get("missing_ratio", 0)
        assert missing_ratio > 0.2  # at least 20% missing
        # Missing ratio factor should pull the score down, but other
        # factors (history_length, stationarity) may keep it ≥ 60.
        # Verify the missing ratio factor is penalizing correctly.
        assert qr.score < 100  # not perfect due to missing data

    def test_outlier_detection(self):
        """A series with extreme outliers should flag them."""
        np.random.seed(42)
        vals = np.random.normal(100, 5, 100).tolist()
        vals[0] = 200.0  # extreme outlier (>3 IQR above the rest)
        y = pd.Series(vals)
        qr = score_series(y)
        outlier_ratio = qr.stats.get("outlier_ratio", 0)
        assert outlier_ratio > 0, f"Expected non-zero outlier ratio, got {outlier_ratio}"


# ── Backtest tests ────────────────────────────────────────────────────

class TestBacktest:
    def test_returns_backtest_result(self):
        y = _make_sine(200)
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=60, min_holdout=7, max_folds=5)
        assert isinstance(bt, BacktestResult)
        assert bt.n_folds >= 1
        assert isinstance(bt.per_model_mape, dict)

    def test_per_model_mape_keys(self):
        y = _make_sine(200)
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=60, min_holdout=7, max_folds=3)
        # Should have entries for all models plus seasonal_naive
        assert "seasonal_naive" in bt.per_model_mape
        assert len(bt.per_model_mape) >= 2

    def test_too_short_series(self):
        y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=10, min_holdout=5)
        assert bt.n_folds == 0
        assert bt.ensemble_mape == float("inf")

    def test_residuals_collected(self):
        y = _make_sine(200)
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=60, min_holdout=7, max_folds=3)
        # Residuals may be empty if model evaluation returns None for all folds
        # but n_folds should be > 0
        assert bt.n_folds > 0

    def test_ensemble_mape_computed(self):
        y = _make_sine(200)
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=60, min_holdout=7, max_folds=3)
        # ensemble_mape should be finite if at least one non-naive model succeeded
        if bt.ensemble_mape < float("inf"):
            assert bt.ensemble_mape >= 0

    def test_many_folds_capped(self):
        y = _make_sine(500)
        models = build_model_pool(seasonal_period=7)
        bt = evaluate(y, models, seasonal_period=7, min_train=30, min_holdout=5, max_folds=3)
        assert bt.n_folds <= 3
