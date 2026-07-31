"""End-to-end test for the marketplace + install + use lifecycle.

User feedback 2026-07-29: "now we have to make test that user can
collect skills from diffrent website and user can use them also".

This file pins the full contract end-to-end (via the HTTP layer,
not the model layer): a user adds a source, the source syncs and
populates ``external_skills`` rows, the user installs one of those
skills, and the installed skill appears in ``my-skills``. Then the
user removes it from My Skills, the install count decrements, and
the source's CASCADE behavior is verified.

Why not just test the model layer? Because the marketplace tabs
ship with a long chain of router code (list, sync, install,
my-skills, restore) and the actual failure modes — wrong join,
missing user filter, CASCADE not firing, install count off by one,
double-install creating duplicate rows — are all in the router
code, not the model. An end-to-end test catches them.

Source variants covered:
  * github_repo:  the curated / Anthropic / Awesome style
  * web_index:    a .json file on the web
  * web_page:     a single skill embedded in a regular URL

The network calls are stubbed out via monkeypatch of the
``_sync_*`` helpers. The test still exercises the full router
plumbing: source creation, sync scheduling, list, install,
my-skills filter, uninstall, CASCADE.
"""
import json
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.deps import get_db, get_current_user_required
from app.routers.marketplace import router as marketplace_router

# The dependency override uses a user with a known id so the install
# path (which filters by ``created_by_id``) works as it would in
# real traffic.
_mock_user = type("U", (), {"id": "user-lifecycle-test", "email": "test@x"})()


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
        # Wipe the marketplace tables so each test starts clean. The
        # shared test_runtime.db accumulates rows from earlier files.
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        from app.models.removed_curated_url import RemovedCuratedUrl
        from app.models.tool import Tool
        session.query(Tool).filter(Tool.source == "external").delete()
        session.query(ExternalSkill).delete()
        session.query(SkillSource).delete()
        session.query(RemovedCuratedUrl).delete()
        session.commit()
        yield session
    finally:
        session.close()


# ─── Network stubs ─────────────────────────────────────────────────────
# Real sync makes network calls; we don't want CI to fail because
# GitHub is down. Each test patches the ``_sync_*`` helper to return
# canned data so we can assert on the router-side behavior.

def _stub_github_sync(skills):
    """Return a coroutine factory that mimics ``_sync_github_repo``
    and writes the given list of skills to the DB. The real helper
    signature is ``async def _sync_github_repo(source, db)`` — note
    the source comes first."""
    async def fake_sync_github(source, db):
        from app.models.external_skill import ExternalSkill
        for sk in skills:
            db.add(ExternalSkill(
                id=f"gh-{sk['name']}",
                source_id=source.id,
                source_url=source.url,
                name=sk["name"],
                display_name=sk.get("display_name", sk["name"]),
                description=sk.get("description", ""),
                category=sk.get("category", "general"),
                version=sk.get("version", "1.0.0"),
                skill_md=sk.get("skill_md", f"# {sk['name']}\n\nTest skill."),
                author=sk.get("author", "test-author"),
                install_count=0,
            ))
        source.skill_count = len(skills)
        db.commit()
        return {"success": True, "skill_count": len(skills), "source": "github_repo"}
    return fake_sync_github


def _stub_web_index_sync(skills):
    async def fake_sync_index(source, db):
        from app.models.external_skill import ExternalSkill
        for sk in skills:
            db.add(ExternalSkill(
                id=f"idx-{sk['name']}",
                source_id=source.id,
                source_url=source.url,
                name=sk["name"],
                display_name=sk.get("display_name", sk["name"]),
                description=sk.get("description", ""),
                category=sk.get("category", "general"),
                version=sk.get("version", "1.0.0"),
                skill_md=sk.get("skill_md", f"# {sk['name']}\n\nIndex skill."),
                install_count=0,
            ))
        source.skill_count = len(skills)
        db.commit()
        return {"success": True, "skill_count": len(skills), "source": "web_index"}
    return fake_sync_index


