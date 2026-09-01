"""Reflexion: LLM-rubric self-critique on plan outputs.

The SynexiaFSM VERIFY state already runs *deterministic* checks (schema
validity, status flags, presence of expected artifacts).  This module
adds an *optional* LLM-driven critique pass that catches things a
deterministic check cannot — tone, completeness, off-by-one summaries,
missed constraints, etc.

The pass is gated by ``SYNEXIA_VERIFIER_LLM_ENABLED`` so it costs
nothing when disabled and is feature-flagged for first deploy.

The rubric is intentionally small (5 prompts) so the call is fast
(~1 second on a typical LLM).  The critique returns a structured
``ReflexionVerdict`` so the FSM can branch on it without re-parsing
free-form text.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReflexionVerdict:
    """Structured self-critique from the LLM rubric."""

    verdict: str = "accept"   # accept | revise | reject
    confidence: float = 0.0   # 0..1
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def is_ok(self) -> bool:
        return self.verdict == "accept" and self.confidence >= 0.5


# The rubric is a single user message; the model is asked to respond
# in strict JSON so parsing is reliable.
_RUBRIC_TEMPLATE = """\
You are reviewing the assistant's last reply for the user's request below.

User request:
{user_message}

Assistant reply (truncated to {max_chars} chars):
{assistant_text}

Rate the reply on a 0..1 scale and return JSON ONLY with the keys:
  "verdict"     : "accept" | "revise" | "reject"
  "confidence"  : number between 0 and 1
  "issues"      : array of short strings (empty if verdict=accept)
  "suggestions" : array of short strings (empty if verdict=accept)

Reject when the reply contradicts a known fact, contains an obvious
fatal error (404 / not found / failed), or misses a required deliverable.
Revise when the reply is on-topic but a paragraph could be tightened, a
summary is missing, or a key fact could be re-stated.  Accept when the
reply satisfies the request without serious issues.
"""


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return None
    # Try direct parse first; if that fails, grab the first {...} block.
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


def _enabled() -> bool:
    """Check the feature flag without pulling in app.config at import time."""
    if os.environ.get("SYNEXIA_VERIFIER_LLM_ENABLED", "").lower() in ("1", "true", "yes"):
        return True
    try:
        from app.config import settings  # type: ignore

        return bool(getattr(settings, "SYNEXIA_VERIFIER_LLM_ENABLED", False))
    except Exception:
        return False


def _fallback_verdict(assistant_text: str) -> ReflexionVerdict:
    """A no-LLM fallback so the call is total and never blocks the FSM.

    If the reply contains tell-tale failure markers we return a
    ``revise`` verdict; otherwise ``accept`` with a low confidence so
    the FSM still moves forward.
    """
    text = (assistant_text or "").lower()
    fatal = ("404", "not found", "failed to load", "internal server error")
    if any(m in text for m in fatal):
        return ReflexionVerdict(
            verdict="revise",
            confidence=0.3,
            issues=["detected failure marker in assistant text"],
            raw="heuristic-only",
        )
    return ReflexionVerdict(verdict="accept", confidence=0.6, raw="heuristic-only")


async def critique(
    *,
    user_message: str,
    assistant_text: str,
    llm_call: Optional[Any] = None,  # async (messages) -> str
    max_chars: int = 4000,
) -> ReflexionVerdict:
    """Run the rubric; never raises.

    Args:
        user_message:    The user's request.
        assistant_text:  The assistant's reply.
        llm_call:        Optional async callable ``(messages) -> str``.  When
                         ``None`` or when ``SYNEXIA_VERIFIER_LLM_ENABLED`` is
                         off, the heuristic fallback is used.
        max_chars:       Cap on the rubric prompt's assistant excerpt.

    Returns:
        A :class:`ReflexionVerdict`.  Always populated, even on total
        failure (e.g. malformed JSON, timeout) — the FSM relies on
        this call being total.
    """
    if not _enabled() or llm_call is None:
        return _fallback_verdict(assistant_text)

    prompt = _RUBRIC_TEMPLATE.format(
        user_message=(user_message or "")[:max_chars],
        assistant_text=(assistant_text or "")[:max_chars],
        max_chars=max_chars,
    )
    messages = [
        {"role": "system", "content": "You are a strict editor. Reply with JSON only."},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = await llm_call(messages)
    except Exception as exc:
        logger.warning("reflexion: llm_call raised (non-fatal): %s", exc)
        return _fallback_verdict(assistant_text)

    parsed = _extract_json(raw)
    if not parsed:
        logger.debug("reflexion: could not parse JSON; falling back to heuristic")
        return _fallback_verdict(assistant_text)

    verdict = str(parsed.get("verdict", "accept")).lower()
    if verdict not in ("accept", "revise", "reject"):
        verdict = "accept"
    try:
        conf = float(parsed.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    issues = [str(x) for x in (parsed.get("issues") or []) if str(x).strip()]
    suggestions = [str(x) for x in (parsed.get("suggestions") or []) if str(x).strip()]
    return ReflexionVerdict(
        verdict=verdict,
        confidence=conf,
        issues=issues,
        suggestions=suggestions,
        raw=raw,
    )


# ── Document-specific critique ────────────────────────────────────────
# Phase 3: document payload review with template-aware quality criteria.

_DOC_RUBRIC_TEMPLATE = """\
You are reviewing a generated {artifact_type} document payload for the user's request below.

