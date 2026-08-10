#!/usr/bin/env python3
"""Generic sandbox runner — generates file artifacts from input data.

This script runs INSIDE the Docker sandbox container. It reads the input
package from /input/ and writes the generated file to /output/.

Input layout (mounted read-only at /input/):
    /input/config.json          — {format, title, instructions, row_count}
    /input/data/*.json          — data snapshots (array of row objects)
    /input/data/*.csv           — data snapshots (CSV format)
    /input/instructions.md      — natural-language instructions
    /input/sandbox_runner.py    — this script itself (self-mount)

Output layout (writable at /output/):
    /output/report.xlsx         — generated file (extension varies by format)
    /output/build_manifest.json — metadata about what was generated

Supported formats:
    xlsx — Excel spreadsheet with auto-sized columns and header styling
    pptx — PowerPoint presentation with title slide + data table
    html — Interactive HTML dashboard with Plotly charts
    pdf  — PDF report with data table
    docx — Word document with data table
    md   — Markdown file with formatted table
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path("/input")
DATA_DIR = INPUT_DIR / "data"
OUTPUT_DIR = Path("/output")
CONFIG_PATH = INPUT_DIR / "config.json"
INSTRUCTIONS_PATH = INPUT_DIR / "instructions.md"


def load_config():
    """Load the skill configuration from /input/config.json."""
    if not CONFIG_PATH.exists():
        print("ERROR: /input/config.json not found", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data():
    """Load all data snapshots from /input/data/*.json."""
    all_rows = []
    if not DATA_DIR.exists():
        return all_rows

    for json_file in sorted(DATA_DIR.glob("*.json")):
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            all_rows.extend(data)
        elif isinstance(data, dict):
            all_rows.append(data)

    # Also try CSV files
    for csv_file in sorted(DATA_DIR.glob("*.csv")):
        import csv
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                all_rows.append(dict(row))

    return all_rows


def load_instructions():
    """Load natural-language instructions from /input/instructions.md."""
    if INSTRUCTIONS_PATH.exists():
        return INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    return ""


def derive_columns(rows):
    """Derive column names from the first row's keys (preserving order)."""
    if not rows:
        return []
    seen = set()
    columns = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(str(key))
    return columns


def write_manifest(config, rows, output_file, fmt):
    """Write build_manifest.json with generation metadata."""
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
        "format": fmt,
        "title": config.get("title", "Generated Report"),
        "row_count": len(rows),
        "columns": derive_columns(rows),
        "output_file": output_file,
        "instructions": config.get("instructions", ""),
        "status": "success",
    }
    manifest_path = OUTPUT_DIR / "build_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"WROTE {manifest_path}")


# --- Format generators ---

def generate_xlsx(rows, config, instructions):
    """Generate an Excel spreadsheet from data rows."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = config.get("title", "Report")[:31]

    columns = derive_columns(rows)

    # Header row
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    for col_idx, col_name in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # Data rows
    for row_idx, row in enumerate(rows, 2):
        for col_idx, col_name in enumerate(columns, 1):
            value = row.get(col_name, "") if isinstance(row, dict) else ""
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = thin_border
            if row_idx % 2 == 0:
                cell.fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

    # Auto-size columns
    for col_idx, col_name in enumerate(columns, 1):
        max_length = len(str(col_name))
        for row in rows:
            val = str(row.get(col_name, "") if isinstance(row, dict) else "")
            if len(val) > max_length:
                max_length = len(val)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_length + 2, 50)

    # Freeze header row
    ws.freeze_panes = "A2"

    # Add summary sheet if multiple numeric columns
    if len(rows) > 0:
        ws2 = wb.create_sheet("Summary")
        ws2.cell(row=1, column=1, value="Report Title").font = Font(bold=True)
        ws2.cell(row=1, column=2, value=config.get("title", "Report"))
        ws2.cell(row=2, column=1, value="Generated At").font = Font(bold=True)
        ws2.cell(row=2, column=2, value=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        ws2.cell(row=3, column=1, value="Total Rows").font = Font(bold=True)
        ws2.cell(row=3, column=2, value=len(rows))
        ws2.cell(row=4, column=1, value="Columns").font = Font(bold=True)
        ws2.cell(row=4, column=2, value=", ".join(columns))

    output_file = "report.xlsx"
    output_path = OUTPUT_DIR / output_file
    wb.save(str(output_path))
    print(f"WROTE {output_path}")
    return output_file


def generate_pptx(rows, config, instructions):
    """Generate a branded Claude-style PowerPoint deck from data + config.

    Runs inside the sandbox container, so ALL pptx imports are function-local
    (module-level pptx imports fail in the worker context). Config carries the
    rich ReportCardPayload-shaped fields (title, summary, source, methodology,
    kpis, insights, key_findings, recommendations, next_step, chart, sections);
    ``rows`` is the raw data snapshot used for the data-table slide.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

    # --- Brand theme (mirrors pptx_export.py via _theme.DeckTheme) ---
    # The sandbox container cannot import the app theme module or read the
    # skills dir, so the orchestrator ships a *resolved* token dict in
    # config["theme_tokens"] (produced by DeckTheme.as_hex_dict()).  When
    # absent we fall back to baked zhanlu-blue defaults so the legacy
    # branded deck is reproduced exactly.  The C_* names are preserved so
    # the slide-builder body below needs no changes.
    def _C(hexstr):
        h = (hexstr or "").lstrip("#")
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    _tk = config.get("theme_tokens") or {}
    _g = _tk.get
    C_PRIMARY = _C(_g("primary", "#2563eb"))
    C_TEXT = _C(_g("text", "#0f172a"))
    C_MUTED = _C(_g("muted", "#64748b"))
    C_BORDER = _C(_g("border", "#e2e8f0"))
    C_BG = _C(_g("band_bg", "#f8fafc"))
    C_KPI_BG = _C(_g("kpi_bg", "#f1f5f9"))
    C_FINDING_BG = _C(_g("finding_bg", "#f5f3ff"))
    C_FINDING_BORDER = _C(_g("finding_accent", "#7c3aed"))
    C_REC_BG = _C(_g("rec_bg", "#eff6ff"))
    C_REC_BORDER = C_PRIMARY  # recommendation / next-step border = primary
    C_WARN_BG = _C(_g("warn_bg", "#fffbeb"))
    C_WARN_BORDER = _C(_g("warn_accent", "#f59e0b"))
    C_WHITE = _C(_g("slide_bg", "#ffffff"))
    C_DELTA_UP = _C(_g("delta_up", "#059669"))
    C_DELTA_DOWN = _C(_g("delta_down", "#dc2626"))
    # Style recipe -> default corner radius (inches) for cards/callouts.
    # sharp=0 keeps plain RECTANGLE (byte-identical legacy output).
    _recipe_name = (config.get("style_recipe") or "sharp").lower()
    CARD_RADIUS = {"sharp": 0.0, "soft": 0.08, "rounded": 0.15, "pill": 0.30}.get(
        _recipe_name, 0.0)
    FOOTER = "Generated by Zhanlu AI"

    prs = Presentation()
    prs.slide_width = Inches(13.333)  # 16:9
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    def _set_bg(slide, color):
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = color

    def _textbox(slide, left, top, width, height, text, size,
                 bold=False, color=None, align=PP_ALIGN.LEFT):
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0)
        tf.margin_right = Inches(0)
        tf.margin_top = Inches(0)
        tf.margin_bottom = Inches(0)
        p = tf.paragraphs[0]
        p.alignment = align
        run = p.add_run()
        run.text = text or ""
        run.font.size = Pt(size)
        run.font.bold = bold
        if color is not None:
            run.font.color.rgb = color

    def _rect(slide, left, top, width, height, fill=None, border=None, radius=0.0):
        # radius > 0 -> ROUNDED_RECTANGLE (adjustment = radius/shorter side,
        # clamped to [0, 0.5]); else plain RECTANGLE (legacy default).
        if radius and radius > 0.0:
            shape = slide.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
            try:
                w_in = float(int(width)) / 914400.0
                h_in = float(int(height)) / 914400.0
                shorter = min(w_in, h_in) or 1.0
                shape.adjustments[0] = max(0.0, min(0.5, radius / shorter))
            except Exception:
                pass
        else:
            shape = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, left, top, width, height)
        shape.shadow.inherit = False
        if fill is not None:
            shape.fill.solid()
            shape.fill.fore_color.rgb = fill
        else:
            shape.fill.background()
        if border is not None:
            shape.line.color.rgb = border
            shape.line.width = Pt(0.75)
        else:
            shape.line.fill.background()
        shape.text_frame.text = ""
        return shape

    def _section_title(slide, text):
        _textbox(slide, Inches(0.6), Inches(0.5), Inches(12.1), Inches(0.7),
                 text, 28, bold=True, color=C_TEXT)
        _rect(slide, Inches(0.6), Inches(1.25), Inches(1.2), Inches(0.05), fill=C_PRIMARY)

    def _coerce(v):
        if v is None or isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    title = config.get("title", "Generated Report")
    source = config.get("source", "")
    summary = config.get("summary", "")
    methodology = config.get("methodology", "")
    kpis = config.get("kpis", []) or []
    insights = config.get("insights", []) or []
    key_findings = config.get("key_findings", []) or []
    recommendations = config.get("recommendations", []) or []
    sections = config.get("sections", []) or []
    next_step = config.get("next_step")
    warnings = config.get("warnings", []) or []
    chart = config.get("chart") or {}
    chart_data_rows = chart.get("data") or []

    # --- Cover ---
    s = prs.slides.add_slide(blank)
    _set_bg(s, C_WHITE)
    _rect(s, 0, 0, prs.slide_width, Inches(0.18), fill=C_PRIMARY)
    _textbox(s, Inches(0.6), Inches(2.3), Inches(12.1), Inches(1.4),
             title, 44, bold=True, color=C_TEXT)
    if source:
        _textbox(s, Inches(0.6), Inches(3.7), Inches(12.1), Inches(0.4),
                 f"Source: {source}", 16, color=C_MUTED)
    meta = f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    if instructions:
        meta += f"  ·  {len(rows)} records"
    _textbox(s, Inches(0.6), Inches(6.6), Inches(12.1), Inches(0.4),
             meta, 11, color=C_MUTED)

    # --- Agenda ---
    if sections:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Agenda")
        top = Inches(1.7)
        for i, sec in enumerate(sections[:10], start=1):
            label = sec.get("title", sec) if isinstance(sec, dict) else str(sec)
            _textbox(s, Inches(0.8), top, Inches(11.9), Inches(0.5),
                     f"{i}. {label}", 18, color=C_TEXT)
            top += Inches(0.55)

    # --- Executive Summary ---
    if summary or warnings:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Executive Summary")
        if summary:
            _textbox(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(2.5),
                     summary, 18, color=C_TEXT)
        if warnings:
            box_top = Inches(4.4) if summary else Inches(1.5)
            box = _rect(s, Inches(0.6), box_top, Inches(12.1), Inches(2.0),
                        fill=C_WARN_BG, border=C_WARN_BORDER, radius=CARD_RADIUS)
            tf = box.text_frame
            tf.word_wrap = True
            tf.margin_left = Inches(0.2)
            tf.margin_right = Inches(0.2)
            p = tf.paragraphs[0]
            p.text = "Warnings"
            p.runs[0].font.size = Pt(16)
            p.runs[0].font.bold = True
            p.runs[0].font.color.rgb = C_TEXT
            for w in warnings:
                p2 = tf.add_paragraph()
                p2.text = f"• {w}"
                p2.runs[0].font.size = Pt(14)
                p2.runs[0].font.color.rgb = C_TEXT

    # --- Key Metrics ---
    if kpis:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Key Metrics")
        n = len(kpis)
        if n == 1:
            ncols, nrows = 1, 1
        elif n == 2:
            ncols, nrows = 2, 1
        elif n <= 4:
            ncols, nrows = 2, 2
        else:
            ncols, nrows = 2, 3
        cell_w, cell_h, gap = Inches(5.8), Inches(2.5), Inches(0.2)
        for idx, k in enumerate(kpis[: ncols * nrows]):
            col = idx % ncols
            row = idx // ncols
            left = Inches(0.6) + (cell_w + gap) * col
            top = Inches(1.5) + (cell_h + gap) * row
            box = _rect(s, left, top, cell_w, cell_h, fill=C_KPI_BG,
                        border=C_BORDER, radius=CARD_RADIUS)
            tf = box.text_frame
            tf.word_wrap = True
            tf.margin_left = Inches(0.25)
            tf.margin_right = Inches(0.25)
            tf.margin_top = Inches(0.15)
            p = tf.paragraphs[0]
            p.text = (k.get("label", "") or "").upper()
            p.runs[0].font.size = Pt(11)
            p.runs[0].font.bold = True
            p.runs[0].font.color.rgb = C_MUTED
            p2 = tf.add_paragraph()
            run = p2.add_run()
            run.text = k.get("value", "—")
            run.font.size = Pt(36)
            run.font.bold = True
            run.font.color.rgb = C_TEXT
            delta = k.get("delta")
            if delta:
                run2 = p2.add_run()
                run2.text = f"   {delta}"
                d = delta.strip()
                run2.font.color.rgb = (C_DELTA_UP if d.startswith(("+", "\u25B2"))
                                       else C_DELTA_DOWN if d.startswith(("-", "\u25BC"))
                                       else C_MUTED)
                run2.font.size = Pt(18)
                run2.font.bold = True
            if k.get("caption"):
                p3 = tf.add_paragraph()
                p3.text = k["caption"]
                p3.runs[0].font.size = Pt(12)
                p3.runs[0].font.color.rgb = C_MUTED

    # --- Chart ---
    if chart_data_rows:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, chart.get("title", "Chart"))
        x_key = chart.get("x_key", "label")
        y_keys = chart.get("y_keys") or ["value"]
        cd = CategoryChartData()
        cd.categories = [str(r.get(x_key, "")) for r in chart_data_rows]
        for k in y_keys:
            cd.add_series(k, [_coerce(r.get(k)) for r in chart_data_rows])
        ctype = (chart.get("type", "bar") or "bar").lower()
        if ctype == "line":
            cs = s.shapes.add_chart(XL_CHART_TYPE.LINE, Inches(0.6), Inches(1.5),
                                    Inches(12.1), Inches(5.6), cd)
        elif ctype == "pie":
            cs = s.shapes.add_chart(XL_CHART_TYPE.PIE, Inches(0.6), Inches(1.5),
                                    Inches(12.1), Inches(5.6), cd)
        else:
            cs = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.6),
                                    Inches(1.5), Inches(12.1), Inches(5.6), cd)
        ch = cs.chart
        ch.has_title = False
        ch.has_legend = len(y_keys) > 1
        if len(y_keys) > 1:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False

    # --- Key Findings ---
    if key_findings:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Key Findings")
        box = _rect(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.3),
                    fill=C_FINDING_BG, border=C_FINDING_BORDER, radius=CARD_RADIUS)
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.3)
        tf.margin_right = Inches(0.3)
        for i, kf in enumerate(key_findings[:6]):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(8)
            text = kf.get("text", "") if isinstance(kf, dict) else str(kf)
            run = p.add_run()
            run.text = f"{i + 1}. {text}"
            run.font.size = Pt(18)
            run.font.color.rgb = C_TEXT

    # --- Insights ---
    if insights:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Insights")
        top = Inches(1.6)
        for ins in insights[:6]:
            text = ins.get("text", "") if isinstance(ins, dict) else str(ins)
            _textbox(s, Inches(0.6), top, Inches(12.1), Inches(0.6),
                     f"•  {text}", 18, color=C_TEXT)
            top += Inches(0.7)

    # --- Recommendations ---
    if recommendations:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Recommendations")
        box = _rect(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.3),
                    fill=C_REC_BG, border=C_REC_BORDER, radius=CARD_RADIUS)
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.3)
        tf.margin_right = Inches(0.3)
        for i, rec in enumerate(recommendations[:6]):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(8)
            text = rec.get("text", "") if isinstance(rec, dict) else str(rec)
            run = p.add_run()
            run.text = f"{i + 1}. {text}"
            run.font.size = Pt(18)
            run.font.color.rgb = C_TEXT

    # --- Data table (chart rows if present, else raw rows) ---
    data_rows = chart_data_rows if chart_data_rows else rows
    if data_rows:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Data")
        keys = derive_columns(data_rows)
        display_cols = keys[:10]
        display_rows = data_rows[:24]
        nrows = len(display_rows) + 1
        ncols = len(display_cols)
        if ncols == 0:
            ncols = 1
        table_shape = s.shapes.add_table(
            nrows, ncols, Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.5))
        table = table_shape.table
        for ci, k in enumerate(display_cols):
            cell = table.cell(0, ci)
            cell.text = k
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(11)
                    r.font.bold = True
                    r.font.color.rgb = C_TEXT
            cell.fill.solid()
            cell.fill.fore_color.rgb = C_BG
        for ri, row in enumerate(display_rows, start=1):
            for ci, k in enumerate(display_cols):
                v = row.get(k, "") if isinstance(row, dict) else ""
                sv = "" if v is None else str(v)
                if len(sv) > 80:
                    sv = sv[:80] + "…"
                cell = table.cell(ri, ci)
                cell.text = sv
                for p in cell.text_frame.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(10)
                        r.font.color.rgb = C_TEXT
                if ri % 2 == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = C_BG
        if len(data_rows) > 24:
            _textbox(s, Inches(0.6), Inches(7.1), Inches(12.1), Inches(0.3),
                     f"… {len(data_rows) - 24} more rows truncated for slide.",
                     10, color=C_MUTED)

    # --- Methodology ---
    if methodology:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _section_title(s, "Methodology")
        _textbox(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.5),
                 methodology, 18, color=C_TEXT)

    # --- Closing / Next Step ---
    if next_step:
        s = prs.slides.add_slide(blank)
        _set_bg(s, C_WHITE)
        _rect(s, 0, 0, prs.slide_width, Inches(0.18), fill=C_PRIMARY)
        _textbox(s, Inches(0.6), Inches(1.2), Inches(12.1), Inches(0.8),
                 "Next Step", 40, bold=True, color=C_TEXT)
        box = _rect(s, Inches(0.6), Inches(2.4), Inches(12.1), Inches(3.0),
                    fill=C_REC_BG, border=C_REC_BORDER, radius=CARD_RADIUS)
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.3)
        tf.margin_right = Inches(0.3)
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = "Next step: "
        run.font.size = Pt(20)
        run.font.bold = True
        run.font.color.rgb = C_REC_BORDER
        run2 = p.add_run()
        run2.text = next_step
        run2.font.size = Pt(20)
        run2.font.color.rgb = C_TEXT

    # --- Footers on every content slide (not the cover) ---
    total = len(prs.slides)
    for i, slide in enumerate(prs.slides, start=1):
        if i == 1:
            continue
        _textbox(slide, Inches(9.0), Inches(7.05), Inches(4.0), Inches(0.3),
                 f"{FOOTER} · {i} / {total}", 10, color=C_MUTED, align=PP_ALIGN.RIGHT)

    output_file = "report.pptx"
    output_path = OUTPUT_DIR / output_file
    prs.save(str(output_path))
    print(f"WROTE {output_path}")
    return output_file


def generate_html(rows, config, instructions):
    """Generate an interactive HTML dashboard with Plotly charts.

    When the caller supplies ``config['kpis']`` (list of {label, value,
    caption} dicts) and ``config['insights']`` (list of strings or
    {text} dicts), they take precedence over the auto-derived
    "Total Records / Columns / Numeric Columns" stat cards and add an
    Insights panel below the data table.  This keeps the sidecar
    preview in lockstep with the DOCX renderer's KPI table.
    """
    columns = derive_columns(rows)
    numeric_columns = []
    for col in columns:
        numeric_count = 0
        for row in rows:
            val = row.get(col) if isinstance(row, dict) else None
            if val is not None:
                try:
                    float(val)
                    numeric_count += 1
                except (ValueError, TypeError):
                    pass
        if numeric_count > len(rows) * 0.5:
            numeric_columns.append(col)

    # Build chart specs
    charts_html = ""
    if numeric_columns and columns:
        x_col = columns[0]
        y_cols = numeric_columns[:4]  # Max 4 numeric series

        # Bar chart
        chart_data = []
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            point = {"label": str(row.get(x_col, ""))}
            for y_col in y_cols:
                try:
                    point[y_col] = float(row.get(y_col, 0))
                except (ValueError, TypeError):
                    point[y_col] = 0
            chart_data.append(point)

        import json as _json
        chart_data_json = _json.dumps(chart_data)

        charts_html = f"""
        <div id="chart_div"></div>
        <script>
            var chartData = {chart_data_json};
            var trace1 = {{
                x: chartData.map(function(d) {{ return d.label; }}),
                y: chartData.map(function(d) {{ return d["{y_cols[0]}"] || 0; }}),
                type: 'bar',
                name: '{y_cols[0]}',
                marker: {{ color: '#4472C4' }}
            }};
            var data = [trace1];
            var layout = {{
                title: '{y_cols[0]} by {x_col}',
                xaxis: {{ tickangle: -45 }},
                margin: {{ t: 40, b: 100 }}
            }};
            Plotly.newPlot('chart_div', data, layout, {{responsive: true}});
        </script>
        """

    # Build data table HTML
    table_rows_html = ""
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        cells = ""
        for col in columns:
            val = row.get(col, "")
            cells += f"<td>{val}</td>"
        table_rows_html += f"<tr>{cells}</tr>"

    header_cells = "".join(f"<th>{col}</th>" for col in columns)

    # --- KPI cards (prefer caller-supplied; fall back to defaults) ---
    kpis = config.get("kpis") or []
    if kpis:
        kpi_cards = "".join(
            f'<div class="stat-card">'
            f'<div class="label">{k.get("label", "")}</div>'
            f'<div class="value">{k.get("value", "")}</div>'
            + (f'<div class="caption">{k.get("caption", "")}</div>' if k.get("caption") else "")
            + '</div>'
            for k in kpis
        )
    else:
        kpi_cards = (
            f'<div class="stat-card"><div class="label">Total Records</div><div class="value">{len(rows)}</div></div>'
            f'<div class="stat-card"><div class="label">Columns</div><div class="value">{len(columns)}</div></div>'
            f'<div class="stat-card"><div class="label">Numeric Columns</div><div class="value">{len(numeric_columns)}</div></div>'
        )

    # --- Insights panel ---
    insights = config.get("insights") or []
    insights_section = ""
    if insights:
        items = "".join(
            f"<li>{ins if isinstance(ins, str) else (ins.get('text', '') if isinstance(ins, dict) else '')}</li>"
            for ins in insights
        )
        insights_section = f"""
        <div class="insights-container">
            <h2>Insights</h2>
            <ul>{items}</ul>
        </div>
        """

    # --- Key findings (narrative paragraphs) ---
    key_findings = config.get("key_findings") or []
    key_findings_section = ""
    if key_findings:
        paras = "".join(
            f"<p>{f if isinstance(f, str) else (f.get('text', '') if isinstance(f, dict) else '')}</p>"
            for f in key_findings
        )
        key_findings_section = f"""
        <div class="findings-container">
            <h2>Key Findings</h2>
            {paras}
        </div>
        """

    # --- Recommendations (bullets) ---
    recommendations = config.get("recommendations") or []
    recommendations_section = ""
    if recommendations:
        items = "".join(
            f"<li>{r if isinstance(r, str) else (r.get('text', '') if isinstance(r, dict) else '')}</li>"
            for r in recommendations
        )
        recommendations_section = f"""
        <div class="insights-container">
            <h2>Recommendations</h2>
            <ul>{items}</ul>
        </div>
        """

    # --- Custom sections (heading + paragraph/bullets) ---
    custom_sections = ""
    for sec in (config.get("sections") or []):
        if not isinstance(sec, dict) or not sec.get("title"):
            continue
        body = ""
        if sec.get("content"):
            body += f"<p>{sec['content']}</p>"
        for b in (sec.get("bullets") or []):
            body += f"<ul><li>{b}</li></ul>"
        custom_sections += f"""
        <div class="insights-container">
            <h2>{sec['title']}</h2>
            {body}
        </div>
        """

    # --- Executive summary callout ---
    summary = (config.get("summary") or "").strip()
    summary_section = ""
    if summary:
        summary_section = f"""
        <div class="summary-container">
            <span class="badge">EXECUTIVE SUMMARY</span>
            <p>{summary}</p>
        </div>
        """

    # --- Methodology callout ---
    methodology = (config.get("methodology") or "").strip()
    methodology_section = ""
    if methodology:
        methodology_section = f"""
        <div class="methodology-container">
            <span class="badge subtle">METHODOLOGY</span>
            <p>{methodology}</p>
        </div>
        """

    # --- Next step callout ---
    next_step = (config.get("next_step") or "").strip()
    next_step_section = ""
    if next_step:
        next_step_section = f"""
        <div class="next-step-container">
            <strong>Next step:</strong> {next_step}
        </div>
        """

    # --- SQL block ---
    sql_text = (config.get("sql") or "").strip()
    sql_section = ""
    if sql_text:
        sql_section = f"""
        <div class="insights-container">
            <h2>SQL</h2>
            <pre style="background:#0F172A;color:#E2E8F0;padding:12px;border-radius:6px;
                        overflow-x:auto;font-size:12px;">{sql_text}</pre>
        </div>
        """

    # Full HTML document
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{config.get('title', 'Dashboard')}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }}
        .header {{ background: linear-gradient(135deg, #1f2a44 0%, #e67e22 100%); color: white; padding: 30px 40px; }}
        .header h1 {{ font-size: 28px; margin-bottom: 8px; }}
        .header .meta {{ font-size: 14px; opacity: 0.9; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .stat-card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .stat-card .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; margin-bottom: 4px; }}
        .stat-card .value {{ font-size: 28px; font-weight: 700; color: #1f2a44; }}
        .stat-card .caption {{ font-size: 11px; color: #94A3B8; margin-top: 4px; }}
        .chart-container {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .table-container {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow-x: auto; }}
        .insights-container {{ background: white; border-radius: 12px; padding: 24px; margin-top: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .insights-container h2 {{ color: #e67e22; font-size: 18px; margin-bottom: 12px; }}
        .insights-container ul {{ list-style: disc; padding-left: 24px; }}
        .insights-container li {{ margin-bottom: 8px; line-height: 1.5; }}
        .findings-container {{ background: white; border-radius: 12px; padding: 24px; margin-top: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .findings-container h2 {{ color: #1f2a44; font-size: 18px; margin-bottom: 12px; }}
        .findings-container p {{ line-height: 1.65; margin: 10px 0; color: #1E293B; }}
        .summary-container {{ background: #FEF3C7; border-left: 4px solid #F59E0B; border-radius: 6px; padding: 16px 20px; margin-bottom: 24px; }}
        .summary-container p {{ margin: 6px 0 0; line-height: 1.55; }}
        .methodology-container {{ background: #F1F5F9; border-left: 4px solid #64748B; border-radius: 6px; padding: 16px 20px; margin-bottom: 24px; }}
        .methodology-container p {{ margin: 6px 0 0; line-height: 1.55; }}
        .next-step-container {{ background: #EFF6FF; border-left: 4px solid #2563EB; border-radius: 6px; padding: 12px 16px; margin-bottom: 16px; }}
        .badge {{ display: inline-block; background: #92400E; color: #fff; font-size: 10px; font-weight: 700; letter-spacing: 1px; padding: 3px 8px; border-radius: 4px; margin-bottom: 6px; }}
        .badge.subtle {{ background: #475569; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th {{ background: #1f2a44; color: white; padding: 10px 12px; text-align: left; font-weight: 600; position: sticky; top: 0; }}
        td {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
        tr:nth-child(even) {{ background: #fafafa; }}
        tr:hover {{ background: #f0f7ff; }}
        .footer {{ text-align: center; padding: 20px; color: #888; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{config.get('title', 'Dashboard')}</h1>
        <div class="meta">Generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {len(rows)} records | {len(columns)} columns</div>
    </div>
    <div class="container">
        {summary_section}
        {methodology_section}
        <div class="stats">{kpi_cards}</div>
        <div class="chart-container">
            {charts_html if charts_html else '<p style="text-align:center;color:#888;padding:40px;">No numeric data available for charts</p>'}
        </div>
        <div class="table-container">
            <table>
                <thead><tr>{header_cells}</tr></thead>
                <tbody>{table_rows_html}</tbody>
            </table>
        </div>
        {key_findings_section}
        {insights_section}
        {recommendations_section}
        {custom_sections}
        {sql_section}
        {next_step_section}
    </div>
    <div class="footer">Generated by Zhanlu AI</div>
</body>
</html>"""

    output_file = "report.html"
    output_path = OUTPUT_DIR / output_file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"WROTE {output_path}")
    return output_file


def generate_pdf(rows, config, instructions):
    """Generate a PDF report from data rows."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch

    output_file = "report.pdf"
    output_path = OUTPUT_DIR / output_file

    doc = SimpleDocTemplate(str(output_path), pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title_style = styles["Heading1"]
    elements.append(Paragraph(config.get("title", "Report"), title_style))
    elements.append(Spacer(1, 0.2 * inch))

    # Meta info
    meta_style = styles["Normal"]
    elements.append(Paragraph(
        f"Generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {len(rows)} records",
        meta_style,
    ))
    elements.append(Spacer(1, 0.3 * inch))

    if instructions:
        elements.append(Paragraph(f"<b>Instructions:</b> {instructions}", meta_style))
        elements.append(Spacer(1, 0.2 * inch))

    # Data table
    columns = derive_columns(rows)
    table_data = [columns]
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        table_data.append([str(row.get(col, "")) for col in columns])

    table = Table(table_data)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9D9D9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
    ]))

    elements.append(table)
    doc.build(elements)
    print(f"WROTE {output_path}")
    return output_file


def generate_docx(rows, config, instructions):
    """Generate a Claude-style Word document from data rows.

    Produces a richly structured report (matches the visual language of
    the rich-HTML dashboard generated by ``generate_html``):

    1. Cover page — themed title, source, generation date, brand line
    2. Executive Summary — config['summary'] in a tinted callout
    3. Methodology — config['methodology'] (if present)
    4. Key Metrics — config['kpis'] as a 1×N table with bold values
    5. Key Findings — config['key_findings'] as narrative paragraphs
    6. Insights — config['insights'] as a bulleted list
    7. Recommendations — config['recommendations'] as a bulleted list
    8. Custom sections — config['sections'] in declared order
    9. Data table — colored header row, banded rows
    10. SQL block — config['sql'] in a tinted code paragraph
    11. Next Step — config['next_step'] as a tinted callout
    12. Footer with page numbers — "Generated by Zhanlu AI · Page X of Y"

    Proper Word styles ("Heading 1", "Heading 2") are used so mammoth's
    docx→html conversion preserves structure.
    """
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    # Theme — kept consistent with generate_html's CSS palette so the
    # user gets the same brand feel in both formats.
    ORANGE = RGBColor(0xE6, 0x7E, 0x22)   # accent / section headings
    DARK = RGBColor(0x1F, 0x2A, 0x44)     # primary text / KPI values
    MUTED = RGBColor(0x6B, 0x72, 0x80)    # captions / labels
    HEADER_FILL = "1F2A44"                # dark navy header background

    doc = Document()

    # Set default body font
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    # Style heading 1/2/3 in dark navy so mammoth converts them to
    # <h1>/<h2>/<h3> with a consistent look.
    for level, (size, color) in {
        1: (16, DARK),
        2: (14, ORANGE),
        3: (12, MUTED),
    }.items():
        h_style = doc.styles[f"Heading {level}"]
        h_style.font.size = Pt(size)
        h_style.font.color.rgb = color
        h_style.font.bold = True
        h_style.font.name = "Calibri"

    # --- 1. Cover page ---
    title_text = config.get("title", "Report")
    cover_title = doc.add_paragraph()
    cover_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cover_run = cover_title.add_run(f"📊  {title_text}")
    cover_run.font.size = Pt(28)
    cover_run.font.bold = True
    cover_run.font.color.rgb = DARK
    cover_title.paragraph_format.space_before = Pt(120)
    cover_title.paragraph_format.space_after = Pt(12)

    source = (config.get("source") or "").strip()
    if source:
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = sub.add_run(f"Source: {source}")
        sr.font.size = Pt(13)
        sr.font.color.rgb = MUTED

    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sub2 = doc.add_paragraph()
    sub2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub2.paragraph_format.space_after = Pt(2)
    sr2 = sub2.add_run(f"Generated {gen_at} · {len(rows)} record{'s' if len(rows) != 1 else ''}")
    sr2.font.size = Pt(11)
    sr2.font.color.rgb = MUTED

    # Brand footer of cover page
    brand = doc.add_paragraph()
    brand.alignment = WD_ALIGN_PARAGRAPH.CENTER
    brand.paragraph_format.space_before = Pt(180)
    br = brand.add_run("Zhanlu AI · Intelligent Data Reports")
    br.font.size = Pt(10)
    br.font.color.rgb = MUTED
    br.italic = True

    # Page break to start the body
    doc.add_page_break()

    def _section_heading(text, level=1):
        """Add a styled section heading."""
        p = doc.add_heading(text, level=level)
        if level == 1:
            for r in p.runs:
                r.font.color.rgb = DARK
                r.font.size = Pt(16)
        elif level == 2:
            for r in p.runs:
                r.font.color.rgb = ORANGE
                r.font.size = Pt(13)
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(4)
        return p

    def _shade_cell(cell, hex_color):
        """Set a cell's background color (hex string without #)."""
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tc_pr.append(shd)

    def _shade_paragraph(paragraph, hex_color):
        """Set a paragraph's background color (for tinted callouts)."""
        pPr = paragraph._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        pPr.append(shd)

    def _insight_text(it):
        if isinstance(it, str):
            return it
        if isinstance(it, dict):
            return it.get("text", "")
        return str(it) if it is not None else ""

    def _add_insight_bullets(items):
        for ins in items or []:
            text = _insight_text(ins)
            if not text:
                continue
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(text)
            run.font.size = Pt(10)

    # --- 2. Executive Summary ---
    summary = (config.get("summary") or "").strip()
    if summary:
        _section_heading("Executive Summary")
        para = doc.add_paragraph(summary)
        _shade_paragraph(para, "FEF3C7")
        para.paragraph_format.left_indent = Cm(0.4)
        para.paragraph_format.space_after = Pt(12)
        for r in para.runs:
            r.font.size = Pt(10)

    # --- 3. Methodology ---
    methodology = (config.get("methodology") or "").strip()
    if methodology:
        _section_heading("Methodology")
        para = doc.add_paragraph(methodology)
        _shade_paragraph(para, "F1F5F9")
        para.paragraph_format.left_indent = Cm(0.4)
        for r in para.runs:
            r.font.size = Pt(10)

    # --- 4. KPI table ---
    kpis = config.get("kpis") or []
    if kpis:
        _section_heading("Key Metrics")
        kpi_table = doc.add_table(rows=2, cols=len(kpis))
        kpi_table.style = "Light Grid Accent 1"
        # Header row — labels
        for i, kpi in enumerate(kpis):
            cell = kpi_table.rows[0].cells[i]
            cell.text = ""
            _shade_cell(cell, "F8FAFC")
            label_p = cell.paragraphs[0]
            label_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            label_run = label_p.add_run(str(kpi.get("label", "")))
            label_run.font.size = Pt(9)
            label_run.font.color.rgb = MUTED
            label_run.font.bold = True
        # Value row
        for i, kpi in enumerate(kpis):
            cell = kpi_table.rows[1].cells[i]
            cell.text = ""
            value_p = cell.paragraphs[0]
            value_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            value_run = value_p.add_run(str(kpi.get("value", "")))
            value_run.font.size = Pt(22)
            value_run.font.bold = True
            value_run.font.color.rgb = DARK
            delta = kpi.get("delta")
            if delta:
                dp = cell.add_paragraph()
                dp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                dr = dp.add_run(str(delta))
                dr.font.size = Pt(9)
                dr.font.bold = True
                dr.font.color.rgb = RGBColor(0x05, 0x96, 0x69)
            caption = kpi.get("caption")
            if caption:
                cap_p = cell.add_paragraph()
                cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                cap_run = cap_p.add_run(str(caption))
                cap_run.font.size = Pt(8)
                cap_run.font.color.rgb = MUTED
                cap_run.italic = True
        doc.add_paragraph()  # spacing after the KPI row

    # --- 5. Key Findings (narrative paragraphs) ---
    key_findings = config.get("key_findings") or []
    if key_findings:
        _section_heading("Key Findings")
        for f in key_findings:
            text = _insight_text(f)
            if not text:
                continue
            p = doc.add_paragraph(text)
            p.paragraph_format.space_after = Pt(6)
            for r in p.runs:
                r.font.size = Pt(10)

    # --- 6. Insights (bullets) ---
    insights = config.get("insights") or []
    if insights:
        _section_heading("Insights")
        _add_insight_bullets(insights)

    # --- 7. Recommendations (bullets) ---
    recommendations = config.get("recommendations") or []
    if recommendations:
        _section_heading("Recommendations")
        _add_insight_bullets(recommendations)

    # --- 8. Custom sections ---
    sections = config.get("sections") or []
    for sec in sections:
        if not isinstance(sec, dict) or not sec.get("title"):
            continue
        _section_heading(sec.get("title"))
        if sec.get("content"):
            p = doc.add_paragraph(sec["content"])
            for r in p.runs:
                r.font.size = Pt(10)
        for b in (sec.get("bullets") or []):
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(str(b))
            run.font.size = Pt(10)

    # --- Instructions (legacy field, kept for backwards compat) ---
    if instructions and not (summary or methodology or key_findings or insights):
        _section_heading("Instructions")
        p = doc.add_paragraph(instructions)
        for r in p.runs:
            r.font.size = Pt(10)

    # --- 9. Data table ---
    columns = derive_columns(rows)
    if columns and rows:
        _section_heading("Data")
        table = doc.add_table(rows=1, cols=len(columns))
        table.style = "Light Grid Accent 1"

        # Header row — dark fill, white bold text
        header_cells = table.rows[0].cells
        for i, col_name in enumerate(columns):
            cell = header_cells[i]
            _shade_cell(cell, HEADER_FILL)
            cell.text = ""
            p = cell.paragraphs[0]
            run = p.add_run(str(col_name))
            run.font.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            run.font.size = Pt(10)

        # Data rows — banded for readability
        for row_idx, row in enumerate(rows[:100]):
            if not isinstance(row, dict):
                continue
            row_cells = table.add_row().cells
            banded = (row_idx % 2 == 1)
            for i, col_name in enumerate(columns):
                cell = row_cells[i]
                cell.text = str(row.get(col_name, ""))
                if banded:
                    _shade_cell(cell, "F2F2F2")
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)

    # --- 10. SQL block ---
    sql = (config.get("sql") or "").strip()
    if sql:
        _section_heading("SQL")
        sql_para = doc.add_paragraph()
        sql_run = sql_para.add_run(sql)
        sql_run.font.name = "Consolas"
        sql_run.font.size = Pt(9)
        sql_run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x8A)
        _shade_paragraph(sql_para, "F1F5F9")

    # --- 11. Next step ---
    next_step = (config.get("next_step") or "").strip()
    if next_step:
        _section_heading("Next Step")
        ns = doc.add_paragraph(next_step)
        _shade_paragraph(ns, "EFF6FF")
        ns.paragraph_format.left_indent = Cm(0.4)
        for r in ns.runs:
            r.italic = True
            r.font.color.rgb = RGBColor(0x25, 0x6, 0xEB)
            r.font.size = Pt(10)

    # --- 12. Footer with page numbers ---
    _add_page_number_footer(doc, "Generated by Zhanlu AI")

    output_file = "report.docx"
    output_path = OUTPUT_DIR / output_file
    doc.save(str(output_path))
    print(f"WROTE {output_path}")
    return output_file


def _add_page_number_footer(doc, brand_text: str) -> None:
    """Add a "brand · Page X of Y" footer to every page using
    Word's PAGE and NUMPAGES fields."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor
    section = doc.sections[0]
    footer = section.footer
    for p in list(footer.paragraphs):
        p._p.getparent().remove(p._p)
    para = footer.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    muted = RGBColor(0x94, 0xA3, 0xB8)
    # "brand_text · Page "
    run = para.add_run(f"{brand_text} · Page ")
    run.font.size = Pt(9)
    run.font.color.rgb = muted

    _add_field(para, "PAGE")
    para.add_run(" of ").font.size = Pt(9)
    _add_field(para, "NUMPAGES")
    for r in para.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = muted


def _add_field(paragraph, field_code: str) -> None:
    """Append a Word field (e.g. 'PAGE', 'NUMPAGES') to a paragraph."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    run = paragraph.add_run()
    fldChar_begin = OxmlElement("w:fldChar")
    fldChar_begin.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = f" {field_code} "
    fldChar_sep = OxmlElement("w:fldChar")
    fldChar_sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "1"
    fldChar_end = OxmlElement("w:fldChar")
    fldChar_end.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar_begin)
    run._r.append(instrText)
    run._r.append(fldChar_sep)
    run._r.append(placeholder)
    run._r.append(fldChar_end)


def generate_md(rows, config, instructions):
    """Generate a Markdown file from data rows."""
    columns = derive_columns(rows)

    lines = [
        f"# {config.get('title', 'Report')}",
        "",
        f"Generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | {len(rows)} records",
        "",
    ]

    if instructions:
        lines.extend(["## Instructions", "", instructions, ""])

    lines.extend(["## Data", ""])

    # Markdown table
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines.append(header)
    lines.append(separator)

    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        cells = [str(row.get(col, "")) for col in columns]
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by Zhanlu Sandbox Runner*")

    output_file = "report.md"
    output_path = OUTPUT_DIR / output_file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"WROTE {output_path}")
    return output_file


# --- Main ---

GENERATORS = {
    "xlsx": generate_xlsx,
    "pptx": generate_pptx,
    "html": generate_html,
    "pdf":  generate_pdf,
    "docx": generate_docx,
    "md":   generate_md,
}


def main():
    """Entry point — read input, generate output, write manifest."""
    print("=== Zhanlu Sandbox Runner ===")
    print(f"Input dir: {INPUT_DIR}")
    print(f"Output dir: {OUTPUT_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load inputs
    config = load_config()
    rows = load_data()
    instructions = load_instructions()

    fmt = config.get("format", "xlsx").lower()
    print(f"Format: {fmt}")
    print(f"Title: {config.get('title', 'N/A')}")
    print(f"Rows loaded: {len(rows)}")

    if not rows:
        print("WARNING: No data rows loaded — generating empty report")

    # Generate
    generator = GENERATORS.get(fmt)
    if not generator:
        print(f"ERROR: Unknown format '{fmt}'", file=sys.stderr)
        sys.exit(1)

    try:
        output_file = generator(rows, config, instructions)
        write_manifest(config, rows, output_file, fmt)
        print(f"SUCCESS: Generated {output_file}")
    except Exception as e:
        print(f"ERROR: Generation failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        # Write error manifest
        error_manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
            "format": fmt,
            "title": config.get("title", "Report"),
            "status": "failed",
            "error": str(e),
        }
        with open(OUTPUT_DIR / "build_manifest.json", "w") as f:
            json.dump(error_manifest, f, indent=2)
        sys.exit(1)


if __name__ == "__main__":
    main()
