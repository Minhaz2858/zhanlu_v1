"""SkillSourceService — auto-detect source type, sync external skill catalogs."""

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.models.skill_source import SkillSource
from app.models.external_skill import ExternalSkill

logger = logging.getLogger(__name__)

CURATED_SOURCES = [
    {
        "name": "Anthropic Skills",
        "url": "https://github.com/anthropics/skills",
        "source_type": "github_repo",
        "description": "Official Anthropic skill examples and best practices",
        "brand_color": "#191919",
        "icon_emoji": "✦",
    },
    {
        "name": "Awesome Claude Code Skills",
        "url": "https://github.com/hesreallyhim/awesome-claude-code",
        "source_type": "github_repo",
        "description": "Community-curated collection of Claude Code skills",
        "brand_color": "#7C3AED",
        "icon_emoji": "★",
    },
]


# Default visual branding applied to a source when none is set. Both are
# also applied to existing rows that predate the columns being added.
DEFAULT_BRAND_COLOR = "#1f2937"
DEFAULT_ICON_EMOJI_PREFIX = "◆"

# Caps for the website-collection path (added 2026-07-30). Both are
# env-overridable so a deployment can tune them without a code change.
# ``MAX_SKILLS_PER_WEBSITE`` stops the GitHub-fetch loop early once the
# source has collected this many skills; ``MAX_PAGES_PER_CRAWL`` stops
# the same-domain browser crawl so a runaway site can't burn the
# background-sync budget.
MAX_SKILLS_PER_WEBSITE = int(
    os.environ.get("MARKETPLACE_MAX_SKILLS_PER_WEBSITE", "50")
)
MAX_PAGES_PER_CRAWL = int(
    os.environ.get("MARKETPLACE_MAX_PAGES_PER_CRAWL", "100")
)


def detect_source_type(url: str) -> str:
    """Auto-detect the source type from the URL."""
    if "github.com" in url:
        return "github_repo"
    if url.endswith(".json"):
        return "web_index"
    return "web_page"


