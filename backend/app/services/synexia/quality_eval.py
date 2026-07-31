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

# Same failure markers as reflexion._fallback_verdict (kept in sync).
_FAILURE_MARKERS = ("404", "not found", "failed to load", "internal server error")


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
    """No-LLM fallback: failure-marker detection (mirrors reflexion)."""
    text = (assistant_text or "").lower()
    if any(m in text for m in _FAILURE_MARKERS):
        return QualityEvalResult(
            verdict="revise",
            completeness_score=0.3,
            confidence=0.3,
            issues=["detected failure marker in assistant text"],
            raw="heuristic-only",
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


__all__ = [
    "QualityEvalResult",
    "evaluate_quality",
    "regenerate_with_feedback",
    "run_quality_loop",
]
