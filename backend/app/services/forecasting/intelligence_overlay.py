"""Synchronous intelligence overlay for the forecast engine.

The forecast engine runs in a background thread (sync SQLAlchemy). This
module provides a sync-path fetch of recent intelligence events and
computes per-product adjustment factors without crossing the async bridge.

Used by ForecastEngine.compute_target() after the ML ensemble step.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Horizon scaling: closer horizons get a larger share of the event impact.
# 3-day horizon: 80% of net impact (rapid repricing)
# 7-day horizon: 100% of net impact (full reflection)
# 30-day horizon: 60% of net impact (attenuated — other factors dominate)
HORIZON_SCALE = {3: 0.80, 7: 1.00, 30: 0.60}

# Lookback window for intelligence events
EVENT_LOOKBACK_HOURS = 48


def fetch_product_intelligence_sync(
    db: Any,
    product_key: str,
    org_id: str = "default-org",
) -> dict[str, Any]:
    """Fetch recent intelligence events and compute a per-product adjustment.

    Returns a dict with:
        active: bool — whether any events affect this product
        net_price_pct: float — weighted-up net percentage impact
        confidence: float — average confidence across matched events
        event_count: int — number of matched events
        bias: str — "bullish", "bearish", or "neutral"
        horizon_adjustments: dict[int, float] — per-horizon pct adjustment
        detail: list[dict] — per-event breakdown (headline, type, impact)
    """
    from app.models.intelligence import IntelligenceEvent

    cutoff = datetime.now(timezone.utc) - timedelta(hours=EVENT_LOOKBACK_HOURS)

    rows = (
        db.query(IntelligenceEvent)
        .filter(
            IntelligenceEvent.org_id == org_id,
            IntelligenceEvent.review_status.in_(["approved", "auto_approved"]),
            IntelligenceEvent.created_date >= cutoff,
        )
        .order_by(IntelligenceEvent.created_date.desc())
        .all()
    )

    if not rows:
        return _empty_result()

    # Match events to product via affected_commodities / headline extraction
    matched = _match_events_to_product(rows, product_key)

    if not matched:
        return _empty_result()

    # Aggregate
    weighted_pct = 0.0
    total_conf = 0.0
    details = []

    for m in matched:
        direction = m.get("direction", "neutral")
        magnitude = m.get("magnitude_estimate", "minor")
        certainty = m.get("certainty", "medium")

        # Convert qualitative to numeric
        impact_pct = _direction_magnitude_to_pct(direction, magnitude)
        conf = _certainty_to_confidence(certainty)

        weighted_pct += impact_pct * conf
        total_conf += conf
        details.append({
            "headline": m.get("headline", ""),
            "event_type": m.get("event_type", "unknown"),
            "impact_pct": impact_pct,
            "confidence": conf,
            "direction": direction,
        })

    net_pct = round(weighted_pct / total_conf, 2) if total_conf > 0 else 0.0
    avg_conf = round(total_conf / len(matched), 3) if matched else 0.0

    bias = "bullish" if net_pct > 0.5 else ("bearish" if net_pct < -0.5 else "neutral")

    horizon_adjustments = {
        h: round(net_pct * scale, 2)
        for h, scale in HORIZON_SCALE.items()
    }

    return {
        "active": abs(net_pct) > 0.05,
        "net_price_pct": net_pct,
        "confidence": avg_conf,
        "event_count": len(matched),
        "bias": bias,
        "horizon_adjustments": horizon_adjustments,
        "detail": details,
    }


def apply_intelligence_adjustment(
    forecast_series: list[float],
    horizon: int,
    intel: dict[str, Any],
) -> list[float]:
    """Apply intelligence overlay to a forecast horizon series.

    Args:
        forecast_series: Raw forecast values for a specific horizon
        horizon: Horizon in days (3, 7, or 30)
        intel: Result from fetch_product_intelligence_sync()

    Returns:
        Adjusted forecast series (new list)
    """
    if not intel.get("active"):
        return forecast_series

    adj_pct = intel.get("horizon_adjustments", {}).get(horizon, 0.0)
    if abs(adj_pct) < 0.01:
        return forecast_series

    factor = 1.0 + adj_pct / 100.0
    return [round(v * factor, 2) for v in forecast_series]


# ---- Internal helpers ---------------------------------------------------------

_PRODUCT_ALIASES: dict[str, set[str]] = {
    "C5": {"c5", "c5 resin", "c5 hydrocarbon", "c5 petroleum"},
    "C9": {"c9", "c9 resin", "c9 hydrocarbon", "c9 petroleum"},
    "DCPD": {"dcpd", "dicyclopentadiene"},
    "Isoprene": {"isoprene", "ip"},
    "Piperylene": {"piperylene", "piperilene"},
    "Styrene": {"styrene", "sm"},
    "Butadiene": {"butadiene", "bd"},
    "Benzene": {"benzene", "bz"},
    "Toluene": {"toluene", "tol"},
    "Xylene": {"xylene", "mx", "mixed xylene"},
    "Ethylene": {"ethylene", "c2"},
    "Propylene": {"propylene", "c3"},
}


def _match_events_to_product(
    rows: list[Any],
    product_key: str,
) -> list[dict]:
    """Match IntelligenceEvent rows to a product key.

    Uses affected_commodities JSON field + headline text matching + alias table.
    """
    from app.services.intelligence.product_card import (
        summarize_product_intelligence,
    )

    # Build a minimal intelligence_context dict that summarize_product_intelligence
    # can consume. We don't have the full causal analysis here (that's async),
    # so we use a simplified path based on raw event fields.
    aliases = _PRODUCT_ALIASES.get(product_key, {product_key.lower()})
    pk_lower = product_key.lower()

    matched = []
    for row in rows:
        # Check affected_commodities
        commodities = getattr(row, "affected_commodities", None) or []
        if isinstance(commodities, list):
            commodity_hit = any(
                any(alias in (c or "").lower() for alias in aliases)
                for c in commodities
            )
        else:
            commodity_hit = False

        # Check headline text
        headline = (getattr(row, "headline", "") or "").lower()
        headline_hit = any(alias in headline for alias in aliases)
        # Also check if product_key appears in headline
        headline_hit = headline_hit or (pk_lower in headline)

        if not commodity_hit and not headline_hit:
            continue

        matched.append({
            "headline": getattr(row, "headline", ""),
            "event_type": getattr(row, "event_type", "unknown"),
            "direction": getattr(row, "direction", "neutral") or "neutral",
            "magnitude_estimate": getattr(row, "magnitude_estimate", "minor") or "minor",
            "certainty": getattr(row, "certainty", "medium") or "medium",
            "event_id": getattr(row, "event_id", ""),
        })

    return matched


def _direction_magnitude_to_pct(direction: str, magnitude: str) -> float:
    """Convert qualitative direction+magnitude to numeric percentage impact.

    Conservative calibration: even "major" events are capped at ±7.5%.
    """
    base = {
        "minor": 1.5,
        "moderate": 3.5,
        "major": 7.5,
        "critical": 12.0,
    }.get(magnitude.lower(), 1.0)

    sign = {
        "up": 1, "bullish": 1, "positive": 1,
        "down": -1, "bearish": -1, "negative": -1,
    }.get(direction.lower(), 0)

    if sign == 0 and "up" in direction.lower():
        sign = 1
    elif sign == 0 and "down" in direction.lower():
        sign = -1

    return base * sign


def _certainty_to_confidence(certainty: str) -> float:
    """Convert qualitative certainty to a confidence multiplier (0.0-1.0)."""
    return {
        "confirmed": 1.0,
        "high": 0.9,
        "moderate": 0.7,
        "medium": 0.6,
        "low": 0.3,
        "speculative": 0.15,
        "rumor": 0.1,
    }.get(certainty.lower(), 0.5)


def _empty_result() -> dict[str, Any]:
    return {
        "active": False,
        "net_price_pct": 0.0,
        "confidence": 0.0,
        "event_count": 0,
        "bias": "neutral",
        "horizon_adjustments": {3: 0.0, 7: 0.0, 30: 0.0},
        "detail": [],
    }
