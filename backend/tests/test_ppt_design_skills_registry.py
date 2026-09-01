"""Tests for the 8 PPT design skills registered under backend/skills/ppt_skills/.

Verifies:
- Exactly the 8 expected design skills are discovered by the skills registry
  (the 4 redundant nested SKILL.md files were renamed to SKILL.md.bak, so
  neither their duplicate names nor unrelated names may register).
- Each skill carries the expected category / compatible_formats metadata,
  a non-empty description, and a body.
- The agent system prompt ("Deck guidance (PPTX)" section) surfaces the
  8-skill design menu and the deliverable rule.
"""

from __future__ import annotations

EXPECTED_PPT_DESIGN_SKILLS = {
    "agentbuff-presentation",
    "frontend-slides",
    "guizang-ppt-skill",
    "kai-slide-creator",
    "knowledge-cat-ppt-skill",
    "ppt-design",
    "slide-maestro",
    "slide-skill",
}

# Names that MUST NOT be registered (nested duplicates / unrelated utilities /
# redirect shims that were disabled by renaming their SKILL.md to .bak).
FORBIDDEN_SKILLS = {"skill-fuse", "slide"}


def test_registry_discovers_exactly_eight_ppt_design_skills():
    """A fresh filesystem scan registers exactly the 8 design skills."""
    from app.services.skills_loader import get_skills_registry

    registry = get_skills_registry()
    skills = registry.reload()
    names = set(skills.keys())

    # Every expected design skill is discoverable.
    missing = EXPECTED_PPT_DESIGN_SKILLS - names
    assert not missing, f"missing PPT design skills: {sorted(missing)}"

    # The nested-duplicate / unrelated names must NOT be registered.
    leaked = FORBIDDEN_SKILLS & names
    assert not leaked, f"forbidden skills leaked into registry: {sorted(leaked)}"

    # Exactly our 8 skills live in the presentation-design category.
    design_names = {
        s.name for s in registry.list_skills(category="presentation-design")
    }
    assert design_names == EXPECTED_PPT_DESIGN_SKILLS, (
        f"presentation-design category mismatch: {sorted(design_names)}"
    )


def test_ppt_design_skill_metadata():
    """Each design skill carries clean category + compatible_formats metadata."""
    from app.services.skills_loader import get_skills_registry

    registry = get_skills_registry()
    skills = registry.reload()
    for name in sorted(EXPECTED_PPT_DESIGN_SKILLS):
        skill = skills.get(name)
        assert skill is not None, name
        assert skill.category == "presentation-design", name
        assert "pptx" in skill.compatible_formats, name
        assert "html" in skill.compatible_formats, name
        assert skill.description, name
        assert skill.body, name


def test_ppt_design_skill_dirs_resolve():
    """get_skill_dir resolves a real folder for every design skill."""
    import os

    from app.services.skills_loader import get_skill_dir

    for name in EXPECTED_PPT_DESIGN_SKILLS:
        d = get_skill_dir(name)
        assert d, f"get_skill_dir returned None for {name}"
        assert os.path.isdir(d), f"skill dir missing for {name}: {d}"


def test_default_skills_block_contains_design_menu():
    """The agent system-prompt block surfaces the design menu + deliverable rule."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK

    block = _DEFAULT_SKILLS_BLOCK
    for name in sorted(EXPECTED_PPT_DESIGN_SKILLS):
        assert name in block, f"{name} missing from default skills block"
    assert "DELIVERABLE RULE" in block
    assert "slide-skill" in block
    assert "knowledge-cat-ppt-skill" in block
