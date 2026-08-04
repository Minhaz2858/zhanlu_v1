"""Tests for ExtractedEvent dataclass — ported from EDIA intelligence layer."""
import pytest

from app.services.intelligence.event_types import ExtractedEvent


def test_extracted_event_creation():
    """Event auto-generates event_id and detected_at."""
    event = ExtractedEvent(
        event_type="plant_outage",
        headline="Major plant shutdown in East China",
        affected_commodities=["mixed_c5", "isoprene"],
        direction="bullish",
        magnitude_estimate="large",
        certainty="confirmed",
        time_horizon="short",
        geographic_scope="china",
        relevance_to_c5_c9="direct",
    )
    assert event.event_id.startswith("evt_")
    assert len(event.event_id) == 16  # "evt_" + 12 hex chars
    assert event.detected_at  # auto-generated ISO timestamp
    assert event.impact_magnitude > 0


def test_extracted_event_impact_magnitude():
    """Impact magnitude = mag*0.40 + cert*0.35 + rel*0.25."""
    event = ExtractedEvent(
        event_type="opec_decision",
        headline="OPEC cuts production",
        affected_commodities=["crude_oil"],
        direction="bullish",
        magnitude_estimate="extreme",
        certainty="confirmed",
        time_horizon="medium",
        geographic_scope="global",
        relevance_to_c5_c9="direct",
    )
    # extreme=0.95, confirmed=0.95, direct=0.90
    # 0.95*0.40 + 0.95*0.35 + 0.90*0.25 ≈ 0.9375 → round(,3) = 0.937 or 0.938
    assert event.impact_magnitude >= 0.937
    assert event.impact_magnitude <= 0.938


def test_extracted_event_low_impact():
    """Speculative + minimal relevance + small magnitude → low impact."""
    event = ExtractedEvent(
        event_type="demand_signal",
        headline="Minor demand fluctuation",
        affected_commodities=["c5_resin"],
        direction="neutral",
        magnitude_estimate="small",
        certainty="speculative",
        time_horizon="medium",
        geographic_scope="global",
        relevance_to_c5_c9="minimal",
    )
    # small=0.15, speculative=0.25, minimal=0.20
    # 0.15*0.40 + 0.25*0.35 + 0.20*0.25 ≈ 0.1975
    assert event.impact_magnitude >= 0.197
    assert event.impact_magnitude <= 0.198
    assert event.impact_magnitude < 0.3


def test_extracted_event_to_dict():
    """to_dict() includes derived scores."""
    event = ExtractedEvent(
        event_type="shipping_disruption",
        headline="Red Sea shipping disruption",
        affected_commodities=["crude_oil", "naphtha"],
        direction="bullish",
        magnitude_estimate="medium",
        certainty="likely",
        time_horizon="short",
        geographic_scope="middle_east",
        relevance_to_c5_c9="indirect",
    )
    d = event.to_dict()
    assert d["event_type"] == "shipping_disruption"
    assert d["headline"] == "Red Sea shipping disruption"
    assert "impact_magnitude" in d
    assert "relevance_score" in d
    assert d["affected_commodities"] == ["crude_oil", "naphtha"]


def test_extracted_event_str():
    """__str__ includes type, headline, impact, direction."""
    event = ExtractedEvent(
        event_type="tariff_policy",
        headline="US imposes tariffs",
        affected_commodities=["mixed_c5"],
        direction="bearish",
        magnitude_estimate="medium",
        certainty="likely",
        time_horizon="medium",
        geographic_scope="global",
        relevance_to_c5_c9="direct",
    )
    s = str(event)
    assert "tariff_policy" in s
    assert "US imposes tariffs" in s
    assert "bearish" in s
