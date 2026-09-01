"""Tests for marketplace service — publish, browse, install, rate, validation.

Uses an in-memory SQLite database per test function so tests are isolated
and can be run in parallel.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.models.base import Base
from app.models.tool import Tool


@pytest.fixture
def db() -> Session:
    """Create an in-memory SQLite database with marketplace tables."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


VALID_SKILL_MD = """---
name: image-gen
description: Generate images from text
summary: AI image generation using MiniMax
version: "1.0.0"
category: media
tags:
  - image
  - generative
---

# Image Generation

Use this skill to generate images from text prompts.

## Usage

1. Describe what you want
2. Call mm_image_gen with your prompt
"""

MINIMAL_SKILL_MD = """---
name: test-skill
description: A test skill
---

# Test Skill

Simple test.
"""


# ─── Validation tests ───────────────────────────────────────────────────

class TestValidation:
    """Test skill_md content validation."""

    def test_valid_skill_md_passes(self):
        from app.services.marketplace import validate_skill_md
        errors = validate_skill_md(VALID_SKILL_MD)
        assert errors == []

    def test_empty_skill_md_fails(self):
        from app.services.marketplace import validate_skill_md
        errors = validate_skill_md("")
        assert len(errors) > 0

    def test_missing_frontmatter_fails(self):
        from app.services.marketplace import validate_skill_md
        errors = validate_skill_md("# No frontmatter\n\nJust some markdown")
        assert any("frontmatter" in e.lower() for e in errors)

    def test_frontmatter_missing_name(self):
        from app.services.marketplace import validate_skill_md
        md = "---\ndescription: No name field\n---\n\n# Body"
        errors = validate_skill_md(md)
        assert any("name" in e.lower() for e in errors)

    def test_frontmatter_missing_description(self):
        from app.services.marketplace import validate_skill_md
        md = "---\nname: test\n---\n\n# Body"
        errors = validate_skill_md(md)
        assert any("description" in e.lower() for e in errors)

    def test_invalid_yaml_frontmatter(self):
        from app.services.marketplace import validate_skill_md
        md = "---\nname: [unclosed\n---\n\n# Body"
        errors = validate_skill_md(md)
        assert any("yaml" in e.lower() for e in errors)

    def test_size_limit(self):
        from app.services.marketplace import validate_skill_md
        # Create content over 100KB
        body = "x" * (101 * 1024)
        md = f"---\nname: big\ndescription: Huge skill\n---\n\n{body}"
        errors = validate_skill_md(md)
        assert any("kb" in e.lower() or "100" in e for e in errors)

    def test_minimal_valid_skill(self):
        from app.services.marketplace import validate_skill_md
        errors = validate_skill_md(MINIMAL_SKILL_MD)
        assert errors == []


# ─── Signature tests ────────────────────────────────────────────────────

class TestSignature:
    """Test HMAC signing and verification."""

    def test_sign_and_verify(self):
        from app.services.marketplace import sign_skill_content, verify_skill_signature
        content = "test content"
        publisher_id = "user-123"
        sig = sign_skill_content(content, publisher_id)
        assert sig is not None
        assert len(sig) == 64  # SHA256 hex
        assert verify_skill_signature(content, publisher_id, sig)

    def test_verify_fails_with_wrong_content(self):
        from app.services.marketplace import sign_skill_content, verify_skill_signature
        sig = sign_skill_content("original", "user-1")
        assert not verify_skill_signature("tampered", "user-1", sig)

    def test_verify_fails_with_wrong_publisher(self):
        from app.services.marketplace import sign_skill_content, verify_skill_signature
        sig = sign_skill_content("content", "user-1")
        assert not verify_skill_signature("content", "user-2", sig)


# ─── Publish tests ──────────────────────────────────────────────────────

class TestPublish:
    """Test publishing skills to marketplace."""

    def test_publish_success(self, db):
        from app.services.marketplace import publish_skill
        from app.models.marketplace_skill import MarketplaceSkill

        skill = publish_skill(
            db,
            name="image-gen",
            description="Generate images",
            skill_md=VALID_SKILL_MD,
            category="media",
            publisher_name="TestUser",
        )
        assert skill.id is not None
        assert skill.name == "image-gen"
        assert skill.category == "media"
        assert skill.signature is not None
        assert skill.is_verified is False
        assert skill.download_count == 0

        # Verify persisted
        db_skill = db.query(MarketplaceSkill).filter_by(name="image-gen").first()
        assert db_skill is not None
        assert db_skill.id == skill.id

    def test_publish_duplicate_fails(self, db):
        from app.services.marketplace import publish_skill

        publish_skill(db, name="dup", description="First", skill_md=MINIMAL_SKILL_MD)
        with pytest.raises(ValueError, match="already exists"):
            publish_skill(db, name="dup", description="Second", skill_md=MINIMAL_SKILL_MD)

    def test_publish_invalid_skill_md_fails(self, db):
        from app.services.marketplace import publish_skill

        with pytest.raises(ValueError, match="empty"):
            publish_skill(db, name="bad", description="bad", skill_md="")

    def test_publish_extracts_summary(self, db):
        from app.services.marketplace import publish_skill

        skill = publish_skill(
            db,
            name="summary-test",
            description="Has summary",
            skill_md=VALID_SKILL_MD,
        )
        assert skill.summary is not None
        assert len(skill.summary) > 0


# ─── Browse tests ───────────────────────────────────────────────────────

