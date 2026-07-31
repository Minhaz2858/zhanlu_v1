"""Tests for the skills loading and injection system.

These tests verify the backend half of the skill-selection flow:
1. Skills are loaded from SKILL.md files with body content (methodology)
2. The DB-synced Tool rows have ``skill_md`` populated
3. ``get_skill_prompt_for_agent`` returns the methodology body
4. Frontmatter is parsed correctly (name, description, trigger, etc.)
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure the backend package is importable
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.skills_loader import (
    SkillsRegistry,
    parse_frontmatter,
    parse_skill_file,
    get_skill_prompt_for_agent,
    get_skill_metadata_for_agent,
    _lookup_skill_summary,
)


# ─── Fixtures ─────────────────────────────────────────────────────────

SKILLS_DIR = BACKEND_DIR / "skills"


@pytest.fixture
def registry():
    """Fresh SkillsRegistry pointed at the real skills/ directory."""
    reg = SkillsRegistry(skills_dir=str(SKILLS_DIR))
    reg.load()
    return reg


# ─── Filesystem skills loader tests ───────────────────────────────────

class TestSkillsLoader:
    def test_skills_directory_exists(self):
        assert SKILLS_DIR.exists(), f"Skills directory not found at {SKILLS_DIR}"

    def test_registry_loads_skills(self, registry):
        skills = registry.list_skills()
        assert len(skills) > 0, "No skills loaded from filesystem"
        # We know there are 35 SKILL.md files
        assert len(skills) >= 30, f"Expected >=30 skills, got {len(skills)}"

    def test_loaded_skills_have_body_content(self, registry):
        """Every loaded skill must have a non-empty body (methodology)."""
        skills = registry.list_skills()
        skills_with_body = [s for s in skills if s.body and s.body.strip()]
        # Most skills should have body content (template-skill has minimal)
        assert len(skills_with_body) >= 30, (
            f"Only {len(skills_with_body)}/{len(skills)} skills have body content"
        )

    def test_specific_skills_are_loaded(self, registry):
        """Verify key skills that the chatbot depends on are present."""
        # frontend-design was replaced by the Claude artifacts-builder
        # skill when the skills folder was swapped (P0 remap).
        expected = ["pdf", "pptx", "docx", "artifacts-builder", "skill-creator"]
        for name in expected:
            skill = registry.get(name)
            assert skill is not None, f"Skill '{name}' not found in registry"
            assert skill.body, f"Skill '{name}' has empty body"

    def test_skill_has_name_and_description(self, registry):
        skill = registry.get("pdf")
        assert skill is not None
        assert skill.name == "pdf"
        assert skill.description, "pdf skill has no description"

    def test_skill_body_contains_methodology(self, registry):
        """The skill body should contain actual methodology content,
        not just a title or empty heading."""
        skill = registry.get("pdf")
        assert skill is not None
        # Body should be substantial (not just "# PDF\n")
        assert len(skill.body) > 100, (
            f"pdf skill body too short ({len(skill.body)} chars) — expected methodology"
        )

    def test_registry_only_loads_skill_md_files(self, registry):
        """The loader must only pick up SKILL.md files, not README.md,
        examples/*.md, or other supporting docs."""
        skills = registry.list_skills()
        skill_names = {s.name for s in skills}
        # These are NOT skills — they're supporting docs that should NOT appear
        forbidden = {"README", "CHANGELOG", "LICENSE"}
        for name in forbidden:
            assert name not in skill_names, f"'{name}' should not be loaded as a skill"

    def test_get_skill_prompt_returns_body(self, registry):
        """get_skill_prompt should return the skill's methodology body."""
        prompt = registry.get_skill_prompt("pdf")
        assert prompt is not None
        assert len(prompt) > 50

    def test_get_skill_prompt_returns_none_for_missing(self, registry):
        assert registry.get_skill_prompt("nonexistent-skill-xyz") is None


# ─── Frontmatter parsing tests ────────────────────────────────────────

class TestFrontmatterParsing:
    def test_parse_valid_frontmatter(self):
        content = """---
name: test-skill
description: A test skill
trigger: /test
---

# Test Skill Body
Step 1: Do something
"""
        fm, body = parse_frontmatter(content)
        assert fm["name"] == "test-skill"
        assert fm["description"] == "A test skill"
        assert fm["trigger"] == "/test"
        assert "# Test Skill Body" in body

    def test_parse_no_frontmatter(self):
        content = "Just some markdown without frontmatter."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_parse_skill_file_from_real_file(self):
        """Parse an actual SKILL.md from the filesystem."""
        pdf_skill_path = SKILLS_DIR / "pdf" / "SKILL.md"
        if not pdf_skill_path.exists():
            pytest.skip(f"{pdf_skill_path} not found")
        skill = parse_skill_file(pdf_skill_path, source="bundled")
        assert skill is not None
        assert skill.name == "pdf"
        assert skill.description
        assert skill.body
        assert skill.category == "pdf"  # category = parent dir name


# ─── DB-backed skill prompt tests ─────────────────────────────────────

class TestSkillPromptForAgent:
    """Test ``get_skill_prompt_for_agent`` which is used by the backend
    to inject skill methodology into agent system prompts."""

    def test_returns_empty_for_empty_list(self):
        result = get_skill_prompt_for_agent([], db=None)
        assert result == ""

    def test_returns_empty_for_nonexistent_skill(self):
        result = get_skill_prompt_for_agent(["nonexistent-xyz"], db=None)
        assert result == ""

    def test_returns_body_for_known_skill_no_db(self, registry):
        """Without a DB session, should fall back to filesystem registry."""
        result = get_skill_prompt_for_agent(["pdf"], db=None)
        assert result
        assert "### Skill: pdf" in result
        assert "## Loaded Skills" in result

    def test_returns_bodies_for_multiple_skills(self, registry):
        result = get_skill_prompt_for_agent(["pdf", "pptx"], db=None)
        assert result
        assert "### Skill: pdf" in result
        assert "### Skill: pptx" in result

    def test_db_lookup_finds_skill_md(self):
        """When a DB session is available, should read skill_md from
        the ``tools`` table (this is what the frontend's Tool entity
        maps to)."""
        from app.database import SessionLocal
        from app.models.tool import Tool

        db = SessionLocal()
        try:
            # Verify at least one tool with skill_md exists
            tool = db.query(Tool).filter(
                Tool.is_deleted == False,
                Tool.enabled == True,
                Tool.skill_md.isnot(None),
            ).first()
            if not tool:
                pytest.skip("No tools with skill_md in DB")

            result = get_skill_prompt_for_agent([tool.name], db=db)
            assert result, f"Expected non-empty prompt for skill '{tool.name}'"
            assert f"### Skill: {tool.name}" in result
            # The body should contain the actual skill_md content
            assert tool.skill_md[:50] in result
        finally:
            db.close()


# ─── Progressive disclosure tests (Phase 0) ────────────────────────────

class TestProgressiveDisclosure:
    """Test the metadata-only injection path (Phase 0.1)."""

    def test_get_skill_metadata_for_agent_empty_list(self):
        """Empty skill list returns empty string."""
        result = get_skill_metadata_for_agent([], db=None)
        assert result == ""

    def test_get_skill_metadata_for_agent_known_skill(self):
        """Metadata path returns name + summary, NOT full body."""
        result = get_skill_metadata_for_agent(["pdf"], db=None)
        assert result
        assert "## Available Skills" in result
        assert "load_skill_body" in result  # hint in the header
        assert "**pdf**" in result
        # MUST NOT contain the full body (which would be hundreds of chars)
        # The summary should be short — the metadata output has no "## Loaded Skills" header
        assert "## Loaded Skills" not in result
        # The full body from pdf skill is typically > 500 chars
        full_prompt = get_skill_prompt_for_agent(["pdf"], db=None)
        assert len(result) < len(full_prompt), (
            "Metadata path should produce shorter output than full-body path"
        )

    def test_get_skill_metadata_for_agent_multiple(self):
        """Metadata path for multiple skills — at least 2 should appear."""
        result = get_skill_metadata_for_agent(["pdf", "docx"], db=None)
        assert result
        assert "**pdf**" in result
        assert "**docx**" in result
        # Still no full body
        assert "## Loaded Skills" not in result

    def test_token_savings_progressive_vs_full(self):
        """Progressive disclosure path is significantly shorter than full-body path."""
        skills = ["pdf", "docx"]
        meta_result = get_skill_metadata_for_agent(skills, db=None)
        full_result = get_skill_prompt_for_agent(skills, db=None)

        meta_len = len(meta_result)
        full_len = len(full_result)

        # Metadata should be at least 5× shorter (typically 10-30×)
        assert full_len > 0, "Full prompt should not be empty"
        assert meta_len > 0, "Metadata prompt should not be empty"
        ratio = full_len / meta_len
        assert ratio >= 5.0, (
            f"Expected metadata to be at least 5× shorter than full body, "
            f"but full={full_len} chars, meta={meta_len} chars (ratio={ratio:.1f}×)"
        )

    def test_summary_lookup_returns_string(self):
        """_lookup_skill_summary returns a short summary string."""
        summary = _lookup_skill_summary("pdf", db=None)
        assert summary is not None
        assert isinstance(summary, str)
        assert len(summary) > 0
        # Summary should be short (≤200 chars when from description fallback)
        assert len(summary) <= 200, (
            f"Summary too long: {len(summary)} chars (expected ≤200)"
        )

    def test_summary_lookup_nonexistent_returns_none(self):
        """Unknown skill returns None."""
        assert _lookup_skill_summary("nonexistent-xyz", db=None) is None


class TestProgressiveDisclosureDB:
    """Test the metadata path with a DB session (DB-first lookup)."""

    @pytest.fixture
    def db_session(self):
        from app.database import SessionLocal
        db = SessionLocal()
        yield db
        db.close()

    def test_metadata_from_db(self, db_session):
        """get_skill_metadata_for_agent reads summaries from DB tools table."""
        from app.models.tool import Tool
        # Find a marketplace skill with a description (skip stray entries)
        tool = db_session.query(Tool).filter(
            Tool.is_deleted == False,
            Tool.enabled == True,
            Tool.source == "marketplace",
            Tool.description.isnot(None),
            Tool.description != "",
        ).first()
        if not tool:
            pytest.skip("No marketplace tools with descriptions in DB")

        result = get_skill_metadata_for_agent([tool.name], db=db_session)
        assert result
        assert f"**{tool.name}**" in result
        assert "## Available Skills" in result

    def test_metadata_is_shorter_than_full_from_db(self, db_session):
        """DB-backed metadata path is shorter than DB-backed full-body path."""
        from app.models.tool import Tool
        tools = db_session.query(Tool).filter(
            Tool.is_deleted == False,
            Tool.enabled == True,
            Tool.source == "marketplace",
            Tool.skill_md.isnot(None),
        ).limit(3).all()
        if len(tools) < 2:
            pytest.skip("Need at least 2 marketplace tools with skill_md")

        names = [t.name for t in tools]
        meta_result = get_skill_metadata_for_agent(names, db=db_session)
        full_result = get_skill_prompt_for_agent(names, db=db_session)

        assert len(meta_result) < len(full_result), (
            f"Metadata ({len(meta_result)} chars) should be shorter "
            f"than full body ({len(full_result)} chars)"
        )


# ─── DB state verification tests ──────────────────────────────────────

class TestDatabaseSkillState:
    """Verify the database has the expected skill rows with methodology."""

    @pytest.fixture
    def db_session(self):
        from app.database import SessionLocal
        db = SessionLocal()
        yield db
        db.close()

    def test_db_has_marketplace_skills(self, db_session):
        from app.models.tool import Tool
        tools = db_session.query(Tool).filter(
            Tool.is_deleted == False,
            Tool.source == "marketplace",
        ).all()
        assert len(tools) >= 30, f"Expected >=30 marketplace skills, got {len(tools)}"

    def test_db_skills_have_skill_md(self, db_session):
        from app.models.tool import Tool
        tools = db_session.query(Tool).filter(
            Tool.is_deleted == False,
            Tool.source == "marketplace",
            Tool.skill_md.isnot(None),
        ).all()
        assert len(tools) >= 30, (
            f"Expected >=30 marketplace skills with skill_md, got {len(tools)}"
        )

    def test_specific_skills_in_db(self, db_session):
        from app.models.tool import Tool
        expected = ["pdf", "pptx", "docx", "frontend-design"]
        for name in expected:
            tool = db_session.query(Tool).filter(
                Tool.name == name,
                Tool.is_deleted == False,
            ).first()
            assert tool is not None, f"Skill '{name}' not found in DB"
            assert tool.skill_md, f"Skill '{name}' has empty skill_md"
            assert len(tool.skill_md) > 100, (
                f"Skill '{name}' skill_md too short ({len(tool.skill_md)} chars)"
            )
