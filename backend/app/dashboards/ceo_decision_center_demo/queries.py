"""Generated metric queries for dashboard `ceo-decision-center-demo`. DO NOT EDIT.

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

DATASOURCE_KB_ID = 'kb-demo-001'

METRICS = [
    {"id": 'kpi_rev', "title": '客户总收入', "type": 'kpi', "sql": 'SELECT ROUND(SUM(revenue), 0) AS revenue FROM customers', "options": {'unit': '¥', 'accent': '#22C55E', 'sub': '8 客户 · 4 区域'}},
    {"id": 'kpi_qty', "title": '总销量', "type": 'kpi', "sql": 'SELECT SUM(qty) AS qty FROM orders', "options": {'unit': '件', 'accent': '#3B82F6', 'sub': '16 订单 · 平均 50 件/单'}},
    {"id": 'kpi_margin', "title": '综合毛利率', "type": 'kpi', "sql": 'SELECT ROUND(AVG(margin) * 100, 1) AS margin FROM orders', "options": {'unit': '%', 'accent': '#F59E0B', 'delta_tone': 'warn', 'sub': '目标 16% · 底线 10%'}},
    {"id": 'kpi_orders', "title": '订单数', "type": 'kpi', "sql": 'SELECT COUNT(*) AS orders FROM orders', "options": {'unit': '单', 'accent': '#8B5CF6', 'sub': '8 产品 · 8 客户'}},
    {"id": 'kpi_top', "title": '最大客户 Acme', "type": 'kpi', "sql": 'SELECT MAX(revenue) AS top_rev FROM customers', "options": {'unit': '¥', 'accent': '#14B8A6', 'sub': '占客户收入 19.4%'}},
    {"id": 'signal_table', "title": '产品信号一览', "type": 'table', "sql": "SELECT p.name AS product, SUM(o.qty) AS qty, ROUND(AVG(o.margin) * 100, 1) AS margin_pct, ROUND(SUM(o.qty) * 100.0 / (SELECT SUM(qty) FROM orders), 1) AS share_pct, CASE WHEN AVG(o.margin) >= 0.15 THEN 'good' WHEN AVG(o.margin) <= 0.10 THEN 'bad' ELSE 'warn' END AS tone, CASE WHEN AVG(o.margin) >= 0.15 THEN '上调' WHEN AVG(o.margin) <= 0.10 THEN '下调' ELSE '关注' END AS action FROM orders o JOIN products p ON p.id = o.product_id GROUP BY p.name ORDER BY qty DESC", "options": {'pills': {'column': 'action', 'map': {'上调': 'up', '下调': 'down', '关注': 'warn'}}, 'row_tone_column': 'tone', 'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 21.2}}},
    {"id": 'prod_qty', "title": '产品销量 (件)', "type": 'bar', "sql": 'SELECT p.name AS product, SUM(o.qty) AS qty FROM orders o JOIN products p ON p.id = o.product_id GROUP BY p.name ORDER BY qty DESC', "options": {'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 21.2}}},
    {"id": 'prod_margin', "title": '产品毛利率 (%)', "type": 'bar', "sql": 'SELECT p.name AS product, ROUND(AVG(o.margin) * 100, 1) AS margin_pct FROM orders o JOIN products p ON p.id = o.product_id GROUP BY p.name ORDER BY margin_pct DESC', "options": {'topItem': {'label': 'Gizmo D', 'value': 17.0, 'share_pct': 15.4}}},
    {"id": 'region_qty', "title": '区域销量 (件)', "type": 'bar', "sql": 'SELECT region, SUM(qty) AS qty FROM orders GROUP BY region ORDER BY qty DESC', "options": {'topItem': {'label': 'North', 'value': 428.0, 'share_pct': 53.3}}},
    {"id": 'region_pie', "title": '区域销量占比', "type": 'pie', "sql": 'SELECT region AS name, SUM(qty) AS value FROM orders GROUP BY region', "options": {'topItem': {'label': 'North', 'value': 428.0, 'share_pct': 53.3}}},
    {"id": 'customer_rev', "title": '客户收入 (¥)', "type": 'bar', "sql": 'SELECT name AS customer, revenue FROM customers ORDER BY revenue DESC', "options": {'topItem': {'label': 'Acme Corp', 'value': 1250000.0, 'share_pct': 19.5}}},
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
