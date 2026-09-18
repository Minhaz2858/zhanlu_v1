"""Server-side turn-action resolver (advisory only — no tool forcing).

This module used to FORCE ``tool_choice`` on iteration 0 based on cheap
detectors (data question / file format / URL / time-sensitive keywords).
That deterministic override hijacked user prompts (e.g. a "please list…"
request was forced into ``create_artifact`` because it mentioned a weekly
summary). Per the model-first harness principle the LLM now decides which
tool to call from the full tool list; this module only derives *advisory*
grounding blocks (see ``grounding_block_for_message``) that are appended to
the system prompt as soft guidance.

``resolve_turn_action`` is kept as a backward-compatible no-op seam: it
always returns a plain :class:`TurnAction` (``forced_tool=None``) so the
chat runtime's ``tool_choice`` stays on ``auto``. Callers that previously
relied on a forced tool must let the model pick.

Grounding blocks (``GROUNDING_REQUIRED_BLOCK`` / ``GROUNDING_URL_BLOCK`` /
``GROUNDING_DOC_BLOCK``) remain prompt-level nudges only — they never
override the user's verb.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

# Reuse existing detectors rather than reinventing them.
from app.services.agent_prompts import GROUNDING_REQUIRED_BLOCK, TIME_SENSITIVE_PATTERN
from app.services.harness.rule_events import observe_rule
from app.services.dashboard_turn_guard import fuzzy_dashboard_request
from app.services.synexia.intent_router import FileFormat, detect_file_intent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
#
# Matches ``http://`` / ``https://`` / ``www.`` followed by at least one
# non-whitespace, non-quote character. We deliberately allow common URL
# punctuation inside the match (slashes, query strings, fragments) so
# `web_extract` receives the full URL.
URL_PATTERN = re.compile(
    r"\b(?:https?://[^\s<>\"']+|www\.[^\s<>\"']+)\b",
    re.IGNORECASE,
)

LIVE_DASHBOARD_PATTERN = re.compile(
    r"\b(dashboard|仪表盘)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Grounding blocks (mirrors GROUNDING_REQUIRED_BLOCK in agent_prompts.py)
# ---------------------------------------------------------------------------

GROUNDING_URL_BLOCK = """

[GROUNDING REQUIRED — web_extract first]
The user's most recent message references a URL. You MUST call `web_extract`
BEFORE responding. Do not summarize the URL from training data. If
`web_extract` is unavailable, fall back to `agent_browser`; if both fail,
tell the user you cannot fetch the URL and ask them to paste the content
directly.
"""


GROUNDING_DOC_BLOCK = """

