"""Hybrid guardrail: LLM-driven detection with regex fast fallback.

The user's explicit insight: keyword/regex patterns are brittle, and
the LLM itself is the better tool for understanding intent and
detecting refusals.  This module wires the LLM-based detectors
(:mod:`app.services.intent_classifier` and
:mod:`app.services.self_critic`) as the **primary** path and the
keyword patterns in :mod:`app.services.turn_action` as the **fast
fallback**.

Design:

* **LLM is primary** — semantic understanding beats regex on
  ambiguous phrasing.  The LLM is asked small focused questions
  (``classify_intent`` before the main call, ``critique_response``
  after) and the answers drive the agent's behavior.

* **Regex is fallback** — when the LLM is unavailable, slow, or
  returns ``unclassified``, the regex patterns still catch the
  obvious cases.  This is defense in depth: never rely on a single
  detector.

* **Total / never raises** — every detector is wrapped in
  try/except.  A failure in the LLM path or the regex path
  degrades gracefully to "no action" so the chat runtime always
  proceeds.

* **Cached** — both detectors cache per session.  The chat loop can
  call them on every turn without re-paying the LLM cost.

The module exposes three public functions:

* :func:`classify_user_intent` — runs the LLM-based intent
  classifier; falls back to keyword-based intent.
* :func:`detect_and_correct_refusal` — runs the LLM-based self
  critic; falls back to keyword-based refusal detection; runs the
  corrective tool if either detects a refusal.
* :func:`run_hybrid_guardrail` — the top-level entry point that
  the chat loop calls once per turn.  Returns a
  :class:`GuardrailOutcome` the caller can act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardrailOutcome:
    """The hybrid guardrail's per-turn outcome."""

    # Intent classification
    intent_source: str = "none"  # "llm" | "regex" | "none"
    intent: str = "unclassified"  # "research" | "data_query" | "file_generation" | "chitchat" | "unclassified"
    intent_confidence: float = 0.0
    suggested_tools: list[str] = field(default_factory=list)
    suggested_query: str = ""

    # Refusal detection
    refusal_source: str = "none"  # "llm" | "regex" | "none"
    refused: bool = False
    refusal_confidence: float = 0.0
    refusal_reason: str = ""
    corrective_tool: Optional[str] = None
    corrective_args: dict = field(default_factory=dict)
    search_results: list[dict] = field(default_factory=list)

    # Action
    action: str = "none"  # "fallback" | "append" | "none"
    followup_text: str = ""  # text to append to assistant_content
    message: str = ""  # human-readable summary for logs / SSE

    def to_dict(self) -> dict:
        return {
            "intent_source": self.intent_source,
            "intent": self.intent,
            "intent_confidence": self.intent_confidence,
            "suggested_tools": self.suggested_tools,
            "suggested_query": self.suggested_query,
            "refusal_source": self.refusal_source,
            "refused": self.refused,
            "refusal_confidence": self.refusal_confidence,
            "refusal_reason": self.refusal_reason,
            "corrective_tool": self.corrective_tool,
            "corrective_args": self.corrective_args,
            "action": self.action,
            "message": self.message,
        }


# ── Intent classification (LLM-first, regex fallback) ──────────────────
async def classify_user_intent(
    user_message: Optional[str],
    *,
    llm_call: Optional[Callable] = None,
    session_id: Optional[str] = None,
) -> tuple[str, float, list[str], str, str]:
    """Classify the user message intent.

    Returns: ``(intent, confidence, suggested_tools, suggested_query, source)``
    where ``source`` is ``"llm"``, ``"regex"``, or ``"none"``.

    The LLM is the primary detector.  When the LLM returns
    ``unclassified`` or is unavailable, the keyword-based
    ``is_online_research_request`` acts as a fast fallback.
    """
    msg = (user_message or "").strip()
    if not msg:
        return "unclassified", 0.0, [], "", "none"

    # 1. LLM-based intent classification.
    try:
        from app.services.intent_classifier import (
            Intent,
            classify_intent,
        )

        result = await classify_intent(
            msg,
            llm_call=llm_call,
            session_id=session_id,
        )
        if result.intent != Intent.UNCLASSIFIED and result.confidence >= 0.5:
            return (
                result.intent.value,
                result.confidence,
                result.suggested_tools,
                result.suggested_query,
                "llm",
            )
    except Exception as exc:
        logger.debug("hybrid_guardrail.classify_user_intent: LLM failed (%s)", exc)

    # 2. Regex-based fallback.
    try:
        from app.services.turn_action import is_online_research_request

        if is_online_research_request(msg):
            # Infer the tools the user likely needs.
            from app.services.turn_action import extract_search_query

            suggested_query = extract_search_query(msg)
            return (
                "research",
                0.6,  # regex is less confident than LLM
                ["web_search"],
                suggested_query,
                "regex",
            )
    except Exception as exc:
        logger.debug("hybrid_guardrail.classify_user_intent: regex failed (%s)", exc)

    return "unclassified", 0.0, [], "", "none"


