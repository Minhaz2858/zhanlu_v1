"""Second-Order Effects — non-linear impacts that pure elasticity math misses.

Maps event types to secondary effects: shipping premiums, substitution patterns,
sentiment-driven behaviors, and market structure adjustments.

Ported from EDIA 5.1.2 — pure domain knowledge, no external dependencies.
"""

from __future__ import annotations

# ── Effect profiles keyed by event type ───────────────────────────────────

SECOND_ORDER_EFFECTS: dict[str, dict] = {
    "geopolitical_supply_disruption": {
        "naphtha_shipping_premium": 0.015,  # +1.5% extra naphtha cost from route disruption
        "china_domestic_supply_premium": 0.008,  # Chinese domestic producers gain pricing power
        "sinopec_listing_acceleration": True,  # SINOPEC may raise listings before month-end
        "inventory_hoarding_behavior": 0.005,  # Buyers build inventory in fear
        "total_extra_pct": 0.028,
    },
    "shipping_disruption": {
        "naphtha_shipping_premium": 0.020,  # +2.0% shipping cost
        "reroute_delay_premium": 0.010,  # Longer delivery times = spot tightness
        "insurance_cost_passthrough": 0.005,
        "total_extra_pct": 0.035,
    },
    "tariff_policy": {
        "import_substitution_effect": {
            "road_marking_resin": -0.020,
            "adhesive_resin": -0.025,
            "sis_rubber": -0.030,
        },
        "export_demand_destruction": -0.015,
        "rmb_depreciation_pressure": 0.008,
        "total_extra_pct": -0.010,  # Net effect is mildly bearish for exporters
    },
    "demand_signal": {
        "downstream_demand_pull": {
            "sis_rubber": 0.012,
            "c5_resin": 0.020,
            "c9_resin": 0.015,
        },
        "inventory_drawdown_acceleration": 0.008,
        "total_extra_pct": 0.015,
    },
    "plant_outage": {
        "spot_price_premium": 0.030,
        "contract_price_lag": 0.015,
        "total_extra_pct": 0.045,
    },
    "sinopec_price_change": {
        "market_follow_probability": 0.92,
        "ecisco_discount_compression_cny": -50,
        "total_extra_pct": 0.0,  # Direct effect already captured; secondary is discount change
    },
    "opec_decision": {
        "sustained_crude_support": 0.010,
        "forward_curve_backwardation": 0.005,
        "total_extra_pct": 0.015,
    },
    "weather_event": {
        "spot_tightness_premium": 0.020,
        "logistics_disruption": 0.010,
        "total_extra_pct": 0.030,
    },
    "currency_move": {
        "import_cost_uplift": 0.012,
        "export_competitiveness_change": -0.008,
        "total_extra_pct": 0.004,
    },
    "sanctions": {
        "uncertainty_premium": 0.015,
        "supply_reroute_cost": 0.010,
        "total_extra_pct": 0.025,
    },
}


def get_second_order_effects(event_type: str) -> dict:
    """Retrieve second-order effects for an event type."""
    return SECOND_ORDER_EFFECTS.get(event_type, {"total_extra_pct": 0.0})


def compute_total_second_order_pct(event_type: str) -> float:
    """Return the aggregate extra % adjustment from second-order effects."""
    effects = SECOND_ORDER_EFFECTS.get(event_type, {})
    return effects.get("total_extra_pct", 0.0)


def get_product_specific_effects(event_type: str) -> dict[str, float]:
    """Return product-specific second-order adjustments (if any)."""
    effects = SECOND_ORDER_EFFECTS.get(event_type, {})
    product_effects: dict[str, float] = {}

    for key, val in effects.items():
        if isinstance(val, dict):
            product_effects.update(val)

    return product_effects
