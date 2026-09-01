"""Generated metric queries for dashboard `editorial-demo-001`. DO NOT EDIT.

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
    {"id": 'kpi_revenue', "title": 'Total Revenue', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(revenue),0) AS value FROM customers', "options": {}},
    {"id": 'kpi_products', "title": 'Products', "type": 'kpi', "sql": 'SELECT COUNT(*) AS value FROM products', "options": {}},
    {"id": 'kpi_orders', "title": 'Orders', "type": 'kpi', "sql": 'SELECT COUNT(*) AS value FROM orders', "options": {}},
    {"id": 'bar_customers', "title": 'Top Customers by Revenue', "type": 'bar', "sql": 'SELECT name AS label, revenue AS value FROM customers ORDER BY revenue DESC LIMIT 5', "options": {'topItem': {'label': 'Acme Corp', 'value': 1250000.0, 'share_pct': 27.3}}},
    {"id": 'bar_products', "title": 'Top Products by Volume', "type": 'bar', "sql": 'SELECT p.name AS label, SUM(o.qty) AS value FROM orders o JOIN products p ON p.id = o.product_id GROUP BY p.name ORDER BY value DESC LIMIT 5', "options": {'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 26.6}}},
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