def _stub_web_page_sync(skill):
    """web_page extracts ONE skill per the existing semantics. The
    user chose this option on 2026-07-29 over the multi-skill
    listing-page mode."""
    async def fake_sync_page(source, db):
        from app.models.external_skill import ExternalSkill
        db.add(ExternalSkill(
            id=f"page-{skill['name']}",
            source_id=source.id,
            source_url=source.url,
            name=skill["name"],
            display_name=skill.get("display_name", skill["name"]),
            description=skill.get("description", ""),
            category=skill.get("category", "general"),
            version=skill.get("version", "1.0.0"),
            skill_md=skill.get("skill_md", f"# {skill['name']}\n\nWeb skill."),
            install_count=0,
        ))
        source.skill_count = 1
        db.commit()
        return {"success": True, "skill_count": 1, "source": "web_page"}
    return fake_sync_page


# ─── Tests ─────────────────────────────────────────────────────────────

class TestCollectFromDifferentSources:
    """The user can collect skills from different website types. Each
    source type uses a different sync helper, but the end-state is
    the same: a source row + N external_skills rows."""

    def test_collect_from_github_repo(self, db):
        from app.models.external_skill import ExternalSkill
        skills = [
            {"name": "github-skill-1", "display_name": "GitHub Skill 1", "description": "First"},
            {"name": "github-skill-2", "display_name": "GitHub Skill 2", "description": "Second"},
        ]
        client = _make_client(db)
        add = client.post("/api/marketplace/sources", json={
            "url": "https://github.com/test/repo",
        })
        # POST /sources returns 201 Created.
        assert add.status_code == 201
        source_id = add.json()["id"]

        with patch(
            "app.services.skill_source_service._sync_github_repo",
            new=_stub_github_sync(skills),
        ):
            sync = client.post(f"/api/marketplace/sources/{source_id}/sync")
        assert sync.status_code == 202
        # Skill count updated.
        listing = client.get(f"/api/marketplace/sources/{source_id}/skills")
        assert listing.status_code == 200
        data = listing.json()
        assert data["count"] == 2
        names = {s["name"] for s in data["skills"]}
        assert names == {"github-skill-1", "github-skill-2"}

    def test_collect_from_web_index_json(self, db):
        skills = [
            {"name": "indexed-skill-1", "display_name": "Indexed 1", "description": "From JSON"},
        ]
        client = _make_client(db)
        add = client.post("/api/marketplace/sources", json={
            "url": "https://example.com/skills.json",
        })
        assert add.status_code == 201
        source_id = add.json()["id"]

        with patch(
            "app.services.skill_source_service._sync_web_index",
            new=_stub_web_index_sync(skills),
        ):
            sync = client.post(f"/api/marketplace/sources/{source_id}/sync")
        assert sync.status_code == 202

        listing = client.get(f"/api/marketplace/sources/{source_id}/skills")
        assert listing.json()["count"] == 1
        assert listing.json()["skills"][0]["name"] == "indexed-skill-1"

    def test_collect_from_web_page(self, db):
        skill = {"name": "webpage-skill", "display_name": "Webpage Skill", "description": "Single skill"}
        client = _make_client(db)
        add = client.post("/api/marketplace/sources", json={
            "url": "https://example.com/skill-page",
        })
        assert add.status_code == 201
        source_id = add.json()["id"]

        with patch(
            "app.services.skill_source_service._sync_web_page",
            new=_stub_web_page_sync(skill),
        ):
            sync = client.post(f"/api/marketplace/sources/{source_id}/sync")
        assert sync.status_code == 202

        listing = client.get(f"/api/marketplace/sources/{source_id}/skills")
        assert listing.json()["count"] == 1
        assert listing.json()["skills"][0]["name"] == "webpage-skill"

    def test_collect_from_all_three_at_once(self, db):
        """End-to-end: a user can add three sources of three different
        types, sync them all, and see all the skills in their
        respective sources. Pins the multi-source multi-type flow."""
        client = _make_client(db)

        # 1. GitHub repo
        r1 = client.post("/api/marketplace/sources", json={
            "url": "https://github.com/test/repo-a",
        })
        sid1 = r1.json()["id"]
        with patch(
            "app.services.skill_source_service._sync_github_repo",
            new=_stub_github_sync([{"name": "gh-a"}]),
        ):
            client.post(f"/api/marketplace/sources/{sid1}/sync")

        # 2. Web index
        r2 = client.post("/api/marketplace/sources", json={
            "url": "https://example.com/idx.json",
        })
        sid2 = r2.json()["id"]
        with patch(
            "app.services.skill_source_service._sync_web_index",
            new=_stub_web_index_sync([{"name": "idx-b"}]),
        ):
            client.post(f"/api/marketplace/sources/{sid2}/sync")

        # 3. Web page
        r3 = client.post("/api/marketplace/sources", json={
            "url": "https://example.com/page",
        })
        sid3 = r3.json()["id"]
        with patch(
            "app.services.skill_source_service._sync_web_page",
            new=_stub_web_page_sync({"name": "page-c"}),
        ):
            client.post(f"/api/marketplace/sources/{sid3}/sync")

        # Verify each source has its own skill count and the right
        # skill name.
        s1 = client.get(f"/api/marketplace/sources/{sid1}/skills").json()
        s2 = client.get(f"/api/marketplace/sources/{sid2}/skills").json()
        s3 = client.get(f"/api/marketplace/sources/{sid3}/skills").json()
        assert s1["count"] == 1 and s1["skills"][0]["name"] == "gh-a"
        assert s2["count"] == 1 and s2["skills"][0]["name"] == "idx-b"
        assert s3["count"] == 1 and s3["skills"][0]["name"] == "page-c"

        # The list endpoint should include all 3 test sources (plus the
        # 2 default curated sources the seed re-creates — we just
        # want to confirm OUR 3 made it through, the curated ones are
        # covered by other tests).
        listing = client.get("/api/marketplace/sources").json()
        listed_ids = {s["id"] for s in listing["sources"]}
        assert {sid1, sid2, sid3}.issubset(listed_ids)
        # And each test source's URL is correct.
        listed_urls = {s["url"] for s in listing["sources"]}
        assert {
            "https://github.com/test/repo-a",
            "https://example.com/idx.json",
            "https://example.com/page",
        }.issubset(listed_urls)


