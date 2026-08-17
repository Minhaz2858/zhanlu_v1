"""LLM-driven deck planner.

Turns raw data rows + user intent into a :class:`DeckPlan` (a narrative
slide sequence) and maps that plan onto :class:`ReportCardPayload` for
the structured renderer.  This is a single, bounded LLM call gated by
``PPT_DECK_PLANNER_ENABLED`` — when disabled, the pipeline falls straight
through to the existing report-card renderer.

The planner is intentionally deterministic in its fallbacks: if the LLM
call fails or times out, it synthesizes a minimal 3-slide plan from the
data profile so the deck still renders.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.config import settings
from app.services.synexia.contracts import (
    ChartSpecInSlide,
    ChartSpec,
    DeckPlan,
    KPISpec,
    KPISpecInSlide,
    InsightSpec,
    ReportCardPayload,
    SlidePlan,
)

from .data_profiler import profile_rows, suggest_charts, summarize_profile

logger = logging.getLogger(__name__)

_PLANNER_TIMEOUT_S = 10.0

# Valid layouts the planner may emit (mirrors the layout engine dispatch).
_VALID_LAYOUTS = {
    "cover", "agenda", "kpi_grid", "chart_full", "chart_with_bullets",
    "findings_cards", "insights_bullets", "recommendations", "data_table",
    "methodology", "section_divider", "closing",
}

# JSON schema for the LLM structured output.
_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "deck_type": {
            "type": "string",
            "enum": ["data_report", "investor_deck", "marketing", "executive_brief"],
        },
        "theme_recommendation": {"type": "string"},
        "summary": {"type": "string"},
        "methodology": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "layout": {"type": "string"},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "chart_spec": {
                        "type": "object",
                        "properties": {
                            "chart_type": {"type": "string"},
                            "x_key": {"type": "string"},
                            "y_keys": {"type": "array", "items": {"type": "string"}},
                            "title": {"type": "string"},
                        },
                    },
                    "kpi_specs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                                "delta": {"type": "string"},
                                "caption": {"type": "string"},
                            },
                        },
                    },
                    "notes": {"type": "string"},
                    "narrative_role": {"type": "string"},
                },
                "required": ["layout", "title"],
            },
        },
    },
    "required": ["title", "slides"],
}


def _build_planner_prompt(
    user_intent: str,
    profile_summary: str,
    chart_hints: list[dict[str, Any]],
    row_count: int,
) -> str:
    chart_lines = "\n".join(
        f"- {c['chart_type']} | x={c['x_key']} | y={c['y_keys']} | {c['title']}"
        for c in chart_hints
    ) or "(none)"
    return (
        "You are a presentation architect. Build a concise, narrative slide "
        "sequence for the following request.\n\n"
        f"USER INTENT:\n{user_intent}\n\n"
        f"DATA (rows={row_count}):\n{profile_summary}\n\n"
        f"SUGGESTED CHARTS:\n{chart_lines}\n\n"
        "Rules:\n"
        "- Output valid JSON matching the requested schema.\n"
        "- 5-10 slides. First slide layout must be 'cover', last 'closing'.\n"
        "- Use layouts only from: cover, agenda, kpi_grid, chart_full, "
        "chart_with_bullets, findings_cards, insights_bullets, "
        "recommendations, data_table, methodology, section_divider, closing.\n"
        "- Keep each bullet <= 12 words; each title <= 8 words.\n"
        "- For data-heavy decks prefer 'data_report' deck_type and reference "
        "the suggested charts (x_key / y_keys must match actual column names).\n"
        "- For 'beautiful'/'investor'/'pitch' requests use investor_deck or "
        "marketing and lean on cover / section_divider / kpi_grid layouts.\n"
    )


async def _llm_deck_plan(
    user_intent: str,
    profile_summary: str,
    chart_hints: list[dict[str, Any]],
    row_count: int,
) -> Optional[DeckPlan]:
    """Make the single bounded LLM call and coerce the result into a DeckPlan.

    Returns ``None`` on any failure / timeout so the caller can fall back.
    """
    from app.services.llm_service import call_llm

    prompt = _build_planner_prompt(user_intent, profile_summary, chart_hints, row_count)
    try:
        result = await asyncio.wait_for(
            call_llm(
                prompt=prompt,
                temperature=0.3,
                response_json_schema=_PLAN_SCHEMA,
                task_type="deck_planning",
            ),
            timeout=_PLANNER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("deck_planner: LLM call timed out after %ss", _PLANNER_TIMEOUT_S)
        return None
    except Exception as e:  # noqa: BLE001 — fall back gracefully on any LLM error
        logger.warning("deck_planner: LLM call failed: %s", e)
        return None

    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        raw = result.get("response") if isinstance(result, dict) else None
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        logger.warning("deck_planner: LLM returned non-dict data; falling back")
        return None

    return _coerce_plan(data)


def _coerce_plan(data: dict[str, Any]) -> Optional[DeckPlan]:
    """Coerce (and sanitize) the LLM dict into a valid DeckPlan."""
    try:
        plan = DeckPlan.model_validate(data)
    except Exception as e:  # noqa: BLE001
        logger.warning("deck_planner: DeckPlan validation failed: %s", e)
        return None

    if not plan.slides:
        return None

    # Sanitize layouts: drop invalid entries, map common aliases.
    cleaned: list[SlidePlan] = []
    for slide in plan.slides:
        layout = _normalize_layout(slide.layout)
        if layout is None:
            continue
        slide.layout = layout
        cleaned.append(slide)

    # Guarantee a cover as the first slide and closing as the last.
    if not cleaned or cleaned[0].layout != "cover":
        cleaned.insert(0, SlidePlan(layout="cover", title=plan.title or "Overview"))
    if cleaned[-1].layout != "closing":
        cleaned.append(SlidePlan(layout="closing", title="Thank you"))

    plan.slides = cleaned
    return plan


def _normalize_layout(layout: str) -> Optional[str]:
    l = (layout or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "title": "cover",
        "title_slide": "cover",
        "kpis": "kpi_grid",
        "kpi": "kpi_grid",
        "metrics": "kpi_grid",
        "chart": "chart_full",
        "chart_and_bullets": "chart_with_bullets",
        "bullets": "insights_bullets",
        "findings": "findings_cards",
        "table": "data_table",
        "divider": "section_divider",
        "section": "section_divider",
        "end": "closing",
        "thank_you": "closing",
    }
    l = aliases.get(l, l)
    return l if l in _VALID_LAYOUTS else None


def _fallback_plan(
    user_intent: str,
    profile: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> DeckPlan:
    """Deterministic minimal plan used when the LLM fails or is disabled."""
    title = "Data Overview"
    chart_hints = suggest_charts(profile)

    slides: list[SlidePlan] = [
        SlidePlan(layout="cover", title=title, narrative_role="hook"),
    ]

    kpis = _profile_kpis(profile, rows)
    if kpis:
        slides.append(
            SlidePlan(
                layout="kpi_grid",
                title="Key Metrics",
                kpi_specs=kpis,
                narrative_role="context",
            )
        )

    if chart_hints:
        hint = chart_hints[0]
        slides.append(
            SlidePlan(
                layout="chart_full",
                title=hint["title"],
                chart_spec=ChartSpecInSlide(
                    chart_type=hint["chart_type"],
                    x_key=hint["x_key"],
                    y_keys=hint["y_keys"],
                    title=hint["title"],
                ),
                narrative_role="evidence",
            )
        )

    slides.append(
        SlidePlan(
            layout="insights_bullets",
            title="Summary",
            bullets=["See the accompanying data for full detail."],
            narrative_role="insight",
        )
    )
    slides.append(SlidePlan(layout="closing", title="Thank you", narrative_role="closing"))

    return DeckPlan(title=title, deck_type="data_report", slides=slides)


def _profile_kpis(
    profile: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> list[KPISpecInSlide]:
    """Derive up to 4 KPI tiles from numeric columns (value = latest row)."""
    numeric = [c for c, m in profile.items() if m["type"] == "numeric"]
    if not rows or not numeric:
        return []
    latest = rows[-1] if isinstance(rows[-1], dict) else {}
    kpis: list[KPISpecInSlide] = []
    for col in numeric[:4]:
        val = latest.get(col)
        if val is None:
            continue
        kpis.append(KPISpecInSlide(label=col, value=str(val)))
    return kpis


async def build_deck_plan(
    user_intent: str,
    rows: list[dict[str, Any]],
) -> tuple[DeckPlan, dict[str, dict[str, Any]]]:
    """Entry point: profile the data, then plan the deck.

    Returns ``(DeckPlan, profile)``.  Falls back to a deterministic plan
    when the LLM call is disabled, fails, or times out.
    """
    profile = profile_rows(rows)
    profile_summary = summarize_profile(profile)
    chart_hints = suggest_charts(profile)
    row_count = len(rows)

    if not settings.PPT_DECK_PLANNER_ENABLED:
        return _fallback_plan(user_intent, profile, rows), profile

    plan = await _llm_deck_plan(user_intent, profile_summary, chart_hints, row_count)
    if plan is None:
        return _fallback_plan(user_intent, profile, rows), profile

    return plan, profile


# ---------------------------------------------------------------------------
# DeckPlan → ReportCardPayload mapping (for the structured renderer)
# ---------------------------------------------------------------------------


def deck_plan_to_report_card(
    plan: DeckPlan,
    rows: list[dict[str, Any]],
    profile: dict[str, dict[str, Any]],
) -> ReportCardPayload:
    """Map a DeckPlan onto ReportCardPayload so the structured renderer works.

    The structured renderer consumes ``ReportCardPayload``.  We translate
    the planner's narrative slides back into the report-card shape:
    - first KPI-grid slide → ``kpis``
    - first chart slide → ``chart`` (with ``data`` pulled from ``rows``)
    - insight bullets → ``insights`` / ``key_findings``
    - recommendations slide → ``recommendations``
    - methodology slide → ``methodology``
    """
    kpis: list[KPISpec] = []
    chart: Optional[ChartSpec] = None
    insights: list[InsightSpec] = []
    findings: list[InsightSpec] = []
    recommendations: list[InsightSpec] = []
    methodology = plan.methodology

    for slide in plan.slides:
        if slide.kpi_specs and not kpis:
            kpis = [
                KPISpec(
                    label=k.label, value=k.value, delta=k.delta, caption=k.caption
                )
                for k in slide.kpi_specs
            ]

        if slide.chart_spec and chart is None:
            cs = slide.chart_spec
            chart = ChartSpec(
                type=cs.chart_type,
                title=cs.title,
                x_key=cs.x_key,
                y_keys=cs.y_keys,
                data=_chart_data(rows, cs.x_key, cs.y_keys),
            )

        if slide.bullets:
            if slide.layout == "recommendations":
                recommendations.extend(InsightSpec(text=b) for b in slide.bullets)
            elif slide.layout in ("insights_bullets", "findings_cards"):
                insights.extend(InsightSpec(text=b) for b in slide.bullets)
            elif slide.layout == "chart_with_bullets":
                findings.extend(InsightSpec(text=b) for b in slide.bullets)

        if slide.layout == "methodology" and not methodology:
            methodology = " ".join(slide.bullets) or slide.subtitle

    return ReportCardPayload(
        title=plan.title,
        summary=plan.summary,
        kpis=kpis,
        chart=chart,
        insights=insights,
        key_findings=findings,
        recommendations=recommendations,
        methodology=methodology,
    )


def _chart_data(
    rows: list[dict[str, Any]], x_key: str, y_keys: list[str]
) -> list[dict[str, Any]]:
    """Project ``rows`` down to the chart's x/y columns for the renderer."""
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        point: dict[str, Any] = {}
        if x_key:
            point[x_key] = row.get(x_key)
        for yk in y_keys:
            point[yk] = row.get(yk)
        if point:
            out.append(point)
    return out
