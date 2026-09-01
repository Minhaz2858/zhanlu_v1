"""Generated metric queries for dashboard `c5c9-live-market-reference`. DO NOT EDIT.

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
    {"id": 'kpi_c5', "title": '裂解C5 均价 (Spot)', "type": 'kpi', "sql": "SELECT AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) AS value FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)`='裂解C5均价' AND `biz_date(业务日期)`=(SELECT MAX(`biz_date(业务日期)`) FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)`='裂解C5均价')", "options": {'unit': '元/吨', 'delta': 7.9, 'deltaLabel': 'WoW', 'color': '#3B82F6'}},
    {"id": 'kpi_c9', "title": '裂解C9 均价 (Spot)', "type": 'kpi', "sql": "SELECT AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) AS value FROM lz_v_裂解c9_data WHERE `supplier_name(厂商名称)`='裂解C9均价' AND `biz_date(业务日期)`=(SELECT MAX(`biz_date(业务日期)`) FROM lz_v_裂解c9_data WHERE `supplier_name(厂商名称)`='裂解C9均价')", "options": {'unit': '元/吨', 'delta': 8.8, 'deltaLabel': 'WoW', 'color': '#8B5CF6'}},
    {"id": 'kpi_erp', "title": 'ERP C5 Realized (Jul 2025)', "type": 'kpi', "sql": "SELECT AVG(price) AS value FROM v_actual_price WHERE material_name='C5' AND date>='2025-07-01' AND date<='2025-07-31'", "options": {'unit': '元/吨', 'delta': -56.9, 'deltaLabel': 'vs spot', 'color': '#F59E0B'}},
    {"id": 'kpi_spread', "title": 'Spot vs ERP Spread', "type": 'kpi', "sql": "SELECT (SELECT AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)`='裂解C5均价' AND `biz_date(业务日期)`=(SELECT MAX(`biz_date(业务日期)`) FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)`='裂解C5均价')) / (SELECT AVG(price) FROM v_actual_price WHERE material_name='C5' AND date>='2025-07-01' AND date<='2025-07-31') * 100 - 100 AS value", "options": {'unit': '%', 'delta': 0, 'deltaLabel': 'ERP stale 229d', 'color': '#EF4444'}},
    {"id": 'combo_upstream_vs_c5', "title": 'Upstream Naphtha vs C5 Spot (Dual Axis)', "type": 'combo', "sql": "SELECT `biz_date(业务日期)` AS label, AVG(CASE WHEN `supplier_name(厂商名称)`='裂解C5均价' THEN CAST(`tax_price(含税单价)` AS DECIMAL(14,2)) END) AS c5, AVG(CASE WHEN `supplier_name(厂商名称)`='日本石脑油' THEN CAST(`tax_price(含税单价)` AS DECIMAL(14,2)) END) AS naphtha FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)` IN ('裂解C5均价','日本石脑油') AND `biz_date(业务日期)` >= '2026/02/01' GROUP BY `biz_date(业务日期)` ORDER BY `biz_date(业务日期)`", "options": {'bars': ['c5'], 'lines': ['naphtha'], 'span': 'wide', 'barColor': '#3B82F6', 'lineColor': '#10B981'}},
    {"id": 'line_c5_trend', "title": '裂解C5 Spot Trend (Daily)', "type": 'line', "sql": "SELECT `biz_date(业务日期)` AS label, AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) AS value FROM lz_v_裂解c5_data WHERE `supplier_name(厂商名称)`='裂解C5均价' AND `biz_date(业务日期)` >= '2026/02/01' GROUP BY `biz_date(业务日期)` ORDER BY `biz_date(业务日期)`", "options": {'color': '#3B82F6', 'unit': '元/吨'}},
    {"id": 'line_c9_trend', "title": '裂解C9 Spot Trend (Daily)', "type": 'line', "sql": "SELECT `biz_date(业务日期)` AS label, AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) AS value FROM lz_v_裂解c9_data WHERE `supplier_name(厂商名称)`='裂解C9均价' AND `biz_date(业务日期)` >= '2026/02/01' GROUP BY `biz_date(业务日期)` ORDER BY `biz_date(业务日期)`", "options": {'color': '#8B5CF6', 'unit': '元/吨'}},
    {"id": 'bar_supplier_latest', "title": 'Latest Quote by Supplier (Tier Context)', "type": 'bar', "sql": "SELECT t.`supplier_name(厂商名称)` AS label, t.p AS value FROM (SELECT `supplier_name(厂商名称)`, AVG(CAST(`tax_price(含税单价)` AS DECIMAL(14,2))) AS p, `biz_date(业务日期)` FROM v_lz_data WHERE `supplier_name(厂商名称)` IN ('裂解C5均价','扬子裂解C5','茂名裂解C5','裂解C9均价','日本石脑油','布伦特') AND `biz_date(业务日期)`=(SELECT MAX(`biz_date(业务日期)`) FROM v_lz_data) GROUP BY `supplier_name(厂商名称)`, `biz_date(业务日期)`) t ORDER BY t.p DESC", "options": {'colors': ['#3B82F6', '#8B5CF6', '#10B981', '#F59E0B', '#EC4899', '#06B6D4']}},
    {"id": 'table_quotes', "title": 'Recent Daily Quotes', "type": 'table', "sql": "SELECT `biz_date(业务日期)` AS date, `supplier_name(厂商名称)` AS supplier, CAST(`tax_price(含税单价)` AS DECIMAL(14,2)) AS price, `material_name(产品名称)` AS product FROM v_lz_data WHERE `biz_date(业务日期)` >= '2026/03/10' ORDER BY `biz_date(业务日期)` DESC LIMIT 25", "options": {}},
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
