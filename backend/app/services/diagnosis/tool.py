"""Diagnosis tool handler for the ecisco_bi_assistant.

Registers:
- `ask_diagnosis` — causal chain analysis for C5/C9 products

Gated by: `ECISCO_BI_PERCEPTION_ENABLED`
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .service import DiagnosisService


async def _handle_ask_diagnosis(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_diagnosis` — causal chain analysis on knowledge graph.

    Args:
        product:   Product ID or name (required)
        snapshot:  Perception snapshot dict (optional; auto-fetched if missing)
        question:  Natural-language question (ignored; kept for compat)

    Returns:
        {"success": True, "diagnosis": {...}}
    """
    from app.services.knowledge_graph.resolver import resolve_product_id

    product: str = (args.get("product") or args.get("product_id") or "").strip()
    if not product:
        return {"success": False, "error": "Missing required arg: product"}

    product_id = resolve_product_id(product)
    if not product_id:
        return {"success": False, "error": f"Could not resolve product '{product}'."}

    # Get perception snapshot (auto-fetch or use provided)
    snapshot = args.get("snapshot") or args.get("perception") or {}
    if not snapshot.get("products"):
        try:
            from app.services.perception.service import PerceptionService
            svc = PerceptionService(db=db)
            snapshot = svc.build_snapshot()
        except Exception as exc:
            logger.warning("Could not build perception snapshot: %s", exc)
            snapshot = {"products": {}}

    svc = DiagnosisService(db=db)
    result = svc.diagnose(product_id, snapshot, args.get("intelligence"))
    prompt_text = DiagnosisService.to_prompt_text(result)

    return {
        "success": True,
        "diagnosis": result,
        "prompt_text": prompt_text,
    }


def register_diagnosis_tools(tool_registry) -> None:
    """Register ask_diagnosis tool. Gated by ECISCO_BI_PERCEPTION_ENABLED."""
    if not settings.ECISCO_BI_PERCEPTION_ENABLED:
        logger.info("ECISCO_BI_PERCEPTION_ENABLED=False — skipping diagnosis tool registration")
        return

    tool_registry.register_tool(
        name="ask_diagnosis",
        handler=_handle_ask_diagnosis,
        description="Causal chain analysis for C5/C9 products. Traverses the knowledge graph "
        "upstream/downstream, ranks drivers by 7d price change with confidence scores, "
        "determines market regime (bullish/bearish/neutral), and propagates signals.",
        parameters={
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product ID or name to diagnose"},
                "snapshot": {"type": "object", "description": "Optional perception snapshot"},
            },
            "required": ["product"],
        },
    )

    logger.info("Registered ask_diagnosis tool")