# ── Refusal detection (LLM-first, regex fallback) ───────────────────────
async def _run_corrective_tool(
    tool_name: str,
    tool_args: dict,
    *,
    db: Any = None,
) -> list[dict]:
    """Run a corrective tool and return its results."""
    if not tool_name:
        return []
    try:
        if tool_name in ("web_search", "web_extract"):
            from app.services.tool_handlers import web_search_tool

            if hasattr(web_search_tool, "_web_search"):
                import asyncio as _asyncio
                result = await web_search_tool._web_search(tool_args, db=db)
            else:
                return []
        else:
            # Unknown tool — try the registry.
            from app.services.tool_registry import registry
            handler = registry.get_handler(tool_name) if hasattr(registry, "get_handler") else None
            if handler is None:
                return []
            import asyncio as _asyncio
            if _asyncio.iscoroutinefunction(handler):
                result = await handler(tool_args, db=db)
            else:
                result = await _asyncio.to_thread(handler, tool_args, db=db)

        if not isinstance(result, dict):
            return []
        if not result.get("success"):
            return []
        # The web_search handler returns {"success": True, "results": [...]}
        return result.get("results") or []
    except Exception as exc:
        logger.warning(
            "hybrid_guardrail._run_corrective_tool: tool %s raised (%s)",
            tool_name, exc,
        )
        return []


