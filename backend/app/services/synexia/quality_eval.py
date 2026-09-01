"""QUALITY_EVAL — combined completeness + reflexion critique with corrective
re-generation.

Post-FINALIZE semantic quality layer for the SynexiaFSM (Tier 2 of the
two-tier quality design, Approach C):

- Tier 1 (VERIFY→re-plan) handles *structural* failures by re-running the
  plan.  Unchanged by this module.
- Tier 2 (QUALITY_EVAL, this module) handles *semantic* failures by
  re-generating the response **text only** — cheap, no tool re-execution.
  This mirrors how Claude Code / Manus revise the writing instead of
  re-running the whole investigation.

A single LLM call judges BOTH completeness (did the output address the task
spec + acceptance criteria?) and quality (reflexion rubric), returning a
structured ``QualityEvalResult``.  When the verdict is ``revise``/``reject``
and the iteration budget remains, the response is re-generated with the
critique as feedback, then re-evaluated.

All functions are TOTAL (never raise).  When the LLM is unavailable or fails
(exception / malformed JSON), the heuristic fallback (failure-marker
detection) is used and the corrective loop is skipped — matching the existing
FSM non-fatal convention (see ``reflexion._fallback_verdict``).

NOTE: this module supersedes the pre-finalize ``verify_with_llm`` path
(``SYNEXIA_VERIFIER_LLM_ENABLED``).  That flag is intentionally left OFF —
running a rubric pass *before* the response exists cannot drive correction,
so it would double-spend an LLM call.  The semantic LLM judgment lives here,
post-response, where it CAN correct.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class QualityEvalResult:
    """Combined completeness + reflexion verdict on a generated response."""

    verdict: str = "accept"           # accept | revise | reject
    completeness_score: float = 0.0   # 0..1 — did output address task spec?
    confidence: float = 0.0           # 0..1 — overall quality confidence
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    iterations: int = 0               # corrective iterations actually run
    final_text: str = ""              # the (possibly revised) assistant text
    raw: str = ""                     # raw LLM response / "heuristic-only"

    @property
    def is_ok(self) -> bool:
        """True only for an accept verdict with adequate completeness."""
        return self.verdict == "accept" and self.completeness_score >= 0.5

    def to_dict(self) -> dict:
        """For storage in ``confidence_factors["quality_eval"]``."""
        return {
            "verdict": self.verdict,
            "completeness_score": self.completeness_score,
            "confidence": self.confidence,
            "issues": self.issues,
            "suggestions": self.suggestions,
            "iterations": self.iterations,
            "is_ok": self.is_ok,
        }


@dataclass
class DocQualityResult:
    """Multi-dimensional quality verdict on a generated document payload."""

    verdict: str = "accept"           # accept | revise | reject
    structure_score: float = 0.0      # 0..1
    content_score: float = 0.0        # 0..1
    visual_score: float = 0.0         # 0..1
    completeness_score: float = 0.0   # 0..1
    density_score: float = 0.0        # 0..1
    overall_score: float = 0.0        # weighted average
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def is_ok(self) -> bool:
        return self.verdict == "accept" and self.overall_score >= 0.6

    @property
    def weighted_score(self) -> float:
        """Composite: structure 25%, content 30%, visual 15%, completeness 20%, density 10%."""
        return (
            self.structure_score * 0.25
            + self.content_score * 0.30
            + self.visual_score * 0.15
            + self.completeness_score * 0.20
            + self.density_score * 0.10
        )

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "structure_score": self.structure_score,
            "content_score": self.content_score,
            "visual_score": self.visual_score,
            "completeness_score": self.completeness_score,
            "density_score": self.density_score,
            "overall_score": self.overall_score,
            "confidence": self.confidence,
            "issues": self.issues,
            "suggestions": self.suggestions,
            "is_ok": self.is_ok,
        }


# ── Prompt templates ──────────────────────────────────────────────────────

# Combined rubric: judges completeness + quality in ONE call (halves cost vs
# two separate calls).
_EVAL_TEMPLATE = """\
You are reviewing the assistant's reply for the user's request below.

