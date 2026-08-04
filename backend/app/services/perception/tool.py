"""Perception tool handler for the ecisco_bi_assistant.

Registers:
- `ask_perception` — build market snapshot with anomaly detection and macro regime

Gated by: `ECISCO_BI_PERCEPTION_ENABLED`
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .service import PerceptionService


async def _handle_ask_perception(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_perception` — build market snapshot for Ecisco BI.

    Args:
        cutoff_date: Optional reference date (default: today)
        product:      Optional product filter (snapshot for all products otherwise)

    Returns:
        {"success": True, "snapshot": {...}, "prompt_text": "..."}
    """
    cutoff_date = args.get("cutoff_date") or None
    product_filter = (args.get("product") or args.get("product_id") or "").strip()

    svc = PerceptionService(db=db)
    snapshot = svc.build_snapshot(cutoff_date=cutoff_date)

    # Optionally filter to single product
    if product_filter:
        from app.services.knowledge_graph.resolver import resolve_product_id
        pid = resolve_product_id(product_filter)
        if pid and pid in snapshot.get("products", {}):
            snapshot["products"] = {pid: snapshot["products"][pid]}
            snapshot["focal_product"] = pid

    prompt_text = PerceptionService.to_prompt_text(snapshot)

    return {
        "success": True,
        "snapshot": snapshot,
        "prompt_text": prompt_text,
    }


def register_perception_tools(tool_registry) -> None:
    """Register ask_perception tool. Gated by ECISCO_BI_PERCEPTION_ENABLED."""
    if not settings.ECISCO_BI_PERCEPTION_ENABLED:
        logger.info("ECISCO_BI_PERCEPTION_ENABLED=False — skipping perception tool registration")
        return

    tool_registry.register_tool(
        name="ask_perception",
        handler=_handle_ask_perception,
        description="Build market snapshot for all C5/C9 products with anomaly detection, "
        "macro regime signals, and alert level assessment.",
        parameters={
            "type": "object",
            "properties": {
                "cutoff_date": {"type": "string", "description": "Optional reference date (default: today)"},
                "product": {"type": "string", "description": "Optional product filter"},
            },
            "required": [],
        },
    )

    logger.info("Registered ask_perception tool")
