"""Generated metric queries for dashboard `financial-overview-c5c9`. DO NOT EDIT.

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
    {"id": 'kpi_revenue_mtd', "title": 'MTD Revenue (Aug 2026)', "type": 'kpi', "sql": "SELECT SUM(FALLAMOUNT) AS total_revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'kpi_revenue_mom', "title": 'Revenue MoM Change', "type": 'kpi', "sql": "SELECT (SELECT SUM(FALLAMOUNT) FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01') AS current_month, (SELECT SUM(FALLAMOUNT) FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND PLANDATE < '2026-08-01') AS prev_month", "options": {}},
    {"id": 'kpi_contracted_qty', "title": 'Contracted Volume (MTD)', "type": 'kpi', "sql": "SELECT SUM(FQTY_ORIGIN) AS contracted_qty FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'kpi_delivery_rate', "title": 'Delivery Rate (MTD)', "type": 'kpi', "sql": "SELECT SUM(FDELIQTY)/SUM(FQTY_ORIGIN) AS delivery_rate FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01'", "options": {}},
    {"id": 'trend_revenue_daily', "title": 'Daily Revenue Trend (Jul–Aug 2026)', "type": 'area', "sql": "SELECT DATE(PLANDATE) AS day, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' GROUP BY day ORDER BY day", "options": {'delta': -13.34, 'deltaLabel': 'vs prev. period', '_locked': {'current': 10396200.0, 'previous': 11996600.0}}},
    {"id": 'trend_monthly_revenue', "title": 'Monthly Revenue (Jan–Aug 2026)', "type": 'bar', "sql": "SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS month, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY month ORDER BY month", "options": {'topItem': {'label': '2026-04', 'value': 404934020.533, 'share_pct': 17.5}}},
    {"id": 'trend_monthly_volume', "title": 'Monthly Contracted Volume (Tons)', "type": 'bar', "sql": "SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS month, SUM(FQTY_ORIGIN) AS volume FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-01-01' GROUP BY month ORDER BY month", "options": {'topItem': {'label': '2026-07', 'value': 50792.033, 'share_pct': 16.4}}},
    {"id": 'breakdown_top_products', "title": 'Top Products by Revenue (Jul–Aug)', "type": 'pie', "sql": "SELECT material_name, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND material_name IS NOT NULL GROUP BY material_name ORDER BY revenue DESC LIMIT 8", "options": {'topItem': {'label': '抽余碳五', 'value': 113456955.41, 'share_pct': 18.3}}},
    {"id": 'breakdown_org_revenue', "title": 'Revenue by Organization (Aug 2026)', "type": 'bar', "sql": "SELECT org_name, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-08-01' AND org_name IS NOT NULL GROUP BY org_name ORDER BY revenue DESC", "options": {'topItem': {'label': '惠州伊斯科', 'value': 248657330.6588, 'share_pct': 87.3}}},
    {"id": 'table_top_products_detail', "title": 'Product Performance Detail (Jul–Aug)', "type": 'table', "sql": "SELECT material_name, SUM(FQTY_ORIGIN) AS contracted_qty, SUM(FDELIQTY) AS delivered_qty, SUM(FALLAMOUNT) AS revenue FROM erp_v_sale_orderentry WHERE PLANDATE >= '2026-07-01' AND material_name IS NOT NULL GROUP BY material_name ORDER BY revenue DESC LIMIT 15", "options": {'topItem': {'label': '戊烷发泡剂', 'value': 15736.5, 'share_pct': 18.4}}},
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