User request:
{user_message}

Acceptance criteria for this task (the reply should satisfy each):
{acceptance_criteria}

Assistant reply (truncated to {max_chars} chars):
{assistant_text}

Judge the reply on TWO dimensions:
1. COMPLETENESS: does the reply address the user's request and satisfy the
   acceptance criteria?
2. QUALITY: is the reply accurate, well-structured, and free of errors?

Return JSON ONLY with the keys:
  "verdict"           : "accept" | "revise" | "reject"
  "completeness_score": number between 0 and 1 (1 = fully addresses request)
  "confidence"        : number between 0 and 1 (overall quality confidence)
  "issues"            : array of short strings (empty if verdict=accept)
  "suggestions"       : array of short strings (empty if verdict=accept)

Reject when the reply contradicts a known fact, contains an obvious fatal
error (404 / not found / failed), or misses a required deliverable.  Revise
when the reply is on-topic but incomplete or a section could be improved.
Accept when the reply satisfies the request without serious issues.
"""

_REGEN_TEMPLATE = """\
You are revising your earlier reply to better satisfy the user's request.

User request:
{user_message}

Your earlier reply:
{original_text}

Issues found in the earlier reply:
{issues}

Suggestions for improvement:
{suggestions}

Original response instructions:
{response_prompt}

Write a revised reply that addresses every issue and incorporates the
suggestions.  Output ONLY the revised reply text — no preamble, no JSON.
"""

# ── Document-specific rubric ───────────────────────────────────────────
# Added Phase 3: evaluates generated document payloads against template
# quality criteria — structure, visual design, data-density, and completeness.

_DOC_EVAL_TEMPLATE = """\
You are reviewing a generated {artifact_type} document payload for the user's request below.

User request:
{user_message}

Document type: {artifact_type}
Template applied: {template_name}

Document payload (JSON):
{payload_json}

Quality criteria for this template (the document SHOULD satisfy each):
{quality_criteria}

Rate the document payload on the following dimensions (0..1 scale each):

1. STRUCTURE — Are sections in the correct order? Does the document follow
   the expected outline? Are section titles appropriate?

2. CONTENT_QUALITY — Is the content accurate, professional, and tailored to
   the audience? Are numbers/data correctly cited? Is the tone appropriate?

3. VISUAL_HINTS — Does the payload include or suggest appropriate visual
   elements (chart types, layout hints, KPI placements)? If pptx, are
   slide count and slide structure reasonable?

4. COMPLETENESS — Are all required sections present? Do optional sections
   exist where data supports them? Are KPIs and charts populated from data?

5. DATA_DENSITY — Is there enough content (not too sparse, not wall of text)?
   Are bullet points substantive? Is each slide/section focused on a single point?

Return JSON ONLY with the keys:
  "verdict"           : "accept" | "revise" | "reject"
  "structure_score"   : number between 0 and 1
  "content_score"     : number between 0 and 1
  "visual_score"      : number between 0 and 1
  "completeness_score": number between 0 and 1
  "density_score"     : number between 0 and 1
  "overall_score"     : number between 0 and 1 (weighted average)
  "confidence"        : number between 0 and 1
  "issues"            : array of short strings (empty if verdict=accept)
  "suggestions"       : array of short strings (empty if verdict=accept)

