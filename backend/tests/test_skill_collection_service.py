"""Test SkillCollectionService — mock agent_browser + LLM, verify the pipeline.

Verifies that:
1. The service calls agent_browser to extract page content
2. The service calls the LLM to structure the content
3. The result is validated and persisted
4. Error cases are handled gracefully
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401

Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def cleanup_skills():
    """Use a temp dir for skill files during tests."""
    from app.services.skill_sync import USER_SKILLS_DIR
    import app.services.skill_sync as skill_sync_mod

    original_dir = USER_SKILLS_DIR
    temp_dir = Path(tempfile.mkdtemp(prefix="test_collect_"))
    skill_sync_mod.USER_SKILLS_DIR = temp_dir

    yield

    skill_sync_mod.USER_SKILLS_DIR = original_dir
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_collect_from_url_success():
    """Full pipeline: extract → structure → validate → persist."""
    from app.services.skill_collection_service import SkillCollectionService

    db = SessionLocal()
    try:
        service = SkillCollectionService(db=db)

        # Mock agent_browser extract action
        mock_extract_result = {
            "success": True,
            "text": "# How to Review Pull Requests\n\nThis guide covers best practices for reviewing PRs.\n\n## Overview\n\nCode review is essential for quality.\n\n## Steps\n\n1. Read the description\n2. Review the diff\n3. Leave comments\n4. Approve or request changes",
            "url": "https://example.com/pr-review",
        }

        # Mock LLM structuring
        mock_llm_result = {
            "response": "{}",
            "model": "test-model",
            "usage": {},
            "data": {
                "name": "pr-review",
                "description": "A skill for reviewing pull requests",
                "body": "## Overview\n\nCode review is essential for quality.\n\n## Steps\n\n1. Read the description\n2. Review the diff\n3. Leave comments\n4. Approve or request changes",
            },
        }

        with patch("app.services.tool_handlers.agent_browser_tool._agent_browser", new_callable=AsyncMock, return_value=mock_extract_result):
            with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_llm_result):
                result = await service.collect_from_url(
                    url="https://example.com/pr-review",
                    skill_name="pr-review",
                )

        assert result["success"] is True
        assert result["skill_name"] == "pr-review"
        assert result["skill_path"] is not None
        assert "scan_findings" in result
        assert result["source_url"] == "https://example.com/pr-review"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_from_url_extract_failure():
    """If agent_browser fails, the service should return an error."""
    from app.services.skill_collection_service import SkillCollectionService

    db = SessionLocal()
    try:
        service = SkillCollectionService(db=db)

        mock_extract_result = {
            "success": False,
            "error": "Connection refused",
        }

        with patch("app.services.tool_handlers.agent_browser_tool._agent_browser", new_callable=AsyncMock, return_value=mock_extract_result):
            result = await service.collect_from_url(
                url="https://unreachable.example.com",
            )

        assert result["success"] is False
        assert result["stage"] == "extract"
        assert "Connection refused" in result["error"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_from_url_empty_page():
    """If the page content is too short, the service should return an error."""
    from app.services.skill_collection_service import SkillCollectionService

    db = SessionLocal()
    try:
        service = SkillCollectionService(db=db)

        mock_extract_result = {
            "success": True,
            "text": "short",  # Too short
            "url": "https://example.com/empty",
        }

        with patch("app.services.tool_handlers.agent_browser_tool._agent_browser", new_callable=AsyncMock, return_value=mock_extract_result):
            result = await service.collect_from_url(
                url="https://example.com/empty",
            )

        assert result["success"] is False
        assert result["stage"] == "extract"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_persists_skill_to_filesystem():
    """The collected skill should be written to the filesystem."""
    from app.services.skill_collection_service import SkillCollectionService
    from app.services.skill_sync import USER_SKILLS_DIR

    db = SessionLocal()
    try:
        service = SkillCollectionService(db=db)

        mock_extract_result = {
            "success": True,
            "text": "# API Integration Guide\n\nA comprehensive guide to API integration.\n\n## Overview\n\nLearn how to integrate APIs.\n\n## Steps\n\n1. Get an API key\n2. Make requests\n3. Handle errors",
            "url": "https://example.com/api-guide",
        }

        mock_llm_result = {
            "response": "{}",
            "model": "test-model",
            "usage": {},
            "data": {
                "name": "api-integration",
                "description": "A skill for API integration",
                "body": "## Overview\n\nLearn how to integrate APIs.\n\n## Steps\n\n1. Get an API key\n2. Make requests\n3. Handle errors",
            },
        }

        with patch("app.services.tool_handlers.agent_browser_tool._agent_browser", new_callable=AsyncMock, return_value=mock_extract_result):
            with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_llm_result):
                result = await service.collect_from_url(
                    url="https://example.com/api-guide",
                    skill_name="api-integration",
                )

        # Verify the skill file was written
        skill_file = USER_SKILLS_DIR / "collected" / "api-integration" / "SKILL.md"
        assert skill_file.exists(), f"SKILL.md not written to {skill_file}"

        content = skill_file.read_text()
        assert "Learn how to integrate APIs" in content
    finally:
        db.close()


def test_derive_name_from_url():
    """_derive_name_from_url should produce a valid kebab-case name."""
    from app.services.skill_collection_service import _derive_name_from_url

    assert _derive_name_from_url("https://example.com/skills/my-cool-skill") == "my-cool-skill"
    assert _derive_name_from_url("https://example.com/skills/my_cool_skill.md") == "my-cool-skill"
    assert _derive_name_from_url("https://example.com") != ""
    assert _derive_name_from_url("https://example.com/path/to/Skill-Name.html") == "skill-name"
