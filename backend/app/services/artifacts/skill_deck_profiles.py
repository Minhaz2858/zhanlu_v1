"""Skill-aware deck personalities (2026-08-29).

The agent can load EIGHT different professional deck skills (ppt-design,
slide-maestro, kai-slide-creator, guizang-ppt-skill, knowledge-cat-ppt-skill,
agentbuff-presentation, frontend-slides, slide-skill) but until now the
chosen skill had ZERO effect on the rendered deck — the deterministic
pipeline rendered every request identically regardless of which skill the
agent loaded.  The user asked for exactly this: "agent can use different
skills to make more interactive and professional ppt".

This module maps each REAL loadable deck-skill name to a design personality
(theme + palette + deck-type defaults) and applies it to the DeckPlan when
the planner left those fields unset or legacy — so picking a different
skill produces a visibly different deck (theme, colors, layout profile).

Rule set (2026-08-29 — skill identity dominates visual, intent keeps structure):
- theme_recommendation: ALWAYS set from the profile when a skill resolves — the
  agent deliberately loaded this skill for this deck, so its visual identity
  wins over the content-based LLM guess.  The user's own style words in the
  message still beat everything (select_theme resolves style keywords BEFORE
  plan.theme_recommendation).
- palette_recommendation: ALWAYS set from the profile (same reasoning).
- deck_type: applied only when still the generic "data_report" default — the
  deck STRUCTURE stays intent-driven (investor_deck/marketing/explicit asks
  are never downgraded by a skill).

STRUCTURE STEER (2026-08-31 — skills now change the NARRATIVE, not just the
look): each profile carries a ``structure`` block (framing + preferred
layouts).  When the skill resolves BEFORE the deck planner runs (threaded via
``build_deck_plan(skill_name=...)``), the steer text is injected into the
planner prompt so the LLM picks the archetype mix the skill is known for —
a slide-maestro investor deck gets roadmap/timeline/swot/comparison slides,
a guizang editorial deck gets quote/section-divider rhythm, etc.  This closes
the last "different skill, same structure" gap against Kimi/Claude/ChatGPT.
"""

from __future__ import annotations

from typing import Any, Optional

# Real loadable deck-skill names (SkillsRegistry / tools table, see
# zhanlu-ppt-deck-pipeline pitfall 24) → deck personality.
SKILL_DECK_PROFILES: dict[str, dict[str, Any]] = {
    # Default consulting skill — balanced business register.
    "ppt-design": {
        "theme": "bold_signal",
        "palette": "analytics_amber",
        "deck_type": "data_report",
        "structure": {
            "framing": "balanced consulting narrative — problem → evidence → "
                       "recommendation, with a KPI snapshot up front and "
                       "findings cards in the middle",
            "preferred_layouts": [
                "kpi_grid", "findings_cards", "chart_with_bullets",
                "comparison", "recommendations",
            ],
        },
    },
    # Strategy / investor-grade narrative.
    "slide-maestro": {
        "theme": "bold_signal",
        "palette": "b2b_navy",
        "deck_type": "investor_deck",
        "structure": {
            "framing": "investor narrative arc — market opportunity, traction "
                       "timeline, roadmap of phases, competitive comparison, "
                       "SWOT, then the ask",
            "preferred_layouts": [
                "roadmap", "timeline", "comparison", "swot", "chart_full",
                "recommendations",
            ],
        },
    },
    # Tech / AI-forward look.
    "kai-slide-creator": {
        "theme": "neon_cyber",
        "palette": "ai_violet",
        "deck_type": "marketing",
        "structure": {
            "framing": "tech-forward launch narrative — capability highlights, "
                       "process flow of how it works, timeline of milestones, "
                       "metric snapshots, big closing vision",
            "preferred_layouts": [
                "kpi_grid", "process_flow", "timeline", "chart_full",
                "quote", "closing",
            ],
        },
    },
    # Editorial / brand / luxury register (top-tier consulting visual style).
    "guizang-ppt-skill": {
        "theme": "vintage_editorial",
        "palette": "luxury_gold",
        "deck_type": "marketing",
        "structure": {
            "framing": "editorial magazine rhythm — a strong pull-quote "
                       "opening, section dividers that breathe, comparison of "
                       "positions, a stat-led KPI moment, closing statement",
            "preferred_layouts": [
                "quote", "section_divider", "comparison", "kpi_grid",
                "findings_cards", "closing",
            ],
        },
    },
    # Academic / research / knowledge communication.
    "knowledge-cat-ppt-skill": {
        "theme": "paper_and_ink",
        "palette": "edu_indigo",
        "deck_type": "executive_brief",
        "structure": {
            "framing": "rigorous research briefing — evidence first, data "
                       "tables for the numbers, methodology disclosed, "
                       "findings as cards, measured recommendations",
            "preferred_layouts": [
                "data_table", "findings_cards", "chart_full", "methodology",
                "insights_bullets",
            ],
        },
    },
    # Marketing / agency energy.
    "agentbuff-presentation": {
        "theme": "creative_voltage",
        "palette": "agency_pink",
        "deck_type": "marketing",
        "structure": {
            "framing": "high-energy campaign narrative — punchy KPI opens, "
                       "before/after comparisons, process flow of the play, "
                       "bold closing call-to-action",
            "preferred_layouts": [
                "kpi_grid", "comparison", "process_flow", "quote",
                "recommendations",
            ],
        },
    },
    # Interactive web-slide feel, mapped to a clean studio theme.
    "frontend-slides": {
        "theme": "electric_studio",
        "palette": "micro_indigo",
        "deck_type": "executive_brief",
        "structure": {
            "framing": "clean studio brief — agenda up front, one idea per "
                       "slide, KPI snapshot, chart evidence, crisp "
                       "recommendations",
            "preferred_layouts": [
                "agenda", "kpi_grid", "chart_with_bullets", "chart_full",
                "recommendations",
            ],
        },
    },
    # The native-editable renderer itself.
    "slide-skill": {
        "theme": "swiss_modern",
        "palette": "saas_blue",
        "deck_type": "data_report",
        "structure": {
            "framing": "clean Swiss data report — strict agenda, KPI grid, "
                       "one chart per dimension, table for dense figures, "
                       "plain-language insights",
            "preferred_layouts": [
                "agenda", "kpi_grid", "chart_full", "data_table",
                "insights_bullets",
            ],
        },
    },
}


