"""Tests for agent-authored `payload.slides` deck authoring (2026-08-29).

Covers the four fixes that make PPT decks keep the agent's consulting
narrative instead of degrading to generic "Notes / Key Metrics /
Methodology" filler:
  A. payload.slides is preserved and rendered as the deck structure
  B. columnar wide charts ({months, amount, qty}) coerce into rows
  C. findings/recommendations blocks get real default titles, not "Notes"
  D. internal fetch-failure noise is stripped from methodology
"""
import pytest


# ── Fix A: agent-authored slides win over the generic plan ──────────────

def test_payload_slides_are_preserved_through_model_validation():
    from app.services.synexia.contracts import ReportCardPayload

    raw = {
        "title": "C5/C9 Market Insight",
        "slides": [
            {"title": "C5/C9 裂解产品市场洞察报告", "subtitle": "数据驱动", "bullets": ["a"]},
            {"title": "执行摘要", "bullets": ["x", "y"], "layout": "executive_brief"},
            {"title": "战略建议", "bullets": ["z"], "layout": "recommendations"},
        ],
    }
    payload = ReportCardPayload.model_validate(raw)
    assert len(payload.slides) == 3
    assert payload.slides[1]["layout"] == "executive_brief"


def test_slides_plan_renders_agent_structure_not_generic_filler():
    from app.services.artifacts.exporters.service import ExportService
    from app.services.synexia.contracts import ReportCardPayload

    raw = {
        "title": "C5/C9 Market Insight",
        "summary": "8月销售额 ¥1.52亿",
        "chart": {
            "type": "combo",
            "title": "月度趋势",
            "data": {
                "months": ["2026-03", "2026-04"],
                "amount": [16566, 21629],
                "qty": [23910, 30300],
            },
        },
        "slides": [
            {"title": "C5/C9 裂解产品市场洞察报告", "subtitle": "数据驱动的产品线分析", "bullets": ["覆盖主体：惠州伊斯科"]},
            {"title": "执行摘要", "bullets": ["2026年8月合同销售额 ¥1.52亿，环比下降 9.7%"], "layout": "executive_brief"},
            {"title": "产品线结构分析", "bullets": ["碳五石油树脂 ¥20.25亿"], "layout": "data_table"},
            {"title": "月度趋势与波动", "bullets": ["3月–4月需求旺盛"], "layout": "chart_full"},
            {"title": "战略建议", "bullets": ["深挖高附加值产品"], "layout": "recommendations"},
            {"title": "结论", "bullets": ["产品线整体健康"], "layout": "closing"},
        ],
    }
    payload = ReportCardPayload.model_validate(raw)
    plan = ExportService._slides_to_deck_plan(payload, "make a c5 c9 market view ppt")

    assert plan is not None
    titles = [s.title for s in plan.slides]
    # The agent's authored titles — NOT "Notes" / "Key Metrics" filler.
    assert titles == [
        "C5/C9 裂解产品市场洞察报告",
        "执行摘要",
        "产品线结构分析",
        "月度趋势与波动",
        "战略建议",
        "结论",
    ]
    layouts = [s.layout for s in plan.slides]
    assert layouts[0] == "cover"
    # executive_brief alias → insights_bullets
    assert layouts[1] == "insights_bullets"
    assert layouts[3] == "chart_full"
    assert layouts[-1] == "closing"
    # The chart slide carries the payload chart data (combo → grouped_bar).
    chart_slide = plan.slides[3]
    assert chart_slide.chart_spec is not None
    assert chart_slide.chart_spec.chart_type == "grouped_bar"
    assert chart_slide.chart_rows == [
        {"months": "2026-03", "amount": 16566, "qty": 23910},
        {"months": "2026-04", "amount": 21629, "qty": 30300},
    ]


def test_slides_plan_rejected_when_too_thin():
    from app.services.artifacts.exporters.service import ExportService
    from app.services.synexia.contracts import ReportCardPayload

    payload = ReportCardPayload.model_validate({
        "title": "Thin",
        "slides": [{"title": "Only one"}],
    })
    assert ExportService._slides_to_deck_plan(payload) is None


def test_rich_payload_with_slides_returns_slides_plan():
    from app.services.artifacts.exporters.service import ExportService
    from app.services.synexia.contracts import ReportCardPayload

    payload = ReportCardPayload.model_validate({
        "title": "Market",
        "summary": "exec",
        "slides": [
            {"title": "Cover", "subtitle": "sub"},
            {"title": "Exec", "bullets": ["b1", "b2"]},
            {"title": "Recs", "bullets": ["r1"], "layout": "recommendations"},
        ],
    })
    plan = ExportService._rich_payload_to_deck_plan(payload, "market ppt")
    assert plan is not None
    assert [s.title for s in plan.slides] == ["Cover", "Exec", "Recs"]


# ── Fix B: columnar wide chart data ─────────────────────────────────────

