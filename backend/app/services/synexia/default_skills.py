"""Default Skills Manifest — built-in skills always available to every agent.

Defines:
- ``DEFAULT_SKILLS``: frozen mapping of format → skill identity
- ``pick_default_skill()``: auto-select the right default based on user message
- ``is_override_skill()``: check if a skill is a user-picked custom skill

When a user makes a request without explicitly naming a skill, the system
automatically selects and applies the appropriate default skill. When the
user picks a custom skill (chip in input bar), the default is skipped
entirely — the user's choice wins.
"""

from __future__ import annotations

import re
from typing import Optional

from app.config import settings
from app.services.synexia.intent_router import (
    detect_deck_edit_intent,
    detect_file_intent,
    FileFormat,
)
from app.services.skill_routing.post_router_hook import post_router_pick

# The six deck-edit tools registered by ``tool_handlers.deck_edit_tool``.
# Surfaced to the agent only when ``DECK_EDIT_ROUTING_ENABLED`` is on.
DECK_EDIT_TOOL_NAMES: tuple[str, ...] = (
    "edit_slide",
    "add_slide",
    "restyle_deck",
    "update_chart",
    "remove_slide",
    "reorder_slide",
)

# ── Default Skills Registry ────────────────────────────────────────────────
#
# Each entry maps a file format to the skill identity on disk / in DB.
# The ``triggers`` list is injected into every agent's system prompt so
# the LLM knows which skill to invoke when it detects a matching keyword.
# The ``skill_name`` must match the directory name under ``backend/skills/``
# AND the ``name`` field in the Tool row (from sync_marketplace_to_db).
#
# Order matters: ``DEFAULT_SKILLS`` is iterated in insertion order when
# building the system-prompt block, so keep it in a user-friendly order.

DEFAULT_SKILLS: dict[str, dict] = {
    "docx": {
        "skill_name": "docx",
        "triggers": ["report", "memo", "word", "docx", "document"],
        "format": "docx",
    },
    "pptx": {
        "skill_name": "pptx",
        "triggers": ["deck", "slides", "presentation", "powerpoint", "pptx"],
        "format": "pptx",
    },
    "pdf": {
        "skill_name": "pdf",
        "triggers": ["pdf", "export pdf"],
        "format": "pdf",
    },
    "html": {
        "skill_name": "artifacts-builder",
        "triggers": ["web page", "html", "interactive", "web app", "webpage"],
        "format": "html",
    },
    "dashboard": {
        "skill_name": "dashboard-generation",
        "triggers": ["dashboard", "kpi", "metrics", "chart"],
        "format": "dashboard",
    },
}

# Set of skill names that are "default" (built-in, always-available).
# Used by sync_marketplace_to_db to flag the DB rows and by the router
# to filter the default-skill list.
DEFAULT_SKILL_NAMES: frozenset[str] = frozenset(
    entry["skill_name"] for entry in DEFAULT_SKILLS.values()
)

# ── Soft-intent heuristic ──────────────────────────────────────────────────
#
# When ``detect_file_intent`` returns None (no explicit format keyword),
# we do a second pass over the message text looking for "soft" signals —
# words that strongly suggest a particular artifact type without the
# user literally saying ".docx" or ".pptx".
#
# The heuristic is dependency-free regex (same pattern as intent_router),
# runs in O(n) over the message text, deterministic, and testable.
#
# Priority order: pptx (deck/slides), then docx (report/memo), then
# dashboard (kpi/metrics), then html (web page). md was removed —
# markdown requests fall through to the LLM (no dedicated skill needed).

_SOFT_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "deck" / "slides" / "presentation" → pptx
    (
        "pptx",
        re.compile(
            r"\b(?:deck|slides|presentation|slide\s*deck|pitch\s*deck)\b",
            re.IGNORECASE,
        ),
    ),
    # "report" / "memo" / "briefing" → docx
    (
        "docx",
        re.compile(
            r"\b(?:report|memo|briefing|summary|analysis|write\s*up)\b",
            re.IGNORECASE,
        ),
    ),
    # "dashboard" / "kpi" / "metrics" → dashboard
    (
        "dashboard",
        re.compile(
            r"\b(?:dashboard|kpis?\b|metrics|chart|visuali[sz]ation)\b",
            re.IGNORECASE,
        ),
    ),
    # "web page" / "html" / "web app" → html
    (
        "html",
        re.compile(
            r"\b(?:web\s*(?:page|app)|html\b|interactive\s*page)\b",
            re.IGNORECASE,
        ),
    ),
)


def detect_soft_intent(text: str) -> Optional[str]:
    """Return the default-skill format key implied by soft signals in ``text``.

    Returns ``None`` when no soft signal is found — the caller should
    fall back to the most general default (``docx``).
    """
    if not text:
        return None
    # ── READ / ANALYZE guard (2026-08-31) ─────────────────────────────
    # "summarize this report" (pointing at an attached file) is a READ —
    # the soft "report" token must NOT route it to the docx creation skill.
    # CREATE phrasing ("make a report") has no READ verb and still routes.
    try:
        from app.services.synexia.intent_router import is_file_read_request
        if is_file_read_request(text):
            return None
    except Exception:
        pass
    for fmt_key, pattern in _SOFT_INTENT_PATTERNS:
        if pattern.search(text):
            return fmt_key
    return None


