"""Pricing + Macro Override tool handlers for the ecisco_bi_assistant.

Registers:
- `ask_pricing`       — run the 9-step pricing ladder for a product
- `ask_macro_override` — manage the macro override (get/set/clear)

Gated by: `ECISCO_BI_PRICING_ENABLED`
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .service import PricingService
from .macro_override import MacroOverrideService


# ── Pricing tool ────────────────────────────────────────────────────

async def _handle_ask_pricing(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_pricing` — 9-step pricing decision for a product.

    Args:
        product:       Product ID or name (required)
        perception:    Perception snapshot dict (optional; auto-fetched if missing)
        diagnosis:     Diagnosis dict (optional)
        forecast:      Forecast dict (optional)
        intelligence:  Intelligence context dict (optional)

    Returns:
        {"success": True, "pricing_decision": {...}} or {"success": False, "error": "..."}
    """
    from app.services.knowledge_graph.resolver import resolve_product_id

    product: str = (args.get("product") or args.get("product_id") or "").strip()
    if not product:
        return {"success": False, "error": "Missing required arg: product (product ID or name)"}

    product_id = resolve_product_id(product)
    if not product_id:
        return {
            "success": False,
            "error": f"Could not resolve product '{product}'. "
            f"Supported: isoprene, piperylene, dcpd, cracked_c5, mixed_c5, "
            f"cracked_c9, raffinate_c5, sis, c5_resin, styrene, blowing_agent",
        }

    # Build or accept perception snapshot
    perception = args.get("perception") or args.get("perception_snapshot") or {}
    if not perception and db:
        try:
            from app.services.market_dashboard.service import MarketDashboardService
            mds = MarketDashboardService(db)
            perception = mds.build_snapshot()
        except Exception as exc:
            logger.warning("Could not build perception snapshot: %s", exc)

    diagnosis = args.get("diagnosis") or {}
    forecast_dict = args.get("forecast") or {}
    intelligence = args.get("intelligence") or args.get("intelligence_context") or {}

    svc = PricingService()
    decision = svc.compute_pricing_decision(
        product_id=product_id,
        perception_snapshot=perception,
        diagnosis=diagnosis,
        forecast=forecast_dict,
        intelligence_context=intelligence,
    )

    prompt_text = svc.to_prompt_text(decision)
    return {
        "success": True,
        "pricing_decision": decision,
        "prompt_text": prompt_text,
    }


# ── Macro override tool ─────────────────────────────────────────────

async def _handle_ask_macro_override(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_macro_override` — manage the active macro override.

    Modes:
    - "get" / "status": Return current override status
    - "set": Apply a new override (naphtha_shock_pct, event_type, description, ttl_hours)
    - "clear": Deactivate current override

    Args:
        mode:              "get" | "set" | "clear" (default "get")
        naphtha_shock_pct: % shock (required for mode="set")
        crude_shock_pct:   % crude shock (mode="set")
        event_type:        Event type (mode="set", default "manual")
        description:       Free-text (mode="set")
        ttl_hours:         TTL in hours (mode="set", default 24)
        product_shocks:    Optional {product_id: extra_pct_adj} (mode="set")

    Returns:
        {"success": True, "override": {...}} or {"success": False, "error": "..."}
    """
    org_id = (context or {}).get("org_id")
    svc = MacroOverrideService(db=db, org_id=org_id)

    mode = (args.get("mode") or "get").strip().lower()

    if mode in ("get", "status"):
        override = svc.get_active_override()
        summary = svc.override_summary(override) if override else "No active macro override."
        return {
            "success": True,
            "active": override is not None,
            "override": override,
            "summary": summary,
        }

    if mode == "set":
        naphtha = args.get("naphtha_shock_pct")
        if naphtha is None:
            return {"success": False, "error": "Missing required arg: naphtha_shock_pct"}
        override = svc.set_override(
            naphtha_shock_pct=float(naphtha),
            crude_shock_pct=float(args.get("crude_shock_pct", 0)),
            event_type=str(args.get("event_type", "manual")),
            description=str(args.get("description", "")),
            ttl_hours=int(args.get("ttl_hours", 24)),
            product_shocks=args.get("product_shocks"),
        )
        return {
            "success": True,
            "active": True,
            "override": override,
            "summary": svc.override_summary(override),
        }

    if mode == "clear":
        svc.clear_override()
        return {"success": True, "active": False, "summary": "Override cleared."}

    return {"success": False, "error": f"Unknown mode: {mode}. Use get/set/clear."}


# ── Registration ────────────────────────────────────────────────────

def register_pricing_tools(tool_registry) -> None:
    """Register ask_pricing and ask_macro_override tools.

    Only registered when ECISCO_BI_PRICING_ENABLED is True in settings.
    """
    if not settings.ECISCO_BI_PRICING_ENABLED:
        logger.info("ECISCO_BI_PRICING_ENABLED=False — skipping pricing tool registration")
        return

    tool_registry.register_tool(
        name="ask_pricing",
        handler=_handle_ask_pricing,
        description="9-step pricing ladder for C5/C9 products (isoprene, piperylene, dcpd, "
        "cracked_c5, mixed_c5, cracked_c9, raffinate_c5, sis, c5_resin, styrene, blowing_agent). "
        "Returns recommended price, range, action, triggers, and confidence.",
        parameters={
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "description": "Product ID or name (e.g. 'isoprene', '异戊二烯', 'cracked_c5')",
                },
                "diagnosis": {
                    "type": "object",
                    "description": "Optional diagnosis dict from Stage 2 pipeline",
                },
                "forecast": {
                    "type": "object",
                    "description": "Optional forecast dict from Stage 3 pipeline",
                },
            },
            "required": ["product"],
        },
    )

    tool_registry.register_tool(
        name="ask_macro_override",
        handler=_handle_ask_macro_override,
        description="Manage the active macro override (naphtha shock). "
        "Modes: get (read current), set (apply a ±4%-capped shock), clear (deactivate). "
        "Used to inject real-time macro events into the pricing ladder.",
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["get", "set", "clear"],
                    "description": "Operation: get current, set new, or clear",
                },
                "naphtha_shock_pct": {
                    "type": "number",
                    "description": "% shock to naphtha (e.g. 2.5 = +2.5%, capped at ±4%). Required for mode=set.",
                },
                "event_type": {
                    "type": "string",
                    "enum": ["crude_spike", "tariff_shock", "force_majeure", "manual"],
                    "description": "Event type for mode=set. Default: manual",
                },
                "description": {
                    "type": "string",
                    "description": "Free-text description for mode=set",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": "Hours until auto-expiry for mode=set. Default: 24",
                },
            },
            "required": ["mode"],
        },
    )

    logger.info("Registered ask_pricing + ask_macro_override tools")
