"""Decision tool handler for the ecisco_bi_assistant.

Registers:
- `ask_decision` — assemble pipeline outputs into final decision synthesis

Gated by: `ECISCO_BI_PRICING_ENABLED` (shared flag with decision)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .service import DecisionService, assemble_decision
from .audit import DecisionAuditService
from ..knowledge_graph.resolver import resolve_product_id


async def _handle_ask_decision(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_decision` — final synthesis from pipeline outputs.

    Assembles perception → diagnosis → forecast → pricing → critic → decision report.

    Args:
        product:            Product ID or name (required)
        perception:         Perception snapshot dict (optional)
        diagnosis:          Diagnosis dict (optional)
        forecast:           Forecast dict (optional)
        pricing_decision:   Pricing decision dict from ask_pricing (optional)
        intelligence:       Intelligence context dict (optional)
        weekly_context:     Weekly report context dict (optional)
        mode:               "full" (default) | "dry_run" (critic only)

    Returns:
        {"success": True, "decision": {...}} or {"success": False, "error": "..."}
    """
    product: str = (args.get("product") or args.get("product_id") or "").strip()
    if not product:
        return {"success": False, "error": "Missing required arg: product"}

    product_id = resolve_product_id(product)
    if not product_id:
        return {
            "success": False,
            "error": f"Could not resolve product '{product}'. "
            f"Supported: isoprene, piperylene, dcpd, cracked_c5, mixed_c5, "
            f"cracked_c9, raffinate_c5, sis, c5_resin, styrene, blowing_agent",
        }

    mode = (args.get("mode") or "full").strip().lower()

    # Try to auto-fetch perception if not provided. Route through the real
    # perception pipeline (which bridges to the price warehouse) instead of the
    # dead MarketDashboardService.build_snapshot() that always returned empty.
    perception = args.get("perception") or {}
    if not perception and db:
        try:
            from app.services.perception.service import build_perception_snapshot

            perception = build_perception_snapshot(db=db)
        except Exception as exc:
            logger.warning("Could not build perception snapshot: %s", exc)

    diagnosis = args.get("diagnosis") or {}
    forecast_dict = args.get("forecast") or {}
    pricing = args.get("pricing_decision") or {}
    intelligence = args.get("intelligence") or args.get("intelligence_context") or {}
    weekly = args.get("weekly_context") or {}

    if mode == "dry_run":
        # Run critic only
        from .critic import DecisionCritic
        c = DecisionCritic()
        evidence = DecisionService(None)._build_evidence_bundle(
            diagnosis, forecast_dict, pricing, intelligence,
        )
        critic = c.review(diagnosis, forecast_dict, pricing, evidence)
        return {"success": True, "mode": "dry_run", "critic_result": critic}

    # Full pipeline
    decision = assemble_decision(
        product_id=product_id, perception=perception, diagnosis=diagnosis,
        forecast=forecast_dict, pricing_decision=pricing,
        intelligence_context=intelligence, weekly_report_context=weekly,
    )

    return {"success": True, "decision": decision}


def register_decision_tools(tool_registry) -> None:
    """Register ask_decision tool. Gated by ECISCO_BI_PRICING_ENABLED."""
    if not settings.ECISCO_BI_PRICING_ENABLED:
        logger.info("ECISCO_BI_PRICING_ENABLED=False — skipping decision tool registration")
        return

    tool_registry.register_tool(
        name="ask_decision",
        handler=_handle_ask_decision,
        description="Final decision synthesis: assemble perception→diagnosis→forecast→pricing "
        "into a decision report with critic checks, evidence bundle, and audit trail. "
        "Mode 'full' runs the complete pipeline; 'dry_run' runs only the critic.",
        parameters={
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "description": "Product ID or name (e.g. 'isoprene', 'dcpd', 'cracked_c5')",
                },
                "pricing_decision": {
                    "type": "object",
                    "description": "Pricing decision output from ask_pricing (optional)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["full", "dry_run"],
                    "description": "Execution mode. Default: full",
                },
            },
            "required": ["product"],
        },
    )

    logger.info("Registered ask_decision tool")
