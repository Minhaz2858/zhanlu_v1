"""LLM copy-polish pass for decks.

Runs ONCE after the deck is planned and rendered — *not* inside the audit /
repair loop — to avoid runaway LLM cost.  It tightens headlines (≤8 words),
shortens bullets (≤12 words), and writes 1–2 sentence speaker notes per slide,
then the deck is re-rendered through the layout engine.

The polish is conservative and structural: it may only *edit text*, never add,
remove, or reorder slides (that would break the planner's narrative contract
and the chart/kpi bindings).  On any LLM failure, timeout, or structural
mismatch it returns the original plan unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.services.synexia.contracts import DeckPlan, SlidePlan

logger = logging.getLogger(__name__)

_POLISH_TIMEOUT_S = 10.0

# The polish only asks the LLM to rewrite text fields — structure is preserved
# by re-attaching the original layout / chart / kpi / narrative_role.
_POLISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "headline_style": {"type": "string", "enum": ["topic", "assertion"]},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["title", "slides"],
}


def _build_polish_prompt(plan: DeckPlan, user_message: str) -> str:
    lines: list[str] = []
    for i, slide in enumerate(plan.slides, 1):
        lines.append(f"Slide {i} [{slide.layout}] {slide.title}")
        if slide.subtitle:
            lines.append(f"  subtitle: {slide.subtitle}")
        for b in slide.bullets:
            lines.append(f"  - {b}")
    body = "\n".join(lines) or "(empty deck)"

    return (
        "You are a presentation copy editor. Tighten the copy of this deck "
        "while preserving its structure, slide order, and layouts.\n\n"
        f"USER INTENT:\n{user_message or plan.title}\n\n"
        f"CURRENT DECK:\n{body}\n\n"
        "Rules:\n"
        "- Return valid JSON matching the requested schema.\n"
        "- Return EXACTLY one slide object per input slide, in the same order.\n"
        "- Tighten each title to at most 8 words.\n"
        "- Rewrite topic-label titles into assertion headlines (a full sentence "
        "stating the takeaway), not a noun phrase.\n"
        "- Shorten each bullet to at most 12 words; drop filler and redundancies.\n"
        "- Write 1-2 sentence speaker notes per slide.\n"
        "- Do NOT add, remove, or reorder slides; do NOT change layouts.\n"
    )


def _extract_data(result: Any) -> Optional[dict[str, Any]]:
    """Best-effort extraction of the JSON dict from a ``call_llm`` result."""
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        return data
    raw = result.get("response")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _apply_polish(plan: DeckPlan, data: dict[str, Any]) -> DeckPlan:
    """Merge the LLM's tightened text back onto the original slide structure."""
    slides_out = data.get("slides")
    if not isinstance(slides_out, list) or len(slides_out) != len(plan.slides):
        logger.warning("copy_polish: slide count mismatch (%s); keeping original", "?")
        return plan

    polished: list[SlidePlan] = []
    for orig, item in zip(plan.slides, slides_out):
        if not isinstance(item, dict):
            polished.append(orig)
            continue
        bullets = item.get("bullets")
        polished.append(
            SlidePlan(
                layout=orig.layout,
                title=item.get("title") or orig.title,
                subtitle=item.get("subtitle", orig.subtitle) or "",
                bullets=list(bullets) if isinstance(bullets, list) else list(orig.bullets),
                chart_spec=orig.chart_spec,
                kpi_specs=orig.kpi_specs,
                notes=item.get("notes", orig.notes) or "",
                narrative_role=orig.narrative_role,
                headline_style=item.get("headline_style", orig.headline_style) or orig.headline_style,
                max_bullets=orig.max_bullets,
                max_words_per_bullet=orig.max_words_per_bullet,
            )
        )

    polished_plan = plan.model_copy(
        update={"title": data.get("title") or plan.title, "slides": polished}
    )
    # Guarantee assertion-style headlines after polish (deterministic; no extra
    # LLM call beyond the polish itself).
    from app.services.artifacts.deck_planner import _enforce_assertion_headlines

    return _enforce_assertion_headlines(polished_plan)


async def polish_deck(
    plan: DeckPlan,
    rows: Optional[list[dict[str, Any]]] = None,
    user_message: str = "",
) -> DeckPlan:
    """Polish a deck's copy via a single bounded LLM call.

    ``rows`` is accepted for signature parity with the planner (and future
    data-aware polish); it is not currently consulted.  Returns the polished
    plan, or the original plan unchanged on any failure/timeout.
    """
    if not plan or not plan.slides:
        return plan

    from app.services.llm_service import call_llm

    prompt = _build_polish_prompt(plan, user_message)
    try:
        result = await asyncio.wait_for(
            call_llm(
                prompt=prompt,
                temperature=0.4,
                response_json_schema=_POLISH_SCHEMA,
                task_type="deck_copy_polish",
            ),
            timeout=_POLISH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("copy_polish: LLM timed out after %ss", _POLISH_TIMEOUT_S)
        return plan
    except Exception as e:  # noqa: BLE001 — polish must never break a render
        logger.warning("copy_polish: LLM call failed: %s", e)
        return plan

    data = _extract_data(result)
    if not data:
        logger.warning("copy_polish: LLM returned non-dict output; keeping original")
        return plan

    return _apply_polish(plan, data)
