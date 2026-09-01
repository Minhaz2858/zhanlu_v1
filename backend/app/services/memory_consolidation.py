"""OHMO memory consolidation hook (P3 Task 4).

Post-turn hook that extracts durable user facts from a completed agent
turn and merges them into the OHMO personal workspace's ``user.md``.

Gated behind ``settings.OHMO_MEMORY_CONSOLIDATION_ENABLED`` (default OFF)
so the LLM call + I/O do not run until explicitly enabled.

Failure modes: every step is best-effort. A failure in the LLM call,
JSON parse, or OHMO I/O is logged at WARNING and the function returns
a zero-result dict. Never raises into the calling SSE path.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# --- tunables --------------------------------------------------------------

# Maximum number of facts merged per turn.
MAX_FACTS_PER_TURN = 5

# Per-fact character cap to prevent memory bloat.
MAX_FACT_LENGTH = 200

# Default workspace dir used by OhmoWorkspace. Tests may override.
DEFAULT_WORKSPACE_DIR = "ohmo"


# --- LLM helper (lazy import; tests inject via monkeypatch) ----------------

def _llm_callable(prompt: str) -> Optional[dict]:
    """Default LLM call. Returns parsed JSON dict or None on any failure.

    Tests monkeypatch this attribute; production code calls the shared
    LLM service. The import is lazy so a missing/broken llm_service never
    blocks the deterministic path.
    """
    try:
        from app.services.llm_service import chat_completion_json_sync
    except Exception as e:
        logger.debug("memory_consolidation: llm service unavailable: %s", e)
        return None
    try:
        return chat_completion_json_sync(prompt)
    except Exception as e:
        logger.warning("memory_consolidation: LLM call failed (non-fatal): %s", e)
        return None


# --- extract_facts_from_turn -----------------------------------------------

_FACT_LINE_RE = _re.compile(r"^\s*(?:-\s*)?(?P<fact>.+?)\s*\.?\s*$")


def _clean_fact(raw: object) -> Optional[str]:
    """Return a cleaned fact string, or None if invalid/empty."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) > MAX_FACT_LENGTH:
        s = s[:MAX_FACT_LENGTH].rstrip()
    # Strip leading bullet/list markers if present.
    s = _re.sub(r"^[-*•]\s*", "", s)
    s = s.strip()
    return s or None


def _build_prompt(user_message: str, assistant_content: str) -> str:
    return (
        "You are a fact-extractor. Given a single exchange between a user and "
        "an AI assistant, identify durable facts about the user that would be "
        "useful to remember across future conversations (preferences, location, "
        "role, project context, recurring topics, expertise level, etc.). "
        "Skip transient task details ('the user wants to know X right now') "
        "and skip anything the assistant inferred without evidence.\n\n"
        "Return strict JSON of the form "
        '{"facts": ["fact 1", "fact 2", ...]} '
        "where each fact is a single short declarative sentence (max 200 chars). "
        "Return {\"facts\": []} when there are no durable facts to record.\n\n"
        f"USER MESSAGE:\n{user_message!r}\n\n"
        f"ASSISTANT RESPONSE:\n{assistant_content!r}\n"
    )


def extract_facts_from_turn(
    user_message: str,
    assistant_content: str,
    llm_callable: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[str]:
    """Return a list of durable user facts extracted from the turn.

    Args:
        user_message: The raw user input text.
        assistant_content: The assistant's response text.
        llm_callable: Optional override for the LLM function (used in tests).

    Returns:
        A list of cleaned, deduped fact strings (max 5, each <= 200 chars).
        Empty when LLM is unavailable, returns malformed JSON, or extracts
        nothing. Never raises.
    """
    if not user_message or not assistant_content:
        return []
    prompt = _build_prompt(user_message, assistant_content)
    callable_ = llm_callable if llm_callable is not None else _llm_callable
    raw = callable_(prompt)
    if not isinstance(raw, dict):
        return []
    facts = raw.get("facts")
    if not isinstance(facts, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for f in facts:
        cleaned = _clean_fact(f)
        if cleaned is None:
            continue
        # Case-insensitive dedup within the response.
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= MAX_FACTS_PER_TURN:
            break
    return out


# --- consolidate_turn_memory (flag-gated entry point) ---------------------


def consolidate_turn_memory(
    user_message: str,
    assistant_content: str,
    workspace_dir: str = DEFAULT_WORKSPACE_DIR,
) -> dict:
    """Extract facts and merge them into the OHMO workspace.

    Best-effort: returns a zero-result dict on any failure and never raises.
    The whole function is skipped when ``OHMO_MEMORY_CONSOLIDATION_ENABLED``
    is False (the default).

    Args:
        user_message: The raw user input text.
        assistant_content: The assistant's response text.
        workspace_dir: Override for the OHMO workspace path (tests use a
            tmp dir; production uses the default "ohmo" workspace).

    Returns:
        A dict ``{"extracted": int, "merged": int}``. ``extracted`` is the
        number of facts returned by the LLM (after cap + clean). ``merged``
        is the number of facts actually appended to ``user.md`` (excludes
        those that dedup-against an existing entry).
    """
    extracted = 0
    merged = 0
    try:
        try:
            from app.config import settings
        except Exception as _cfg_err:
            logger.debug("memory_consolidation: settings unavailable: %s", _cfg_err)
            return {"extracted": 0, "merged": 0}
        if not getattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", False):
            return {"extracted": 0, "merged": 0}

        facts = extract_facts_from_turn(user_message, assistant_content)
        extracted = len(facts)
        if not facts:
            return {"extracted": 0, "merged": 0}

        from app.services.ohmo import OhmoWorkspace
        ws = OhmoWorkspace(workspace_dir=workspace_dir)
        # Only init if the workspace hasn't been bootstrapped — init_workspace
        # is cheap and idempotent (uses _ensure_file which checks existence).
        ws.init_workspace()

        before = ws.get_user_profile() or ""
        for fact in facts:
            ws.append_user_fact(fact)
        after = ws.get_user_profile() or ""
        # Compute "merged" by checking which facts actually got appended.
        # (append_user_fact already does substring dedup, so the count is
        # an upper bound — but we can read it from the diff.)
        new_lines = set(after.splitlines()) - set(before.splitlines())
        for fact in facts:
            if any(fact in line for line in new_lines):
                merged += 1
        return {"extracted": extracted, "merged": merged}
    except Exception as _consolidate_err:
        logger.warning(
            "memory_consolidation: consolidate_turn_memory failed (non-fatal): %s",
            _consolidate_err,
        )
        return {"extracted": extracted, "merged": merged}
