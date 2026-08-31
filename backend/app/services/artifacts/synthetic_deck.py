"""General no-data deck builder (2026-08-29).

The agent must answer ANY question — sales, tech, HR, market, operations —
and deliver a professional deck even when the warehouse has no rows for the
topic.  Doubao/Claude do this by structuring a full consulting narrative from
whatever the request carries, never by hardcoding a domain.

This module builds a complete consulting-structure DeckPlan from the
PAYLOAD narrative alone (the agent-authored summary / kpis / key_findings /
recommendations / sections / chart).  It is domain-agnostic:

* It NEVER hardcodes domain facts (no company names, no product segments,
  no regional shares).  It structures what the agent already wrote.
* When the payload carries real narrative (summary, kpis, findings, recs,
  sections, chart), every one of those becomes a designed slide.
* When the payload is genuinely empty, it frames the request honestly:
  cover → executive framing → agenda → topic context → analysis dimensions →
  recommendations (framed as questions to answer) → Q&A closing, with
  figures labeled as "illustrative / to be confirmed" wherever a number
  would normally sit.

Trigger: ``build_synthetic_deck_plan(intent, payload)`` returns a plan when
the caller has NO grounded rows (empty warehouse result) — for ANY intent.
This is the general replacement for the earlier market-only synth: a user
who asks about C5/C9, Q3 sales, or AI trends all get the same rich
structure, with content coming from the agent's own payload.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.synexia.contracts import (
    ChartSpecInSlide,
    DeckPlan,
    KPISpecInSlide,
    SlidePlan,
)

logger = logging.getLogger(__name__)

_NON_CONTENT_LAYOUTS = {"cover", "agenda", "section_divider", "closing"}


def _text_of(item: Any) -> str:
    """Pull display text from a dict or object (InsightSpec-like)."""
    if item is None:
        return ""
    if isinstance(item, dict):
        return str(item.get("text") or item.get("title") or item.get("content") or "")
    return str(getattr(item, "text", "") or getattr(item, "title", "") or item)


def _kpi_specs_from_payload(payload: Any) -> list[KPISpecInSlide]:
    """Map payload.kpis (list of dict/objects) to KPI tiles."""
    kpis = getattr(payload, "kpis", None) or []
    if not isinstance(kpis, list):
        return []
    specs: list[KPISpecInSlide] = []
    for k in kpis[:6]:
        if isinstance(k, dict):
            specs.append(KPISpecInSlide(
                label=str(k.get("label") or k.get("name") or "KPI"),
                value=str(k.get("value") or k.get("display") or ""),
                delta=str(k.get("delta") or ""),
                caption=str(k.get("caption") or ""),
            ))
        elif hasattr(k, "label"):
            specs.append(KPISpecInSlide(
                label=str(getattr(k, "label", None) or "KPI"),
                value=str(getattr(k, "value", None) or getattr(k, "display", "") or ""),
                delta=str(getattr(k, "delta", None) or ""),
                caption=str(getattr(k, "caption", None) or ""),
            ))
    return specs


def _chart_from_payload(payload: Any) -> Optional[tuple[ChartSpecInSlide, list[dict[str, Any]]]]:
    """Map payload.chart (ChartSpec with data) to (spec, rows)."""
    chart = getattr(payload, "chart", None)
    if chart is None:
        return None
    if isinstance(chart, dict):
        ctype = str(chart.get("type") or "bar")
        x_key = str(chart.get("x_key") or "")
        y_keys = list(chart.get("y_keys") or []) or []
        title = str(chart.get("title") or "")
        rows = chart.get("data") or []
    else:
        ctype = str(getattr(chart, "type", "bar") or "bar")
        x_key = str(getattr(chart, "x_key", "") or "")
        y_keys = list(getattr(chart, "y_keys", None) or [])
        title = str(getattr(chart, "title", "") or "")
        rows = getattr(chart, "data", None) or []
    if not rows:
        return None
    return ChartSpecInSlide(chart_type=ctype, x_key=x_key, y_keys=y_keys, title=title), rows


def _bullets_from_list(items: Any, limit: int = 5) -> list[str]:
    """Extract ≤limit text bullets from a list of dicts/objects."""
    if not items or not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        t = _text_of(it)
        if t and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _sections_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Extract structured sections [{title, content/bullets}] from payload."""
    sections = getattr(payload, "sections", None) or []
    if not isinstance(sections, list):
        return []
    out: list[dict[str, Any]] = []
    for s in sections[:6]:
        if isinstance(s, dict):
            out.append({
                "title": str(s.get("title") or "Section"),
                "content": str(s.get("content") or ""),
                "bullets": list(s.get("bullets") or []),
            })
        elif hasattr(s, "title"):
            out.append({
                "title": str(getattr(s, "title", "Section") or "Section"),
                "content": str(getattr(s, "content", "") or ""),
                "bullets": list(getattr(s, "bullets", None) or []),
            })
    return out


