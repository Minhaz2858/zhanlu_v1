"""Regression tests for the fully-dynamic document generation pipeline.

Guards the two hard-won invariants:

1. ``render()`` for docx/pptx returns a flat ``(bytes, mime, ext)`` tuple —
   never a nested tuple whose first element is itself a tuple (the bug that
   made dynamic docx come back as 3 "bytes").
2. The server-side ``architect.synthesize_plan`` inspects the SHAPE of the
   data (date + numeric -> trend line chart; categorical + numeric -> ranking
   bar chart + table) instead of emitting a fixed template.

Run:  pytest tests/test_dynamic_document.py -q
"""
import sys

import pytest

# Ensure the backend package is importable when pytest runs from repo root.
sys.path.insert(0, ".")

from app.services.synexia.contracts import ReportCardPayload  # noqa: E402
from app.services.artifacts.document_plan import DocumentBlock, DocumentPlan  # noqa: E402
from app.services.artifacts.exporters import docx_export, pptx_export  # noqa: E402
from app.services.artifacts.architect import synthesize_plan  # noqa: E402


def _explicit_plan_payload():
    """A rich agent-authored plan: cover, KPIs, trend line, ranking bar,
    table, findings, recommendations, methodology."""
    blocks = [
        DocumentBlock(type="cover", title="Q3 Margin Review",
                      subtitle="Source: ERP C5/C9"),
        DocumentBlock(type="paragraph", title="Executive Summary",
                      text="Margins compressed 4.2pp QoQ.", style={"lead": True}),
        DocumentBlock(type="kpi_grid", items=[
            {"label": "Gross Margin", "value": "18.6%", "delta": "-4.2pp"},
            {"label": "Revenue", "value": "¥2.41B", "delta": "+3.1%"},
        ]),
        DocumentBlock(type="chart", title="Monthly Margin %", chart_type="line",
                      chart={"x_label": "month", "y_label": "margin_pct",
                             "x": ["Jan", "Feb", "Mar", "Apr"],
                             "y": [[20.1, 19.8, 19.4, 19.0]]}),
        DocumentBlock(type="chart", title="Revenue by Product", chart_type="bar",
                      chart={"x_label": "product", "y_label": "revenue_m",
                             "x": ["Isoprene", "DCPD", "SIS"],
                             "y": [742, 531, 388]}),
        DocumentBlock(type="data_table", title="Performance",
                      columns=["product", "revenue_m"],
                      rows=[["Isoprene", 742], ["DCPD", 531], ["SIS", 388]]),
        DocumentBlock(type="findings", title="Key Findings", items=[
            {"label": "Cost", "text": "Feedstock costs rose 9% QoQ."}]),
        DocumentBlock(type="recommendations", title="Recommendations", items=[
            {"label": "Pricing", "text": "Re-price DCPD contracts."}]),
        DocumentBlock(type="callout", variant="info", title="Methodology",
                      text="Weighted ERP sale-order prices."),
    ]
    plan = DocumentPlan.from_blocks(blocks, meta={"title": "Q3 Margin Review"})
    return ReportCardPayload(
        title=plan.title, source="ERP C5/C9", generated_at="2026-08-27",
        blocks=[b.model_dump() for b in plan.blocks],
    )


def _mixed_dataset_payload(tone="analyst"):
    rows = [
        {"month": "2026-01", "product": "Isoprene", "revenue_m": 110},
        {"month": "2026-02", "product": "Isoprene", "revenue_m": 118},
        {"month": "2026-03", "product": "Isoprene", "revenue_m": 121},
        {"month": "2026-01", "product": "DCPD", "revenue_m": 80},
        {"month": "2026-02", "product": "DCPD", "revenue_m": 84},
        {"month": "2026-03", "product": "DCPD", "revenue_m": 90},
    ]
    arch = synthesize_plan(
        "Monthly Revenue by Product", rows=rows,
        columns=["month", "product", "revenue_m"],
        request_text="executive summary of revenue trend by product",
        user_context={"role": tone},
    )
    return arch, ReportCardPayload(
        title="Monthly Revenue by Product", source="ERP",
        generated_at="2026-08-27",
        blocks=[b.model_dump() for b in arch.blocks],
    )


