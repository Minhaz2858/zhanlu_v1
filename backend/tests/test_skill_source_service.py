import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.database import Base, engine, SessionLocal
import app.models  # noqa
Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Clean marketplace tables before each test to avoid cross-file UNIQUE conflicts."""
    from app.models.skill_source import SkillSource
    from app.models.external_skill import ExternalSkill
    db = SessionLocal()
    try:
        db.query(ExternalSkill).delete()
        db.query(SkillSource).delete()
        db.commit()
    finally:
        db.close()
    yield


class TestDetectSourceType:
    def test_github_url(self):
        from app.services.skill_source_service import detect_source_type
        assert detect_source_type("https://github.com/anthropics/skills") == "github_repo"

    def test_json_url(self):
        from app.services.skill_source_service import detect_source_type
        assert detect_source_type("https://example.com/index.json") == "web_index"

    def test_web_page_url(self):
        from app.services.skill_source_service import detect_source_type
        assert detect_source_type("https://example.com/skills/docs") == "web_page"


class TestSyncGithubRepo:
    @pytest.mark.asyncio
    async def test_sync_github_success(self):
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource

        db = SessionLocal()
        try:
            src = SkillSource(name="Test GH", url="https://github.com/test/repo", source_type="github_repo")
            db.add(src)
            db.commit()
            db.refresh(src)

            mock_tree = {"tree": [
                {"path": "skills/my-skill/SKILL.md", "type": "blob"},
                {"path": "skills/other-skill/SKILL.md", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ]}
            # Base64-encoded frontmatter + body
            import base64
            content1 = base64.b64encode(
                "---\nname: my-skill\ndescription: A test skill\nversion: 1.0.0\ncategory: data\nauthor: tester\ntags:\n  - data\n  - analysis\n---\n# The skill body".encode()
            ).decode()
            content2 = base64.b64encode(
                "---\nname: other-skill\ndescription: Other skill\nversion: 2.0.0\ncategory: utils\n---\n# Other body".encode()
            ).decode()
            mock_contents = [
                {"content": content1, "encoding": "base64"},
                {"content": content2, "encoding": "base64"},
            ]
            with patch("app.services.skill_source_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_client.get = AsyncMock(side_effect=[
                    AsyncMock(status_code=200, json=lambda: mock_tree),
                    AsyncMock(status_code=200, json=lambda: mock_contents[0]),
                    AsyncMock(status_code=200, json=lambda: mock_contents[1]),
                ])
                result = await sync_source(src.id, db)

            assert result["success"] is True
            assert result["skill_count"] == 2
            db.refresh(src)
            assert src.last_sync_status == "success"
            assert src.skill_count == 2
        finally:
            db.close()


class TestSyncWebIndex:
    @pytest.mark.asyncio
    async def test_sync_web_index_success(self):
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource

        db = SessionLocal()
        try:
            src = SkillSource(name="Index", url="https://example.com/index.json", source_type="web_index")
            db.add(src)
            db.commit()
            db.refresh(src)

            mock_index = {"skills": [
                {"name": "indexed-skill", "description": "From index", "skill_md": "## Overview\n\nBody", "category": "tools", "version": "1.0.0"},
            ]}
            with patch("app.services.skill_source_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_client.get = AsyncMock(return_value=AsyncMock(status_code=200, json=lambda: mock_index, raise_for_status=MagicMock()))
                result = await sync_source(src.id, db)

            assert result["success"] is True
            assert result["skill_count"] == 1
        finally:
            db.close()


class TestSeedCurated:
    def test_seed_creates_defaults_if_empty(self):
        from app.services.skill_source_service import seed_curated_sources
        from app.models.skill_source import SkillSource

        db = SessionLocal()
        try:
            count = db.query(SkillSource).filter(SkillSource.is_default == True).count()
            assert count == 0
            created = seed_curated_sources(db)
            assert created >= 1
            count_after = db.query(SkillSource).filter(SkillSource.is_default == True).count()
            assert count_after == created
        finally:
            db.close()

    def test_seed_idempotent(self):
        from app.services.skill_source_service import seed_curated_sources
        db = SessionLocal()
        try:
            first = seed_curated_sources(db)
            second = seed_curated_sources(db)
            assert second == 0  # already seeded
        finally:
            db.close()

    def test_get_curated_sources_needing_sync(self):
        """The marketplace tab's auto-sync trigger returns only default,
        non-hidden sources that have never been synced (or that last
        failed — a transient error shouldn't permanently disable the
        card)."""
        from app.services.skill_source_service import (
            seed_curated_sources, get_curated_sources_needing_sync,
        )
        from app.models.skill_source import SkillSource

        db = SessionLocal()
        try:
            seed_curated_sources(db)
            # After seeding, every curated source should be in the
            # "needs sync" bucket (last_sync_status="never").
            pending = get_curated_sources_needing_sync(db)
            assert len(pending) >= 1
            assert all(s.is_default for s in pending)
            assert all(s.last_sync_status in ("never", "failed") for s in pending)
            assert all(not s.is_hidden for s in pending)

            # Mark one as "success" — it should drop out of the pending list.
            pending[0].last_sync_status = "success"
            db.commit()
            remaining = get_curated_sources_needing_sync(db)
            assert pending[0].id not in [s.id for s in remaining]

            # Mark one as "failed" — it should STAY in the list (we
            # retry failed sources on every list call so a transient
            # error doesn't permanently disable the card).
            if len(remaining) >= 1:
                remaining[0].last_sync_status = "failed"
                db.commit()
                final = get_curated_sources_needing_sync(db)
                assert remaining[0].id in [s.id for s in final]

            # Hide another — it should drop out (we never sync hidden
            # sources, even on first load).
            final[0].is_hidden = True
            db.commit()
            last = get_curated_sources_needing_sync(db)
            assert final[0].id not in [s.id for s in last]
        finally:
            db.close()


# ─── website-source (web_page) collection: added 2026-07-30 ──────────
#
# These tests pin the new behavior of ``_sync_web_page``:
#   * browse the site, harvest GitHub links, fetch real SKILL.md files
#   * 50-skill cap per website
#   * stale rows are marked is_deleted=True on re-sync
#   * LLM single-skill extraction is the FALLBACK when no GitHub links
#     are found on the site
#
# The browser is mocked at the crawler module boundary and the GitHub
# HTTP layer is mocked via ``httpx.AsyncClient``. Real DB writes go
# through the real helpers.

import base64
from app.services import website_skill_crawler as crawler_mod  # noqa: E402


def _make_skill_md(name, description, category="general", tags=None, version="1.0.0"):
    md = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"version: {version}\n"
        f"category: {category}\n"
        + (f"tags:\n  - " + "\n  - ".join(tags or []) + "\n" if tags else "")
        + f"---\n# {name}\n\nBody for {name}."
    )
    return base64.b64encode(md.encode()).decode()


class TestSyncWebPageWebsite:
    """When a user adds a website URL, ``_sync_web_page`` now browses
    it, collects every ``github.com`` link, and turns each link into a
    real ``ExternalSkill`` row by fetching the underlying SKILL.md."""

    @pytest.mark.asyncio
    async def test_collects_skills_from_github_links(self):
        from app.services.skill_source_service import sync_source, _collect_skills_from_github_links
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        # The crawler returns: listing page → 2 GitHub tree links, one
        # pointing at a directory with a SKILL.md and one bare repo.
        page_links = {
            "https://awesomeskill.ai/": [
                "https://github.com/davila7/claude-code-templates/tree/main/skills/writer",
                "https://github.com/davila7/claude-code-templates/tree/main/skills/reader",
            ],
        }
        # GitHub tree: the writer dir has SKILL.md, the reader dir has SKILL.md.
        tree_payload = {
            "tree": [
                {"path": "skills/writer/SKILL.md", "type": "blob"},
                {"path": "skills/reader/SKILL.md", "type": "blob"},
            ],
            "truncated": False,
        }
        writer_md = _make_skill_md("writer", "The writer skill", category="business", tags=["writing"])
        reader_md = _make_skill_md("reader", "The reader skill", category="data", tags=["parsing"])
        # Sequential contents responses.
        contents_responses = [
            {"content": writer_md, "encoding": "base64"},
            {"content": reader_md, "encoding": "base64"},
        ]

        db = SessionLocal()
        try:
            src = SkillSource(
                name="Awesome", url="https://awesomeskill.ai/", source_type="web_page",
                is_default=False, brand_color="#000", icon_emoji="A",
            )
            db.add(src)
            db.commit()
            db.refresh(src)

            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value=page_links)), \
                 patch("app.services.skill_source_service.httpx.AsyncClient") as mock_client_cls:
                # Track call sequence to route tree vs contents.
                responses = [
                    # tree
                    AsyncMock(status_code=200, json=lambda: tree_payload),
                    # contents: writer
                    AsyncMock(status_code=200, json=lambda: contents_responses[0]),
                    # contents: reader
                    AsyncMock(status_code=200, json=lambda: contents_responses[1]),
                ]
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_client.get = AsyncMock(side_effect=responses)

                result = await sync_source(src.id, db)

            assert result["success"] is True
            assert result["skill_count"] == 2
            db.refresh(src)
            assert src.last_sync_status == "success"
            assert src.skill_count == 2

            skills = db.query(ExternalSkill).filter(ExternalSkill.source_id == src.id).all()
            assert {s.name for s in skills} == {"writer", "reader"}
            for s in skills:
                assert s.source_id == src.id
                # Discovery page is the listing page where the GitHub link was found.
                assert s.source_url == "https://awesomeskill.ai/"
                assert s.github_url and s.github_url.startswith(
                    "https://github.com/davila7/claude-code-templates"
                )
                assert s.skill_md and "Body for" in s.skill_md
                assert s.tags  # parsed from frontmatter
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_respects_50_skill_cap(self):
        """Even if the crawler returns 80 GitHub links, only the first
        50 skills get collected. The source's skill_count reflects 50."""
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        import app.services.skill_source_service as svc

        # 80 unique tree links, each pointing at its own skill directory.
        page_links = {
            "https://awesomeskill.ai/": [
                f"https://github.com/owner/repo/tree/main/skills/skill-{i:02d}"
                for i in range(80)
            ],
        }
        db = SessionLocal()
        try:
            src = SkillSource(
                name="Big", url="https://awesomeskill.ai/", source_type="web_page",
                is_default=False, brand_color="#000", icon_emoji="B",
            )
            db.add(src)
            db.commit()
            db.refresh(src)

            # Build a tree that contains SKILL.md under each skill dir.
            tree_paths = [
                {"path": f"skills/skill-{i:02d}/SKILL.md", "type": "blob"}
                for i in range(80)
            ]
            tree_payload = {"tree": tree_paths, "truncated": False}

            def make_skill_content(i):
                md = (
                    f"---\nname: skill-{i:02d}\n"
                    f"description: Skill {i}\nversion: 1.0.0\ncategory: general\n---\n# body"
                )
                return base64.b64encode(md.encode()).decode()

            contents_payloads = [
                {"content": make_skill_content(i), "encoding": "base64"}
                for i in range(50)
            ]
            response_queue = [
                AsyncMock(status_code=200, json=lambda: tree_payload),
                *[AsyncMock(status_code=200, json=lambda p=p: p) for p in contents_payloads],
            ]

            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value=page_links)), \
                 patch("app.services.skill_source_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_client.get = AsyncMock(side_effect=response_queue)
                result = await sync_source(src.id, db)

            assert result["skill_count"] == svc.MAX_SKILLS_PER_WEBSITE
            db.refresh(src)
            assert src.skill_count == svc.MAX_SKILLS_PER_WEBSITE
            rows = db.query(ExternalSkill).filter(ExternalSkill.source_id == src.id).count()
            assert rows == svc.MAX_SKILLS_PER_WEBSITE
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_marks_stale_rows_deleted_on_re_sync(self):
        """On re-sync, skills that no longer exist on the upstream
        site are marked ``is_deleted=True`` (consistent with the
        GitHub-repo path)."""
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        page_links_v1 = {
            "https://awesomeskill.ai/": [
                "https://github.com/owner/repo/tree/main/skills/keep",
                "https://github.com/owner/repo/tree/main/skills/drop",
            ],
        }
        # Second sync: only "keep" remains.
        page_links_v2 = {
            "https://awesomeskill.ai/": [
                "https://github.com/owner/repo/tree/main/skills/keep",
            ],
        }
        def make_tree(*names):
            return {"tree": [{"path": f"skills/{n}/SKILL.md", "type": "blob"} for n in names], "truncated": False}
        def make_contents(name):
            md = f"---\nname: {name}\ndescription: {name}\nversion: 1.0.0\ncategory: general\n---\n# body"
            return {"content": base64.b64encode(md.encode()).decode(), "encoding": "base64"}

        db = SessionLocal()
        try:
            src = SkillSource(
                name="S", url="https://awesomeskill.ai/", source_type="web_page",
                is_default=False, brand_color="#000", icon_emoji="S",
            )
            db.add(src)
            db.commit()
            db.refresh(src)

            # First sync: 2 skills.
            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value=page_links_v1)), \
                 patch("app.services.skill_source_service.httpx.AsyncClient") as mc1:
                tree1 = AsyncMock(status_code=200, json=lambda: make_tree("keep", "drop"))
                c1 = AsyncMock(status_code=200, json=lambda: make_contents("keep"))
                c2 = AsyncMock(status_code=200, json=lambda: make_contents("drop"))
                cl1 = AsyncMock()
                cl1.get = AsyncMock(side_effect=[tree1, c1, c2])
                mc1.return_value.__aenter__ = AsyncMock(return_value=cl1)
                mc1.return_value.__aexit__ = AsyncMock(return_value=None)
                await sync_source(src.id, db)

            assert db.query(ExternalSkill).filter(
                ExternalSkill.source_id == src.id,
                ExternalSkill.is_deleted == False,  # noqa: E712
            ).count() == 2

            # Second sync: only "keep" — "drop" should be marked stale.
            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value=page_links_v2)), \
                 patch("app.services.skill_source_service.httpx.AsyncClient") as mc2:
                tree2 = AsyncMock(status_code=200, json=lambda: make_tree("keep"))
                c1b = AsyncMock(status_code=200, json=lambda: make_contents("keep"))
                cl2 = AsyncMock()
                cl2.get = AsyncMock(side_effect=[tree2, c1b])
                mc2.return_value.__aenter__ = AsyncMock(return_value=cl2)
                mc2.return_value.__aexit__ = AsyncMock(return_value=None)
                await sync_source(src.id, db)

            keep = db.query(ExternalSkill).filter(
                ExternalSkill.source_id == src.id, ExternalSkill.name == "keep"
            ).first()
            drop = db.query(ExternalSkill).filter(
                ExternalSkill.source_id == src.id, ExternalSkill.name == "drop"
            ).first()
            assert keep.is_deleted is False
            assert drop.is_deleted is True
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_falls_back_to_llm_when_no_github_links(self):
        """When the crawler returns no GitHub links, the existing
        single-skill LLM extraction path runs (preserves the
        documentation-page use case)."""
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill

        db = SessionLocal()
        try:
            src = SkillSource(
                name="Docs", url="https://example.com/docs", source_type="web_page",
                is_default=False, brand_color="#000", icon_emoji="D",
            )
            db.add(src)
            db.commit()
            db.refresh(src)

            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value={})), \
                 patch(
                     "app.services.tool_handlers.agent_browser_tool._agent_browser",
                     new=AsyncMock(return_value={
                         "success": True,
                         "text": "This page documents an API skill with lots of detail. " * 30,
                     }),
                 ), \
                 patch(
                     "app.services.llm_service.call_llm",
                     new=AsyncMock(return_value={"data": {
                         "name": "docs-skill",
                         "description": "An API documentation skill",
                         "body": "Detailed content here.",
                     }}),
                 ) as mock_llm:
                result = await sync_source(src.id, db)

            assert result["success"] is True
            assert result["skill_count"] == 1
            assert mock_llm.called
            skills = db.query(ExternalSkill).filter(ExternalSkill.source_id == src.id).all()
            assert len(skills) == 1
            assert skills[0].name == "docs-skill"
            assert skills[0].category == "scraped"  # the LLM-path category marker
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_fails_when_no_links_and_llm_also_fails(self):
        """Both paths fail → sync returns success=False with a useful error."""
        from app.services.skill_source_service import sync_source
        from app.models.skill_source import SkillSource

        db = SessionLocal()
        try:
            src = SkillSource(
                name="Empty", url="https://example.com/empty", source_type="web_page",
                is_default=False, brand_color="#000", icon_emoji="E",
            )
            db.add(src)
            db.commit()
            db.refresh(src)

            with patch.object(crawler_mod, "crawl_site_for_github_links",
                              new=AsyncMock(return_value={})), \
                 patch(
                     "app.services.tool_handlers.agent_browser_tool._agent_browser",
                     new=AsyncMock(return_value={"success": False, "error": "browser unavailable"}),
                 ):
                result = await sync_source(src.id, db)

            assert result["success"] is False
            assert "error" in result
            db.refresh(src)
            assert src.last_sync_status == "failed"
            assert src.last_sync_error  # populated
        finally:
            db.close()
