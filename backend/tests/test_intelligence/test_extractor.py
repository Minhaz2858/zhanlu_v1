"""Tests for event extractor — keyword fallback mode (no LLM required)."""
import pytest
from app.services.intelligence.event_extractor import (
    EventExtractor,
    _keyword_event_type,
    _normalize_event_payload,
)
from app.services.intelligence.event_types import ExtractedEvent


def test_keyword_event_type_opec():
    assert _keyword_event_type("OPEC announces production cut", "general") == "opec_decision"


def test_keyword_event_type_shipping():
    assert _keyword_event_type("Red Sea shipping disruption", "general") == "shipping_disruption"


def test_keyword_event_type_plant_outage():
    assert _keyword_event_type("Plant shutdown for maintenance", "general") == "plant_outage"


def test_keyword_event_type_tariff():
    assert _keyword_event_type("New tariff on chemicals", "general") == "tariff_policy"


def test_keyword_event_type_weather():
    assert _keyword_event_type("Typhoon hits Guangdong", "general") == "weather_event"


def test_keyword_event_type_fallback():
    assert _keyword_event_type("Demand increases for resins", "general") == "demand_signal"


def test_normalize_event_payload_defaults():
    raw = {"headline": "Test event"}
    result = _normalize_event_payload(raw, "article text", "general")
    assert result["event_type"] == "demand_signal"  # fallback
    assert result["direction"] == "uncertain"
    assert result["magnitude_estimate"] == "medium"
    assert result["certainty"] == "likely"
    assert result["affected_commodities"] == []


def test_normalize_event_payload_validates_enums():
    raw = {
        "event_type": "invalid_type",
        "direction": "invalid_dir",
        "magnitude_estimate": "huge",
        "certainty": "definitely",
        "time_horizon": "long",
        "geographic_scope": "mars",
        "relevance_to_c5_c9": "critical",
    }
    result = _normalize_event_payload(raw, "OPEC cuts production", "general")
    assert result["event_type"] == "opec_decision"  # keyword fallback
    assert result["direction"] == "uncertain"
    assert result["magnitude_estimate"] == "medium"


@pytest.mark.asyncio
async def test_extractor_keyword_fallback_no_llm():
    """Without an LLM caller, extractor uses keyword-based fallback."""
    extractor = EventExtractor(llm_caller=None)
    event = await extractor.extract(
        "OPEC announces surprise production cut of 1.5 million barrels per day.",
        category="crude_oil",
        source_url="https://example.com/news",
    )
    assert event is not None
    assert event.event_type == "opec_decision"
    assert event.source_url == "https://example.com/news"


@pytest.mark.asyncio
async def test_extractor_returns_none_for_short_text():
    extractor = EventExtractor(llm_caller=None)
    event = await extractor.extract("Short", category="general")
    assert event is None


@pytest.mark.asyncio
async def test_extractor_keyword_fallback_detects_bearish():
    extractor = EventExtractor(llm_caller=None)
    event = await extractor.extract(
        "SINOPEC announces price cut of 300 CNY per ton for C5 listing.",
        category="sinopec_cnpc",
    )
    assert event is not None
    assert event.event_type == "sinopec_price_change"
    assert event.direction == "bearish"
