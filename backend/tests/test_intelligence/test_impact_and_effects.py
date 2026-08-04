"""Tests for event impact DB and second-order effects."""
from app.services.intelligence.event_impact_db import EventImpactDB, event_impact_db
from app.services.intelligence.second_order_effects import (
    get_second_order_effects,
    compute_total_second_order_pct,
    get_product_specific_effects,
)


def test_impact_db_find_similar_events():
    db = EventImpactDB()
    result = db.find_similar_events("plant_outage", k=3)
    assert "similar_events" in result
    assert len(result["similar_events"]) <= 3
    assert "weighted_historical_impact" in result
    assert "confidence_from_precedents" in result


def test_impact_db_find_shipping_events():
    db = EventImpactDB()
    result = db.find_similar_events("shipping_disruption", k=3)
    # Should find the Red Sea Houthi event at minimum
    types = [e["event_type"] for e in result["similar_events"]]
    assert "shipping_disruption" in types or "geopolitical_supply_disruption" in types


def test_impact_db_add_observed_outcome():
    db = EventImpactDB()
    initial_count = len(db.get_all_events())
    db.add_observed_outcome({
        "event_id": "test_outcome",
        "event_type": "plant_outage",
        "date": "2025-06-01",
        "c5_impact_cny": 100,
        "duration_days": 14,
    })
    assert len(db.get_all_events()) == initial_count + 1


def test_second_order_effects_geopolitical():
    effects = get_second_order_effects("geopolitical_supply_disruption")
    assert "naphtha_shipping_premium" in effects
    assert effects["total_extra_pct"] == 0.028


def test_compute_total_second_order_pct():
    assert compute_total_second_order_pct("shipping_disruption") == 0.035
    assert compute_total_second_order_pct("plant_outage") == 0.045
    assert compute_total_second_order_pct("unknown_type") == 0.0


def test_product_specific_effects_tariff():
    effects = get_product_specific_effects("tariff_policy")
    assert "road_marking_resin" in effects
    assert effects["road_marking_resin"] == -0.020
    assert effects["sis_rubber"] == -0.030


def test_product_specific_effects_no_products():
    effects = get_product_specific_effects("opec_decision")
    assert effects == {}  # opec_decision has no product-specific sub-dicts
