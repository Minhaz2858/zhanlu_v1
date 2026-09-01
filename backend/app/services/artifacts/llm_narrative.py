"""LLM narrative enrichment for the dynamic document pipeline.

The deterministic :func:`app.services.artifacts.architect.synthesize_plan`
owns *structure* — which sections, charts and tables appear, and in what
order — from the data shape and the user's perspective. This module adds the
LLM layer that closes the gap to modern report agents (Kimi / MiniMax /
Claude): it writes the fluent **executive-summary prose** and *selects the
interesting findings* and *targeted recommendations*, instead of the
templated text the architect emits.

This is the hybrid "skill → LLM plans narrative, platform owns structure"
pattern. The skill (e.g. ``/ppt-data-report``) is the entry point; the LLM
follows it to enrich the document with human-like narrative while the
deterministic block skeleton keeps the layout safe and data-accurate.

Design rules (same safety posture as the rest of the pipeline):
  * Pure enhancement — if the LLM is unavailable or errors, the plan is
    returned *unchanged* (the deterministic narrative remains). The export
    never breaks because of this step.
  * Synchronous callers are supported via :func:`enrich_plan_narrative_sync`
    (thread-pool coroutine runner), since the execution→payload builder that
    invokes this runs outside an event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from typing import Any, Optional

from app.services.artifacts.document_plan import DocumentBlock, DocumentPlan

logger = logging.getLogger(__name__)

# Structured-output contract. Matches the keys the renderer expects to find in
# the Executive Summary paragraph / findings / recommendations blocks.
_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "findings", "recommendations"],
}

# Bounds to keep the LLM output disciplined (matches the renderer's capacity).
_MAX_FINDINGS = 6
_MAX_RECOMMENDATIONS = 6


def _run_coro(coro: Any) -> Any:
    """Run a coroutine to completion regardless of the current async context.

    ``asyncio.get_running_loop()`` raises when no loop is active (the common
    case for the synchronous payload builder) — use ``asyncio.run`` directly.
    When called from inside a running loop, a dedicated thread with its own
    loop is used so the call always executes instead of raising
    "This event loop is already running".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def enrich_plan_narrative(
    plan: DocumentPlan,
    *,
    rows: Optional[list] = None,
    columns: Optional[list] = None,
    request_text: str = "",
    user_context: Optional[dict] = None,
) -> DocumentPlan:
    """Add LLM-written prose to a deterministic architect plan.

    Overwrites the "Executive Summary" paragraph and the findings /
    recommendations item lists with LLM-authored content. Returns the same
    plan object (mutated in place) on success, or the *unchanged* plan if the
    LLM call fails for any reason.
    """
    try:
        narrative = await _generate_narrative(
            plan=plan,
            rows=rows or [],
            columns=columns or [],
            request_text=request_text,
            user_context=user_context or {},
        )
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment only
        logger.warning("llm_narrative: skipped (LLM error): %s", exc)
        return plan

    if not narrative:
        return plan

    summary = (narrative.get("executive_summary") or "").strip()
    findings = [f for f in (narrative.get("findings") or []) if isinstance(f, str) and f.strip()]
    recs = [
        r for r in (narrative.get("recommendations") or [])
        if isinstance(r, str) and r.strip()
    ]

    if summary:
        for b in plan.blocks:
            if b.type == "paragraph" and (b.title or "").lower().startswith("executive"):
                b.text = summary
                break

    if findings:
        # Overwrite an existing findings block, else insert one (the architect
        # only emits it when the agent passes findings — the LLM may add them).
        _set_or_insert_block(
            plan,
            lambda b: b.type == "findings",
            DocumentBlock(
                type="findings",
                items=[{"label": "", "text": f} for f in findings[:_MAX_FINDINGS]],
            ),
        )

    if recs:
        _set_or_insert_block(
            plan,
            lambda b: b.type == "recommendations",
            DocumentBlock(
                type="recommendations",
                items=[
                    {"label": "Action", "text": r}
                    for r in recs[:_MAX_RECOMMENDATIONS]
                ],
            ),
        )

    return plan


def _set_or_insert_block(
    plan: DocumentPlan,
    predicate,
    new_block: DocumentBlock,
) -> None:
    """Replace the first block matching ``predicate``; otherwise insert the new
    block before the methodology/appendix tail (so it sits in the narrative
    body, not after the appendix)."""
    for idx, b in enumerate(plan.blocks):
        if predicate(b):
            b.items = new_block.items
            b.text = new_block.text
            b.title = new_block.title or b.title
            return
    # No existing block — find the insertion point: just before methodology or
    # appendix (whichever comes first), else append at the end.
    insert_at = len(plan.blocks)
    for i, b in enumerate(plan.blocks):
        if b.type in ("methodology", "appendix"):
            insert_at = i
            break
    # A divider keeps the inserted section visually distinct in the renderer.
    plan.blocks.insert(insert_at, DocumentBlock(
        type="section_divider",
        title="Key Findings" if new_block.type == "findings" else "Recommendations",
    ))
    plan.blocks.insert(insert_at + 1, new_block)