Reject when: critical sections are missing, content is factually wrong,
or the payload is empty/truncated. Revise when: the document is usable
but could be significantly improved. Accept when: all criteria are met
at an adequate level.
"""

# Same failure markers as reflexion._fallback_verdict (kept in sync).
_FAILURE_MARKERS = ("404", "not found", "failed to load", "internal server error")

# Apology markers: the agent couldn't synthesize an answer despite having data.
# Parallel to the _APOLOGY_PATTERN_RE in agents.py but simpler (substring match).
_APOLOGY_MARKERS = (
    "had trouble putting it all together",
    "unable to put", "unable to synthesize", "unable to compile",
    "couldn't put together", "could not put together",
    "couldn't synthesize", "could not synthesize",
)

# Bounce-back markers: the agent dumped raw data and invited the user to
# "ask for a summary" instead of actually answering. This is a non-answer
# and must be flagged for revision.
_BOUNCE_BACK_MARKERS = (
    "i retrieved", "retrieved 1 rows", "retrieved 1 records",
    "you can ask me", "ask me for a summary", "ask me for a breakdown",
    "ask me for a chart", "would you like me to",
    "feel free to ask", "let me know if you",
    # Chinese
    "你可以让我", "需要我提供", "是否需要我", "我可以提供",
)


# ── Helpers (mirrors reflexion.py — kept here so the module is self-contained) ─

def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        v = float(v)
    except Exception:
        v = 0.5
    return max(lo, min(hi, v))


def _heuristic_verdict(assistant_text: str) -> QualityEvalResult:
    """No-LLM fallback: failure-marker + apology + bounce-back detection."""
    text = (assistant_text or "").lower()
    if any(m in text for m in _FAILURE_MARKERS):
        return QualityEvalResult(
            verdict="revise",
            completeness_score=0.3,
            confidence=0.3,
            issues=["detected failure marker in assistant text"],
            raw="heuristic-only",
        )
    # Apology: agent couldn't synthesize despite having data.
    if any(m in text for m in _APOLOGY_MARKERS):
        return QualityEvalResult(
            verdict="revise",
            completeness_score=0.25,
            confidence=0.4,
            issues=["apology: agent couldn't synthesize answer from data"],
            suggestions=[
                "Write a comprehensive answer using the data you already have.",
                "Do NOT apologize — just answer the question.",
            ],
            raw="heuristic-apology",
        )
    # Bounce-back: agent dumped data + invited user to re-ask.
    if any(m in text for m in _BOUNCE_BACK_MARKERS):
        return QualityEvalResult(
            verdict="revise",
            completeness_score=0.2,
            confidence=0.4,
            issues=["bounce-back: agent dumped raw data instead of answering"],
            suggestions=[
                "Write a comprehensive answer using the data you already have.",
                "Do NOT invite the user to ask again.",
                "Do NOT dump raw tables — synthesize the data into prose.",
            ],
            raw="heuristic-bounce-back",
        )
    return QualityEvalResult(
        verdict="accept",
        completeness_score=0.5,
        confidence=0.6,
        raw="heuristic-only",
    )


def _format_acceptance_criteria(task_spec: dict) -> str:
    """Render the task's acceptance criteria for the eval prompt.

    Falls back to ``kpis`` when ``acceptance_criteria`` is absent (the older
    TaskSpec field) so GOAL-time criteria are honoured even before the
    acceptance_criteria field is fully populated.
    """
    criteria = (task_spec or {}).get("acceptance_criteria") or []
    if not criteria:
        criteria = (task_spec or {}).get("kpis") or []
    if not criteria:
        return "(none specified — judge whether the reply addresses the request)"
    return "\n".join(f"- {c}" for c in criteria)


# ── Public API ────────────────────────────────────────────────────────────

def evaluate_quality(
    *,
    user_message: str,
    assistant_text: str,
    task_spec: dict,
    llm_call: Optional[Callable] = None,
    max_chars: int = 4000,
) -> QualityEvalResult:
    """Run the combined completeness + reflexion critique (1 LLM call).

    Never raises.  When ``llm_call`` is None or fails (exception / malformed
    JSON), the heuristic fallback is used.
    """
    if llm_call is None:
        return _heuristic_verdict(assistant_text)

    prompt = _EVAL_TEMPLATE.format(
        user_message=(user_message or "")[:max_chars],
        acceptance_criteria=_format_acceptance_criteria(task_spec),
        assistant_text=(assistant_text or "")[:max_chars],
        max_chars=max_chars,
    )
    messages = [
        {"role": "system", "content": "You are a strict editor. Reply with JSON only."},
        {"role": "user", "content": prompt},
    ]
    try:
        result = llm_call(prompt=prompt, messages=messages, temperature=0)
    except Exception as exc:
        logger.warning("quality_eval: llm_call raised (non-fatal): %s", exc)
        return _heuristic_verdict(assistant_text)

    raw = ""
    if isinstance(result, dict):
        raw = result.get("response", "")
    elif isinstance(result, str):
        raw = result

    parsed = _extract_json(raw)
    if not parsed:
        logger.debug("quality_eval: could not parse JSON; heuristic fallback")
        return _heuristic_verdict(assistant_text)

    verdict = str(parsed.get("verdict", "accept")).lower()
    if verdict not in ("accept", "revise", "reject"):
        verdict = "accept"
    issues = [str(x) for x in (parsed.get("issues") or []) if str(x).strip()]
    suggestions = [str(x) for x in (parsed.get("suggestions") or []) if str(x).strip()]
    return QualityEvalResult(
        verdict=verdict,
        completeness_score=_clamp(parsed.get("completeness_score", 0.5)),
        confidence=_clamp(parsed.get("confidence", 0.5)),
        issues=issues,
        suggestions=suggestions,
        raw=raw,
    )


def regenerate_with_feedback(
    *,
    user_message: str,
    original_text: str,
    critique: QualityEvalResult,
    response_prompt: str,
    llm_call: Optional[Callable] = None,
) -> str:
    """Re-generate the response with the critique as feedback (1 LLM call).

    Never raises.  When ``llm_call`` is None or fails, ``original_text`` is
    returned unchanged.
    """
    if llm_call is None:
        return original_text

    prompt = _REGEN_TEMPLATE.format(
        user_message=(user_message or "")[:4000],
        original_text=(original_text or "")[:4000],
        issues="\n".join(f"- {i}" for i in critique.issues) or "(none)",
        suggestions="\n".join(f"- {s}" for s in critique.suggestions) or "(none)",
        response_prompt=(response_prompt or "")[:4000],
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = llm_call(prompt=prompt, messages=messages, temperature=0.7)
    except Exception as exc:
        logger.warning("quality_eval: regenerate llm_call raised (non-fatal): %s", exc)
        return original_text

    if isinstance(result, dict):
        text = result.get("response", "")
    elif isinstance(result, str):
        text = result
    else:
        text = ""
    return text.strip() or original_text


def run_quality_loop(
    *,
    user_message: str,
    initial_text: str,
    task_spec: dict,
    response_prompt: str,
    max_iterations: int = 2,
    llm_call: Optional[Callable] = None,
) -> QualityEvalResult:
    """Orchestrate the bounded corrective re-generation loop.

    1. Evaluate the initial text (1 LLM call).
    2. If accept → return immediately (0 corrective iterations).
    3. If revise/reject and iterations remain → regenerate with feedback
       (1 call), re-evaluate (1 call).  Repeat up to ``max_iterations``.

    Returns a ``QualityEvalResult`` with ``final_text`` set to the best
    version and ``iterations`` counting corrective cycles actually run.
    Never raises.

    Cost bound: 0 extra calls on a clean accept; up to
    ``1 + max_iterations * 2`` calls otherwise (1 initial eval + per
    iteration: 1 regenerate + 1 re-eval).
    """
    current_text = initial_text
    result = evaluate_quality(
        user_message=user_message,
        assistant_text=current_text,
        task_spec=task_spec,
        llm_call=llm_call,
    )
    iterations = 0
    while not result.is_ok and iterations < max_iterations:
        # Without an LLM the heuristic can't improve the text — stop.
        if llm_call is None:
            break
        current_text = regenerate_with_feedback(
            user_message=user_message,
            original_text=current_text,
            critique=result,
            response_prompt=response_prompt,
            llm_call=llm_call,
        )
        iterations += 1
        result = evaluate_quality(
            user_message=user_message,
            assistant_text=current_text,
            task_spec=task_spec,
            llm_call=llm_call,
        )
    result.final_text = current_text
    result.iterations = iterations
    return result


# ── Standalone quality eval (non-FSM paths) ──────────────────────────


def evaluate_response_quality(
    user_message: str,
    assistant_text: str,
    task_spec: dict | None = None,
    response_prompt: str = "",
    max_iterations: int = 2,
) -> QualityEvalResult:
    """Standalone quality evaluation for non-FSM code paths.

    This is the entry point for ReAct (v2) and v3 non-FSM streaming
    paths that don't have an ``SynexiaFSM`` instance. It builds an
    ``llm_call`` bridge from ``llm_service.call_llm`` and delegates to
    ``run_quality_loop``.

    Feature-gated by ``QUALITY_EVAL_ALL_PATHS`` (default True when
    ``SYNEXIA_QUALITY_EVAL_ENABLED`` is True). Never raises — returns
    ``QualityEvalResult(verdict="accept")`` with ``final_text`` unchanged
    when quality eval is disabled or LLM unavailable.
    """
    from app.config import settings

    # Feature gate
    if not getattr(settings, "SYNEXIA_QUALITY_EVAL_ENABLED", True):
        return QualityEvalResult(
            verdict="accept",
            final_text=assistant_text,
            raw="quality_eval_disabled",
        )
    if not getattr(settings, "QUALITY_EVAL_ALL_PATHS", True):
        return QualityEvalResult(
            verdict="accept",
            final_text=assistant_text,
            raw="quality_eval_all_paths_disabled",
        )

    spec = task_spec or {}

    # Build an async→sync llm_call bridge via asyncio.
    # Always spin a NEW event loop: this bridge is invoked from worker
    # threads (via asyncio.to_thread in the v3 streaming paths), never
    # from the running loop itself.  Using get_running_loop() here would
    # raise "RuntimeError: This event loop is already running" whenever a
    # loop IS running (the old bug), or create an unclosed loop otherwise.
    # (Fixed 2026-08-17.)
    def _sync_llm_bridge(prompt="", messages=None, temperature=0.0):
        """Sync bridge for quality eval's `llm_call` parameter."""
        import asyncio
        from app.services.llm_service import call_llm

        result = call_llm(
            prompt=prompt,
            messages=messages,
            temperature=temperature,
            task_type="simple_chat",
        )
        # Production call_llm is async → run it in a fresh loop (we are in a
        # worker thread via asyncio.to_thread).  Tests may inject a plain
        # sync stub returning a dict → pass it through untouched.
        if not asyncio.iscoroutine(result):
            return result
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(result)
        finally:
            try:
                loop.close()
            except Exception:  # pragma: no cover — loop teardown best-effort
                pass

    return run_quality_loop(
        user_message=user_message,
        initial_text=assistant_text,
        task_spec=spec,
        response_prompt=response_prompt,
        max_iterations=max_iterations,
        llm_call=_sync_llm_bridge,
    )


