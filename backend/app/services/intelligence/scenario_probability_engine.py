"""Scenario Probability Engine — evidence-based scenario generation.

Replaces the simple symmetric Bull/Base/Bear (33/33/33) with dynamic,
event-driven probabilities derived from:
1. Historical base rates for this event type
2. Current event certainty + source credibility
3. Magnitude adjustment (extreme events resolve slower)
4. LLM narrative per scenario with trigger + falsification conditions

Ported from EDIA 5.1.2. Adapted: LLM enrichment uses pluggable async caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.causal_narrative_engine import CausalChainResult

logger = logging.getLogger(__name__)

_HISTORICAL_BASE_RATES: dict[str, dict[str, float]] = {
    "geopolitical_supply_disruption": {"resolves_in_7d": 0.35, "resolves_in_14d": 0.35, "structural": 0.30},
    "shipping_disruption": {"resolves_in_7d": 0.20, "resolves_in_14d": 0.40, "structural": 0.40},
    "plant_outage": {"resolves_in_7d": 0.30, "resolves_in_14d": 0.45, "structural": 0.25},
    "tariff_policy": {"resolves_in_7d": 0.10, "resolves_in_14d": 0.25, "structural": 0.65},
    "demand_signal": {"resolves_in_7d": 0.15, "resolves_in_14d": 0.35, "structural": 0.50},
    "opec_decision": {"resolves_in_7d": 0.10, "resolves_in_14d": 0.30, "structural": 0.60},
    "sinopec_price_change": {"resolves_in_7d": 0.50, "resolves_in_14d": 0.40, "structural": 0.10},
    "weather_event": {"resolves_in_7d": 0.55, "resolves_in_14d": 0.35, "structural": 0.10},
    "currency_move": {"resolves_in_7d": 0.15, "resolves_in_14d": 0.35, "structural": 0.50},
    "sanctions": {"resolves_in_7d": 0.10, "resolves_in_14d": 0.25, "structural": 0.65},
}

_MAGNITUDE_ADJUSTMENT: dict[str, dict[str, float]] = {
    "small": {"fast_resolution": 0.20, "structural": -0.10},
    "medium": {"fast_resolution": 0.0, "structural": 0.0},
    "large": {"fast_resolution": -0.15, "structural": 0.10},
    "extreme": {"fast_resolution": -0.25, "structural": 0.20},
}


@dataclass
class IntelligentScenario:
    scenario_id: str
    label: str
    probability: float
    price_targets: dict[str, float]
    causal_narrative: str
    trigger_conditions: list[str]
    falsification_conditions: list[str]
    time_to_resolution: str

    def to_dict(self) -> dict:
        return asdict(self)


class ScenarioProbabilityEngine:
    """Generates evidence-based scenarios with asymmetric probabilities."""

    def __init__(self, llm_caller: Optional[Callable] = None):
        self._llm_caller = llm_caller

    async def generate_scenarios(
        self,
        event: ExtractedEvent,
        causal_chain: CausalChainResult,
    ) -> list[IntelligentScenario]:
        """Generate 3 scenarios with calibrated probabilities."""
        probs = self._compute_probabilities(event)
        scenarios = []

        bull_targets = {}
        for pid, imp in causal_chain.product_impacts.items():
            base_price = imp.get("cny_impact", 0) / (imp.get("pct_impact", 1) / 100.0 + 0.001)
            bull_targets[pid] = round(base_price + imp["cny_impact"] * 1.4, 0)

        scenarios.append(IntelligentScenario(
            scenario_id=f"bull_{event.event_type[:20]}",
            label=self._bull_label(event),
            probability=probs["bull"],
            price_targets=bull_targets,
            causal_narrative=f"If the {event.event_type.replace('_', ' ')} persists or escalates beyond 2 weeks, "
            f"the supply chain impact compounds. Historical precedent suggests sustained "
            f"price elevation of +40% above base impact estimate.",
            trigger_conditions=self._bull_triggers(event),
            falsification_conditions=self._bear_triggers(event),
            time_to_resolution="14-45 days",
        ))

        base_targets = {}
        for pid, imp in causal_chain.product_impacts.items():
            base_price = imp.get("cny_impact", 0) / (imp.get("pct_impact", 1) / 100.0 + 0.001)
            base_targets[pid] = round(base_price + imp["cny_impact"], 0)

        scenarios.append(IntelligentScenario(
            scenario_id=f"base_{event.event_type[:20]}",
            label=self._base_label(event),
            probability=probs["base"],
            price_targets=base_targets,
            causal_narrative=f"Historical {event.event_type.replace('_', ' ')} events average 7-14 day "
            f"resolution. Prices settle at the computed causal chain impact level "
            f"then gradually revert.",
            trigger_conditions=[
                "Price movement aligns with computed causal chain within ±15%",
                f"Event shows signs of resolution within 10 days",
            ],
            falsification_conditions=[
                "Event escalates significantly beyond initial assessment",
                "New related event compounds the impact",
            ],
            time_to_resolution="7-14 days",
        ))

        bear_targets = {}
        for pid, imp in causal_chain.product_impacts.items():
            base_price = imp.get("cny_impact", 0) / (imp.get("pct_impact", 1) / 100.0 + 0.001)
            bear_targets[pid] = round(base_price + imp["cny_impact"] * 0.4, 0)

        scenarios.append(IntelligentScenario(
            scenario_id=f"bear_{event.event_type[:20]}",
            label=self._bear_label(event),
            probability=probs["bear"],
            price_targets=bear_targets,
            causal_narrative=f"Rapid resolution or market overreaction means prices give back most "
            f"of the initial move. The 2019 Abqaiq precedent: crude +15% resolved "
            f"in 8 days. Do not chase the initial spike.",
            trigger_conditions=self._bear_triggers(event),
            falsification_conditions=self._bull_triggers(event),
            time_to_resolution="3-7 days",
        ))

        if self._llm_caller:
            scenarios = await self._enrich_with_llm(event, causal_chain, scenarios)

        return scenarios

    def _compute_probabilities(self, event: ExtractedEvent) -> dict[str, float]:
        base_rates = _HISTORICAL_BASE_RATES.get(
            event.event_type,
            {"resolves_in_7d": 0.33, "resolves_in_14d": 0.34, "structural": 0.33},
        )
        mag_adj = _MAGNITUDE_ADJUSTMENT.get(event.magnitude_estimate, {})
        raw = {
            "bull": base_rates["structural"] + mag_adj.get("structural", 0),
            "base": base_rates["resolves_in_14d"],
            "bear": base_rates["resolves_in_7d"] + mag_adj.get("fast_resolution", 0),
        }
        raw = {k: max(v, 0.05) for k, v in raw.items()}
        total = sum(raw.values())
        return {k: round(v / total, 2) for k, v in raw.items()}

    def _bull_label(self, event: ExtractedEvent) -> str:
        labels = {
            "geopolitical_supply_disruption": "Bull: Sustained Disruption",
            "shipping_disruption": "Bull: Prolonged Route Disruption",
            "plant_outage": "Bull: Extended Outage",
            "tariff_policy": "Bull: Full Tariff Implementation",
            "demand_signal": "Bull: Demand Surge Sustains",
            "opec_decision": "Bull: OPEC Cuts Hold",
        }
        return labels.get(event.event_type, "Bull: Event Persists")

    def _base_label(self, event: ExtractedEvent) -> str:
        return "Base: Gradual Resolution (7-14d)"

    def _bear_label(self, event: ExtractedEvent) -> str:
        labels = {
            "geopolitical_supply_disruption": "Bear: Rapid Diplomatic Resolution",
            "shipping_disruption": "Bear: Quick Route Normalization",
            "plant_outage": "Bear: Fast Restart",
            "tariff_policy": "Bear: Partial Exemption / Rollback",
        }
        return labels.get(event.event_type, "Bear: Rapid Resolution")

    def _bull_triggers(self, event: ExtractedEvent) -> list[str]:
        triggers = {
            "geopolitical_supply_disruption": [
                "Crude sustains above spike level after Day 7",
                "No diplomatic resolution signals within 10 days",
                "Insurance rates remain elevated",
            ],
            "shipping_disruption": [
                "Rerouting costs remain elevated beyond 2 weeks",
                "Additional shipping incidents reported",
            ],
            "plant_outage": [
                "Restart timeline extended beyond original estimate",
                "Spot market premium persists beyond 1 week",
            ],
            "tariff_policy": [
                "No exemption negotiations announced",
                "Retaliatory tariffs imposed",
            ],
        }
        return triggers.get(event.event_type, ["Event intensity does not decrease within 7 days"])

    def _bear_triggers(self, event: ExtractedEvent) -> list[str]:
        triggers = {
            "geopolitical_supply_disruption": [
                "Diplomatic back-channel talks confirmed",
                "Crude falls >5% from peak",
                "One party signals de-escalation",
            ],
            "shipping_disruption": [
                "First commercial transit successfully completes",
                "Naval escort program announced",
            ],
            "plant_outage": [
                "Restart confirmation from operator",
                "Alternative supply secured by major buyers",
            ],
            "tariff_policy": [
                "Exemption or delay announcement",
                "Trade negotiation talks scheduled",
            ],
        }
        return triggers.get(event.event_type, ["Clear signs of resolution within 3 days"])

    async def _enrich_with_llm(
        self,
        event: ExtractedEvent,
        causal_chain: CausalChainResult,
        scenarios: list[IntelligentScenario],
    ) -> list[IntelligentScenario]:
        try:
            scenario_summary = "\n".join(
                f"- {s.label} (prob={s.probability:.0%}): {s.causal_narrative[:100]}..."
                for s in scenarios
            )
            prompt = f"""Given this market event and 3 scenarios, provide ONE additional sentence
per scenario about what Ecisco should do IF that scenario materializes.

Event: {event.headline}
Scenarios:
{scenario_summary}

Return as JSON array of 3 strings: ["action for bull", "action for base", "action for bear"]"""

            result = await self._llm_caller(
                "You are a petrochemical market strategist. Return ONLY valid JSON.",
                prompt,
            )
            if isinstance(result, list) and len(result) >= 3:
                for i, action_note in enumerate(result[:3]):
                    if isinstance(action_note, str):
                        scenarios[i].causal_narrative += f"\n\nEcisco Action: {action_note}"
        except Exception as e:
            logger.debug("Scenario enrichment failed: %s", e)
        return scenarios
