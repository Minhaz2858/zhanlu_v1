"""Server-side turn-action router.

This module is the single source of truth for "what MUST the LLM do on this
turn?" — independent of how the agent is configured. It composes the
existing cheap detectors (``TIME_SENSITIVE_PATTERN``, ``detect_file_intent``)
and one new detector (``URL_PATTERN``) into a :class:`TurnAction` plan that
the chat runtime uses to:

1. Force ``tool_choice`` on iteration 0 (so a weak function-calling model
   cannot "answer from memory" on the first hop), and
2. Inject a corresponding ``[GROUNDING REQUIRED]`` / ``[FILE GENERATION
   REQUIRED]`` prompt block so the model understands *why* it is forced,
3. Optionally hand the result to the **generation orchestrator** as a
   fallback — if the LLM still fails to produce a marker or a
   ``create_artifact`` call after a doc request, the backend invokes the
   proven ``_create_artifact_tool`` pipeline itself so the user always
   receives an artifact or a clear error (never a bare "I'll create…").

Why a separate module?

* Pure: no DB, no LLM, no network. Unit-testable, no I/O risk in the hot
  path (cost is O(len(message)) regex).
* Composable: reuses ``detect_file_intent`` (single source of truth for
  docx / pptx / xlsx / pdf / md / html / dashboard) and
  ``TIME_SENSITIVE_PATTERN`` (no detector is reinvented).
* Universal: every agent that flows through the shared chat runtime
  (v2 main + v3 SSE) gets the same forcing behavior, so newly-created
  agents inherit the fix automatically.

Precedence (highest first):
    1. ``ask_data_agent``  — bound KBs + data question (existing behavior)
    2. ``create_artifact`` — file-format intent (``detect_file_intent``)
    3. ``web_extract``     — URL present in the message
    4. ``web_search``      — time-sensitive keyword heuristic
    5. ``None``            — general chitchat; fall back to ``auto``

Tool presence is required to force — we never ask the LLM to call a tool
the agent has not been granted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

# Reuse existing detectors rather than reinventing them.
from app.services.agent_prompts import GROUNDING_REQUIRED_BLOCK, TIME_SENSITIVE_PATTERN
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
) -> TurnAction:
    """Return the action plan for this turn.

    The function is a *pure* reducer over the four cheap detectors. It
    never mutates ``tool_names`` and never raises. Forcing only applies on
    ``iteration == 0`` so multi-step tool loops are not interfered with.

    Args:
        user_message: Latest user message (may be ``None``).
        tool_names:   Names of tools granted to the agent (may be
                      ``None``). Forcing a tool that is *not* in this
                      list is silently skipped — that would break the
                      LLM contract.
        data_ctx_extras: Data-source runtime context dict (must expose
                         ``bound_kb_ids`` if relevant). May be ``None``.
        is_data_question: Pre-computed result of the
                          ``_is_data_question`` heuristic in
                          ``app.routers.agents``. We accept it as a
                          parameter to avoid a circular import.
        iteration:    Current loop iteration (0-based).

    Returns:
        A :class:`TurnAction` describing the forced tool (or ``None``),
        the grounding block, and the doc format (or ``None``).
    """
    # Iteration guard: forcing only on the first hop of a multi-step loop.
    # On subsequent iterations the LLM is already in a tool-calling
    # state and forcing would just corrupt the conversation flow.
    if iteration != 0:
        return TurnAction()

    tool_set = set(tool_names or [])
    ctx = data_ctx_extras or {}

    if user_message and (
        LIVE_DASHBOARD_PATTERN.search(user_message)
        or fuzzy_dashboard_request(user_message)
    ):
        logger.info("turn_action: live dashboard intent detected; leaving tool_choice on auto")
        return TurnAction()

    # 1) Bound KB + data question → ask_data_agent
    bound_kb_ids = ctx.get("bound_kb_ids") or []
    if bound_kb_ids and is_data_question and "ask_data_agent" in tool_set:
        logger.info(
            "turn_action: forcing ask_data_agent (bound_kb_ids=%d, is_data_question=True)",
            len(bound_kb_ids),
        )
        return TurnAction(forced_tool="ask_data_agent")

    if not user_message:
        return TurnAction()

    # 2) File-format intent → create_artifact
    #    detect_file_intent is the single source of truth for docx / pptx /
    #    xlsx / pdf / md / html / dashboard. Reuse it; do not add a second
    #    detector.
    doc_format = detect_file_intent(user_message)
    if doc_format and "create_artifact" in tool_set:
        logger.info(
            "turn_action: forcing create_artifact (doc_format=%s)", doc_format,
        )
        return TurnAction(
            forced_tool="create_artifact",
            grounding_block=GROUNDING_DOC_BLOCK,
            doc_format=doc_format,
        )

    # 3) URL → web_extract
    if URL_PATTERN.search(user_message) and "web_extract" in tool_set:
        logger.info("turn_action: forcing web_extract (URL detected)")
        return TurnAction(
            forced_tool="web_extract",
            grounding_block=GROUNDING_URL_BLOCK,
        )

    # 4) Time-sensitive → web_search
    if "web_search" in tool_set and TIME_SENSITIVE_PATTERN.search(user_message):
        logger.info("turn_action: forcing web_search (time-sensitive)")
        return TurnAction(
            forced_tool="web_search",
            grounding_block=GROUNDING_REQUIRED_BLOCK,
        )

    # 5) No forcing — general chitchat / LLM decides
    return TurnAction()


# ---------------------------------------------------------------------------
# Prompt-block helper (no tool-presence guard)
# ---------------------------------------------------------------------------


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
