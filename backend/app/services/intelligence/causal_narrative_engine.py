"""Causal Narrative Engine — multi-hop causal chain reasoning from events to product impacts.

Given any event (Strait closure, tariff, plant shutdown), traces the exact
causal path from event → upstream commodity → C5/C9 products with:
1. Numerical impact estimates per product (CNY/ton)
2. Human-readable causal narrative
3. Confidence decay over time horizons
4. Historical precedent comparison

Ported from EDIA 5.1.2. Adapted: LLM calls use a pluggable async caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.event_impact_db import EventImpactDB, event_impact_db
from app.services.intelligence.second_order_effects import (
    compute_total_second_order_pct,
    get_product_specific_effects,
    get_second_order_effects,
)

logger = logging.getLogger(__name__)

# ── Causal chain elasticities (from knowledge graph) ──────────────────────

_NAPHTHA_ELASTICITIES: dict[str, float] = {
    "mixed_c5": 0.82,
    "isoprene": 0.82 * 0.88 * 0.85,
    "piperylene": 0.82 * 0.85 * 0.85,
    "dcpd": 0.82 * 0.78 * 0.85,
    "c5_resin": 0.82 * 0.85 * 0.70 * 0.72,
    "sis_rubber": 0.82 * 0.88 * 0.65 * 0.72,
    "mixed_c9": 0.70,
    "c9_resin": 0.70 * 0.75 * 0.85,
}

_CRUDE_TO_NAPHTHA_FACTOR = 1.35
_NAPHTHA_BASE_USD = 252.0
_USD_CNY = 7.25

_APPROX_PRICES_CNY: dict[str, float] = {
    "mixed_c5": 5000,
    "isoprene": 9700,
    "piperylene": 8700,
    "dcpd": 6500,
    "c5_resin": 8200,
    "sis_rubber": 13000,
    "mixed_c9": 4500,
    "c9_resin": 7500,
}


@dataclass
class ProductImpact:
    product_id: str
    pct_impact: float
    cny_impact: float
    causal_path: str
    elasticity_used: float
    second_order_pct: float


@dataclass
class CausalChainResult:
    event: dict
    entry_commodity: str
    primary_impact_pct: float
    naphtha_total_pct: float
    product_impacts: dict[str, dict]
    causal_narrative: str
    confidence_by_horizon: dict
    historical_precedents: dict
    recommended_override_type: str
    second_order_summary: dict

    def to_dict(self) -> dict:
        return asdict(self)


class CausalNarrativeEngine:
    """Traces causal chains from events through the supply chain to product-level impacts."""

    def __init__(self, llm_caller: Optional[Callable] = None, impact_db: Optional[EventImpactDB] = None):
        self._llm_caller = llm_caller
        self.impact_db = impact_db or event_impact_db

    async def reason_through_event(self, event: ExtractedEvent) -> CausalChainResult:
        """Full multi-hop causal reasoning from event to product impacts."""
        entry_commodity = self._map_event_to_entry(event)
        primary_pct = self._quantify_primary_impact(event)
        naphtha_base_pct = self._crude_to_naphtha_pct(primary_pct, event)
        second_order_extra = compute_total_second_order_pct(event.event_type)
        naphtha_total_pct = naphtha_base_pct + second_order_extra * 100

        precedents = self.impact_db.find_similar_events(
            event_type=event.event_type,
            magnitude=event.magnitude_estimate,
            geographic_scope=event.geographic_scope,
            k=3,
        )

        product_impacts = {}
        product_specific_effects = get_product_specific_effects(event.event_type)

        for product_id, elasticity in _NAPHTHA_ELASTICITIES.items():
            pct_impact = naphtha_total_pct * elasticity / 100
            product_extra = product_specific_effects.get(product_id, 0.0)
            total_pct = pct_impact + product_extra
            approx_price = _APPROX_PRICES_CNY.get(product_id, 5000)
            cny_impact = round(approx_price * total_pct, 0)

            product_impacts[product_id] = {
                "pct_impact": round(total_pct * 100, 2),
                "cny_impact": cny_impact,
                "causal_path": f"event → {entry_commodity} → naphtha → {product_id}",
                "elasticity_used": round(elasticity, 3),
                "second_order_pct": round(product_extra * 100, 2),
            }

        narrative = await self._generate_narrative(
            event, primary_pct, naphtha_total_pct, product_impacts, precedents
        )

        confidence_by_horizon = self._compute_confidence_decay(event)
        override_type = self._suggest_override_type(event)

        return CausalChainResult(
            event=event.to_dict(),
            entry_commodity=entry_commodity,
            primary_impact_pct=round(primary_pct, 2),
            naphtha_total_pct=round(naphtha_total_pct, 2),
            product_impacts=product_impacts,
            causal_narrative=narrative,
            confidence_by_horizon=confidence_by_horizon,
            historical_precedents=precedents,
            recommended_override_type=override_type,
            second_order_summary=get_second_order_effects(event.event_type),
        )

    def _map_event_to_entry(self, event: ExtractedEvent) -> str:
        type_to_entry = {
            "geopolitical_supply_disruption": "crude_oil",
            "shipping_disruption": "naphtha",
            "plant_outage": "mixed_c5",
            "sinopec_price_change": "mixed_c5",
            "tariff_policy": "mixed_c5",
            "demand_signal": "downstream",
            "opec_decision": "crude_oil",
            "weather_event": "naphtha",
            "currency_move": "naphtha",
            "sanctions": "crude_oil",
        }
        return type_to_entry.get(event.event_type, "crude_oil")

    def _quantify_primary_impact(self, event: ExtractedEvent) -> float:
        magnitude_to_pct = {
            "small": 1.5,
            "medium": 4.0,
            "large": 8.0,
            "extreme": 12.0,
        }
        base_pct = magnitude_to_pct.get(event.magnitude_estimate, 3.0)
        if event.direction == "bearish":
            base_pct = -base_pct
        elif event.direction in ("neutral", "uncertain"):
            base_pct *= 0.3

        certainty_factor = {
            "confirmed": 1.0,
            "likely": 0.80,
            "rumored": 0.50,
            "speculative": 0.30,
        }
        base_pct *= certainty_factor.get(event.certainty, 0.5)
        return base_pct

    def _crude_to_naphtha_pct(self, crude_pct: float, event: ExtractedEvent) -> float:
        entry = self._map_event_to_entry(event)
        if entry == "crude_oil":
            return crude_pct * _CRUDE_TO_NAPHTHA_FACTOR
        elif entry in ("naphtha", "mixed_c5"):
            return crude_pct
        else:
            return crude_pct * 0.5

    def _compute_confidence_decay(self, event: ExtractedEvent) -> dict:
        base = {"confirmed": 0.90, "likely": 0.75, "rumored": 0.55, "speculative": 0.35}
        initial = base.get(event.certainty, 0.6)
        horizon_to_decay = {
            "immediate": 0.02,
            "short": 0.05,
            "medium": 0.08,
        }
        decay_per_week = horizon_to_decay.get(event.time_horizon, 0.06)
        return {
            "3d": round(initial, 2),
            "7d": round(initial - decay_per_week, 2),
            "14d": round(initial - decay_per_week * 2, 2),
            "30d": round(max(initial - decay_per_week * 4, 0.20), 2),
        }

    def _suggest_override_type(self, event: ExtractedEvent) -> str:
        mapping = {
            "geopolitical_supply_disruption": "crude_spike",
            "shipping_disruption": "crude_spike",
            "opec_decision": "crude_spike",
            "tariff_policy": "tariff_shock",
            "sanctions": "tariff_shock",
            "plant_outage": "force_majeure",
            "weather_event": "force_majeure",
            "sinopec_price_change": "manual",
            "demand_signal": "manual",
            "currency_move": "manual",
        }
        return mapping.get(event.event_type, "manual")

    async def _generate_narrative(
        self,
        event: ExtractedEvent,
        primary_pct: float,
        naphtha_pct: float,
        product_impacts: dict,
        precedents: dict,
    ) -> str:
        """Generate human-readable causal narrative using LLM (with fallback)."""
        impact_lines = []
        for pid, imp in sorted(
            product_impacts.items(), key=lambda x: abs(x[1]["cny_impact"]), reverse=True
        ):
            impact_lines.append(
                f"  {pid}: {imp['cny_impact']:+.0f} CNY/ton ({imp['pct_impact']:+.1f}%) via {imp['causal_path']}"
            )
        impact_table = "\n".join(impact_lines)

        prec_lines = []
        for p in precedents.get("similar_events", [])[:3]:
            prec_lines.append(
                f"  - {p['date']} {p['headline']}: crude {p.get('crude_impact_pct', 0):+.1f}%, "
                f"C5 {p.get('c5_impact_cny', 0):+.0f} CNY, resolved in {p.get('duration_days', '?')} days. "
                f"Lesson: {p.get('key_lesson', 'N/A')}"
            )
        prec_text = "\n".join(prec_lines) if prec_lines else "  No close precedents found."

        if self._llm_caller is None:
            return self._fallback_narrative(event, primary_pct, naphtha_pct, impact_table, prec_text)

        prompt = f"""You are a senior C5/C9 petrochemical market strategist at Ecisco.
