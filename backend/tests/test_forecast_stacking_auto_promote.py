"""Test stacking meta-learner champion/challenger auto-promotion.

Covers:
- StackingMetaLearner.compute_mape() cross-validated MAPE
- ChallengerShadowRun persistence in engine.py (mocked)
- Auto-promotion after consecutive winning nights
"""
import datetime as _dt
import math
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.services.forecasting.models.stacking_meta import StackingMetaLearner
from app.services.forecasting.ops.champion_challenger import (
    ChampionChallengerTracker,
    ShadowForecastResult,
    run_nightly_champion_challenger,
    _MIN_CONSECUTIVE_NIGHTS,
    _MIN_IMPROVEMENT_PP,
)


# ---------------------------------------------------------------------------
# StackingMetaLearner.compute_mape()
# ---------------------------------------------------------------------------

class TestStackingComputeMape:
    """Tests for the cross-validated MAPE method."""

    def test_compute_mape_returns_finite_value(self):
        """With 3+ folds of reasonable data, compute_mape should return a finite float."""
        stacker = StackingMetaLearner(alpha=1.0, scale=True)
        np.random.seed(42)
        for _ in range(5):
            actuals = np.random.uniform(100, 200, size=20)
            preds = {
                "xgb": actuals + np.random.normal(0, 5, size=20),
                "naive": actuals + np.random.normal(0, 15, size=20),
            }
            stacker.record_fold(preds, actuals)

        assert stacker.fit_meta() is True
        mape = stacker.compute_mape()
        assert mape is not None
        assert math.isfinite(mape)
        assert 0 < mape < 50  # reasonable range for synthetic data

    def test_compute_mape_none_when_insufficient_folds(self):
        """With <2 folds, compute_mape should return None."""
        stacker = StackingMetaLearner()
        stacker.record_fold({"a": [1, 2], "b": [2, 3]}, [1.5, 2.5])
        mape = stacker.compute_mape()
        assert mape is None

    def test_compute_mape_better_than_worst_base(self):
        """Stacking MAPE should generally be <= worst base model MAPE."""
        np.random.seed(123)
        stacker = StackingMetaLearner(alpha=1.0)
        n_folds = 5
        for _ in range(n_folds):
            actuals = np.random.uniform(50, 150, size=30)
            good_preds = actuals + np.random.normal(0, 3, size=30)
            bad_preds = actuals + np.random.normal(0, 20, size=30)
            stacker.record_fold({"good": good_preds, "bad": bad_preds}, actuals)

        assert stacker.fit_meta() is True
        stack_mape = stacker.compute_mape()

        # Compute per-model MAPE for comparison
        X_all = np.column_stack([
            np.concatenate([df["good"].values for df in stacker._preds_list]),
            np.concatenate([df["bad"].values for df in stacker._preds_list]),
        ])
        y_all = np.concatenate(stacker._actuals_list)
        bad_mape = np.mean(np.abs((y_all - X_all[:, 1]) / y_all)) * 100

        assert stack_mape is not None
        assert stack_mape <= bad_mape + 5  # allow small margin for CV variance


# ---------------------------------------------------------------------------
# ChampionChallengerTracker (in-memory)
# ---------------------------------------------------------------------------

class TestChampionChallengerTracker:
    """Tests for the in-memory tracker."""

    def test_no_promotion_without_enough_weeks(self):
        tracker = ChampionChallengerTracker()
        for _ in range(3):
            tracker.record_weekly_result("prod_A", 5.0, 10.0)  # 50% improvement
        result = tracker.check_promotion("prod_A")
        assert result is None  # need 4 consecutive weeks

    def test_promotion_with_enough_weeks(self):
        tracker = ChampionChallengerTracker()
        for _ in range(4):
            tracker.record_weekly_result("prod_A", 5.0, 10.0)
        result = tracker.check_promotion("prod_A")
        assert result is not None
        assert result["product_id"] == "prod_A"
        assert result["improvement_pct"] > 0


# ---------------------------------------------------------------------------
# Nightly auto-promotion (DB-backed)
# ---------------------------------------------------------------------------

class TestNightlyAutoPromotion:
    """Tests for run_nightly_champion_challenger auto-promotion logic."""

    def _make_mock_db(self, targets, shadow_runs):
        """Build a mock DB session with targets and shadow runs."""
        db = MagicMock()

        # Mock target query
        db.query.return_value.filter.return_value.all.return_value = targets

        # Mock shadow run queries — need to handle chained filters
        mock_shadow_q = MagicMock()
        mock_shadow_q.filter.return_value = mock_shadow_q
        mock_shadow_q.order_by.return_value = mock_shadow_q
        mock_shadow_q.limit.return_value = mock_shadow_q
        mock_shadow_q.all.return_value = shadow_runs
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = shadow_runs

        return db

    def test_no_promotion_when_shadow_delta_below_threshold(self):
        """If stacking MAPE is only marginally better, no promotion."""
        target = MagicMock()
        target.id = "t1"
        target.product_key = "naphtha"
        target.model_config = {}

        shadow = MagicMock()
        shadow.shadow_delta_mape = 0.5  # below _MIN_IMPROVEMENT_PP (1.0)

        db = self._make_mock_db([target], [shadow])
        result = run_nightly_champion_challenger(db)
        assert result["promotions"] == 0

    def test_promotion_when_consecutive_wins(self):
        """If stacking wins 3 consecutive nights with delta >= 1pp, promote."""
        target = MagicMock()
        target.id = "t1"
        target.product_key = "naphtha"
        target.model_config = {}

        shadows = [
            MagicMock(shadow_delta_mape=2.0),
            MagicMock(shadow_delta_mape=1.5),
            MagicMock(shadow_delta_mape=1.2),
        ]

        db = self._make_mock_db([target], shadows)
        result = run_nightly_champion_challenger(db)
        assert result["promotions"] == 1

        # Check that ensemble_overrides were written
        override = target.model_config.get("ensemble_overrides", {})
        assert override.get("source") == "stacking_meta"
        assert override.get("weights", {}).get("stacking") == 0.6

    def test_no_promotion_if_streak_broken(self):
        """If the most recent run has delta < threshold, no promotion."""
        target = MagicMock()
        target.id = "t1"
        target.product_key = "naphtha"
        target.model_config = {}

        shadows = [
            MagicMock(shadow_delta_mape=0.3),  # most recent — streak broken
            MagicMock(shadow_delta_mape=2.0),
            MagicMock(shadow_delta_mape=1.5),
        ]

        db = self._make_mock_db([target], shadows)
        result = run_nightly_champion_challenger(db)
        assert result["promotions"] == 0

    def test_no_double_promotion(self):
        """If already promoted (ensemble_overrides source=stacking_meta), skip."""
        target = MagicMock()
        target.id = "t1"
        target.product_key = "naphtha"
        target.model_config = {
            "ensemble_overrides": {"source": "stacking_meta", "weights": {"stacking": 0.6}},
        }

        shadows = [
            MagicMock(shadow_delta_mape=2.0),
            MagicMock(shadow_delta_mape=1.5),
            MagicMock(shadow_delta_mape=1.2),
        ]

        db = self._make_mock_db([target], shadows)
        result = run_nightly_champion_challenger(db)
        assert result["promotions"] == 0
