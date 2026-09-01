"""Tests for decision_roi.py (pure functions, zero I/O)."""
from __future__ import annotations

import pytest

from app.services.forecasting.features.decision_roi import (
    aggregate_roi,
    grid_search_thresholds,
    replay_under_thresholds,
    score_decision,
    score_pending_decisions,
    RoiSummary,
)


class DummyLog:
    """Minimal object standing in for ForecastDecisionLog rows."""
    def __init__(self, log_id, action, roi_pct=None,
                 predicted_p_rise=None, predicted_change_pct=None,
                 actual_price_t=None, actual_price_th=None):
        self.id = log_id
        self.action = action
        self.roi_pct = roi_pct
        self.predicted_p_rise = predicted_p_rise
        self.predicted_change_pct = predicted_change_pct
        self.actual_price_t = actual_price_t
        self.actual_price_th = actual_price_th


# ------------------------------------------------------------------ #
# score_decision
# ------------------------------------------------------------------ #

class TestScoreDecision:
    def test_buy_good(self):
        assert score_decision("buy", 100.0, 110.0) == pytest.approx(10.0)

    def test_buy_bad(self):
        assert score_decision("buy", 100.0, 90.0) == pytest.approx(-10.0)

    def test_sell_good(self):
        assert score_decision("sell", 110.0, 100.0) == pytest.approx(9.0909, rel=1e-3)

    def test_sell_bad(self):
        assert score_decision("sell", 100.0, 110.0) == pytest.approx(-10.0)

    def test_hold_zero(self):
        assert score_decision("hold", 100.0, 110.0) == 0.0

    def test_watch_zero(self):
        assert score_decision("watch", 100.0, 110.0) == 0.0

    def test_zero_price_returns_zero(self):
        assert score_decision("buy", 0.0, 110.0) == 0.0
        assert score_decision("sell", 110.0, 0.0) == 0.0

    def test_margin_subtraction(self):
        # ROI = 10% - 0.5% margin
        assert score_decision("buy", 100, 110, margin_pct=0.5) == pytest.approx(9.5)


# ------------------------------------------------------------------ #
# score_pending_decisions
# ------------------------------------------------------------------ #

class TestScorePendingDecisions:
    def test_scores_realized_logs(self):
        logs = [
            DummyLog(1, "buy", actual_price_t=100, actual_price_th=110),
            DummyLog(2, "sell", actual_price_t=110, actual_price_th=100),
            DummyLog(3, "hold", actual_price_t=100, actual_price_th=110),
        ]
        results = score_pending_decisions(logs)
        assert len(results) == 3
        assert results[0]["roi_pct"] == pytest.approx(10.0)
        assert results[1]["roi_pct"] == pytest.approx(9.0909, rel=1e-3)
        assert results[2]["roi_pct"] == 0.0

    def test_skips_no_price(self):
        logs = [DummyLog(1, "buy", actual_price_t=None)]
        assert score_pending_decisions(logs) == []


# ------------------------------------------------------------------ #
# aggregate_roi
# ------------------------------------------------------------------ #

class TestAggregateRoi:
    def test_mixed_bag(self):
        logs = [
            DummyLog(1, "buy", roi_pct=10.0),
            DummyLog(2, "buy", roi_pct=-5.0),
            DummyLog(3, "sell", roi_pct=8.0),
            DummyLog(4, "sell", roi_pct=-3.0),
            DummyLog(5, "hold", roi_pct=None),
        ]
        summary = aggregate_roi(logs)
        assert summary.total_decisions == 5
        assert summary.buy_count == 2
        assert summary.sell_count == 2
        assert summary.hold_count == 1
        assert summary.buy_roi_avg == pytest.approx(2.5)
        assert summary.sell_roi_avg == pytest.approx(2.5)
        assert summary.buy_correct == 1  # only the +10 one
        assert summary.sell_correct == 1  # only the +8 one
        assert summary.accuracy_pct == pytest.approx(50.0)

    def test_no_realized(self):
        logs = [DummyLog(1, "hold"), DummyLog(2, "watch")]
        summary = aggregate_roi(logs)
        assert summary.total_decisions == 2
        assert summary.hold_count == 2
        assert summary.weighted_roi == 0.0


# ------------------------------------------------------------------ #
# replay_under_thresholds
# ------------------------------------------------------------------ #

class TestReplayUnderThresholds:
    def test_replay(self):
        logs = [
            DummyLog(1, "buy", predicted_p_rise=0.75, predicted_change_pct=0.05,
                     actual_price_t=100, actual_price_th=105),
            DummyLog(2, "hold", predicted_p_rise=0.55, predicted_change_pct=0.01,
                     actual_price_t=100, actual_price_th=102),
            DummyLog(3, "sell", predicted_p_rise=0.25, predicted_change_pct=-0.04,
                     actual_price_t=100, actual_price_th=98),
        ]
        results = replay_under_thresholds(
            logs, buy_threshold=0.70, sell_threshold=0.30, min_change=0.03,
        )
        assert len(results) == 3
        assert results[0]["replayed_action"] == "buy"
        assert results[1]["replayed_action"] == "hold"  # change too small
        assert results[2]["replayed_action"] == "sell"

    def test_different_threshold_changes_action(self):
        """Lowering buy_threshold should turn a hold into a buy."""
        logs = [
            DummyLog(1, "hold", predicted_p_rise=0.60, predicted_change_pct=0.04,
                     actual_price_t=100, actual_price_th=110),
        ]
        # At default 0.70 threshold, this is a hold
        results = replay_under_thresholds(
            logs, buy_threshold=0.70, sell_threshold=0.30,
        )
        assert results[0]["replayed_action"] == "hold"

        # At 0.55 threshold, this becomes a buy
        results = replay_under_thresholds(
            logs, buy_threshold=0.55, sell_threshold=0.30,
        )
        assert results[0]["replayed_action"] == "buy"

    def test_no_price_data(self):
        logs = [DummyLog(1, "buy", predicted_p_rise=0.80,
                         predicted_change_pct=0.05)]
        results = replay_under_thresholds(logs, 0.70, 0.30)
        assert results[0]["roi_pct"] == 0.0


# ------------------------------------------------------------------ #
# grid_search_thresholds
# ------------------------------------------------------------------ #

class TestGridSearch:
    def test_finds_best(self):
        logs = [
            # Under default (0.70/0.30): this would be hold
            # Under lower (0.55/0.30): this would be buy, and it was correct
            DummyLog(1, "hold", predicted_p_rise=0.60, predicted_change_pct=0.05,
                     actual_price_t=100, actual_price_th=120),
        ]
        best = grid_search_thresholds(
            logs,
            buy_range=[0.55, 0.60, 0.65, 0.70],
            sell_range=[0.25, 0.30],
        )
        assert len(best) == 1
        # Lower threshold should be found optimal since it captures the good buy
        assert best[0]["buy_threshold"] <= 0.65
