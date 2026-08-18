"""Generated metric queries for dashboard `e2e-erp-sales-smoke`. DO NOT EDIT.

Executes each metric's read-only SELECT against the bound datasource KB
through the same safety-guarded execution path as the legacy dashboard
widgets (``_run_single_sql``: token render + read-only validation + row
cap + timeout). Errors are returned in the payload, never raised.
"""
from app.services.dashboard_query import (
    DEFAULT_MAX_ROWS,
    DEFAULT_TIMEOUT_S,
    _run_single_sql,
    validate_widget_sql,
)

DATASOURCE_KB_ID = '54119786-3718-40a5-b1a4-dc589944b2df'

METRICS = [
    {"id": 'kpi_total_qty', "title": 'Total Shipment Qty', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(shipment_quantity),0) AS total_qty FROM erp_product_sales_details', "options": {'value_column': 'total_qty', 'format': 'integer'}},
    {"id": 'line_monthly_trend', "title": 'Monthly Shipment Trend', "type": 'line', "sql": "SELECT DATE_FORMAT(shipment_date, '%Y-%m') AS month, SUM(shipment_quantity) AS qty FROM erp_product_sales_details GROUP BY month ORDER BY month DESC LIMIT 12", "options": {'x_column': 'month', 'y_column': 'qty'}},
    {"id": 'table_recent', "title": 'Recent Shipments', "type": 'table', "sql": 'SELECT shipment_date, FNAME, partner_name, shipment_quantity, contract_price FROM erp_product_sales_details ORDER BY shipment_date DESC LIMIT 50', "options": {}},
]


async def run_metric(db, metric_id: str) -> dict:
    """Render + validate + execute ONE metric. Returns {columns, rows, error, truncated}."""
    m = next((x for x in METRICS if x["id"] == metric_id), None)
    if m is None:
        return {"columns": [], "rows": [], "error": f"unknown metric {metric_id}", "truncated": False}
    try:
        validate_widget_sql(m["sql"])
    except ValueError as exc:
        return {"columns": [], "rows": [], "error": str(exc), "truncated": False}
    return await _run_single_sql(
        db, DATASOURCE_KB_ID, m["sql"], {}, [], DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S
    )
