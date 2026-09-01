import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import uuid
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.routers.marketplace import router as marketplace_router
import app.models  # noqa

Base.metadata.create_all(engine)

_mock_user = type("U", (), {"id": "test-user"})()


def _make_client(db):
    """Create a TestClient that shares the test's DB session."""
    app = FastAPI()
    # Match the real app's `app.include_router(marketplace_router, prefix="/api")`
    # in `main.py` — without the prefix the test would hit a 404.
    app.include_router(marketplace_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: _mock_user
    return TestClient(app)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def isolated_skills(monkeypatch):
    temp_dir = Path(tempfile.mkdtemp(prefix="api_test_"))
    import app.services.skill_sync as ss
    monkeypatch.setattr(ss, "USER_SKILLS_DIR", temp_dir)
    import app.services.skills_loader as sl
    monkeypatch.setattr(sl, "_registry", None)
    monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


class TestSources:
    def test_list_sources_seeds_curated(self, db):
        client = _make_client(db)
        resp = client.get("/api/marketplace/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert len(data["sources"]) >= 1

    def test_add_source(self, db):
        client = _make_client(db)
        resp = client.post("/api/marketplace/sources", json={
            "url": f"https://github.com/test/new-repo-{uuid.uuid4().hex[:6]}",
            "name": "New Repo",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["source_type"] == "github_repo"

    def test_delete_user_source(self, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="ToDelete", url=f"https://example.com/del-{uuid.uuid4().hex[:6]}", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200

    def test_delete_default_source_hides(self, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="Default", url=f"https://example.com/default-{uuid.uuid4().hex[:6]}", source_type="github_repo", is_default=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200
        db.refresh(src)
        assert src.is_hidden is True

    def test_delete_default_source_with_force_hard_deletes(self, db):
        """``?force=true`` upgrades the soft-hide to a hard delete for
        default sources. The marketplace tab exposes this via a
        separate "Delete" button next to "Hide" on curated sources —
        see test_skill_marketplace_force_delete.py for the full
        CASCADE + skill-removal assertions."""
        from app.models.skill_source import SkillSource
        src = SkillSource(name="ForceDelete", url=f"https://example.com/force-{uuid.uuid4().hex[:6]}", source_type="github_repo", is_default=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}?force=true")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "deleted": True}
        # Source is gone, not just hidden.
        assert db.query(SkillSource).filter(SkillSource.id == src.id).first() is None

    def test_sync_source(self, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="Sync", url=f"https://example.com/sync-{uuid.uuid4().hex[:6]}", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        with patch("app.services.skill_source_service.sync_source", new_callable=AsyncMock, return_value={"success": True, "skill_count": 3, "error": None}):
            client = _make_client(db)
            resp = client.post(f"/api/marketplace/sources/{src.id}/sync")
        assert resp.status_code == 202

    def test_list_source_skills(self, db):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        src = SkillSource(name="WithSkills", url=f"https://example.com/ws-{uuid.uuid4().hex[:6]}", source_type="web_index")
        db.add(src)
        db.commit()
        db.refresh(src)
        db.add(ExternalSkill(source_id=src.id, name="sk1", display_name="SK1", description="d", category="c", version="1", skill_md="x", source_url="https://example.com"))
        db.commit()
        client = _make_client(db)
        resp = client.get(f"/api/marketplace/sources/{src.id}/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["skills"][0]["name"] == "sk1"


class TestInstall:
    def test_install_skill(self, db, isolated_skills):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        src = SkillSource(name="Install", url=f"https://example.com/install-{uuid.uuid4().hex[:6]}", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        skill = ExternalSkill(source_id=src.id, name=f"installable-{uuid.uuid4().hex[:6]}", display_name="Installable",
                              description="d", category="tools", version="1.0.0", skill_md="## Overview\n\nBody",
                              source_url="https://example.com")
        db.add(skill)
        db.commit()
        db.refresh(skill)
        client = _make_client(db)
        resp = client.post(f"/api/marketplace/skills/{skill.id}/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["skill_name"] == skill.name

    def test_install_idempotent(self, db, isolated_skills):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        src = SkillSource(name="Idem", url=f"https://example.com/idem-{uuid.uuid4().hex[:6]}", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        skill_name = f"idem-skill-{uuid.uuid4().hex[:6]}"
        skill = ExternalSkill(source_id=src.id, name=skill_name, display_name="Idem",
                              description="d", category="tools", version="1.0.0", skill_md="## Overview\n\nBody",
                              source_url="https://example.com")
        db.add(skill)
        db.commit()
        db.refresh(skill)
        client = _make_client(db)
        r1 = client.post(f"/api/marketplace/skills/{skill.id}/install")
        r2 = client.post(f"/api/marketplace/skills/{skill.id}/install")
        assert r1.json()["already_installed"] is False
        assert r2.json()["already_installed"] is True


class TestMySkills:
    def test_list_my_skills(self, db):
        from app.models.tool import Tool
        db.add(Tool(name=f"my-installed-{uuid.uuid4().hex[:6]}", kind="system_skill", source="external", created_by_id="test-user"))
        db.commit()
        client = _make_client(db)
        resp = client.get("/api/marketplace/my-skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) >= 1


class TestSourceBranding:
    """The new `brand_color` + `icon_emoji` fields power the source cards
    on the Browse Marketplace tab. Verify they round-trip through the API.
    """

    def test_list_sources_returns_brand_fields(self, db):
        client = _make_client(db)
        resp = client.get("/api/marketplace/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sources"]
        for src in data["sources"]:
            # Both fields are always present in the response (may be None for
            # legacy rows that predate the columns — the UI handles that).
            assert "brand_color" in src
            assert "icon_emoji" in src

    def test_add_source_accepts_brand_color_and_emoji(self, db):
        client = _make_client(db)
        url = f"https://github.com/test/branded-{uuid.uuid4().hex[:6]}"
        resp = client.post("/api/marketplace/sources", json={
            "url": url,
            "name": "Branded",
            "brand_color": "#7C3AED",
            "icon_emoji": "★",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["brand_color"] == "#7C3AED"
        assert data["icon_emoji"] == "★"

    def test_add_source_defaults_when_brand_omitted(self, db):
        client = _make_client(db)
        url = f"https://github.com/test/defaults-{uuid.uuid4().hex[:6]}"
        resp = client.post("/api/marketplace/sources", json={
            "url": url,
            "name": "Default",
        })
        assert resp.status_code == 201
        data = resp.json()
        # Default brand_color is the neutral gray; default icon_emoji falls
        # back to the first letter of the source name.
        assert data["brand_color"] is not None
        assert data["icon_emoji"] == "D"