async def detect_and_correct_refusal(
    user_message: Optional[str],
    assistant_text: Optional[str],
    *,
    llm_call: Optional[Callable] = None,
    call_llm: Optional[Callable] = None,
    session_id: Optional[str] = None,
    db: Any = None,
) -> GuardrailOutcome:
    """Detect a refusal and run a corrective action if needed.

    Returns a :class:`GuardrailOutcome` with:

    * ``refused`` + ``refusal_source`` + ``refusal_confidence``
    * ``corrective_tool`` + ``corrective_args`` (what we tried)
    * ``search_results`` (results from the corrective tool)
    * ``action``: ``"fallback"`` if the corrective tool ran AND a
      follow-up LLM call succeeded, ``"append"`` if only the
      results were appended, ``"none"`` if no action.
    * ``followup_text``: text the caller can append to
      ``assistant_content``.
    * ``message``: human-readable summary for logs / SSE.
    """
    outcome = GuardrailOutcome()
    msg = (user_message or "").strip()
    asst = (assistant_text or "").strip()
    if not msg or not asst:
        return outcome

    refused = False
    refusal_source = "none"
    refusal_confidence = 0.0
    refusal_reason = ""
    corrective_tool = None
    corrective_args: dict = {}

    # 1. LLM-based self-critic.
    try:
        from app.services.self_critic import critique_response

        critic_decision = await critique_response(
            user_message=msg,
            assistant_text=asst,
            llm_call=llm_call,
            session_id=session_id,
        )
        if critic_decision.refused and critic_decision.confidence >= 0.5:
            refused = True
            refusal_source = "llm"
            refusal_confidence = critic_decision.confidence
            refusal_reason = critic_decision.reasoning
            corrective_tool = critic_decision.corrective_tool
            corrective_args = critic_decision.corrective_args
    except Exception as exc:
        logger.debug("hybrid_guardrail.detect_and_correct_refusal: LLM failed (%s)", exc)

    # 2. Regex-based fallback.
    if not refused:
        try:
            from app.services.turn_action import (
                check_and_fallback,
                is_online_research_request,
            )

            if is_online_research_request(msg):
                # Use the existing regex-based check_and_fallback; it
                # already handles the call_llm and search fallback.
                decision = await check_and_fallback(
                    user_message=msg,
                    assistant_text=asst,
                    db=db,
                    call_llm=call_llm,
                )
                if decision.get("triggered"):
                    refused = True
                    refusal_source = "regex"
                    refusal_confidence = 0.6
                    refusal_reason = "regex pattern matched"
                    corrective_tool = decision.get("search_query") and "web_search" or None
                    corrective_args = {"query": decision.get("search_query") or ""}
                    outcome.search_results = decision.get("search_results") or []
                    outcome.action = decision.get("action") or "append"
        except Exception as exc:
            logger.debug("hybrid_guardrail.detect_and_correct_refusal: regex failed (%s)", exc)

    if not refused:
        return outcome

    outcome.refused = True
    outcome.refusal_source = refusal_source
    outcome.refusal_confidence = refusal_confidence
    outcome.refusal_reason = refusal_reason
    outcome.corrective_tool = corrective_tool
    outcome.corrective_args = corrective_args

    # Run the corrective tool if we don't already have search results.
    if not outcome.search_results and corrective_tool:
        outcome.search_results = await _run_corrective_tool(
            corrective_tool, corrective_args, db=db
        )

    if not outcome.search_results:
        outcome.message = (
            f"Refusal detected ({refusal_source}, conf={refusal_confidence:.2f}) but "
            f"corrective tool {corrective_tool!r} returned no results."
        )
        return outcome

    # Build the followup text.
    bullets = "\n".join(
        f"- [{r.get('title', '?')}]({r.get('url', '?')}): {r.get('snippet', '')}"
        for r in outcome.search_results
    )
    query = corrective_args.get("query") or "your request"
    if outcome.action == "fallback" and call_llm is not None:
        outcome.followup_text = (
            f"{asst}\n\n---\n"
            f"_I can do this — I used the available tools to look up "
            f"\"{query}\". Here is what I found:_\n\n{bullets}"
        )
        outcome.message = (
            f"Refusal detected ({refusal_source}). Re-prompted LLM with "
            f"{len(outcome.search_results)} results for query={query!r}."
        )
    else:
        outcome.action = "append"
        outcome.followup_text = (
            f"{asst}\n\n---\n"
            f"_I can do this — here is what I found for \"{query}\":_\n\n"
            f"{bullets}"
        )
        outcome.message = (
            f"Refusal detected ({refusal_source}). Auto-ran {corrective_tool!r} "
            f"for query={query!r} and got {len(outcome.search_results)} result(s)."
        )
    return outcome


# ── Top-level entry point ────────────────────────────────────────────────
async def run_hybrid_guardrail(
    user_message: Optional[str],
    assistant_text: Optional[str],
    *,
    llm_call: Optional[Callable] = None,
    call_llm: Optional[Callable] = None,
    session_id: Optional[str] = None,
    db: Any = None,
) -> GuardrailOutcome:
    """The chat-loop-facing entry point.

    Runs intent classification (before the main LLM call, but here
    we only consume the result) and refusal detection (after the
    main LLM call).  Returns a :class:`GuardrailOutcome` the caller
    can use to amend the assistant's reply.
    """
    intent, intent_conf, suggested_tools, suggested_query, intent_source = (
        await classify_user_intent(
            user_message, llm_call=llm_call, session_id=session_id,
        )
    )
    outcome = await detect_and_correct_refusal(
        user_message, assistant_text,
        llm_call=llm_call,
        call_llm=call_llm,
        session_id=session_id,
        db=db,
    )
    outcome.intent = intent
    outcome.intent_confidence = intent_conf
    outcome.suggested_tools = suggested_tools
    outcome.suggested_query = suggested_query
    outcome.intent_source = intent_source
    return outcome


__all__ = [
    "GuardrailOutcome",
    "classify_user_intent",
    "detect_and_correct_refusal",
    "run_hybrid_guardrail",
]
