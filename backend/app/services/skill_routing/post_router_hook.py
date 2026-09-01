"""Post-router skill auto-selection hook.

Sits between the deterministic format/soft-intent router (Path A — 6 format
skills) and the LLM catalog pick (Path B — let the LLM select from ~500).
When the deterministic router returns ``None`` ("llm_catalog_pick"), this
hook runs :func:`unified_search` against all skills in the registry, scores
each result on a 0–1 scale, and returns a forced-skill dict when the top
match crosses :data:`STRONG_MATCH_THRESHOLD`.

Failure modes:

* Empty / stopword-only message → ``None`` (no ``unified_search`` call)
* Empty ``unified_search`` results → ``None``
* Top score < threshold → ``None``
* Any internal exception → ``None`` (logged at debug)

When a strong match is found, the returned dict has the shape of a
``DEFAULT_SKILLS`` entry so it slots into existing downstream code
(``task_spec_parser.py``, ``plan_dag.py``) without protocol changes::

    {
        "skill_name": "slack-gif-creator",
        "triggers": [],
        "format": None,
        "forced": True,
        "score": 1.0,
        "source": "filesystem",
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.services.skills_loader import unified_search

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────

# A skill match at or above this score is considered strong enough to force
# activation of the Skill meta-tool. Tuned to match "skill name tokens
# present in the message" without yielding false positives on generic
# single-token matches like "doc" → docx.
STRONG_MATCH_THRESHOLD: float = 0.6

# Messages that are pure greetings or small-talk skip ``unified_search``
# entirely. Designed for fast-path rejection of noise.
_STOPWORDS: frozenset[str] = frozenset({
    "hello", "hi", "hey", "thanks", "thank", "ok", "okay", "yes",
    "no", "sure", "cool", "nice", "great", "bye", "lol", "hmm",
})

# Tokens this short never count as a "name match" — they appear in too
# many skill names ("doc", "x", etc.) and produce false positives.
_MIN_NAME_TOKEN_LEN: int = 3

_DASHBOARD_INTENT_RE = re.compile(r"\b(?:dashboard|dash-board|kpi\s*dashboard|仪表盘)\b", re.IGNORECASE)

_WEEKLY_REPORT_INTENT_RE = re.compile(
    # NOTE: `\b` only wraps the ASCII alternatives. CJK characters are word
    # chars in Python re, so `\b周报\b` would fail when 周报 is adjacent to
    # another CJK char (e.g. "本周的周报"). CJK alternatives match bare.
    r"(?:\b(?:weekly\s*(?:market\s*)?report|weekly\s*summary|weekly\s*review|"
    r"market\s*report)\b|周报|周度报告|本周报告)",
    re.IGNORECASE,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _normalize(message: str) -> str:
    """Lowercase + collapse internal whitespace."""
    return re.sub(r"\s+", " ", str(message or "").lower()).strip()


def _is_greeting_only(message: Optional[str]) -> bool:
    """True if the message is empty or contains only stopword tokens."""
    if not message:
        return True
    tokens = re.findall(r"\b\w+\b", _normalize(message))
    if not tokens:
        return True
    return all(t in _STOPWORDS for t in tokens)


def _content_tokens(message: str) -> list[str]:
    """Tokens from the message, excluding stopwords."""
    return [t for t in re.findall(r"\b\w+\b", _normalize(message)) if t not in _STOPWORDS]


def _name_tokens(name: str) -> list[str]:
    """Skill-name parts (split on -/_/space) of length >= min-token-len."""
    return [t for t in re.split(r"[-_\s]+", (name or "").lower().strip()) if len(t) >= _MIN_NAME_TOKEN_LEN]


def _trigger_hits(message: str, trigger: str) -> int:
    """Count trigger-keyword whole-word hits in ``message``."""
    if not trigger:
        return 0
    msg = _normalize(message)
    hits = 0
    for trig in re.split(r"[\s,]+", trigger.lower().strip()):
        if len(trig) < _MIN_NAME_TOKEN_LEN:
            continue
        if re.search(rf"\b{re.escape(trig)}\b", msg):
            hits += 1
    return hits


# ── Public API ─────────────────────────────────────────────────────────────


def score_skill_match(user_message: Optional[str], skill: dict[str, Any]) -> float:
    """Score 0.0–1.0 how strongly ``user_message`` references ``skill``.

    Scoring rules:

    * **Name-token overlap**: each skill-name part (length ≥ 3) that
      appears as a whole word in the message contributes a primary signal.
      Two-or-more matches → ``+0.6``; one match → ``+0.3``.
    * **Exact-phrase bonus**: the normalized full skill name appearing in
      the message (e.g. ``canvas design`` for skill ``canvas-design``)
      promotes the score to at least ``0.7``.
    * **Trigger-keyword hits**: each trigger keyword (length ≥ 3) that
      appears as a whole word in the message contributes ``+0.2``,
      capped at ``+0.4``.
    * **Short-message penalty**: if the message has < 3 content tokens
      and at most one weak signal (single name-token or single trigger
      hit), subtract ``0.2`` to suppress single-word false positives.
    """
    if not user_message or not isinstance(skill, dict):
        return 0.0

    msg = _normalize(user_message)
    if not msg:
        return 0.0

    name = (skill.get("name") or "").lower().strip()
    if not name:
        return 0.0

    score = 0.0

    # Primary: skill-name token overlap.
    parts = _name_tokens(name)
    msg_words = set(re.findall(r"\b\w+\b", msg))
    matched = sum(1 for tok in parts if tok in msg_words)
    if matched >= 2:
        score += 0.6
    elif matched == 1:
        score += 0.3

    # Exact-phrase bonus: full normalized name present as a substring.
    norm_name = re.sub(r"[-_\s]+", " ", name)
    if norm_name and norm_name in msg:
        score = max(score, 0.7)

    # Trigger-keyword hits, capped at +0.4.
    trig_hits = _trigger_hits(msg, skill.get("trigger") or "")
    score += min(trig_hits * 0.2, 0.4)

    # Short-message penalty — prevents "doc" → docx forcing.
    content = _content_tokens(msg)
    if len(content) < 3 and matched <= 1 and trig_hits <= 1:
        score -= 0.2

    return max(0.0, min(score, 1.0))


def post_router_pick(
    user_message: Optional[str],
    db: Optional[Any] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """Return a forced-skill dict if the message strongly matches a skill.

    Parameters
    ----------
    user_message:
        The raw user message. May be ``None`` or whitespace.
    db:
        Optional SQLAlchemy session. When provided, ``unified_search``
        also queries the DB ``tools`` table.
    candidates:
        Optional pre-computed search results (e.g. from a prior
        ``unified_search`` call earlier in the same request). When
        provided, ``post_router_pick`` skips its own ``unified_search``
        call and scores these candidates directly. This avoids redundant
        searches in the agent chat loop (``routers/agents.py``) where
        the catalog-tier search has already been done for the priority
        hint.

    Returns
    -------
    Optional[dict]
        ``None`` if no strong match, otherwise a dict shaped like a
        ``DEFAULT_SKILLS`` entry, with ``"forced": True`` and the raw
        ``"score"`` included for auditing.
    """
    if _is_greeting_only(user_message):
        logger.debug("post_router_pick: greeting/empty message, skipping")
        return None

    if candidates is None:
        try:
            candidates = unified_search(user_message, limit=10, db=db)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("post_router_pick: unified_search call failed: %s", exc)
            return None

    if not candidates:
        return None

    if _DASHBOARD_INTENT_RE.search(str(user_message or "")):
        for cand in candidates:
            if (cand.get("name") or "").strip().lower() == "dashboard-generation":
                return {
                    "skill_name": "dashboard-generation",
                    "triggers": [],
                    "format": "dashboard",
                    "forced": True,
                    "score": 1.0,
                    "source": cand.get("source", "filesystem"),
                }

    if _WEEKLY_REPORT_INTENT_RE.search(str(user_message or "")):
        for cand in candidates:
            if (cand.get("name") or "").strip().lower() == "weekly-report-generation":
                return {
                    "skill_name": "weekly-report-generation",
                    "triggers": [],
                    "format": None,
                    "forced": True,
                    "score": 1.0,
                    "source": cand.get("source", "filesystem"),
                }

    best: Optional[dict[str, Any]] = None
    best_score: float = 0.0
    for cand in candidates:
        score = score_skill_match(user_message, cand)
        if score > best_score:
            best_score = score
            best = cand

    if best is None or best_score < STRONG_MATCH_THRESHOLD:
        return None

    skill_name = best.get("name")
    if not skill_name:
        return None

    logger.info(
        "post_router_pick: forced skill=%s score=%.2f source=%s",
        skill_name,
        best_score,
        best.get("source", "?"),
    )

    return {
        "skill_name": skill_name,
        "triggers": [],
        "format": None,
        "forced": True,
        "score": best_score,
        "source": best.get("source", "filesystem"),
    }


def build_agent_forced_skill_directive(
    skill_name: str,
    score: Optional[float] = None,
) -> str:
    """Build a HARD system-prompt directive for the agent chat loop.

    Used by ``routers/agents.py`` (main + v3 paths) to inject a
    ``<forced_skill>`` block that strongly nudges the LLM to invoke the
    Skill meta-tool as its first action when the post-router hook fired.

    Args:
        skill_name: The skill to activate (e.g. ``"slack-gif-creator"``).
        score: Optional 0..1 confidence score from the scoring pass. When
            provided, included in the directive so the LLM can weigh
            its urgency.

    Returns:
        A multi-line ``<forced_skill>…</forced_skill>`` block ready to
        append to a system prompt.
    """
    if not skill_name:
        return ""
    score_phrase = (
        f" (auto-selection score {score:.2f})"
        if isinstance(score, (int, float))
        else ""
    )
    return (
        "\n<forced_skill>\n"
        f"A skill auto-selector matched the user's request to the skill "
        f"`{skill_name}`{score_phrase}. As your FIRST action in this turn, "
        f"call the Skill meta-tool:\n\n"
        f'  Skill {{"command": "execute {skill_name}"}}\n\n'
        f"Do NOT answer the user raw, and do NOT invoke any other skill or "
        f"tool before activating `{skill_name}`.\n"
        f"</forced_skill>\n"
    )


__all__ = [
    "STRONG_MATCH_THRESHOLD",
    "build_agent_forced_skill_directive",
    "post_router_pick",
    "score_skill_match",
]
