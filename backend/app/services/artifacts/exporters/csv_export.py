"""CSV export — the "spreadsheet-friendly" view of a ReportCardPayload.

This is the simplest format: a UTF-8 CSV with a metadata header (a
commented block at the top), then the chart's data as proper CSV
columns, then the KPI / insight / warning rows as a tail.

The metadata block uses ``#`` line prefixes — most spreadsheet apps
will either ignore the lines on import, or surface them as a
single column "Metadata".

Why CSV at all when XLSX exists?  Because:
  1. It's the lingua franca for data tooling (pandas, awk, jq, …)
  2. It's tiny (the renderers above are 100+ LOC; this is 30)
  3. Some users explicitly want a CSV even when the chat asked for
     "export" — they pipe it into their own pipeline.

If the payload has no chart, this exporter emits a flat
"key,value" sheet with the title, source, generated-at, summary,
KPI tiles, and insights as rows.  That way the file is never empty.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Optional

from app.services.synexia.contracts import ReportCardPayload
from app.services.artifacts.exporters._common import (
    ExportContext,
    chart_rows, chart_x_key, chart_y_keys,
    short_generated_at,
)


# Columns we always try to preserve in the data section.
# Anything not in here is appended after, in the order it first
# appears in the rows.
def render(payload: ReportCardPayload, ctx: Optional[ExportContext] = None) -> tuple[bytes, str, str]:
    ctx = ctx or ExportContext()
    rows = chart_rows(payload)

    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)

    # --- Metadata header (lines starting with # are ignored by most CSV
    #     parsers; pandas reads them as a single column unless you pass
    #     comment="#" — we keep them for human readability) ---
    w.writerow([f"# Title: {payload.title or 'Zhanlu report'}"])
    if payload.source:
        w.writerow([f"# Source: {payload.source}"])
    w.writerow([f"# Generated: {short_generated_at(payload)}"])
    w.writerow([f"# user_signal: {payload.user_signal or 'default'}"])
    if payload.summary:
        w.writerow([f"# Summary: {_one_line(payload.summary)}"])
    if ctx.conversation_id:
        w.writerow([f"# conversation_id: {ctx.conversation_id}"])
    if ctx.sql:
        w.writerow([f"# SQL: {_one_line(ctx.sql)}"])

    # Empty separator row
    w.writerow([])

    # --- KPI tiles (always present if any) ---
    if payload.kpis:
        w.writerow(["# --- Key metrics ---"])
        w.writerow(["label", "value", "delta", "caption"])
        for k in payload.kpis:
            w.writerow([k.label or "", k.value or "", k.delta or "", k.caption or ""])
        w.writerow([])

    # --- Chart data (the "real" data) ---
    if rows:
        # Stable column order: prefer the LLM-provided chart x_key + y_keys first
        preferred = [chart_x_key(payload), *chart_y_keys(payload)]
        seen = set()
        ordered_keys: list[str] = []
        for k in preferred:
            if k and k in rows[0] and k not in seen:
                ordered_keys.append(k)
                seen.add(k)
        for k in rows[0].keys():
            if k not in seen:
                ordered_keys.append(k)
                seen.add(k)

        if payload.chart and payload.chart.title:
            w.writerow([f"# --- {payload.chart.title} ---"])
        else:
            w.writerow(["# --- Data ---"])
        w.writerow(ordered_keys)
        for r in rows:
            w.writerow([_csv_cell(r.get(k, "")) for k in ordered_keys])
        w.writerow([])

    # --- Insights (as a tail table) ---
    if payload.insights:
        w.writerow(["# --- Insights ---"])
        w.writerow(["icon", "text"])
        for ins in payload.insights:
            w.writerow([ins.icon or "", ins.text or ""])
        w.writerow([])

    # --- Warnings (if any) ---
    if payload.warnings:
        w.writerow(["# --- Warnings ---"])
        w.writerow(["warning"])
        for wn in payload.warnings:
            w.writerow([wn])
        w.writerow([])

    # next_step intentionally omitted — conversational guidance, not data.

    # --- Fallback for "no data" case ---
    if not rows and not payload.kpis and not payload.insights and not payload.summary:
        w.writerow(["# No data in this report."])

    # Encode as UTF-8 with BOM (helps Excel auto-detect encoding on Windows)
    text = buf.getvalue()
    return (b"\xef\xbb\xbf" + text.encode("utf-8"), "text/csv; charset=utf-8", ".csv")


# --- Helpers -----------------------------------------------------------------


def _csv_cell(v: Any) -> Any:
    """Coerce a chart row cell into a CSV-friendly value.

    Numbers stay numbers (so pandas / Excel can sum them); strings
    pass through; None becomes "".
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v)


def _one_line(s: str, max_len: int = 240) -> str:
    """Collapse a multi-line string to a single line for the # comment rows."""
    if not s:
        return ""
    s = " ".join(s.split())  # collapse whitespace
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s
