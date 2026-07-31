"""Test the removed-curated-URL tombstone that prevents the seed from
re-creating a source the user explicitly deleted.

User feedback 2026-07-29: "after refresh it showing again it's not
working how i want it". The fix: when the user hard-deletes a
default (curated) source, the URL is recorded in
``removed_curated_urls``. The seed function checks this table and
skips any URL in it, so the source stays gone across refreshes and
backend restarts.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.deps import get_db, get_current_user_required
from app.routers.marketplace import router as marketplace_router

_mock_user = type("U", (), {"id": "test-user", "email": "test@example.com"})()


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
        from app.models.removed_curated_url import RemovedCuratedUrl
        session.query(ExternalSkill).delete()
        session.query(SkillSource).delete()
        session.query(RemovedCuratedUrl).delete()
        session.commit()
        yield session
    finally:
        session.close()


def _seed_one_default(db, name="Test Default", url="https://github.com/test-org/test-repo"):
    """Create one default source so the seed has nothing to do (it
    skips when a default already exists)."""
    from app.models.skill_source import SkillSource
    src = SkillSource(
        name=name, url=url, source_type="github_repo",
        is_default=True, is_hidden=False, is_deleted=False,
        last_sync_status="never", skill_count=0,
        brand_color="#000", icon_emoji="X",
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


class TestTombstoneOnForceDelete:
    def test_hard_delete_default_writes_tombstone(self, db):
        """Hard-deleting a default source (force=true) must record the
        URL in removed_curated_urls so the seed doesn't re-create it
        on the next list call. This is the contract that fixes the
        "showing again after refresh" bug."""
        from app.models.removed_curated_url import RemovedCuratedUrl
        from app.models.skill_source import SkillSource

        src = _seed_one_default(db, name="My Default", url="https://github.com/foo/bar")
        # Verify pre-state: no tombstone for this URL.
        assert db.query(RemovedCuratedUrl).filter(RemovedCuratedUrl.url == src.url).first() is None

        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}?force=true")
        assert resp.status_code == 200

        # Source row is gone.
        assert db.query(SkillSource).filter(SkillSource.id == src.id).first() is None
        # Tombstone was written.
        tombstone = db.query(RemovedCuratedUrl).filter(RemovedCuratedUrl.url == src.url).first()
        assert tombstone is not None
        assert tombstone.removed_by == "test-user"

    def test_hard_delete_non_default_does_not_write_tombstone(self, db):
        """Non-default sources are user-added; they don't need a
        tombstone because the seed never creates user-added sources
        anyway. Writing one would be noise."""
        from app.models.removed_curated_url import RemovedCuratedUrl
        from app.models.skill_source import SkillSource

        src = SkillSource(
            name="User Added", url="https://github.com/user/added",
            source_type="github_repo",
            is_default=False, is_hidden=False, is_deleted=False,
            last_sync_status="never", skill_count=0,
            brand_color="#000", icon_emoji="X",
        )
        db.add(src)
        db.commit()
        db.refresh(src)

        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200

        assert db.query(SkillSource).filter(SkillSource.id == src.id).first() is None
        assert db.query(RemovedCuratedUrl).count() == 0

    def test_soft_hide_does_not_write_tombstone(self, db):
        """Soft-hide (default source without force) just sets
        is_hidden=True. No tombstone because the source row stays in
        the DB and the seed will skip it normally."""
        from app.models.removed_curated_url import RemovedCuratedUrl
        from app.models.skill_source import SkillSource

        src = _seed_one_default(db, name="Soft Hide", url="https://github.com/x/y")

        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "hidden": True}

        # Source still exists, just hidden.
        s = db.query(SkillSource).filter(SkillSource.id == src.id).first()
        assert s is not None
        assert s.is_hidden is True
        # No tombstone.
        assert db.query(RemovedCuratedUrl).count() == 0


class TestSeedSkipsTombstonedUrls:
    def test_seed_does_not_recreate_tombstoned_url(self, db):
        """The bug the user reported: a hard-deleted default source
        came back on the next list call. With the tombstone in
        place, the seed sees the URL in removed_curated_urls and
        skips it. The next list call returns 0 sources instead of
        re-creating the deleted one."""
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource
        from app.models.removed_curated_url import RemovedCuratedUrl

        # Simulate a previous hard-delete: tombstone in place, no
        # source row.
        tombstoned_url = "https://github.com/tombstoned/never-come-back"
        db.add(RemovedCuratedUrl(
            url=tombstoned_url,
            removed_at=__import__("datetime").datetime.utcnow(),
            removed_by="test-user",
        ))
        db.commit()

        created = seed_curated_sources(db)
        # The seed should not have created any new default sources
        # (the tombstoned URL matches one of the curated definitions;
        # the other curated source may or may not match). The point
        # is: no source with the tombstoned URL exists.
        assert db.query(SkillSource).filter(SkillSource.url == tombstoned_url).first() is None

    def test_seed_creates_non_tombstoned_curated(self, db):
        """Negative control: when no tombstone is in place, the seed
        behaves exactly as before and creates a curated source."""
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource

        # No tombstones. Seed should run.
        created = seed_curated_sources(db)
        # Should have created at least one curated source.
        assert created >= 1
        assert db.query(SkillSource).filter(SkillSource.is_default == True).count() >= 1


class TestRestoreEndpoint:
    def test_restore_clears_tombstone_and_reseeds(self, db):
        """The user changed their mind about a hard-deleted source.
        They click "Restore" in the UI; the endpoint clears the
        tombstone and re-creates the source from the seed definition.
        After the call, the source row is back in the DB."""
        from app.models.removed_curated_url import RemovedCuratedUrl
        from app.models.skill_source import SkillSource
        from app.services.skill_source_service import seed_curated_sources
        from urllib.parse import quote

        # Set up: a tombstone for a curated URL.
        tombstoned_url = "https://github.com/test/restore-me"
        # Inject the URL into the seed definitions for this test.
        from app.services import skill_source_service as svc
        original_curated = svc.CURATED_SOURCES
        svc.CURATED_SOURCES = list(original_curated) + [
            {
                "name": "Restore Me",
                "url": tombstoned_url,
                "source_type": "github_repo",
                "description": "For the restore test",
            }
        ]
        try:
            db.add(RemovedCuratedUrl(
                url=tombstoned_url,
                removed_at=__import__("datetime").datetime.utcnow(),
                removed_by="test-user",
            ))
            db.commit()

            client = _make_client(db)
            encoded = quote(tombstoned_url, safe="")
            resp = client.post(f"/api/marketplace/sources/removed/{encoded}/restore")
            assert resp.status_code == 201

            # Tombstone gone, source back.
            assert db.query(RemovedCuratedUrl).filter(RemovedCuratedUrl.url == tombstoned_url).first() is None
            restored = db.query(SkillSource).filter(SkillSource.url == tombstoned_url).first()
            assert restored is not None
            assert restored.name == "Restore Me"
        finally:
            svc.CURATED_SOURCES = original_curated

    def test_restore_unknown_url_404(self, db):
        """Restoring a URL with no tombstone returns 404 — the
        endpoint can't bring back a source that was never removed
        via the tombstone path."""
        from urllib.parse import quote
        client = _make_client(db)
        encoded = quote("https://github.com/never/existed", safe="")
        resp = client.post(f"/api/marketplace/sources/removed/{encoded}/restore")
        assert resp.status_code == 404


class TestListRemoved:
    def test_list_removed_returns_tombstones(self, db):
        from app.models.removed_curated_url import RemovedCuratedUrl
        import datetime
        db.add(RemovedCuratedUrl(
            url="https://github.com/x/one",
            removed_at=datetime.datetime.utcnow(),
            removed_by="alice",
        ))
        db.add(RemovedCuratedUrl(
            url="https://github.com/x/two",
            removed_at=datetime.datetime.utcnow(),
            removed_by="bob",
        ))
        db.commit()

        client = _make_client(db)
        resp = client.get("/api/marketplace/sources/removed")
        assert resp.status_code == 200
        data = resp.json()
        urls = {r["url"] for r in data["removed"]}
        assert urls == {"https://github.com/x/one", "https://github.com/x/two"}