def resolve_skill_profile(skill_name: Optional[str]) -> Optional[dict[str, Any]]:
    """Return the deck personality for a skill name (exact, then fuzzy).

    Fuzzy matches cover aliases the agent may pass: "guizang" →
    "guizang-ppt-skill", "kai" → "kai-slide-creator", "ppt design" →
    "ppt-design".  Returns None for unknown skills (no-op — never error).
    """
    if not skill_name:
        return None
    key = str(skill_name).strip().lower().replace(" ", "-")
    if key in SKILL_DECK_PROFILES:
        return SKILL_DECK_PROFILES[key]
    for name, prof in SKILL_DECK_PROFILES.items():
        if key in name or name in key:
            return prof
    return None


def skill_structure_steer(skill_name: Optional[str]) -> str:
    """Return planner-prompt steer text for a skill's narrative structure.

    Empty string when the skill is unknown (no-op — never injects noise).
    The steer names the skill and lists its preferred layouts + framing so
    the LLM deck planner picks the archetype mix the skill is known for —
    structure variety between skills, not just color variety.
    """
    prof = resolve_skill_profile(skill_name)
    if prof is None:
        return ""
    structure = prof.get("structure") or {}
    framing = structure.get("framing", "")
    layouts = structure.get("preferred_layouts") or []
    if not framing and not layouts:
        return ""
    lines = [
        f"\nSKILL STRUCTURE STEER (this deck is built with the '{skill_name}' "
        f"deck skill — shape the narrative the way that skill would):",
    ]
    if framing:
        lines.append(f"- Framing: {framing}")
    if layouts:
        lines.append(
            "- Prefer these layouts when the content fits: "
            + ", ".join(layouts)
            + ". Only use them when they genuinely fit; never force a layout "
            "that the content doesn't support."
        )
    return "\n".join(lines) + "\n"


def apply_skill_profile(plan: Any, skill_name: Optional[str]) -> bool:
    """Apply a skill's deck personality onto a DeckPlan in place.

    Visual identity (theme + palette) is set from the profile whenever a
    skill resolves — the agent loaded that skill for THIS deck, so its look
    dominates the LLM's content-based theme guess.  The user's own style
    words in the message still win (select_theme checks style keywords
    before plan.theme_recommendation).  Deck type is only filled when still
    the generic default so intent-driven structure is never downgraded.
    Returns True when a profile was applied (theme/palette set).
    """
    prof = resolve_skill_profile(skill_name)
    if prof is None or plan is None:
        return False

    plan.theme_recommendation = prof["theme"]
    plan.palette_recommendation = prof["palette"]
    changed = True
    # Deck type: only when still the generic default.
    if (getattr(plan, "deck_type", "") or "data_report") == "data_report":
        plan.deck_type = prof["deck_type"]
    return changed


__all__ = [
    "SKILL_DECK_PROFILES",
    "resolve_skill_profile",
    "apply_skill_profile",
    "skill_structure_steer",
]