def _parse_github_url(url: str) -> tuple[str, str, str]:
    """Extract owner, repo, and branch from a GitHub URL."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)(?:\.git)?(?:/tree/([^/]+))?", url)
    if not m:
        raise ValueError(f"Could not parse GitHub URL: {url}")
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    branch = m.group(3) or "main"
    return owner, repo, branch


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md content. Returns (meta, body)."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_text = parts[1].strip()
    body = parts[2].strip()
    meta = {}
    for line in fm_text.split("\n"):
        if ":" in line and not line.strip().startswith("-"):
            key, _, val = line.partition(":")
            val = val.strip().strip('"').strip("'")
            if val:
                meta[key.strip()] = val
    # Parse tags (YAML list)
    if "tags" in fm_text:
        tags = []
        in_tags = False
        for line in fm_text.split("\n"):
            if line.strip().startswith("tags:"):
                in_tags = True
                continue
            if in_tags:
                if line.strip().startswith("- "):
                    tags.append(line.strip()[2:].strip())
                else:
                    break
        if tags:
            meta["tags"] = tags
    return meta, body


async def _fetch_repo_tree(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    headers: dict,
) -> Optional[list]:
    """Fetch the recursive git tree of a GitHub repo.

    Returns the list of entry paths. Returns ``None`` if the repository
    or branch is not found (HTTP 404). Raises ``httpx.HTTPError`` on
    transport / 4xx errors so the caller can surface them as sync
    failures. Behavior-preserving extraction from ``_sync_github_repo``;
    reused by the website collection path so both routes go through the
    same GitHub surface.
    """
    tree_url = (
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    )
    resp = await client.get(tree_url, headers=headers)
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        raise httpx.HTTPStatusError(
            "GitHub API rate limit exceeded. Set GITHUB_TOKEN env var.",
            request=resp.request,
            response=resp,
        )
    resp.raise_for_status()
    tree = resp.json()
    return [t["path"] for t in tree.get("tree", [])]


async def _fetch_skill_md(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    path: str,
    headers: dict,
) -> Optional[str]:
    """Fetch a single file from a GitHub repo and return its decoded text.

    Returns ``None`` on non-200 (logged at the call site). Raises
    ``httpx.HTTPError`` on transport errors.
    """
    content_url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    )
    resp = await client.get(content_url, headers=headers)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")


def _upsert_external_skill(
    db: Session,
    source: SkillSource,
    *,
    name: str,
    raw_skill_md: str,
    github_url: str,
    source_page_url: Optional[str] = None,
    meta: Optional[dict] = None,
    fallback_category: str = "general",
) -> ExternalSkill:
    """Insert or update an ``ExternalSkill`` row by ``(source_id, name)``.

    Frontmatter ``meta`` drives the user-facing fields; missing keys fall
    back to sensible defaults. ``source_page_url`` is the page on the
    external site where the GitHub link was discovered (or the same as
    ``github_url`` for plain GitHub sources). Returns the row.
    """
    meta = meta or {}
    display = meta.get("name", name)
    desc = meta.get("description", "")
    tags = meta.get("tags", [])
    now = datetime.now(timezone.utc)
    existing = (
        db.query(ExternalSkill)
        .filter(
            ExternalSkill.source_id == source.id,
            ExternalSkill.name == name,
        )
        .first()
    )
    if existing:
        existing.display_name = display
        existing.description = desc
        existing.summary = desc[:200]
        existing.category = meta.get("category", fallback_category)
        existing.version = meta.get("version", "1.0.0")
        existing.author = meta.get("author")
        existing.skill_md = raw_skill_md
        existing.tags = tags
        existing.source_url = source_page_url or existing.source_url
        existing.github_url = github_url
        existing.last_synced_at = now
        return existing
    row = ExternalSkill(
        source_id=source.id,
        name=name,
        display_name=display,
        description=desc,
        summary=desc[:200],
        category=meta.get("category", fallback_category),
        version=meta.get("version", "1.0.0"),
        author=meta.get("author"),
        skill_md=raw_skill_md,
        tags=tags,
        source_url=source_page_url,
        github_url=github_url,
        last_synced_at=now,
    )
    db.add(row)
    return row


async def _sync_github_repo(source: SkillSource, db: Session) -> dict:
    """Sync a GitHub repo source by listing the tree and fetching SKILL.md files."""
    owner, repo, branch = _parse_github_url(source.url)
    github_token = os.environ.get("GITHUB_TOKEN")
    headers = {"Authorization": f"token {github_token}"} if github_token else {}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            tree_paths = await _fetch_repo_tree(client, owner, repo, branch, headers)
        except httpx.HTTPError as exc:
            return {"success": False, "skill_count": 0, "error": str(exc)[:500]}
        if tree_paths is None:
            return {"success": False, "skill_count": 0, "error": "Repository or branch not found"}

        skill_paths = [p for p in tree_paths if p.endswith("SKILL.md")]
        if not skill_paths:
            return {"success": False, "skill_count": 0, "error": "No SKILL.md files found in repository"}

        seen_names = set()
        for path in skill_paths:
            raw = await _fetch_skill_md(client, owner, repo, branch, path, headers)
            if raw is None:
                logger.warning("Failed to fetch %s", path)
                continue
            meta, _body = _parse_frontmatter(raw)
            name = meta.get("name") or Path(path).parent.name
            seen_names.add(name)
            github_skill_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"
            _upsert_external_skill(
                db,
                source,
                name=name,
                raw_skill_md=raw,
                github_url=github_skill_url,
                source_page_url=github_skill_url,
                meta=meta,
            )

        # Mark stale entries as deleted
        if seen_names:
            stale = db.query(ExternalSkill).filter(
                ExternalSkill.source_id == source.id,
                ~ExternalSkill.name.in_(seen_names),
            ).all()
            for s in stale:
                s.is_deleted = True

    db.commit()
    return {"success": True, "skill_count": len(seen_names), "error": None}


async def _sync_web_index(source: SkillSource, db: Session) -> dict:
    """Sync a JSON index source."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(source.url)
        resp.raise_for_status()
        data = resp.json()

    skills = data.get("skills", [])
    if not skills:
        return {"success": False, "skill_count": 0, "error": "No skills found in index"}

    seen_names = set()
    for sk in skills:
        name = sk.get("name", "unknown")
        seen_names.add(name)
        skill_md = sk.get("skill_md", "")
        if not skill_md and sk.get("skill_url"):
            async with httpx.AsyncClient(timeout=15) as c:
                sr = await c.get(sk["skill_url"])
                skill_md = sr.text

        existing = db.query(ExternalSkill).filter(
            ExternalSkill.source_id == source.id,
            ExternalSkill.name == name,
        ).first()
        if existing:
            existing.display_name = sk.get("name", name)
            existing.description = sk.get("description", "")
            existing.summary = sk.get("description", "")[:200]
            existing.category = sk.get("category", "general")
            existing.version = sk.get("version", "1.0.0")
            existing.author = sk.get("author")
            existing.skill_md = skill_md
            existing.tags = sk.get("tags", [])
            existing.source_url = sk.get("skill_url", source.url)
            existing.last_synced_at = datetime.now(timezone.utc)
        else:
            db.add(ExternalSkill(
                source_id=source.id,
                name=name,
                display_name=sk.get("name", name),
                description=sk.get("description", ""),
                summary=sk.get("description", "")[:200],
                category=sk.get("category", "general"),
                version=sk.get("version", "1.0.0"),
                author=sk.get("author"),
                skill_md=skill_md,
                tags=sk.get("tags", []),
                source_url=sk.get("skill_url", source.url),
                last_synced_at=datetime.now(timezone.utc),
            ))

    db.commit()
    return {"success": True, "skill_count": len(seen_names), "error": None}


