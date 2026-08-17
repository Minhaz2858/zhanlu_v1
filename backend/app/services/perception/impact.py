"""Shared ¥/day business-impact computation for alerts.

Refactored out of ``DigestService`` so both the CEO morning digest and the
smart-alert scan cycle compute impact with a single formula.

Formulas:
  - price alerts:  |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
  - arbitrage:     (spread − freight) × avg_daily_volume_tons

Volume source: ``erp_v_sale_orderentry.FDELIQTY`` (tons), 30d trailing avg,
resolved via ``_PRODUCT_MATERIAL_NAMES[product_id]``.

All functions are pure — they never write to the DB — and degrade gracefully
to ``None`` when ERP volume is unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.mysql_db import get_mysql_engine

logger = logging.getLogger(__name__)


def material_name_params(material_names: list[str]) -> tuple[str, dict]:
    """Build a MySQL IN-clause with named placeholders: (sql, params)."""
    placeholders = []
    params = {}
    for idx, name in enumerate(material_names):
        key = f"name_{idx}"
        placeholders.append(f":{key}")
        params[key] = name
    return ", ".join(placeholders), params


def get_avg_daily_volume(alert: dict) -> float | None:
    """30d average daily shipment volume (tons) for a product.

    Resolves ``product_id`` through the canonical ``_PRODUCT_MATERIAL_NAMES``
    map to Chinese ``material_name`` values, then averages ``FDELIQTY``
    (tons) over the trailing 30 days from ``erp_v_sale_orderentry``.

    Notes:
      - The table lives in the EDIA MySQL warehouse, NOT the app's Postgres
        schema (querying ``self.db`` previously 500'd the digest route).
      - ``erp_product_sales_details.FNAME`` is NULL in the warehouse and its
        rows are stale (2025), so we use the canonical sales-order view.
      - Returns None on failure (graceful degradation).
    """
    product_id = alert.get("product_id") or ""
    product_name = alert.get("product_name") or ""
    if not product_id and not product_name:
        return None

    from app.services.erp.kpi import _PRODUCT_MATERIAL_NAMES

    material_names = [
        n for n in _PRODUCT_MATERIAL_NAMES.get(product_id, [product_name]) if n
    ]
    if not material_names:
        return None

    engine = get_mysql_engine()
    if engine is None:
        logger.info(
            "EDIA MySQL unconfigured — skipping ERP volume for %s",
            product_id or product_name,
        )
        return None

    ph, params = material_name_params(material_names)
    params["start_date"] = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%d")
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    f"""
                    SELECT SUM(COALESCE(FDELIQTY, 0)) AS total_tons
                    FROM erp_v_sale_orderentry
                    WHERE material_name IN ({ph})
                      AND PLANDATE IS NOT NULL
                      AND DATE(PLANDATE) >= :start_date
                    """
                ),
                params,
            ).fetchone()
        total = float(result.total_tons) if result and result.total_tons else None
        return round(total / 30.0, 2) if total else None
    except Exception:
        logger.warning(
            "Failed to query ERP volume for %s",
            product_id or product_name,
            exc_info=True,
        )
        return None


def compute_impact(alert: dict) -> dict:
    """Compute ¥ impact per day for a single alert.

    Price alerts:  |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
    Arbitrage:     (spread − freight) × avg_daily_volume_tons
    """
    avg_volume = get_avg_daily_volume(alert)
    alert_type = alert.get("alert_type", "")
    impact_yuan_per_day = None

    if alert_type in ("price_spike", "price_drop"):
        price = alert.get("price") or alert.get("current_price")
        change_pct = alert.get("change_7d_pct", 0)
        if price and change_pct and avg_volume:
            impact_yuan_per_day = abs(price * change_pct / 100) / 7 * avg_volume

    elif alert_type == "regional_arbitrage":
        # spread = price column for arbitrage alerts; freight_cost from payload/message
        spread = alert.get("price", 0)
        freight = alert.get("freight_cost", 0)
        if avg_volume and spread:
            impact_yuan_per_day = (spread - freight) * avg_volume

    return {
        "impact_yuan_per_day": impact_yuan_per_day,
        "avg_daily_volume_tons": avg_volume,
    }
