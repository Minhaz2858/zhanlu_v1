"""Generated metric queries for dashboard `sales-performance-dashboard`. DO NOT EDIT.

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
    {"id": 'total_revenue', "title": 'Total Revenue (Incl. Tax)', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(FALLAMOUNT),0) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {'prefix': '¥', 'format': 'compact'}},
    {"id": 'order_volume', "title": 'Order Volume (Tons)', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(FQTY_ORIGIN),0) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {'suffix': ' t'}},
    {"id": 'order_count', "title": 'Distinct Orders', "type": 'kpi', "sql": 'SELECT COUNT(DISTINCT FID) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {}},
    {"id": 'avg_price', "title": 'Avg Unit Price', "type": 'kpi', "sql": 'SELECT COALESCE(AVG(FPRICE),0) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND FPRICE > 0', "options": {'prefix': '¥'}},
    {"id": 'delivery_rate', "title": 'Delivery Fulfillment Rate', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(FDELIQTY)/NULLIF(SUM(FQTY_ORIGIN),0),0)*100 AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {'suffix': '%'}},
    {"id": 'tax_amount', "title": 'Tax Amount', "type": 'kpi', "sql": 'SELECT COALESCE(SUM(FTAXAMOUNT),0) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)', "options": {'prefix': '¥'}},
    {"id": 'revenue_trend', "title": 'Revenue Trend (30d)', "type": 'area', "sql": 'SELECT DATE(PLANDATE) AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY DATE(PLANDATE) ORDER BY x', "options": {}},
    {"id": 'volume_trend', "title": 'Order Volume Trend (30d)', "type": 'line', "sql": 'SELECT DATE(PLANDATE) AS x, SUM(FQTY_ORIGIN) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) GROUP BY DATE(PLANDATE) ORDER BY x', "options": {}},
    {"id": 'top_products', "title": 'Top Products by Revenue', "type": 'bar', "sql": 'SELECT material_name AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND material_name IS NOT NULL GROUP BY material_name ORDER BY y DESC LIMIT 10', "options": {}},
    {"id": 'product_mix', "title": 'Revenue by Material Group', "type": 'pie', "sql": 'SELECT material_group AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND material_group IS NOT NULL GROUP BY material_group ORDER BY y DESC LIMIT 8', "options": {}},
    {"id": 'customer_split', "title": 'Top Customers by Revenue', "type": 'bar', "sql": 'SELECT CUST_NAME AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND CUST_NAME IS NOT NULL GROUP BY CUST_NAME ORDER BY y DESC LIMIT 10', "options": {}},
    {"id": 'org_split', "title": 'Revenue by Organization', "type": 'pie', "sql": 'SELECT org_name AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND org_name IS NOT NULL GROUP BY org_name ORDER BY y DESC LIMIT 8', "options": {}},
    {"id": 'unit_split', "title": 'Revenue by Unit of Measure', "type": 'bar', "sql": 'SELECT material_unit AS x, SUM(FALLAMOUNT) AS y FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND material_unit IS NOT NULL GROUP BY material_unit ORDER BY y DESC LIMIT 5', "options": {}},
    {"id": 'recent_orders', "title": 'Recent Orders', "type": 'table', "sql": 'SELECT FBILLNO AS bill_no, material_name AS product, CUST_NAME AS customer, FQTY_ORIGIN AS qty, FPRICE AS price, FALLAMOUNT AS amount, FDELIQTY AS delivered, DATE(PLANDATE) AS date FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) ORDER BY PLANDATE DESC LIMIT 50', "options": {}},
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