def _title_from_intent(intent: str, payload: Any) -> str:
    """Derive a clean deck title: payload title > intent > generic."""
    ptitle = getattr(payload, "title", "") or ""
    if isinstance(ptitle, str) and ptitle.strip():
        return ptitle.strip()[:80]
    intent = (intent or "").strip().rstrip(".")
    if intent:
        # Strip the "make a ... ppt" scaffolding for a cleaner title.
        lowered = intent.lower()
        for prefix in ("make a ", "make an ", "create a ", "create an ", "give me a ",
                       "build a ", "build an "):
            if lowered.startswith(prefix):
                intent = intent[len(prefix):]
                break
        # Cut "ppt about X", "presentation on X", "deck for X" patterns.
        # (run BEFORE the suffix cut so "deck on EV market" → "EV market")
        for connector in ("ppt about ", "ppt on ", "ppt for ", "ppt of ",
                          "presentation about ", "presentation on ", "presentation for ",
                          "deck about ", "deck on ", "deck for ",
                          "slides about ", "slides on ", "slides for ",
                          "presentation ", "powerpoint on "):
            idx = intent.lower().find(connector)
            if idx >= 0:
                intent = intent[idx + len(connector):]
                break
        for suffix in (" pptx", " ppt", " presentation", " slide deck", " deck",
                       " powerpoint", " power point"):
            idx = intent.lower().find(suffix)
            if idx > 0:
                intent = intent[:idx]
                break
        # Cut trailing instruction clauses that are not part of the title:
        # "don't use my data", "use fake data", "fake data", etc.
        for marker in (" dont use my data", " don't use my data", " use fake data",
                       " dont use ", " fake data", " 用假数据", " 不要用我的数据",
                       " 别用我的数据", " 模拟数据"):
            idx = intent.lower().find(marker)
            if idx > 0:
                intent = intent[:idx]
                break
        return intent.strip()[:80] or "Overview"
    return "Overview"


