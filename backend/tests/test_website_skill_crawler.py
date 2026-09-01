"""Unit tests for ``app.services.website_skill_crawler`` (added 2026-07-30).

Covers the pure logic only: GitHub link parsing and the BFS crawl rules.
End-to-end behavior (crawler → ``_sync_web_page`` → DB rows) is pinned
in ``test_skill_source_service.py::TestSyncWebPageWebsite`` and
``test_skill_marketplace_lifecycle.py::TestCollectFromWebsiteWithGithubLinks``.

The browser is injected as an ``async (args) -> dict`` callable so we
can simulate any page shape without touching the real agent-browser
binary. The real tool is bound to a unique ``conversation_id`` in
production (see the module's default wrapper).
"""
import pytest
from unittest.mock import AsyncMock

from app.services.website_skill_crawler import (
    parse_github_skill_link,
    crawl_site_for_github_links,
    GithubLinkInfo,
)


# ─── parse_github_skill_link ──────────────────────────────────────────


class TestParseGithubSkillLink:
    def test_bare_repo_defaults_main_branch(self):
        info = parse_github_skill_link("https://github.com/anthropics/skills")
        assert info == GithubLinkInfo(
            owner="anthropics", repo="skills", branch="main", subpath="", is_file=False
        )

    def test_bare_repo_with_git_suffix(self):
        info = parse_github_skill_link("https://github.com/owner/repo.git")
        assert info.owner == "owner"
        assert info.repo == "repo"
        assert info.subpath == ""
        assert info.is_file is False

    def test_tree_link_with_subpath(self):
        # The exact shape from the user's screenshot:
        # https://github.com/davila7/claude-code-templates/tree/main/cli-tool/components/skills/...
        info = parse_github_skill_link(
            "https://github.com/davila7/claude-code-templates/tree/main/"
            "cli-tool/components/skills/business-marketing/content-research-writer"
        )
        assert info.owner == "davila7"
        assert info.repo == "claude-code-templates"
        assert info.branch == "main"
        assert info.subpath == "cli-tool/components/skills/business-marketing/content-research-writer"
        assert info.is_file is False

    def test_tree_link_branch_only_treated_as_whole_repo(self):
        info = parse_github_skill_link(
            "https://github.com/owner/repo/tree/develop"
        )
        assert info.branch == "develop"
        assert info.subpath == ""
        assert info.is_file is False

    def test_blob_link_is_marked_as_file(self):
        info = parse_github_skill_link(
            "https://github.com/owner/repo/blob/main/skills/foo/SKILL.md"
        )
        assert info.owner == "owner"
        assert info.repo == "repo"
        assert info.branch == "main"
        assert info.subpath == "skills/foo/SKILL.md"
        assert info.is_file is True

    def test_non_github_url_returns_none(self):
        assert parse_github_skill_link("https://example.com/foo") is None
        assert parse_github_skill_link("https://gitlab.com/owner/repo") is None
        assert parse_github_skill_link("not-a-url") is None

    def test_unparseable_github_url_returns_none(self):
        assert parse_github_skill_link("https://github.com/") is None
        assert parse_github_skill_link("https://github.com/only-owner") is None


# ─── crawl BFS rules ─────────────────────────────────────────────────


def _make_browser(pages: dict[str, dict]):
    """Build a fake agent_browser that returns the canned page data.

    ``pages`` maps URL → ``{"links": [...same_domain or external hrefs...], "text": "..."}``.
    The fake dispatches:
      - "navigate"        → success
      - "eval" (array expression) → JSON-encoded list of hrefs
      - "extract"         → text with markdown links [t](href)
      - "close"           → success (recorded)
    The fake tracks calls so tests can assert on the close-in-finally behavior.
    """
    state = {"calls": [], "closed": False}

    async def fake(args):
        action = (args.get("action") or "navigate").lower()
        state["calls"].append(args)
        if action == "navigate":
            return {"success": True, "url": args.get("url")}
        if action == "eval":
            # We assume the caller's expression asks for all anchor hrefs;
            # the fake returns whatever hrefs the page declares.
            url = _eval_current_url(state["calls"], args) or next(iter(pages))
            payload = pages.get(url, {"links": [], "text": ""})
            return {"success": True, "result": payload["links"]}
        if action == "extract":
            url = args.get("url") or _eval_current_url(state["calls"], args)
            payload = pages.get(url, {"text": ""})
            # Render hrefs as markdown links so the regex fallback can find them.
            md_links = "".join(f"[l]({href})" for href in payload.get("links", []))
            return {"success": True, "text": payload.get("text", "") + "\n" + md_links}
        if action == "close":
            state["closed"] = True
            return {"success": True, "message": "closed"}
        return {"success": False, "error": f"unknown action {action}"}

    return fake, state


