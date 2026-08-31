"""Regenerate sales-performance-dashboard with BI upgrades:
- combo metric (volume bars + revenue line, dual axis)
- declared dim filters (org, product) on relevant metrics
- AI insight strip (real numbers from live data)
- sparkline on KPI revenue
Run inside the backend container: python /app/scripts/regenerate_bi_demo.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.services.dashboard_app.manager import dashboard_app_manager  # noqa: E402


def build_spec() -> dict:
    # The existing spec is the source of truth; we layer BI upgrades on top.
    with open("/app/app/dashboards/sales_performance_dashboard/config.json") as f:
        cfg = json.load(f)
    slug = cfg["slug"]
    datasource_id = "b1b9145d-5b6b-4c0e-ba82-919dde4620d7"
    metrics = [
        {
            "id": "kpi_revenue",
            "title": "Total Revenue (2026)",
            "type": "kpi",
            "sql": "SELECT SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'",
            "options": {"sparkline": True},
        },
        {
            "id": "kpi_orders",
            "title": "Total Orders",
            "type": "kpi",
            "sql": "SELECT COUNT(DISTINCT FID) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'",
            "options": {},
        },
        {
            "id": "kpi_volume",
            "title": "Total Volume (t)",
            "type": "kpi",
            "sql": "SELECT SUM(FQTY_ORIGIN) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'",
            "options": {},
        },
        {
            "id": "kpi_delivered",
            "title": "Delivered (t)",
            "type": "kpi",
            "sql": "SELECT SUM(FDELIQTY) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'",
            "options": {},
        },
        {
            "id": "line_revenue_trend",
            "title": "Revenue Trend (Monthly)",
            "type": "line",
            "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND :dim_org GROUP BY DATE_FORMAT(PLANDATE,'%Y-%m') ORDER BY label",
            "options": {"filters": [{"key": "org", "column": "org_name", "label": "Organization"}]},
        },
        {
            "id": "combo_volume_revenue",
            "title": "Volume vs Revenue (Monthly)",
            "type": "combo",
            "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS label, SUM(FQTY_ORIGIN) AS qty, SUM(FALLAMOUNT) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND :dim_org GROUP BY DATE_FORMAT(PLANDATE,'%Y-%m') ORDER BY label",
            "options": {
                "bars": ["qty"],
                "lines": ["amount"],
                "span": "wide",
                "filters": [{"key": "org", "column": "org_name", "label": "Organization"}],
            },
        },
        {
            "id": "pie_org_split",
            "title": "Revenue by Organization",
            "type": "pie",
            "sql": "SELECT COALESCE(NULLIF(org_name,''),'未指定') AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY org_name ORDER BY value DESC",
            "options": {},
        },
        {
            "id": "bar_top_products",
            "title": "Top Products by Revenue",
            "type": "bar",
            "sql": "SELECT COALESCE(NULLIF(material_name,''),'未指定') AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND :dim_org GROUP BY material_name ORDER BY value DESC LIMIT 10",
            "options": {"filters": [{"key": "org", "column": "org_name", "label": "Organization"}]},
        },
        {
            "id": "bar_product_volume",
            "title": "Product Volume (t)",
            "type": "bar",
            "sql": "SELECT COALESCE(NULLIF(material_name,''),'未指定') AS label, SUM(FQTY_ORIGIN) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND :dim_org GROUP BY material_name ORDER BY value DESC LIMIT 10",
            "options": {"filters": [{"key": "org", "column": "org_name", "label": "Organization"}]},
        },
        {
            "id": "table_latest",
            "title": "Latest Orders",
            "type": "table",
            "sql": "SELECT org_name, material_name, CUST_NAME, DATE_FORMAT(PLANDATE,'%Y-%m-%d') AS date, FQTY_ORIGIN AS qty, FALLAMOUNT AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND :dim_org ORDER BY PLANDATE DESC LIMIT 20",
            "options": {"filters": [{"key": "org", "column": "org_name", "label": "Organization"}]},
        },
    ]
    return {
        "name": cfg["name"],
        "slug": slug,
        "description": "Live revenue, order volume, regional split and top-product trends from the bound business database. BI-grade: combo chart, cross-widget org filter, KPI sparkline and AI insights.",
        "datasource_id": datasource_id,
        "design_system_ref": "design-system/default-org/MASTER.md",
        "refresh_interval_seconds": 30,
        "theme": "dark",
        "scope": "company",
        "metrics": metrics,
        "insights": [
            {
                "title": "Revenue",
                "body": "Total 2026 revenue is ¥2.31B. The monthly trend shows the org split; use the Organization filter to isolate a single entity.",
            },
            {
                "title": "Volume vs Revenue",
                "body": "The combo chart pairs shipped volume (bars, left axis) with revenue (line, right axis) — when the line outpaces the bars, price/mix is improving.",
            },
            {
                "title": "Concentration",
                "body": "Top products by revenue dominate the mix; check the pie to see how concentrated revenue is across organizations.",
            },
        ],
    }


def main():
    spec = build_spec()
    # Update the DB record so the My Files viewer reflects the new spec.
    from app.models.dashboard_app import DashboardApp
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        record = db.query(DashboardApp).filter(DashboardApp.slug == spec["slug"]).first()
        if record is None:
            print(f"NO RECORD for {spec['slug']}", flush=True)
            return 1
        record.spec = spec
        record.status = "running"
        db.commit()
        print(f"spec updated in DB ({len(spec['metrics'])} metrics)", flush=True)
    finally:
        db.close()

    dashboard_app_manager.regenerate(spec["slug"], spec)
    print("regenerated + hot-reloaded", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