User request:
{user_message}

Document payload (JSON):
{payload_json}

Template: {template_name}
Quality criteria:
{quality_criteria}

Rate the document on:
- Structure: logical section ordering, appropriate slide/section types
- Content: accuracy, professionalism, audience-appropriateness
- Visuals: chart presence, KPI layout, visual-hint relevance
- Completeness: required sections present, data populated
- Density: neither too sparse nor too dense

Return JSON ONLY with:
  "verdict"      : "accept" | "revise" | "reject"
  "confidence"   : number between 0 and 1
  "issues"       : array of short strings
  "suggestions"  : array of short strings
  "score_breakdown": {{ "structure": 0..1, "content": 0..1, "visual": 0..1, "completeness": 0..1, "density": 0..1 }}
"""


async def critique_document(
    *,
    user_message: str,
    artifact_type: str,
    payload: dict,
    llm_call=None,
    template_name: str = "default",
    quality_criteria: Optional[list[str]] = None,
    max_chars: int = 4000,
) -> ReflexionVerdict:
    """Run the document rubric on a generated payload.

    Args:
        user_message: The user's original request.
        artifact_type: One of pptrx / docx / pdf / html.
        payload: The document's source_json dict.
        llm_call: Optional async callable ``(messages) -> str``.
        template_name: Name of the template applied.
        quality_criteria: Optional override list of criteria strings.
        max_chars: Cap on message excerpt in the prompt.

    Returns:
        A ``ReflexionVerdict`` with document-specific issues/suggestions.
    """
    import json as _json

    if quality_criteria is None:
        # Templates module was removed in favor of the C-Heavy skill-
        # driven runner.  Quality criteria are now defined inline; if a
        # future design wants template-specific rubrics again, this is
        # the place to plug them in.
        quality_criteria = [
            "Document has clear structure with logical section flow",
            "Content is accurate, professional, and audience-appropriate",
            "Visual elements (charts, KPIs) are well-placed",
            "All required sections are present and populated",
            "Content density is appropriate for the format",
        ]

    try:
        payload_json = _json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except Exception:
        payload_json = str(payload)
    if len(payload_json) > 8000:
        payload_json = payload_json[:8000] + "\n... [truncated]"

    if not _enabled() or llm_call is None:
        return _fallback_verdict(str(payload))

    prompt = _DOC_RUBRIC_TEMPLATE.format(
        artifact_type=artifact_type,
        user_message=(user_message or "")[:max_chars],
        payload_json=payload_json,
        template_name=template_name,
        quality_criteria="\n".join(f"- {c}" for c in quality_criteria),
    )
    messages = [
        {"role": "system", "content": "You are a strict document editor. Reply with JSON only."},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = await llm_call(messages)
    except Exception as exc:
        logger.warning("reflexion.doc_critique: llm_call raised (non-fatal): %s", exc)
        return _fallback_verdict(str(payload))

    parsed = _extract_json(raw)
    if not parsed:
        return _fallback_verdict(str(payload))

    verdict = str(parsed.get("verdict", "accept")).lower()
    if verdict not in ("accept", "revise", "reject"):
        verdict = "accept"
    try:
        conf = float(parsed.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    issues = [str(x) for x in (parsed.get("issues") or []) if str(x).strip()]
    suggestions = [str(x) for x in (parsed.get("suggestions") or []) if str(x).strip()]

    # Augment suggestions with score-breakdown detail
    breakdown = parsed.get("score_breakdown", {})
    if isinstance(breakdown, dict):
        for dim, dim_score in breakdown.items():
            try:
                if float(dim_score) < 0.5:
                    suggestions.append(f"Improve {dim}: currently {float(dim_score):.0%}")
            except Exception:
                pass

    return ReflexionVerdict(
        verdict=verdict,
        confidence=conf,
        issues=issues,
        suggestions=suggestions,
        raw=raw,
    )


__all__ = ["ReflexionVerdict", "critique", "critique_document"]