def _eval_current_url(calls, this_args):
    # Last navigate's url = current page.
    for c in reversed(calls):
        if c.get("action") == "navigate":
            return c.get("url")
    return None


class TestCrawlSiteForGithubLinks:
    @pytest.mark.asyncio
    async def test_single_page_harvests_github_links_via_eval(self):
        # One page with two GitHub links (one tree, one bare).
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://github.com/owner/repo/tree/main/skills/a",
                    "https://github.com/owner2/repo2",
                    "https://awesomeskill.ai/about",  # same-domain, not github
                    "https://example.com/x",          # external
                ],
            }
        }
        browser, state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=10, agent_browser=browser
        )
        # Only GitHub links in the result, mapped to their discovery page.
        assert "https://awesomeskill.ai/" in result
        assert set(result["https://awesomeskill.ai/"]) == {
            "https://github.com/owner/repo/tree/main/skills/a",
            "https://github.com/owner2/repo2",
        }
        assert state["closed"] is True  # close called in finally

    @pytest.mark.asyncio
    async def test_bfs_follows_same_domain_links(self):
        # Listing page → detail page → another detail page.
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://awesomeskill.ai/skills/a",
                    "https://awesomeskill.ai/skills/b",
                    "https://github.com/owner/repo",  # also here
                    "https://twitter.com/x",          # external, ignored
                ],
            },
            "https://awesomeskill.ai/skills/a": {
                "links": ["https://github.com/owner/repo/tree/main/a"],
            },
            "https://awesomeskill.ai/skills/b": {
                "links": ["https://github.com/owner/repo/tree/main/b"],
            },
        }
        browser, state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=20, agent_browser=browser
        )
        # All 3 pages were visited and yielded GitHub links.
        assert set(result.keys()) == {
            "https://awesomeskill.ai/",
            "https://awesomeskill.ai/skills/a",
            "https://awesomeskill.ai/skills/b",
        }
        flat = {href for hrefs in result.values() for href in hrefs}
        assert flat == {
            "https://github.com/owner/repo",
            "https://github.com/owner/repo/tree/main/a",
            "https://github.com/owner/repo/tree/main/b",
        }

    @pytest.mark.asyncio
    async def test_stops_at_max_pages_budget(self):
        # A site with a chain of 5 same-domain pages; cap at 3.
        pages = {
            f"https://awesomeskill.ai/p{i}": {"links": [f"https://awesomeskill.ai/p{i+1}"]}
            for i in range(5)
        }
        pages["https://awesomeskill.ai/p0"]["links"].append("https://github.com/x/y")
        browser, _state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/p0", max_pages=3, agent_browser=browser
        )
        # At most 3 pages visited.
        assert len(result) <= 3

    @pytest.mark.asyncio
    async def test_skips_static_extensions_when_crawling(self):
        # Listing page links to a .pdf and a .zip; those should not be visited
        # (they'd waste the page budget) but the GitHub link is still harvested.
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://awesomeskill.ai/guide.pdf",
                    "https://awesomeskill.ai/download.zip",
                    "https://github.com/owner/repo",
                ],
            },
            "https://awesomeskill.ai/guide.pdf": {"links": []},
            "https://awesomeskill.ai/download.zip": {"links": []},
        }
        browser, state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=20, agent_browser=browser
        )
        # Only the listing page yielded content; the .pdf/.zip pages were skipped.
        navigated = [c["url"] for c in state["calls"] if c.get("action") == "navigate"]
        assert "https://awesomeskill.ai/guide.pdf" not in navigated
        assert "https://awesomeskill.ai/download.zip" not in navigated
        # But the GitHub link from the listing is in the result.
        assert "https://github.com/owner/repo" in result["https://awesomeskill.ai/"]

    @pytest.mark.asyncio
    async def test_skips_auth_paths_when_crawling(self):
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://awesomeskill.ai/login",
                    "https://awesomeskill.ai/signup",
                    "https://github.com/owner/repo",
                ],
            },
            "https://awesomeskill.ai/login": {"links": []},
            "https://awesomeskill.ai/signup": {"links": []},
        }
        browser, state = _make_browser(pages)
        await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=20, agent_browser=browser
        )
        navigated = [c["url"] for c in state["calls"] if c.get("action") == "navigate"]
        assert "https://awesomeskill.ai/login" not in navigated
        assert "https://awesomeskill.ai/signup" not in navigated

    @pytest.mark.asyncio
    async def test_dedupes_pages(self):
        # Page A links to B; B links back to A. A should be visited only once.
        pages = {
            "https://awesomeskill.ai/": {"links": ["https://awesomeskill.ai/b"]},
            "https://awesomeskill.ai/b": {
                "links": ["https://awesomeskill.ai/", "https://github.com/o/r"]
            },
        }
        browser, _state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=20, agent_browser=browser
        )
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_normalizes_www_and_strips_fragments(self):
        # www. and the start should be treated as the same site; fragment stripped.
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://www.awesomeskill.ai/skills/a#section",
                    "https://awesomeskill.ai/skills/a?utm_source=x",  # utm stripped
                ],
            },
            "https://awesomeskill.ai/skills/a": {"links": ["https://github.com/o/r"]},
        }
        browser, _state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=20, agent_browser=browser
        )
        # Both normalized forms resolve to the same page → visited once.
        assert len(result) == 2  # listing + the detail page

    @pytest.mark.asyncio
    async def test_closes_browser_in_finally_on_exception(self):
        # An eval that raises — the close must still run.
        async def bad_browser(args):
            if (args.get("action") or "navigate").lower() == "close":
                return {"success": True}
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await crawl_site_for_github_links(
                "https://awesomeskill.ai/", max_pages=5, agent_browser=bad_browser
            )
        # We can't easily inspect the bad_browser's calls (it raised), so
        # instead test with a recording browser that raises mid-crawl then
        # still records close.
        state = {"closed": False}

        async def recording_browser(args):
            state[args.get("action")] = True
            if (args.get("action") or "navigate").lower() == "eval":
                raise RuntimeError("boom")
            return {"success": True}

        with pytest.raises(RuntimeError):
            await crawl_site_for_github_links(
                "https://awesomeskill.ai/", max_pages=5, agent_browser=recording_browser
            )
        assert state.get("close") is True

    @pytest.mark.asyncio
    async def test_empty_start_page_returns_empty(self):
        pages = {"https://awesomeskill.ai/": {"links": []}}
        browser, _state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=10, agent_browser=browser
        )
        # The page is recorded even with no links (the caller may want to
        # know we visited it; the orchestrator checks len(github_links) == 0).
        assert result == {"https://awesomeskill.ai/": []}

    @pytest.mark.asyncio
    async def test_non_github_links_excluded_from_result(self):
        pages = {
            "https://awesomeskill.ai/": {
                "links": [
                    "https://gitlab.com/o/r",
                    "https://example.com/x",
                    "https://github.com/o/r",
                ],
            }
        }
        browser, _state = _make_browser(pages)
        result = await crawl_site_for_github_links(
            "https://awesomeskill.ai/", max_pages=10, agent_browser=browser
        )
        flat = {h for hrefs in result.values() for h in hrefs}
        assert flat == {"https://github.com/o/r"}