# ── Document quality evaluation ────────────────────────────────────────


def evaluate_document_quality(
    *,
    user_message: str,
    artifact_type: str,
    payload: dict,
    llm_call: Optional[Callable] = None,
    template_name: str = "default",
    quality_criteria: Optional[list[str]] = None,
) -> DocQualityResult:
    """Evaluate a generated document payload against template quality criteria.

    When a template is detected (via :func:`app.services.templates.detect_template`),
    its ``quality_criteria`` list is used; otherwise a generic rubric is applied.

    Never raises. Returns a ``DocQualityResult`` with dimensional scores.

    Args:
        user_message: The user's original request.
        artifact_type: One of pptrx / docx / pdf / html.
        payload: The document's source_json (ReportCardPayload dict).
        llm_call: Optional callable (same signature as ``evaluate_quality``).
        template_name: Name of the applied template (informational).
        quality_criteria: Optional override list of criteria strings.
    """
    import json as _json

    # Resolve quality criteria — templates module was removed in favor of
    # the C-Heavy skill-driven runner, so we use a generic rubric.  If
    # a future design wants template-specific criteria again, this is
    # the place to plug them in.
    if not quality_criteria:
        quality_criteria = [
            "Document follows a clear structure with defined sections",
            "Content is accurate, professional, and on-topic",
            "Appropriate visual elements (charts, KPIs) are present",
            "All required sections are included and populated",
            "Content density is balanced — not too sparse, not overwhelming",
        ]

    # Serialize payload for the LLM (cap depth to 5 levels, cap string lengths)
    try:
        payload_json = _json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except Exception:
        payload_json = str(payload)
    # Crude cap: 8000 chars
    if len(payload_json) > 8000:
        payload_json = payload_json[:8000] + "\n... [truncated]"

    if llm_call is None:
        return _heuristic_doc_verdict(payload, artifact_type)

    prompt = _DOC_EVAL_TEMPLATE.format(
        artifact_type=artifact_type,
        template_name=template_name,
        user_message=(user_message or "")[:4000],
        payload_json=payload_json,
        quality_criteria="\n".join(f"- {c}" for c in quality_criteria),
    )
    messages = [
        {"role": "system", "content": "You are a strict document reviewer. Reply with JSON only."},
        {"role": "user", "content": prompt},
    ]
    try:
        result = llm_call(prompt=prompt, messages=messages, temperature=0)
    except Exception as exc:
        logger.warning("evaluate_document_quality: llm_call raised (non-fatal): %s", exc)
        return _heuristic_doc_verdict(payload, artifact_type)

    raw = ""
    if isinstance(result, dict):
        raw = result.get("response", "")
    elif isinstance(result, str):
        raw = result

    parsed = _extract_json(raw)
    if not parsed:
        return _heuristic_doc_verdict(payload, artifact_type)

    verdict = str(parsed.get("verdict", "accept")).lower()
    if verdict not in ("accept", "revise", "reject"):
        verdict = "accept"

    def _s(k: str) -> float:
        return _clamp(parsed.get(k, 0.5))

    overall = _s("overall_score")
    # If overall wasn't returned, compute weighted average
    if overall == 0.5 and "structure_score" not in parsed:
        overall = _s("completeness_score")  # fall back to single score

    return DocQualityResult(
        verdict=verdict,
        structure_score=_s("structure_score"),
        content_score=_s("content_score"),
        visual_score=_s("visual_score"),
        completeness_score=_s("completeness_score"),
        density_score=_s("density_score"),
        overall_score=overall,
        confidence=_clamp(parsed.get("confidence", 0.5)),
        issues=[str(x) for x in (parsed.get("issues") or []) if str(x).strip()],
        suggestions=[str(x) for x in (parsed.get("suggestions") or []) if str(x).strip()],
        raw=raw,
    )


