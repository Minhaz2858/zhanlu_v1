"""End-to-end test for the auto-sync-on-list behavior.

The user feedback on 2026-07-29 was: "why this card are prebuild when
user add skills source then it need to show [the skills]". The fix is
the auto-sync hook in the GET /api/marketplace/sources router — every
list call schedules a background sync for any curated source that has
never been synced.

This test exercises the full flow against a faked ``sync_source`` so
we can verify the wiring without making a real network call:

  1. After seed, every default source has ``last_sync_status="never"``.
  2. A GET /sources schedules a sync for each one.
  3. After the sync runs, ``last_sync_status`` is no longer "never".

The thread-fallback the router uses when there's no running event loop
is what makes this test work — the thread is still running when the
test asserts, so the fake ``sync_source`` is invoked and updates the
status. We poll for the state change rather than poking at the
scheduling primitives directly.
"""
import pytest
import time
import threading
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.deps import get_db, get_current_user_required
from app.routers.marketplace import router as marketplace_router

_mock_user = type("U", (), {"id": "test-user"})()


def _make_client(db):
    """Create a TestClient that shares the test's DB session."""
    app = FastAPI()
    app.include_router(marketplace_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: _mock_user
    return TestClient(app)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    # Truncate the relevant tables so each test starts with a clean
    # state. The shared test_runtime.db persists across tests, and the
    # marketplace + skills tables accumulate rows from earlier test
    # files (force-delete, CASCADE, etc.) — without this we'd see
    # polluted counts.
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


def _wait_for_threads(timeout=2.0):
    """Block until all non-daemon threads have finished, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate() if t is not threading.current_thread() and t.daemon]
        if not alive:
            return
        time.sleep(0.05)


class TestAutoSyncOnList:
    def test_list_syncs_unsynced_curated_sources(self, db):
        """The first GET /sources after seed must transition every
        default source out of 'never' status — that's the contract the
        marketplace tab depends on to populate the card skill counts
        without the user having to click Sync on every card."""
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource

        seed_curated_sources(db)
        # Sanity: at least one source in "never" state.
        unsynced = db.query(SkillSource).filter(
            SkillSource.is_default == True,
            SkillSource.last_sync_status == "never",
        ).count()
        assert unsynced >= 1

        # The fake is an async coroutine matching the real sync_source
        # signature. The router calls it via ``asyncio.run(sync_source(
        # src.id))`` in the thread fallback, so it must return a
        # coroutine to avoid "a coroutine was expected" warnings.
        async def fake_sync(source_id, db=None):
            from app.database import SessionLocal
            sess = SessionLocal()
            try:
                src = sess.query(SkillSource).filter(SkillSource.id == source_id).first()
                if src:
                    src.last_sync_status = "success"
                    src.skill_count = 7
                    sess.commit()
            finally:
                sess.close()
            return {"success": True, "skill_count": 7}

        with patch("app.services.skill_source_service.sync_source", new=fake_sync):
            client = _make_client(db)
            resp = client.get("/api/marketplace/sources")
            assert resp.status_code == 200
            # Let the thread the router spawned finish its work.
            _wait_for_threads()

        # After the thread completes, every default source that was in
        # "never" state should now be in "success" state with a count.
        remaining_unsynced = db.query(SkillSource).filter(
            SkillSource.is_default == True,
            SkillSource.last_sync_status == "never",
        ).count()
        assert remaining_unsynced == 0, (
            f"expected all default sources to be synced after list, "
            f"but {remaining_unsynced} are still in 'never' status"
        )

    def test_list_skips_already_synced_sources(self, db):
        """Once a source is synced, the list hook should leave it alone.
        This is what prevents an N+1 storm of syncs on every page load
        once the cards are populated."""
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource

        seed_curated_sources(db)
        for s in db.query(SkillSource).filter(SkillSource.is_default == True).all():
            s.last_sync_status = "success"
            s.skill_count = 5
        db.commit()
        already_synced_count = db.query(SkillSource).filter(
            SkillSource.is_default == True,
            SkillSource.last_sync_status == "success",
        ).count()

        called_with = []
        async def fake_sync(source_id, db=None):
            called_with.append(source_id)
            return {"success": True, "skill_count": 0}

        with patch("app.services.skill_source_service.sync_source", new=fake_sync):
            client = _make_client(db)
            client.get("/api/marketplace/sources")
            _wait_for_threads()

        assert called_with == [], (
            f"already-synced sources should not be re-synced on list, but {called_with} was called"
        )
        # Skill counts should be unchanged.
        for s in db.query(SkillSource).filter(SkillSource.is_default == True).all():
            assert s.skill_count == 5

    def test_list_skips_hidden_sources(self, db):
        """A hidden default source should not be re-synced on list."""
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource

        seed_curated_sources(db)
        sources = db.query(SkillSource).filter(SkillSource.is_default == True).all()
        assert len(sources) >= 2
        # Reset all to "never" so the auto-sync hook considers them
        # eligible. (Previous tests may have left them in "success".)
        for s in sources:
            s.last_sync_status = "never"
        # Hide all but the last one
        for s in sources[:-1]:
            s.is_hidden = True
        visible_id = sources[-1].id
        db.commit()

        called_with = []
        async def fake_sync(source_id, db=None):
            called_with.append(source_id)
            return {"success": True, "skill_count": 0}

        with patch("app.services.skill_source_service.sync_source", new=fake_sync):
            client = _make_client(db)
            client.get("/api/marketplace/sources")
            _wait_for_threads()

        assert called_with == [visible_id], (
            f"only the unhidden source should be synced, got: {called_with}"
        )


class TestCascadeDelete:
    """When a user deletes a skill source, every skill that came from
    that source should be deleted too. The CASCADE is set on the FK
    (``ondelete='CASCADE'``), so a single DELETE on the source row
    cleans up the entire skill graph — no need for an explicit
    ``DELETE FROM external_skills WHERE source_id=...`` in the router
    (the router still does that defensively, but this test pins the
    FK behavior so a future migration that drops the CASCADE breaks
    a test instead of silently leaking rows)."""

    def test_deleting_source_cascades_to_skills(self, db):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        import uuid

        # Create a fresh source + 2 skills (use fresh uuids so we don't
        # collide with the seeded curated sources).
        src_id = f"cascade-{uuid.uuid4()}"
        db.add(SkillSource(
            id=src_id, name="Cascade Test", url="https://example.com",
            source_type="web_page", is_default=False, is_hidden=False,
            is_deleted=False, last_sync_status="success", skill_count=2,
            brand_color="#000", icon_emoji="X",
        ))
        # Commit the source first so the FK on ExternalSkill can see it
        # (SQLite enforces FKs immediately; Postgres defers).
        db.commit()
        for i in range(2):
            db.add(ExternalSkill(
                id=f"cascade-skill-{uuid.uuid4()}",
                source_id=src_id, source_url="https://example.com/skill",
                name=f"Test Skill {i}", display_name=f"Test Skill {i}",
                description="d", category="test", version="1",
                skill_md="x", install_count=0, is_deleted=False,
            ))
        db.commit()
        assert db.query(ExternalSkill).filter(ExternalSkill.source_id == src_id).count() == 2

        # Delete the source — the FK CASCADE should remove the skills.
        db.query(SkillSource).filter(SkillSource.id == src_id).delete()
        db.commit()

        assert db.query(ExternalSkill).filter(ExternalSkill.source_id == src_id).count() == 0