def test_chart_columnar_wide_shape_coerces_to_rows():
    from app.services.synexia.contracts import ChartSpec

    spec = ChartSpec.model_validate({
        "type": "combo",
        "title": "C5/C9 月度合同销售额与量",
        "data": {
            "months": ["2026-03", "2026-04", "2026-05"],
            "amount": [16566, 21629, 14672],
            "qty": [23910, 30300, 16472],
        },
    })
    assert spec.data == [
        {"months": "2026-03", "amount": 16566, "qty": 23910},
        {"months": "2026-04", "amount": 21629, "qty": 30300},
        {"months": "2026-05", "amount": 14672, "qty": 16472},
    ]
    # x_key / y_keys backfilled from the first row.
    assert spec.x_key == "months"
    assert spec.y_keys == ["amount", "qty"]


def test_chart_legacy_labels_values_still_works():
    from app.services.synexia.contracts import ChartSpec

    spec = ChartSpec.model_validate({
        "data": {"labels": ["A", "B"], "values": [1, 2]},
    })
    assert spec.data == [{"label": "A", "value": 1}, {"label": "B", "value": 2}]


# ── Fix C: real default titles instead of "Notes" ───────────────────────

def test_findings_block_gets_real_title_not_notes():
    from app.services.artifacts.document_plan import (
        DocumentBlock,
        DocumentPlan,
    )

    plan = DocumentPlan(title="T", blocks=[
        DocumentBlock(type="findings", items=[
            {"label": "top", "text": "惠州伊斯科 89.9%"},
        ]),
        DocumentBlock(type="recommendations", items=[
            {"text": "深挖高附加值产品"},
        ]),
    ])
    deck = plan.to_deck_plan()
    assert [s.title for s in deck.slides] == ["Key Findings", "Recommendations"]
    assert "Notes" not in [s.title for s in deck.slides]


# ── Fix D: methodology noise is stripped ────────────────────────────────

def test_fetch_failure_noise_stripped_from_methodology():
    from app.services.synexia.contracts import ReportCardPayload

    payload = ReportCardPayload.model_validate({
        "title": "T",
        "summary": "s",
        "methodology": "Data sourced from fetch_data_batch (0 rows, 0 columns). Cached at unknown.",
    })
    assert payload.methodology == ""


def test_legit_methodology_survives():
    from app.services.synexia.contracts import ReportCardPayload

    payload = ReportCardPayload.model_validate({
        "title": "T",
        "summary": "s",
        "methodology": "Queried erp_v_sale_orderentry filtered by date >= 2026-03-01.",
    })
    assert "erp_v_sale_orderentry" in payload.methodology


# ── Full round-trip: the exact C5/C9 payload now yields the authored deck ─

def test_c5c9_payload_roundtrip_keeps_consulting_structure():
    from app.services.artifacts.exporters.service import ExportService
    from app.services.synexia.contracts import ReportCardPayload

    raw = {
        "title": "C5/C9 裂解产品市场洞察报告",
        "summary": "2026年8月实现合同销售额 ¥1.52亿",
        "chart": {
            "title": "C5/C9 月度合同销售额与量 (2026.03–08)",
            "type": "combo",
            "data": {
                "months": ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"],
                "amount": [16566, 21629, 14672, 4918, 16822, 15181],
                "qty": [23910, 30300, 16472, 7606, 26320, 19474],
            },
        },
        "slides": [
            {"title": "C5/C9 裂解产品市场洞察报告", "subtitle": "数据驱动的产品线分析与战略建议",
             "bullets": ["数据来源：ERP 销售订单", "覆盖主体：惠州伊斯科"]},
            {"title": "执行摘要", "bullets": ["2026年8月 C5/C9 产品线实现合同销售额 ¥1.52亿"], "layout": "executive_brief"},
            {"title": "产品线结构分析", "bullets": ["碳五石油树脂 ¥20.25亿 第一大类"], "layout": "data_table"},
            {"title": "月度趋势与波动", "bullets": ["3月–4月为上半年高点"], "layout": "chart_full"},
            {"title": "组织贡献分析", "bullets": ["惠州伊斯科 89.9%"], "layout": "kpi_grid"},
            {"title": "战略建议", "bullets": ["优先级1 — 深挖碳五石油树脂"], "layout": "recommendations"},
            {"title": "结论", "bullets": ["C5/C9 产品线整体健康"], "layout": "closing"},
        ],
    }
    payload = ReportCardPayload.model_validate(raw)
    plan = ExportService._rich_payload_to_deck_plan(payload, "make a c5 c9 market view ppt")

    assert plan is not None
    assert len(plan.slides) == 7
    assert [s.title for s in plan.slides] == [
        "C5/C9 裂解产品市场洞察报告",
        "执行摘要",
        "产品线结构分析",
        "月度趋势与波动",
        "组织贡献分析",
        "战略建议",
        "结论",
    ]
    assert plan.slides[3].layout == "chart_full"
    assert plan.slides[3].chart_spec is not None
    assert len(plan.slides[3].chart_rows) == 6