def _heuristic_doc_verdict(payload: dict, artifact_type: str) -> DocQualityResult:
    """No-LLM fallback for document quality evaluation.

    Performs lightweight heuristic checks:
    - Does the payload have a title?
    - Does it have kpis/slides/content?
    - Is it empty/near-empty?
    """
    issues: list[str] = []
    suggestions: list[str] = []

    title = payload.get("title", "")
    if not title:
        issues.append("Missing title")
        suggestions.append("Add a descriptive title")

    structure_score = 0.7
    content_score = 0.7
    visual_score = 0.7
    completeness_score = 0.7
    density_score = 0.7

    # Check for core content fields
    kpis = payload.get("kpis", [])
    slides = payload.get("slides", [])
    sections = payload.get("sections", [])
    insights = payload.get("insights", [])
    chart = payload.get("chart")
    summary = payload.get("summary", "")
    key_findings = payload.get("key_findings", [])

    has_content = bool(kpis or slides or sections or insights or summary or key_findings)
    has_visual = bool(chart or kpis or slides)

    if not has_content:
        issues.append("No content sections found in payload")
        suggestions.append("Add KPIs, slides, or text sections")
        content_score = 0.3
        completeness_score = 0.3

    if not has_visual:
        visual_score = 0.4
        suggestions.append("Add a chart or KPI visual element")

    # Check sparse content
    total_text_chars = len(str(payload))
    if total_text_chars < 100:
        issues.append("Payload appears nearly empty")
        density_score = 0.2
        completeness_score = 0.2

    if not title or not has_content:
        return DocQualityResult(
            verdict="revise",
            structure_score=structure_score,
            content_score=content_score,
            visual_score=visual_score,
            completeness_score=completeness_score,
            density_score=density_score,
            overall_score=0.4,
            confidence=0.5,
            issues=issues,
            suggestions=suggestions,
            raw="heuristic-only",
        )

    overall = (
        structure_score * 0.25
        + content_score * 0.30
        + visual_score * 0.15
        + completeness_score * 0.20
        + density_score * 0.10
    )

    return DocQualityResult(
        verdict="accept" if overall >= 0.5 and not issues else "revise",
        structure_score=structure_score,
        content_score=content_score,
        visual_score=visual_score,
        completeness_score=completeness_score,
        density_score=density_score,
        overall_score=overall,
        confidence=0.6,
        issues=issues,
        suggestions=suggestions,
        raw="heuristic-only",
    )


