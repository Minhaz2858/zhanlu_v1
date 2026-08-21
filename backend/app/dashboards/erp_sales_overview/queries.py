"""Generated metric queries for dashboard `erp-sales-overview`. DO NOT EDIT.

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

DATASOURCE_KB_ID = '54119786-3718-40a5-b1a4-dc589944b2df'

METRICS = [
    {"id": 'total_revenue_30d', "title": 'Revenue (Last 30 Days)', "type": 'kpi', "sql": "SELECT SUM(e.F_PAEZ_BHSAMOUNT) AS total_revenue FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A'", "options": {'unit': 'CNY', 'subtitle': 'vs prior 30 days'}},
    {"id": 'total_volume_30d', "title": 'Sales Volume (Last 30 Days)', "type": 'kpi', "sql": "SELECT SUM(e.FREALQTY) AS total_volume FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A'", "options": {'unit': 'tons', 'subtitle': 'shipped quantity'}},
    {"id": 'avg_exec_price', "title": 'Avg Execution Price', "type": 'kpi', "sql": "SELECT ROUND(SUM(e.F_PAEZ_BHSAMOUNT) / NULLIF(SUM(e.FREALQTY),0), 2) AS avg_price FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A'", "options": {'unit': 'CNY/ton', 'subtitle': 'weighted average'}},
    {"id": 'active_orders', "title": 'Active Orders (30d)', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT h.FID) AS order_count FROM erp_t_sal_outstock h WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A'", "options": {'unit': 'orders', 'subtitle': 'audited shipments'}},
    {"id": 'revenue_trend_6m', "title": 'Monthly Revenue Trend (6 Months)', "type": 'area', "sql": "SELECT DATE_FORMAT(h.FDATE, '%Y-%m') AS month, ROUND(SUM(e.F_PAEZ_BHSAMOUNT),2) AS revenue FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH) AND h.FDOCUMENTSTATUS = 'A' GROUP BY DATE_FORMAT(h.FDATE, '%Y-%m') ORDER BY month", "options": {'x': 'month', 'y': 'revenue', 'unit': 'CNY'}},
    {"id": 'volume_by_product', "title": 'Sales Volume by Product', "type": 'bar', "sql": "SELECT m.F_PAEZ_JC AS product, ROUND(SUM(e.FREALQTY),2) AS volume FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID JOIN erp_t_sal_orderentry o ON e.FSEQ = o.FSEQ JOIN erp_material_master m ON o.FMATERIALID = m.FMATERIALID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A' GROUP BY m.F_PAEZ_JC ORDER BY volume DESC LIMIT 10", "options": {'x': 'product', 'y': 'volume', 'unit': 'tons'}},
    {"id": 'revenue_by_customer', "title": 'Revenue by Customer (Top 10)', "type": 'bar', "sql": "SELECT c.FNAME AS customer, ROUND(SUM(e.F_PAEZ_BHSAMOUNT),2) AS revenue FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID JOIN erp_customer_master c ON h.FCUSTOMERID = c.FCUSTOMERID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) AND h.FDOCUMENTSTATUS = 'A' GROUP BY c.FNAME ORDER BY revenue DESC LIMIT 10", "options": {'x': 'customer', 'y': 'revenue', 'unit': 'CNY'}},
    {"id": 'inventory_snapshot', "title": 'Current Inventory by Product', "type": 'bar', "sql": 'SELECT m.F_PAEZ_JC AS product, ROUND(SUM(i.FBASEQTY),2) AS stock FROM erp_v_stk_inventory i JOIN erp_material_master m ON i.FMATERIALID = m.FMATERIALID GROUP BY m.F_PAEZ_JC ORDER BY stock DESC LIMIT 10', "options": {'x': 'product', 'y': 'stock', 'unit': 'tons'}},
    {"id": 'recent_orders', "title": 'Recent Shipments', "type": 'table', "sql": "SELECT h.FBILLNO AS order_no, c.FNAME AS customer, DATE_FORMAT(h.FDATE,'%Y-%m-%d') AS date, ROUND(e.FREALQTY,2) AS volume, ROUND(e.F_PAEZ_BHSAMOUNT,2) AS revenue FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID JOIN erp_customer_master c ON h.FCUSTOMERID = c.FCUSTOMERID WHERE h.FDATE >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) AND h.FDOCUMENTSTATUS = 'A' ORDER BY h.FDATE DESC LIMIT 15", "options": {'columns': ['order_no', 'customer', 'date', 'volume', 'revenue']}},
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
    return await _run_single_sql(
        db, DATASOURCE_KB_ID, m["sql"], {"filters": filters or {}}, dims,
        DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S,
    )
