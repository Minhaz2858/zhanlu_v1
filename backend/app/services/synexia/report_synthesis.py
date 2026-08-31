"""Report synthesis — the forced second LLM turn that writes the report.

The data-agent sub-loop returns rows + SQL, but its narrative field
(`answer`) is unreliable: the LLM often stops after `execute_query`
without writing prose.  Worse, the calling agent's prompt is shaped
to prevent fabrication, which trains the LLM to stop after the tool
call.

This module forces a SECOND LLM turn in the calling-agent loop, with:

- no tool_choice (so it can't call `ask_data_agent` again)
- a system nudge that REQUIRES the response to contain:
    * a 1-2 sentence direct answer
    * a `chart_spec` JSON block (consumed by ReportCard.jsx)
    * 3-5 insight bullets
    * a follow-up recommendation chip
- the raw rows and SQL are included in the prompt so the LLM can ground
  every claim in real data

The result is a `ReportCardPayload` plus an `assistant_content` string.
Both are attached to the calling agent's tool-call record so the
frontend's MessageBubble can render them in place of the empty
DataTableCard.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.synexia.chart_quality import (
    pick_chart_columns as _pick_chart_columns,
    validate_chart_spec as _validate_chart_spec,
)
from app.services.synexia.contracts import (
    ActionSpec,
    ChartSpec,
    FinalizeResult,
    InsightSpec,
    KPISpec,
    ReportCardPayload,
)
from app.services.synexia.user_signal import detect_user_signal

logger = logging.getLogger(__name__)


# JSON block regex: matches ```json ... ``` or ``` ... ``` inside the
# LLM's reply.  We extract the chart_spec + report payload from this.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


# ── Task-type detection ──────────────────────────────────────────────
# Keyword → task_type mapping for the synthesis prompt.  The task type
# drives which KPI suggestions and column-name patterns the LLM (and
# the fallback) should prefer.
_TASK_KEYWORDS: list[tuple[str, str]] = [
    ("contract|履约|合同|fulfil|execution.rate", "contract"),
    ("sales|revenue|revenu|revenue|turnover|pipeline|deal|opportunity|crm", "sales"),
    ("inventory|stock|instock|warehouse|sku|supply|on.hand", "inventory"),
    ("customer|client|account|lead|prospect|contact|segment|retention|churn", "customer"),
    ("trend|time.series|monthly|quarterly|yearly|weekly|daily|over.time|period", "time_series"),
    ("expense|cost|budget|finance|profit|margin|p&l", "financial"),
]

def _detect_task_type(user_message: str) -> str:
    """Detect the report task type from the user message.

    Returns one of: ``"sales"``, ``"inventory"``, ``"customer"``,
    ``"time_series"``, ``"financial"``, or ``"general"``.
    """
    lower = user_message.lower()
    for pattern, task_type in _TASK_KEYWORDS:
        if re.search(pattern, lower):
            return task_type
    return "general"


# ── Column classification helpers for data-shape guard ──────────────
_DATE_KEYWORDS = frozenset({
    "date", "time", "timestamp", "period", "month", "year", "quarter",
    "datetime", "created_at", "updated_at", "dt", "sale_date",
    "order_date", "entry_date",
})
_LABEL_KEYWORDS = frozenset({
    "name", "title", "label", "category", "segment", "region", "type",
    "material", "product", "customer", "supplier", "warehouse", "sku",
    "id", "code", "status", "department", "channel", "brand",
})


def _is_date_column(col_name: str) -> bool:
    """Heuristic: does this column name look like a date/time field?"""
    lower = col_name.lower().replace("_", "").replace(" ", "")
    return any(kw in lower for kw in _DATE_KEYWORDS)


def _is_label_column(col_name: str, rows: list[dict]) -> bool:
    """Heuristic: is this column a categorical label (not numeric, not date)?"""
    lower = col_name.lower().replace("_", "")
    if any(kw in lower for kw in _DATE_KEYWORDS):
        return False
    if any(kw in lower for kw in _LABEL_KEYWORDS):
        return True
    # Fallback: check whether values are strings (label) or numbers (value)
    sample_values = [r.get(col_name) for r in rows[:20] if r.get(col_name) is not None]
    if not sample_values:
        return False
    str_count = sum(1 for v in sample_values if isinstance(v, str))
    return str_count > len(sample_values) * 0.5


def _is_value_column(col_name: str, rows: list[dict]) -> bool:
    """Heuristic: is this column numeric (a measure/value)?"""
    lower = col_name.lower().replace("_", "")
    if any(kw in lower for kw in _DATE_KEYWORDS):
        return False
    sample_values = [r.get(col_name) for r in rows[:20] if r.get(col_name) is not None]
    if not sample_values:
        return False
    num_count = sum(1 for v in sample_values if isinstance(v, (int, float)))
    return num_count == len(sample_values)


_SYNTHESIS_SYSTEM_PROMPT = """\
You are the REPORT SYNTHESIZER for a professional data-driven analytics platform.
Your reports are read by executives, analysts, and operations managers who expect
depth, rigour, and actionable intelligence — NOT thin summaries.

You just received a data snapshot from the data agent.  Your job is to
produce a COMPREHENSIVE, WELL-STRUCTURED analytical report — not a thin
summary and definitely not just the raw table.

