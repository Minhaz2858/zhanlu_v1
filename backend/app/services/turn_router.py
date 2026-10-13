"""Turn router — ONE LLM-based routing decision per chat turn (Option C,
Phase 1 of the 2026-10-13 approved architecture).

Problem being replaced: the FSM-vs-ReAct choice was made by a pile of
keyword/regex governors (data-bound override, data-tool override,
definitional shortcut, non-data intent…) that repeatedly hijacked turns —
the glossary shortcut answered an image question with the FAMOUNT
definition, sandbox steps were stripped on missing keywords, artifacts
were suppressed by file-intent regexes. Every new feature added another
regex and more interference.

Phase 1 does NOT delete the heuristics yet. It adds a single LLM routing
decision that runs ALONGSIDE them:

  mode="off"   (default)  heuristic routing only — zero behavior change
  mode="dark"             LLM decision computed and LOGGED next to the
                          heuristic one; heuristic still wins. Collects
                          the disagreement data that justifies the flip.
  mode="on"               a confident LLM decision OVERRIDES the
                          heuristics (except automation-forced planning —
                          scheduled runs always plan, by design).

Fail-open everywhere: any router error returns None and the heuristic
decision stands. The router never sees credentials or org data — just
the message text and two context booleans.
"""

from __future__ import annotations

import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """You are the router of an AI agent platform. Decide which pipeline \
should handle this user message. Reply with ONLY a JSON object:

{{"route": "data_pipeline" | "direct", "confidence": 0.0-1.0, "reason": "<10 words>"}}

route="data_pipeline": the message needs data from the user's CONNECTED databases — \
queries, aggregations, rankings, trends, reports, charts, forecasts, definitions of \
the business's own metrics.
route="direct": everything else — greetings, small talk, general/world knowledge, \
writing, coding, brainstorming, questions about files or images the user attached, \
follow-ups about what the agent just said, or requests to build/edit non-database \
artifacts from the conversation.

Message: {message}
Has connected databases: {has_data}
Previous turns produced data/results: {has_prior_results}

JSON:"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def decide_turn_route(
    user_message: str,
    *,
    has_data_context: bool,
    has_prior_results: bool = False,
    endpoint=None,
) -> dict | None:
    """Ask the LLM which pipeline should own this turn.

    Returns {"route": "data_pipeline"|"direct", "confidence": float,
    "reason": str, "source": "llm"} or None on any failure (fail-open).
    """
    from app.services.llm_service import call_llm

    try:
        result = await call_llm(
            prompt=ROUTER_PROMPT.format(
                message=(user_message or "")[:800],
                has_data="yes" if has_data_context else "no",
                has_prior_results="yes" if has_prior_results else "no",
            ),
            temperature=0.0,
            task_type="turn_routing",
            endpoint=endpoint,
            # Structured output where the provider supports it; the regex
            # fallback below still handles plain-text JSON.
            response_json_schema={
                "type": "object",
                "properties": {
                    "route": {"type": "string", "enum": ["data_pipeline", "direct"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["route"],
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("turn_router: LLM call failed: %s", exc)
        return None

    parsed = None
    if isinstance(result.get("data"), dict):
        parsed = result["data"]  # provider honored the schema
    else:
        # call_llm returns the raw text under "response" (also tolerate
        # content/text for robustness against helper changes).
        text = str(
            result.get("response") or result.get("content") or result.get("text") or ""
        )
        match = _JSON_RE.search(text)
        if not match:
            logger.debug("turn_router: no JSON in LLM reply: %.120s", text)
            return None
        import json as _json

        try:
            parsed = _json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            logger.debug("turn_router: unparseable JSON: %.120s", text)
            return None
    route = str(parsed.get("route") or "").strip().lower()
    if route not in ("data_pipeline", "direct"):
        return None
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
    except Exception:  # noqa: BLE001
        confidence = 0.5
    return {
        "route": route,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "")[:120],
        "source": "llm",
    }


def router_mode() -> str:
    """off | dark | on — from settings, defaulting to off."""
    mode = str(getattr(settings, "V3_LLM_ROUTER_MODE", "off") or "off").strip().lower()
    return mode if mode in ("off", "dark", "on") else "off"


def apply_llm_route(
    heuristic_wants_fsm: bool,
    llm_decision: dict | None,
    *,
    heuristic_source: str = "",
    forced: bool = False,
) -> tuple[bool, str]:
    """Merge the LLM decision with the heuristic one.

    Returns (wants_fsm, source). Automation-forced turns are never
    overridden. In "dark" the heuristic always wins; in "on" a confident
    LLM decision overrides; anything else keeps the heuristic.
    """
    mode = router_mode()
    if forced or mode == "off" or llm_decision is None:
        return heuristic_wants_fsm, heuristic_source or "heuristic"

    llm_wants_fsm = llm_decision["route"] == "data_pipeline"
    agrees = llm_wants_fsm == heuristic_wants_fsm

    if mode == "dark":
        if not agrees:
            logger.info(
                "turn_router[dark]: DISAGREEMENT — heuristic=%s(%s) llm=%s "
                "(conf=%.2f, %s). Heuristic wins; flip V3_LLM_ROUTER_MODE=on "
                "when disagreement data justifies it.",
                "fsm" if heuristic_wants_fsm else "react",
                heuristic_source or "?",
                llm_decision["route"],
                llm_decision["confidence"],
                llm_decision["reason"],
            )
        else:
            logger.debug(
                "turn_router[dark]: agreement — %s (llm conf %.2f)",
                llm_decision["route"], llm_decision["confidence"],
            )
        return heuristic_wants_fsm, heuristic_source or "heuristic"

    # mode == "on": confident LLM decision overrides the heuristics.
    if llm_decision["confidence"] >= 0.6 or agrees:
        if not agrees:
            logger.info(
                "turn_router[on]: OVERRIDE — heuristic=%s(%s) → llm=%s "
                "(conf=%.2f, %s)",
                "fsm" if heuristic_wants_fsm else "react",
                heuristic_source or "?",
                llm_decision["route"],
                llm_decision["confidence"],
                llm_decision["reason"],
            )
        return llm_wants_fsm, "llm-router"
    logger.info(
        "turn_router[on]: low-confidence LLM decision (%.2f) — heuristic stands",
        llm_decision["confidence"],
    )
    return heuristic_wants_fsm, heuristic_source or "heuristic"