def enrich_plan_narrative_sync(
    plan: DocumentPlan,
    *,
    rows: Optional[list] = None,
    columns: Optional[list] = None,
    request_text: str = "",
    user_context: Optional[dict] = None,
) -> DocumentPlan:
    """Synchronous wrapper for :func:`enrich_plan_narrative`.

    Safe to call from the synchronous execution→payload builder; runs the async
    LLM call in its own loop/thread.
    """
    return _run_coro(
        enrich_plan_narrative(
            plan,
            rows=rows,
            columns=columns,
            request_text=request_text,
            user_context=user_context,
        )
    )


async def _generate_narrative(
    plan: DocumentPlan,
    *,
    rows: list,
    columns: list,
    request_text: str,
    user_context: dict,
) -> Optional[dict]:
    """One bounded LLM call that returns the structured narrative dict."""
    from app.services.llm_service import call_llm

    kpi_items: list[str] = []
    chart_hints: list[str] = []
    for b in plan.blocks:
        if b.type == "kpi_grid" and b.items:
            kpi_items.extend(
                f"{i.get('label', '')}: {i.get('value', '')}" for i in b.items[:6]
            )
        if b.type == "chart":
            chart_hints.append(b.title or b.chart_type or "chart")

    perspective = (plan.meta or {}).get("perspective") or "balanced"
    role = (user_context or {}).get("role") or "general"

    # A short, anonymized sample of the raw rows (<= 6) for grounding.
    sample: list[Any] = []
    for r in rows[:6]:
        if isinstance(r, dict):
            sample.append({str(k): _short(v) for k, v in r.items()})
        elif isinstance(r, (list, tuple)):
            sample.append([_short(v) for v in r])

    is_exec = (
        perspective == "executive"
        or str(role).lower() in ("ceo", "executive", "cfo", "board", "boss")
    )
    if is_exec:
        audience_line = (
            "Audience: THE CHIEF EXECUTIVE OFFICER (CEO). Write a strategic "
            "DECISION BRIEFING, not an analytical memo.\n"
        )
        extra = (
            "CEO-briefing requirements:\n"
            "- 'executive_summary': open with the single most important business "
            "outcome and the DECISION it implies. Quantify with the headline figures "
            "(e.g. revenue, volume, MoM%). 3-4 decisive sentences, no jargon.\n"
            "- 'findings': 3-4 board-ready insights — concentration risk, margin "
            "erosion, growth drivers, anomalies. Each under 26 words, each grounded "
            "in the numbers above.\n"
            "- 'recommendations': 2-4 concrete STRATEGIC DECISIONS for the CEO to "
            "approve. Write each as '建议：<action>' and state the owner type "
            "(销售 / 生产 / 供应链) and the expected impact. Tie every item to a finding.\n"
        )
    else:
        audience_line = f"Audience: {role} (tone: {perspective}).\n"
        extra = ""

    prompt = (
        "You are a senior business analyst writing a polished report.\n"
        + audience_line
        + f"User's request: {request_text or '(not specified)'}\n"
        + f"Document title: {plan.title}\n"
        + f"Key figures: {kpi_items or 'n/a'}\n"
        + f"Visualizations included: {chart_hints or 'n/a'}\n"
        + f"Data sample (first rows): {json.dumps(sample, ensure_ascii=False)[:1500]}\n\n"
        + extra
        + "Respond with JSON containing exactly three keys:\n"
        "- 'executive_summary': a fluent 2-4 sentence paragraph stating what the "
        "data shows and why it matters. Be specific and lead with the most "
        "important takeaway.\n"
        "- 'findings': 2-4 sharp, data-grounded insights (each under 28 words). "
        "Surface anything notable — trends, concentration, anomalies, outliers.\n"
        "- 'recommendations': 2-4 concrete, actionable next steps tied to the "
        "findings.\n"
        "Return ONLY valid JSON (no markdown fences)."
    )

    result = await asyncio.wait_for(
        call_llm(
            prompt=prompt,
            temperature=0.3,
            task_type="doc_narrative",
            response_json_schema=_NARRATIVE_SCHEMA,
        ),
        timeout=30,
    )
    return _parse_narrative(result)


def _parse_narrative(result: Any) -> Optional[dict]:
    """Extract the narrative dict from a :func:`call_llm` result robustly.

    Handles both schema-parsed results (``result['data']['response']`` may be a
    dict or a JSON string) and plain-text responses (extract the first JSON
    object). Returns ``None`` when nothing usable is found.
    """
    if not isinstance(result, dict):
        return None

    data = result.get("data")
    if isinstance(data, dict):
        resp = data.get("response")
        if isinstance(resp, dict):
            return resp
        if isinstance(resp, str):
            try:
                return json.loads(resp)
            except (ValueError, TypeError):
                pass
    raw = result.get("response")
    if isinstance(raw, str):
        return _extract_json_object(raw)
    return None


def _extract_json_object(raw: str) -> Optional[dict]:
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except (ValueError, TypeError):
        return None
    return None


def _short(v: Any, limit: int = 40) -> str:
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


__all__ = [
    "enrich_plan_narrative",
    "enrich_plan_narrative_sync",
]
