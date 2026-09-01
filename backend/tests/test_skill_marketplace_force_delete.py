"""Test the force-delete parameter on the delete source endpoint.

The user feedback on 2026-07-29 was: "make sure user can delete also and
add also from website". Previously the marketplace tab's Delete button
on a default (curated) source was actually a soft Hide (is_hidden=True).
The new contract:
  * Default source, no ``force``  → soft hide (reversible).
  * Default source, ``force=true`` → hard delete (CASCADE).
  * Non-default source           → always hard delete.

These tests pin the contract so a future refactor that drops the
``force`` flag or breaks the CASCADE breaks a test instead of silently
leaking rows.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.deps import get_db, get_current_user_required
from app.routers.marketplace import router as marketplace_router

_mock_user = type("U", (), {"id": "test-user"})()


def _make_client(db):
    app = FastAPI()
    app.include_router(marketplace_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: _mock_user
    return TestClient(app)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        session.query(ExternalSkill).delete()
        session.query(SkillSource).delete()
        session.commit()
        yield session
    finally:
        session.close()


def _seed_source_with_skills(db, *, is_default, skill_count=2):
    """Create a source + N skills so we can test the CASCADE behavior."""
    import uuid
    from app.models.skill_source import SkillSource
    from app.models.external_skill import ExternalSkill

    src_id = f"force-test-{uuid.uuid4()}"
    db.add(SkillSource(
        id=src_id, name="Force Test", url=f"https://example.com/{src_id}",
        source_type="web_page", is_default=is_default, is_hidden=False,
        is_deleted=False, last_sync_status="success", skill_count=skill_count,
        brand_color="#000", icon_emoji="X",
    ))
    db.commit()
    for i in range(skill_count):
        db.add(ExternalSkill(
            id=f"force-skill-{uuid.uuid4()}",
            source_id=src_id, source_url="https://example.com/skill",
            name=f"Skill {i}", display_name=f"Skill {i}",
            description="d", category="test", version="1",
            skill_md="x", install_count=0, is_deleted=False,
        ))
    db.commit()
    return src_id


class TestForceDelete:
    def test_default_source_without_force_soft_hides(self, db):
        """The default path: deleting a default source without ``force``
        sets ``is_hidden=True`` instead of removing the row. The user
        can re-show it later — reversible."""
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        src_id = _seed_source_with_skills(db, is_default=True, skill_count=2)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src_id}")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "hidden": True}

        # Source row still exists, but is_hidden=True.
        src = db.query(SkillSource).filter(SkillSource.id == src_id).first()
        assert src is not None
        assert src.is_hidden is True
        # Skills are intact.
        assert db.query(ExternalSkill).filter(ExternalSkill.source_id == src_id).count() == 2

    def test_default_source_with_force_hard_deletes(self, db):
        """The new path: deleting a default source with ``force=true``
        hard-deletes the source row AND fires the CASCADE on
        external_skills."""
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        src_id = _seed_source_with_skills(db, is_default=True, skill_count=2)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src_id}?force=true")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "deleted": True}

        # Source row is gone.
        assert db.query(SkillSource).filter(SkillSource.id == src_id).first() is None
        # CASCADE removed the skills.
        assert db.query(ExternalSkill).filter(ExternalSkill.source_id == src_id).count() == 0

    def test_non_default_source_always_hard_deletes(self, db):
        """Non-default sources ignore ``force`` — they always hard-delete."""
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        src_id = _seed_source_with_skills(db, is_default=False, skill_count=3)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src_id}")
        assert resp.status_code == 200

        assert db.query(SkillSource).filter(SkillSource.id == src_id).first() is None
        assert db.query(ExternalSkill).filter(ExternalSkill.source_id == src_id).count() == 0
