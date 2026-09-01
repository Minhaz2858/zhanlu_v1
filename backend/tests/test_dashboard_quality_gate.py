"""Quality gate for fullstack dashboard specs (Aug 2026).

The create/update_fullstack_dashboard tools return a deterministic
`quality` report (grade + widget-mix counts + recommendations) so the
agent can self-correct in the same turn instead of shipping a thin
one-chart board.
"""

from app.services.tool_handlers.dashboard_tools import _dashboard_quality_report


def _metric(metric_id, mtype, **extra):
    m = {"id": metric_id, "title": metric_id.replace("_", " ").title(),
         "type": mtype, "sql": "SELECT 1"}
    m.update(extra)
    return m


def _kpi(metric_id, **extra):
    return _metric(metric_id, "kpi", **extra)


def _perfect_spec():
    """6 widgets, 2 KPI + trend + breakdown + table, layout, insights, design ref."""
    return {
        "name": "Sales Orders",
        "slug": "sales-orders-dashboard",
        "datasource_id": "kb-1",
        "design_system_ref": "design-system/default-org/MASTER.md",
        "theme": "dark",
        "refresh_interval_seconds": 30,
        "layout": [
            {"title": "Headline KPIs", "widgets": ["kpi_amount", "kpi_qty"]},
            {"title": "Trends", "widgets": ["trend_monthly"]},
            {"title": "Breakdowns", "widgets": ["top_customers", "top_materials", "orders_table"]},
        ],
        "insights": [
            {"title": "Revenue up", "body": "Order amount rose 12% MoM."},
            {"title": "Top customer", "body": "Customer A is 18% of amount."},
        ],
        "metrics": [
            _kpi("kpi_amount", options={"delta": "+12.3%"}),
            _kpi("kpi_qty"),
            _metric("trend_monthly", "line",
                    options={"span": "wide",
                             "filters": [{"key": "product", "column": "FNAME", "label": "Product"}]}),
            _metric("top_customers", "bar"),
            _metric("top_materials", "pie"),
            _metric("orders_table", "table"),
        ],
    }


def test_perfect_spec_scores_a():
    report = _dashboard_quality_report(_perfect_spec())
    assert report["passing"] is True
    assert report["grade"] == "A"
    assert report["widget_count"] == 6
    assert report["kpi_count"] == 2
    assert report["trend_count"] == 1
    assert report["breakdown_count"] == 3
    assert report["section_count"] == 3
    assert report["insights_count"] == 2
    assert report["design_system_applied"] is True
    assert report["recommendations"] == []


def test_thin_spec_scores_c_with_hard_gaps():
    spec = {
        "name": "Thin",
        "slug": "thin-dashboard",
        "datasource_id": "kb-1",
        "metrics": [
            _kpi("kpi_amount"),
        ],
    }
    report = _dashboard_quality_report(spec)
    assert report["passing"] is False
    assert report["grade"] == "C"
    assert report["widget_count"] == 1
    joined = " ".join(report["recommendations"]).lower()
    assert "5" in joined or "widget(s)" in joined
    assert any("kpi" in r.lower() for r in report["recommendations"])
    assert any("trend" in r.lower() for r in report["recommendations"])
    assert any("insight" in r.lower() for r in report["recommendations"])
    assert any("design_system_ref" in r for r in report["recommendations"])


def test_almost_perfect_scores_b_minor_nudges_only():
    # All hard requirements met, but 9 widgets is crowded → B with a
    # consolidation nudge, never a hard fail.
    spec = _perfect_spec()
    spec["metrics"].extend([
        _metric("extra_gauge", "gauge"),
        _metric("extra_radar", "radar"),
        _metric("extra_area2", "area"),
    ])
    report = _dashboard_quality_report(spec)
    assert report["passing"] is True
    assert report["grade"] == "B"
    assert any("crowded" in r.lower() or "consolidate" in r.lower()
               for r in report["recommendations"])


def test_missing_filters_is_hard_fail():
    spec = _perfect_spec()
    spec["metrics"][2]["options"] = {"span": "wide"}  # drop the filters
    report = _dashboard_quality_report(spec)
    assert report["passing"] is False
    assert report["grade"] == "C"
    assert "filters" in report["hard_gaps"]
    assert any("filter" in r.lower() for r in report["recommendations"])


def test_missing_sections_is_hard_fail():
    spec = _perfect_spec()
    spec["layout"] = [{"title": "Everything", "widgets": ["kpi_amount"]}]
    report = _dashboard_quality_report(spec)
    assert report["passing"] is False
    assert report["grade"] == "C"
    assert any("section" in r.lower() for r in report["recommendations"])


def test_missing_design_ref_is_hard_fail():
    spec = _perfect_spec()
    spec["design_system_ref"] = None
    report = _dashboard_quality_report(spec)
    assert report["passing"] is False
    assert report["grade"] == "C"
    assert any("design_system_ref" in r for r in report["recommendations"])


def test_no_metrics_is_hard_fail():
    report = _dashboard_quality_report({"name": "Empty", "slug": "empty-dash",
                                        "datasource_id": "kb-1"})
    assert report["passing"] is False
    assert report["grade"] == "C"
    assert any("metrics" in r.lower() for r in report["recommendations"])


def test_flat_layout_single_section_gets_nudge():
    spec = _perfect_spec()
    spec["layout"] = [{"title": "Everything", "widgets": ["kpi_amount"]}]
    report = _dashboard_quality_report(spec)
    assert report["section_count"] == 1
    assert any("section" in r.lower() for r in report["recommendations"])


# ── Tier 1 auto-refine verifier ──────────────────────────────────────────
from app.services.dashboard_turn_guard import verify_dashboard_quality_refined


def test_grade_a_needs_no_refine():
    assert verify_dashboard_quality_refined("A", refined=False) is None
    assert verify_dashboard_quality_refined(None, refined=False) is None


def test_grade_b_unrefined_warns():
    msg = verify_dashboard_quality_refined("B", refined=False, hard_gaps=["filters"])
    assert msg is not None
    assert "BUILD QUALITY B" in msg
    assert "filters" in msg
    assert "update_fullstack_dashboard" in msg


def test_grade_c_unrefined_warns():
    msg = verify_dashboard_quality_refined("C", refined=False, hard_gaps=["sections", "insights"])
    assert msg is not None
    assert "BUILD QUALITY C" in msg
    assert "sections" in msg and "insights" in msg


def test_refined_after_build_does_not_warn():
    assert verify_dashboard_quality_refined("B", refined=True, hard_gaps=["filters"]) is None
    assert verify_dashboard_quality_refined("C", refined=True) is None