class TestBrowse:
    """Test browsing marketplace skills."""

    def _seed(self, db):
        from app.services.marketplace import publish_skill

        publish_skill(db, name="skill-a", description="Alpha", skill_md=MINIMAL_SKILL_MD, category="cat1")
        publish_skill(db, name="skill-b", description="Beta", skill_md=MINIMAL_SKILL_MD, category="cat2")
        publish_skill(db, name="skill-c", description="Gamma alpha", skill_md=MINIMAL_SKILL_MD, category="cat1")

    def test_browse_all(self, db):
        from app.services.marketplace import browse_skills
        self._seed(db)
        result = browse_skills(db)
        assert result["total"] >= 3
        assert len(result["skills"]) >= 3

    def test_browse_filter_by_category(self, db):
        from app.services.marketplace import browse_skills
        self._seed(db)
        result = browse_skills(db, category="cat1")
        assert result["total"] == 2

    def test_browse_search_by_query(self, db):
        from app.services.marketplace import browse_skills
        self._seed(db)
        result = browse_skills(db, query="alpha")
        assert result["total"] >= 2  # skill-a and skill-c

    def test_browse_paginated(self, db):
        from app.services.marketplace import browse_skills
        self._seed(db)
        result = browse_skills(db, page=1, page_size=2)
        assert len(result["skills"]) <= 2
        assert result["total_pages"] >= 2

    def test_browse_sort_by_newest(self, db):
        from app.services.marketplace import browse_skills
        self._seed(db)
        result = browse_skills(db, sort="newest")
        assert result["total"] >= 3


# ─── Install tests ──────────────────────────────────────────────────────

class TestInstall:
    """Test installing marketplace skills."""

    def _publish(self, db):
        from app.services.marketplace import publish_skill
        return publish_skill(
            db,
            name="install-test",
            description="Test install",
            skill_md=VALID_SKILL_MD,
            category="tools",
        )

    def test_install_creates_tool(self, db):
        from app.services.marketplace import install_skill
        mskill = self._publish(db)

        tool = install_skill(db, mskill.id, user_id="user-1")
        assert tool.name == "install-test"
        assert tool.source == "marketplace_user"
        assert tool.created_by_id == "user-1"

    def test_install_increments_download_count(self, db):
        from app.services.marketplace import install_skill
        mskill = self._publish(db)

        install_skill(db, mskill.id, user_id="user-1")
        db.refresh(mskill)
        assert mskill.download_count == 1

        install_skill(db, mskill.id, user_id="user-2")
        db.refresh(mskill)
        assert mskill.download_count == 2

    def test_install_same_user_returns_existing(self, db):
        from app.services.marketplace import install_skill
        mskill = self._publish(db)

        t1 = install_skill(db, mskill.id, user_id="user-1")
        t2 = install_skill(db, mskill.id, user_id="user-1")
        assert t1.id == t2.id  # Same row returned

    def test_install_nonexistent_fails(self, db):
        from app.services.marketplace import install_skill
        with pytest.raises(ValueError, match="not found"):
            install_skill(db, "no-such-id", user_id="user-1")


# ─── Rate tests ─────────────────────────────────────────────────────────

class TestRate:
    """Test rating marketplace skills."""

    def _publish(self, db):
        from app.services.marketplace import publish_skill
        return publish_skill(
            db,
            name="rate-test",
            description="Rate this",
            skill_md=MINIMAL_SKILL_MD,
        )

    def test_rate_success(self, db):
        from app.services.marketplace import rate_skill
        mskill = self._publish(db)

        rating = rate_skill(db, mskill.id, user_id="u1", rating=4)
        assert rating.rating == 4

        db.refresh(mskill)
        assert mskill.avg_rating == 4.0
        assert mskill.ratings_count == 1

    def test_rate_updates_existing(self, db):
        from app.services.marketplace import rate_skill
        mskill = self._publish(db)

        rate_skill(db, mskill.id, user_id="u1", rating=3)
        rate_skill(db, mskill.id, user_id="u1", rating=5)

        db.refresh(mskill)
        assert mskill.avg_rating == 5.0
        assert mskill.ratings_count == 1  # Same user, updated not new

    def test_rate_multiple_users_averages(self, db):
        from app.services.marketplace import rate_skill
        mskill = self._publish(db)

        rate_skill(db, mskill.id, user_id="u1", rating=2)
        rate_skill(db, mskill.id, user_id="u2", rating=4)
        rate_skill(db, mskill.id, user_id="u3", rating=3)

        db.refresh(mskill)
        assert mskill.avg_rating == 3.0
        assert mskill.ratings_count == 3

    def test_rate_out_of_range_fails(self, db):
        from app.services.marketplace import rate_skill
        mskill = self._publish(db)

        with pytest.raises(ValueError, match="between 1 and 5"):
            rate_skill(db, mskill.id, user_id="u1", rating=0)

        with pytest.raises(ValueError, match="between 1 and 5"):
            rate_skill(db, mskill.id, user_id="u1", rating=6)


# ─── Feature: get_skill_ratings ─────────────────────────────────────────

class TestGetRatings:
    """Test fetching paginated ratings."""

    def test_get_ratings_empty(self, db):
        from app.services.marketplace import publish_skill, get_skill_ratings
        mskill = publish_skill(db, name="nr", description="No ratings", skill_md=MINIMAL_SKILL_MD)
        result = get_skill_ratings(db, mskill.id)
        assert result["total"] == 0
        assert result["ratings"] == []

    def test_get_ratings_paginated(self, db):
        from app.services.marketplace import publish_skill, rate_skill, get_skill_ratings
        mskill = publish_skill(db, name="many", description="Many ratings", skill_md=MINIMAL_SKILL_MD)
        for i in range(5):
            rate_skill(db, mskill.id, user_id=f"u{i}", rating=4)

        result = get_skill_ratings(db, mskill.id, page=1, page_size=3)
        assert result["total"] == 5
        assert len(result["ratings"]) == 3
