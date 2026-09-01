import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from app.database import Base, engine, SessionLocal
import app.models  # noqa

Base.metadata.create_all(engine)


def test_skill_source_model_crud():
    from app.models.skill_source import SkillSource
    db = SessionLocal()
    try:
        src = SkillSource(
            name="Test Source",
            url="https://github.com/test/repo",
            source_type="github_repo",
            description="A test source",
            is_default=False,
            last_sync_status="never",
            skill_count=0,
        )
        db.add(src)
        db.commit()
        db.refresh(src)

        assert src.id is not None
        assert src.name == "Test Source"
        assert src.source_type == "github_repo"
        assert src.is_default is False
        assert src.last_sync_status == "never"
        assert src.skill_count == 0
        assert src.created_date is not None
        assert src.org_id == "default-org"
    finally:
        db.close()


def test_external_skill_model_crud():
    from app.models.external_skill import ExternalSkill
    from app.models.skill_source import SkillSource
    db = SessionLocal()
    try:
        src = SkillSource(name="S2", url="https://example.com/index.json", source_type="web_index")
        db.add(src)
        db.commit()
        db.refresh(src)

        skill = ExternalSkill(
            source_id=src.id,
            name="my-skill",
            display_name="My Skill",
            description="A test skill",
            summary="A test skill",
            category="data",
            version="1.0.0",
            author="tester",
            skill_md="## Overview\n\nTest content",
            tags=["data", "analysis"],
            source_url="https://example.com/skills/my-skill",
            install_count=0,
        )
        db.add(skill)
        db.commit()
        db.refresh(skill)

        assert skill.id is not None
        assert skill.source_id == src.id
        assert skill.name == "my-skill"
        assert skill.tags == ["data", "analysis"]
        assert skill.skill_md.startswith("## Overview")
    finally:
        db.close()


def test_external_skill_cascade_on_source_delete():
    from app.models.skill_source import SkillSource
    from app.models.external_skill import ExternalSkill
    db = SessionLocal()
    try:
        src = SkillSource(name="S3", url="https://example.com/s3", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        skill = ExternalSkill(source_id=src.id, name="cascaded", display_name="Cascaded",
                              description="d", category="c", version="1", skill_md="x",
                              source_url="https://example.com/s3")
        db.add(skill)
        db.commit()

        db.delete(src)
        db.commit()

        remaining = db.query(ExternalSkill).filter(ExternalSkill.source_id == src.id).first()
        assert remaining is None
    finally:
        db.close()


def test_skill_source_brand_color_and_icon_emoji():
    """SkillSource accepts brand_color + icon_emoji (Browse Marketplace branding).

    Both columns are nullable — they're purely visual hints for the source
    card on the new in-page marketplace tab. The columns must round-trip
    through the DB and accept empty strings as a "no brand" signal.
    """
    from app.models.skill_source import SkillSource
    db = SessionLocal()
    try:
        src = SkillSource(
            name="Branded Source",
            url="https://example.com/branded",
            source_type="web_index",
            brand_color="#7C3AED",
            icon_emoji="★",
        )
        db.add(src)
        db.commit()
        db.refresh(src)

        assert src.brand_color == "#7C3AED"
        assert src.icon_emoji == "★"

        # Default (no brand fields) — both should be None, not crash.
        plain = SkillSource(
            name="Plain Source",
            url="https://example.com/plain",
            source_type="web_index",
        )
        db.add(plain)
        db.commit()
        db.refresh(plain)
        assert plain.brand_color is None
        assert plain.icon_emoji is None
    finally:
        db.close()


def test_seed_curated_sources_backfills_brand_fields():
    """`seed_curated_sources` backfills brand_color + icon_emoji on existing
    rows that predate the columns. New rows get explicit values.
    """
    from app.services.skill_source_service import seed_curated_sources, DEFAULT_BRAND_COLOR
    from app.models.skill_source import SkillSource
    db = SessionLocal()
    try:
        # Wipe any prior runs so the test is hermetic.
        db.query(SkillSource).delete()
        db.commit()

        # Existing "old" row with no brand fields.
        old = SkillSource(
            name="Legacy Source",
            url="https://example.com/legacy",
            source_type="web_index",
        )
        db.add(old)
        db.commit()
        db.refresh(old)
        assert old.brand_color is None and old.icon_emoji is None

        seed_curated_sources(db)
        db.refresh(old)
        # Old row was backfilled, not deleted.
        assert old.brand_color == DEFAULT_BRAND_COLOR
        assert old.icon_emoji == "L"  # first letter of "Legacy Source"

        # A new curated row was inserted with explicit brand fields.
        curated = db.query(SkillSource).filter(
            SkillSource.is_default == True,
        ).all()
        assert len(curated) >= 1
        for src in curated:
            assert src.brand_color
            assert src.icon_emoji
    finally:
        db.close()