OUTPUT FORMAT (strict — the frontend parses this):
- Write the `assistant_content` (the user's spoken text answer). This is the
  TEXT the user reads in the chat bubble BEFORE the artifact card. It must
  always be a COMPREHENSIVE analytical narrative — NEVER a thin one-liner.
- When the user requests a file deliverable (PPT, DOCX, PDF), the
  `assistant_content` is what they read before seeing the file card, so it
  must give them the full analytical picture:
  * Restate what they asked for in your own words (1 sentence)
  * Summarize the key numbers from the rows (2-3 sentences with specific
    figures, percentages, or rankings drawn from the actual data)
  * Highlight 1-2 notable patterns or anomalies (1-2 sentences)
  * End with one concrete follow-up suggestion (1 sentence)
- For data-rich queries (multiple metrics, comparisons, many rows), provide
  a thorough analytical narrative: name key figures, rank top performers,
  highlight anomalies, compare periods, and flag risks or opportunities.
- NEVER fabricate data not present in the rows below.
- NEVER end `assistant_content` with "see the file" or "see below" — the
  artifact card is rendered separately, the prose must stand on its own
  as a complete analytical answer.
  * 3+ metrics or 10+ rows → 5-10 sentences covering top findings + comparison
  * Complex multi-table analysis → full structured narrative (15+ sentences)
  Always include at least one concrete number from the data in the text.
- Append a single fenced JSON block ```json ... ``` that matches the
  ReportCardPayload schema:

  {
    "title": "Sales report — top items by revenue",  // string
    "source": "database_table · data_source_name",     // string, may be empty — use actual source from rows
    "summary": "Top 7 items generated 189.3M (76% of total). Item A leads at 66.2M (35% share, 3x the runner-up). Month-over-month, Category X declined 12% while Category Y surged 28%.",  // 3-8 sentences, depth proportional to data
    "methodology": "Data sourced from the connected database covering the requested period. Rows aggregated by the relevant dimension with SUM(revenue) and SUM(quantity). Top-N analysis includes the highest-value items; tail is grouped as 'Other.'",  // REQUIRED — 2-4 sentences
    "kpis": [
      {"label": "Total revenue", "value": "189.3M CNY", "caption": "Top 7 materials · Jun–Jul 2026"},
      {"label": "Total quantity", "value": "11,210 tons", "caption": "Jun–Jul 2026"},
      {"label": "Top material share", "value": "35%", "caption": "Top material · 66.2M CNY"},
      {"label": "MoM change", "value": "+8.2%", "caption": "Jul vs Jun 2026"},
      {"label": "Concentration (HHI)", "value": "2,140", "caption": "Moderately concentrated"}
    ],
    "chart": {
      "type": "bar",                 // "bar" | "line" | "pie"
      "title": "Top materials by revenue (Jun–Jul 2026)",
      "x_key": "material_name",      // column from rows
      "y_keys": ["total_revenue"],   // one or more numeric columns
      "data": [<the actual rows>],   // copy the rows verbatim
      "unit": "CNY"
    },
    "insights": [
      {"icon": "trending-up",   "text": "Top 3 materials account for 76% of revenue — moderate concentration risk; single-source dependency on the top material warrants diversification review."},
      {"icon": "bar-chart-3",   "text": "The top material (66.2M) is 3.1x the runner-up (21.4M) — a classic Pareto distribution suggesting category leader economics."},
      {"icon": "zap",           "text": "The second-ranked material grew 28% MoM (Jul vs Jun), the fastest-growing material in the top 7 — potential shift in downstream demand mix."},
      {"icon": "trending-down", "text": "The top material declined 12% MoM despite holding #1 rank — monitor for sustained erosion or seasonal dip."},
      {"icon": "target",        "text": "The bottom 4 materials combined account for only 24% of revenue — evaluate SKU rationalization or bundling opportunities."}
    ],
    "key_findings": [
      {"icon": "shield-check",  "text": "Revenue is highly concentrated: the top material alone represents 35% of the top-7 total (66.2M of 189.3M CNY). Any supply disruption or price correction in this single SKU would materially impact the P&L."},
      {"icon": "shield-check",  "text": "Month-over-month trajectory is positive overall (+8.2% Jul vs Jun), but the composition is shifting — the traditional leader is declining while challengers are accelerating."},
      {"icon": "shield-check",  "text": "The Herfindahl-Hirschman Index (HHI) of 2,140 indicates a moderately concentrated portfolio. This is below the 2,500 antitrust threshold but above the 1,500 level where diversification benefits typically emerge."}
    ],
    "recommendations": [
      {"icon": "arrow-right",   "text": "Initiate dual-sourcing qualification for the top material to mitigate single-supplier concentration risk — target Q4 2026."},
      {"icon": "arrow-right",   "text": "Accelerate capacity planning for the fastest-growing material given its 28% MoM growth trajectory; if sustained, current allocation will constrain supply by Q1 2027."},
      {"icon": "arrow-right",   "text": "Run a 12-month trend analysis (Jul 2025–Jul 2026) to distinguish seasonal patterns from structural shifts — current 2-month window may over-fit transient signals."}
    ],
    "next_step": "Want to break this down by region, or extend the analysis to a 12-month window?",
    "actions": [
      {"label": "Break down by region", "prompt": "Break this down by region."},
      {"label": "12-month trend",       "prompt": "Extend this analysis to the last 12 months."}
    ]
  }

RULES (these are the anti-hallucination rules — applied to the
NARRATIVE part, not the chart part):
1. Every number, name, and category you mention MUST come from the
   data snapshot below.  No invented customers, no invented months.
2. The KPIs' `value` strings must be derived from the rows (totals,
   shares, counts, growth rates) — not from your own memory.
3. Insights MUST be: (a) derivable from the rows, (b) categorized
   implicitly by icon (trending-up=driver, zap=outlier, target=opportunity,
   trending-down=risk, bar-chart-3=structure, lightbulb=observation),
   (c) at least one sentence with analytical commentary, not bare data recitation.
