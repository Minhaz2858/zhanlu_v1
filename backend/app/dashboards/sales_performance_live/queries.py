"""Generated metric queries for dashboard `sales-performance-live`. DO NOT EDIT.

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
    {"id": 'kpi_revenue', "title": '实时销售额', "type": 'kpi', "sql": "SELECT IFNULL(SUM(FALLAMOUNT),0) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'compare_label': '2026年至今', 'precision': 0, 'unit': '¥'}},
    {"id": 'kpi_orders', "title": '订单量', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT FBILLNO) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'compare_label': '2026年至今', 'precision': 0, 'unit': '单'}},
    {"id": 'kpi_qty', "title": '销量', "type": 'kpi', "sql": "SELECT IFNULL(SUM(FQTY_ORIGIN),0) AS qty FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'compare_label': '2026年至今', 'precision': 1, 'unit': '吨'}},
    {"id": 'kpi_avg_order', "title": '客单价', "type": 'kpi', "sql": "SELECT IFNULL(SUM(FALLAMOUNT),0)/NULLIF(COUNT(DISTINCT FBILLNO),0) AS avg_order FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'compare_label': '单均成交额', 'precision': 0, 'unit': '¥'}},
    {"id": 'trend_revenue', "title": '月度销售趋势', "type": 'area', "sql": "SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS month, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND PLANDATE < '2026-08-01' GROUP BY DATE_FORMAT(PLANDATE, '%Y-%m') ORDER BY month", "options": {'stacked': False, 'x': 'month', 'y': 'revenue', 'delta': 123.07, 'deltaLabel': 'vs prev. period', '_locked': {'current': 348986022.3273, 'previous': 156444806.9482}}},
    {"id": 'trend_daily', "title": '日销售趋势', "type": 'line', "sql": "SELECT DATE(PLANDATE) AS day, SUM(FALLAMOUNT) AS revenue, COUNT(DISTINCT FBILLNO) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' GROUP BY DATE(PLANDATE) ORDER BY day LIMIT 60", "options": {'x': 'day', 'y': 'revenue', 'delta': -13.34, 'deltaLabel': 'vs prev. period', '_locked': {'current': 10396200.0, 'previous': 11996600.0}}},
    {"id": 'split_org', "title": '区域销售占比', "type": 'pie', "sql": "SELECT IFNULL(org_name,'未分配') AS org, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY org_name", "options": {'label': 'org', 'value': 'revenue', 'topItem': {'label': '惠州伊斯科', 'value': 1814050075.0719, 'share_pct': 78.6}}},
    {"id": 'top_materials', "title": 'TOP10 产品销售额', "type": 'bar', "sql": "SELECT material_name AS material, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND material_name IS NOT NULL GROUP BY material_name ORDER BY revenue DESC LIMIT 10", "options": {'horizontal': True, 'x': 'material', 'y': 'revenue', 'topItem': {'label': '异戊二烯', 'value': 509887532.5906, 'share_pct': 22.4}}},
    {"id": 'top_customers', "title": 'TOP10 客户', "type": 'table', "sql": "SELECT CUST_NAME AS customer, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND CUST_NAME IS NOT NULL GROUP BY CUST_NAME ORDER BY revenue DESC LIMIT 10", "options": {'topItem': {'label': '中国石化化工销售有限公司华中分公司', 'value': 294861494.05, 'share_pct': 23.4}}},
    {"id": 'region_trend', "title": '区域月度对比', "type": 'combo', "sql": "SELECT org_name AS org, DATE_FORMAT(PLANDATE, '%Y-%m') AS month, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND org_name IS NOT NULL GROUP BY org_name, DATE_FORMAT(PLANDATE, '%Y-%m') ORDER BY month", "options": {'series': 'org', 'x': 'month', 'y': 'revenue', 'delta': -85.45, 'deltaLabel': 'vs prev. period', '_locked': {'current': 36182700.0, 'previous': 248657330.6588}}},
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
