"""Test that SkillFactory.create_from_description actually calls the LLM.

Before the fix, the async detection in create_from_description was broken —
it always raised RuntimeError and fell back to _generate_fallback_code,
meaning the LLM was never called. This test verifies the fix: the LLM
IS called and its response is used as the skill body.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

# Set up test DB
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401 — register all models

Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def cleanup_skills():
    """Clean up any skills written to ~/.zhanlu/skills/ during tests."""
    import tempfile
    from pathlib import Path
    from app.services.skill_sync import USER_SKILLS_DIR

    # Save original and use a temp dir for tests
    original_dir = USER_SKILLS_DIR
    temp_dir = Path(tempfile.mkdtemp(prefix="test_skills_"))

    # Patch the USER_SKILLS_DIR for the duration of the test
    import app.services.skill_sync as skill_sync_mod
    skill_sync_mod.USER_SKILLS_DIR = temp_dir

    yield

    # Restore
    skill_sync_mod.USER_SKILLS_DIR = original_dir

    # Clean up temp dir
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_factory_calls_llm():
    """The LLM should be called and its response used as the skill body."""
    from app.services.agent_studio.skill_factory import SkillFactory

    db = SessionLocal()
    try:
        factory = SkillFactory(db)

        # Mock call_llm to return a specific response
        mock_response = {
            "response": "## Overview\n\nThis is a test skill.\n\n## Steps\n\n1. Do something\n2. Do something else\n\n## Best Practices\n\nBe careful.",
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "data": None,
        }

        with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_response) as mock_llm:
            candidate = await factory.create_from_description(
                name="test-skill-llm",
                description="A test skill for LLM verification",
            )

            # Verify the LLM was actually called
            assert mock_llm.called, "call_llm was never called — the bug is still present"
            assert mock_llm.call_count == 1

            # Verify the LLM response was used in the generated skill
            assert "This is a test skill" in candidate.generated_skill_md
            assert "Do something" in candidate.generated_skill_md

            # Verify the source_data records that LLM was used
            assert candidate.source_data.get("llm_used") is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_factory_persists_skill():
    """The factory should persist the skill to the filesystem and reload the registry."""
    from app.services.agent_studio.skill_factory import SkillFactory
    from app.services.skill_sync import USER_SKILLS_DIR

    db = SessionLocal()
    try:
        factory = SkillFactory(db)

        mock_response = {
            "response": "## Overview\n\nA persisted test skill.\n\n## Steps\n\n1. Step one",
            "model": "test-model",
            "usage": {},
            "data": None,
        }

        with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_response):
            candidate = await factory.create_from_description(
                name="test-persisted-skill",
                description="A skill that should be persisted",
            )

            # Verify the skill file was written
            skill_file = USER_SKILLS_DIR / "custom" / "test-persisted-skill" / "SKILL.md"
            assert skill_file.exists(), f"SKILL.md not written to {skill_file}"

            # Verify the file contains the generated body
            content = skill_file.read_text()
            assert "A persisted test skill" in content
            assert "Step one" in content

            # Verify the skill path is recorded in source_data
            assert candidate.source_data.get("skill_path") is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_factory_fallback_on_llm_failure():
    """If the LLM fails, the factory should fall back gracefully."""
    from app.services.agent_studio.skill_factory import SkillFactory

    db = SessionLocal()
    try:
        factory = SkillFactory(db)

        with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, side_effect=Exception("LLM unavailable")):
            candidate = await factory.create_from_description(
                name="test-fallback-skill",
                description="A skill that should use fallback",
            )

            # Verify LLM was attempted but failed
            assert candidate.source_data.get("llm_used") is False

            # Verify fallback body was generated
            assert candidate.generated_skill_md is not None
            assert len(candidate.generated_skill_md) > 0
            assert "Overview" in candidate.generated_skill_md
    finally:
        db.close()
