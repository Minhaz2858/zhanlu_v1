"""_report_auto_analysis — server-side auto-analysis for create_artifact.

When a chat agent asks for a Word/PPT/PDF report, the LLM is often unreliable
about populating the full ReportCard shape (summary, kpis, key_findings,
recommendations, sections) — it tends to dump raw rows into ``chart.data``
and call it done. The result is a doc with just an "Instructions" header
and a 25-row truncated data table.

This module is the safety net. It takes the rows the data tool actually
returned and deterministically derives:

  * ``summary``       – 2–4 sentence narrative built from real KPIs.
  * ``kpis``          – row count, distinct counts, sums, means.
  * ``key_findings``  – top 3–5 derived facts (top categories by sum, etc.).
  * ``recommendations`` – 2–3 actionable hints derived from the data shape.
  * ``chart``         – a 2-col aggregated view (never raw 100+ row dump).
  * ``sections``      – top-N breakdown sections by the strongest columns.

It is best-effort: any failure logs a warning and returns ``{}`` so the
report still renders with whatever the LLM provided.

DB-agnostic, no network calls, no extra deps beyond pandas (with a
pure-python fallback if pandas import fails).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Tunable constants — kept here so they're easy to find / adjust.
MAX_BREAKDOWN_ROWS = 10          # top-N for each breakdown section
MAX_KEY_FINDINGS = 5
MAX_RECOMMENDATIONS = 4
CHART_MAX_CATEGORIES = 20        # cap chart x-axis to keep it readable
RAW_DUMP_ROW_THRESHOLD = 25      # if cached rows > this, never emit as raw chart.data


def auto_analyze(rows: Any, columns: Any, *, tool_name: str = "data tool",
                 title_hint: str | None = None) -> dict:
    """Return a ReportCard-shaped dict derived from raw rows.

    The returned dict contains ONLY the fields the docx exporter consumes
    (``summary, kpis, key_findings, recommendations, chart, sections,
    methodology, title``). Existing keys the LLM passed in still win —
    the caller should ``{**auto_analyze(...), **llm_payload}`` if you
    want LLM values to override.
    """
    if not rows or not columns:
        return {}

    cols = [str(c) for c in columns]
    # rows may be list[dict] or list[list] — normalise to list[dict].
    norm_rows = _normalize_rows(rows, cols)
    if not norm_rows:
        return {}

    try:
        return _auto_analyze_pandas(norm_rows, cols, tool_name=tool_name,
                                     title_hint=title_hint)
    except ImportError:
        return _auto_analyze_plain(norm_rows, cols, tool_name=tool_name,
                                   title_hint=title_hint)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto_analyze failed: %s; falling back to plain",
                       exc)
        try:
            return _auto_analyze_plain(norm_rows, cols, tool_name=tool_name,
                                       title_hint=title_hint)
        except Exception as exc2:  # pragma: no cover
            logger.warning("auto_analyze plain fallback also failed: %s",
                           exc2)
            return {}


# ---------- internals ----------

def _normalize_rows(rows: Iterable, cols: list[str]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if isinstance(r, dict):
            out.append({k: r.get(k) for k in cols})
        elif isinstance(r, (list, tuple)):
            if len(r) >= len(cols):
                out.append({cols[i]: r[i] for i in range(len(cols))})
            else:
                # pad short rows with None so we don't crash
                out.append({cols[i]: (r[i] if i < len(r) else None)
                            for i in range(len(cols))})
        else:
            # scalar / unknown — skip
            continue
    return out


# ---------- pandas path ----------

def _auto_analyze_pandas(rows: list[dict], cols: list[str], *,
                         tool_name: str, title_hint: str | None) -> dict:
    import pandas as pd  # noqa: WPS433 — lazy import is intentional

    df = pd.DataFrame(rows)
    return _build_payload(df, pd, cols=cols, tool_name=tool_name,
                          title_hint=title_hint)


def _build_payload(df, pd, *, cols: list[str], tool_name: str,
                   title_hint: str | None) -> dict:
    n_rows = len(df)

    # Detect column roles.
    numeric_cols: list[str] = []
    datetime_cols: list[str] = []
    categorical_cols: list[str] = []
    for c in cols:
        if c not in df.columns:
            continue
        s = df[c]
        # datetime detection
        if pd.api.types.is_datetime64_any_dtype(s):
            datetime_cols.append(c)
            continue
        if s.dtype == object:
            # try a fuzzy parse
            try:
                parsed = pd.to_datetime(s, errors="coerce", utc=False)
                if parsed.notna().mean() >= 0.8:
                    df[c] = parsed
                    datetime_cols.append(c)
                    continue
            except Exception:
                pass
        # numeric detection
        if pd.api.types.is_numeric_dtype(s):
            numeric_cols.append(c)
            continue
        # try numeric coercion
        coerced = pd.to_numeric(s, errors="coerce")
        if coerced.notna().mean() >= 0.8:
            df[c] = coerced
            numeric_cols.append(c)
            continue
        categorical_cols.append(c)

    # Pick the "amount" / "primary measure" — heuristic: first numeric col
    # whose name matches a money/score pattern, else first numeric col.
    measure_col = _pick_primary_measure(df, numeric_cols)
    # Pick the "category" — first categorical (string/object) col.
    category_col = _pick_primary_category(categorical_cols)
    # Pick the "date" — first detected datetime col.
    date_col = datetime_cols[0] if datetime_cols else None

    # --- KPIs ---
    kpis: list[dict] = []
    kpis.append(_kpi("Total Records", f"{n_rows:,}",
                     caption=f"Rows returned by {tool_name}"))
    if measure_col is not None:
        total = float(df[measure_col].sum())
        mean = float(df[measure_col].mean())
        kpis.append(_kpi(f"Total {measure_col}", _fmt_number(total)))
        if n_rows > 0:
            kpis.append(_kpi(f"Average {measure_col}", _fmt_number(mean)))
    distinct_target_cols = [c for c in (category_col, date_col,
                                         categorical_cols[1] if len(categorical_cols) > 1 else None)
                            if c]
    seen_labels: set[str] = set()
    for c in distinct_target_cols:
        if c in seen_labels:
            continue
        seen_labels.add(c)
        try:
            ndistinct = int(df[c].nunique())
        except Exception:
            continue
        label = f"Distinct {c}" if c != measure_col else f"Unique {c}"
        if ndistinct > 0:
            kpis.append(_kpi(label, f"{ndistinct:,}"))

    # --- Summary ---
    summary = _build_summary(df, n_rows=n_rows, measure_col=measure_col,
                             category_col=category_col, date_col=date_col,
                             kpis=kpis, title_hint=title_hint)

    # --- Key findings ---
    key_findings = _build_key_findings(df, n_rows=n_rows,
                                       measure_col=measure_col,
                                       category_col=category_col,
                                       date_col=date_col,
                                       numeric_cols=numeric_cols,
                                       categorical_cols=categorical_cols)

    # --- Recommendations ---
    recommendations = _build_recommendations(df, n_rows=n_rows,
                                            measure_col=measure_col,
                                            category_col=category_col,
                                            date_col=date_col)

    # --- Chart (always aggregate; never raw 100+ row dump) ---
    chart = _build_chart(df, measure_col=measure_col,
                         category_col=category_col, date_col=date_col)

    # --- Sections ---
    sections = _build_sections(df, n_rows=n_rows,
                                measure_col=measure_col,
                                category_col=category_col,
                                date_col=date_col,
                                categorical_cols=categorical_cols,
                                numeric_cols=numeric_cols)

    # --- Methodology ---
    n_num = len(numeric_cols)
    n_cat = len(categorical_cols)
    n_dt = len(datetime_cols)
    methodology = (
        f"Auto-analysis of {n_rows:,} row(s) from {tool_name}. "
        f"Detected {n_num} numeric column(s), {n_cat} categorical, "
        f"and {n_dt} datetime column(s). "
        f"Primary measure: {measure_col or 'n/a'}. "
        f"Primary category: {category_col or 'n/a'}."
    )

    return {
        "title": title_hint or f"Report from {tool_name}",
        "summary": summary,
        "kpis": kpis,
        "key_findings": key_findings,
        "recommendations": recommendations,
        "chart": chart,
        "sections": sections,
        "methodology": methodology,
    }


# ---------- pure-python fallback ----------

def _auto_analyze_plain(rows: list[dict], cols: list[str], *,
                        tool_name: str, title_hint: str | None) -> dict:
    """Pure-Python fallback when pandas isn't usable.

    Computes row count, distinct counts on string cols, sum/mean on numeric
    cols. Produces a simpler payload than the pandas path.
    """
    n_rows = len(rows)
    numeric_cols: list[str] = []
    categorical_cols: list[str] = []
    for c in cols:
        sample = [r.get(c) for r in rows[:50] if r.get(c) is not None]
        if not sample:
            continue
        nums = 0
        for v in sample:
            try:
                float(v)
            except (TypeError, ValueError):
                pass
            else:
                nums += 1
        if nums >= max(1, len(sample) * 0.8):
            numeric_cols.append(c)
        else:
            categorical_cols.append(c)

    measure_col = _pick_primary_measure_plain(numeric_cols)
    category_col = _pick_primary_category(categorical_cols)

    kpis = [_kpi("Total Records", f"{n_rows:,}",
                 caption=f"Rows returned by {tool_name}")]
    if measure_col:
        vals = []
        for r in rows:
            v = r.get(measure_col)
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        if vals:
            total = sum(vals)
            mean = total / len(vals)
            kpis.append(_kpi(f"Total {measure_col}", _fmt_number(total)))
            kpis.append(_kpi(f"Average {measure_col}", _fmt_number(mean)))

    distinct_target = [c for c in (category_col,
                                    categorical_cols[1] if len(categorical_cols) > 1 else None)
                       if c]
    seen: set[str] = set()
    for c in distinct_target:
        if c in seen:
            continue
        seen.add(c)
        vals = {str(r.get(c)) for r in rows if r.get(c) is not None}
        if vals:
            kpis.append(_kpi(f"Distinct {c}", f"{len(vals):,}"))

    # Top categories by measure
    top_breakdown: list[tuple[str, float]] = []
    if measure_col and category_col:
        sums: dict[str, float] = {}
        for r in rows:
            cat = r.get(category_col)
            val = r.get(measure_col)
            try:
                val_f = float(val)
            except (TypeError, ValueError):
                continue
            key = str(cat) if cat is not None else "(unknown)"
            sums[key] = sums.get(key, 0.0) + val_f
        top_breakdown = sorted(sums.items(), key=lambda kv: kv[1],
                               reverse=True)[:MAX_BREAKDOWN_ROWS]

    summary = (
        f"Report covers {n_rows:,} record(s) from {tool_name}. "
        + (f"Total {measure_col} is {_fmt_number(sum(float(r.get(measure_col)) for r in rows if _is_num(r.get(measure_col))))}."
           if measure_col else "")
        + (f" {len(top_breakdown)} distinct '{category_col}' group(s)."
           if category_col and top_breakdown else "")
    ).strip()

    key_findings: list[dict] = []
    if measure_col and category_col:
        for cat, val in top_breakdown[:MAX_KEY_FINDINGS]:
            key_findings.append({"text":
                f"{category_col} '{cat}' contributes {_fmt_number(val)} to total {measure_col}."
            })

    sections: list[dict] = []
    if measure_col and category_col and top_breakdown:
        bullets = [f"{cat}: {_fmt_number(val)}"
                   for cat, val in top_breakdown[:MAX_BREAKDOWN_ROWS]]
        total_top = sum(v for _, v in top_breakdown[:MAX_BREAKDOWN_ROWS])
        sections.append({
            "title": f"Top {min(len(top_breakdown), MAX_BREAKDOWN_ROWS)} {category_col} by {measure_col}",
            "content": (f"The table below ranks the leading {category_col} "
                        f"groups by total {measure_col}."),
            "bullets": bullets,
            "type": "data",
        })

    return {
        "title": title_hint or f"Report from {tool_name}",
        "summary": summary,
        "kpis": kpis,
        "key_findings": key_findings,
        "recommendations": [],
        "chart": _build_chart_plain(rows, measure_col=measure_col,
                                    category_col=category_col),
        "sections": sections,
        "methodology": f"Plain-Python analysis of {n_rows:,} row(s) from {tool_name}.",
    }


# ---------- builders ----------

def _build_summary(df, *, n_rows: int, measure_col: str | None,
                   category_col: str | None, date_col: str | None,
                   kpis: list[dict], title_hint: str | None) -> str:
    parts: list[str] = []
    parts.append(
        f"This report covers **{n_rows:,} records** retrieved from the data source."
    )
    if measure_col:
        try:
            total = float(df[measure_col].sum())
            mean = float(df[measure_col].mean())
            parts.append(
                f"Total **{measure_col}** is **{_fmt_number(total)}** "
                f"(average per record: **{_fmt_number(mean)}**)."
            )
        except Exception:
            pass
    if category_col:
        try:
            n_cat = int(df[category_col].nunique())
            parts.append(f"Records span **{n_cat} distinct "
                         f"{category_col}** value(s).")
        except Exception:
            pass
    if date_col:
        try:
            d_min = df[date_col].min()
            d_max = df[date_col].max()
            if hasattr(d_min, "strftime") and hasattr(d_max, "strftime"):
                if d_min == d_max or pd_is_na(d_min) or pd_is_na(d_max):
                    parts.append(f"All records dated **{d_max}**.")
                else:
                    parts.append(
                        f"Period covered: **{d_min} → {d_max}**.")
        except Exception:
            pass
    return " ".join(parts)


def _build_key_findings(df, *, n_rows: int, measure_col: str | None,
                        category_col: str | None,
                        date_col: str | None,
                        numeric_cols: list[str],
                        categorical_cols: list[str]) -> list[dict]:
    findings: list[dict] = []
    if not measure_col or not category_col:
        return findings

    # Top categories by total measure.
    grp = df.groupby(category_col, dropna=False)[measure_col].sum(
        numeric_only=True).sort_values(ascending=False)
    if not grp.empty:
        top_cat = str(grp.index[0])
        top_val = float(grp.iloc[0])
        total = float(grp.sum())
        share = (top_val / total * 100.0) if total else 0.0
        findings.append({
            "text": (
                f"Largest {category_col} by total {measure_col}: "
                f"**{top_cat}** with **{_fmt_number(top_val)}** "
                f"({share:.1f}% of grand total)."
            )
        })
        # Second and third for context.
        for i in range(1, min(3, len(grp))):
            cat = str(grp.index[i])
            val = float(grp.iloc[i])
            share = (val / total * 100.0) if total else 0.0
            findings.append({
                "text": (f"#{i + 1} {category_col}: **{cat}** — "
                         f"{_fmt_number(val)} ({share:.1f}%).")
            })

    # Top contributor (record-level) for variability narratives.
    if numeric_cols and categorical_cols:
        try:
            idx = df[measure_col].idxmax()
            row = df.loc[idx]
            label_parts = []
            for c in categorical_cols[:3]:
                if c in row.index:
                    label_parts.append(f"{c}={row[c]}")
            label = ", ".join(label_parts) or f"row #{idx}"
            findings.append({
                "text": (f"Highest single {measure_col} value: "
                         f"**{_fmt_number(float(row[measure_col]))}** "
                         f"({label}).")
            })
        except Exception:
            pass

    if date_col and measure_col:
        try:
            grp_d = df.groupby(date_col)[measure_col].sum().sort_values(
                ascending=False)
            if not grp_d.empty:
                d_top = grp_d.index[0]
                d_val = float(grp_d.iloc[0])
                findings.append({
                    "text": (f"Peak day for {measure_col}: "
                             f"**{d_top}** with {_fmt_number(d_val)}.")
                })
        except Exception:
            pass

    return findings[:MAX_KEY_FINDINGS]


def _build_recommendations(df, *, n_rows: int, measure_col: str | None,
                           category_col: str | None,
                           date_col: str | None) -> list[dict]:
    recs: list[dict] = []
    if not measure_col or not category_col:
        return recs

    # Concentration insight
    try:
        grp = df.groupby(category_col)[measure_col].sum().sort_values(
            ascending=False)
        if len(grp) >= 2:
            total = float(grp.sum())
            top_share = float(grp.iloc[0]) / total * 100.0 if total else 0.0
            if top_share >= 40.0:
                recs.append({
                    "text": (f"Concentration risk: the top {category_col} "
                             f"accounts for {top_share:.1f}% of {measure_col}. "
                             f"Consider diversification or stronger engagement "
                             f"to retain this segment.")
                })
            else:
                recs.append({
                    "text": (f"Healthy spread: the top {category_col} is "
                             f"{top_share:.1f}% of total {measure_col}. "
                             f"Continue monitoring to keep diversification.")
                })
    except Exception:
        pass

    # Tail / long-tail
    try:
        grp = df.groupby(category_col)[measure_col].sum().sort_values(
            ascending=False)
        n_distinct = len(grp)
        if n_distinct >= 5:
            tail_share = float(grp.iloc[-max(1, n_distinct // 5):].sum()) \
                / float(grp.sum()) * 100.0
            if tail_share < 5.0:
                recs.append({
                    "text": (f"Long-tail alert: the bottom 20% of "
                             f"{category_col} values contribute only "
                             f"{tail_share:.1f}% of {measure_col}. "
                             f"Investigate whether these are worth "
                             f"maintaining or candidates to deprioritise.")
                })
    except Exception:
        pass

    # Period rec
    if date_col:
        try:
            d_min = df[date_col].min()
            d_max = df[date_col].max()
            if hasattr(d_min, "strftime") and hasattr(d_max, "strftime") \
                    and d_min != d_max:
                recs.append({
                    "text": (f"Comparison baseline: re-run the same query "
                             f"for the prior period ({d_min} → {d_max}) to "
                             f"compute period-over-period deltas.")
                })
        except Exception:
            pass

    return recs[:MAX_RECOMMENDATIONS]


def _build_chart(df, *, measure_col: str | None,
                 category_col: str | None,
                 date_col: str | None) -> dict | None:
    if not measure_col:
        return None

    # Prefer (category, sum(measure)) as a clean bar chart.
    if category_col:
        grp = df.groupby(category_col, dropna=False)[measure_col].sum(
            numeric_only=True).sort_values(ascending=False).head(
            CHART_MAX_CATEGORIES)
        if not grp.empty:
            data = [
                {category_col: str(idx), measure_col: float(val) if val is not None else 0.0}
                for idx, val in grp.items()
            ]
            return {
                "type": "bar",
                "title": f"{measure_col} by {category_col}",
                "x_key": category_col,
                "y_keys": [measure_col],
                "data": data,
            }

    # Fallback: time-series of total measure per day.
    if date_col:
        grp = df.groupby(date_col)[measure_col].sum().sort_index()
        if not grp.empty:
            data = [
                {date_col: str(idx)[:10], measure_col: float(val) if val is not None else 0.0}
                for idx, val in grp.items()
            ]
            return {
                "type": "line",
                "title": f"{measure_col} over time",
                "x_key": date_col,
                "y_keys": [measure_col],
                "data": data,
            }

    return None


def _build_chart_plain(rows: list[dict], *, measure_col: str | None,
                       category_col: str | None) -> dict | None:
    if not measure_col or not category_col:
        return None
    sums: dict[str, float] = {}
    for r in rows:
        cat = r.get(category_col)
        try:
            v = float(r.get(measure_col))
        except (TypeError, ValueError):
            continue
        key = str(cat) if cat is not None else "(unknown)"
        sums[key] = sums.get(key, 0.0) + v
    if not sums:
        return None
    sorted_items = sorted(sums.items(), key=lambda kv: kv[1],
                          reverse=True)[:CHART_MAX_CATEGORIES]
    data = [{category_col: k, measure_col: v} for k, v in sorted_items]
    return {
        "type": "bar",
        "title": f"{measure_col} by {category_col}",
        "x_key": category_col,
        "y_keys": [measure_col],
        "data": data,
    }


def _build_sections(df, *, n_rows: int, measure_col: str | None,
                    category_col: str | None,
                    date_col: str | None,
                    categorical_cols: list[str],
                    numeric_cols: list[str]) -> list[dict]:
    sections: list[dict] = []
    if not measure_col:
        return sections

    # Top-N breakdown by primary category.
    if category_col:
        try:
            grp = df.groupby(category_col, dropna=False)[measure_col].sum(
                numeric_only=True).sort_values(ascending=False)
            top = grp.head(MAX_BREAKDOWN_ROWS)
            total = float(grp.sum())
            bullets = []
            for cat, val in top.items():
                try:
                    _vf = float(val)
                except (TypeError, ValueError):
                    continue
                import math
                if math.isnan(_vf) or math.isinf(_vf):
                    continue
                share = (_vf / total * 100.0) if total else 0.0
                bullets.append(
                    f"{cat}: {_fmt_number(_vf)} ({share:.1f}%)"
                )
            sections.append({
                "title": (f"Top {len(top)} {category_col} groups "
                          f"by total {measure_col}"),
                "content": (f"The chart and bullets below rank the leading "
                            f"{category_col} groups by their total {measure_col} "
                            f"across the {n_rows:,} records."),
                "bullets": bullets,
                "type": "data",
            })
        except Exception:
            pass

    # Second categorical breakdown (if present).
    if len(categorical_cols) >= 2 and categorical_cols[1] != category_col:
        cat2 = categorical_cols[1]
        try:
            grp = df.groupby(cat2, dropna=False)[measure_col].sum(
                numeric_only=True).sort_values(ascending=False).head(
                MAX_BREAKDOWN_ROWS)
            if not grp.empty:
                _bullets = []
                for cat, val in grp.items():
                    try:
                        _vf = float(val)
                    except (TypeError, ValueError):
                        continue
                    import math
                    if math.isnan(_vf) or math.isinf(_vf):
                        continue
                    _bullets.append(f"{cat}: {_fmt_number(_vf)}")
                if _bullets:
                    sections.append({
                        "title": (f"Top {len(grp)} {cat2} groups by total "
                                  f"{measure_col}"),
                        "content": (f"This view breaks the same {measure_col} total "
                                    f"by {cat2} (the second-most-informative "
                                    f"dimension detected)."),
                        "bullets": _bullets,
                        "type": "data",
                    })
        except Exception:
            pass

    # Daily trend section when a date column exists.
    if date_col:
        try:
            grp = df.groupby(date_col)[measure_col].sum().sort_index()
            if not grp.empty and len(grp) >= 2:
                top = grp.sort_values(ascending=False).head(3)
                _t_bullets = []
                for idx, val in top.items():
                    try:
                        _vf = float(val)
                    except (TypeError, ValueError):
                        continue
                    import math
                    if math.isnan(_vf) or math.isinf(_vf):
                        continue
                    _t_bullets.append(f"{idx}: {_fmt_number(_vf)}")
                if _t_bullets:
                    sections.append({
                    "title": f"Daily trend of {measure_col}",
                    "content": (f"Day-by-day totals of {measure_col} across "
                                f"the {len(grp)} covered date(s). The leading "
                                f"days are listed below."),
                    "bullets": _t_bullets,
                    "type": "data",
                })
        except Exception:
            pass

    return sections


# ---------- helpers ----------

def _pick_primary_measure(df, numeric_cols: list[str]) -> str | None:
    """Heuristic: prefer 'money'-shaped column names over arbitrary numerics."""
    money_patterns = [
        re.compile(r"total.*amount", re.I),
        re.compile(r"amount", re.I),
        re.compile(r"revenue", re.I),
        re.compile(r"sales", re.I),
        re.compile(r"price", re.I),
        re.compile(r"value", re.I),
        re.compile(r"qty|quantity", re.I),
        re.compile(r"count", re.I),
        re.compile(r"score", re.I),
    ]
    for pat in money_patterns:
        for c in numeric_cols:
            if pat.search(c):
                return c
    return numeric_cols[0] if numeric_cols else None


def _pick_primary_measure_plain(numeric_cols: list[str]) -> str | None:
    money_patterns = [
        re.compile(r"total.*amount", re.I),
        re.compile(r"amount", re.I),
        re.compile(r"revenue", re.I),
        re.compile(r"sales", re.I),
        re.compile(r"price", re.I),
        re.compile(r"value", re.I),
        re.compile(r"qty|quantity", re.I),
        re.compile(r"count", re.I),
    ]
    for pat in money_patterns:
        for c in numeric_cols:
            if pat.search(c):
                return c
    return numeric_cols[0] if numeric_cols else None


def _pick_primary_category(categorical_cols: list[str]) -> str | None:
    """Heuristic: prefer entity-like column names (product, customer, ...)."""
    nice_patterns = [
        re.compile(r"^product", re.I),
        re.compile(r"^customer", re.I),
        re.compile(r"^vendor|^supplier", re.I),
        re.compile(r"^region|^country", re.I),
        re.compile(r"^category|^type", re.I),
        re.compile(r"^name", re.I),
    ]
    for pat in nice_patterns:
        for c in categorical_cols:
            if pat.search(c):
                return c
    return categorical_cols[0] if categorical_cols else None


def _kpi(label: str, value: Any, *, caption: str | None = None) -> dict:
    return {"label": label, "value": value, "caption": caption}


def _fmt_number(value: float | int) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Guard against NaN / Inf from bad coercion (e.g. float("16.96M")
    # or pandas aggregation over non-numeric data).
    import math
    if math.isnan(v) or math.isinf(v):
        return "\u2014"  # em-dash: clean "no data" marker, never literal "nan"
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def pd_is_na(v) -> bool:
    try:
        return bool(pd.isna(v))
    except Exception:
        return False
