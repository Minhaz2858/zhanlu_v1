"""Tests for Phase 5: Intelligence Event auto-approval logic.

Validates that IntelligenceEventRegistry._compute_auto_approval correctly
promotes events meeting quality thresholds to "auto_approved", preserves
already-decided statuses, and handles edge cases.

Note: ExtractedEvent derives `impact_magnitude` and `relevance_score`
from string fields (magnitude_estimate / certainty / relevance_to_c5_c9),
so we craft input combinations that produce the desired score ranges:

  To get impact_magnitude >= 0.7 AND relevance_score >= 0.7:
    magnitude_estimate = "large" or "extreme"
    certainty = "confirmed" or "likely"
    relevance_to_c5_c9 = "direct"
"""
from __future__ import annotations

import pytest

from app.services.intelligence.event_registry import IntelligenceEventRegistry
from app.services.intelligence.event_types import ExtractedEvent


def _make_event(
    *,
    magnitude: str = "large",
    certainty: str = "confirmed",
    relevance_to_c5_c9: str = "direct",
    credibility: str = "tier1",
    review_status: str = "approved",
    event_id: str = "ev-test-001",
) -> ExtractedEvent:
    """Build an ExtractedEvent with the desired derived-score profile."""
    return ExtractedEvent(
        event_id=event_id,
        event_type="geopolitical_supply_disruption",
        headline="Test event",
        affected_commodities=["C5"],
        direction="bullish",
        magnitude_estimate=magnitude,
        certainty=certainty,
        time_horizon="short",
        geographic_scope="global",
        relevance_to_c5_c9=relevance_to_c5_c9,
        key_entities=[],
        key_information="test info",
        causal_chain_hint="",
        source_url="",
        source_credibility=credibility,
        full_text="",
        raw_quote="",
        review_status=review_status,
    )


class TestComputeAutoApproval:
    """Tests for the _compute_auto_approval static method."""

    def test_high_impact_high_relevance_tier1_approves(self):
        ev = _make_event(
            magnitude="large", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="tier1",
        )
        assert ev.impact_magnitude >= 0.7
        assert ev.relevance_score >= 0.7
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "auto_approved"

    def test_high_impact_high_relevance_tier2_approves(self):
        ev = _make_event(
            magnitude="extreme", certainty="likely",
            relevance_to_c5_c9="direct", credibility="tier2",
        )
        assert ev.impact_magnitude >= 0.7
        assert ev.relevance_score >= 0.7
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "auto_approved"

    def test_low_magnitude_does_not_approve(self):
        ev = _make_event(
            magnitude="small", certainty="speculative",
            relevance_to_c5_c9="minimal", credibility="tier1",
        )
        assert ev.impact_magnitude < 0.7
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "approved"

    def test_low_credibility_does_not_approve(self):
        """tier3 events are never auto-approved even with perfect scores."""
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="tier3",
        )
        assert ev.impact_magnitude >= 0.7
        assert ev.relevance_score >= 0.7
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "approved"

    def test_unknown_credibility_treated_as_tier2(self):
        """Empty credibility defaults to 'tier2' — still qualifies."""
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "auto_approved"

    def test_rejected_event_is_never_auto_approved(self):
        """Hard rule: once rejected, stay rejected even if impact is high."""
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="tier1",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "rejected")
        assert result == "rejected"

    def test_already_auto_approved_stays_auto_approved(self):
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="tier1",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "auto_approved")
        assert result == "auto_approved"

    def test_pending_event_gets_auto_approved_if_qualifies(self):
        ev = _make_event(
            magnitude="large", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="tier1",
            review_status="pending",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "pending")
        assert result == "auto_approved"

    def test_pending_event_stays_pending_if_not_high_quality(self):
        ev = _make_event(
            magnitude="small", certainty="speculative",
            relevance_to_c5_c9="minimal", credibility="tier1",
            review_status="pending",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "pending")
        assert result == "pending"

    def test_credibility_case_insensitive(self):
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="TIER1",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "auto_approved"

    def test_credibility_whitespace_tolerated(self):
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct", credibility="  tier2  ",
        )
        result = IntelligenceEventRegistry._compute_auto_approval(ev, "approved")
        assert result == "auto_approved"


class TestExtractedEventDerivedScores:
    """Sanity checks that derived scores land where the auto-approval expects."""

    def test_large_confirmed_direct_score(self):
        ev = _make_event(
            magnitude="large", certainty="confirmed",
            relevance_to_c5_c9="direct",
        )
        # 0.70*0.40 + 0.95*0.35 + 0.90*0.25 = 0.28+0.3325+0.225 = 0.8375
        assert 0.83 <= ev.impact_magnitude <= 0.85
        assert ev.relevance_score == 0.90

    def test_extreme_confirmed_direct_score(self):
        ev = _make_event(
            magnitude="extreme", certainty="confirmed",
            relevance_to_c5_c9="direct",
        )
        # 0.95*0.40 + 0.95*0.35 + 0.90*0.25 = 0.38+0.3325+0.225 = 0.9375
        assert 0.93 <= ev.impact_magnitude <= 0.94
        assert ev.relevance_score == 0.90

    def test_small_speculative_minimal_score(self):
        ev = _make_event(
            magnitude="small", certainty="speculative",
            relevance_to_c5_c9="minimal",
        )
        # 0.15*0.40 + 0.25*0.35 + 0.20*0.25 = 0.06+0.0875+0.05 = 0.1975
        assert 0.19 <= ev.impact_magnitude <= 0.20
        assert ev.relevance_score == 0.20

    def test_indirect_relevance_score(self):
        ev = _make_event(relevance_to_c5_c9="indirect")
        assert ev.relevance_score == 0.60