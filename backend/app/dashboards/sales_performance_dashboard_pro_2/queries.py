"""Generated metric queries for dashboard `sales-performance-dashboard-pro-2`. DO NOT EDIT.

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
    {"id": 'kpi_revenue_30d', "title": 'Total Revenue — Last 30 Days', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30'", "options": {'unit': '¥', 'format': 'currency', 'caption': 'Last 30 days (Jul 30 – Aug 28)'}},
    {"id": 'kpi_revenue_15d', "title": 'Total Revenue — Last 15 Days', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-14'", "options": {'unit': '¥', 'format': 'currency', 'caption': 'Last 15 days (Aug 14 – Aug 28)'}},
    {"id": 'kpi_revenue_7d', "title": 'Total Revenue — Last 7 Days', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-22'", "options": {'unit': '¥', 'format': 'currency', 'caption': 'Last 7 days (Aug 22 – Aug 28)'}},
    {"id": 'kpi_orders_30d', "title": 'Order Volume — 30 Days', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT FID) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30'", "options": {'format': 'number', 'caption': 'Distinct orders, last 30 days'}},
    {"id": 'trend_daily', "title": 'Daily Revenue — Trailing 30 Days', "type": 'area', "sql": "SELECT DATE(PLANDATE) AS d, SUM(FALLAMOUNT) AS revenue, COUNT(DISTINCT FID) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30' GROUP BY DATE(PLANDATE) ORDER BY d", "options": {'x': 'd', 'y': 'revenue', 'area': True, 'unit': '¥', 'color': '#22C55E', 'delta': 8572.57, 'format': 'currency', '_locked': {'current': 21716800.0, 'previous': 250407.824}, 'caption': 'Daily revenue, Jul 30 – Aug 28', 'deltaLabel': 'vs prev. period'}},
    {"id": 'split_region', "title": 'Regional Split — Last 30 Days', "type": 'pie', "sql": "SELECT org_name, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30' GROUP BY org_name ORDER BY revenue DESC", "options": {'unit': '¥', 'label': 'org_name', 'value': 'revenue', 'format': 'currency', 'caption': 'Revenue by operating entity, last 30 days', 'topItem': {'label': '惠州伊斯科', 'value': 299925731.4948, 'share_pct': 79.8}}},
    {"id": 'top_products', "title": 'Top Products — Last 30 Days', "type": 'bar', "sql": "SELECT material_name, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30' GROUP BY material_name ORDER BY revenue DESC LIMIT 10", "options": {'x': 'material_name', 'y': 'revenue', 'unit': '¥', 'color': '#22C55E', 'format': 'currency', 'caption': 'Top 10 products, last 30 days', 'topItem': {'label': '戊烷发泡剂', 'value': 77676050.0, 'share_pct': 20.7}, 'horizontal': True}},
    {"id": 'top_customers', "title": 'Top Customers — Last 30 Days', "type": 'bar', "sql": "SELECT CUST_NAME, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-30' GROUP BY CUST_NAME ORDER BY revenue DESC LIMIT 8", "options": {'x': 'CUST_NAME', 'y': 'revenue', 'unit': '¥', 'color': '#38BDF8', 'format': 'currency', 'caption': 'Top 8 customers, last 30 days', 'topItem': {'label': '中海壳牌石油化工有限公司', 'value': 68360034.51, 'share_pct': 30.5}, 'horizontal': True}},
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
