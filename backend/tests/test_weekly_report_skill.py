"""Tests for the ``weekly-report-generation`` filesystem skill.

Verifies:

* **Discoverability** — the skill package under ``backend/skills/weekly-report-generation/``
  loads through :func:`app.services.skills_loader.load_skill_package` and is
  registered by :class:`~app.services.skills_loader.SkillsRegistry`.
* **Router hook** — ``post_router_hook.post_router_pick`` returns the forced-skill
  dict for ``weekly-report-generation`` when the message matches the
  ``_WEEKLY_REPORT_INTENT_RE`` regex (same pattern as ``dashboard-generation``).
* **Scoring** — ``score_skill_match`` scores the weekly-report skill at or above
  ``STRONG_MATCH_THRESHOLD`` for weekly-report intent messages.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

SKILL_DIR = _BACKEND_ROOT / "skills" / "weekly-report-generation"


def test_manifest_valid():
    """manifest.yaml parses and carries the expected skill identity."""
    import yaml

    m = yaml.safe_load((SKILL_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    assert m["name"] == "weekly-report-generation"
    assert m["version"]
    assert "weekly" in m.get("tags", [])
    assert m.get("user_invocable", True) is True


def test_skill_loads_from_package():
    """load_skill_package returns metadata with a non-empty methodology body."""
    from app.services.skills_loader import load_skill_package

    skill = load_skill_package(SKILL_DIR, source="bundled")
    assert skill is not None, "weekly-report-generation failed to load from disk"
    assert skill.name == "weekly-report-generation"
    assert skill.description
    assert skill.body, "SKILL.md body must be non-empty"


def test_skill_discoverable_by_registry():
    """A SkillsRegistry pointed at backend/skills registers the skill."""
    from app.services.skills_loader import SkillsRegistry

    registry = SkillsRegistry(skills_dir=str(_BACKEND_ROOT / "skills"))
    skill = registry.get("weekly-report-generation")
    assert skill is not None, "weekly-report-generation not found in registry"
    assert "ask_perception" in skill.body
    assert "ask_data_agent" in skill.body


def test_prompt_describes_pipeline_and_schema_pitfall():
    """SKILL.md codifies the 4-step pipeline and the FNAME schema rule."""
    p = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for token in [
        "ask_perception",
        "ask_intelligence",
        "ask_diagnosis",
        "ask_data_agent",
        "ask_forecast_agent",
        "ask_pricing",
        "Executive Summary",
        "Price Dashboard",
        "Key Risks & Opportunities",
        "FNAME",
    ]:
        assert token in p, f"SKILL.md missing token: {token}"
    assert "material_name" in p, "SKILL.md must warn against material_name"


def test_score_skill_match_above_threshold():
    """Weekly-report intent must score >= STRONG_MATCH_THRESHOLD."""
    from app.services.skill_routing.post_router_hook import (
        STRONG_MATCH_THRESHOLD,
        score_skill_match,
    )

    skill = {
        "name": "weekly-report-generation",
        "description": "Generates a CEO-level weekly market report",
        "trigger": "weekly report, weekly market report, market report, weekly summary",
    }
    for msg in [
        "generate the weekly report for C5",
        "please give me a weekly market report",
        "I need the weekly summary of sales",
    ]:
        score = score_skill_match(msg, skill)
        assert score >= STRONG_MATCH_THRESHOLD, (
            f"score {score} below {STRONG_MATCH_THRESHOLD} for message: {msg!r}"
        )


def test_post_router_pick_forces_weekly_report_skill():
    """A weekly-report message with the skill in candidates forces the skill."""
    from app.services.skill_routing.post_router_hook import post_router_pick

    candidates = [
        {
            "name": "weekly-report-generation",
            "description": "Generates a CEO-level weekly market report",
            "trigger": "weekly report, weekly market report",
            "source": "filesystem",
        },
        {
            "name": "business-report-generator",
            "description": "Generates business reports",
            "trigger": "report",
            "source": "filesystem",
        },
    ]

    result = post_router_pick(
        "Give me the weekly market report for C5 resin",
        candidates=candidates,
    )
    assert result is not None
    assert result["skill_name"] == "weekly-report-generation"
    assert result["forced"] is True
    assert result["score"] == 1.0


def test_post_router_pick_weekly_intent_chinese():
    """Chinese weekly-report phrasing must also force the skill."""
    from app.services.skill_routing.post_router_hook import post_router_pick

    candidates = [
        {
            "name": "weekly-report-generation",
            "description": "Generates a CEO-level weekly market report",
            "trigger": "weekly report, 周报",
            "source": "filesystem",
        },
    ]
    result = post_router_pick("帮我生成一份本周的周报", candidates=candidates)
    assert result is not None
    assert result["skill_name"] == "weekly-report-generation"
