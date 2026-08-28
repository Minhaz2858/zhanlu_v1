"""Regression test: pptx requests must route to the ppt-design skill.

FIX 2026-08-23 (resolver.py): DEFAULT_SKILL_MAP["pptx"] was changed from the
bare python-pptx skill to the professional design skill ``ppt-design``
(backend/skills/ppt_skills/Powerpoint-fancy-design-main — styles, slide
engine, audit rubric). These tests lock that wiring in so a future refactor
cannot silently regress pptx routing back to a plain pptx builder.

Covers:
1. DEFAULT_SKILL_MAP["pptx"] == "ppt-design"
2. resolve() with a real pptx-format intent (via the real
   ``detect_file_intent`` — no mocking) returns chosen_skill == "ppt-design"
3. the ppt-design skill directory exists on disk (SKILL.md + audit rubric)
4. the agent system prompt still advertises the ppt-design skill bullet
"""

from pathlib import Path

import pytest

from app.services.skill_routing.resolver import DEFAULT_SKILL_MAP, SkillResolver
from app.services.synexia.intent_router import detect_file_intent

# backend/ is the repo root for app.* imports; this file lives at
# backend/tests/services/skill_routing/test_resolver_pptx.py
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_PPT_DESIGN_DIR = (
    _BACKEND_ROOT / "skills" / "ppt_skills" / "Powerpoint-fancy-design-main"
)


def test_default_skill_map_pptx_points_to_ppt_design():
    """The FIX: pptx format must map to the professional design skill."""
    assert DEFAULT_SKILL_MAP["pptx"] == "ppt-design"


def test_detect_file_intent_sees_pptx_keyword():
    """Sanity: the format detector actually recognizes the test phrase."""
    assert detect_file_intent("make a pptx about c5/c9 market") == "pptx"


def test_pptx_format_intent_routes_to_ppt_design():
    """End-to-end: generic pptx request (no picked skill) → ppt-design."""
    resolver = SkillResolver()
    decision = resolver.resolve(
        user_message="make a pptx about c5/c9 market",
        picked_skill=None,
    )
    assert decision.chosen_skill == "ppt-design"
    assert decision.namespace == "builtin:ppt-design"
    assert decision.source == "builtin"
    assert decision.is_default is True
    assert decision.reason == "format_intent"


@pytest.mark.parametrize(
    "message",
    [
        "make a pptx about c5/c9 market",
        "can you build a PowerPoint deck on c5/c9 market share",
        "create a slide deck for the c9 launch",
        "请做一个关于 c5/c9 市场的演示文稿",
    ],
)
def test_pptx_intent_variants_route_to_ppt_design(message):
    """EN + ZH phrasings all funnel into the ppt-design skill."""
    resolver = SkillResolver()
    decision = resolver.resolve(user_message=message, picked_skill=None)
    assert decision.chosen_skill == "ppt-design"
    assert decision.reason == "format_intent"


def test_pptx_without_format_keyword_does_not_route_to_ppt_design():
    """Control: a non-pptx message must NOT hard-route to ppt-design."""
    resolver = SkillResolver()
    decision = resolver.resolve(
        user_message="hello, what can you do for me?",
        picked_skill=None,
    )
    assert decision.chosen_skill != "ppt-design"


def test_ppt_design_skill_directory_exists_on_disk():
    """The skill the resolver points at must actually ship with the backend."""
    skill_md = _PPT_DESIGN_DIR / "SKILL.md"
    assert skill_md.is_file(), f"ppt-design SKILL.md missing: {skill_md}"
    # Core of the FIX: the skill carries a slide engine + audit rubric.
    assert (_PPT_DESIGN_DIR / "references" / "presentation-quality-rubric.md").is_file()
    assert (_PPT_DESIGN_DIR / "styles").is_dir()


def test_agent_prompt_advertises_ppt_design_skill():
    """agent_prompts.py must still surface the ppt-design skill bullet."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK

    assert "ppt-design" in _DEFAULT_SKILLS_BLOCK
