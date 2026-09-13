"""Generated metric queries for dashboard `daily-sales-data-sync`. DO NOT EDIT.

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

DATASOURCE_KB_ID = 'b1b9145d-5b6b-4c0e-ba82-919dde4620d7'

METRICS = [
    {"id": 'kpi_latest_volume', "title": 'Latest Day Volume', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(shipment_quantity),0) AS value FROM erp_product_sales_details WHERE shipment_date = (SELECT MAX(shipment_date) FROM erp_product_sales_details) AND (:dim_date IS NULL OR shipment_date >= :dim_date)', "options": {'delta': 'vs prior day', 'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}, 'unit': 't'}},
    {"id": 'kpi_latest_orders', "title": 'Latest Day Order Lines', "type": 'kpi', "sql": 'SELECT COUNT(*) AS value FROM erp_product_sales_details WHERE shipment_date = (SELECT MAX(shipment_date) FROM erp_product_sales_details) AND (:dim_date IS NULL OR shipment_date >= :dim_date)', "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}},
    {"id": 'kpi_latest_customers', "title": 'Active Customers', "type": 'kpi', "sql": 'SELECT COUNT(DISTINCT partner_name) AS value FROM erp_product_sales_details WHERE shipment_date = (SELECT MAX(shipment_date) FROM erp_product_sales_details) AND (:dim_date IS NULL OR shipment_date >= :dim_date)', "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}},
    {"id": 'trend_volume', "title": 'Daily Shipment Volume', "type": 'line', "sql": 'SELECT shipment_date AS label, COALESCE(SUM(shipment_quantity),0) AS value FROM erp_product_sales_details WHERE shipment_date >= DATE_SUB((SELECT MAX(shipment_date) FROM erp_product_sales_details), INTERVAL 14 DAY) AND (:dim_date IS NULL OR shipment_date >= :dim_date) GROUP BY shipment_date ORDER BY shipment_date', "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}, 'span': 'wide', 'unit': 't'}},
    {"id": 'bar_order_activity', "title": 'Daily Order-Line Activity', "type": 'bar', "sql": 'SELECT shipment_date AS label, COUNT(*) AS value FROM erp_product_sales_details WHERE shipment_date >= DATE_SUB((SELECT MAX(shipment_date) FROM erp_product_sales_details), INTERVAL 14 DAY) AND (:dim_date IS NULL OR shipment_date >= :dim_date) GROUP BY shipment_date ORDER BY shipment_date', "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}},
    {"id": 'pie_customers', "title": 'Customer Share (Latest Day)', "type": 'pie', "sql": 'SELECT partner_name AS label, COALESCE(SUM(shipment_quantity),0) AS value FROM erp_product_sales_details WHERE shipment_date = (SELECT MAX(shipment_date) FROM erp_product_sales_details) AND (:dim_date IS NULL OR shipment_date >= :dim_date) GROUP BY partner_name ORDER BY value DESC LIMIT 6', "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}},
    {"id": 'table_recent', "title": 'Recent Shipment Days', "type": 'table', "sql": "SELECT shipment_date AS 'Shipment Date', COUNT(*) AS 'Order Lines', COALESCE(SUM(shipment_quantity),0) AS 'Volume (t)', COUNT(DISTINCT partner_name) AS 'Customers' FROM erp_product_sales_details WHERE shipment_date >= DATE_SUB((SELECT MAX(shipment_date) FROM erp_product_sales_details), INTERVAL 14 DAY) AND (:dim_date IS NULL OR shipment_date >= :dim_date) GROUP BY shipment_date ORDER BY shipment_date DESC", "options": {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}},
]


def metric_dimensions(m: dict) -> list[dict]:
    """Declared filter tokens for one metric: ``[{token, column}, ...]``.

    Only filters declared in the metric spec's ``options.filters`` are accepted
    (e.g. ``[{"key": "product", "column": "FNAME"}]``). The metric SQL then uses
    the safe ``:dim_product`` token. A URL-supplied key can NEVER become a SQL
    column name — undeclared keys are ignored and unknown tokens raise before
    execution. Values are injected through ``render_widget_sql``'s ``_literal``
    escaping (never string concatenation), so query-string input cannot break
    out of the generated SQL.
    """
    filters = (m.get("options") or {}).get("filters") or []
    # ``options.filters`` may be a LIST of URL-keyed dimension specs
    # ([{"key": "product", "column": "FNAME"}]) OR a DICT of object filters
    # ({"date": {"column": "shipment_date", "op": ">=", "value": ":dim_date"}})
    # emitted by the dashboard generator. Iterating a dict yields bare string
    # keys and crashed with ``AttributeError: 'str' object has no attribute
    # 'get'`` — observed 2026-09-03: every daily-sales-data-sync metric poll
    # logged a traceback on each 30 s refresh. Normalize dict filters: the
    # dict key IS the declared token name (e.g. "date" -> :dim_date).
    if isinstance(filters, dict):
        filters = [
            {"key": name, "column": spec.get("column") or ""}
            for name, spec in filters.items()
            if isinstance(spec, dict)
        ]
    return [
        {"token": f["key"], "column": f["column"]}
        for f in filters
        if isinstance(f, dict) and f.get("key") and f.get("column")
    ]


async def run_metric(db, metric_id: str, filters: dict | None = None) -> dict:
    """Render + validate + execute ONE metric. Returns {columns, rows, error, truncated}.

    ``filters`` is the (optional) query-string map from the dashboard URL, e.g.
    ``{"product": "乙二醇"}``. Only declared ``:dim_*`` tokens are substituted;
    everything else stays inert, and every value is SQL-literal-escaped.
    """
    m = next((x for x in METRICS if x["id"] == metric_id), None)
    if m is None:
        return {"columns": [], "rows": [], "error": f"unknown metric {metric_id}", "truncated": False}
    try:
        validate_widget_sql(m["sql"])
    except ValueError as exc:
        return {"columns": [], "rows": [], "error": str(exc), "truncated": False}
    dims = metric_dimensions(m)
    # Query-string params arrive FLAT from the router ({product: x, from: y, to: z}).
    # render_widget_sql expects date-window keys at the TOP level and dims nested
    # under ``filters`` — lift from/to/date so :from/:to/:date tokens actually render.
    qp = filters or {}
    params = {"filters": qp}
    if qp.get("from"):
        params["from"] = qp["from"]
    if qp.get("to"):
        params["to"] = qp["to"]
    if qp.get("date"):
        params["date"] = qp["date"]
    return await _run_single_sql(
        db, DATASOURCE_KB_ID, m["sql"], params, dims,
        DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S,
    )
