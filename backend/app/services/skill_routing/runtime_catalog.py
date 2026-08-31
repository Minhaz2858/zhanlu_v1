"""Turn-level skill catalog context for agent system prompts.

Composes everything the LLM needs to actually USE skills in a turn:

1. **Forced-skill directive** — when ``post_router_pick`` fires a strong
   match, a hard "invoke this skill first" directive.
2. **Truthful tool instructions** — describes the tools that actually
   exist (``skills`` action=search/load, plus ``load_skill_body`` and the
   ``Skill`` dispatcher when present) so the model never tries a phantom
   tool.  (FIX 2026-08-29: the prompt previously instructed agents to
   call ``load_skill_body`` / ``Skill`` even though neither was
   registered at runtime — real traces show ``Unknown tool:
   load_skill_body`` followed by the agent giving up on skills.)
3. **Relevance-forced catalog** — ``unified_search`` hits and the
   deterministic resolver's routed skill are force-included in the
   ``<available_skills>`` block so budget truncation (52/894 skills at
   the old fixed 15K budget) can no longer hide the relevant skill.
4. **Routed-skill body directive** — when the SkillResolver
   deterministically routes the message (format_intent / soft_intent,
   e.g. "make a ppt" → ``ppt-design``), the full SKILL.md body is
   injected as a hard directive so methodology adherence does NOT depend
   on the model discovering + loading the skill on its own.
5. **Context-scaled budget** — the catalog budget scales with the
   model's context window (15K chars floor, 40K ceiling) so large-window
   models see far more of the registry.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.skill_routing.catalog import DEFAULT_BUDGET_CHARS, build_catalog

logger = logging.getLogger(__name__)

# ── budget / body sizing ────────────────────────────────────────────────
_CATALOG_BUDGET_FLOOR = 15_000
_CATALOG_BUDGET_CEIL = 40_000
# ~1 char per 4 tokens is a safe conservative ratio for the compact
# catalog entries; the block is metadata, not prose.
_CATALOG_TOKENS_PER_CHAR = 4

_SMALL_MODEL_BODY_LIMIT = 8_000
_LARGE_MODEL_BODY_LIMIT = 14_000
_SMALL_MODEL_WINDOW = 48_000


def catalog_budget_for_window(context_window_tokens: Optional[int]) -> int:
    """Scale the catalog character budget to the model's context window.

    Floor at DEFAULT_BUDGET_CHARS (15K) so small models still see a
    useful catalog; ceiling at 40K so a 200K model doesn't drown the
    prompt.  ``None`` (unknown window) → the floor.
    """
    if not context_window_tokens:
        return DEFAULT_BUDGET_CHARS
    return min(
        _CATALOG_BUDGET_CEIL,
        max(DEFAULT_BUDGET_CHARS, context_window_tokens // _CATALOG_TOKENS_PER_CHAR),
    )


def _skill_body_directive(name: str, body: str, context_window_tokens: Optional[int]) -> str:
    """Build the hard "this skill is active" directive with the body."""
    if not body:
        return ""
    # Conservative for unknown/small windows: unknown → small-model limit.
    if not context_window_tokens or context_window_tokens < _SMALL_MODEL_WINDOW:
        body = body[:_SMALL_MODEL_BODY_LIMIT]
    else:
        body = body[:_LARGE_MODEL_BODY_LIMIT]
    return (
        "\n\n## Active Skill (auto-routed)\n"
        f"The user's request matches the `{name}` skill. Its methodology is "
        "ALREADY loaded below — follow it EXACTLY, do not ask for "
        "confirmation, and do not load it again:\n\n"
        f"```\n{body}\n```"
    )


def _tool_instruction_block(has_load_skill_body: bool, has_skill_meta: bool) -> str:
    """Truthful instructions naming only tools that exist for this agent.

    ``skills`` (action=search/load/execute) is the guaranteed path — it is
    in general_assistant's tool_config and DEFAULT_USER_AGENT_TOOLS.
    ``load_skill_body`` and the ``Skill`` dispatcher are described only
    when actually available.
    """
    lines = [
        "You have access to ALL skills below — not just the ones bound to you.",
        "To read a skill's full methodology, call the `skills` tool with "
        '{"action": "load", "name": "<skill-name>"} — or {"action": "search", '
        '"query": "<topic>"} to discover skills by topic.',
    ]
    if has_load_skill_body:
        lines.append(
            "The dedicated `load_skill_body` tool "
            "({name: '<skill-name>'}) also loads a skill's full methodology."
        )
    if has_skill_meta:
        lines.append(
            "The `Skill` dispatcher ({command: 'load <name>'} or "
            "{command: 'execute <name>'}) activates a skill for the turn."
        )
    lines.append(
        "NEVER invent tool names — only call tools listed in your available "
        "functions. If an Active Skill directive is already injected below, "
        "follow it immediately without loading it again."
    )
    return "\n".join(lines)


def build_skill_catalog_context(
    user_content: Optional[str],
    db=None,
    *,
    bound_skills: Optional[set[str]] = None,
    context_window_tokens: Optional[int] = None,
    explicit_skill_name: Optional[str] = None,
) -> str:
    """Build the full skill-catalog context block for one turn.

    Returns ``""`` when nothing can be built (no skills, or any failure —
    best-effort like the callers that use it).

    Parameters
    ----------
    user_content:
        The raw user message (used for search + resolver routing).
    db:
        Optional SQLAlchemy session for DB-backed skill lookups.
    bound_skills:
        Skill names already bound to the agent (excluded from the
        priority hint).
    context_window_tokens:
        The model's real context window (admin-set or probed) — scales
        the catalog budget and skill-body size. ``None`` → conservative
        defaults.
    explicit_skill_name:
        A skill the user explicitly selected (already injected elsewhere);
        the resolver default is skipped when it would pick the same skill.
    """
    try:
        from app.services.skills_loader import (
            get_skill,
            list_skills as loader_list_skills,
            unified_search,
        )
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
            post_router_pick,
        )
        from app.services.skill_routing.resolver import SkillResolver
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("skill catalog context imports failed: %s", exc)
        return ""

    try:
        all_skills = [s.to_dict() for s in loader_list_skills()]
        if not all_skills:
            return ""
    except Exception as exc:
        logger.warning("skill catalog list failed (non-fatal): %s", exc)
        return ""

    forced_directive = ""
    priority_hint = ""
    routed_skill_name: Optional[str] = None
    always_include: list[str] = []

    if user_content:
        try:
            results = unified_search(user_content, limit=5, db=db) or []
            always_include = [r["name"] for r in results if r.get("name")][:5]

            # Strong-match forced directive (post-router hook)
            forced = post_router_pick(user_content, db=db, candidates=results)
            if forced:
                forced_directive = build_agent_forced_skill_directive(
                    forced["skill_name"],
                    score=forced.get("score"),
                )

            # Priority hint for unbound matches (top 3)
            bound = bound_skills or set()
            unbound = [r for r in results if r.get("name") not in bound][:3]
            if unbound:
                hints = "\n".join(
                    f"- **{r['name']}**: {r.get('description', '')}"
                    for r in unbound
                )
                priority_hint = (
                    "\n### Priority matches for this request\n"
                    "These skills look most relevant to the user's message — "
                    "consider loading them first:\n"
                    f"{hints}\n"
                )
        except Exception:
            pass  # Search / post-router is best-effort

        # Deterministic routing: format_intent / soft_intent pick a default
        # skill (e.g. "make a ppt" → ppt-design). Inject its body as a hard
        # directive so the agent follows the methodology WITHOUT needing to
        # discover or load the skill itself.
        try:
            decision = SkillResolver().resolve(user_content, picked_skill=None, db=db)
            if (
                decision
                and decision.chosen_skill
                and decision.reason in ("format_intent", "soft_intent")
                and decision.chosen_skill != explicit_skill_name
            ):
                routed_skill_name = decision.chosen_skill
                if routed_skill_name not in always_include:
                    always_include.append(routed_skill_name)
        except Exception:
            pass  # Resolver is best-effort

    # Context-scaled budget + relevance force-include
    catalog_block = ""
    try:
        budget = catalog_budget_for_window(context_window_tokens)
        catalog_block = build_catalog(
            all_skills,
            budget_chars=budget,
            always_include=always_include,
        )
    except Exception:
        catalog_block = ""
        logger.warning("skill catalog build failed (non-fatal)", exc_info=True)

    # Truthful tool instructions: only name tools that exist for this agent.
    try:
        from app.services.tool_registry import registry
        registered = set(registry.list_names()) | set(registry.list_available())
        has_load = "load_skill_body" in registered
        has_meta = "Skill" in registered
    except Exception:
        has_load = True
        has_meta = True

    parts = [forced_directive]
    if catalog_block:
        parts.append(
            "\n\n## Skill Catalog (dynamic discovery)\n"
            + _tool_instruction_block(has_load, has_meta)
            + "\n"
            + priority_hint
            + "\n<available_skills>\n"
            + catalog_block
            + "\n</available_skills>"
        )
    if routed_skill_name:
        try:
            meta = get_skill(routed_skill_name)
            if meta is not None:
                parts.append(
                    _skill_body_directive(
                        routed_skill_name,
                        meta.body or "",
                        context_window_tokens,
                    )
                )
        except Exception:
            pass  # Body injection is best-effort

    return "\n".join(p for p in parts if p)