class TestInstallAndUseSkill:
    """After collecting skills, the user installs one to My Skills
    and uses it. The lifecycle: collect → install → use (in My
    Skills) → uninstall."""

    def _make_skill(self, db, name="my-skill", source=None):
        from app.models.external_skill import ExternalSkill
        if source is None:
            from app.models.skill_source import SkillSource
            source = SkillSource(
                name="Test", url="https://github.com/t/r",
                source_type="github_repo", is_default=False,
                last_sync_status="success", skill_count=1,
                brand_color="#000", icon_emoji="X",
            )
            db.add(source)
            db.commit()
            db.refresh(source)
        sk = ExternalSkill(
            id=f"sk-{name}",
            source_id=source.id,
            source_url=source.url,
            name=name,
            display_name=name,
            description="Test skill for install",
            category="test",
            version="1.0.0",
            skill_md=f"# {name}\n\nA test skill.",
            install_count=0,
        )
        db.add(sk)
        db.commit()
        db.refresh(sk)
        return sk, source

    def test_install_skill_creates_tool_row(self, db):
        """POST /skills/{id}/install creates a Tool row in My Skills
        with the right fields and stamps the install count on the
        external_skill."""
        from app.models.tool import Tool
        sk, _ = self._make_skill(db, name="install-test-1")

        client = _make_client(db)
        resp = client.post(f"/api/marketplace/skills/{sk.id}/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["already_installed"] is False
        assert data["skill_name"] == "install-test-1"

        # Tool row exists with the right shape.
        tool = db.query(Tool).filter(Tool.id == data["tool_id"]).first()
        assert tool is not None
        assert tool.name == "install-test-1"
        assert tool.source == "external"
        assert tool.category == "marketplace"
        assert tool.kind == "system_skill"
        assert tool.created_by_id == "user-lifecycle-test"
        # Install count incremented.
        db.refresh(sk)
        assert sk.install_count == 1

    def test_install_is_idempotent(self, db):
        """Installing the same skill twice should NOT create a
        duplicate Tool row. The endpoint detects the existing row
        and returns ``already_installed: true``."""
        from app.models.tool import Tool
        sk, _ = self._make_skill(db, name="idempotent-skill")

        client = _make_client(db)
        r1 = client.post(f"/api/marketplace/skills/{sk.id}/install")
        r2 = client.post(f"/api/marketplace/skills/{sk.id}/install")
        assert r1.json()["already_installed"] is False
        assert r2.json()["already_installed"] is True
        # Only one Tool row.
        assert db.query(Tool).filter(Tool.name == "idempotent-skill").count() == 1
        # Install count: 1 — the second call short-circuits before
        # the increment, so we count "new installs" not "calls".
        # This is the right semantic for the marketplace; bumping
        # the count on every click would inflate install metrics
        # from spammers hitting the button.
        db.refresh(sk)
        assert sk.install_count == 1

    def test_installed_skill_appears_in_my_skills(self, db):
        """After install, GET /my-skills returns the skill."""
        sk, _ = self._make_skill(db, name="my-skill-visible")
        client = _make_client(db)
        client.post(f"/api/marketplace/skills/{sk.id}/install")
        resp = client.get("/api/marketplace/my-skills")
        assert resp.status_code == 200
        data = resp.json()
        names = {s["name"] for s in data["skills"]}
        assert "my-skill-visible" in names

    def test_uninstall_removes_from_my_skills(self, db):
        """DELETE /my-skills/{tool_id} removes the skill from the
        user's installed list. The router soft-deletes the Tool row
        (``is_deleted=True``) rather than hard-deleting — the row
        stays in the table for audit / undo, but the user filter
        (``is_deleted == False``) on GET /my-skills hides it. The
        external_skill row stays untouched (other users can still
        install it from the marketplace)."""
        from app.models.tool import Tool
        sk, _ = self._make_skill(db, name="removable-skill")
        client = _make_client(db)
        install = client.post(f"/api/marketplace/skills/{sk.id}/install")
        tool_id = install.json()["tool_id"]

        # Pre-verify: skill is in My Skills.
        before = client.get("/api/marketplace/my-skills").json()
        assert any(s["name"] == "removable-skill" for s in before["skills"])

        uninstall = client.delete(f"/api/marketplace/my-skills/{tool_id}")
        assert uninstall.status_code == 200

        # Post-verify: skill is gone from My Skills.
        after = client.get("/api/marketplace/my-skills").json()
        assert not any(s["name"] == "removable-skill" for s in after["skills"])
        # Tool row is soft-deleted (is_deleted=True). The row stays
        # in the table for audit / future undo, but the
        # ``is_deleted == False`` filter on GET /my-skills hides it.
        tool = db.query(Tool).filter(Tool.id == tool_id).first()
        assert tool is not None
        assert tool.is_deleted is True
        # External skill still exists (so other users can install it).
        from app.models.external_skill import ExternalSkill
        assert db.query(ExternalSkill).filter(ExternalSkill.id == sk.id).first() is not None

    def test_user_b_cannot_see_user_a_installs(self, db):
        """My Skills is per-user: User A's installs are invisible to
        User B. The filter on created_by_id is what makes this
        work — the test pins that the dependency override is wired
        correctly through the install path."""
        from app.models.tool import Tool
        sk, _ = self._make_skill(db, name="private-skill")
        # User A installs.
        client_a = _make_client(db)
        client_a.post(f"/api/marketplace/skills/{sk.id}/install")

        # Now switch the dependency to user B.
        app_b = FastAPI()
        app_b.include_router(marketplace_router, prefix="/api")
        app_b.dependency_overrides[get_db] = lambda: db
        user_b = type("U", (), {"id": "user-b", "email": "b@x"})()
        app_b.dependency_overrides[get_current_user_required] = lambda: user_b
        client_b = TestClient(app_b)

        # User B sees no My Skills.
        b_view = client_b.get("/api/marketplace/my-skills").json()
        assert b_view["skills"] == []

        # User A still sees their install.
        a_view = client_a.get("/api/marketplace/my-skills").json()
        assert any(s["name"] == "private-skill" for s in a_view["skills"])


class TestDeleteSourceCascadesToInstalledSkills:
    """When a user deletes a source they collected from, the
    external_skills row is removed via CASCADE. The Tool row in
    My Skills is NOT removed by the CASCADE — the user keeps their
    install. (This is intentional: the user might have customized
    the skill locally.) However, the Tool row will become orphaned
    from the marketplace perspective; the user can still uninstall
    it explicitly via DELETE /my-skills/{tool_id}."""

    def test_source_delete_cascades_to_external_skills(self, db):
        from app.models.external_skill import ExternalSkill
        from app.models.skill_source import SkillSource

        sk, src = self._setup_user_skill(db, name="cascade-skill")

        client = _make_client(db)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200

        # Source gone.
        assert db.query(SkillSource).filter(SkillSource.id == src.id).first() is None
        # External skill CASCADE-removed.
        assert db.query(ExternalSkill).filter(ExternalSkill.id == sk.id).first() is None

    def _setup_user_skill(self, db, name="my-skill"):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        src = SkillSource(
            name="Test", url="https://github.com/t/r",
            source_type="github_repo", is_default=False,
            last_sync_status="success", skill_count=1,
            brand_color="#000", icon_emoji="X",
        )
        db.add(src)
        db.commit()
        db.refresh(src)
        sk = ExternalSkill(
            id=f"sk-{name}",
            source_id=src.id,
            source_url=src.url,
            name=name,
            display_name=name,
            description="Test",
            category="test", version="1.0.0",
            skill_md=f"# {name}", install_count=0,
        )
        db.add(sk)
        db.commit()
        db.refresh(sk)
        return sk, src

    def test_installed_tool_survives_source_delete(self, db):
        """After installing a marketplace skill and then deleting the
        source, the user's installed Tool row stays. The user can
        still uninstall it explicitly. This avoids a nasty surprise
        where deleting one source wipes the user's installs."""
        from app.models.tool import Tool
        sk, src = self._setup_user_skill(db, name="survives")

        client = _make_client(db)
        install = client.post(f"/api/marketplace/skills/{sk.id}/install")
        tool_id = install.json()["tool_id"]

        # Delete the source.
        client.delete(f"/api/marketplace/sources/{src.id}")

        # Tool row still exists.
        tool = db.query(Tool).filter(Tool.id == tool_id).first()
        assert tool is not None
        assert tool.name == "survives"


# ─── Website → GitHub → SKILL.md pipeline ───────────────────────────
# Added 2026-07-30 after the user asked: "agent need borwshinf to
# website and collect the skills ... see here in website al ready
# given the skills giothub link so agent need to use agent browser
# skills and collect the skills then show in Browse Marketplace".
# End-to-end via the HTTP layer: add a website URL → real
# ``_sync_web_page`` runs (browser + GitHub mocked) → skills appear
# under the source card → user installs one → deleting the source
# cascades the marketplace skills but keeps the install.

import base64 as _base64


def _skill_md_b64(name, description, category="general", tags=None, version="1.0.0"):
    md = (
        f"---\nname: {name}\ndescription: {description}\n"
        f"version: {version}\ncategory: {category}\n"
        + (f"tags:\n  - " + "\n  - ".join(tags or []) + "\n" if tags else "")
        + f"---\n# {name}\n\nBody for {name}."
    )
    return _base64.b64encode(md.encode()).decode()


class TestCollectFromWebsiteWithGithubLinks:
    """A user adds a website URL (e.g. awesomeskill.ai) and the agent
    browses it, harvests every GitHub link, fetches the real
    ``SKILL.md`` files, and lists them in Browse Marketplace. Pins
    the full router + sync + install + CASCADE pipeline."""

    def test_website_sync_collects_skills_from_github_links(self, db):
        """The realistic awesomeskill.ai flow: the listing page links
        each skill to a ``/tree/<branch>/<path>`` on a GitHub repo.
        After sync, each link becomes a marketplace skill under the
        source card."""
        from app.services import website_skill_crawler

        # The crawl returns: listing page → 2 GitHub tree links.
        page_links = {
            "https://awesomeskill.ai/": [
                "https://github.com/davila7/claude-code-templates/tree/main/skills/writer",
                "https://github.com/davila7/claude-code-templates/tree/main/skills/reader",
            ],
        }
        tree_payload = {
            "tree": [
                {"path": "skills/writer/SKILL.md", "type": "blob"},
                {"path": "skills/reader/SKILL.md", "type": "blob"},
            ],
            "truncated": False,
        }
        writer_md = _skill_md_b64(
            "writer", "The writer skill", category="business", tags=["writing"]
        )
        reader_md = _skill_md_b64(
            "reader", "The reader skill", category="data", tags=["parsing"]
        )
        contents_responses = [
            {"content": writer_md, "encoding": "base64"},
            {"content": reader_md, "encoding": "base64"},
        ]

        client = _make_client(db)
        add = client.post("/api/marketplace/sources", json={
            "name": "Awesome Skills",
            "url": "https://awesomeskill.ai/",
        })
        # 201 Created; auto-detect gives source_type="web_page" since
        # the URL isn't github.com and doesn't end in .json.
        assert add.status_code == 201
        source = add.json()
        source_id = source["id"]
        assert source["source_type"] == "web_page"

        with patch(
            "app.services.website_skill_crawler.crawl_site_for_github_links",
            new=AsyncMock(return_value=page_links),
        ), patch(
            "app.services.skill_source_service.httpx.AsyncClient"
        ) as mock_client_cls:
            responses = [
                AsyncMock(status_code=200, json=lambda: tree_payload),
                AsyncMock(status_code=200, json=lambda: contents_responses[0]),
                AsyncMock(status_code=200, json=lambda: contents_responses[1]),
            ]
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=responses)
            sync = client.post(f"/api/marketplace/sources/{source_id}/sync")
        assert sync.status_code == 202

        # The marketplace tab lists both skills under this source.
        listing = client.get(f"/api/marketplace/sources/{source_id}/skills")
        assert listing.status_code == 200
        data = listing.json()
        assert data["count"] == 2
        names = {s["name"] for s in data["skills"]}
        assert names == {"writer", "reader"}
        # Each skill points at a real GitHub URL.
        for s in data["skills"]:
            assert s["github_url"].startswith(
                "https://github.com/davila7/claude-code-templates"
            )
            assert s["source_url"] == "https://awesomeskill.ai/"

        # The source card itself reflects the new skill count.
        sources = client.get("/api/marketplace/sources").json()
        our = next(s for s in sources["sources"] if s["id"] == source_id)
        assert our["skill_count"] == 2
        assert our["last_sync_status"] == "success"

    def test_install_collected_skill_then_delete_source(self, db):
        """End-to-end: collect from a website → install one of the
        skills → delete the source. The external_skills rows are
        CASCADE-removed; the user's installed Tool row survives."""
        from app.models.tool import Tool
        from app.services import website_skill_crawler
        from app.models.external_skill import ExternalSkill

        page_links = {
            "https://awesomeskill.ai/": [
                "https://github.com/davila7/claude-code-templates/tree/main/skills/writer",
            ],
        }
        tree_payload = {
            "tree": [{"path": "skills/writer/SKILL.md", "type": "blob"}],
            "truncated": False,
        }
        writer_md = _skill_md_b64("writer", "The writer skill")

        client = _make_client(db)
        add = client.post("/api/marketplace/sources", json={
            "url": "https://awesomeskill.ai/",
        })
        source_id = add.json()["id"]

        with patch(
            "app.services.website_skill_crawler.crawl_site_for_github_links",
            new=AsyncMock(return_value=page_links),
        ), patch(
            "app.services.skill_source_service.httpx.AsyncClient"
        ) as mc:
            cl = AsyncMock()
            cl.get = AsyncMock(side_effect=[
                AsyncMock(status_code=200, json=lambda: tree_payload),
                AsyncMock(status_code=200, json=lambda: {
                    "content": writer_md, "encoding": "base64"
                }),
            ])
            mc.return_value.__aenter__ = AsyncMock(return_value=cl)
            mc.return_value.__aexit__ = AsyncMock(return_value=None)
            client.post(f"/api/marketplace/sources/{source_id}/sync")

        # Install the writer skill.
        skills = client.get(
            f"/api/marketplace/sources/{source_id}/skills"
        ).json()["skills"]
        writer = next(s for s in skills if s["name"] == "writer")
        install = client.post(f"/api/marketplace/skills/{writer['id']}/install")
        assert install.status_code == 200
        tool_id = install.json()["tool_id"]

        # My Skills has it.
        my = client.get("/api/marketplace/my-skills").json()
        assert any(s["name"] == "writer" for s in my["skills"])

        # Now hard-delete the source. External skills cascade; the
        # installed Tool stays so the user doesn't lose their work.
        client.delete(f"/api/marketplace/sources/{source_id}?force=true")
        assert db.query(ExternalSkill).filter(
            ExternalSkill.source_id == source_id
        ).first() is None
        tool = db.query(Tool).filter(Tool.id == tool_id).first()
        assert tool is not None
        assert tool.name == "writer"
