"""Generated metric queries for dashboard `ceo-c5c9`. DO NOT EDIT.

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
    {"id": 'kpi_revenue', "title": 'Revenue (excl VAT)', "type": 'kpi', "sql": "SELECT CONCAT('¥', FORMAT(COALESCE(SUM(FAMOUNT),0)/1000000,1), 'M') AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product", "options": {'unit': '¥M', 'delta': True, 'filters': [{'key': 'product', 'label': 'Product', 'column': 'material_name'}], 'sparkline': True}},
    {"id": 'kpi_volume', "title": 'Volume (ordered)', "type": 'kpi', "sql": "SELECT CONCAT(FORMAT(COALESCE(SUM(FQTY_ORIGIN),0),0), ' t') AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product", "options": {'unit': 't', 'delta': True, 'sparkline': True}},
    {"id": 'kpi_price', "title": 'Weighted Avg Price', "type": 'kpi', "sql": "SELECT CONCAT('¥', FORMAT(COALESCE(SUM(FAMOUNT)/NULLIF(SUM(FQTY_ORIGIN),0),0),0), '/t') AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product", "options": {'unit': '¥/t', 'delta': True}},
    {"id": 'kpi_delivery', "title": 'Delivery Rate', "type": 'kpi', "sql": "SELECT CONCAT(FORMAT(COALESCE(SUM(FDELIQTY)/NULLIF(SUM(FQTY_ORIGIN),0),0)*100,1), '%') AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product", "options": {'unit': '%', 'delta': True}},
    {"id": 'kpi_ytd', "title": 'YTD Revenue', "type": 'kpi', "sql": "SELECT CONCAT('¥', FORMAT(COALESCE(SUM(FAMOUNT),0)/1000000,1), 'M') AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND PLANDATE < '2026-08-01' AND :dim_product", "options": {'unit': '¥M', 'delta': True}},
    {"id": 'trend_revenue_12m', "title": 'Revenue Trend (12 months)', "type": 'line', "sql": "SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS period, SUM(FAMOUNT)/1000000 AS revenue_m FROM erp_v_sale_orderentry WHERE PLANDATE >= '2025-09-01' AND :dim_product GROUP BY DATE_FORMAT(PLANDATE, '%Y-%m') ORDER BY period", "options": {'span': 'wide', 'x_key': 'period', 'y_keys': ['revenue_m']}},
    {"id": 'bar_products', "title": 'Top Products by Revenue', "type": 'bar', "sql": "SELECT material_name, SUM(FAMOUNT)/1000000 AS revenue_m FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product GROUP BY material_name ORDER BY revenue_m DESC LIMIT 10", "options": {'x_key': 'material_name', 'y_keys': ['revenue_m']}},
    {"id": 'table_customers', "title": 'Top Customers', "type": 'table', "sql": "SELECT org_name AS customer, SUM(FQTY_ORIGIN) AS volume_t, SUM(FDELIQTY) AS delivered_t, SUM(FAMOUNT)/1000000 AS revenue_m, ROUND(SUM(FAMOUNT)/NULLIF(SUM(FQTY_ORIGIN),0),0) AS avg_price FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01' AND :dim_product GROUP BY org_name ORDER BY revenue_m DESC LIMIT 12", "options": {}},
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
    return [
        {"token": f["key"], "column": f["column"]}
        for f in filters
        if f.get("key") and f.get("column")
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
