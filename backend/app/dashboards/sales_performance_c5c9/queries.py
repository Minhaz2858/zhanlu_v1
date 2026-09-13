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
    {"id": 'kpi_revenue', "title": '销售额（含税）', "type": 'kpi', "sql": 'SELECT IFNULL(SUM(FALLAMOUNT),0) AS value FROM erp_v_sale_orderentry', "options": {'accent': '#2563EB', 'unit': '元'}},
    {"id": 'kpi_qty', "title": '订单量', "type": 'kpi', "sql": 'SELECT COUNT(*) AS value FROM erp_v_sale_orderentry', "options": {'accent': '#10B981', 'unit': '单'}},
    {"id": 'kpi_customers', "title": '客户数', "type": 'kpi', "sql": 'SELECT COUNT(DISTINCT CUST_NAME) AS value FROM erp_v_sale_orderentry', "options": {'accent': '#F59E0B', 'unit': '家'}},
    {"id": 'trend_revenue', "title": '销售趋势（按月）', "type": 'line', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS period, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry GROUP BY DATE_FORMAT(PLANDATE,'%Y-%m') ORDER BY period", "options": {'x': 'period', 'y': 'value', 'delta': -3.1, 'deltaLabel': 'vs prev. period', '_locked': {'current': 340109291.9228, 'previous': 350996881.5073}}},
    {"id": 'trend_qty', "title": '订单量趋势（按月）', "type": 'bar', "sql": "SELECT DATE_FORMAT(PLANDATE,'%Y-%m') AS period, COUNT(*) AS value FROM erp_v_sale_orderentry GROUP BY DATE_FORMAT(PLANDATE,'%Y-%m') ORDER BY period", "options": {'x': 'period', 'y': 'value', 'topItem': {'label': '2023-03', 'value': 305.0, 'share_pct': 2.1}}},
    {"id": 'area_revenue', "title": '区域销售额分布', "type": 'pie', "sql": 'SELECT org_name AS name, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry GROUP BY org_name ORDER BY value DESC', "options": {'x': 'name', 'y': 'value', 'topItem': {'label': '惠州伊斯科', 'value': 17713689986.378975, 'share_pct': 96.2}}},
    {"id": 'area_qty', "title": '区域订单量', "type": 'bar', "sql": 'SELECT org_name AS name, COUNT(*) AS value FROM erp_v_sale_orderentry GROUP BY org_name ORDER BY value DESC', "options": {'x': 'name', 'y': 'value', 'topItem': {'label': '惠州伊斯科', 'value': 14111.0, 'share_pct': 97.7}}},
    {"id": 'top_products', "title": 'TOP 产品销售额', "type": 'bar', "sql": 'SELECT material_name AS name, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry GROUP BY material_name ORDER BY value DESC LIMIT 10', "options": {'x': 'name', 'y': 'value', 'topItem': {'label': '戊烷发泡剂', 'value': 4009750065.7, 'share_pct': 22.3}}},
    {"id": 'top_products_qty', "title": 'TOP 产品明细', "type": 'table', "sql": 'SELECT material_name AS 产品, SUM(FQTY_ORIGIN) AS 数量, SUM(FALLAMOUNT) AS 销售额 FROM erp_v_sale_orderentry GROUP BY material_name ORDER BY 销售额 DESC LIMIT 10', "options": {'pills': {'销售额': 'up'}, 'topItem': {'label': '戊烷发泡剂', 'value': 702780.47, 'share_pct': 25.9}}},
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
