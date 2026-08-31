"""Tests for skill-aware deck personalities (2026-08-29).

The agent loads one of eight professional deck skills; the chosen skill now
changes the deck's visual identity via skill_deck_profiles (theme, palette,
deck-type).  These tests lock the personality map, the fuzzy resolver, and
the conservative apply rules (never override explicit planner picks).

2026-08-31: skills now also steer the NARRATIVE structure — each profile
carries a ``structure`` block (framing + preferred layouts) injected into the
deck-planner prompt via ``skill_structure_steer``.  Tests lock the block
completeness, layout-catalog sync, and the steer text.
"""

from app.services.synexia.contracts import DeckPlan, SlidePlan
from app.services.artifacts.deck_planner import _VALID_LAYOUTS
from app.services.artifacts.skill_deck_profiles import (
    SKILL_DECK_PROFILES,
    apply_skill_profile,
    resolve_skill_profile,
    skill_structure_steer,
)
from app.services.artifacts.themes import THEME_CATALOG


def _plan(theme: str = "", deck_type: str = "data_report", palette: str = "") -> DeckPlan:
    return DeckPlan(
        title="T",
        deck_type=deck_type,
        theme_recommendation=theme,
        palette_recommendation=palette,
        slides=[SlidePlan(layout="cover", title="C")],
    )


def test_profiles_cover_all_real_loadable_skills() -> None:
    # The 8 real deck skills the planner can pick (pitfall 24 list).
    for name in (
        "ppt-design", "slide-maestro", "kai-slide-creator", "guizang-ppt-skill",
        "knowledge-cat-ppt-skill", "agentbuff-presentation", "frontend-slides",
        "slide-skill",
    ):
        assert name in SKILL_DECK_PROFILES, f"missing profile for {name}"


def test_profile_themes_are_real_catalog_names() -> None:
    for name, prof in SKILL_DECK_PROFILES.items():
        assert prof["theme"] in THEME_CATALOG, (
            f"skill {name} theme {prof['theme']!r} not in THEME_CATALOG"
        )
        assert prof["deck_type"] in (
            "data_report", "investor_deck", "marketing", "executive_brief",
        )


def test_resolve_exact_and_fuzzy() -> None:
    prof = resolve_skill_profile("kai-slide-creator")
    assert prof is not None and prof["theme"] == "neon_cyber"
    prof = resolve_skill_profile("KAI-SLIDE-CREATOR")
    assert prof is not None and prof["theme"] == "neon_cyber"
    # Fuzzy aliases the agent may pass.
    prof = resolve_skill_profile("kai")
    assert prof is not None and prof["theme"] == "neon_cyber"
    prof = resolve_skill_profile("guizang")
    assert prof is not None and prof["theme"] == "vintage_editorial"
    prof = resolve_skill_profile("ppt-design")
    assert prof is not None and prof["palette"] == "analytics_amber"


def test_resolve_unknown_is_none() -> None:
    assert resolve_skill_profile("") is None
    assert resolve_skill_profile(None) is None
    assert resolve_skill_profile("not-a-real-skill") is None


def test_apply_fills_unset_fields() -> None:
    plan = _plan()
    assert apply_skill_profile(plan, "kai-slide-creator") is True
    assert plan.theme_recommendation == "neon_cyber"
    assert plan.palette_recommendation == "ai_violet"
    assert plan.deck_type == "marketing"


def test_apply_dominates_visual_keeps_intent_structure() -> None:
    # Skill identity wins over the LLM's content-based theme guess...
    plan = _plan(theme="paper_and_ink", palette="luxury_gold", deck_type="investor_deck")
    assert apply_skill_profile(plan, "kai-slide-creator") is True
    assert plan.theme_recommendation == "neon_cyber"
    assert plan.palette_recommendation == "ai_violet"
    # ...but an explicit deck TYPE is never downgraded by the skill.
    assert plan.deck_type == "investor_deck"


def test_apply_theme_overrides_legacy_name() -> None:
    # Legacy names (zhanlu-blue) aren't in the 12-theme catalog → replaced.
    plan = _plan(theme="zhanlu-blue")
    assert apply_skill_profile(plan, "guizang-ppt-skill") is True
    assert plan.theme_recommendation == "vintage_editorial"


def test_apply_unknown_skill_noop() -> None:
    plan = _plan()
    assert apply_skill_profile(plan, "unknown-skill") is False
    assert plan.theme_recommendation == ""


# ---------------------------------------------------------------------------
# Structure steer (2026-08-31)
# ---------------------------------------------------------------------------


def test_every_profile_has_structure_block() -> None:
    for name, prof in SKILL_DECK_PROFILES.items():
        structure = prof.get("structure")
        assert structure is not None, f"skill {name} missing structure block"
        assert structure.get("framing"), f"skill {name} missing framing"
        layouts = structure.get("preferred_layouts")
        assert layouts, f"skill {name} missing preferred_layouts"


def test_preferred_layouts_are_real_catalog_names() -> None:
    for name, prof in SKILL_DECK_PROFILES.items():
        for layout in prof["structure"]["preferred_layouts"]:
            assert layout in _VALID_LAYOUTS, (
                f"skill {name} preferred layout {layout!r} not in _VALID_LAYOUTS"
            )


def test_structure_steer_emits_framing_and_layouts() -> None:
    steer = skill_structure_steer("slide-maestro")
    assert "SKILL STRUCTURE STEER" in steer
    assert "slide-maestro" in steer
    assert "investor narrative arc" in steer
    assert "roadmap" in steer and "swot" in steer


def test_structure_steer_fuzzy_alias() -> None:
    steer = skill_structure_steer("kai")
    assert "SKILL STRUCTURE STEER" in steer
    assert "tech-forward launch narrative" in steer


def test_structure_steer_unknown_is_empty() -> None:
    assert skill_structure_steer("") == ""
    assert skill_structure_steer(None) == ""
    assert skill_structure_steer("not-a-real-skill") == ""


def test_planner_prompt_embeds_structure_steer() -> None:
    from app.services.artifacts.deck_planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        "make an investor deck",
        "Deck profile: data_report — Target 8-12 slides.",
        [],
        0,
        skill_structure=skill_structure_steer("slide-maestro"),
    )
    assert "SKILL STRUCTURE STEER" in prompt
    assert "roadmap" in prompt
    # The steer must sit before the methodology block, not be dropped.
    assert prompt.index("SKILL STRUCTURE STEER") < prompt.index("METHODOLOGY")

    plain = _build_planner_prompt("make a deck", "x", [], 0)
    assert "SKILL STRUCTURE STEER" not in plain