def _derive_skill_required_elements(skill_body: str) -> list[str]:
    """Heuristically infer required output elements from a skill body."""
    lower = (skill_body or "").lower()
    requirements: list[str] = []
    checks = [
        ("summary", ("executive summary", "summary")),
        ("methodology", ("methodology", "data source", "how the data was gathered")),
        ("kpis", ("kpi", "metric", "metrics")),
        ("chart", ("chart", "graph", "visual")),
        ("insights", ("insight", "finding", "findings")),
        ("recommendations", ("recommendation", "next step", "action item")),
        ("sections", ("section", "outline", "structure")),
    ]
    for key, tokens in checks:
        if any(token in lower for token in tokens):
            requirements.append(key)
    return requirements


def validate_selected_skill_payload(
    *,
    skill_name: str,
    skill_body: str,
    artifact_type: str,
    payload: dict,
) -> dict:
    """Heuristically validate whether a payload matches the selected skill.

    Runs the generic document-quality heuristic, then overlays hard checks
    inferred from the selected skill's methodology body.
    """
    doc = evaluate_document_quality(
        user_message=skill_name,
        artifact_type=artifact_type,
        payload=payload,
        llm_call=None,
        template_name=skill_name or "selected-skill",
    )

    required = _derive_skill_required_elements(skill_body)
    missing: list[str] = []

    field_checks = {
        "summary": bool(payload.get("summary")),
        "methodology": bool(payload.get("methodology")),
        "kpis": bool(payload.get("kpis")),
        "chart": bool(payload.get("chart")),
        "insights": bool(payload.get("insights") or payload.get("key_findings")),
        "recommendations": bool(payload.get("recommendations") or payload.get("next_step")),
        "sections": bool(payload.get("sections") or payload.get("slides")),
    }
    for req in required:
        if not field_checks.get(req, False):
            missing.append(req)

    issues = list(doc.issues)
    suggestions = list(doc.suggestions)
    if missing:
        issues.append("Missing selected-skill requirements: " + ", ".join(missing))
        suggestions.append("Populate the fields required by the selected skill: " + ", ".join(missing))

    overall = doc.overall_score
    if missing:
        overall = min(overall, 0.45)

    verdict = doc.verdict
    if missing and verdict == "accept":
        verdict = "revise"

    is_ok = verdict == "accept" and overall >= 0.6 and not missing
    return {
        "skill_name": skill_name,
        "artifact_type": artifact_type,
        "required_elements": required,
        "missing_elements": missing,
        "issues": issues,
        "suggestions": suggestions,
        "overall_score": overall,
        "verdict": verdict,
        "is_ok": is_ok,
        "doc_quality": doc.to_dict(),
    }


__all__ = [
    "QualityEvalResult",
    "DocQualityResult",
    "evaluate_quality",
    "evaluate_document_quality",
    "validate_selected_skill_payload",
    "regenerate_with_feedback",
    "run_quality_loop",
]