def detect_deck_edit_routing(text: str) -> Optional[str]:
    """Return the deck-edit tool name for ``text``, gated by the flag.

    Returns ``None`` when ``DECK_EDIT_ROUTING_ENABLED`` is off OR the message
    is not a deck-edit request (including regeneration phrasing, which
    ``detect_deck_edit_intent`` short-circuits to ``None``).

    This is the routing signal used by ``pick_default_skill`` (to avoid
    forcing a regeneration skill for an edit request) and by the chat loop
    (to surface the deck-edit tools).
    """
    if not getattr(settings, "DECK_EDIT_ROUTING_ENABLED", False):
        return None
    return detect_deck_edit_intent(text)


# ── Public API ──────────────────────────────────────────────────────────────


def pick_default_skill(
    user_message: str,
    active_skill: dict | None = None,
) -> dict | None:
    """Return the default-skill dict to apply, or None when no default applies.

    Decision tree:

    1. **Override path**: If ``active_skill`` is non-null (the user picked a
       skill chip), return ``None`` — the caller must use the user-picked
       skill exclusively. Defaults are skipped.

     2. **Explicit format**: If ``detect_file_intent`` returns a format whose
         key exists in ``DEFAULT_SKILLS``, return that default skill.

     3. **Strong skill match**: Consult the full skill catalog. If a custom or
         marketplace skill strongly matches the request, return it as a forced
         skill before applying generic soft-intent defaults.

     4. **Soft intent**: Run the heuristic over the message text. If a soft
       signal maps to a default-skill key, return that default.

     5. **No signal**: Return ``None`` — no deterministic default applies.
       The caller (task_spec_parser / plan_dag) interprets ``None`` as
       "let the LLM pick from the full skill catalog via the Skill
       meta-tool". This replaces the former forced-``docx`` fallback,
       which incorrectly turned every ambiguous request into a Word
       document. ``FALLBACK_SKILL`` in ``resolver.py`` remains as the
       absolute last resort if the LLM also finds nothing relevant.

    Args:
        user_message: The raw user message text.
        active_skill: The skill object picked by the user (from the chip
            in the input bar). None when the user didn't pick a skill.

    Returns:
        The default skill dict (from DEFAULT_SKILLS) or None when the
        override path applies OR no deterministic default matches.
    """
    # 1. Override path — user picked a specific skill → skip defaults
    if active_skill is not None:
        return None

    # 2. Explicit format keyword (e.g. "give me a .docx")
    fmt: Optional[FileFormat] = detect_file_intent(user_message)
    if fmt and fmt in DEFAULT_SKILLS:
        return DEFAULT_SKILLS[fmt]

    # 2.5 Deck-edit routing. When the flag is on and the message is an edit
    #     request (e.g. "update the chart", "add a slide", "change theme"),
    #     do NOT force a generation skill — the chat loop surfaces the
    #     deck-edit tools and the agent edits the existing artifact instead.
    #     Regeneration phrasing is already short-circuited inside
    #     ``detect_deck_edit_intent``, so it still falls through to normal
    #     skill selection (full regeneration). Off by default: when the flag
    #     is off, ``detect_deck_edit_routing`` returns None and behavior is
    #     identical to before.
    if detect_deck_edit_routing(user_message):
        return None

    # 3. Strong skill match. This must run before generic report/docx soft
    # intent so enabled custom skills like weekly-report-generation can win
    # over the broad "report" → docx default, while explicit .docx/.pptx
    # requests still keep deterministic format priority.
    try:
        forced = post_router_pick(user_message)
    except Exception:
        # Never let the post-router hook crash the router; fall through.
        forced = None
    if forced:
        return forced

    # 4. Soft-intent heuristic (e.g. "make a sales report" → docx)
    soft_key = detect_soft_intent(user_message)
    if soft_key and soft_key in DEFAULT_SKILLS:
        return DEFAULT_SKILLS[soft_key]

    # 5. No deterministic signal AND no strong skill match — let the LLM
    #    pick from the catalog. Returning ``None`` signals task_spec_parser
    #    / plan_dag to NOT inject a forced default-skill hint, so the
    #    planner chooses from the full skill catalog injected via
    #    SkillPlannerHook.build_plan_prompt_extra().
    return None


def is_override_skill(skill: dict | None) -> bool:
    """Return True if ``skill`` is a user-picked custom skill (not a default).

    A skill is considered an override when:
    - It is non-null (a skill was explicitly picked), AND
    - Its ``name`` is NOT in ``DEFAULT_SKILL_NAMES``.

    This function is called from the 3-layer defense-in-depth:
    1. Frontend: Chat.jsx skips buildDefaultSkillContext when override
    2. Backend: task_spec_parser sets skill_override=True on TaskSpec
    3. Backend: _build_default_plan checks skill_override before injecting defaults

    Args:
        skill: The skill object (or None) from the user's skill picker.

    Returns:
        True when a non-default custom skill is active.
    """
    if not skill:
        return False
    skill_name = (skill.get("name") or "").strip()
    if not skill_name:
        return False
    return skill_name not in DEFAULT_SKILL_NAMES


def has_override(active_skill: dict | None) -> bool:
    """Convenience alias for ``is_override_skill``.

    Returns True when the user has picked a custom skill that should
    override the default-skill auto-selection. Called from backend
    layers 2 (task_spec_parser) and 3 (plan_dag).
    """
    return is_override_skill(active_skill)


def get_default_skills_list() -> list[dict]:
    """Return the default skills as a plain list of dicts for API responses.

    Each dict has ``skill_name``, ``triggers``, and ``format`` keys.
    """
    return [
        {"skill_name": v["skill_name"], "triggers": v["triggers"], "format": v["format"]}
        for v in DEFAULT_SKILLS.values()
    ]