def build_synthetic_deck_plan(
    intent: str,
    payload: Any = None,
) -> Optional[DeckPlan]:
    """Build a full consulting-structure deck from the payload narrative.

    Domain-agnostic: structures whatever the agent authored (summary, kpis,
    findings, recs, sections, chart).  Empty payload → honest generic framing
    with illustrative labels.  Returns None only when even ``intent`` is
    empty (nothing to title).
    """
    intent = (intent or "").strip()
    if not intent and payload is None:
        return None

    title = _title_from_intent(intent, payload)
    summary = str(getattr(payload, "summary", "") or "").strip() if payload is not None else ""

    kpi_specs = _kpi_specs_from_payload(payload) if payload is not None else []
    chart = _chart_from_payload(payload) if payload is not None else None
    findings = _bullets_from_list(
        getattr(payload, "key_findings", None) or getattr(payload, "insights", None),
    ) if payload is not None else []
    recs = _bullets_from_list(getattr(payload, "recommendations", None)) if payload is not None else []
    sections = _sections_from_payload(payload) if payload is not None else []

    # ── Executive summary bullets from the payload summary ──────────────
    summary_bullets: list[str] = []
    if summary:
        sentences = [
            s.strip() for s in summary.replace("。", ". ").replace("\n", " ").split(". ")
            if len(s.strip()) > 12
        ]
        summary_bullets = (sentences or [summary])[:4]

    slides: list[SlidePlan] = [
        SlidePlan(layout="cover", title=title,
                  subtitle="Analysis & outlook", period="2026",
                  narrative_role="hook"),
    ]

    # 1) Executive framing — KPI grid if the agent gave numbers, else summary.
    if kpi_specs:
        slides.append(SlidePlan(
            layout="kpi_grid",
            title="Key metrics at a glance",
            subtitle="Executive summary",
            kpi_specs=kpi_specs,
            narrative_role="context",
        ))
    elif summary_bullets:
        slides.append(SlidePlan(
            layout="insights_bullets",
            title="Executive summary",
            subtitle="Bottom line up front",
            bullets=summary_bullets,
            narrative_role="context",
        ))
    else:
        slides.append(SlidePlan(
            layout="insights_bullets",
            title="Executive framing",
            subtitle="What this analysis addresses",
            bullets=[
                f"Topic: {title}",
                "No warehouse rows were available for this request — "
                "figures shown are illustrative placeholders to be confirmed",
                "Structure covers context, analysis dimensions, and next steps",
            ],
            narrative_role="context",
        ))

    # 2) Agenda — mirrors whatever the deck will contain.
    agenda_items: list[str] = []
    if sections:
        agenda_items += [s["title"] for s in sections[:4]]
    if findings:
        agenda_items.append("Key findings")
    if chart:
        agenda_items.append("Data view")
    if recs:
        agenda_items.append("Recommendations")
    agenda_items = agenda_items[:6] or [
        "Context & scope", "Analysis dimensions", "Key observations",
        "Risks & opportunities", "Recommendations", "Q&A",
    ]
    slides.append(SlidePlan(
        layout="agenda", title="What this deck covers",
        subtitle="Agenda", bullets=agenda_items,
        narrative_role="context",
    ))

    # 3) Payload sections → divider + content per section.
    for i, sec in enumerate(sections[:4], start=1):
        sec_bullets = list(sec.get("bullets") or [])
        content = sec.get("content") or ""
        if content and not sec_bullets:
            sentences = [s.strip() for s in content.replace("。", ". ").split(". ")
                         if len(s.strip()) > 12]
            sec_bullets = (sentences or [content])[:4]
        if not sec_bullets:
            continue
        slides.append(SlidePlan(
            layout="section_divider",
            title=f"{i:02d} · {sec['title']}",
            narrative_role="context",
        ))
        slides.append(SlidePlan(
            layout="insights_bullets",
            title=sec["title"],
            bullets=sec_bullets,
            narrative_role="context",
        ))

    # 4) Chart slide (from payload.chart) if present.
    if chart is not None:
        spec, rows = chart
        slides.append(SlidePlan(
            layout="chart_full",
            title=spec.title or "Data view",
            subtitle="Illustrative data",
            chart_spec=spec,
            chart_rows=rows,
            narrative_role="evidence",
        ))

    # 5) Findings cards.
    if findings:
        slides.append(SlidePlan(
            layout="findings_cards",
            title="Key findings",
            bullets=findings,
            narrative_role="evidence",
        ))

    # 6) Analysis dimensions — generic, honest framing when the payload has
    #    no sectioned narrative (keeps the deck at a full 9-slide floor even
    #    when the agent only supplied kpis/findings/recs).
    if not sections:
        slides.append(SlidePlan(
            layout="section_divider", title="01 · Analysis dimensions",
            narrative_role="context",
        ))
        slides.append(SlidePlan(
            layout="comparison",
            title="Key dimensions to assess",
            subtitle="Analytical frame",
            bullets=[
                "Current state vs. target state",
                "Strengths vs. weaknesses",
                "Opportunities vs. threats",
                "Short-term vs. long-term horizon",
            ],
            narrative_role="insight",
        ))
        slides.append(SlidePlan(
            layout="insights_bullets",
            title="Key observations to validate",
            subtitle="Working hypotheses",
            bullets=[
                "What is the current baseline and trajectory?",
                "Which segments or regions move the outcome most?",
                "What external factors could change the picture?",
                "Where are the biggest risks and quickest wins?",
            ],
            narrative_role="evidence",
        ))
        slides.append(SlidePlan(
            layout="insights_bullets",
            title="Sources & next steps",
            subtitle="How to ground this deck",
            bullets=[
                "Connect the relevant data source to refresh every figure",
                "Confirm scope, time horizon, and success metrics",
                "Review the hypotheses above with the business owner",
                "Iterate this deck once real data is available",
            ],
            narrative_role="context",
        ))

    # 7) Recommendations.
    if recs:
        slides.append(SlidePlan(
            layout="recommendations", title="Recommendations",
            bullets=recs, narrative_role="action",
        ))
    else:
        slides.append(SlidePlan(
            layout="recommendations",
            title="Recommended next steps",
            subtitle="Illustrative — confirm with real data",
            bullets=[
                "Confirm the underlying data source and refresh figures",
                "Prioritize the dimensions above by expected impact",
                "Define owners and timelines for each action",
                "Review this deck once real data is available",
            ],
            narrative_role="action",
        ))

    slides.append(SlidePlan(
        layout="closing", title="Thank you — happy to go deeper",
        subtitle="Q&A", narrative_role="closing",
    ))

    # Speaker notes auto-fill: the synthetic path has no LLM to write
    # presenter scripts, so derive a 1-sentence note per slide from its
    # title (plus the first bullet when present). Keeps the deck
    # presentation-ready out of the box, like the planner path.
    for sp in slides:
        if sp.notes:
            continue
        lead = (sp.bullets[0] if sp.bullets else "").strip()
        note = sp.title
        if lead and lead != sp.title:
            note = f"{sp.title}. Key point: {lead}."
        else:
            note = f"{sp.title}."
        if sp.layout == "cover":
            note = f"Open with the topic and why it matters: {sp.title}."
        elif sp.layout == "closing":
            note = "Close by inviting questions and offering to go deeper on any section."
        sp.notes = note

    plan = DeckPlan(
        title=title,
        deck_type="data_report",
        headline_style="assertion",
        summary=summary or (
            f"{title} — analysis framed from the request; "
            "figures are illustrative until grounded in real data."
        ),
        slides=slides,
    )
    logger.info(
        "synthetic_deck: built general no-data plan (slides=%d, title=%.60s, "
        "payload_rich=%s)",
        len(slides), title,
        bool(kpi_specs or findings or recs or sections or chart),
    )
    return plan
