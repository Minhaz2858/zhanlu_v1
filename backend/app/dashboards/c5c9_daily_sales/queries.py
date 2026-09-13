"""Generated metric queries for dashboard `c5c9-daily-sales`. DO NOT EDIT.

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
    {"id": 'kpi_value', "title": 'Order Value (Last 30d)', "type": 'kpi', "sql": 'SELECT ROUND(SUM(FAMOUNT),2) AS value FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY', "options": {}},
    {"id": 'kpi_volume', "title": 'Order Volume (t)', "type": 'kpi', "sql": 'SELECT ROUND(SUM(FQTY_ORIGIN),1) AS volume FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY', "options": {}},
    {"id": 'kpi_lines', "title": 'Order Lines', "type": 'kpi', "sql": 'SELECT COUNT(*) AS lines FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY', "options": {}},
    {"id": 'trend_value', "title": 'Daily Order Value & Volume', "type": 'combo', "sql": 'SELECT DATE(PLANDATE) AS day, ROUND(SUM(FQTY_ORIGIN),1) AS qty, ROUND(SUM(FAMOUNT),0) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY GROUP BY DATE(PLANDATE) ORDER BY day', "options": {'bars': ['qty'], 'span': 'wide', 'delta': -85.73, 'lines': ['amount'], '_locked': {'current': 52.8, 'previous': 370.0}, 'deltaLabel': 'vs prev. period'}},
    {"id": 'top_products', "title": 'Top Materials by Value', "type": 'bar', "sql": 'SELECT FNAME AS material, ROUND(SUM(FAMOUNT),0) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY GROUP BY FNAME ORDER BY amount DESC LIMIT 8', "options": {}},
    {"id": 'top_customers', "title": 'Top Customers by Value', "type": 'bar', "sql": 'SELECT partner_name AS customer, ROUND(SUM(FAMOUNT),0) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY GROUP BY partner_name ORDER BY amount DESC LIMIT 8', "options": {}},
    {"id": 'composition', "title": 'Value Share by Material', "type": 'pie', "sql": 'SELECT FNAME AS material, ROUND(SUM(FAMOUNT),0) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 30 DAY GROUP BY FNAME ORDER BY amount DESC LIMIT 6', "options": {}},
    {"id": 'detail_table', "title": 'Recent Order Detail', "type": 'table', "sql": 'SELECT DATE(PLANDATE) AS order_date, FNAME AS material, partner_name AS customer, FQTY_ORIGIN AS qty, ROUND(FAMOUNT,0) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= CURDATE() - INTERVAL 14 DAY ORDER BY PLANDATE DESC LIMIT 100', "options": {}},
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