4. If the rows are empty, set `kpis` to a single "no data" tile,
   `insights` to [{"icon": "info", "text": "No data found for this query."}],
   and `next_step` to "Try a different scope (region, time period, or product)."
   HOWEVER — if the user explicitly requested a file deliverable (PPT, DOCX,
   PDF) AND rows are empty, DO NOT just say "no data". Instead write a 5-7
   sentence `assistant_content` narrative that:
   (a) restates what the user asked for in your own words (e.g. "You asked
   for a July 2026 sales report covering volume, revenue, margin, and
   inventory delivered as PowerPoint"),
   (b) describes what was attempted (which data sources were queried, which
   time window was used, which dimensions were explored),
   (c) explains the gap honestly ("the bound data source returned no rows
   matching these filters — possibly because the table only holds data from
   a different period, or the column names differ from what was assumed"),
   (d) lists 3-5 concrete next-step suggestions the user can try (different
   time window, broader filters, alternate data source),
   (e) ends with a one-line recommendation for follow-up.
   Set `methodology` to describe the attempted query in plain English so it
   shows up in the file. Set `summary` to a single sentence acknowledging
   the gap. Set `kpis` to a single "no data" tile and `next_step` to one
   concrete retry suggestion. The file artifact is still produced even with
   no rows so the user has something to download.
5. EMIT AT LEAST 5 insights and AT LEAST 2 key_findings and AT LEAST 2
   recommendations.  Thin reports with <5 insights are rejected.
6. ALWAYS include `methodology` (2-4 sentences covering data source,
   time period, aggregation method, scope, and any caveats).
7. When a date/timestamp column is present, always compute and mention
   a comparison (MoM, YoY, period-over-period change) in both summary
   and at least one insight.

CONTRACT-PERFORMANCE GUIDANCE (when the user asks about contracts /
履约 / 合同 / contract performance):
- Use the contract vocabulary: contracted quantity, delivered (shipped)
  quantity, outstanding/remaining quantity, execution (fulfillment)
  rate = delivered ÷ contracted × 100, contracted value, delivered value.
- The headline KPI is the EXECUTION RATE and the OUTSTANDING volume —
  not just revenue. Flag contracts below 80% execution as risk items.
- Rank customers/materials by contracted value or contracted quantity.
- Mention MoM change in contracted/delivered volume when the data spans
  two months (prior-month vs current-month columns).
- NEVER describe contract rows as "sales orders" and never rename
  delivered qty to "revenue" — keep the contract semantics.

CHART RULES (these prevent the "all bars = 1" degenerate chart):
8. The chart's `data` array MUST use the EXACT column names present in
   the rows — never invent or fabricate new columns (e.g. do NOT add
   a synthetic "count": 1 per row). If the rows have no numeric
   measure suitable for charting, set `"chart": null` and explain in
   the summary why no chart is shown.
9. Before building `chart.data`, GROUP rows by the x-axis column and
   SUM the y-axis values. Duplicate categories MUST be aggregated
   (e.g., 3 rows of "EMEA"/100/50/80 → one row "EMEA"/230). Raw row-
   by-row data is only acceptable for line charts over time (one
   row per period, deduped by date).
10. Cap `chart.data` to the top 12 categories by y-value magnitude;
    any remainder must be merged into a single row with the x value
    "Other". Never emit more than 12 x-axis labels in a bar/pie chart.
11. If after inspection the snapshot has no meaningful numeric
    measure (e.g. it's a list of names only), set `"chart": null`
    and prefer textual insights instead.

CEO-GRADE 6-SECTION STRUCTURE (your report is read by the company CEO —
write it like an executive briefing, NOT a data dump):

The `assistant_content` must be organized as a 6-section narrative that
stands alone as a complete executive briefing. Use clear headings or
paragraph breaks for each section (Markdown formatting is rendered):

SECTION 1 — EXECUTIVE SUMMARY (2-3 sentences): Lead with the single most
important number of the period (total revenue / volume / headline metric),
the period-over-period change, and one strategic takeaway. Write like a
CEO's one-page brief opener — confident, specific, forward-looking.

SECTION 2 — KEY METRICS: Present the main metrics (revenue, volume,
margin, inventory, etc.) with concrete figures. Name the top performer
and its share. Use the numbers from the PRE-COMPUTED ANALYSIS block
below — they are exact aggregates computed from ALL rows.

SECTION 3 — BREAKDOWN BY DIMENSION: For EVERY dimension the user asked
about (e.g. "by product, by customer, by region"), give a ranked
breakdown: the top 3-5 items, each with its value and share, plus the
tail. Cover ALL requested dimensions — do not stop at product if the
user also asked for customer and region. If only one dimension was
requested, still give a top-N ranking.

SECTION 4 — ANOMALIES & RISKS (2-3 items): Flag data-quality issues
(nulls, negative values, outliers), concentration risk, margin alerts,
or data coverage gaps. Quote exact figures from the data.

SECTION 5 — RECOMMENDED ACTIONS (2-3 items): Executive-actionable
recommendations tied to the numbers above. Each must name the metric
it addresses.

SECTION 6 — APPENDIX NOTE (1-2 sentences): State the data source, time
period covered, and any caveats about data coverage (e.g. "Latest data
available is 2025-12-31; analysis uses the available data.").

Aim for 12-25 sentences total in the `assistant_content` for multi-
metric queries. Thin 1-paragraph answers are rejected.

MULTI-DIMENSION RULE (HARD): When the user asks for a breakdown "by X,
by Y, by Z" (e.g. "by product, by customer, by region"), you MUST cover
EVERY requested dimension in SECTION 3, each with its own ranked
breakdown. Never collapse a multi-dimensional request into a single
dimension. The PRE-COMPUTED ANALYSIS block contains top-N breakdowns
for each detected dimension — use them.

LANGUAGE: write the prose and insights in the same language the user
used.  If the user message is in English, respond in English.  If in
Chinese, respond in Chinese.  Keep field names in English (the schema
is English-keyed).

TASK-AWARE GUIDANCE (use the detected task type from the user message):

If the task is **sales** (user asks about sales / revenue / pipeline):
- KPIs: total revenue, total quantity, top performer + share, distinct products/customers, average deal size, MoM/YoY growth rate, concentration (HHI or top-3 share)
- Chart: bar or pie (top N by revenue/quantity); line if a date column exists
- Insights (5-8, categorized): concentration risk, top-N share, outlier products, growth/decline drivers, seasonal patterns, cross-sell opportunities
- key_findings: narrative analysis of revenue drivers, portfolio concentration, growth composition
- recommendations: diversification, pricing, capacity, or channel actions

If the task is **inventory** (user asks about stock / warehouse / SKU):
- KPIs: total stock count, low-stock SKU count (if a threshold column exists), distinct SKUs, average quantity per SKU, total value, turnover ratio
- Chart: bar (by category/warehouse); gauge-like summary if single value
- Insights (5-8): stock-out risk, overstock items, category distribution, turnover velocity, dead-stock identification
- key_findings: inventory health summary, risk hotspots, optimization opportunities
- recommendations: reorder points, SKU rationalization, safety-stock adjustments

If the task is **customer** (user asks about clients / accounts / leads):
- KPIs: total customers, active vs inactive, new this period, top segment, churn/retention rate if columns support it, average revenue per customer
- Chart: bar or pie (by segment/region); line if date column exists
- Insights (5-8): segment concentration, new-customer trend, at-risk accounts, lifetime value signals, regional patterns
- key_findings: customer base health, growth engine assessment, churn drivers
- recommendations: retention playbook, segment expansion, onboarding improvements

If the task is **time_series** (user asks about trends over time):
- KPIs: date range covered, total over period, peak value + date, trough value + date, average per period, growth rate, volatility (std dev or CV)
- Chart: ALWAYS line chart (date on X axis)
- Insights (5-8): peak/trough analysis, trend direction, anomaly dates, seasonal decomposition, YoY comparison, acceleration/deceleration signals
- key_findings: trend narrative, structural vs cyclical patterns, inflection points
- recommendations: forecast horizon, intervention timing, monitoring thresholds

If the task is **financial** (user asks about cost / budget / profit):
- KPIs: total cost/expense, total revenue (if both present: margin, margin %), top cost category, budget variance, cost-to-revenue ratio
- Chart: bar (by category); line for trends; waterfall for margin breakdown
- Insights (5-8): cost drivers, margin trend, budget overruns, efficiency ratios, one-off vs recurring costs
- key_findings: profitability narrative, cost structure analysis, margin pressure points
- recommendations: cost optimization, reallocation, hedging or budgeting adjustments

If the task is **general** (no specific type detected):
- KPIs: row count, any obvious aggregate (sum/count of numeric columns), top performer + share, distinct count of key column, any discoverable ratio
- Chart: bar if label+value columns exist; line if date column; otherwise omit
- Insights (5-8): top-N share, distribution shape, data completeness, outlier detection, correlation signals
- key_findings: data landscape summary, signal-vs-noise assessment, analytical pathways
- recommendations: data quality improvements, deeper-dive suggestions"""


def _format_money(v: Any) -> str:
    """Best-effort money formatter for KPI strings."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v) if v is not None else "—"
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.1f}M"
    if abs(f) >= 1_000:
        return f"{f / 1_000:.1f}K"
    return f"{f:,.2f}"


def _fallback_payload(
    *,
    user_message: str,
    rows: list[dict] | None,
    sql: str | None,
    source_name: str | None,
    user_signal: str,
    task_type: str = "general",
    warnings: list[str] | None = None,
) -> ReportCardPayload:
    """Heuristic payload when the LLM synthesis fails or returns garbage.

    This is the "we always emit SOMETHING" guarantee — the calling agent
    never shows up empty-handed.  KPI numbers are derived from the rows
    using obvious heuristics (largest column, sum, top-N share).

    ``task_type`` drives which column-name patterns are preferred when
    picking the label and value columns, so the fallback KPIs are more
    task-appropriate (e.g. revenue/quantity for sales, stock/count for
    inventory).
    """
    # Priority column-name patterns per task type.
    # Each entry: (task_type → (label_patterns, value_patterns))
    _TASK_COLUMN_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
        "contract": (
            ["name", "customer", "material", "product", "partner"],
            ["quantity", "qty", "amount", "value", "rate", "execution", "price", "revenue", "out"],
        ),
        "sales": (
            ["name", "material", "product", "customer", "region"],
            ["revenue", "amount", "total", "sales", "quantity", "value", "price"],
        ),
        "inventory": (
            ["name", "product", "sku", "warehouse", "category"],
            ["stock", "quantity", "count", "on_hand", "available", "total"],
        ),
        "customer": (
            ["name", "customer", "segment", "region", "account"],
            ["count", "total", "revenue", "value", "amount"],
        ),
        "time_series": (
            ["date", "month", "period", "year", "quarter", "time"],
            ["value", "revenue", "amount", "count", "quantity", "total"],
        ),
        "financial": (
            ["name", "category", "account", "department", "type"],
            ["amount", "total", "cost", "expense", "revenue", "budget", "value"],
        ),
    }

    rows = rows or []
    warnings = list(warnings or [])

    # ── Smart column selection: try task-specific patterns first ──
    label_col: str | None = None
    value_col: str | None = None
    secondary_value_col: str | None = None

    if rows:
        first = rows[0]
        all_keys = list(first.keys())

        # Try task-specific patterns first
        label_patterns, value_patterns = _TASK_COLUMN_PATTERNS.get(
            task_type, (["name"], ["value", "total", "amount", "count"])
        )

        # Best matching label column
        for pattern in label_patterns:
            for key in all_keys:
                if pattern.lower() in key.lower() and isinstance(first[key], str):
                    label_col = key
                    break
            if label_col:
                break

        # Fallback: first string column
        if label_col is None:
            for k, v in first.items():
                if isinstance(v, str):
                    label_col = k
                    break

        # Best matching value column (prefer task-specific patterns)
        for pattern in value_patterns:
            for key in all_keys:
                if pattern.lower() in key.lower() and isinstance(first[key], (int, float)):
                    if value_col is None:
                        value_col = key
                    elif secondary_value_col is None and key != value_col:
                        secondary_value_col = key
            if value_col:
                break

        # Fallback: first numeric column
        if value_col is None:
            for k, v in first.items():
                if isinstance(v, (int, float)):
                    value_col = k
                    break

    # ── Compute aggregates ──
    total = 0.0
    top_value = 0.0
    top_label = "—"
    if value_col:
        try:
            total = sum(float(r.get(value_col, 0) or 0) for r in rows)
            if rows:
                top_row = max(rows, key=lambda r: float(r.get(value_col, 0) or 0))
                top_value = float(top_row.get(value_col, 0) or 0)
                top_label = str(top_row.get(label_col, "—")) if label_col else "—"
        except (TypeError, ValueError):
            total = 0.0

    top_share = top_value / total if total > 0 else 0.0

    secondary_total = 0.0
    if secondary_value_col:
        try:
            secondary_total = sum(float(r.get(secondary_value_col, 0) or 0) for r in rows)
        except (TypeError, ValueError):
            secondary_total = 0.0

    # ── Task-aware KPIs (3-4 tiles) ──
    kpis: list[KPISpec] = []

    # Row count (always present)
    kpis.append(KPISpec(
        label="Row count",
        value=str(len(rows)),
        caption="Distinct items" if rows else "No data",
    ))

    if value_col and rows:
        label_hint = (label_col or "item").replace("_", " ")
        kpis.append(KPISpec(
            label=f"Total {value_col.replace('_', ' ')}",
            value=_format_money(total),
            caption=f"Across {len(rows)} {label_hint}s",
        ))
        if top_label and top_value > 0:
            kpis.append(KPISpec(
                label="Top performer",
                value=_format_money(top_value),
                caption=f"{top_label} ({top_share * 100:.0f}% share)",
            ))
        # Add distribution count for larger result sets
        if len(rows) >= 5 and top_share > 0.3:
            kpis.append(KPISpec(
                label="Concentration",
                value=f"{top_share * 100:.0f}%",
                caption="Top item share of total",
            ))
    else:
        kpis.append(KPISpec(label="Row count", value="0", caption="No data"))

    # ── Task-aware chart ──
    # If the task-specific pattern matching didn't find both columns,
    # fall back to the shared picker which avoids id-like and
    # timestamp-like columns for the value and prefers a low-
    # cardinality string column for the label.
    if rows and (not label_col or not value_col):
        picked_label, picked_value = _pick_chart_columns(rows)
        if not label_col and picked_label:
            label_col = picked_label
        if not value_col and picked_value:
            value_col = picked_value

    chart: Optional[ChartSpec] = None
    if label_col and value_col and rows:
        chart_type = "line" if task_type == "time_series" else "bar"
        raw_chart = ChartSpec(
            type=chart_type,
            title=f"{value_col.replace('_', ' ')} by {label_col.replace('_', ' ')}",
            x_key=label_col,
            y_keys=[value_col] + ([secondary_value_col] if secondary_value_col else []),
            data=rows,
            unit="",
        )
        # Run the fallback chart through the shared validator. This
        # aggregates duplicate x-labels, drops constant y_keys (the
        # very pattern that produced the "all bars = 1" bug), caps
        # categories to top-12 + "Other", and coerces string numerics.
        chart, chart_warnings = _validate_chart_spec(raw_chart)
        if chart_warnings:
            warnings.extend(chart_warnings)

    # ── Task-aware insights ──
    insights: list[InsightSpec] = []
    if rows and value_col and total > 0:
        # Top performer insight (all tasks)
        insights.append(InsightSpec(
            icon="trending-up",
            text=f"Top performer is {top_label} at {_format_money(top_value)} ({top_share * 100:.0f}% of total).",
        ))
        # Concentration warning (all tasks where relevant)
        if len(rows) >= 3 and top_share > 0.4:
            insights.append(InsightSpec(
                icon="alert-triangle",
                text=f"Top share is {top_share * 100:.0f}% — concentration risk worth monitoring.",
            ))
        # Task-specific insights
        if task_type == "sales" and len(rows) >= 5:
            insights.append(InsightSpec(
                icon="bar-chart-2",
                text=f"Top 3 items account for ~{min(top_share * 2, 0.95) * 100:.0f}% of revenue — consider portfolio analysis.",
            ))
        elif task_type == "inventory":
            insights.append(InsightSpec(
                icon="package",
                text=f"Total stock across {len(rows)} SKUs is {_format_money(total)} units.",
            ))
        elif task_type == "time_series" and len(rows) >= 3:
            insights.append(InsightSpec(
                icon="calendar",
                text=f"Data covers {len(rows)} time periods — consider trend and seasonality analysis.",
            ))

    # ── Title and summary ──
    task_titles = {
        "contract": "Contract performance report",
        "sales": "Sales report",
        "inventory": "Inventory report",
        "customer": "Customer report",
        "time_series": "Trend analysis",
        "financial": "Financial report",
        "general": "Data summary",
    }
    title = task_titles.get(task_type, "Data summary")

    if rows:
        summary_parts = [f"Retrieved {len(rows)} row{'s' if len(rows) != 1 else ''} from {source_name or 'the bound data source'}"]
        if label_col and value_col:
            summary_parts.append(f"spanning {top_label if top_label else 'multiple'} entries")
        summary = ". ".join(summary_parts) + "."
    else:
        summary = "No data returned for this query."

    task_actions = {
        "contract": [
            ActionSpec(label="Top customers", prompt="Show me the top customers by contract value."),
            ActionSpec(label="Execution risk", prompt="Which contracts are executing below 80%?"),
        ],
        "sales": [
            ActionSpec(label="Break down by region", prompt="Break this down by region."),
            ActionSpec(label="Save as weekly", prompt="Save this as a recurring weekly report."),
        ],
        "inventory": [
            ActionSpec(label="Check low stock", prompt="Show me items with low stock."),
            ActionSpec(label="By warehouse", prompt="Break this down by warehouse."),
        ],
        "customer": [
            ActionSpec(label="Show top customers", prompt="Show me the top 10 customers."),
            ActionSpec(label="By segment", prompt="Break this down by customer segment."),
        ],
        "time_series": [
            ActionSpec(label="Show monthly trend", prompt="Show monthly breakdown."),
            ActionSpec(label="Compare periods", prompt="Compare this period with previous."),
        ],
        "financial": [
            ActionSpec(label="By category", prompt="Break this down by cost category."),
            ActionSpec(label="Save as report", prompt="Save this as a recurring monthly report."),
        ],
        "general": [
            ActionSpec(label="Save as recurring", prompt="Save this as a recurring weekly report."),
            ActionSpec(label="Change scope", prompt="Try a different scope (region, period, product)."),
        ],
    }

    # ── Methodology ──
    methodology = (
        f"Data sourced from {source_name or 'the bound data source'}"
        + (f" (SQL: {sql[:80]}…)" if sql else "")
        + f" returning {len(rows)} rows"
        + (f" aggregated by {label_col.replace('_', ' ')}" if label_col else "")
        + ". Analysis reflects snapshot at query time."
    )

    # ── Key findings (data-driven) ──
    key_findings: list[InsightSpec] = []
    if rows and value_col and total > 0:
        key_findings.append(InsightSpec(
            icon="shield-check",
            text=f"The dataset contains {len(rows)} distinct "
                 + (label_col.replace("_", " ") if label_col else "items")
                 + f" with a total {value_col.replace('_', ' ')} of {_format_money(total)}. "
                 + (f"Top performer {top_label} accounts for {top_share * 100:.0f}% of the total, "
                    f"indicating a {'highly concentrated' if top_share > 0.5 else 'moderately concentrated' if top_share > 0.3 else 'well-distributed'} portfolio."
                    if top_label and top_share > 0
                    else ""),
        ))
    if len(rows) >= 3 and top_share > 0.25:
        other_count = len(rows) - 1
        tail_share = 1.0 - top_share
        key_findings.append(InsightSpec(
            icon="shield-check",
            text=f"The remaining {other_count} item{'s' if other_count > 1 else ''} collectively represent "
                 f"{tail_share * 100:.0f}% of total — "
                 + ("a significant long tail worth investigating for upsell or rationalization."
                    if tail_share < 0.3
                    else "a meaningful contribution that should not be overlooked in strategic planning."),
        ))

    # ── Recommendations (task-aware) ──
    recommendations: list[InsightSpec] = []
    if rows and value_col and total > 0:
        recommendations.append(InsightSpec(
            icon="arrow-right",
            text="Extend the analysis window to 12 months to distinguish seasonal patterns "
                 "from structural trends — the current snapshot may over-fit short-term signals.",
        ))
        if top_share > 0.35:
            recommendations.append(InsightSpec(
                icon="arrow-right",
                text=f"Given {top_label}'s {top_share * 100:.0f}% share, consider supply-chain diversification "
                     "or strategic pricing to mitigate single-point concentration risk.",
            ))
        else:
            recommendations.append(InsightSpec(
                icon="arrow-right",
                text="Set up a recurring weekly report to monitor shifts in the mix and catch anomalies early.",
            ))

    return ReportCardPayload(
        title=title,
        source=f"{source_name or 'data source'}" + (f" · {sql[:60]}…" if sql else ""),
        generated_at=datetime.now(timezone.utc).isoformat() + "Z",
        summary=summary,
        methodology=methodology,
        kpis=kpis,
        chart=chart,
        insights=insights or [InsightSpec(icon="info", text="No actionable insights — the data may be empty or unstructured.")],
        key_findings=key_findings,
        recommendations=recommendations,
        next_step="Want to narrow the scope, or save this as a recurring report?",
        actions=task_actions.get(task_type, task_actions["general"]),
        user_signal=user_signal,
        warnings=warnings,
    )


def _extract_json_block(text: str) -> Optional[dict]:
    """Pull the FIRST JSON fenced block out of the LLM reply, if any.

    Tries, in order:
    1. ```json ... ``` or ``` ... ``` fence.
    2. A bare JSON object that starts with `{` and ends with the matching `}`.
    3. The text in full (last-ditch).
    """
    if not text:
        return None
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.debug("Synthesis fenced-JSON parse failed: %s", e)
            # fall through to bare-JSON attempt

    # Bare JSON object: find the first "{" and walk braces to find the
    # matching closing brace.  This handles the case where the LLM
    # prints prose then a bare JSON object on a new line.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
        if end != -1:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                logger.debug("Bare-JSON parse failed at %d: %s", start, e)
                # try the next "{" after this one
                start = text.find("{", start + 1)
                continue
        break
    return None


def _strip_json_block(text: str) -> str:
    """Remove the JSON fence from a synthesis reply so we can keep the prose."""
    return _JSON_FENCE.sub("", text).strip()


def _safe_payload_from_dict(d: dict, *, default_title: str) -> ReportCardPayload:
    """Coerce an LLM-provided dict into a ReportCardPayload, with safe defaults.

    The chart is additionally run through :func:`validate_chart_spec` so
    that degenerate LLM output (constant-value series, unaggregated
    rows, missing y_keys, too-many categories) is either repaired or
    dropped with a warning, rather than rendered as a flat /
    meaningless chart in the chat.
    """
    extra_chart_warnings: list[str] = []

    def _repaired_chart(raw: Any) -> Optional[ChartSpec]:
        if not isinstance(raw, dict):
            return None
        try:
            spec = ChartSpec.model_validate(raw)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("ChartSpec validation failed: %s", e)
            extra_chart_warnings.append(f"chart spec failed validation: {e}")
            return None
        repaired, warns = _validate_chart_spec(spec)
        extra_chart_warnings.extend(warns)
        return repaired

    try:
        payload = ReportCardPayload.model_validate(d)
    except Exception as e:
        logger.debug("LLM payload did not validate, using best-effort: %s", e)
        # Best-effort: keep the bits that did parse.
        payload = ReportCardPayload(
            title=d.get("title") or default_title,
            source=d.get("source", ""),
            summary=d.get("summary", ""),
            kpis=[KPISpec.model_validate(k) for k in d.get("kpis", []) if isinstance(k, dict)],
            chart=_repaired_chart(d.get("chart")),
            insights=[InsightSpec.model_validate(i) for i in d.get("insights", []) if isinstance(i, dict)],
            next_step=d.get("next_step"),
            actions=[ActionSpec.model_validate(a) for a in d.get("actions", []) if isinstance(a, dict)],
        )
    else:
        # The LLM-emitted chart is repaired/validated even when the
        # rest of the payload validates cleanly — this is the
        # single-point gate that prevents the "all bars = 1" bug.
        if payload.chart is not None:
            payload.chart = _repaired_chart(payload.chart.model_dump())

    if extra_chart_warnings:
        existing = list(payload.warnings or [])
        payload.warnings = existing + extra_chart_warnings

    return payload


def merge_answer_rows(datasets: list[dict] | None) -> list[dict]:
    """Concatenate rows from every answer-tagged dataset into one list.

    Later queries refine earlier ones, so row order follows dataset record
    order and identical row dicts (JSON-key stable) are deduplicated while
    preserving first-seen position.  This is the deterministic merge behind
    multi-dataset synthesis — a degenerate final query (e.g. a 1-row
    data-quality check) can no longer starve the report of the earlier,
    richer datasets.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for ds in datasets or []:
        for row in ds.get("rows") or []:
            key = json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                merged.append(row)
    return merged


async def synthesize_report(
    *,
    user_message: str,
    rows: list[dict] | None,
    sql: str | None,
    source_name: str | None,
    source_id: str | None,
    call_llm_fn,
    user_signal: Optional[str] = None,
    skill_name: Optional[str] = None,
    skill_methodology: Optional[str] = None,
    datasets: list[dict] | None = None,
) -> FinalizeResult:
    """Run the synthesis LLM turn and return a FinalizeResult.

    `call_llm_fn` is the same LLM call helper the calling agent uses
    (kept injected so this module is testable without a live LLM).  It
    is awaited with (system, messages) and must return a dict with
    `content` (the reply text).
    """
    user_signal = user_signal or detect_user_signal(user_message)
    task_type = _detect_task_type(user_message)
    rows = rows or []
    if datasets and not rows:
        # Safety net: if the caller only passed the answer datasets, derive
        # the merged row set here so the payload is never built from a single
        # (possibly degenerate) dataset.
        rows = merge_answer_rows(datasets)
    warnings: list[str] = []

    if not rows:
        warnings.append("Data agent returned 0 rows — synthesis is based on an empty snapshot.")

    # Cap the rows we ship to the LLM (avoid blowing context on 10k rows).
    # We now ALSO ship a pre-computed aggregate summary (computed from ALL
    # rows), so the synthesizer interprets exact numbers instead of doing
    # mental arithmetic over a raw sample.
    MAX_ROWS_FOR_LLM = 300
    rows_for_llm = rows[:MAX_ROWS_FOR_LLM]
    if len(rows) > MAX_ROWS_FOR_LLM:
        warnings.append(
            f"Snapshot had {len(rows)} rows; synthesis used the first {MAX_ROWS_FOR_LLM} "
            "plus pre-computed aggregates over all rows."
        )

    # ── Pre-aggregation (compute exact aggregates over ALL rows) ────────
    # This is the key P0 improvement: instead of asking the LLM to compute
    # totals/breakdowns from a 100-row sample, we compute them here (pure
    # Python, milliseconds) and hand the LLM the answers to interpret.
    preagg_block = ""
    try:
        from app.services.synexia.pre_aggregation import pre_aggregate

        preagg = pre_aggregate(rows)
        preagg_block = preagg.to_prompt_block()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Pre-aggregation failed (continuing without it): %s", e)
        preagg_block = ""

    # ── Data-shape guard ─────────────────────────────────────────────
    # Enrich the user payload with shape warnings when the snapshot is
    # too sparse to support a professional analytical report.
    shape_warnings: list[str] = []
    if rows:
        # Time-series sparsity check: if a date/timestamp column exists,
        # count distinct periods.  Warn if fewer than 6 so the LLM can
        # qualify its findings.
        date_cols = [c for c in rows[0].keys() if _is_date_column(c)]
        if date_cols:
            date_col = date_cols[0]
            distinct_periods = len({r.get(date_col) for r in rows})
            if distinct_periods < 2:
                shape_warnings.append(
                    "⚠ DATA WARNING: Only 1 time period in snapshot — no comparison is possible. "
                    "State this explicitly in the summary and avoid MoM/YoY claims."
                )
            elif distinct_periods < 6:
                shape_warnings.append(
                    f"⚠ DATA WARNING: Only {distinct_periods} time periods — this is below the "
                    "recommended 6-period minimum for reliable trend analysis. "
                    "Qualify all trend claims with the narrow window caveat."
                )

        # Category sparsity check
        cat_cols = [c for c in rows[0].keys() if _is_label_column(c, rows)]
        if cat_cols and len(rows) < 5:
            shape_warnings.append(
                f"⚠ DATA WARNING: Only {len(rows)} rows returned — the query may be too "
                "narrow. If this is supposed to be a top-N ranking, ensure N≥7 for "
                "meaningful Pareto analysis. Qualify findings with data-scope caveats."
            )

        # Value sparsity check: all-zero value columns
        val_cols = [c for c in rows[0].keys() if _is_value_column(c, rows)]
        if val_cols:
            all_rows_zero = all(
                all((r.get(vc) or 0) == 0 for vc in val_cols)
                for r in rows
            )
            if all_rows_zero:
                shape_warnings.append(
                    "⚠ DATA WARNING: All numeric columns are zero — the snapshot may contain "
                    "only header rows. Verify the query and data source."
                )

    if shape_warnings:
        warnings.extend(shape_warnings)

    # ── System prompt ─────────────────────────────────────────────────
    # We previously injected a hardcoded template block here (sales_report,
    # pitch_deck, etc.).  Templates were deleted in favor of the C-Heavy
    # skill-driven runner, which plans the document structure dynamically
    # inside the sandbox from the user's actual request.  The task-type
    # guidance already built into _SYNTHESIS_SYSTEM_PROMPT (sales /
    # inventory / customer / time_series / financial / general) provides
    # all the per-task-type structural hints we need.
    system_prompt = _SYNTHESIS_SYSTEM_PROMPT
    if skill_methodology:
        clipped = skill_methodology[:5000]
        system_prompt += (
            "\n\nRUNTIME SELECTED SKILL METHODOLOGY:\n"
            f"The user explicitly selected the skill '{skill_name or 'selected-skill'}'. "
            "Follow this skill's methodology, output structure, tone, naming, and formatting conventions whenever they are compatible with the real data snapshot. "
            "Do not invent facts; adapt the methodology to the available data.\n\n"
            f"{clipped}"
        )

    datasets_overview = ""
    if datasets:
        overview_lines = [
            f"─── MULTI-DATASET CONTEXT ({len(datasets)} queries executed this turn) ───"
        ]
        for i, ds in enumerate(datasets, 1):
            ds_rows = list(ds.get("rows") or [])
            cols = list(ds_rows[0].keys()) if ds_rows else []
            overview_lines.append(
                f"Dataset {i}/{len(datasets)}: source={ds.get('source_name') or 'unknown'} "
                f"(id={ds.get('source_id') or 'n/a'}), rows={len(ds_rows)}, columns={cols}"
            )
            ds_sql = ds.get("sql")
            if ds_sql:
                overview_lines.append(f"  sql: {ds_sql}")
        overview_lines.append(
            "The rows below are the MERGED rows from ALL datasets above "
            "(later queries refine earlier ones; identical rows deduplicated). "
            "Synthesize from the full multi-query picture — do not restrict "
            "your analysis to any single query."
        )
        datasets_overview = "\n".join(overview_lines) + "\n\n"

    user_payload = (
        f"User message: {user_message}\n\n"
        f"Source: {source_name or 'unknown'} (id={source_id or 'n/a'})\n"
        f"SQL run: {sql or '(not provided)'}\n"
        f"Row count: {len(rows)}\n\n"
        + (f"{preagg_block}\n\n" if preagg_block else "")
        + (
            "Rows (JSON sample, first "
            f"{min(len(rows_for_llm), 100)} of {len(rows_for_llm)} shown — "
            "use the PRE-COMPUTED ANALYSIS above for exact totals/breakdowns):\n"
            f"```json\n{json.dumps(rows_for_llm[:100], default=str, ensure_ascii=False)}\n```\n\n"
        )
        + (
            f"Selected skill: {skill_name}\n"
            "Honor the selected skill's methodology when composing the summary, methodology, KPIs, key findings, recommendations, and final instructions.\n\n"
            if skill_name else ""
        )
        + (
            "─── DATA SHAPE NOTES (embed these caveats in your response) ───\n"
            + "\n".join(shape_warnings) + "\n\n"
            if shape_warnings else ""
        )
        + datasets_overview
        + "Now write the synthesis in the format described in the system prompt."
    )

    try:
        llm_response = await call_llm_fn(
            system_prompt,
            [{"role": "user", "content": user_payload}],
        )
    except Exception as e:
        logger.warning("Synthesis LLM call failed: %s", e)
        warnings.append(f"Synthesis LLM call failed: {e}")
        payload = _fallback_payload(
            user_message=user_message,
            rows=rows,
            sql=sql,
            source_name=source_name,
            user_signal=user_signal,
            task_type=task_type,
            warnings=warnings,
        )
        return FinalizeResult(
            task_kind="report",
            assistant_content=payload.summary,
            report_card_payload=payload,
            user_signal=user_signal,
            warnings=warnings,  # type: ignore[arg-type]
        )

    reply_text = (llm_response or {}).get("content", "") or ""
    json_block = _extract_json_block(reply_text)
    prose = _strip_json_block(reply_text)

    if json_block is None:
        # LLM didn't return the fenced JSON — fall back to the heuristic
        # payload so the user still gets something, but keep the prose
        # the LLM did write as the assistant_content.
        warnings.append("Synthesis reply did not contain a JSON payload — using fallback KPIs.")
        payload = _fallback_payload(
            user_message=user_message,
            rows=rows,
            sql=sql,
            source_name=source_name,
            user_signal=user_signal,
            task_type=task_type,
            warnings=warnings,
        )
        return FinalizeResult(
            task_kind="report",
            assistant_content=prose or payload.summary,
            report_card_payload=payload,
            user_signal=user_signal,
        )

    payload = _safe_payload_from_dict(json_block, default_title="Sales report")
    # Make sure user_signal + generated_at + title are sane.
    payload.user_signal = user_signal
    payload.warnings = list({*payload.warnings, *warnings})
    if not payload.generated_at:
        payload.generated_at = datetime.now(timezone.utc).isoformat() + "Z"
    if not payload.title:
        payload.title = "Sales report" if "report" in user_message.lower() else "Data summary"

    return FinalizeResult(
        task_kind="report",
        assistant_content=prose or payload.summary,
        report_card_payload=payload,
        user_signal=user_signal,
    )
