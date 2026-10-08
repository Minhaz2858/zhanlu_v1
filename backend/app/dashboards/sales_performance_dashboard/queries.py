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
    {"id": 'kpi_revenue', "title": '总营收 (¥)', "type": 'kpi', "sql": "SELECT ROUND(SUM(order_amount),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category)", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'kpi_orders', "title": '订单笔数', "type": 'kpi', "sql": "SELECT COUNT(*) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category)", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'kpi_qty', "title": '总销量 (吨)', "type": 'kpi', "sql": "SELECT ROUND(SUM(order_quantity),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category)", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'kpi_customers', "title": '客户数 (家)', "type": 'kpi', "sql": "SELECT COUNT(DISTINCT customer_id) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category)", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'trend_revenue', "title": '营收日趋势 (¥)', "type": 'line', "sql": "SELECT DATE(order_date) AS label, ROUND(SUM(order_amount),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category) GROUP BY DATE(order_date) ORDER BY label", "options": {'span': 'wide', 'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'orders_trend', "title": '订单量日趋势', "type": 'line', "sql": "SELECT DATE(order_date) AS label, COUNT(*) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category) GROUP BY DATE(order_date) ORDER BY label", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'region_pie', "title": '区域营收分布', "type": 'pie', "sql": "SELECT organization AS label, ROUND(SUM(order_amount),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category) GROUP BY organization ORDER BY value DESC", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'category_pie', "title": '品类营收分布', "type": 'pie', "sql": "SELECT product_group AS label, ROUND(SUM(order_amount),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category) GROUP BY product_group ORDER BY value DESC", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
    {"id": 'top_products', "title": '热销产品 Top 10', "type": 'bar', "sql": "SELECT product_name AS label, ROUND(SUM(order_amount),2) AS value FROM v_sale_order WHERE (:dim_org = '' OR organization = :dim_org) AND (:dim_category = '' OR product_group = :dim_category) GROUP BY product_name ORDER BY value DESC LIMIT 10", "options": {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}},
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
