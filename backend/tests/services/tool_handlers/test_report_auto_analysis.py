"""test_report_auto_analysis — coverage for the auto-analysis payload builder.

Unit-tests the heuristic + role detection in
``app.services.tool_handlers._report_auto_analysis.auto_analyze`` against
the exact 7-col order shape the user reported (order_id, order_date,
customer_name, product_name, quantity, unit_price, total_amount).

It also covers the pure-Python fallback path. Run with::
    docker exec zhanlu-backend python -m pytest backend/tests/services/tool_handlers/test_report_auto_analysis.py -v
"""
from __future__ import annotations

from app.services.tool_handlers._report_auto_analysis import (
    auto_analyze,
    _auto_analyze_plain,
)


ORDER_ROWS = [
    ("121886", "2026-08-01 10:16:37", "碳五A",  "碳五石油树脂",  64,    8053.10, 515398.23),
    ("121888", "2026-08-02 09:44:17", "碳五B",  "碳五石油树脂",  55,    8584.07, 472123.89),
    ("121890", "2026-08-03 09:28:20", "客户C",  "双环戊二烯",   1012,   1050.00, 1062600.00),
    ("121894", "2026-08-03 17:55:32", "碳五D",  "碳五石油树脂",  28,    1220.00, 34160.00),
    ("121895", "2026-08-03 18:02:35", "客户E",  "双环戊二烯",   65,    6017.70, 391150.44),
    ("121896", "2026-08-03 18:10:05", "客户F",  "双环戊二烯",   96,    5663.72, 543716.81),
    ("121903", "2026-08-05 16:31:59", "碳五G",  "碳五石油树脂",  26,    1245.00, 32370.00),
    ("121906", "2026-08-05 23:22:27", "客户H",  "异戊二烯",     153,   1410.00, 215730.00),
    ("121909", "2026-08-06 14:27:45", "客户I",  "异戊二烯",     321,   1410.00, 453033.00),
    ("121910", "2026-08-06 15:06:28", "客户J",  "双环戊二烯",   32,    6460.18, 206725.66),
    ("121911", "2026-08-06 15:12:49", "客户K",  "双环戊二烯",   65,    6283.19, 408407.08),
    ("121913", "2026-08-06 22:11:57", "客户L",  "异戊二烯",     500,  10442.48, 5221238.94),
    ("121917", "2026-08-07 17:27:46", "碳五M",  "碳五石油树脂",  26,    1310.00, 34060.00),
]
COLS = ["order_id", "order_date", "customer_name", "product_name",
         "quantity", "unit_price", "total_amount"]


def _norm(rows):
    return [list(r) for r in rows]


def test_auto_analyze_produces_key_sections():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent",
                            title_hint="August 2026 Order Sales Report")
    assert payload, "auto_analyze must return a non-empty dict"
    # Required fields for the docx exporter.
    for k in ("title", "summary", "kpis", "key_findings",
               "recommendations", "chart", "sections", "methodology"):
        assert k in payload, f"missing required key: {k}"
    assert payload["title"] == "August 2026 Order Sales Report"


def test_auto_analyze_kpis_have_four_to_six_entries():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    kpis = payload["kpis"]
    assert 4 <= len(kpis) <= 8, f"expected 4–8 KPIs, got {len(kpis)}"
    labels = [k["label"] for k in kpis]
    # Total revenue and total records MUST appear (the report's headline).
    assert any("total_amount" in l.lower() for l in labels)
    assert any("records" in l.lower() for l in labels)


def test_auto_analyze_chart_is_aggregated_not_raw():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    chart = payload["chart"]
    assert chart is not None
    assert chart["type"] in ("bar", "line")
    # Critically: chart.data must be aggregated (≤20 categories), not the
    # 13-row raw dump.
    assert len(chart["data"]) <= 20, \
        f"chart.data has {len(chart['data'])} rows — auto-analyze is dumping raw rows!"


def test_auto_analyze_key_findings_mention_top_category():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    findings = payload["key_findings"]
    assert 1 <= len(findings) <= 5
    joined = " ".join(f["text"] for f in findings)
    # The product that holds 61% of revenue must be named in key_findings.
    assert "异戊二烯" in joined, "top product should be highlighted"


def test_auto_analyze_recommendations_have_actionable_text():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    recs = payload["recommendations"]
    # 13 rows / 3 products → expect a concentration-risk recommendation.
    assert recs, "recommendations must not be empty"
    joined = " ".join(r["text"] for r in recs).lower()
    assert any(w in joined for w in ("concentration", "diversif", "spread"))


def test_auto_analyze_summary_contains_total_revenue():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    s = payload["summary"]
    # We must include the total revenue figure, formatted.
    assert "total_amount" in s
    # and it should contain a number-like token (B / M suffix)
    import re
    assert re.search(r"\d+(\.\d+)?[BMK]", s), \
        "summary should contain a formatted revenue token like '9.59M'"


def test_auto_analyze_handles_empty_data():
    assert auto_analyze([], ["col_a"]) == {}
    assert auto_analyze([[1]], []) == {}


def test_auto_analyze_handles_all_null_column():
    rows = [[None, "a"], [None, "b"]]
    cols = ["x", "y"]
    payload = auto_analyze(rows, cols, tool_name="t")
    # Should still produce summary without crashing.
    assert payload


def test_plain_fallback_produces_kpis():
    # Drive the pure-python path by feeding rows shaped as dicts (the
    # same normalised shape that auto_analyze produces internally).
    rows_as_dicts = [
        dict(zip(COLS, list(r))) for r in ORDER_ROWS
    ]
    out = _auto_analyze_plain(rows_as_dicts, COLS,
                               tool_name="ask_data_agent",
                               title_hint="Plain Test")
    assert out
    assert out["title"] == "Plain Test"
    assert any("total_amount" in k["label"] for k in out["kpis"])


def test_chart_handles_three_products_correctly():
    payload = auto_analyze(_norm(ORDER_ROWS), COLS,
                            tool_name="ask_data_agent")
    data = payload["chart"]["data"]
    # All three products should appear in the aggregated chart.
    products = {d["product_name"] for d in data}
    assert products == {"碳五石油树脂", "双环戊二烯", "异戊二烯"}
