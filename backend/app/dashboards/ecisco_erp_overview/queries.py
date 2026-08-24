"""Generated metric queries for dashboard `ecisco-erp-overview`. DO NOT EDIT.

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
    {"id": 'kpi_total_revenue', "title": 'Total Revenue (MTD)', "type": 'kpi', "sql": "SELECT SUM(e.F_PAEZ_BHSAMOUNT) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_FORMAT(NOW(), '%Y-%m-01')", "options": {'unit': 'CNY', 'compare_column': 'value'}},
    {"id": 'kpi_total_qty', "title": 'Sales Volume (MTD)', "type": 'kpi', "sql": "SELECT SUM(e.FBASEUNITQTY) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_FORMAT(NOW(), '%Y-%m-01')", "options": {'unit': 'units'}},
    {"id": 'kpi_inventory', "title": 'Current Inventory', "type": 'kpi', "sql": 'SELECT SUM(FBASEQTY) AS value FROM erp_v_stk_inventory', "options": {'unit': 'units'}},
    {"id": 'kpi_top_product', "title": 'Top Product by Revenue', "type": 'kpi', "sql": "SELECT e.FMATERIALID AS label, SUM(e.F_PAEZ_BHSAMOUNT) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_FORMAT(NOW(), '%Y-%m-01') GROUP BY e.FMATERIALID ORDER BY value DESC LIMIT 1", "options": {'unit': 'CNY'}},
    {"id": 'trend_revenue', "title": 'Revenue Trend (6 months)', "type": 'line', "sql": "SELECT DATE_FORMAT(h.FDATE, '%Y-%m') AS label, SUM(e.F_PAEZ_BHSAMOUNT) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(NOW(), INTERVAL 6 MONTH) GROUP BY DATE_FORMAT(h.FDATE, '%Y-%m') ORDER BY label", "options": {'unit': 'CNY', 'show_markers': True}},
    {"id": 'trend_volume', "title": 'Sales Volume Trend (6 months)', "type": 'line', "sql": "SELECT DATE_FORMAT(h.FDATE, '%Y-%m') AS label, SUM(e.FBASEUNITQTY) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(NOW(), INTERVAL 6 MONTH) GROUP BY DATE_FORMAT(h.FDATE, '%Y-%m') ORDER BY label", "options": {'unit': 'units', 'show_markers': True}},
    {"id": 'bar_top_products', "title": 'Top Products by Revenue (MTD)', "type": 'bar', "sql": "SELECT e.FMATERIALID AS label, SUM(e.F_PAEZ_BHSAMOUNT) AS value FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_FORMAT(NOW(), '%Y-%m-01') GROUP BY e.FMATERIALID ORDER BY value DESC LIMIT 10", "options": {'unit': 'CNY'}},
    {"id": 'bar_inventory', "title": 'Inventory by Product (Top 10)', "type": 'bar', "sql": 'SELECT material_name AS label, SUM(FBASEQTY) AS value FROM erp_v_stk_inventory GROUP BY material_name ORDER BY value DESC LIMIT 10', "options": {'unit': 'units'}},
    {"id": 'table_sales_detail', "title": 'Sales Detail (Last 30 days)', "type": 'table', "sql": 'SELECT h.FDATE AS date, e.FMATERIALID AS material, e.FBASEUNITQTY AS qty, e.F_PAEZ_BHSAMOUNT AS revenue FROM erp_t_sal_outstock h JOIN erp_t_sal_outstockentry e ON h.FID = e.FID WHERE h.FDATE >= DATE_SUB(NOW(), INTERVAL 30 DAY) ORDER BY h.FDATE DESC LIMIT 50', "options": {'columns': ['date', 'material', 'qty', 'revenue']}},
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
