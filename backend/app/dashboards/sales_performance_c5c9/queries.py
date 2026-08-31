"""Generated metric queries for dashboard `sales-performance-c5c9`. DO NOT EDIT.

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
    {"id": 'total_revenue', "title": 'Total Revenue (Aug MTD)', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'total_orders', "title": 'Order Volume (Aug MTD)', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT FID) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'avg_order_value', "title": 'Avg Order Value (¥)', "type": 'kpi', "sql": "SELECT ROUND(SUM(FALLAMOUNT)/COUNT(DISTINCT FID), 2) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'delivery_rate', "title": 'Delivery Completion %', "type": 'kpi', "sql": "SELECT ROUND(SUM(FDELIQTY)/SUM(FQTY_ORIGIN)*100, 1) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'mom_growth', "title": 'Revenue MoM Growth %', "type": 'kpi', "sql": "SELECT ROUND(((SELECT SUM(FALLAMOUNT) FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' AND PLANDATE < '2026-08-29') - (SELECT SUM(FALLAMOUNT) FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-07-29')) / (SELECT SUM(FALLAMOUNT) FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-07-29') * 100, 1) AS value", "options": {}},
    {"id": 'revenue_trend', "title": 'Daily Revenue Trend', "type": 'area', "sql": "SELECT DATE(PLANDATE) AS d, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' GROUP BY DATE(PLANDATE) ORDER BY d", "options": {'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}},
    {"id": 'delivery_trend', "title": 'Daily Delivery Quantity', "type": 'line', "sql": "SELECT DATE(PLANDATE) AS d, SUM(FDELIQTY) AS delivered FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' GROUP BY DATE(PLANDATE) ORDER BY d", "options": {'delta': 1077.29, 'deltaLabel': 'vs prev. period', '_locked': {'current': 329.64, 'previous': 28.0}}},
    {"id": 'order_trend', "title": 'Daily Order Volume', "type": 'bar', "sql": "SELECT DATE(PLANDATE) AS d, COUNT(DISTINCT FID) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' GROUP BY DATE(PLANDATE) ORDER BY d", "options": {'topItem': {'label': '2026-08-13', 'value': 19.0, 'share_pct': 11.6}}},
    {"id": 'regional_split', "title": 'Regional Revenue Split', "type": 'pie', "sql": "SELECT org_name AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' GROUP BY org_name ORDER BY value DESC", "options": {'topItem': {'label': '惠州伊斯科', 'value': 270624538.4828, 'share_pct': 83.6}}},
    {"id": 'top_products', "title": 'Top Products by Revenue', "type": 'bar', "sql": "SELECT material_name AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' GROUP BY material_name ORDER BY value DESC LIMIT 8", "options": {'topItem': {'label': '抽余碳五', 'value': 68360034.51, 'share_pct': 21.3}}},
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