def assert_flat_tuple(result, min_bytes, label):
    assert isinstance(result, tuple) and len(result) == 3, f"{label}: not 3-tuple"
    data, mime, ext = result
    # Critical: first element must be raw bytes, NOT a tuple.
    assert isinstance(data, (bytes, bytearray)), \
        f"{label}: first elem is {type(data)} — nested-tuple regression!"
    assert isinstance(mime, str) and isinstance(ext, str)
    assert len(data) > min_bytes, f"{label}: too small ({len(data)} bytes)"
    return data


# ---------------------------------------------------------------------------
# 1. render() contract — no nested-tuple regression
# ---------------------------------------------------------------------------

def test_docx_render_explicit_blocks_is_flat_tuple():
    data = assert_flat_tuple(docx_export.render(_explicit_plan_payload()),
                             15000, "docx explicit")
    assert data[:2] == b"PK"  # docx is a zip


def test_pptx_render_explicit_blocks_is_flat_tuple():
    data = assert_flat_tuple(pptx_export.render(_explicit_plan_payload()),
                             3000, "pptx explicit")
    assert data[:2] == b"PK"  # pptx is a zip


def test_docx_render_architect_blocks_is_flat_tuple():
    _arch, payload = _mixed_dataset_payload()
    assert_flat_tuple(docx_export.render(payload), 15000, "docx architect")


def test_pptx_render_architect_blocks_is_flat_tuple():
    _arch, payload = _mixed_dataset_payload()
    assert_flat_tuple(pptx_export.render(payload), 3000, "pptx architect")


# ---------------------------------------------------------------------------
# 2. architect adapts structure to data shape (no fixed template)
# ---------------------------------------------------------------------------

def test_architect_emits_trend_and_ranking_charts():
    arch, _ = _mixed_dataset_payload()
    types = [b.type for b in arch.blocks]
    assert "cover" in types
    chart_types = [b.chart_type for b in arch.blocks if b.type == "chart"]
    # date + numeric -> line (trend); categorical + numeric -> bar (ranking)
    assert "line" in chart_types, f"missing trend line chart: {types}"
    assert "bar" in chart_types, f"missing ranking bar chart: {types}"
    # Ranking must carry an actual data table.
    assert "data_table" in types


def test_architect_handles_empty_data_gracefully():
    arch = synthesize_plan("Empty", rows=[], columns=[])
    assert arch.blocks  # at least a cover so it never renders blank
    assert arch.blocks[0].type == "cover"


def test_architect_year_month_strings_classified_as_dates():
    # "2026-01" (year-month) must be treated as a date so trends fire.
    rows = [
        {"period": "2026-01", "value": 10},
        {"period": "2026-02", "value": 20},
        {"period": "2026-03", "value": 15},
    ]
    arch = synthesize_plan("Trend", rows=rows, columns=["period", "value"])
    types = [b.type for b in arch.blocks]
    chart_types = [b.chart_type for b in arch.blocks if b.type == "chart"]
    assert "line" in chart_types, f"year-month not detected as date: {types}"


# ---------------------------------------------------------------------------
# 3. legacy fallback (no blocks) still renders
# ---------------------------------------------------------------------------

def test_docx_legacy_fallback_without_blocks():
    legacy = ReportCardPayload(
        title="Legacy", summary="Plain summary.",
        kpis=[{"label": "X", "value": "1"}],
    )
    assert_flat_tuple(docx_export.render(legacy), 1000, "docx legacy")


# ---------------------------------------------------------------------------
# 4. DocumentPlan <-> DeckPlan bridge
# ---------------------------------------------------------------------------

def test_document_plan_to_deck_plan_preserves_charts():
    blocks = [
        DocumentBlock(type="cover", title="X"),
        DocumentBlock(type="chart", title="Trend", chart_type="line",
                      chart={"x": ["a", "b"], "y": [[1, 2]]}),
    ]
    plan = DocumentPlan.from_blocks(blocks)
    deck = plan.to_deck_plan()
    assert deck.slides
    layouts = [s.layout for s in deck.slides]
    assert "cover" in layouts
    assert "chart_full" in layouts
