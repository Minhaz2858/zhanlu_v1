"""Alert pipeline diagnostics.

One-shot probe that reveals *why* the Market Alerts panel shows zero alerts,
without guessing between "warehouse unreachable", "no price rows", and
"thresholds too strict for stable petrochemical prices".

The function is intentionally self-contained and read-only:
- it consumes the existing `is_edia_reachable()` probe and
  `read_product_history_rows()` bridge (no new infrastructure),
- it reuses `PerceptionService._get_product_price()` so the 7-day-change math
  is identical to what the worker would produce at scan time,
- it reuses `AlertService.effective_thresholds()` so the `would_trigger`
  evaluation matches the live detection thresholds.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.core.mysql_db import is_edia_reachable
from app.services.knowledge_graph.graph import build_c5_c9_graph
from app.services.market_dashboard_service import read_product_history_rows
from app.services.perception.alert_service import effective_thresholds
from app.services.perception.service import PerceptionService

logger = logging.getLogger(__name__)


def _classify_trigger(change_7d_pct: float | None,
                      spike: float, drop: float, high: float) -> tuple[bool, str | None]:
    """Return (would_trigger, trigger_type) for a single change value."""
    if change_7d_pct is None:
        return False, None
    if change_7d_pct >= spike:
        severity = "high" if abs(change_7d_pct) >= high else "medium"
        return True, f"price_spike/{severity}"
    if change_7d_pct <= drop:
        severity = "high" if abs(change_7d_pct) >= high else "medium"
        return True, f"price_drop/{severity}"
    return False, None


def build_alert_diagnostics(db=None) -> dict:
    """Probe the alert pipeline end-to-end and return a structured report.

    ``db`` is accepted for API symmetry with other perception entry points but
    is not currently required (the warehouse bridge opens its own connection).
    """
    spike, drop, high = effective_thresholds()

    warehouse_configured = bool(settings.EDIA_MYSQL_URL)
    warehouse_reachable = is_edia_reachable() if warehouse_configured else False

    graph = build_c5_c9_graph()
    products: list[dict] = []
    triggering_products: list[str] = []

    for node in graph.nodes:
        product_id = node.id

        # Row count via the same bridge the worker uses.
        try:
            rows = read_product_history_rows(product_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("diagnostics: row probe failed for %s: %s", product_id, exc)
            rows = []

        row_count = len(rows) if rows else 0

        # Latest price + 7-day change via the same path the worker uses, so
        # the numbers here match what a scan cycle would actually see.
        price_info = None
        try:
            price_info = PerceptionService._get_product_price(product_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("diagnostics: price probe failed for %s: %s", product_id, exc)

        latest_price = price_info.get("latest_price") if price_info else None
        latest_date = price_info.get("latest_date") if price_info else None
        change_7d_pct = price_info.get("change_7d_pct") if price_info else None

        would_trigger, trigger_type = _classify_trigger(change_7d_pct, spike, drop, high)
        if would_trigger:
            triggering_products.append(product_id)

        products.append({
            "product_id": product_id,
            "name_en": node.name_en,
            "name_cn": node.name_cn,
            "row_count": row_count,
            "latest_price": latest_price,
            "latest_date": latest_date,
            "change_7d_pct": change_7d_pct,
            "would_trigger": would_trigger,
            "trigger_type": trigger_type,
        })

    return {
        "flags": {
            "ecisco_bi_alerts_enabled": settings.ECISCO_BI_ALERTS_ENABLED,
            "alert_digest_enabled": getattr(settings, "ALERT_DIGEST_ENABLED", False),
        },
        "warehouse": {
            "configured": warehouse_configured,
            "reachable": warehouse_reachable,
        },
        "thresholds": {
            "price_spike": spike,
            "price_drop": drop,
            "high_severity": high,
        },
        "effective_org_id": settings.DEFAULT_ORG_ID,
        "products": products,
        "triggering_products": triggering_products,
    }
