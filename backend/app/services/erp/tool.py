"""ERP tool handlers for the ecisco_bi_assistant.

Registers:
- `ask_erp_kpi`      — query product ERP KPIs
- `ask_erp_writeback`  — stage pricing for ERP review

Gated by: `ECISCO_BI_ERP_WRITEBACK_ENABLED`
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .kpi import ErpKpiService
from .writeback import ErpWritebackService


async def _handle_ask_erp_kpi(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_erp_kpi` — query product KPIs from ERP.

    Args:
        product: Product ID or name (required)
        days:    Lookback period in days (default: 30)

    Returns:
        {"success": True, "kpi": {...}}
    """
    from app.services.knowledge_graph.resolver import resolve_product_id

    product: str = (args.get("product") or args.get("product_id") or "").strip()
    if not product:
        return {"success": False, "error": "Missing required arg: product"}

    product_id = resolve_product_id(product)
    if not product_id:
        return {"success": False, "error": f"Could not resolve product '{product}'."}

    days = int(args.get("days", 30))
    svc = ErpKpiService(db=db, org_id=(context or {}).get("org_id"))
    kpi = svc.get_product_kpis(product_id, days=days)

    return {"success": True, "kpi": kpi}


async def _handle_ask_erp_writeback(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_erp_writeback` — stage pricing for ERP review.

    Args:
        product:            Product ID or name (required)
        recommended_price:  Recommended price in CNY/ton (required)
        valid_from:         Start date (default: today)
        valid_until:        End date (optional)
        rationale:          Free-text justification (optional)

    Returns:
        {"success": True, "record": {...}}
    """
    from app.services.knowledge_graph.resolver import resolve_product_id

    product: str = (args.get("product") or args.get("product_id") or "").strip()
    if not product:
        return {"success": False, "error": "Missing required arg: product"}

    price = args.get("recommended_price")
    if price is None:
        return {"success": False, "error": "Missing required arg: recommended_price"}

    product_id = resolve_product_id(product)
    if not product_id:
        return {"success": False, "error": f"Could not resolve product '{product}'."}

    svc = ErpWritebackService(db=db, org_id=(context or {}).get("org_id"))
    result = svc.stage_recommendation(
        product_id=product_id,
        recommended_price=float(price),
        valid_from=args.get("valid_from"),
        valid_until=args.get("valid_until"),
        rationale=args.get("rationale"),
        submitted_by=user_id or "ecisco_bi_assistant",
        session_id=(context or {}).get("session_id"),
    )

    return result


def register_erp_tools(tool_registry) -> None:
    """Register ask_erp_kpi + ask_erp_writeback. Gated by ECISCO_BI_ERP_WRITEBACK_ENABLED."""
    if not settings.ECISCO_BI_ERP_WRITEBACK_ENABLED:
        logger.info("ECISCO_BI_ERP_WRITEBACK_ENABLED=False — skipping ERP tool registration")
        return

    tool_registry.register_tool(
        name="ask_erp_kpi",
        handler=_handle_ask_erp_kpi,
        description="Query ERP KPIs for a C5/C9 product: inventory (tons, days), "
        "30-day sales volume, plant utilization. Provides inventory pressure signals "
        "for pricing adjustment.",
        parameters={
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product ID or name"},
                "days": {"type": "integer", "description": "Lookback period (default: 30)"},
            },
            "required": ["product"],
        },
    )

    tool_registry.register_tool(
        name="ask_erp_writeback",
        handler=_handle_ask_erp_writeback,
        description="Stage a pricing recommendation for ERP review. "
        "Human-in-the-loop gate: recommendations are staged as 'pending' and "
        "require admin approval before being written to live ERP tables.",
        parameters={
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product ID or name"},
                "recommended_price": {"type": "number", "description": "Recommended price in CNY/ton"},
                "valid_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "valid_until": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "rationale": {"type": "string", "description": "Free-text justification"},
            },
            "required": ["product", "recommended_price"],
        },
    )

    logger.info("Registered ask_erp_kpi + ask_erp_writeback tools")
