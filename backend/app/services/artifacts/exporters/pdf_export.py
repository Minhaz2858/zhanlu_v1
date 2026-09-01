"""PDF export — render a ReportCardPayload into a self-contained PDF.

Uses reportlab (BSD-3, pure-Python, no system deps).  The output is a
single-page-or-more document with:

  * Title + source + generated-at header
  * Summary paragraph
  * KPI tiles (a 2- or 4-column grid, drawn with canvases)
  * Chart (drawn natively with reportlab.graphics.charts — bar / line / pie)
  * Insights bullet list with emoji glyphs
  * "Next step" callout box
  * Data table (first 100 rows of the chart data)
  * Footer with SQL + conversation id

This is the v1 — visually clean, no decorative flourishes, prints well.
A future task can switch to WeasyPrint + a CSS template for richer
typography without changing the public `render()` signature.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    KeepTogether, Flowable,
)
from reportlab.platypus.flowables import HRFlowable
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.legends import Legend
from reportlab.lib.colors import HexColor

from app.services.synexia.contracts import ReportCardPayload
from app.services.artifacts.exporters._common import (
    ExportContext,
    chart_rows, chart_x_key, chart_y_keys, chart_x_value, coerce_number,
    short_generated_at, insight_icon_to_emoji,
)


# Brand colors — same palette as the frontend ReportCard.jsx
COLOR_PRIMARY = HexColor("#2563EB")     # blue-600
COLOR_TEXT = HexColor("#0F172A")        # slate-900
COLOR_MUTED = HexColor("#64748B")       # slate-500
COLOR_BORDER = HexColor("#E2E8F0")      # slate-200
COLOR_BG = HexColor("#F8FAFC")          # slate-50
COLOR_KPI_BG = HexColor("#F1F5F9")      # slate-100
COLOR_INSIGHT_BG = HexColor("#EFF6FF")  # blue-50
COLOR_WARN_BG = HexColor("#FFFBEB")     # amber-50
COLOR_WARN_BORDER = HexColor("#F59E0B")
COLOR_DELTA_UP = HexColor("#059669")
COLOR_DELTA_DOWN = HexColor("#DC2626")


# --- Drawing primitives ------------------------------------------------------


class _ChartDrawing(Flowable):
    """A Flowable that wraps a reportlab `Drawing` so it can sit in a
    SimpleDocTemplate flow.  Sized to the available width.
    """

    def __init__(self, drawing: Drawing, width: float, height: float = 110 * mm):
        super().__init__()
        self.drawing = drawing
        self.width = width
        self.height = height

    def wrap(self, _avail_w, _avail_h):
        return self.width, self.height

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        # Drawing has its own coordinate system; translate so (0,0) of the
        # drawing ends up at the top-left of our flowable box.
        self.drawing.drawOn(canvas, 0, 0)
        canvas.restoreState()


def _build_bar_chart(payload: ReportCardPayload, width: float, height: float) -> Drawing:
    """Bar chart for the chart spec."""
    rows = chart_rows(payload)
    y_keys = chart_y_keys(payload)
    if not rows or not y_keys:
        return _empty_drawing(width, height, "No data for chart")

    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 50
    bc.y = 30
    bc.width = width - 60
    bc.height = height - 60
    bc.data = []
    labels = []
    for k in y_keys:
        col = [coerce_number(r.get(k)) or 0 for r in rows]
        bc.data.append(col)
    for r in rows:
        labels.append(str(chart_x_value(r, payload))[:18])
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontSize = 7
    bc.categoryAxis.labels.angle = 30
    bc.valueAxis.valueMin = 0
    bc.bars.strokeColor = COLOR_PRIMARY
    bc.bars.fillColor = COLOR_PRIMARY
    if len(y_keys) == 1:
        bc.bars[0].fillColor = COLOR_PRIMARY
    else:
        palette = [COLOR_PRIMARY, HexColor("#7C3AED"), HexColor("#DB2777"), HexColor("#F59E0B")]
        for i, bar in enumerate(bc.bars):
            bar.fillColor = palette[i % len(palette)]
    d.add(bc)
    return d


def _build_line_chart(payload: ReportCardPayload, width: float, height: float) -> Drawing:
    rows = chart_rows(payload)
    y_keys = chart_y_keys(payload)
    if not rows or not y_keys:
        return _empty_drawing(width, height, "No data for chart")

    d = Drawing(width, height)
    lc = HorizontalLineChart()
    lc.x = 50
    lc.y = 30
    lc.width = width - 60
    lc.height = height - 60
    lc.data = []
    for k in y_keys:
        col = [coerce_number(r.get(k)) or 0 for r in rows]
        lc.data.append(col)
    labels = [str(chart_x_value(r, payload))[:18] for r in rows]
    lc.categoryAxis.categoryNames = labels
    lc.categoryAxis.labels.fontSize = 7
    lc.categoryAxis.labels.angle = 30
    lc.lines[0].strokeColor = COLOR_PRIMARY
    lc.lines[0].strokeWidth = 2
    if len(y_keys) > 1:
        palette = [COLOR_PRIMARY, HexColor("#7C3AED"), HexColor("#DB2777")]
        for i, line in enumerate(lc.lines):
            line.strokeColor = palette[i % len(palette)]
            line.strokeWidth = 2
    d.add(lc)
    return d


def _build_pie_chart(payload: ReportCardPayload, width: float, height: float) -> Drawing:
    rows = chart_rows(payload)
    y_keys = chart_y_keys(payload)
    if not rows or not y_keys:
        return _empty_drawing(width, height, "No data for chart")

    d = Drawing(width, height)
    pie = Pie()
    pie.x = width / 2 - 60
    pie.y = height / 2 - 60
    pie.width = 120
    pie.height = 120
    pie.data = [coerce_number(r.get(y_keys[0])) or 0 for r in rows]
    pie.labels = [str(chart_x_value(r, payload))[:12] for r in rows]
    palette = [COLOR_PRIMARY, HexColor("#7C3AED"), HexColor("#DB2777"),
               HexColor("#F59E0B"), HexColor("#10B981"), HexColor("#EF4444"),
               HexColor("#6366F1"), HexColor("#0EA5E9")]
    for i, slc in enumerate(pie.slices):
        slc.fillColor = palette[i % len(palette)]
        slc.strokeColor = colors.white
    pie.sideLabels = True
    pie.simpleLabels = False
    d.add(pie)
    return d


def _empty_drawing(width: float, height: float, msg: str) -> Drawing:
    d = Drawing(width, height)
    d.add(String(width / 2, height / 2, msg, fontSize=10,
                 fillColor=COLOR_MUTED, textAnchor="middle"))
    return d


def _build_chart_drawing(payload: ReportCardPayload, width: float, height: float) -> Drawing:
    chart_type = (payload.chart.type if payload.chart else "bar") or "bar"
    if chart_type == "line":
        return _build_line_chart(payload, width, height)
    if chart_type == "pie":
        return _build_pie_chart(payload, width, height)
    return _build_bar_chart(payload, width, height)


# --- Public entry point ------------------------------------------------------


def render(payload: ReportCardPayload, ctx: Optional[ExportContext] = None) -> tuple[bytes, str, str]:
    """Render the payload as a PDF.  Returns ``(bytes, mime, ext)``."""
    ctx = ctx or ExportContext()
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=payload.title or "Zhanlu report",
    )

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle("Title", parent=styles["Title"],
                             fontSize=18, leading=22, textColor=COLOR_TEXT,
                             spaceAfter=2 * mm)
    h_meta = ParagraphStyle("Meta", parent=styles["Normal"],
                            fontSize=8.5, leading=11, textColor=COLOR_MUTED,
                            spaceAfter=4 * mm)
    h_h2 = ParagraphStyle("H2", parent=styles["Heading2"],
                          fontSize=12, leading=15, textColor=COLOR_TEXT,
                          spaceBefore=4 * mm, spaceAfter=2 * mm)
    body = ParagraphStyle("Body", parent=styles["Normal"],
                           fontSize=9.5, leading=13, textColor=COLOR_TEXT)
    summary = ParagraphStyle("Summary", parent=body,
                             fontSize=10, leading=14, textColor=COLOR_TEXT,
                             leftIndent=4 * mm, rightIndent=4 * mm)
    insight = ParagraphStyle("Insight", parent=body,
                             fontSize=9.5, leading=13, textColor=COLOR_TEXT,
                             leftIndent=2 * mm)
    footer = ParagraphStyle("Footer", parent=styles["Normal"],
                            fontSize=7.5, leading=10, textColor=COLOR_MUTED)

    flow: list[Flowable] = []

    # --- Header ---
    flow.append(Paragraph(_html(payload.title or "Zhanlu report"), h_title))
    meta_bits = []
    if payload.source:
        meta_bits.append(f"Source: <b>{_html(payload.source)}</b>")
    meta_bits.append(f"Generated: {_html(short_generated_at(payload))}")
    meta_bits.append(f"user_signal: <b>{_html(payload.user_signal or 'default')}</b>")
    flow.append(Paragraph(" &nbsp;·&nbsp; ".join(meta_bits), h_meta))

    # --- Summary ---
    if payload.summary:
        flow.append(_summary_box(payload.summary, summary))
        flow.append(Spacer(1, 2 * mm))

    # --- Warnings (if any) ---
    if payload.warnings:
        flow.append(_warn_box(payload.warnings, body))
        flow.append(Spacer(1, 2 * mm))

    # --- KPI tiles ---
    if payload.kpis:
        flow.append(_kpi_table(payload.kpis, body))
        flow.append(Spacer(1, 3 * mm))

    # --- Chart ---
    if payload.chart and payload.chart.data:
        flow.append(Paragraph(_html(payload.chart.title or "Chart"), h_h2))
        avail_w = doc.width
        drawing = _build_chart_drawing(payload, avail_w, 80 * mm)
        flow.append(_ChartDrawing(drawing, avail_w, 80 * mm))
        flow.append(Spacer(1, 2 * mm))

    # --- Insights ---
    if payload.insights:
        flow.append(Paragraph("Insights", h_h2))
        for ins in payload.insights:
            emoji = insight_icon_to_emoji(ins.icon)
            flow.append(Paragraph(f"{emoji} &nbsp; {_html(ins.text)}", insight))
            flow.append(Spacer(1, 1 * mm))

    # --- Methodology / Key Findings / Recommendations / Custom sections ---
    # These were previously dropped by the PDF renderer (leaving the PDF far
    # thinner than the DOCX). Render them the same way the DOCX renderer does.
    # ``payload.sections`` may be pydantic ``SectionSpec`` (from
    # ``_payload_to_reportcard``) or plain dicts (legacy callers) — tolerate
    # both, plus the common key aliases the agent/LLM uses.
    if payload.methodology:
        flow.append(Paragraph("Methodology", h_h2))
        flow.append(Paragraph(_html(payload.methodology), body))
        flow.append(Spacer(1, 2 * mm))

    if payload.key_findings:
        flow.append(Paragraph("Key Findings", h_h2))
        for fnd in payload.key_findings:
            txt = fnd.text if hasattr(fnd, "text") else str(fnd)
            flow.append(Paragraph("• &nbsp; " + _html(txt), insight))
            flow.append(Spacer(1, 1 * mm))

    if payload.recommendations:
        flow.append(Paragraph("Recommendations", h_h2))
        for rec in payload.recommendations:
            txt = rec.text if hasattr(rec, "text") else str(rec)
            flow.append(Paragraph("• &nbsp; " + _html(txt), insight))
            flow.append(Spacer(1, 1 * mm))

    for sec in (payload.sections or []):
        if isinstance(sec, dict):
            heading = sec.get("title") or sec.get("heading") or sec.get("name")
            content = sec.get("content") or sec.get("body") or sec.get("text")
            bullets = list(sec.get("bullets") or sec.get("paragraphs") or [])
        else:
            heading = getattr(sec, "title", None) or getattr(sec, "heading", None)
            content = getattr(sec, "content", None) or getattr(sec, "body", None)
            bullets = list(getattr(sec, "bullets", None) or [])
        if heading:
            flow.append(Paragraph(_html(str(heading)), h_h2))
        if content:
            if isinstance(content, list):
                for line in content:
                    flow.append(Paragraph(_html(str(line)), body))
                    flow.append(Spacer(1, 1 * mm))
            else:
                flow.append(Paragraph(_html(str(content)), body))
                flow.append(Spacer(1, 2 * mm))
        for b in bullets:
            flow.append(Paragraph("• &nbsp; " + _html(str(b)), insight))
            flow.append(Spacer(1, 1 * mm))

    # NOTE: payload.next_step is intentionally NOT rendered — it is
    # conversational guidance for the in-chat card, not report content.

    # --- Data table ---
    if payload.chart and payload.chart.data:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("Data", h_h2))
        flow.append(_data_table(payload, body, max_rows=100))

    # --- Footer (SQL, conversation id) ---
    flow.append(Spacer(1, 4 * mm))
    flow.append(HRFlowable(width="100%", thickness=0.5, color=COLOR_BORDER,
                           spaceBefore=2, spaceAfter=2))
    footer_bits = []
    if ctx.conversation_id:
        footer_bits.append(f"conversation: {_html(ctx.conversation_id)}")
    if ctx.source and ctx.source != payload.source:
        footer_bits.append(f"source: {_html(ctx.source)}")
    if ctx.sql:
        footer_bits.append(f"SQL: <font face='Courier'>{_html(ctx.sql[:200])}</font>")
    footer_bits.append("Generated by Zhanlu")
    flow.append(Paragraph(" &nbsp;·&nbsp; ".join(footer_bits), footer))

    doc.build(flow)
    return buf.getvalue(), "application/pdf", ".pdf"


# --- Sub-builders ------------------------------------------------------------


def _html(s: str) -> str:
    """Lightweight HTML escape for reportlab Paragraphs."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _summary_box(text: str, style: ParagraphStyle) -> Flowable:
    """A tinted card-style box for the summary paragraph."""
    p = Paragraph(_html(text), style)
    t = Table([[p]], colWidths=["*"])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _warn_box(warnings: list[str], style: ParagraphStyle) -> Flowable:
    body = "".join(f"<bullet>&bull;</bullet> {_html(w)}" for w in warnings)
    p = Paragraph(f"<b>Warnings</b><br/>{body}", style)
    t = Table([[p]], colWidths=["*"])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_WARN_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, COLOR_WARN_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _kpi_table(kpis: list, body: ParagraphStyle) -> Flowable:
    """Render KPIs as a 2- or 4-column grid of label / value / caption."""
    cells: list[list] = []
    for k in kpis:
        # Each KPI is a single cell with three paragraphs
        delta_color = ""
        delta_prefix = ""
        if k.delta:
            d = k.delta.strip()
            if d.startswith("+") or d.startswith("\u25B2") or "up" in d.lower():
                delta_color = f'<font color="{COLOR_DELTA_UP.hexval()}">\u25B2 { _html(d) }</font>'
            elif d.startswith("-") or d.startswith("\u25BC") or "down" in d.lower():
                delta_color = f'<font color="{COLOR_DELTA_DOWN.hexval()}">\u25BC { _html(d) }</font>'
            else:
                delta_color = _html(d)
            delta_prefix = " &nbsp; "
        else:
            delta_color = ""

        lines = [
            f'<font color="{COLOR_MUTED.hexval()}" size="8"><b>{_html(k.label or "").upper()}</b></font>',
            f'<font size="14"><b>{_html(k.value or "—")}</b></font>'
            + (delta_prefix + delta_color if delta_color else ""),
        ]
        if k.caption:
            lines.append(f'<font color="{COLOR_MUTED.hexval()}" size="8">{_html(k.caption)}</font>')
        cell_paras = [Paragraph("<br/>".join(lines), body)]

        cells.append(cell_paras)

    # Wrap in a grid (2 or 4 cols, whatever fits)
    n = len(cells)
    if n <= 2:
        ncols = n
    elif n == 3:
        ncols = 3
    else:
        ncols = 4
    nrows = (n + ncols - 1) // ncols

    # Pad to fill the grid
    while len(cells) < nrows * ncols:
        cells.append([""])

    rows: list[list] = []
    for r in range(nrows):
        rows.append(cells[r * ncols : (r + 1) * ncols])

    col_widths = ["*"] * ncols
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_KPI_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, COLOR_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _data_table(payload: ReportCardPayload, body: ParagraphStyle, max_rows: int = 100) -> Flowable:
    rows = chart_rows(payload)
    if not rows:
        return Paragraph("<i>No data.</i>", body)

    keys = list(rows[0].keys())
    # Truncate super-long string columns to keep the table readable
    def _trunc(v: Any) -> str:
        s = "" if v is None else str(v)
        return s[:40] + "…" if len(s) > 40 else s

    header = [Paragraph(f"<b>{_html(k)}</b>", body) for k in keys]
    data: list[list] = [header]
    for r in rows[:max_rows]:
        data.append([Paragraph(_html(_trunc(r.get(k))), body) for k in keys])
    if len(rows) > max_rows:
        # Append a footer row noting truncation
        trunc_note = Paragraph(
            f"<font color='{COLOR_MUTED.hexval()}' size='8'><i>… {len(rows) - max_rows} more rows truncated for print.</i></font>",
            body,
        )
        data.append([trunc_note] + [""] * (len(keys) - 1))

    col_widths = [f"{100 / len(keys):.1f}%" for _ in keys]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_BG),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, COLOR_BORDER),
        ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
    ]))
    return t
