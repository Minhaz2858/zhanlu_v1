"""Weekly report tool handler for the ecisco_bi_assistant.

Registers:
- `ask_weekly_report` — generate a full weekly report

Gated by: `ECISCO_BI_WEEKLY_REPORT_ENABLED`
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

from .generator import WeeklyReportService


async def _handle_ask_weekly_report(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_weekly_report` — generate C5/C9 weekly market report.

    Args:
        week:    Optional report week (ISO format "2026-W31" or date "2026-08-03")
        products:Optional product IDs to include (default: all 12)
        mode:    "full" (default) | "summary_only"

    Returns:
        {"success": True, "report": {...}, "prompt_text": "..."}
    """
    week = args.get("week") or args.get("report_week") or None
    products = args.get("products") or None
    mode = (args.get("mode") or "full").strip().lower()

    svc = WeeklyReportService(db=db)
    report = svc.generate_report(
        report_week=week, products=products,
        include_charts=(mode != "summary_only"),
        include_forecast=(mode != "summary_only"),
    )

    result: dict[str, Any] = {"success": True, "report": report}

    if mode == "summary_only":
        result["summary"] = report.get("summary")
        result["markdown"] = ""  # Don't send full markdown for summary mode
    else:
        result["markdown"] = report.get("markdown", "")

    return result


def register_weekly_report_tools(tool_registry) -> None:
    """Register ask_weekly_report tool. Gated by ECISCO_BI_WEEKLY_REPORT_ENABLED."""
    if not settings.ECISCO_BI_WEEKLY_REPORT_ENABLED:
        logger.info("ECISCO_BI_WEEKLY_REPORT_ENABLED=False — skipping weekly_report tool registration")
        return

    tool_registry.register_tool(
        name="ask_weekly_report",
        handler=_handle_ask_weekly_report,
        description="Generate a C5/C9 weekly market report with macro context, per-product analysis, "
        "and summary recommendations. Returns structured markdown with all numbers pre-computed.",
        parameters={
            "type": "object",
            "properties": {
                "week": {"type": "string", "description": "Report week (ISO '2026-W31' or date '2026-08-03')"},
                "products": {"type": "array", "items": {"type": "string"}, "description": "Product IDs to include"},
                "mode": {"type": "string", "enum": ["full", "summary_only"], "description": "Output mode"},
            },
            "required": [],
        },
    )

    logger.info("Registered ask_weekly_report tool")
