"""Tests for intelligence SQLAlchemy models."""
import pytest
from datetime import datetime

from app.models.intelligence import IntelligenceEvent, IntelligenceIngestionStatus
from app.models.base import TimestampedBase


def test_intelligence_event_model_fields():
    """IntelligenceEvent stores all required fields correctly."""
    event = IntelligenceEvent(
        event_id="evt_test123",
        event_type="plant_outage",
        headline="Test event",
        affected_commodities=["mixed_c5"],
        direction="bullish",
        magnitude_estimate="large",
        certainty="confirmed",
        impact_magnitude=0.85,
        review_status="approved",
        detected_at=datetime.utcnow().isoformat(),
    )
    assert event.event_id == "evt_test123"
    assert event.event_type == "plant_outage"
    assert event.headline == "Test event"
    assert event.affected_commodities == ["mixed_c5"]
    assert event.direction == "bullish"
    assert event.impact_magnitude == 0.85
    assert event.review_status == "approved"


def test_intelligence_event_inherits_timestamped_base():
    """IntelligenceEvent is a TimestampedBase subclass."""
    assert issubclass(IntelligenceEvent, TimestampedBase)
    assert IntelligenceEvent.__tablename__ == "intelligence_events"

    event = IntelligenceEvent(
        event_id="evt_test456",
        event_type="opec_decision",
        headline="OPEC cuts",
        detected_at=datetime.utcnow().isoformat(),
    )
    # TimestampedBase provides these columns
    assert hasattr(event, "org_id")
    assert hasattr(event, "app_id")
    assert hasattr(event, "is_deleted")
    assert hasattr(event, "id")


def test_intelligence_event_to_dict():
    """to_dict() from TimestampedBase works on IntelligenceEvent."""
    event = IntelligenceEvent(
        event_id="evt_test789",
        event_type="shipping_disruption",
        headline="Red Sea disruption",
        detected_at=datetime.utcnow().isoformat(),
        impact_magnitude=0.72,
    )
    d = event.to_dict()
    assert d["event_id"] == "evt_test789"
    assert d["event_type"] == "shipping_disruption"
    assert d["impact_magnitude"] == 0.72
    assert "is_deleted" not in d  # internal field excluded


def test_ingestion_status_model():
    """IntelligenceIngestionStatus has all required fields."""
    status = IntelligenceIngestionStatus(
        agent_name="news_ingestion_agent",
        running=False,
        cycle_in_progress=False,
    )
    assert status.agent_name == "news_ingestion_agent"
    assert status.running is False
    assert status.cycle_in_progress is False


def test_ingestion_status_inherits_timestamped_base():
    """IngestionStatus is a TimestampedBase subclass."""
    assert issubclass(IntelligenceIngestionStatus, TimestampedBase)
    assert IntelligenceIngestionStatus.__tablename__ == "intelligence_ingestion_status"

    status = IntelligenceIngestionStatus(agent_name="news_ingestion_agent")
    assert hasattr(status, "org_id")
    assert hasattr(status, "app_id")
    assert hasattr(status, "is_deleted")
