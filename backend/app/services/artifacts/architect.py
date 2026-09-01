"""Dynamic document architect — turns raw data + user context into a
adaptive :class:`DocumentPlan`.

This is the engine that makes document generation *fully dynamic and
data-driven* rather than a fixed template.  Given the actual rows/columns
the agent gathered, it inspects the **shape** of the data (numeric vs
categorical vs datetime columns, cardinality, row count) and the **user's
perspective** (executive vs analyst), then decides:

  * which sections to include,
  * what headings / narrative to give each,
  * which visual elements (KPI grid, bar/line/pie chart, data table,
    comparison, timeline) best carry the data,
  * how much raw detail to show.

There is NO hard-coded section order — the plan is computed fresh for
every report.  The agent (LLM) may still override with its own ``blocks``;
this module is the guaranteed, deterministic fallback that ensures the
output always reflects the richness of the underlying data.

Deterministic by design (no LLM call) so it is fast, cheap, and never
produces a blank or "overly simple" document.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

from app.services.artifacts.document_plan import DocumentBlock, DocumentPlan


# ---------------------------------------------------------------------------
# Low-level data helpers (pure-python, dataset-capped)
# ---------------------------------------------------------------------------

# Accepts: 2026-01-15, 2026-01 (year-month, very common in business
# time-series), 2026/1, 2026年1月, and month-name forms ("Jan 2026").
_DATE_RE = re.compile(
    r"\d{4}[-/]\d{1,2}([-/]\d{1,2})?"      # 2026-01 or 2026-01-15
    r"|\d{4}年\d{1,2}月"                      # 2026年1月
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b",  # Jan 2026
    re.I,
)


def _coerce_num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _looks_like_date(v: Any) -> bool:
    if isinstance(v, (_dt.date, _dt.datetime)):
        return True
    if isinstance(v, str):
        return bool(_DATE_RE.search(v.strip()))
    return False


def _is_temporal_name(col: str) -> bool:
    """True when a column name strongly hints at a time axis."""
    c = (col or "").lower()
    return any(k in c for k in ("date", "month", "year", "period",
                                "quarter", "time", "ym", "dt", "week"))


def _column_kinds(columns: list[str], rows: list[list]) -> dict[str, str]:
    """Classify each column as numeric / datetime / categorical / id / text."""
    kinds: dict[str, str] = {}
    n = len(rows)
    for idx, col in enumerate(columns):
        vals = [r[idx] for r in rows if idx < len(r)]
        sample = [v for v in vals if v not in (None, "", "NULL", "null")]
        if not sample:
            kinds[col] = "text"
            continue
        num = sum(1 for v in sample if _coerce_num(v) is not None)
        dates = sum(1 for v in sample if _looks_like_date(v))
        distinct = len({str(v) for v in sample})
        cname = col.lower()
        if dates >= max(1, 0.5 * len(sample)):
            kinds[col] = "datetime"
        elif num >= 0.7 * len(sample):
            kinds[col] = "numeric"
        elif distinct <= max(1, min(30, 0.5 * n)) and distinct < len(sample):
            kinds[col] = "categorical"
        elif "id" in cname or distinct >= 0.9 * len(sample):
            kinds[col] = "id"
        else:
            kinds[col] = "text"
        # Integer year columns (2015..2026) are a time axis even though they
        # are numerically coercible — promote them to datetime so trends fire.
        if kinds[col] != "datetime" and _is_temporal_name(col):
            if all(isinstance(v, int) and 1900 <= v <= 2100 for v in sample):
                kinds[col] = "datetime"
    return kinds


def _topn_by_numeric(rows, cat_idx, num_idx, n=10, ascending=False):
    scored = []
    for r in rows:
        if cat_idx >= len(r) or num_idx >= len(r):
            continue
        v = _coerce_num(r[num_idx])
        if v is None:
            continue
        scored.append((str(r[cat_idx]), v))
    scored.sort(key=lambda x: x[1], reverse=not ascending)
    return scored[:n]


def _aggregate_numeric(rows, num_idx):
    vals = [_coerce_num(r[num_idx]) for r in rows if num_idx < len(r)]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {
        "count": len(vals),
        "sum": sum(vals),
        "avg": sum(vals) / len(vals),
        "min": min(vals),
        "max": max(vals),
    }


def _fmt(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K"
    if abs(v - round(v)) < 1e-9:
        return str(int(v))
    return f"{v:.2f}"


# ---------------------------------------------------------------------------
# Perspective
# ---------------------------------------------------------------------------

def _perspective(request_text: str, user_context: Optional[dict]) -> dict:
    """Return lightweight audience profile that shapes the plan."""
    role = ""
    if isinstance(user_context, dict):
        role = str(user_context.get("role") or user_context.get("persona") or "").lower()
    text = (request_text or "").lower()
    exec_signals = ["exec", "executive", "leadership", "boss", "ceo", "cfo", "manager",
                    "summary", "board", "汇报", "领导", "管理层", "summary for",
                    "决策", "战略", "decision", "strategy", "董事长", "总裁",
                    "总经理", "汇报给", "决策建议", "战略建议"]
    is_exec = any(s in role for s in exec_signals) or any(s in text for s in exec_signals)
    analyst_signals = ["analyst", "analysis", "detail", "deep dive", "明细", "分析", "explore"]
    is_analyst = any(s in role for s in analyst_signals) or any(s in text for s in analyst_signals)
    if is_exec:
        tone = "executive"
    elif is_analyst:
        tone = "analyst"
    else:
        tone = "balanced"
    # Executives get less raw appendix detail; analysts get more.
    appendix_cap = 12 if tone == "executive" else (30 if tone == "analyst" else 20)
    return {"tone": tone, "appendix_cap": appendix_cap}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def synthesize_plan(
    title: str,
    *,
    rows: Optional[list] = None,
    columns: Optional[list] = None,
    summary: str = "",
    kpis: Optional[list] = None,
    findings: Optional[list] = None,
    recommendations: Optional[list] = None,
    request_text: str = "",
    user_context: Optional[dict] = None,
    theme: str = "zhanlu-blue",
) -> DocumentPlan:
    """Build an adaptive DocumentPlan from gathered data + user context."""
    rows = rows or []
    # Normalize rows to list-of-lists and derive columns if needed.
    norm_rows: list[list] = []
    if rows and isinstance(rows[0], dict):
        if not columns:
            columns = list(rows[0].keys())
        for r in rows:
            norm_rows.append([r.get(c, "") for c in columns])
    else:
        norm_rows = [list(r) for r in rows]
    columns = columns or []

    perspective = _perspective(request_text, user_context)
    blocks: list[DocumentBlock] = []

    # 1) Cover — always.
    blocks.append(DocumentBlock(
        type="cover",
        title=title or "Report",
        subtitle=_cover_subtitle(columns, norm_rows, request_text, perspective),
    ))

    # 2) Executive summary — narrative lead (LLM summary if present, else a
    #    data-derived overview so it is never empty).
    exec_text = summary.strip() if summary else _default_summary(columns, norm_rows, perspective)
    if exec_text:
        blocks.append(DocumentBlock(
            type="paragraph", text=exec_text,
            title="Executive Summary", style={"lead": True}))

    # 3) KPI grid — from explicit KPIs or computed aggregates.
    kpi_items = [_kpi_item(k) for k in (kpis or [])]
    if not kpi_items:
        kpi_items = _computed_kpis(columns, norm_rows)
    if kpi_items:
        blocks.append(DocumentBlock(type="kpi_grid", items=kpi_items[:6]))

    # 4) Data-driven sections (the heart of "dynamic").
    kinds = _column_kinds(columns, norm_rows) if columns else {}
    numeric_cols = [c for c, k in kinds.items() if k == "numeric"]
    cat_cols = [c for c, k in kinds.items() if k == "categorical"]
    date_cols = [c for c in kinds if kinds[c] == "datetime"]

    # 4a-0) Executive / CEO view: lead with a period-over-period comparison
    # table (e.g. month-over-month) so the leadership reader sees the delta
    # immediately, before the trend chart. Data-driven: only when a time
    # dimension with 2+ periods and a numeric measure exist.
    if perspective["tone"] == "executive" and date_cols and numeric_cols:
        _pc = _period_comparison(norm_rows, columns.index(date_cols[0]),
                                 columns.index(numeric_cols[0]))
        if _pc:
            blocks.append(DocumentBlock(
                type="data_table",
                title="月度核心指标对比 (Period-over-Period)",
                columns=["周期", "数值", "环比变动 (MoM)"],
                rows=_pc,
            ))

    # 4a) Trend over time (datetime + numeric).
    if date_cols and numeric_cols:
        dcol = date_cols[0]
        ncol = numeric_cols[0]
        di = columns.index(dcol)
        ni = columns.index(ncol)
        paired = _time_series(norm_rows, di, ni)
        if len(paired) >= 2:
            blocks.append(DocumentBlock(type="section_divider",
                                        title="Trend Over Time",
                                        subtitle=f"{ncol} across {dcol}"))
            blocks.append(DocumentBlock(
                type="chart", chart_type="line",
                title=f"{ncol} over {dcol}",
                chart={"type": "line", "x_label": dcol, "y_label": ncol,
                       "x": [p[0] for p in paired], "y": [p[1] for p in paired]}))
            blocks.append(DocumentBlock(
                type="paragraph", text=_trend_narrative(dcol, ncol, paired)))

    # 4b) Ranking / top performers (categorical + numeric).
    if cat_cols and numeric_cols:
        ccol = cat_cols[0]
        ncol = numeric_cols[0]
        ci = columns.index(ccol)
        ni = columns.index(ncol)
        top = _topn_by_numeric(norm_rows, ci, ni, n=10)
        if top:
            blocks.append(DocumentBlock(type="section_divider",
                                        title=f"Top {ccol.title()}",
                                        subtitle=f"Ranked by {ncol}"))
            blocks.append(DocumentBlock(
                type="chart", chart_type="bar",
                title=f"Top {len(top)} {ccol} by {ncol}",
                chart={"type": "bar", "x_label": ccol, "y_label": ncol,
                       "x": [t[0] for t in top], "y": [t[1] for t in top]}))
            blocks.append(DocumentBlock(
                type="data_table",
                title=f"Ranking — {ccol} by {ncol}",
                columns=[ccol, ncol],
                rows=[[t[0], _fmt(t[1])] for t in top]))
            blocks.append(DocumentBlock(
                type="paragraph", text=_ranking_narrative(ccol, ncol, top)))

    # 4c) Distribution (low-cardinality categorical + optional numeric).
    if cat_cols:
        ccol = cat_cols[0]
        ci = columns.index(ccol)
        dist = _distribution(norm_rows, ci)
        if 2 <= len(dist) <= 12 and not (date_cols and numeric_cols):
            blocks.append(DocumentBlock(type="section_divider",
                                        title=f"{ccol.title()} Distribution",
                                        subtitle="Share of records"))
            blocks.append(DocumentBlock(
                type="chart", chart_type="donut",
                title=f"{ccol} breakdown",
                chart={"type": "donut", "x": [d[0] for d in dist],
                       "y": [d[1] for d in dist]}))

    # 5) Key findings.
    finding_items = [_insight_item(f) for f in (findings or [])]
    if finding_items:
        blocks.append(DocumentBlock(type="section_divider", title="Key Findings"))
        blocks.append(DocumentBlock(type="findings", items=finding_items[:8]))

    # 6) Recommendations.
    rec_items = [_insight_item(r, default_label="Action") for r in (recommendations or [])]
    if rec_items:
        _rec_title = ("管理层决策与战略建议 (CEO Action Items)"
                      if perspective["tone"] == "executive" else "Recommendations")
        blocks.append(DocumentBlock(type="section_divider", title=_rec_title))
        blocks.append(DocumentBlock(type="recommendations", items=rec_items[:8]))

    # 7) Methodology.
    if columns and norm_rows:
        blocks.append(DocumentBlock(
            type="methodology",
            title="Methodology",
            text=(
                f"Analysis based on {len(norm_rows)} records across "
                f"{len(columns)} fields ({', '.join(columns[:8])}"
                f"{'…' if len(columns) > 8 else ''}). "
                f"Figures are computed directly from the retrieved data."
            )))

    # 8) Appendix — capped raw data (depth scales with audience).
    cap = perspective["appendix_cap"]
    if norm_rows and columns and perspective["tone"] != "executive":
        appendix_rows = norm_rows[:cap]
        blocks.append(DocumentBlock(
            type="appendix", title="Appendix — Source Data",
            columns=list(columns),
            rows=[[str(c) for c in r] for r in appendix_rows]))

    return DocumentPlan(
        title=title or "Report",
        subtitle=_cover_subtitle(columns, norm_rows, request_text, perspective),
        theme=theme,
        blocks=blocks,
        meta={"perspective": perspective["tone"]},
    )


# ---------------------------------------------------------------------------
# Narrative / item builders
# ---------------------------------------------------------------------------

def _cover_subtitle(columns, rows, request_text, perspective=None):
    tone = (perspective or {}).get("tone") if isinstance(perspective, dict) else None
    if tone == "executive":
        # CEO decision-report framing — the audience is explicit.
        return "致：首席执行官（CEO）"
    if columns and rows:
        return f"{len(rows):,} records · {len(columns)} fields"
    return ""


def _default_summary(columns, rows, perspective):
    if not columns or not rows:
        return ""
    numeric_cols = [c for c in columns]
    parts = [f"This report covers {len(rows):,} records across {len(columns)} fields."]
    if perspective["tone"] == "executive":
        parts.append("Key figures and highlights are summarized in the sections below.")
    return " ".join(parts)


def _kpi_item(k):
    if isinstance(k, dict):
        return {"label": k.get("label") or k.get("name") or "KPI",
                "value": k.get("value") if k.get("value") is not None else k.get("display", ""),
                "delta": k.get("delta") or "", "caption": k.get("caption") or ""}
    return {"label": getattr(k, "label", "KPI"),
            "value": getattr(k, "value", "") or getattr(k, "display", ""),
            "delta": getattr(k, "delta", "") or "", "caption": getattr(k, "caption", "") or ""}


def _computed_kpis(columns, rows):
    if not columns or not rows:
        return []
    kinds = _column_kinds(columns, rows)
    numeric = [c for c, k in kinds.items() if k == "numeric"]
    items = []
    if numeric:
        ni = columns.index(numeric[0])
        agg = _aggregate_numeric(rows, ni)
        if agg:
            items.append({"label": f"Total {numeric[0]}", "value": _fmt(agg["sum"]),
                          "caption": f"avg {_fmt(agg['avg'])}"})
            items.append({"label": f"Max {numeric[0]}", "value": _fmt(agg["max"]),
                          "caption": f"min {_fmt(agg['min'])}"})
    items.append({"label": "Records", "value": _fmt(len(rows)), "caption": f"{len(columns)} fields"})
    return items[:6]


def _time_series(rows, di, ni):
    out = []
    seen = set()
    for r in rows:
        if di >= len(r) or ni >= len(r):
            continue
        d = str(r[di])
        v = _coerce_num(r[ni])
        if v is None or d in seen:
            continue
        seen.add(d)
        out.append((d, v))
    return out[:24]


def _period_comparison(norm_rows, di, ni):
    """Aggregate a numeric measure per time-period and compute period-over-period
    (MoM) deltas. Returns ``[[period, value, delta%], ...]`` or ``None`` when
    there are fewer than two periods. Used by the executive (CEO) view to show
    a monthly core-metrics comparison table.
    """
    agg: dict[str, float] = {}
    order: list[str] = []
    for r in norm_rows:
        if di >= len(r) or ni >= len(r):
            continue
        d = str(r[di])
        v = _coerce_num(r[ni])
        if v is None:
            continue
        if d not in agg:
            agg[d] = 0.0
            order.append(d)
        agg[d] += v
    if len(order) < 2:
        return None
    rows_out: list[list[str]] = []
    prev: Optional[float] = None
    for d in order:
        v = agg[d]
        if prev is None:
            rows_out.append([d, _fmt(v), "—"])
        else:
            delta = (v - prev) / prev * 100 if prev else 0.0
            rows_out.append([d, _fmt(v), f"{delta:+.1f}%"])
        prev = v
    return rows_out



def _distribution(rows, ci):
    counts: dict[str, int] = {}
    for r in rows:
        if ci >= len(r):
            continue
        k = str(r[ci])
        counts[k] = counts.get(k, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)


def _trend_narrative(dcol, ncol, paired):
    first_v, last_v = paired[0][1], paired[-1][1]
    if first_v:
        pct = (last_v - first_v) / abs(first_v) * 100
        direction = "up" if pct >= 0 else "down"
        return (
            f"{ncol} moved from {_fmt(first_v)} to {_fmt(last_v)} across the "
            f"{len(paired)} {dcol} points shown — a {abs(pct):.1f}% change "
            f"({direction})."
        )
    return f"{ncol} trend across {dcol} is shown in the chart above."


def _ranking_narrative(ccol, ncol, top):
    if not top:
        return ""
    top_label, top_val = top[0]
    return (
        f"{top_label} leads with {_fmt(top_val)} {ncol}, followed by "
        f"{top[1][0]} ({_fmt(top[1][1])})" if len(top) > 1 else
        f"{top_label} is the top entry at {_fmt(top_val)} {ncol}."
    )


def _insight_item(x, default_label=""):
    # Plain strings are the most common shape an LLM/agent emits for a
    # finding or recommendation — map them to {label, text} so the prose is
    # never silently dropped (the previous else-branch read .text/.icon off a
    # str, which are always absent, leaving the bullet empty).
    if isinstance(x, str):
        return {"label": default_label, "text": x}
    if isinstance(x, dict):
        return {"label": x.get("label") or x.get("icon") or default_label,
                "text": x.get("text") or ""}
    # Dataclass / arbitrary object (e.g. a Pydantic model).
    return {"label": getattr(x, "icon", "") or getattr(x, "label", "") or default_label,
            "text": getattr(x, "text", "") or ""}


__all__ = ["synthesize_plan"]
