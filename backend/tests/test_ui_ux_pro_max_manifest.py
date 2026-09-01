"""Tests for the ui-ux-pro-max skill manifest discovery."""

from __future__ import annotations

import pytest


def test_manifest_index_discovers_uiux():
    """ManifestIndex picks up ui-ux-pro-max via manifest.yaml."""
    from app.services.skills_loader.manifest_index import get_manifest_index

    idx = get_manifest_index()
    idx.ensure_loaded()
    m = idx.get("ui-ux-pro-max")
    assert m is not None, "ui-ux-pro-max manifest not discovered"
    assert m.name == "ui-ux-pro-max"
    assert m.version  # semver string set
    assert "design" in m.tags
    assert "ui" in m.tags
    assert "dashboard" in m.tags


def test_manifest_appears_in_default_skills_block():
    """The default skills block exposed by agent_prompts mentions ui-ux-pro-max."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK

    assert "ui-ux-pro-max" in _DEFAULT_SKILLS_BLOCK
    # And mentions the tool names so the planner can call them
    assert "uiux_design_system" in _DEFAULT_SKILLS_BLOCK
    assert "uiux_search" in _DEFAULT_SKILLS_BLOCK
    # And tells the planner to call BEFORE building visual artifacts
    assert "before" in _DEFAULT_SKILLS_BLOCK.lower()


def test_skills_loader_loads_skill_md_body():
    """The SkillsRegistry (filesystem) exposes ui-ux-pro-max with body content.

    Note: ``get_skill_prompt_for_agent`` prefers the DB-synced body for
    marketplace skills (which were pre-synced before this integration), so
    here we go directly to the filesystem registry which reflects the
    on-disk SKILL.md content.
    """
    from app.services.skills_loader import get_skills_registry

    registry = get_skills_registry()
    skill = registry.get("ui-ux-pro-max")
    assert skill is not None
    assert skill.body
    assert "ui-ux-pro-max" in skill.body
    # SKILL.md documents the workflow
    assert "uiux_design_system" in skill.body or "uiux_search" in skill.body


def test_resolver_has_companion_routes():
    """skill_routing.resolver exposes ui-ux-pro-max as companion target."""
    from app.services.skill_routing.resolver import (
        COMPANION_SKILLS,
        DEFAULT_SKILL_MAP,
    )

    assert "ui-ux-pro-max" in COMPANION_SKILLS
    assert DEFAULT_SKILL_MAP.get("design") == "ui-ux-pro-max"
    assert DEFAULT_SKILL_MAP.get("uiux") == "ui-ux-pro-max"
    assert DEFAULT_SKILL_MAP.get("palette") == "ui-ux-pro-max"
    # dashboard was remapped to the Claude artifacts-builder skill
    assert DEFAULT_SKILL_MAP.get("dashboard") == "artifacts-builder"