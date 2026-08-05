"""Ecisco BI ERP KPI Service — product-level inventory + sales queries.

Port of EDIA 5.1.2 backend/src/service/erp_kpi_service.py.
Adapted:
  * EDIA used a global SQLAlchemy `db.engine` — we take a `KnowledgeBase`
    row and go through `connector_factory.get_connector(kb)` so the data
    path is project-scoped (the project's bound Resource is the live
    source, not a hardcoded env var).
  * Original `get_product_kpis(product_id, days=30)` always returned a
    stub. The new signature adds a `kb` keyword; when `kb` is None or the
    connection fails, the service still returns the structured stub so
    callers degrade gracefully.

KPI dimensions:
- Inventory (tons, days)
- 30-day sales volume (tons)
- Plant utilization (%)
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta
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


def _parse_ref_date(ref: Any) -> _date:
    """Coerce a `MAX(DATE(PLANDATE))` return value to a `date`."""
    if isinstance(ref, _date):
        return ref
    if ref is None:
        return _date.today()
    return _date.fromisoformat(str(ref)[:10])


def _stub_result(product_id: str, days: int, reason: str) -> dict:
    """Return the structured stub response used when no live data is available."""
    material_names = _PRODUCT_MATERIAL_NAMES.get(product_id, [product_id])
    return {
        "product_id": product_id,
        "material_names": material_names,
        "period_days": days,
        "stub": True,
        "is_live": False,
        "source": None,
        "kpi": {
            "inventory_tons": None,
            "inventory_days": None,
            "sales_30d_tons": None,
            "utilization_pct": None,
        },
        "adjustment_pct": {"3d": 0.0, "7d": 0.0, "30d": 0.0},
        "summary": reason,
    }


def _material_name_params(
    material_names: list[str],
) -> tuple[str, dict[str, Any]]:
    """Build a SQL IN-clause with named placeholders, returning (sql, params)."""
    placeholders = []
    params: dict[str, Any] = {}
    for idx, name in enumerate(material_names):
        key = f"name_{idx}"
        placeholders.append(f":{key}")
        params[key] = name
    return ", ".join(placeholders), params


class ErpKpiService:
    """Query ERP KPIs for a product from a project-bound KnowledgeBase."""

    def __init__(self, db=None, org_id: str | None = None):
        self.db = db
        self.org_id = org_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_product_kpis(
        self,
        product_id: str,
        days: int = 30,
        kb: Any | None = None,
    ) -> dict:
        """Get inventory, sales, and utilization KPIs for a product.

        Args:
            product_id: One of the keys in `_PRODUCT_MATERIAL_NAMES`
                        (e.g. ``isoprene``, ``cracked_c5``).
            days:       Lookback window for sales/utilization.
            kb:         A `KnowledgeBase` row whose `db_type` is one of
                        the supported connector dialects (mysql,
                        sqlite, postgres, mssql, oracle). When ``None``
                        or when the connector fails, a structured stub
                        is returned so callers degrade gracefully.
        """
        if kb is None:
            return _stub_result(
                product_id, days, reason=f"No KB bound for {product_id}."
            )

        material_names = _PRODUCT_MATERIAL_NAMES.get(product_id, [product_id])

        try:
            from app.services.db.connector_factory import get_connector

            with get_connector(kb) as conn:
                # ---- 0. latest PLANDATE in the sales table ----------------
                ref_date = _date.today()
                try:
                    latest_rows = conn.execute(
                        "SELECT MAX(DATE(PLANDATE)) AS ref_date "
                        "FROM erp_v_sale_orderentry "
                        "WHERE PLANDATE IS NOT NULL"
                    )
                    if latest_rows:
                        ref_date = _parse_ref_date(latest_rows[0].get("ref_date"))
                except Exception as exc:
                    logger.debug("ref_date lookup failed: %s", exc)
                ref_str = ref_date.isoformat()

                # ---- 1. inventory_tons ------------------------------------
                inv_ph, inv_params = _material_name_params(material_names)
                inv_rows = conn.execute(
                    f"SELECT SUM(COALESCE(FBASEQTY, 0)) AS inv_tons "
                    f"FROM erp_v_stk_inventory "
                    f"WHERE material_name IN ({inv_ph})",
                    inv_params,
                )
                inventory_tons = None
                if inv_rows:
                    raw = inv_rows[0].get("inv_tons")
                    if raw is not None:
                        inventory_tons = round(float(raw), 2)

                # ---- 2. sales_30d_tons ------------------------------------
                cutoff_30 = (ref_date - timedelta(days=days)).isoformat()
                sales_ph, sales_params = _material_name_params(material_names)
                sales_params["start_date"] = cutoff_30
                sales_params["end_date"] = ref_str
                sales_rows = conn.execute(
                    f"SELECT SUM(COALESCE(FDELIQTY, 0)) AS vol "
                    f"FROM erp_v_sale_orderentry "
                    f"WHERE material_name IN ({sales_ph}) "
                    f"AND PLANDATE IS NOT NULL "
                    f"AND DATE(PLANDATE) >= :start_date "
                    f"AND DATE(PLANDATE) <= :end_date",
                    sales_params,
                )
                sales_30d_tons = None
                if sales_rows:
                    raw = sales_rows[0].get("vol")
                    if raw is not None:
                        sales_30d_tons = round(float(raw), 2)

                # ---- 3. utilization_pct -----------------------------------
                # Live MySQL has produce_qty / plan_qty as varbinary(0)
                # (unpopulated). Distinguish "0% utilization" from
                # "no data" by returning None when both are zero.
                util_ph, util_params = _material_name_params(material_names)
                util_params["cutoff_date"] = cutoff_30
                util_rows = conn.execute(
                    f"SELECT SUM(COALESCE(produce_qty, 0)) AS produced_qty, "
                    f"       SUM(COALESCE(plan_qty, 0)) AS planned_qty "
                    f"FROM erp_v_sale_orderentry "
                    f"WHERE material_name IN ({util_ph}) "
                    f"AND PLANDATE IS NOT NULL "
                    f"AND DATE(PLANDATE) >= :cutoff_date",
                    util_params,
                )
                utilization_pct = None
                if util_rows:
                    produced = util_rows[0].get("produced_qty") or 0
                    planned = util_rows[0].get("planned_qty") or 0
                    produced_f = float(produced) if produced else 0.0
                    planned_f = float(planned) if planned else 0.0
                    if produced_f > 0 and planned_f > 0:
                        utilization_pct = round(
                            produced_f / planned_f * 100, 2
                        )
        except Exception as exc:
            logger.warning(
                "ErpKpiService live read failed for %s: %s", product_id, exc
            )
            return _stub_result(
                product_id,
                days,
                reason=f"ERP live read failed for {product_id}: {exc}",
            )

        # ---- 4. inventory_days (derived) -------------------------------
        inventory_days = None
        if (
            inventory_tons is not None
            and sales_30d_tons is not None
            and sales_30d_tons > 0
        ):
            inventory_days = round(inventory_tons / (sales_30d_tons / days), 2)

        source_label = _format_source(kb)
        if all(v is None for v in (
            inventory_tons, sales_30d_tons, utilization_pct
        )):
            return _stub_result(
                product_id,
                days,
                reason=(
                    f"Connected to {source_label} but no rows found "
                    f"for {product_id} (material names: {material_names})."
                ),
            )

        return {
            "product_id": product_id,
            "material_names": material_names,
            "period_days": days,
            "stub": False,
            "is_live": True,
            "source": source_label,
            "kpi": {
                "inventory_tons": inventory_tons,
                "inventory_days": inventory_days,
                "sales_30d_tons": sales_30d_tons,
                "utilization_pct": utilization_pct,
            },
            "adjustment_pct": {"3d": 0.0, "7d": 0.0, "30d": 0.0},
            "summary": (
                f"Live ERP read from {source_label} for {product_id} "
                f"(ref date {ref_str})."
            ),
        }

    def get_inventory_trend(self, product_id: str, days: int = 7) -> dict:
        """Get daily inventory trend for the past N days.

        Stub for now — full time-series support is in the next iteration.
        """
        return {
            "product_id": product_id,
            "trend": [{"date": "", "inventory_tons": None} for _ in range(days)],
            "stub": True,
        }


def _format_source(kb: Any) -> str:
    """Human-readable label for which KB the live read came from."""
    db_type = (getattr(kb, "db_type", "") or "").strip().lower()
    host = getattr(kb, "host", None) or ""
    db_name = getattr(kb, "database_name", None) or ""
    name = getattr(kb, "name", None) or ""
    if db_type == "sqlite":
        return f"SQLite ({name or 'local'})"
    if host and db_name:
        return f"{db_type}://{host}/{db_name}"
    if name:
        return f"{db_type} ({name})"
    return db_type or "kb"
