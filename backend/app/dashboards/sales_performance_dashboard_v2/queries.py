"""Generated metric queries for dashboard `sales-performance-dashboard-v2`. DO NOT EDIT.

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
    {"id": 'total_revenue', "title": 'Total Revenue (2026)', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS total_revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {}},
    {"id": 'total_orders', "title": 'Total Orders', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT FID) AS total_orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {}},
    {"id": 'total_volume', "title": 'Total Volume (t)', "type": 'kpi', "sql": "SELECT SUM(FQTY_ORIGIN) AS total_volume FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {}},
    {"id": 'delivered_volume', "title": 'Delivered (t)', "type": 'kpi', "sql": "SELECT SUM(FDELIQTY) AS delivered_volume FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {}},
    {"id": 'revenue_trend', "title": 'Revenue Trend (Monthly)', "type": 'line', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS ym, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY ym ORDER BY ym", "options": {'delta': -13.52, '_locked': {'current': 301796030.6588, 'previous': 348986022.3273}, 'deltaLabel': 'vs prev. period'}},
    {"id": 'order_trend', "title": 'Order Volume Trend', "type": 'line', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS ym, COUNT(DISTINCT FID) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY ym ORDER BY ym", "options": {'delta': -20.3, '_locked': {'current': 157.0, 'previous': 197.0}, 'deltaLabel': 'vs prev. period'}},
    {"id": 'org_split', "title": 'Revenue by Organization', "type": 'pie', "sql": "SELECT COALESCE(NULLIF(org_name,''),'未指定') AS org, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY org ORDER BY revenue DESC", "options": {'topItem': {'label': '惠州伊斯科', 'value': 1814050075.0719, 'share_pct': 78.6}}},
    {"id": 'top_products', "title": 'Top Products by Revenue', "type": 'bar', "sql": "SELECT COALESCE(NULLIF(material_name,''),'未指定') AS material, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY material ORDER BY revenue DESC LIMIT 10", "options": {'topItem': {'label': '异戊二烯', 'value': 509887532.5906, 'share_pct': 22.2}}},
    {"id": 'product_volume', "title": 'Product Volume (t)', "type": 'bar', "sql": "SELECT COALESCE(NULLIF(material_name,''),'未指定') AS material, SUM(FQTY_ORIGIN) AS qty FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY material ORDER BY qty DESC LIMIT 10", "options": {'topItem': {'label': '工业用裂解碳五', 'value': 80138.06, 'share_pct': 26.0}}},
    {"id": 'latest_orders', "title": 'Latest Orders', "type": 'table', "sql": "SELECT PLANDATE AS date, COALESCE(NULLIF(material_name,''),'未指定') AS product, COALESCE(NULLIF(org_name,''),'未指定') AS org, FQTY_ORIGIN AS qty, FALLAMOUNT AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' ORDER BY PLANDATE DESC LIMIT 15", "options": {'topItem': {'label': '2026-08-26T09:53:32', 'value': 280.0, 'share_pct': 22.0}}},
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
