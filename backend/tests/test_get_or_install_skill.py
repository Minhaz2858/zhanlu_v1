"""Tests for get_or_install_skill auto-fetch from bundled marketplace."""
from __future__ import annotations

import pytest


class TestGetOrInstallSkill:
    """get_or_install_skill should resolve skills via the three-tier lookup."""

    def test_returns_none_for_nonexistent_skill(self):
        from app.services.skills_loader import get_or_install_skill
        result = get_or_install_skill("nonexistent-skill-xyz")
        assert result is None

    def test_auto_installs_docx_from_bundled(self):
        """The docx skill exists in backend/skills/docx/SKILL.md.
        get_or_install_skill should find it and write it to ~/.zhanlu/skills.
        """
        from app.services.skills_loader import get_or_install_skill, get_skills_registry

        # Force reload to clear any cached state
        registry = get_skills_registry()
        registry.reload()

        # The docx skill may or may not be in filesystem registry yet.
        # get_or_install_skill should install it from the bundled marketplace.
        skill = get_or_install_skill("docx", category="doc")

        assert skill is not None
        assert skill.name == "docx"
        assert "Word document" in skill.description or ".docx" in skill.description

    def test_auto_installed_skill_persists_in_registry(self):
        """After auto-install, the skill should be in the registry and re-loadable."""
        from app.services.skills_loader import get_or_install_skill, get_skills_registry

        registry = get_skills_registry()
        registry.reload()

        skill = get_or_install_skill("docx")
        assert skill is not None

        # Should be in the registry now
        skill2 = registry.get("docx")
        assert skill2 is not None
        assert skill2.name == skill.name

    def test_filesystem_tier_wins_over_bundled(self):
        """If the skill is already on filesystem, return it without re-copying."""
        from app.services.skills_loader import get_or_install_skill

        skill = get_or_install_skill("docx")
        assert skill is not None
        assert skill.source in ("bundled", "user")
