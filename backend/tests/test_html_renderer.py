"""Tests for the enterprise HTML chat renderer (spec §10.2)."""
from __future__ import annotations

from app.services.enterprise_orchestrator.renderers.html_chat import (
    render_enterprise_html,
)


def _sample_payload() -> dict:
    return {
        "enterprise_report_kind": "executive",
        "title": "2026-07-21 to 2026-08-20 Supply Chain Report",
        "domain": "supply_chain",
        "period": ["2026-07-21", "2026-08-20"],
        "period_label": "2026-07-21 to 2026-08-20",
        "primary_metric": "volume",
        "executive_summary": (
            "Supply Chain summary for 2026-07-21 to 2026-08-20: "
            "1,234.56 tons sold; top-3 customer share 45.0%."
        ),
        "primary_metric_breakdown": {
            "label": "Primary Metric Breakdown",
            "metric": "volume",
            "rows": [
                {"material_name": "Steel", "total_volume_tons": 500.0, "total_revenue": 50.0},
                {"material_name": "Aluminium", "total_volume_tons": 400.0, "total_revenue": 40.0},
            ],
            "kpi": {"total_volume_tons": 1234.56, "total_revenue": 123.0},
            "available": True,
            "unavailable_reason": "",
        },
        "segment_decomposition": {
            "label": "Segment Decomposition",
            "rows": [
                {"customer_name": "Acme", "total_volume_tons": 300.0, "total_revenue": 30.0},
                {"customer_name": "Globex", "total_volume_tons": 200.0, "total_revenue": 20.0},
            ],
            "available": True,
            "unavailable_reason": "",
            "observations": ["Top segment: Acme with 300.00 tons / ¥30.00M revenue."],
        },
        "operational_drivers": {
            "label": "Supply-Demand Drivers",
            "available": True,
            "drivers": ["Volume + revenue dominated by top 5 materials."],
        },
        "risk_section": {
            "label": "Transmission Risk",
            "available": True,
            "risks": ["Customer concentration high: top-3 = 45.00%."],
        },
        "recommended_actions": [
            {
                "action": "Restock SKUs with sub-five-day supply before the next delivery window.",
                "severity": "high",
                "source_facet": "inventory_position",
                "metric_value": 2.0,
                "threshold": 5.0,
            },
        ],
        "data_confidence": {
            "covered_facets": ["sales_summary", "inventory_position"],
            "missing_facets": ["competitor_benchmark"],
            "missing_reasons": {"competitor_benchmark": "no competitor data table"},
        },
        "claims": [
            {
                "claim_id": "primary_kpi",
                "text": "Total volume over the period: 1,234.56 tons",
                "source_facet": "sales_summary",
                "source_row_ids": ["Steel", "Aluminium"],
                "source_sql": "SELECT SUM(qty) FROM erp_v_sale_orderentry",
                "verified": True,
            },
        ],
        "lineage": {
            "sales_summary": {
                "source_sql": "SELECT SUM(qty) FROM erp_v_sale_orderentry",
                "source_label": "erp_v_sale_orderentry",
                "row_count": 2,
                "execution_log": [
                    {"step": "service_invoke", "latency_ms": 120, "status": "ok"},
                ],
                "available": True,
            },
            "competitor_benchmark": {
                "source_sql": "",
                "source_label": "",
                "row_count": 0,
                "execution_log": [],
                "available": False,
            },
        },
        "created_at": "2026-08-20T00:00:00Z",
    }


def test_returns_string():
    out = render_enterprise_html(_sample_payload())
    assert isinstance(out, str)
    assert len(out) > 0


def test_six_sections_inline_markdown():
    out = render_enterprise_html(_sample_payload())
    for heading in (
        "Executive Summary",
        "Primary Metric Breakdown",
        "Segment Decomposition",
        "Supply-Demand Drivers",
        "Transmission Risk",
        "Recommended Actions",
    ):
        assert heading in out, f"missing section heading: {heading}"


def test_lineage_collapsed_details():
    out = render_enterprise_html(_sample_payload())
    assert "<details" in out
    assert "Data Lineage" in out
    assert "SELECT SUM(qty) FROM erp_v_sale_orderentry" in out


def test_citation_anchor_links_to_details_entry():
    out = render_enterprise_html(_sample_payload())
    # The [1] citation is an anchor to the claim entry in the details block.
    assert "claim-1" in out


def test_missing_facet_rendered_as_unavailable():
    out = render_enterprise_html(_sample_payload())
    assert "competitor_benchmark" in out
    assert "no competitor data table" in out


def test_primary_table_renders_rows():
    out = render_enterprise_html(_sample_payload())
    assert "Steel" in out
    assert "Aluminium" in out


def test_recommended_actions_rendered():
    out = render_enterprise_html(_sample_payload())
    assert "Restock SKUs" in out
