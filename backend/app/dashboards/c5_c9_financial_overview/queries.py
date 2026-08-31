"""Generated metric queries for dashboard `c5_c9_financial_overview`. DO NOT EDIT.

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
    {"id": 'revenue_total', "title": 'Total Revenue (Tax-Incl)', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(FALLAMOUNT),0) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {}},
    {"id": 'revenue_30d_trend', "title": 'Revenue Trend (30 Days)', "type": 'line', "sql": 'SELECT DATE(PLANDATE) AS d, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY DATE(PLANDATE) ORDER BY d', "options": {'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}},
    {"id": 'revenue_by_product', "title": 'Revenue by Product', "type": 'bar', "sql": 'SELECT material_name AS label, SUM(FALLAMOUNT) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY material_name ORDER BY value DESC LIMIT 10', "options": {'topItem': {'label': '异戊二烯', 'value': 88471606.2878, 'share_pct': 21.7}}},
    {"id": 'top_customers', "title": 'Top Customers (30D)', "type": 'table', "sql": 'SELECT CUST_NAME AS customer, SUM(FQTY_ORIGIN) AS qty, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY CUST_NAME ORDER BY revenue DESC LIMIT 10', "options": {'topItem': {'label': '中海壳牌石油化工有限公司', 'value': 8649.0, 'share_pct': 25.5}}},
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