[FILE GENERATION REQUIRED — create_artifact first]
The user is asking for a file in a specific format. You MUST call
`create_artifact` (or emit the appropriate `◤PPTX◤` / `◤MD_DOCX◤` /
`◤HTML_DOCX◤` marker) BEFORE responding with the file content. After
emitting the marker, return a one-sentence summary of the file.
"""


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnAction:
    """Server-derived action plan for a single LLM turn.

    Attributes:
        forced_tool:    Name of the tool to force via ``tool_choice`` on
                        iteration 0, or ``None`` to leave the choice to
                        the LLM (``auto``).
        grounding_block: Prompt block to inject into the system prompt on
                        iteration 0. Empty when no grounding is required.
                        Reuses the existing soft-nudge flow so the model
                        still understands *why* the force is in effect.
        doc_format:     The file format the user asked for (when
                        ``forced_tool == "create_artifact"``), or
                        ``None``. The orchestrator uses this for the
                        fallback path.
    """

    forced_tool: Optional[str] = None
    grounding_block: str = ""
    doc_format: Optional[FileFormat] = None


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_turn_action(
    user_message: Optional[str],
    tool_names: Optional[list[str]],
    data_ctx_extras: Optional[dict],
    is_data_question: bool,
    iteration: int,
    is_dashboard_followup: bool = False,
) -> TurnAction:
    """Return the advisory action plan for this turn.

    Model-first harness (Phase A/B): the LLM decides which tool to call —
    server-side ``tool_choice`` forcing is removed. This resolver is kept as
    a backward-compatible seam and always returns a plain ``TurnAction``
    (``forced_tool=None``, no grounding block, no doc_format), leaving the
    runtime's ``tool_choice`` on ``auto``. Soft grounding nudges are still
    derived separately by ``grounding_block_for_message`` and appended to
    the system prompt — prompt text only, never a hard override.

    Args are retained for signature compatibility with existing callers;
    none of them influence the result anymore.
    """
    logger.debug(
        "turn_action: advisory resolver (no tool forcing) msg=%r iter=%d",
        (user_message or "")[:80], iteration,
    )
    return TurnAction()



def grounding_block_for_message(user_message: Optional[str]) -> str:
    """Return the appropriate grounding block to inject into the system prompt.

    Mirrors the precedence of :func:`resolve_turn_action` (doc > url > time)
    but does NOT require the tool to be present. The prompt block is a soft
    nudge that re-states the rule just before the LLM picks a tool; even if
    the agent has not been granted the matching tool, the LLM will
    gracefully say "I cannot fetch URLs / files" rather than hallucinate.

    This is the only place we need to add the URL and doc blocks to the
    system prompt — the time-sensitive block was already injected by
    ``get_system_prompt`` in ``agent_prompts.py``.

    Args:
        user_message: Latest user message (may be ``None``).

    Returns:
        The grounding block to append, or ``""`` if no grounding is
        required.
    """
    if not user_message:
        return ""
    if LIVE_DASHBOARD_PATTERN.search(user_message) or fuzzy_dashboard_request(user_message):
        return ""
    if detect_file_intent(user_message):
        return GROUNDING_DOC_BLOCK
    if URL_PATTERN.search(user_message):
        return GROUNDING_URL_BLOCK
    if TIME_SENSITIVE_PATTERN.search(user_message):
        return GROUNDING_REQUIRED_BLOCK
    return ""


__all__ = [
    "URL_PATTERN",
    "GROUNDING_URL_BLOCK",
    "GROUNDING_DOC_BLOCK",
    "TurnAction",
    "resolve_turn_action",
    "grounding_block_for_message",
]


# ---------------------------------------------------------------------------
# Self-healing refusal guardrail
# ---------------------------------------------------------------------------
# When the LLM still refuses to browse the web despite the system prompt
# telling it to, this guardrail detects the refusal + research-request
# pattern and *automatically* runs web_search on the user's behalf.  See
# the module docstring at the top of this file for the full design.

# Re-import ONLINE_RESEARCH_PATTERN and WEB_BROWSE_REFUSAL_PATTERN
# from agent_prompts (added there by the same commit).  These are
# forward-imported so the module can be loaded even if agent_prompts
# is mid-edit.
def _online_research_pattern():
    from app.services.agent_prompts import ONLINE_RESEARCH_PATTERN
    return ONLINE_RESEARCH_PATTERN


def _web_browse_refusal_pattern():
    from app.services.agent_prompts import WEB_BROWSE_REFUSAL_PATTERN
    return WEB_BROWSE_REFUSAL_PATTERN


@observe_rule("turn_action.is_online_research_request")
def is_online_research_request(user_message):
    """True when the user's message is an online research request.

    Broader than TIME_SENSITIVE_PATTERN: catches "collect news",
    "search online", "find from website", etc.
    """
    if not user_message:
        return False
    return bool(_online_research_pattern().search(user_message))


def is_web_browse_refusal(assistant_text):
    """True when the LLM response contains a web-browse refusal phrase."""
    if not assistant_text:
        return False
    return bool(_web_browse_refusal_pattern().search(assistant_text))


def extract_search_query(user_message):
    """Strip filler ("can you", "from website") and return the search core."""
    if not user_message:
        return ""
    import re as _re
    text = user_message.strip()
    politeness = (
        r"^(please\s+)?(can\s+you|could\s+you|would\s+you|will\s+you|"
        r"i\s+want\s+you\s+to|i\s+need\s+you\s+to|i\s+would\s+like\s+you\s+to|"
        r"i\s+want|i\s+need|i\s+would\s+like|help\s+me)\s+"
    )
    text = _re.sub(politeness, "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"\s+(please|thanks|thank\s+you)\.?\s*$", "", text, flags=_re.IGNORECASE)
    text = _re.sub(
        r"\s+(from\s+(the\s+)?(website|web|internet|online|websites?)|"
        r"on\s+(the\s+)?(web|internet|online))\s*",
        " ",
        text,
        flags=_re.IGNORECASE,
    )
    text = _re.sub(
        r"^(collect|fetch|search\s+for|find|lookup|look\s+up|gather|"
        r"scrape|crawl|get|grab|pull|browse)\s+",
        "",
        text,
        flags=_re.IGNORECASE,
    )
    text = _re.sub(r"\s+", " ", text).strip()
    return text or user_message.strip()


async def _run_web_search_fallback(query, db=None):
    """Call web_search directly (no LLM); return list of result dicts."""
    if not query:
        return []
    import asyncio as _asyncio
    try:
        from app.services.tool_handlers.web_search_tool import _web_search as ws_handler
    except Exception as exc:
        # Fall back to the public name if the private symbol changes.
        try:
            from app.services.tool_handlers import web_search_tool as ws_module
            ws_handler = getattr(ws_module, "_web_search", None) or getattr(ws_module, "handle", None)
            if ws_handler is None:
                raise AttributeError("no _web_search or handle")
        except Exception as exc2:
            logger.warning("refusal_guardrail: cannot import web_search handler (%s / %s)", exc, exc2)
            return []
    try:
        # _web_search is async; some legacy "handle" names are sync.
        if _asyncio.iscoroutinefunction(ws_handler):
            result = await ws_handler({"query": query, "top_k": 5}, db=db)
        else:
            result = await _asyncio.to_thread(
                ws_handler, {"query": query, "top_k": 5}, db=db,
            )
    except Exception as exc:
        logger.warning("refusal_guardrail: web_search raised (%s)", exc)
        return []
    if not isinstance(result, dict) or not result.get("success"):
        return []
    results = result.get("results") or []
    return results if isinstance(results, list) else []


async def check_and_fallback(
    user_message,
    assistant_text,
    *,
    db=None,
    call_llm=None,
):
    """Run the refusal guardrail and return a structured decision.

    The chat runtime is expected to use ``decision.action``:
      - ``"fallback"``: re-ask the LLM with the search results.
      - ``"append"``:   show the search results alongside the reply.
      - ``"none"``:     no-op (no refusal detected).

    Never raises — failures are logged and degrade to a no-op.
    """
    decision = {
        "triggered": False,
        "action": "none",
        "search_query": "",
        "search_results": [],
        "message": "",
    }
    if not user_message or not assistant_text:
        return decision
    if not is_online_research_request(user_message):
        return decision
    if not is_web_browse_refusal(assistant_text):
        return decision

    query = extract_search_query(user_message)
    if not query:
        return decision

    logger.info("refusal_guardrail: triggered for query=%r", query)
    results = await _run_web_search_fallback(query, db=db)
    decision["triggered"] = True
    decision["search_query"] = query
    decision["search_results"] = results
    if not results:
        decision["message"] = "Refusal detected but web_search returned no results."
        return decision

    if call_llm is not None:
        try:
            followup = await call_llm(query, results)
        except Exception as exc:
            logger.warning("refusal_guardrail: call_llm raised (%s)", exc)
            followup = None
        if followup:
            decision["action"] = "fallback"
            decision["message"] = (
                f"Refusal detected. Re-asked the LLM with {len(results)} "
                f"web_search results for query={query!r}."
            )
            return decision

    decision["action"] = "append"
    decision["message"] = (
        f"Refusal detected. Auto-ran web_search for query={query!r} "
        f"and got {len(results)} result(s)."
    )
    return decision
