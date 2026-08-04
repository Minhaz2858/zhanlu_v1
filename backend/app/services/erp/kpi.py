"""Ecisco BI ERP KPI Service — product-level inventory + sales queries.

Port of EDIA 5.1.2 backend/src/service/erp_kpi_service.py.
Adapted: EDIA SQLite → Zhanlu PostgreSQL, raw SQL → async SQLAlchemy (future).

KPI dimensions:
- Inventory (tons, days)
- 30-day sales volume (tons)
- Plant utilization (%)
- Price trend vs. ERP moving average
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_PRODUCT_MATERIAL_NAMES: dict[str, list[str]] = {
    "isoprene": ["异戊二烯"],
    "piperylene": ["间戊二烯", "顺式间戊二烯产品"],
    "dcpd": ["双环戊二烯"],
    "cracked_c5": ["工业用裂解碳五", "裂解碳五"],
    "cracked_c9": ["工业用裂解碳九"],
    "raffinate_c5": ["抽余碳五"],
    "sis": ["SIS", "热塑性橡胶"],
    "c5_resin": ["碳五石油树脂"],
    "blowing_agent": ["戊烷发泡剂"],
    "styrene": ["苯乙烯"],
}


class ErpKpiService:
    """Query ERP KPIs for a product."""

    def __init__(self, db=None, org_id: str | None = None):
        self.db = db
        self.org_id = org_id

    def get_product_kpis(self, product_id: str, days: int = 30) -> dict:
        """Get inventory, sales, and utilization KPIs for a product.

        Currently returns structured stub — real ERP queries will be added
        when the ERP MySQL data source (10.10.10.49) is wired.
        """
        material_names = _PRODUCT_MATERIAL_NAMES.get(product_id, [product_id])

        return {
            "product_id": product_id,
            "material_names": material_names,
            "period_days": days,
            "stub": True,
            "kpi": {
                "inventory_tons": None,
                "inventory_days": None,
                "sales_30d_tons": None,
                "utilization_pct": None,
            },
            "adjustment_pct": {"3d": 0.0, "7d": 0.0, "30d": 0.0},
            "summary": f"ERP data not yet connected for {product_id}.",
        }

    def get_inventory_trend(self, product_id: str, days: int = 7) -> dict:
        """Get daily inventory trend for the past N days."""
        return {
            "product_id": product_id,
            "trend": [{"date": "", "inventory_tons": None} for _ in range(days)],
            "stub": True,
        }
