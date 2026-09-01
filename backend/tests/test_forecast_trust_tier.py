"""Tests for EDIA-ported forecast trust tiers."""
from __future__ import annotations

import pytest

from app.services.forecasting.forecast_trust_tier import (
    _HIGH_SKILL_PRODUCTS,
    _WEEKLY_CADENCE_PRODUCTS,
    compute_forecast_trust_tier,
)


def test_high_skill_products_includes_known_set():
    """Products with documented ARIMA skill vs naive baseline."""
    assert "isoprene" in _HIGH_SKILL_PRODUCTS
    assert "piperylene" in _HIGH_SKILL_PRODUCTS
    assert "c5_resin" in _HIGH_SKILL_PRODUCTS
    assert "dcpd" in _HIGH_SKILL_PRODUCTS


def test_weekly_cadence_products_includes_known_set():
    """Products that trade weekly — daily forecast is misleading."""
    assert "cracked_c9" in _WEEKLY_CADENCE_PRODUCTS
    assert "mixed_c9" in _WEEKLY_CADENCE_PRODUCTS
    assert "trimethylbenzene" in _WEEKLY_CADENCE_PRODUCTS
    assert "raffinate_c5" in _WEEKLY_CADENCE_PRODUCTS


def test_high_tier_for_skilled_product_beating_naive():
    """High-skill product that beats naive → tier='high', green badge."""
    result = compute_forecast_trust_tier(
        product_id="isoprene",
        below_naive=False,
        cadence_class="daily",
        mape=8.0,
    )
    assert result["tier"] == "high"
    assert result["badge_color"] == "green"
    assert result["badge_label_en"] == "High Confidence"
    assert "8.0" in result["reason_en"]


def test_directional_tier_when_model_worse_than_naive():
    """below_naive=True → tier='directional', orange badge."""
    result = compute_forecast_trust_tier(
        product_id="some_product",
        below_naive=True,
        cadence_class="daily",
        mape=25.0,
    )
    assert result["tier"] == "directional"
    assert result["badge_color"] == "orange"
    assert result["badge_label_en"] == "Directional Only"


def test_directional_tier_for_weekly_cadence_product():
    """Weekly-cadence product with below_naive → directional + weekly reason."""
    result = compute_forecast_trust_tier(
        product_id="cracked_c9",
        below_naive=True,
        cadence_class="weekly",
        mape=30.0,
    )
    assert result["tier"] == "directional"
    assert "weekly" in result["reason_en"].lower()


def test_medium_tier_for_weekly_product_with_skill():
    """Weekly product NOT in high-skill set and NOT below_naive → medium."""
    result = compute_forecast_trust_tier(
        product_id="cracked_c9",
        below_naive=False,
        cadence_class="weekly",
        mape=15.0,
    )
    assert result["tier"] == "medium"
    assert result["badge_color"] == "yellow"
    assert result["badge_label_en"] == "Weekly Reference"


def test_low_tier_for_sparse_data():
    """Sparse/unknown cadence → tier='low', red badge."""
    result = compute_forecast_trust_tier(
        product_id="unknown",
        below_naive=False,
        cadence_class="sparse",
        mape=None,
    )
    assert result["tier"] == "low"
    assert result["badge_color"] == "red"
    assert result["badge_label_en"] == "Insufficient Data"


def test_low_tier_for_unknown_cadence():
    """cadence_class='unknown' → tier='low'."""
    result = compute_forecast_trust_tier(
        product_id="whatever",
        below_naive=False,
        cadence_class="unknown",
        mape=None,
    )
    assert result["tier"] == "low"


def test_default_medium_tier_for_daily_product():
    """Daily product with some skill, not high-skill, not below_naive → medium."""
    result = compute_forecast_trust_tier(
        product_id="some_daily_product",
        below_naive=False,
        cadence_class="daily",
        mape=12.0,
    )
    assert result["tier"] == "medium"
    assert result["badge_color"] == "yellow"


def test_return_dict_has_all_required_keys():
    """The returned dict must have all keys the frontend expects."""
    result = compute_forecast_trust_tier("isoprene", below_naive=False, cadence_class="daily", mape=8.0)
    for key in ("tier", "below_naive", "cadence_class", "mape",
                "reason_zh", "reason_en", "badge_color",
                "badge_label_zh", "badge_label_en"):
        assert key in result, f"missing key: {key}"


def test_tier_values_are_constrained():
    """tier must be one of the 4 valid values."""
    for pid, bn, cad in [
        ("isoprene", False, "daily"),
        ("cracked_c9", True, "weekly"),
        ("unknown", False, "sparse"),
        ("random", False, "daily"),
    ]:
        result = compute_forecast_trust_tier(pid, below_naive=bn, cadence_class=cad, mape=10.0)
        assert result["tier"] in ("high", "medium", "directional", "low"), (
            f"{pid}: invalid tier {result['tier']}"
        )