A market event has just occurred. Write a concise but thorough market intelligence briefing.

EVENT: {event.headline}
Type: {event.event_type} | Direction: {event.direction} | Magnitude: {event.magnitude_estimate} | Certainty: {event.certainty}
Primary commodity impact: {primary_pct:+.1f}%
Naphtha total impact (incl. second-order): {naphtha_pct:+.1f}%

PRODUCT IMPACTS:
{impact_table}

HISTORICAL PRECEDENTS:
{prec_text}

Write a 3-paragraph briefing:
Paragraph 1 — WHAT HAPPENED AND WHY IT MATTERS
Paragraph 2 — QUANTIFIED IMPACT PER PRODUCT
Paragraph 3 — WHAT ECISCO SHOULD DO RIGHT NOW
Write in professional market analysis style with specific CNY/ton numbers."""

        try:
            narrative = await self._llm_caller(
                "You are a senior petrochemical market strategist providing actionable intelligence briefings.",
                prompt,
            )
            if isinstance(narrative, str):
                return narrative
            return str(narrative)
        except Exception as e:
            logger.warning("Narrative generation failed: %s", e)
            return self._fallback_narrative(event, primary_pct, naphtha_pct, impact_table, prec_text)

    @staticmethod
    def _fallback_narrative(
        event: ExtractedEvent, primary_pct: float, naphtha_pct: float,
        impact_table: str, prec_text: str,
    ) -> str:
        return (
            f"EVENT: {event.headline}\n"
            f"Type: {event.event_type} | Direction: {event.direction}\n"
            f"Primary impact: {primary_pct:+.1f}%\n"
            f"Naphtha total impact: {naphtha_pct:+.1f}%\n\n"
            f"PRODUCT IMPACTS:\n{impact_table}\n\n"
            f"HISTORICAL PRECEDENTS:\n{prec_text}"
        )
