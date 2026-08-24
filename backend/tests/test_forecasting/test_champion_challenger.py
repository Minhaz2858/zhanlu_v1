"""P2.16: Champion/challenger shadow runs + ops queue endpoints.

Champion/challenger: one candidate model per product is shadow-run nightly.
Its forecast is persisted as a shadow entry in model_detail (never published).
Realized MASE is tracked; promotion rule (>5% better over 4 consecutive weeks)
writes a recommendation row.

Ops endpoints:
  GET /forecast/ops/rebuild-queue  → targets with needs_rebuild flags
  GET /forecast/ops/threshold-configs?status=staged  → staged configs
  POST /forecast/ops/threshold-configs/{id}/promote  → activate + demote prior
"""
import pytest

from app.services.forecasting.ops.champion_challenger import (
    ChampionChallengerTracker,
    ShadowForecastResult,
)
from app.services.forecasting.ops.accuracy_feedback import needs_rebuild_products


class TestChampionChallenger:
    """Shadow runs and promotion logic."""

    def test_tracker_records_shadow(self):
        """Tracker must record shadow forecasts without affecting champion."""
        tracker = ChampionChallengerTracker()
        shadow = ShadowForecastResult(
            product_id="isoprene",
            challenger_model="ets_tuned",
            forecast_value=12500.0,
            mase_estimate=0.85,
        )
        tracker.record_shadow(shadow)
        assert len(tracker.shadows) == 1
        assert tracker.shadows[0].product_id == "isoprene"

    def test_promotion_requires_4_consecutive_weeks(self):
        """Promotion rule: >5% MASE improvement over 4 consecutive weeks."""
        tracker = ChampionChallengerTracker()
        # 3 weeks of improvement — not enough
        for week in range(3):
            tracker.record_weekly_result("isoprene", challenger_mase=0.80, champion_mase=0.90)
        assert not tracker.check_promotion("isoprene")

        # 4th week — now eligible
        tracker.record_weekly_result("isoprene", challenger_mase=0.80, champion_mase=0.90)
        rec = tracker.check_promotion("isoprene")
        assert rec is not None
        assert rec["product_id"] == "isoprene"
        assert rec["improvement_pct"] > 5.0

    def test_no_promotion_when_not_better(self):
        """Challenger that doesn't beat champion → no promotion."""
        tracker = ChampionChallengerTracker()
        for week in range(4):
            tracker.record_weekly_result("styrene", challenger_mase=1.05, champion_mase=1.00)
        assert not tracker.check_promotion("styrene")

    def test_streak_resets_on_miss(self):
        """If challenger fails one week, streak resets."""
        tracker = ChampionChallengerTracker()
        tracker.record_weekly_result("butadiene", challenger_mase=0.85, champion_mase=0.95)  # W1 ✓
        tracker.record_weekly_result("butadiene", challenger_mase=0.85, champion_mase=0.95)  # W2 ✓
        tracker.record_weekly_result("butadiene", challenger_mase=1.10, champion_mase=0.95)  # W3 ✗
        tracker.record_weekly_result("butadiene", challenger_mase=0.85, champion_mase=0.95)  # W4 ✓
        # Only 1 consecutive after reset — not enough
        assert not tracker.check_promotion("butadiene")


class TestOpsQueueEndpoints:
    """Rebuild queue and threshold-config promotion."""

    def test_needs_rebuild_products_exists(self):
        """needs_rebuild_products function must exist and be callable."""
        assert callable(needs_rebuild_products)

    def test_needs_rebuild_returns_list(self):
        """Without a DB session, should return empty list (not crash)."""
        result = needs_rebuild_products(session=None)
        assert isinstance(result, list)