async def _collect_skills_from_github_links(
    source: SkillSource,
    db: Session,
    page_links: dict[str, list[str]],
) -> dict:
    """Resolve GitHub links harvested from a website into ``ExternalSkill`` rows.

    For each ``(page, github_url)`` pair we:
      * parse the URL into ``(owner, repo, branch, subpath, is_file)``
        via :func:`parse_github_skill_link`
      * for a ``/blob/`` link, fetch that exact ``SKILL.md`` file
      * for a ``/tree/`` or bare-repo link, fetch the repo tree once
        (cached per ``(owner, repo, branch)``) and find every
        ``SKILL.md`` under the link's subtree
      * dedupe across links pointing at the same file
      * stop once ``MAX_SKILLS_PER_WEBSITE`` is reached

    Stale rows (skills the source USED to have but no longer surfaces)
    are marked ``is_deleted=True`` after a successful pass — matches
    the semantics of ``_sync_github_repo`` so the marketplace tab
    hides skills that have disappeared upstream.

    Per-link failures (404, 403, transport errors) are logged and
    skipped so a single bad link doesn't fail the whole sync. The
    function only returns a hard failure when the input yields zero
    parseable links at all.
    """
    from app.services.website_skill_crawler import parse_github_skill_link

    github_token = os.environ.get("GITHUB_TOKEN")
    headers = {"Authorization": f"token {github_token}"} if github_token else {}

    # Flatten and parse all links first; the parse step is cheap and
    # lets us bail out early on empty input without opening an HTTP
    # client.
    parsed: list[tuple[str, str, str, str, str, bool]] = []
    # (page, owner, repo, branch, subpath, is_file)
    for page, links in page_links.items():
        for link in links:
            info = parse_github_skill_link(link)
            if info is None:
                continue
            parsed.append(
                (page, info.owner, info.repo, info.branch, info.subpath, info.is_file)
            )

    if not parsed:
        return {"success": True, "skill_count": 0, "error": None}

    seen_names: set[str] = set()
    fetched: set[tuple[str, str, str, str]] = set()
    tree_cache: dict[tuple[str, str, str], Optional[list[str]]] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for page, owner, repo, branch, subpath, is_file in parsed:
            if len(seen_names) >= MAX_SKILLS_PER_WEBSITE:
                break

            if is_file:
                # Exact-file link: only collect SKILL.md files. Other
                # files in a GitHub repo aren't skills.
                if not subpath.endswith("SKILL.md"):
                    continue
                key = (owner, repo, branch, subpath)
                if key in fetched:
                    continue
                fetched.add(key)
                try:
                    raw = await _fetch_skill_md(
                        client, owner, repo, branch, subpath, headers
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Skill md fetch failed: %s/%s: %s", repo, subpath, exc)
                    continue
                if raw is None:
                    continue
                meta, _body = _parse_frontmatter(raw)
                name = (
                    meta.get("name")
                    or (Path(subpath).parent.name if "/" in subpath else Path(subpath).stem)
                )
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                github_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{subpath}"
                _upsert_external_skill(
                    db,
                    source,
                    name=name,
                    raw_skill_md=raw,
                    github_url=github_url,
                    source_page_url=page,
                    meta=meta,
                    fallback_category="website",
                )
                continue

            # Tree or bare-repo link: resolve via the repo tree.
            tkey = (owner, repo, branch)
            if tkey not in tree_cache:
                try:
                    tree_cache[tkey] = await _fetch_repo_tree(
                        client, owner, repo, branch, headers
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Tree fetch failed for %s/%s: %s", owner, repo, exc)
                    tree_cache[tkey] = None
            tree_paths = tree_cache[tkey]
            if not tree_paths:
                continue

            if subpath == "":
                candidates = [p for p in tree_paths if p.endswith("SKILL.md")]
            else:
                prefix = subpath.rstrip("/") + "/"
                candidates = [
                    p for p in tree_paths
                    if p.endswith("SKILL.md") and p.startswith(prefix)
                ]

            for path in candidates:
                if len(seen_names) >= MAX_SKILLS_PER_WEBSITE:
                    break
                key = (owner, repo, branch, path)
                if key in fetched:
                    continue
                fetched.add(key)
                try:
                    raw = await _fetch_skill_md(
                        client, owner, repo, branch, path, headers
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Skill md fetch failed: %s/%s: %s", repo, path, exc)
                    continue
                if raw is None:
                    continue
                meta, _body = _parse_frontmatter(raw)
                name = meta.get("name") or Path(path).parent.name
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                github_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"
                _upsert_external_skill(
                    db,
                    source,
                    name=name,
                    raw_skill_md=raw,
                    github_url=github_url,
                    source_page_url=page,
                    meta=meta,
                    fallback_category="website",
                )

    # Stale-mark skills that were under this source before but
    # weren't seen in this pass. Mirrors ``_sync_github_repo`` so
    # the marketplace tab hides skills the upstream no longer
    # lists. Skipped when we found nothing at all so a totally
    # empty pass doesn't wipe existing data.
    if seen_names:
        stale = (
            db.query(ExternalSkill)
            .filter(
                ExternalSkill.source_id == source.id,
                ~ExternalSkill.name.in_(seen_names),
            )
            .all()
        )
        for s in stale:
            s.is_deleted = True

    db.commit()
    return {"success": True, "skill_count": len(seen_names), "error": None}


async def _sync_web_page(source: SkillSource, db: Session) -> dict:
    """Sync a web page source.

    Primary path: browse the site with agent-browser, harvest every
    GitHub link, and turn each link into a real ``ExternalSkill``
    row by fetching the underlying ``SKILL.md`` from GitHub. See
    :func:`_collect_skills_from_github_links` for the per-link
    resolution logic and the ``MAX_SKILLS_PER_WEBSITE`` cap.

    Fallback: when the crawl finds no GitHub links, fall back to
    the legacy single-skill LLM extraction. This preserves the
    documentation-page use case (e.g. a single API reference page
    that doesn't link to GitHub) and matches the pre-2026-07-30
    behavior exactly.
    """
    from app.services.website_skill_crawler import crawl_site_for_github_links

    try:
        page_links = await crawl_site_for_github_links(
            source.url, max_pages=MAX_PAGES_PER_CRAWL
        )
    except Exception:
        logger.exception("Website crawl failed for %s", source.url)
        page_links = {}

    has_github_links = any(links for links in page_links.values())
    if has_github_links:
        return await _collect_skills_from_github_links(source, db, page_links)

    # Fallback: keep the original single-skill LLM extraction.
    from app.services.tool_handlers.agent_browser_tool import _agent_browser
    from app.services.llm_service import call_llm

    extract_result = await _agent_browser({"action": "extract", "url": source.url})
    if not extract_result.get("success"):
        return {"success": False, "skill_count": 0, "error": extract_result.get("error", "Extraction failed")}
    page_text = extract_result.get("text", "")
    if len(page_text) < 100:
        return {"success": False, "skill_count": 0, "error": "Page content too short"}

    llm_result = await call_llm(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Extract a skill from this page. Return JSON with keys: name, description, body.\n\n{page_text[:8000]}"}],
        response_format={"type": "json_object"},
    )
    skill_data = (llm_result.get("data") or {})
    name = skill_data.get("name", "scraped-skill")
    body = skill_data.get("body", "")
    if not body:
        return {"success": False, "skill_count": 0, "error": "LLM returned empty body"}

    skill_md = f"## Overview\n\n{skill_data.get('description', '')}\n\n{body}"
    existing = db.query(ExternalSkill).filter(
        ExternalSkill.source_id == source.id,
        ExternalSkill.name == name,
    ).first()
    if existing:
        existing.description = skill_data.get("description", "")
        existing.skill_md = skill_md
        existing.last_synced_at = datetime.now(timezone.utc)
    else:
        db.add(ExternalSkill(
            source_id=source.id,
            name=name,
            display_name=name,
            description=skill_data.get("description", ""),
            category="scraped",
            version="1.0.0",
            skill_md=skill_md,
            source_url=source.url,
            last_synced_at=datetime.now(timezone.utc),
        ))
    db.commit()
    return {"success": True, "skill_count": 1, "error": None}


async def sync_source(source_id: str, db: Optional[Session] = None) -> dict:
    """Sync a skill source. Updates last_sync_status on the source row.

    Accepts an optional session so background tasks (kicked off via
    ``asyncio.create_task``) can pass ``None`` and get their own
    request-independent session — the original FastAPI request session
    is closed the moment the request returns, so reusing it from a
    background task races with the close and silently drops the write.
    """
    own_session = db is None
    if own_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        source = db.query(SkillSource).filter(SkillSource.id == source_id).first()
        if not source:
            return {"success": False, "skill_count": 0, "error": "Source not found"}

        source.last_sync_status = "syncing"
        db.commit()

        try:
            if source.source_type == "github_repo":
                result = await _sync_github_repo(source, db)
            elif source.source_type == "web_index":
                result = await _sync_web_index(source, db)
            else:
                result = await _sync_web_page(source, db)

            if result["success"]:
                source.last_sync_status = "success"
                source.last_synced_at = datetime.now(timezone.utc)
                source.last_sync_error = None
                source.skill_count = result["skill_count"]
            else:
                source.last_sync_status = "failed"
                source.last_sync_error = result.get("error", "Unknown error")
            db.commit()
            return result
        except Exception as exc:
            logger.exception("Sync failed for source %s", source_id)
            source.last_sync_status = "failed"
            source.last_sync_error = str(exc)[:500]
            db.commit()
            return {"success": False, "skill_count": 0, "error": str(exc)}
    finally:
        if own_session:
            db.close()


def get_curated_sources_needing_sync(db: Session) -> list:
    """Return curated (default) sources that need a sync trigger.

    The marketplace tab shows source cards with a skill count; the count is
    0 until the source is synced. Without a trigger, the user sees "0
    skills" until they click Sync manually. This helper is the trigger —
    the router calls it on every list and kicks off an async sync for
    each ID, so the cards populate automatically after the first
    marketplace tab visit (or after backend startup, whichever comes
    first).

    Sources in "never" (newly seeded) AND "failed" (last attempt didn't
    work — network blip, GitHub rate limit, etc.) both qualify. We don't
    re-trigger "success" or "syncing" sources — the first because
    there's no work to do, the second because the in-flight task will
    update the row when it finishes.
    """
    from app.models.skill_source import SkillSource
    return db.query(SkillSource).filter(
        SkillSource.is_default == True,
        SkillSource.is_hidden == False,
        SkillSource.last_sync_status.in_(["never", "failed"]),
    ).all()


def seed_curated_sources(db: Session) -> int:
    """Seed curated default sources if none exist. Returns count created.

    Also backfills ``brand_color`` and ``icon_emoji`` on existing sources
    that predate the columns (the auto-migration in ``main.py`` only adds
    the columns — it can't pick reasonable per-row defaults).
    """
    # Backfill defaults on existing rows so every source card has a color +
    # glyph even if the user installed it before the brand fields existed.
    existing_rows = db.query(SkillSource).filter(
        (SkillSource.brand_color.is_(None)) | (SkillSource.icon_emoji.is_(None))
    ).all()
    for src in existing_rows:
        if not src.brand_color:
            src.brand_color = DEFAULT_BRAND_COLOR
        if not src.icon_emoji:
            # First letter of name as a fallback glyph — works in any font
            # and avoids guessing per-source emojis. Curated sources get
            # explicit values on first seed.
            src.icon_emoji = (src.name[:1] or "?").upper()

    existing = db.query(SkillSource).filter(SkillSource.is_default == True).count()
    if existing > 0:
        if existing_rows:
            db.commit()
        return 0
    created = 0
    # User-removed URL tombstones. When the user hard-deletes a
    # curated source, we record the URL here so the seed won't
    # re-create it on subsequent runs. The user can clear the
    # tombstone to restore the source.
    from app.models.removed_curated_url import RemovedCuratedUrl
    removed_urls = {
        row.url for row in db.query(RemovedCuratedUrl).all()
    }
    for src_def in CURATED_SOURCES:
        already = db.query(SkillSource).filter(SkillSource.url == src_def["url"]).first()
        if already:
            continue
        if src_def["url"] in removed_urls:
            # The user explicitly removed this catalog. Don't re-seed.
            continue
        db.add(SkillSource(
            name=src_def["name"],
            url=src_def["url"],
            source_type=src_def["source_type"],
            description=src_def["description"],
            is_default=True,
            last_sync_status="never",
            brand_color=src_def.get("brand_color") or DEFAULT_BRAND_COLOR,
            icon_emoji=src_def.get("icon_emoji") or DEFAULT_ICON_EMOJI_PREFIX,
        ))
        created += 1
    if created:
        db.commit()
    return created
