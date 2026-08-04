"""Intelligence tool handlers — tools for the intelligence_agent.

These tools allow the intelligence_agent to:
- get_intelligence_context: Retrieve the full intelligence context (events, causal analyses, scenarios)
- get_market_events: List recent market events as a simple list
- ingest_manual_event: Manually submit an event and run the full pipeline on it
"""

from __future__ import annotations

import logging

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _get_intelligence_context(args, db, user_id, context=None):
    """Retrieve the full intelligence context for agent prompt injection."""
    try:
        from app.services.intelligence.intelligence_orchestrator import IntelligenceOrchestrator
        org_id = (context or {}).get("org_id", "default-org") if context else "default-org"
        lookback_hours = int(args.get("lookback_hours", 48))
        min_impact = float(args.get("min_impact", 0.3))
        orchestrator = IntelligenceOrchestrator(db, org_id=org_id, llm_caller=None, min_impact=min_impact, lookback_hours=lookback_hours)
        result = await orchestrator.get_intelligence_context(lookback_hours=lookback_hours, min_impact=min_impact)
        return {"success": True, "context": result}
    except Exception as e:
        logger.error("get_intelligence_context failed: %s", e)
        return {"success": False, "error": str(e)}


async def _get_market_events(args, db, user_id, context=None):
    """List recent market events."""
    try:
        from app.services.intelligence.intelligence_orchestrator import IntelligenceOrchestrator
        org_id = (context or {}).get("org_id", "default-org") if context else "default-org"
        hours = int(args.get("hours", 24))
        min_impact = float(args.get("min_impact", 0.0))
        limit = int(args.get("limit", 50))
        orchestrator = IntelligenceOrchestrator(db, org_id=org_id, llm_caller=None)
        events = await orchestrator.get_market_events(hours=hours, min_impact=min_impact, limit=limit)
        return {"success": True, "events": events, "count": len(events)}
    except Exception as e:
        logger.error("get_market_events failed: %s", e)
        return {"success": False, "error": str(e)}


async def _ingest_manual_event(args, db, user_id, context=None):
    """Manually submit a market event and run the full intelligence pipeline on it."""
    try:
        from app.services.intelligence.event_types import ExtractedEvent
        from app.services.intelligence.intelligence_orchestrator import IntelligenceOrchestrator
        org_id = (context or {}).get("org_id", "default-org") if context else "default-org"
        event = ExtractedEvent(
            event_type=args.get("event_type", "demand_signal"),
            headline=args.get("headline", "Manual event"),
            affected_commodities=args.get("affected_commodities", []),
            direction=args.get("direction", "uncertain"),
            magnitude_estimate=args.get("magnitude_estimate", "medium"),
            certainty=args.get("certainty", "likely"),
            time_horizon=args.get("time_horizon", "short"),
            geographic_scope=args.get("geographic_scope", "global"),
            relevance_to_c5_c9=args.get("relevance_to_c5_c9", "indirect"),
            key_entities=args.get("key_entities", []),
            causal_chain_hint=args.get("causal_chain_hint", ""),
            source_url=args.get("source_url", ""),
            source_credibility=args.get("source_credibility", "tier2"),
            key_information=args.get("key_information", ""),
        )
        orchestrator = IntelligenceOrchestrator(db, org_id=org_id, llm_caller=None)
        result = await orchestrator.ingest_manual_event(event)
        return {"success": True, "result": result}
    except Exception as e:
        logger.error("ingest_manual_event failed: %s", e)
        return {"success": False, "error": str(e)}


# Schemas & Registration

_INTEL_CONTEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_intelligence_context",
        "description": "Retrieve the full market intelligence context: recent events, causal chain analyses, scenario probabilities, and auto-override decisions. Use this before pricing or diagnosis to inject event-aware context.",
        "parameters": {
            "type": "object",
            "properties": {
                "lookback_hours": {"type": "integer", "description": "Hours to look back (default 48)."},
                "min_impact": {"type": "number", "description": "Min impact 0-1 (default 0.3)."},
            },
        },
    },
}

_MARKET_EVENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_market_events",
        "description": "List recent market events detected by the intelligence layer.",
        "parameters": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Hours to look back (default 24)."},
                "min_impact": {"type": "number", "description": "Min impact 0-1 (default 0)."},
                "limit": {"type": "integer", "description": "Max events (default 50)."},
            },
        },
    },
}

_INGEST_EVENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ingest_manual_event",
        "description": "Manually submit a market event and run the full intelligence pipeline (causal analysis, scenarios, override).",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "description": "geopolitical_supply_disruption, plant_outage, sinopec_price_change, tariff_policy, demand_signal, shipping_disruption, opec_decision, weather_event, currency_move, sanctions"},
                "headline": {"type": "string", "description": "One-sentence summary."},
                "direction": {"type": "string", "description": "bullish, bearish, neutral, uncertain"},
                "magnitude_estimate": {"type": "string", "description": "small, medium, large, extreme"},
                "certainty": {"type": "string", "description": "confirmed, likely, rumored, speculative"},
                "affected_commodities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["event_type", "headline"],
        },
    },
}

for _name, _schema, _handler, _desc in (
    ("get_intelligence_context", _INTEL_CONTEXT_SCHEMA, _get_intelligence_context, "Get full intelligence context."),
    ("get_market_events", _MARKET_EVENTS_SCHEMA, _get_market_events, "List recent market events."),
    ("ingest_manual_event", _INGEST_EVENT_SCHEMA, _ingest_manual_event, "Manually ingest a market event."),
):
    registry.register(
        name=_name, schema=_schema, handler=_handler,
        category="intelligence", enabled_by_default=False, description=_desc,
    )
