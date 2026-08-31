"""DocumentPlan — the unified, fully-dynamic document contract.

A :class:`DocumentPlan` is an ORDERED list of :class:`DocumentBlock` objects.
It is deliberately NOT a fixed template: the structure, section headings,
visual elements and styling are decided per-report by analyzing the
underlying data and the user's perspective (see ``architect.synthesize_plan``
and the LLM ``blocks`` override in ``artifact_tool``).

Renderers (``dynamic_docx`` for Word, ``to_deck_plan`` → the PPTX layout
engine) are PURE block executors: they draw whatever blocks the plan
contains, in order. There is no hard-coded "cover → summary → KPIs →
findings" skeleton — that skeleton is an *output* of the analysis, encoded
as blocks, not baked into the renderer.

Block types (``DocumentBlock.type``):
    cover, section_divider, heading, paragraph, bullets, numbered,
    kpi_grid, data_table, chart, callout, comparison, timeline, quote,
    image, recommendations, findings, methodology, appendix, toc

Every block carries optional styling hints (``accent`` hex, ``level``,
``variant`` for callouts, ``span`` for two-column). Unknown fields are
ignored so the schema stays forward-compatible.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------

class DocumentBlock(BaseModel):
    """One semantic unit of a document.

    The renderer switches on ``type``. Most fields are optional and only
    meaningful for certain block types. This permissive shape lets the
    architect / LLM compose arbitrary structures without a rigid schema.
    """

    type: str = "paragraph"
    # --- text content ---
    title: str = ""
    subtitle: str = ""
    text: str = ""
    caption: str = ""
    # --- lists ---
    bullets: list[str] = Field(default_factory=list)
    # --- structured items (kpi_grid / comparison / timeline / recommendations) ---
    items: list[dict[str, Any]] = Field(default_factory=list)
    # --- tables ---
    columns: list[str] = Field(default_factory=list)
    rows: list[Any] = Field(default_factory=list)  # list[list] or list[dict]
    # --- charts ---
    chart_type: str = "bar"  # bar | line | pie | donut | stacked_bar
    chart: dict[str, Any] = Field(default_factory=dict)  # {x, y, series, labels}
    # --- media ---
    image: str = ""  # base64 data URI or absolute path
    # --- styling hints ---
    accent: str = ""  # hex, overrides theme accent for this block
    level: int = 1  # heading level
    variant: str = ""  # callout tone: info|success|warning|risk|opportunity
    span: str = "full"  # full | half (two-column layouts)
    style: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class DocumentPlan(BaseModel):
    """An ordered, data-driven document structure."""

    title: str = "Report"
    subtitle: str = ""
    author: str = "Zhanlu AI"
    doc_type: str = "report"  # report | brief | memo
    theme: str = "zhanlu-blue"
    mode: str = "light"
    accent: str = ""  # global accent override (hex)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_blocks(cls, blocks: list[Any], *, meta: Optional[dict] = None) -> "DocumentPlan":
        """Build a plan from a raw ``blocks`` list (dicts accepted)."""
        norm: list[DocumentBlock] = []
        title = (meta or {}).get("title") or "Report"
        subtitle = (meta or {}).get("subtitle") or ""
        theme = (meta or {}).get("theme") or "zhanlu-blue"
        for b in blocks or []:
            if isinstance(b, DocumentBlock):
                norm.append(b)
            elif isinstance(b, dict):
                if not b.get("type"):
                    b = {**b, "type": "paragraph"}
                norm.append(DocumentBlock(**b))
        if meta and meta.get("title"):
            title = meta["title"]
        return cls(
            title=title,
            subtitle=subtitle,
            theme=theme,
            blocks=norm,
            meta=meta or {},
        )

    @classmethod
    def from_reportcard(cls, rcp: Any) -> "DocumentPlan":
        """Convert a legacy ``ReportCardPayload`` into a DocumentPlan.

        Used as the deterministic fallback so a rich ReportCardPayload still
        renders through the dynamic engine (and existing tests keep passing).
        """
        title = getattr(rcp, "title", None) or "Report"
        summary = getattr(rcp, "summary", "") or ""
        methodology = getattr(rcp, "methodology", "") or ""
        kpis = getattr(rcp, "kpis", []) or []
        findings = getattr(rcp, "key_findings", []) or []
        insights = getattr(rcp, "insights", []) or []
        recs = getattr(rcp, "recommendations", []) or []
        sections = getattr(rcp, "sections", []) or []
        chart = getattr(rcp, "chart", None)
        sql = getattr(rcp, "sql", "") or ""
        source = getattr(rcp, "source", "") or ""

        blocks: list[DocumentBlock] = []
        blocks.append(DocumentBlock(type="cover", title=title, subtitle=source))

        if summary:
            blocks.append(DocumentBlock(
                type="paragraph", text=summary, style={"lead": True}))

        if kpis:
            blocks.append(DocumentBlock(
                type="kpi_grid",
                items=[_kpi_to_item(k) for k in kpis]))

        if methodology:
            blocks.append(DocumentBlock(
                type="methodology", text=methodology))

        if findings or insights:
            items = [_insight_to_item(f) for f in (findings or [])]
            items += [_insight_to_item(i) for i in (insights or [])]
            blocks.append(DocumentBlock(type="findings", items=items))

        for s in sections:
            title = s.get("title") if isinstance(s, dict) else getattr(s, "title", "")
            content = s.get("content") if isinstance(s, dict) else getattr(s, "content", "")
            bullets = s.get("bullets") if isinstance(s, dict) else getattr(s, "bullets", [])
            blocks.append(DocumentBlock(
                type="heading", title=title or "Section", level=2))
            if content:
                blocks.append(DocumentBlock(type="paragraph", text=content))
            if bullets:
                blocks.append(DocumentBlock(type="bullets", bullets=list(bullets)))

        if chart and getattr(chart, "data", None):
            blocks.append(DocumentBlock(
                type="chart",
                title=getattr(chart, "title", "Data"),
                chart_type=getattr(chart, "type", "bar"),
                chart=_chart_to_dict(chart)))

        if recs:
            items = [_insight_to_item(r) for r in recs]
            blocks.append(DocumentBlock(type="recommendations", items=items))

        if sql:
            blocks.append(DocumentBlock(type="callout", variant="info",
                                        title="SQL", text=sql))

        return cls(title=title, blocks=blocks)

    # -- pptx bridge -------------------------------------------------------

    def to_deck_plan(self) -> "Any":
        """Translate this plan into a ``DeckPlan`` for the PPTX pipeline.

        Reuses the mature ``DeckPlan``/``SlidePlan`` layout engine so the
        SAME blocks drive both Word and PowerPoint. Each block becomes one
        or more slides with the most appropriate layout.
        """
        from app.services.synexia.contracts import DeckPlan, SlidePlan, ChartSpecInSlide

        slides: list[SlidePlan] = []
        for b in self.blocks:
            slides.extend(_block_to_slides(b, ChartSpecInSlide, SlidePlan))

        return DeckPlan(
            title=self.title,
            deck_type="data_report",
            theme_recommendation=self.theme or "zhanlu-blue",
            slides=slides,
            summary=self.subtitle,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kpi_to_item(k) -> dict:
    if isinstance(k, dict):
        return {
            "label": k.get("label") or k.get("name") or "KPI",
            "value": k.get("value") or k.get("display") or "",
            "delta": k.get("delta") or "",
            "caption": k.get("caption") or "",
        }
    return {
        "label": getattr(k, "label", None) or getattr(k, "name", "KPI"),
        "value": getattr(k, "value", None) or getattr(k, "display", ""),
        "delta": getattr(k, "delta", "") or "",
        "caption": getattr(k, "caption", "") or "",
    }


def _insight_to_item(x) -> dict:
    if isinstance(x, dict):
        return {"label": x.get("label") or "", "text": x.get("text") or ""}
    return {"label": getattr(x, "icon", "") or "", "text": getattr(x, "text", "") or ""}


def _chart_to_dict(chart) -> dict:
    out: dict[str, Any] = {}
    for f in ("type", "title", "data", "x", "y", "x_key", "y_keys", "unit"):
        v = getattr(chart, f, None)
        if v is not None:
            out[f] = v
    return out


def _block_to_slides(b: DocumentBlock, ChartSpecInSlide, SlidePlan) -> list:
    """Map a single DocumentBlock to one or more SlidePlan objects."""
    t = b.type
    out: list = []

    if t == "cover":
        out.append(SlidePlan(layout="cover", title=b.title or "Report",
                              subtitle=b.subtitle or ""))
    elif t == "section_divider":
        out.append(SlidePlan(layout="section_divider", title=b.title,
                              subtitle=b.subtitle))
    elif t == "kpi_grid":
        out.append(SlidePlan(
            layout="kpi_grid",
            title=b.title or "Key Metrics",
            kpi_specs=[_item_to_kpi(i) for i in b.items],
        ))
    elif t == "chart":
        spec = ChartSpecInSlide(
            chart_type=b.chart_type or "bar",
            x_key=(b.chart or {}).get("x_key") or (b.chart or {}).get("x_label") or "",
            y_keys=(b.chart or {}).get("y_keys") or [],
            title=b.title or "Chart",
        )
        rows = _chart_to_rows(b.chart)
        out.append(SlidePlan(
            layout="chart_full",
            title=b.title or "Chart",
            chart_spec=spec,
            chart_rows=rows,
        ))
    elif t == "data_table":
        out.append(SlidePlan(
            layout="data_table",
            title=b.title or "Data",
            table_cols=list(b.columns),
            table_rows=_norm_rows(b.rows, b.columns),
        ))
    elif t in ("callout", "findings", "recommendations", "bullets", "paragraph", "heading"):
        bullets = list(b.bullets)
        if t in ("findings", "recommendations") and b.items:
            bullets = [f"{(i.get('label') + ': ') if i.get('label') else ''}{i.get('text','')}"
                       for i in b.items]
        if t == "paragraph" and b.text and not bullets:
            bullets = [b.text]
        if t == "heading":
            out.append(SlidePlan(layout="section_divider", title=b.title))
        else:
            layout = {
                "findings": "findings_cards",
                "recommendations": "recommendations",
                "callout": "insights_bullets",
                "bullets": "insights_bullets",
                "paragraph": "insights_bullets",
            }.get(t, "insights_bullets")
            # Real default titles per block type — never the generic
            # "Notes" filler (2026-08-29).  A findings block without an
            # explicit title renders as "Key Findings", not "Notes".
            _default_title = {
                "findings": "Key Findings",
                "recommendations": "Recommendations",
                "callout": "Key Insight",
                "bullets": "Key Points",
                "paragraph": "Summary",
            }.get(t, "Notes")
            out.append(SlidePlan(
                layout=layout,
                title=b.title or (b.text[:40] if b.text else _default_title),
                bullets=bullets[:8],
            ))
    elif t == "comparison":
        out.append(SlidePlan(
            layout="chart_with_bullets",
            title=b.title or "Comparison",
            bullets=[f"{i.get('label','')}: {i.get('value','')}" for i in b.items][:6],
        ))
    elif t == "timeline":
        out.append(SlidePlan(
            layout="findings_cards",
            title=b.title or "Timeline",
            bullets=[f"{i.get('date','')} — {i.get('title','')}" for i in b.items][:6],
        ))
    elif t == "methodology":
        out.append(SlidePlan(layout="methodology", title=b.title or "Methodology",
                             bullets=[b.text] if b.text else []))
    elif t == "appendix":
        out.append(SlidePlan(
            layout="data_table",
            title=b.title or "Appendix",
            table_cols=list(b.columns),
            table_rows=_norm_rows(b.rows, b.columns),
        ))
    else:
        if b.title or b.text:
            out.append(SlidePlan(layout="insights_bullets",
                                 title=b.title or "Notes",
                                 bullets=[b.text] if b.text else []))
    return out


def _item_to_kpi(i: dict):
    from app.services.synexia.contracts import KPISpecInSlide
    return KPISpecInSlide(
        label=i.get("label", ""),
        value=str(i.get("value", "")),
        delta=i.get("delta") or None,
        caption=i.get("caption") or None,
    )


def _chart_to_rows(chart: dict) -> list[dict]:
    data = chart.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[:30]
    x = chart.get("x") or []
    y = chart.get("y") or []
    if isinstance(y, list) and y and isinstance(y[0], (list, tuple)):
        # multiple series
        labels = chart.get("labels") or [f"series{i}" for i in range(len(y))]
        rows = []
        for j, series in enumerate(y):
            for k, val in enumerate(series):
                rows.append({chart.get("x_label", "x"): x[k] if k < len(x) else "",
                             "series": labels[j], "value": val})
        return rows[:60]
    rows = []
    for k, xv in enumerate(x):
        rows.append({chart.get("x_label", "x"): xv,
                     chart.get("y_label", "value"): y[k] if k < len(y) else ""})
    return rows[:30]


def _norm_rows(rows: list, columns: list) -> list[dict]:
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, (list, tuple)):
            out.append({columns[i]: r[i] for i in range(min(len(r), len(columns)))})
    return out[:30]


__all__ = ["DocumentBlock", "DocumentPlan"]
