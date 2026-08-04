"""Tests for event deduplicator."""
from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.event_deduplicator import EventDeduplicator


def _make_event(event_type="plant_outage", headline="Test", direction="bullish"):
    return ExtractedEvent(
        event_type=event_type,
        headline=headline,
        affected_commodities=["mixed_c5"],
        direction=direction,
        magnitude_estimate="medium",
        certainty="likely",
        time_horizon="short",
        geographic_scope="china",
        relevance_to_c5_c9="direct",
    )


def test_deduplicator_detects_exact_duplicate():
    dedup = EventDeduplicator()
    ev = _make_event()
    assert not dedup.is_duplicate(ev)
    dedup.mark_seen(ev)
    assert dedup.is_duplicate(ev)


def test_deduplicator_allows_different_events():
    dedup = EventDeduplicator()
    ev1 = _make_event(event_type="plant_outage", direction="bullish")
    ev2 = _make_event(event_type="opec_decision", direction="bearish")
    dedup.mark_seen(ev1)
    assert not dedup.is_duplicate(ev2)


def test_deduplicator_batch():
    dedup = EventDeduplicator()
    ev1 = _make_event(headline="First event")
    ev2 = _make_event(headline="Second event", event_type="opec_decision")
    ev3 = _make_event(headline="First event")  # same fingerprint as ev1
    result = dedup.deduplicate([ev1, ev2, ev3])
    assert len(result) == 2


def test_deduplicator_cache_size():
    dedup = EventDeduplicator()
    assert dedup.cache_size == 0
    dedup.mark_seen(_make_event())
    assert dedup.cache_size == 1
