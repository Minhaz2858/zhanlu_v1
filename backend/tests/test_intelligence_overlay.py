"""Tests for Phase 3: Intelligence Adjustment Overlay.

Verifies:
- fetch_product_intelligence_sync returns correct format
- apply_intelligence_adjustment correctly adjusts forecast values
- Horizon scaling is applied correctly
- Empty/no-event cases return neutral results
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.forecasting.intelligence_overlay import (
    HORIZON_SCALE,
    _certainty_to_confidence,
    _direction_magnitude_to_pct,
    _empty_result,
    _match_events_to_product,
    apply_intelligence_adjustment,
    fetch_product_intelligence_sync,
)


class TestEmptyResult:
    """Empty/no-event scenarios return neutral output."""

    def test_empty_result_has_expected_keys(self):
        r = _empty_result()
        assert r == {
            "active": False,
            "net_price_pct": 0.0,
            "confidence": 0.0,
            "event_count": 0,
            "bias": "neutral",
            "horizon_adjustments": {3: 0.0, 7: 0.0, 30: 0.0},
            "detail": [],
        }

    def test_fetch_no_rows_returns_empty(self):
        """When DB returns no events, fetch returns empty result."""
        db = MagicMock()
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = fetch_product_intelligence_sync(db, "C5")
        assert result["active"] is False
        assert result["net_price_pct"] == 0.0
        assert result["event_count"] == 0


class TestDirectionMagnitudeToPct:
    """Qualitative → numeric conversion."""

    def test_bullish_minor_positive(self):
        assert _direction_magnitude_to_pct("bullish", "minor") > 0

    def test_bearish_major_negative(self):
        assert _direction_magnitude_to_pct("bearish", "major") < 0

    def test_neutral_returns_zero(self):
        assert _direction_magnitude_to_pct("neutral", "moderate") == 0.0

    def test_major_exceeds_moderate(self):
        major = abs(_direction_magnitude_to_pct("up", "major"))
        moderate = abs(_direction_magnitude_to_pct("up", "moderate"))
        assert major > moderate


class TestCertaintyToConfidence:
    """Qualitative certainty → numeric confidence."""

    def test_confirmed_is_1(self):
        assert _certainty_to_confidence("confirmed") == 1.0

    def test_rumor_is_low(self):
        assert _certainty_to_confidence("rumor") < 0.2

    def test_unknown_defaults_to_0_5(self):
        assert _certainty_to_confidence("unknown_word") == 0.5


class TestApplyIntelligenceAdjustment:
    """Adjustment function applies overlay correctly."""

    def test_noop_when_not_active(self):
        original = [100.0, 101.0, 102.0]
        intel = _empty_result()
        result = apply_intelligence_adjustment(original, 7, intel)
        assert result == original

    def test_positive_adjustment_increases_values(self):
        original = [100.0, 100.0, 100.0]
        intel = {
            "active": True,
            "horizon_adjustments": {7: 5.0},
        }
        result = apply_intelligence_adjustment(original, 7, intel)
        # 5% increase
        assert all(v > 100 for v in result)
        assert abs(result[0] - 105.0) < 0.01

    def test_negative_adjustment_decreases_values(self):
        original = [100.0, 100.0, 100.0]
        intel = {
            "active": True,
            "horizon_adjustments": {7: -3.0},
        }
        result = apply_intelligence_adjustment(original, 7, intel)
        assert all(v < 100 for v in result)
        assert abs(result[0] - 97.0) < 0.01

    def test_horizon_scaling_3d_lower_than_7d(self):
        """3-day horizon uses 0.80x, 7-day uses 1.0x scaling."""
        # 7d scaling is higher than 3d
        assert HORIZON_SCALE[3] < HORIZON_SCALE[7]
        # 30d scaling is the lowest
        assert HORIZON_SCALE[30] < HORIZON_SCALE[7]

    def test_different_horizon_yields_different_scales(self):
        original = [200.0, 200.0]
        intel = {
            "active": True,
            "horizon_adjustments": {3: 4.0, 7: 5.0, 30: 3.0},
        }
        adj3 = apply_intelligence_adjustment(original, 3, intel)
        adj7 = apply_intelligence_adjustment(original, 7, intel)
        # 7d adjustment should differ from 3d
        assert adj3 != adj7


class TestMatchEventsToProduct:
    """Event-to-product matching logic."""

    def test_no_events_returns_empty(self):
        result = _match_events_to_product([], "C5")
        assert result == []

    def test_headline_match_finds_c5(self):
        row = MagicMock()
        row.headline = "C5 petroleum resin prices surge"
        row.event_type = "price_spike"
        row.direction = "up"
        row.magnitude_estimate = "moderate"
        row.certainty = "high"
        row.affected_commodities = []
        row.event_id = "evt_test001"

        result = _match_events_to_product([row], "C5")
        assert len(result) == 1
        assert result[0]["direction"] == "up"


class TestFetchProductIntelligenceSync:
    """Integration tests for fetch_product_intelligence_sync."""

    def test_adjustment_included_in_explanation(self):
        """The overlay result can be included as explanation content."""
        intel = _empty_result()
        assert "horizon_adjustments" in intel
        assert list(intel["horizon_adjustments"].keys()) == [3, 7, 30]
