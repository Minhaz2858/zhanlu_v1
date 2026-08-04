"""Tests for intelligence orchestrator and news ingestion agent."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.intelligence_orchestrator import IntelligenceOrchestrator
from app.services.intelligence.news_ingestion_agent import NewsIngestionAgent


def _make_event(event_type="plant_outage", impact=0.7):
    return ExtractedEvent(
        event_type=event_type,
        headline=f"Test {event_type}",
        affected_commodities=["mixed_c5"],
        direction="bullish",
        magnitude_estimate="large",
        certainty="confirmed",
        time_horizon="short",
        geographic_scope="china",
        relevance_to_c5_c9="direct",
        source_credibility="tier1",
    )


# ── Intelligence Orchestrator ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_empty_context_when_no_events():
    """When registry has no events, orchestrator returns empty context."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

    orchestrator = IntelligenceOrchestrator(db, org_id="test-org", llm_caller=None)
    context = await orchestrator.get_intelligence_context()

    assert context["recent_events"] == []
    assert context["causal_analyses"] == []
    assert "No intelligence events" in context["summary"]


@pytest.mark.asyncio
async def test_orchestrator_get_market_events_returns_list():
    """get_market_events returns a list of event dicts."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(all=MagicMock(return_value=[]))))

    orchestrator = IntelligenceOrchestrator(db, org_id="test-org", llm_caller=None)
    events = await orchestrator.get_market_events(hours=24)
    assert isinstance(events, list)


@pytest.mark.asyncio
async def test_orchestrator_ingest_manual_event():
    """ingest_manual_event stores and processes the event."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    ))
    db.flush = AsyncMock()
    db.add = MagicMock()

    orchestrator = IntelligenceOrchestrator(db, org_id="test-org", llm_caller=None)
    event = _make_event()
    result = await orchestrator.ingest_manual_event(event)

    assert "event" in result
    assert "causal_analysis" in result
    assert "scenarios" in result
    assert "override" in result
    assert result["event"]["event_type"] == "plant_outage"


# ── News Ingestion Agent ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_news_agent_initialization():
    """NewsIngestionAgent initializes with all required services."""
    db = AsyncMock()
    agent = NewsIngestionAgent(db, org_id="test-org", llm_caller=None)
    assert agent._agent_name == "news_ingestion_agent"
    assert agent._org_id == "test-org"


@pytest.mark.asyncio
async def test_news_agent_run_cycle_with_no_articles():
    """When no articles are fetched, cycle completes with zero counts."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    ))
    db.flush = AsyncMock()
    db.add = MagicMock()

    agent = NewsIngestionAgent(db, org_id="test-org", llm_caller=None)

    # Mock RSS and web search to return empty
    agent._rss = MagicMock()
    agent._rss.fetch_category = MagicMock(return_value=[])
    agent._web_search = MagicMock()
    agent._web_search._is_disabled = MagicMock(return_value=True)

    result = await agent.run_news_cycle(trigger="test")

    assert result["success"] is True
    assert result["articles_seen"] == 0
    assert result["events_extracted"] == 0
    assert result["events_stored"] == 0


@pytest.mark.asyncio
async def test_news_agent_fetch_rss_articles():
    """_fetch_rss_articles returns structured articles from RSS entries."""
    db = AsyncMock()
    agent = NewsIngestionAgent(db, org_id="test-org", llm_caller=None)

    # Mock RSS to return one entry
    mock_entry = MagicMock()
    mock_entry.title = "OPEC cuts production"
    mock_entry.url = "https://example.com/news/1"
    mock_entry.snippet = "OPEC announces surprise production cut"
    mock_entry.source = "Reuters"
    mock_entry.published_date = "2026-07-31"

    agent._rss = MagicMock()
    agent._rss.fetch_category = MagicMock(return_value=[mock_entry])

    articles = agent._fetch_rss_articles()
    assert len(articles) > 0
    assert articles[0]["text"] == "OPEC cuts production. OPEC announces surprise production cut"
    assert articles[0]["url"] == "https://example.com/news/1"
    assert articles[0]["category"] == "crude_oil"
