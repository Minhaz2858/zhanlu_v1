"""Product Intelligence — summarize intelligence signals per product (Phase 5D).

Port of EDIA 5.1.2 backend/src/intelligence/product_intelligence.py.
Extracts product-specific impacts from intelligence context dicts.
"""

from __future__ import annotations

from typing import Any

_PRODUCT_ALIASES: dict[str, set[str]] = {
    "cracked_c5": {"cracked_c5", "mixed_c5"},
    "mixed_c5": {"cracked_c5", "mixed_c5"},
    "cracked_c9": {"cracked_c9", "mixed_c9"},
    "mixed_c9": {"cracked_c9", "mixed_c9"},
    "sis": {"sis", "sis_rubber"},
    "sis_rubber": {"sis", "sis_rubber"},
    "c9_resin": {"c9_resin", "hydrogenated_c9"},
    "hydrogenated_c9": {"c9_resin", "hydrogenated_c9"},
}


def summarize_product_intelligence(
    product_id: str,
    intelligence_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract intelligence signals relevant to a specific product.

    Args:
        product_id: Target product ID
        intelligence_context: Dict with causal_analyses list from intelligence pipeline

    Returns:
        {active, bias, event_count, net_price_pct, net_price_cny, dominant_event, events}
    """
    if not intelligence_context or not intelligence_context.get("has_intelligence"):
        return {
            "active": False, "event_count": 0, "bias": "neutral",
            "confidence": 0.0, "net_price_pct": 0.0, "net_price_cny": 0.0,
            "dominant_event": None, "events": [],
        }

    aliases = _PRODUCT_ALIASES.get(product_id, {product_id})
    matched_events: list[dict] = []

    for analysis in intelligence_context.get("causal_analyses", []):
        impacts = analysis.get("product_impacts", {})
        matched_impact = None
        matched_alias = None
        for alias in aliases:
            if alias in impacts:
                matched_alias = alias
                matched_impact = impacts[alias]
                break
        if not matched_impact:
            continue

        confidence_by_horizon = analysis.get("confidence_by_horizon", {})
        confidence = float(confidence_by_horizon.get("7d", 0.6) or 0.6)

        matched_events.append({
            "event_id": analysis.get("event_id"),
            "headline": analysis.get("event_headline"),
            "event_type": analysis.get("event_type"),
            "entry_commodity": analysis.get("entry_commodity"),
            "naphtha_total_pct": float(analysis.get("naphtha_total_pct", 0.0) or 0.0),
            "product_alias": matched_alias,
            "price_pct": matched_impact.get("price_pct", 0.0),
            "price_cny": matched_impact.get("price_cny", 0.0),
            "confidence": confidence,
            "direction": matched_impact.get("direction", "neutral"),
            "reason": matched_impact.get("reason", ""),
        })

    if not matched_events:
        return {
            "active": False, "event_count": 0, "bias": "neutral",
            "confidence": 0.0, "net_price_pct": 0.0, "net_price_cny": 0.0,
            "dominant_event": None, "events": [],
        }

    # Aggregate signals
    weighted_pct = sum(e["price_pct"] * e["confidence"] for e in matched_events)
    total_conf = sum(e["confidence"] for e in matched_events)
    net_pct = round(weighted_pct / total_conf, 2) if total_conf > 0 else 0.0

    weighted_cny = sum(e["price_cny"] * e["confidence"] for e in matched_events)
    net_cny = round(weighted_cny / total_conf, 2) if total_conf > 0 else 0.0

    bias = "bullish" if net_pct > 0 else ("bearish" if net_pct < 0 else "neutral")

    # Dominant event = highest absolute impact
    dominant = max(matched_events, key=lambda e: abs(e["price_cny"]))

    return {
        "active": True,
        "event_count": len(matched_events),
        "bias": bias,
        "confidence": round(total_conf / len(matched_events), 3) if matched_events else 0.0,
        "net_price_pct": net_pct,
        "net_price_cny": net_cny,
        "dominant_event": {
            "headline": dominant["headline"], "event_type": dominant["event_type"],
            "price_cny": dominant["price_cny"],
        },
        "events": matched_events,
    }
