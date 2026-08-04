"""Intelligence Orchestrator — central coordinator for the intelligence pipeline.

Coordinates: fetch events → run causal analysis → generate scenarios → evaluate overrides.
Returns structured context that agents can inject into their prompts.

Ported from EDIA 5.1.2. Adapted: module-level singletons replaced with
class-based service that accepts a db session.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.event_registry import IntelligenceEventRegistry
from app.services.intelligence.causal_narrative_engine import (
    CausalNarrativeEngine,
    CausalChainResult,
)
from app.services.intelligence.scenario_probability_engine import (
    ScenarioProbabilityEngine,
    IntelligentScenario,
)
from app.services.intelligence.auto_override_decision import (
    AutoOverrideDecisionEngine,
    OverrideDecision,
)

logger = logging.getLogger(__name__)


class IntelligenceOrchestrator:
    """Coordinates the full intelligence pipeline for a given org."""

    def __init__(
        self,
        db: AsyncSession,
        org_id: str = "default-org",
        llm_caller: Optional[Callable] = None,
        min_impact: float = 0.3,
        lookback_hours: int = 48,
    ):
        self._db = db
        self._org_id = org_id
        self._llm_caller = llm_caller
        self._min_impact = min_impact
        self._lookback_hours = lookback_hours

        self._registry = IntelligenceEventRegistry(db)
        self._causal_engine = CausalNarrativeEngine(llm_caller=llm_caller)
        self._scenario_engine = ScenarioProbabilityEngine(llm_caller=llm_caller)
        self._override_engine = AutoOverrideDecisionEngine()

    async def get_intelligence_context(
        self,
        lookback_hours: Optional[int] = None,
        min_impact: Optional[float] = None,
    ) -> dict[str, Any]:
        """Get the full intelligence context for agent prompt injection."""
        hours = lookback_hours or self._lookback_hours
        impact = min_impact or self._min_impact

        events = await self._registry.list_recent_events(
            hours=hours, min_impact=impact, review_status="approved",
            limit=20, org_id=self._org_id,
        )

        if not events:
            return self._empty_context(hours)

        causal_results: list[CausalChainResult] = []
        scenarios_by_event: dict[str, list[IntelligentScenario]] = {}
        overrides: list[OverrideDecision] = []

        top_events = sorted(events, key=lambda e: e.impact_magnitude, reverse=True)[:5]

        for event in top_events:
            try:
                causal = await self._causal_engine.reason_through_event(event)
                causal_results.append(causal)
                event_scenarios = await self._scenario_engine.generate_scenarios(event, causal)
                scenarios_by_event[event.event_id] = event_scenarios
                override = self._override_engine.decide(event, causal)
                if override.action != "log_only":
                    overrides.append(override)
            except Exception as e:
                logger.warning("Failed to process event %s: %s", event.event_id, e)

        freshness = await self._registry.get_ingestion_freshness(
            "news_ingestion_agent", org_id=self._org_id
        )
        summary = self._build_summary(events, causal_results, overrides, freshness)

        return {
            "recent_events": [e.to_dict() for e in events],
            "causal_analyses": [c.to_dict() for c in causal_results],
            "scenarios": {eid: [s.to_dict() for s in scens] for eid, scens in scenarios_by_event.items()},
            "overrides": [o.to_dict() for o in overrides],
            "freshness": freshness,
            "summary": summary,
        }

    async def get_market_events(
        self, hours: int = 24, min_impact: float = 0.0, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get a list of recent market events as dicts."""
        events = await self._registry.list_recent_events(
            hours=hours, min_impact=min_impact, review_status="approved",
            limit=limit, org_id=self._org_id,
        )
        return [e.to_dict() for e in events]

    async def ingest_manual_event(self, event: ExtractedEvent) -> dict[str, Any]:
        """Manually ingest an event and run the full pipeline on it."""
        await self._registry.upsert_event(event, org_id=self._org_id)
        try:
            causal = await self._causal_engine.reason_through_event(event)
            scenarios = await self._scenario_engine.generate_scenarios(event, causal)
            override = self._override_engine.decide(event, causal)
            return {
                "event": event.to_dict(),
                "causal_analysis": causal.to_dict(),
                "scenarios": [s.to_dict() for s in scenarios],
                "override": override.to_dict(),
            }
        except Exception as e:
            logger.warning("Manual event pipeline failed: %s", e)
            return {"event": event.to_dict(), "error": str(e)}

    def _empty_context(self, hours: int) -> dict[str, Any]:
        return {
            "recent_events": [], "causal_analyses": [], "scenarios": {},
            "overrides": [], "freshness": {"state": "unknown", "stale": True},
            "summary": f"No intelligence events detected in the last {hours} hours.",
        }

    def _build_summary(
        self, events: list[ExtractedEvent], causal_results: list[CausalChainResult],
        overrides: list[OverrideDecision], freshness: dict,
    ) -> str:
        lines = [f"INTELLIGENCE SUMMARY ({len(events)} events in last {self._lookback_hours}h):"]
        if freshness.get("stale"):
            lines.append(f"  [WARNING] Intelligence data is stale (last update: {freshness.get('last_success_at', 'unknown')})")
        for event in events[:5]:
            lines.append(f"  [{event.event_type}] {event.headline[:80]} (impact={event.impact_magnitude:.2f}, dir={event.direction})")
        if causal_results:
            lines.append("\nCAUSAL ANALYSIS:")
            for causal in causal_results[:3]:
                lines.append(f"  naphtha {causal.naphtha_total_pct:+.1f}% — {causal.event.get('headline', '')[:60]}")
        if overrides:
            lines.append(f"\nOVERRIDES: {len(overrides)} pending/auto-injected")
            for o in overrides[:3]:
                lines.append(f"  [{o.action}] {o.event_headline[:60]} (conf={o.injection_confidence:.0%})")
        return "\n".join(lines)
