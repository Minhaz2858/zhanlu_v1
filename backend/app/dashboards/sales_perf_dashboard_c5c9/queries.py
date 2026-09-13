"""Generated metric queries for dashboard `sales-perf-dashboard-c5c9`. DO NOT EDIT.

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
    {"id": 'kpi_revenue', "title": '总营收（净额）', "type": 'kpi', "sql": "SELECT ROUND(SUM(FAMOUNT),2) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'unit': '¥'}},
    {"id": 'kpi_orders', "title": '订单量', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT FID) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'unit': '单'}},
    {"id": 'kpi_tons', "title": '交付量', "type": 'kpi', "sql": "SELECT ROUND(SUM(FDELIQTY),2) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01'", "options": {'unit': '吨'}},
    {"id": 'kpi_customers', "title": '活跃客户', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT CUST_NAME) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND CUST_NAME IS NOT NULL", "options": {'unit': '家'}},
    {"id": 'trend_revenue_tons', "title": '月度营收与销量趋势（2025-2026）', "type": 'combo', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS ym, COUNT(DISTINCT FID) AS orders, ROUND(SUM(FAMOUNT),2) AS revenue, ROUND(SUM(FDELIQTY),2) AS tons FROM erp_v_sale_orderentry WHERE PLANDATE >= '2025-01-01' GROUP BY ym ORDER BY ym", "options": {'bars': ['revenue'], 'lines': ['tons'], 'span': 'wide', 'x': 'ym', 'delta': -6.6, 'deltaLabel': 'vs prev. period', '_locked': {'current': 184.0, 'previous': 197.0}}},
    {"id": 'region_pie', "title": '区域营收占比（2026 YTD）', "type": 'pie', "sql": "SELECT COALESCE(org_name,'未分区域') AS name, ROUND(SUM(FAMOUNT),2) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY org_name ORDER BY value DESC", "options": {'topItem': {'label': '惠州伊斯科', 'value': 1657921227.56, 'share_pct': 79.4}}},
    {"id": 'region_trend', "title": '区域对比：营收 vs 销量', "type": 'bar', "sql": "SELECT COALESCE(org_name,'未分区域') AS region, ROUND(SUM(FAMOUNT),2) AS revenue, ROUND(SUM(FDELIQTY),2) AS tons, COUNT(DISTINCT FID) AS orders FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY org_name ORDER BY revenue DESC", "options": {'x': 'region', 'topItem': {'label': '惠州伊斯科', 'value': 1657921227.56, 'share_pct': 79.4}}},
    {"id": 'product_bar', "title": '热销产品 Top 12（2026 YTD 营收）', "type": 'bar', "sql": "SELECT material_name AS product, ROUND(SUM(FAMOUNT),2) AS revenue, ROUND(SUM(FDELIQTY),2) AS tons FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' AND material_name IS NOT NULL GROUP BY material_name ORDER BY revenue DESC LIMIT 12", "options": {'x': 'product', 'topItem': {'label': '异戊二烯', 'value': 463655686.14, 'share_pct': 22.5}}},
    {"id": 'recent_orders_table', "title": '最新订单明细（2026-08）', "type": 'table', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m-%d') AS date, material_name AS product, CUST_NAME AS customer, org_name AS region, ROUND(FDELIQTY,2) AS tons, ROUND(FAMOUNT,2) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' ORDER BY PLANDATE DESC LIMIT 50", "options": {'topItem': {'label': '2026-08-28', 'value': 286.33, 'share_pct': 7.8}}},
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
