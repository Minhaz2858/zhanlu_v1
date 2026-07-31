# Skill Marketplace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-source Skill Marketplace at `/marketplace` that aggregates skills from external websites (GitHub repos, JSON indexes, web pages), organized by source tabs, with browse/select/install-to-runtime workflow and a "My Skills" personal collection.

**Architecture:** Two new DB tables (`SkillSource`, `ExternalSkill`) cache a synced catalog of external skills. A `SkillSourceService` handles auto-detection + sync per source type (GitHub API / JSON fetch / agent-browser scrape). The marketplace router (10 endpoints) replaces the old community-publish router. The frontend rewrites `Marketplace.jsx` with a tab-per-source layout (CodeBuddy Plugins style), Add Marketplace dialog, and My Skills tab. Installing a skill writes it to the filesystem via `skill_sync.write_skill_md` + creates a Tool row, making it immediately runtime-available.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React + Tailwind + lucide-react (frontend), pytest (backend tests), vitest + @testing-library/react (frontend tests)

**Spec:** `docs/superpowers/specs/2026-07-29-skill-marketplace-design.md`

## Global Constraints

- Test DB: `sqlite:///./test_runtime.db` — rebuild with `Base.metadata.create_all(engine)` before tests. Run specific test files only (RAM-limited box, no `-n` workers).
- All models inherit `TimestampedBase` (provides `id`, `created_date`, `updated_date`, `created_by_id`, `is_deleted`, `org_id`, `app_id`).
- Auth dependency: `get_current_user_required` from `app.deps`. DB dependency: `get_db` from `app.deps`.
- Frontend API calls use `authFetch` from `@/lib/authFetch` (handles 401 refresh-retry).
- Frontend i18n: `useLanguage()` from `@/lib/LanguageProvider` returns `{ t }` with `t.sidebar.*`, `t.marketplace.*` keys.
- `write_skill_md(name, description, body, category, trigger, version, author, tags, summary, runtime)` writes to `~/.zhanlu/skills/<category>/<name>/SKILL.md` and returns the path. Patches `USER_SKILLS_DIR` in tests.
- Existing route `/marketplace` already registered in `App.jsx` (line 98) with lazy import (line 33). Sidebar does NOT have a `/marketplace` entry yet — must add one.
- Sidebar nav item format: `{ to: '/path', label: t.sidebar.xxx, icon: IconComponent }`.

---

## File Structure

### Backend (new)
| File | Responsibility |
|------|---------------|
| `app/models/skill_source.py` | `SkillSource` model — registered external sources |
| `app/models/external_skill.py` | `ExternalSkill` model — cached skill catalog entries |
| `app/services/skill_source_service.py` | `SkillSourceService`: auto-detect type, sync (github/web_index/web_page), CRUD |
| `app/routers/marketplace.py` | **Rewrite**: 10 endpoints replacing old community-publish router |

### Backend (modify)
| File | Change |
|------|--------|
| `app/models/__init__.py` | Register `SkillSource`, `ExternalSkill` |
| `app/services/skill_sync.py` | Add `delete_skill_md(name, category)` function |

### Frontend (new)
| File | Responsibility |
|------|---------------|
| `src/api/marketplace.js` | API client: 10 endpoints via authFetch |
| `src/components/marketplace/SkillCard.jsx` | Skill card (name, desc, category, tags, Add/Added button) |
| `src/components/marketplace/SkillDetailSheet.jsx` | Full skill_md preview + install confirmation |
| `src/components/marketplace/SourceTab.jsx` | Source header + skill grid for one source tab |
| `src/components/marketplace/AddMarketplaceDialog.jsx` | Add source dialog with sync progress |
| `src/components/marketplace/MySkillsTab.jsx` | Installed skills grid with actions |

### Frontend (modify)
| File | Change |
|------|--------|
| `src/pages/Marketplace.jsx` | **Rewrite**: tab bar + source tabs + My Skills tab |
| `src/components/Sidebar.jsx` | Add `/marketplace` nav entry |

### Tests (new)
| File | Scope |
|------|-------|
| `backend/tests/test_skill_source_service.py` | Auto-detect, 3 sync types, error handling, upsert, stale cleanup |
| `backend/tests/test_skill_marketplace_api.py` | All 10 endpoints, auth, install/remove, curated seeding |
| `backend/tests/test_skill_marketplace_install.py` | Install writes SKILL.md + Tool, reload registry, idempotent, remove |
| `frontend/src/components/marketplace/SkillCard.test.jsx` | Card render, Add→Added state, provenance badge |
| `frontend/src/components/marketplace/AddMarketplaceDialog.test.jsx` | Dialog flow, auto-detect, sync progress |
| `frontend/src/pages/Marketplace.tabs.test.jsx` | Tab bar, switching, source header, My Skills tab |

---

## Task 1: Backend Models — SkillSource + ExternalSkill

**Files:**
- Create: `app/models/skill_source.py`
- Create: `app/models/external_skill.py`
- Modify: `app/models/__init__.py`
- Test: `backend/tests/test_skill_source_models.py`

**Interfaces:**
- Produces: `SkillSource` model (table `skill_sources`), `ExternalSkill` model (table `external_skills`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_skill_source_models.py
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
                              description="d", category="c", version="1", skill_md="x")
        db.add(skill)
        db.commit()

        db.delete(src)
        db.commit()

        remaining = db.query(ExternalSkill).filter(ExternalSkill.source_id == src.id).first()
        assert remaining is None
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_source_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.skill_source'`

- [ ] **Step 3: Write the SkillSource model**

```python
# app/models/skill_source.py
"""SkillSource model — registered external skill sources (GitHub repos, JSON indexes, web pages)."""

from sqlalchemy import String, Text, Integer, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional
from datetime import datetime

from app.models.base import TimestampedBase


class SkillSource(TimestampedBase):
    __tablename__ = "skill_sources"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # github_repo | web_index | web_page
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    added_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(20), default="never", nullable=False)
    last_sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

- [ ] **Step 4: Write the ExternalSkill model**

```python
# app/models/external_skill.py
"""ExternalSkill model — cached skill catalog entries from external sources."""

from sqlalchemy import String, Text, Integer, JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional
from datetime import datetime

from app.models.base import TimestampedBase


class ExternalSkill(TimestampedBase):
    __tablename__ = "external_skills"

    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill_sources.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="general")
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    skill_md: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    github_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    install_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 5: Register models in `__init__.py`**

Add to `app/models/__init__.py`:
```python
from app.models.skill_source import SkillSource
from app.models.external_skill import ExternalSkill
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_source_models.py -v`
Expected: 3 PASSED

- [ ] **Step 7: Commit**

```bash
git add app/models/skill_source.py app/models/external_skill.py app/models/__init__.py tests/test_skill_source_models.py
git commit -m "feat: add SkillSource and ExternalSkill models for marketplace aggregator"
```

---

## Task 2: skill_sync.delete_skill_md

**Files:**
- Modify: `app/services/skill_sync.py`
- Test: `backend/tests/test_skill_delete.py`

**Interfaces:**
- Consumes: `USER_SKILLS_DIR` (module-level Path in skill_sync.py), `reload_skills_registry()` from skills_loader
- Produces: `delete_skill_md(name: str, category: str = "marketplace") -> bool` — removes the skill directory + reloads registry

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_skill_delete.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import shutil, tempfile
from pathlib import Path
import pytest

from app.database import Base, engine
import app.models  # noqa
Base.metadata.create_all(engine)

@pytest.fixture
def isolated_skills_dir(monkeypatch):
    temp_dir = Path(tempfile.mkdtemp(prefix="delete_test_"))
    import app.services.skill_sync as ss
    monkeypatch.setattr(ss, "USER_SKILLS_DIR", temp_dir)
    import app.services.skills_loader as sl
    monkeypatch.setattr(sl, "_registry", None)
    monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)

def test_delete_skill_md_removes_file(isolated_skills_dir):
    from app.services.skill_sync import write_skill_md, delete_skill_md
    path = write_skill_md(name="to-delete", description="d", body="## Overview\n\nx", category="marketplace")
    assert Path(path).exists()
    result = delete_skill_md(name="to-delete", category="marketplace")
    assert result is True
    assert not Path(path).exists()

def test_delete_skill_md_returns_false_for_missing(isolated_skills_dir):
    from app.services.skill_sync import delete_skill_md
    result = delete_skill_md(name="never-existed", category="marketplace")
    assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_delete.py -v`
Expected: FAIL with `ImportError: cannot import name 'delete_skill_md'`

- [ ] **Step 3: Implement delete_skill_md**

Add to `app/services/skill_sync.py` (after the `write_skill_md` function):

```python
def delete_skill_md(name: str, category: str = "marketplace") -> bool:
    """Remove a skill's directory from ``~/.zhanlu/skills/<category>/<name>/``.

    Returns True if the directory existed and was removed, False otherwise.
    Reloads the skills registry after deletion so the skill is no longer
    discoverable by runtime agents.
    """
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", name).strip("-").lower()
    safe_category = re.sub(r"[^a-zA-Z0-9_-]", "-", category).strip("-").lower()
    skill_dir = USER_SKILLS_DIR / safe_category / safe_name
    if not skill_dir.exists():
        return False
    shutil.rmtree(skill_dir, ignore_errors=True)
    try:
        reload_skills_registry()
    except Exception:
        pass
    return True
```

Ensure `shutil` and `re` are imported at the top of `skill_sync.py` (they likely already are for `write_skill_md`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_delete.py -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/services/skill_sync.py tests/test_skill_delete.py
git commit -m "feat: add delete_skill_md to skill_sync for marketplace remove flow"
```

---

## Task 3: SkillSourceService — Auto-Detect + Sync Pipeline

**Files:**
- Create: `app/services/skill_source_service.py`
- Test: `backend/tests/test_skill_source_service.py`

**Interfaces:**
- Consumes: `SkillSource`, `ExternalSkill` models (Task 1); `call_llm` from `app.services.llm_service`; agent_browser `_agent_browser` from `app.services.tool_handlers.agent_browser_tool`
- Produces:
  - `detect_source_type(url: str) -> str` — returns `"github_repo" | "web_index" | "web_page"`
  - `async sync_source(source_id: str, db: Session) -> dict` — syncs a source, returns `{"success": bool, "skill_count": int, "error": str | None}`
  - `seed_curated_sources(db: Session) -> int` — seeds default sources if none exist, returns count created
  - `CURATED_SOURCES` — list of default source definitions

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_skill_source_service.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from unittest.mock import AsyncMock, patch
from app.database import Base, engine, SessionLocal
import app.models  # noqa
Base.metadata.create_all(engine)

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
            mock_contents = [
                {"content": "LS0tCm5hbWU6IG15LXNraWxsCmRlc2NyaXB0aW9uOiBBIHRlc3Qgc2tpbGwKdmVyc2lvbjogMS4wLjAKY2F0ZWdvcnk6IGRhdGEKYXV0aG9yOiB0ZXN0ZXIKdGFnczoKICAtIGRhdGEKICAtIGFuYWx5c2lzCi0tLQojIFRoZSBza2lsbCBib2R5", "encoding": "base64"},
                {"content": "LS0tCm5hbWU6IG90aGVyLXNraWxsCmRlc2NyaXB0aW9uOiBPdGhlciBza2lsbAp2ZXJzaW9uOiAyLjAuMApjYXRlZ29yeTogdXRpbHMKLS0tCiMgT3RoZXIgYm9keQ==", "encoding": "base64"},
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
                mock_client.get = AsyncMock(return_value=AsyncMock(status_code=200, json=lambda: mock_index))
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_source_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement SkillSourceService**

```python
# app/services/skill_source_service.py
"""SkillSourceService — auto-detect source type, sync external skill catalogs."""

import asyncio
import base64
import logging
import re
from datetime import datetime, timezone
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
    },
    {
        "name": "Awesome Claude Code Skills",
        "url": "https://github.com/hesreallyhim/awesome-claude-code",
        "source_type": "github_repo",
        "description": "Community-curated collection of Claude Code skills",
    },
]


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
        if ":" in line:
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


async def _sync_github_repo(source: SkillSource, db: Session) -> dict:
    """Sync a GitHub repo source by listing the tree and fetching SKILL.md files."""
    owner, repo, branch = _parse_github_url(source.url)
    github_token = os.environ.get("GITHUB_TOKEN")
    headers = {"Authorization": f"token {github_token}"} if github_token else {}

    async with httpx.AsyncClient(timeout=30) as client:
        tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        resp = await client.get(tree_url, headers=headers)
        if resp.status_code == 404:
            return {"success": False, "skill_count": 0, "error": "Repository or branch not found"}
        if resp.status_code == 403:
            return {"success": False, "skill_count": 0, "error": "GitHub API rate limit exceeded. Set GITHUB_TOKEN env var."}
        resp.raise_for_status()
        tree = resp.json()

        skill_paths = [t["path"] for t in tree.get("tree", []) if t["path"].endswith("SKILL.md")]
        if not skill_paths:
            return {"success": False, "skill_count": 0, "error": "No SKILL.md files found in repository"}

        seen_names = set()
        for path in skill_paths:
            content_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            cresp = await client.get(content_url, headers=headers)
            if cresp.status_code != 200:
                logger.warning("Failed to fetch %s: %s", path, cresp.status_code)
                continue
            cdata = cresp.json()
            raw = base64.b64decode(cdata.get("content", "")).decode("utf-8", errors="replace")
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name") or Path(path).parent.name
            seen_names.add(name)

            existing = db.query(ExternalSkill).filter(
                ExternalSkill.source_id == source.id,
                ExternalSkill.name == name,
            ).first()
            github_skill_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"
            if existing:
                existing.display_name = meta.get("name", name)
                existing.description = meta.get("description", "")
                existing.summary = meta.get("description", "")[:200]
                existing.category = meta.get("category", "general")
                existing.version = meta.get("version", "1.0.0")
                existing.author = meta.get("author")
                existing.skill_md = raw
                existing.tags = meta.get("tags", [])
                existing.source_url = github_skill_url
                existing.github_url = github_skill_url
                existing.last_synced_at = datetime.now(timezone.utc)
            else:
                db.add(ExternalSkill(
                    source_id=source.id,
                    name=name,
                    display_name=meta.get("name", name),
                    description=meta.get("description", ""),
                    summary=meta.get("description", "")[:200],
                    category=meta.get("category", "general"),
                    version=meta.get("version", "1.0.0"),
                    author=meta.get("author"),
                    skill_md=raw,
                    tags=meta.get("tags", []),
                    source_url=github_skill_url,
                    github_url=github_skill_url,
                    last_synced_at=datetime.now(timezone.utc),
                ))

        # Mark stale entries as deleted
        stale = db.query(ExternalSkill).filter(
            ExternalSkill.source_id == source.id,
            ~ExternalSkill.name.in_(seen_names) if seen_names else True,
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


async def _sync_web_page(source: SkillSource, db: Session) -> dict:
    """Sync a web page source by scraping via agent-browser + LLM (single-skill extraction)."""
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


async def sync_source(source_id: str, db: Session) -> dict:
    """Sync a skill source. Updates last_sync_status on the source row."""
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


def seed_curated_sources(db: Session) -> int:
    """Seed curated default sources if none exist. Returns count created."""
    existing = db.query(SkillSource).filter(SkillSource.is_default == True).count()
    if existing > 0:
        return 0
    created = 0
    for src_def in CURATED_SOURCES:
        already = db.query(SkillSource).filter(SkillSource.url == src_def["url"]).first()
        if already:
            continue
        db.add(SkillSource(
            name=src_def["name"],
            url=src_def["url"],
            source_type=src_def["source_type"],
            description=src_def["description"],
            is_default=True,
            last_sync_status="never",
        ))
        created += 1
    if created:
        db.commit()
    return created
```

Add `from pathlib import Path` and `import os` to the imports at the top.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_source_service.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/services/skill_source_service.py tests/test_skill_source_service.py
git commit -m "feat: add SkillSourceService with auto-detect + 3 sync types + curated seeding"
```

---

## Task 4: Marketplace Router — 10 Endpoints

**Files:**
- Modify (rewrite): `app/routers/marketplace.py`
- Test: `backend/tests/test_skill_marketplace_api.py`

**Interfaces:**
- Consumes: `SkillSource`, `ExternalSkill` models (Task 1); `SkillSourceService` (Task 3); `write_skill_md`, `delete_skill_md` from `skill_sync` (Task 2); `reload_skills_registry` from `skills_loader`; `Tool` model; `get_db`, `get_current_user_required` from `app.deps`
- Produces: `router` (APIRouter with 10 endpoints under `/api/marketplace/*`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_skill_marketplace_api.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
import app.models  # noqa
Base.metadata.create_all(engine)

@pytest.fixture
def client():
    from app.main import app
    # Bypass auth for tests
    from app.deps import get_current_user_required
    app.dependency_overrides[get_current_user_required] = lambda: type("U", (), {"id": "test-user"})()
    yield TestClient(app)
    app.dependency_overrides.clear()

@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()

class TestSources:
    def test_list_sources_seeds_curated(self, client, db):
        resp = client.get("/api/marketplace/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert len(data["sources"]) >= 1  # curated seeded

    def test_add_source(self, client, db):
        resp = client.post("/api/marketplace/sources", json={
            "url": "https://github.com/test/new-repo",
            "name": "New Repo",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["url"] == "https://github.com/test/new-repo"
        assert data["source_type"] == "github_repo"

    def test_delete_user_source(self, client, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="ToDelete", url="https://example.com/del", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200

    def test_delete_default_source_hides(self, client, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="Default", url="https://example.com/default", source_type="github_repo", is_default=True)
        db.add(src)
        db.commit()
        db.refresh(src)
        resp = client.delete(f"/api/marketplace/sources/{src.id}")
        assert resp.status_code == 200
        db.refresh(src)
        assert src.is_hidden is True

    def test_sync_source(self, client, db):
        from app.models.skill_source import SkillSource
        src = SkillSource(name="Sync", url="https://example.com/sync", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        with patch("app.services.skill_source_service.sync_source", new_callable=AsyncMock, return_value={"success": True, "skill_count": 3, "error": None}):
            resp = client.post(f"/api/marketplace/sources/{src.id}/sync")
        assert resp.status_code == 202

    def test_list_source_skills(self, client, db):
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        src = SkillSource(name="WithSkills", url="https://example.com/ws", source_type="web_index")
        db.add(src)
        db.commit()
        db.refresh(src)
        db.add(ExternalSkill(source_id=src.id, name="sk1", display_name="SK1", description="d", category="c", version="1", skill_md="x"))
        db.commit()
        resp = client.get(f"/api/marketplace/sources/{src.id}/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["skills"][0]["name"] == "sk1"

class TestInstall:
    def test_install_skill(self, client, db, monkeypatch):
        import tempfile, shutil
        from pathlib import Path
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        temp_dir = Path(tempfile.mkdtemp())
        import app.services.skill_sync as ss
        monkeypatch.setattr(ss, "USER_SKILLS_DIR", temp_dir)
        import app.services.skills_loader as sl
        monkeypatch.setattr(sl, "_registry", None)
        monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))

        src = SkillSource(name="Install", url="https://example.com/install", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        skill = ExternalSkill(source_id=src.id, name="installable", display_name="Installable",
                              description="d", category="tools", version="1.0.0", skill_md="## Overview\n\nBody")
        db.add(skill)
        db.commit()
        db.refresh(skill)

        resp = client.post(f"/api/marketplace/skills/{skill.id}/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["skill_name"] == "installable"
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_install_idempotent(self, client, db, monkeypatch):
        import tempfile, shutil
        from pathlib import Path
        from app.models.skill_source import SkillSource
        from app.models.external_skill import ExternalSkill
        from app.models.tool import Tool
        temp_dir = Path(tempfile.mkdtemp())
        import app.services.skill_sync as ss
        monkeypatch.setattr(ss, "USER_SKILLS_DIR", temp_dir)
        import app.services.skills_loader as sl
        monkeypatch.setattr(sl, "_registry", None)
        monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))

        src = SkillSource(name="Idem", url="https://example.com/idem", source_type="web_page")
        db.add(src)
        db.commit()
        db.refresh(src)
        skill = ExternalSkill(source_id=src.id, name="idem-skill", display_name="Idem",
                              description="d", category="tools", version="1.0.0", skill_md="## Overview\n\nBody")
        db.add(skill)
        db.commit()
        db.refresh(skill)

        r1 = client.post(f"/api/marketplace/skills/{skill.id}/install")
        r2 = client.post(f"/api/marketplace/skills/{skill.id}/install")
        assert r1.json()["already_installed"] is False
        assert r2.json()["already_installed"] is True
        shutil.rmtree(temp_dir, ignore_errors=True)

class TestMySkills:
    def test_list_my_skills(self, client, db):
        from app.models.tool import Tool
        db.add(Tool(name="my-installed", kind="system_skill", source="external", created_by_id="test-user"))
        db.commit()
        resp = client.get("/api/marketplace/my-skills")
        assert resp.status_code == 200
        data = resp.json()
        assert any(s["name"] == "my-installed" for s in data["skills"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_marketplace_api.py -v`
Expected: FAIL (old router endpoints don't match)

- [ ] **Step 3: Rewrite the marketplace router**

```python
# app/routers/marketplace.py
"""Marketplace router — multi-source skill aggregator.

Replaces the old community-publish router. Endpoints:
  GET    /api/marketplace/sources               — list sources
  POST   /api/marketplace/sources               — add source
  DELETE /api/marketplace/sources/{id}          — remove/hide source
  POST   /api/marketplace/sources/{id}/sync     — trigger re-sync
  GET    /api/marketplace/sources/{id}/skills   — list skills for a source
  GET    /api/marketplace/skills/{id}           — external skill detail
  POST   /api/marketplace/skills/{id}/install   — install to My Skills (runtime)
  GET    /api/marketplace/my-skills             — list installed skills
  DELETE /api/marketplace/my-skills/{tool_id}   — remove from My Skills
  GET    /api/marketplace/curated               — list curated defaults
"""

import logging
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"], dependencies=[Depends(get_current_user_required)])


class AddSourceRequest(BaseModel):
    url: str
    name: Optional[str] = None
    source_type: Optional[str] = None
    description: Optional[str] = None


def _source_to_dict(src) -> dict:
    return {
        "id": src.id,
        "name": src.name,
        "url": src.url,
        "source_type": src.source_type,
        "description": src.description,
        "is_default": src.is_default,
        "is_hidden": src.is_hidden,
        "last_synced_at": src.last_synced_at.isoformat() if src.last_synced_at else None,
        "last_sync_status": src.last_sync_status,
        "last_sync_error": src.last_sync_error,
        "skill_count": src.skill_count,
    }


def _external_skill_to_dict(sk) -> dict:
    return {
        "id": sk.id,
        "source_id": sk.source_id,
        "name": sk.name,
        "display_name": sk.display_name,
        "description": sk.description,
        "summary": sk.summary,
        "category": sk.category,
        "version": sk.version,
        "author": sk.author,
        "tags": sk.tags or [],
        "source_url": sk.source_url,
        "github_url": sk.github_url,
        "install_count": sk.install_count,
    }


# ─── Sources ────────────────────────────────────────────────────────────

@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    from app.services.skill_source_service import seed_curated_sources
    from app.models.skill_source import SkillSource
    seed_curated_sources(db)
    sources = db.query(SkillSource).filter(SkillSource.is_hidden == False).order_by(SkillSource.is_default.desc(), SkillSource.created_date.desc()).all()
    return {"sources": [_source_to_dict(s) for s in sources]}


@router.post("/sources", status_code=201)
def add_source(req: AddSourceRequest, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.services.skill_source_service import detect_source_type, sync_source
    from app.models.skill_source import SkillSource

    existing = db.query(SkillSource).filter(SkillSource.url == req.url).first()
    if existing:
        raise HTTPException(409, detail={"message": "Source already registered", "source_id": existing.id})

    source_type = req.source_type or detect_source_type(req.url)
    name = req.name or req.url.split("//")[-1].split("/")[0]
    src = SkillSource(
        name=name,
        url=req.url,
        source_type=source_type,
        description=req.description,
        added_by=getattr(user, "id", None),
        is_default=False,
        last_sync_status="never",
    )
    db.add(src)
    db.commit()
    db.refresh(src)

    # Kick off async sync in background
    try:
        asyncio.create_task(sync_source(src.id, db))
    except RuntimeError:
        pass  # No event loop — sync will happen on first manual trigger

    return _source_to_dict(src)


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, db: Session = Depends(get_db)):
    from app.models.skill_source import SkillSource
    from app.models.external_skill import ExternalSkill
    src = db.query(SkillSource).filter(SkillSource.id == source_id).first()
    if not src:
        raise HTTPException(404, detail="Source not found")
    if src.is_default:
        src.is_hidden = True
        db.commit()
        return {"success": True, "hidden": True}
    db.query(ExternalSkill).filter(ExternalSkill.source_id == source_id).delete()
    db.delete(src)
    db.commit()
    return {"success": True}


@router.post("/sources/{source_id}/sync", status_code=202)
async def sync_source_endpoint(source_id: str, db: Session = Depends(get_db)):
    from app.services.skill_source_service import sync_source
    result = await sync_source(source_id, db)
    return {"status": "completed", **result}


@router.get("/sources/{source_id}/skills")
def list_source_skills(
    source_id: str,
    q: Optional[str] = None,
    sort: str = "name",
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    from app.models.external_skill import ExternalSkill
    query = db.query(ExternalSkill).filter(
        ExternalSkill.source_id == source_id,
        ExternalSkill.is_deleted == False,
    )
    if q:
        query = query.filter(ExternalSkill.name.ilike(f"%{q}%") | ExternalSkill.description.ilike(f"%{q}%"))
    if sort == "popular":
        query = query.order_by(ExternalSkill.install_count.desc())
    else:
        query = query.order_by(ExternalSkill.name.asc())
    total = query.count()
    skills = query.offset(offset).limit(limit).all()
    return {"skills": [_external_skill_to_dict(s) for s in skills], "count": total}


# ─── External Skill Detail + Install ────────────────────────────────────

@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, db: Session = Depends(get_db)):
    from app.models.external_skill import ExternalSkill
    sk = db.query(ExternalSkill).filter(ExternalSkill.id == skill_id).first()
    if not sk:
        raise HTTPException(404, detail="Skill not found")
    d = _external_skill_to_dict(sk)
    d["skill_md"] = sk.skill_md
    return d


@router.post("/skills/{skill_id}/install")
def install_skill(skill_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.external_skill import ExternalSkill
    from app.models.tool import Tool
    from app.services.skill_sync import write_skill_md
    from app.services.skills_loader import reload_skills_registry

    sk = db.query(ExternalSkill).filter(ExternalSkill.id == skill_id).first()
    if not sk:
        raise HTTPException(404, detail="Skill not found")

    user_id = getattr(user, "id", None)
    existing = db.query(Tool).filter(
        Tool.name == sk.name,
        Tool.source == "external",
        Tool.created_by_id == user_id,
    ).first()
    if existing:
        return {"success": True, "already_installed": True, "tool_id": existing.id, "skill_name": sk.name}

    write_skill_md(
        name=sk.name,
        description=sk.description,
        body=sk.skill_md,
        category="marketplace",
        version=sk.version,
        author=sk.author or "external",
        tags=sk.tags or [],
    )
    tool = Tool(
        name=sk.name,
        description=sk.description,
        kind="system_skill",
        category="marketplace",
        source="external",
        github_url=sk.github_url,
        version=sk.version,
        publisher=sk.author,
        skill_md=sk.skill_md,
        enabled=True,
        status="active",
        created_by_id=user_id,
    )
    db.add(tool)
    sk.install_count = (sk.install_count or 0) + 1
    db.commit()
    db.refresh(tool)
    try:
        reload_skills_registry()
    except Exception:
        pass
    return {"success": True, "already_installed": False, "tool_id": tool.id, "skill_name": sk.name}


# ─── My Skills ──────────────────────────────────────────────────────────

@router.get("/my-skills")
def list_my_skills(db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.tool import Tool
    user_id = getattr(user, "id", None)
    tools = db.query(Tool).filter(
        Tool.source == "external",
        Tool.created_by_id == user_id,
        Tool.is_deleted == False,
    ).order_by(Tool.created_date.desc()).all()
    return {"skills": [_tool_to_dict(t) for t in tools]}


@router.delete("/my-skills/{tool_id}")
def remove_my_skill(tool_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.tool import Tool
    from app.services.skill_sync import delete_skill_md
    from app.services.skills_loader import reload_skills_registry

    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(404, detail="Skill not found")
    user_id = getattr(user, "id", None)
    if tool.created_by_id != user_id:
        raise HTTPException(403, detail="Not your skill")

    delete_skill_md(tool.name, category="marketplace")
    tool.is_deleted = True
    db.commit()
    try:
        reload_skills_registry()
    except Exception:
        pass
    return {"success": True}


@router.get("/curated")
def list_curated():
    from app.services.skill_source_service import CURATED_SOURCES
    return {"sources": CURATED_SOURCES}


def _tool_to_dict(t) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "category": t.category,
        "version": t.version,
        "publisher": t.publisher,
        "github_url": t.github_url,
        "created_date": t.created_date.isoformat() if t.created_date else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_marketplace_api.py -v`
Expected: 9 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/routers/marketplace.py tests/test_skill_marketplace_api.py
git commit -m "feat: rewrite marketplace router with 10 source-aggregator endpoints"
```

---

## Task 5: Frontend API Client

**Files:**
- Create: `src/api/marketplace.js`

**Interfaces:**
- Consumes: `authFetch` from `@/lib/authFetch`
- Produces: `listSources`, `addSource`, `deleteSource`, `syncSource`, `listSourceSkills`, `getSkill`, `installSkill`, `listMySkills`, `removeMySkill`, `listCurated`

- [ ] **Step 1: Implement the API client**

```javascript
// src/api/marketplace.js
import { authFetch } from '@/lib/authFetch';

const BASE = '/api/marketplace';

export async function listSources() {
  const resp = await authFetch(`${BASE}/sources`);
  if (!resp.ok) throw new Error('Failed to list sources');
  return resp.json();
}

export async function addSource({ url, name, source_type, description }) {
  const resp = await authFetch(`${BASE}/sources`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, name, source_type, description }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Failed to add source');
  }
  return resp.json();
}

export async function deleteSource(sourceId) {
  const resp = await authFetch(`${BASE}/sources/${sourceId}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error('Failed to delete source');
  return resp.json();
}

export async function syncSource(sourceId) {
  const resp = await authFetch(`${BASE}/sources/${sourceId}/sync`, { method: 'POST' });
  if (!resp.ok) throw new Error('Failed to sync source');
  return resp.json();
}

export async function listSourceSkills(sourceId, { q, sort, limit, offset } = {}) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (sort) params.set('sort', sort);
  if (limit) params.set('limit', limit);
  if (offset) params.set('offset', offset);
  const resp = await authFetch(`${BASE}/sources/${sourceId}/skills?${params}`);
  if (!resp.ok) throw new Error('Failed to list skills');
  return resp.json();
}

export async function getSkill(skillId) {
  const resp = await authFetch(`${BASE}/skills/${skillId}`);
  if (!resp.ok) throw new Error('Failed to get skill');
  return resp.json();
}

export async function installSkill(skillId) {
  const resp = await authFetch(`${BASE}/skills/${skillId}/install`, { method: 'POST' });
  if (!resp.ok) throw new Error('Failed to install skill');
  return resp.json();
}

export async function listMySkills() {
  const resp = await authFetch(`${BASE}/my-skills`);
  if (!resp.ok) throw new Error('Failed to list my skills');
  return resp.json();
}

export async function removeMySkill(toolId) {
  const resp = await authFetch(`${BASE}/my-skills/${toolId}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error('Failed to remove skill');
  return resp.json();
}

export async function listCurated() {
  const resp = await authFetch(`${BASE}/curated`);
  if (!resp.ok) throw new Error('Failed to list curated');
  return resp.json();
}
```

- [ ] **Step 2: Commit**

```bash
git add src/api/marketplace.js
git commit -m "feat: add marketplace API client with 10 endpoints"
```

---

## Task 6: SkillCard + SkillDetailSheet Components

**Files:**
- Create: `src/components/marketplace/SkillCard.jsx`
- Create: `src/components/marketplace/SkillDetailSheet.jsx`
- Test: `src/components/marketplace/SkillCard.test.jsx`

**Interfaces:**
- Consumes: `installSkill` from `@/api/marketplace`
- Produces: `SkillCard` (props: `skill`, `isInstalled`, `onInstall`, `onOpenDetail`), `SkillDetailSheet` (props: `skill`, `open`, `onClose`, `onInstall`, `isInstalled`)

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/marketplace/SkillCard.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SkillCard from '@/components/marketplace/SkillCard';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { add: 'Add', added: 'Added' } } }),
}));

const SKILL = {
  id: 's1', name: 'data-viz', display_name: 'Data Viz',
  description: 'Create beautiful data visualizations',
  category: 'data', version: '1.0.0', author: 'tester',
  tags: ['charts', 'visualization'], source_url: 'https://example.com',
  install_count: 5,
};

describe('SkillCard', () => {
  it('renders skill name, description, category, and tags', () => {
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Data Viz')).toBeDefined();
    expect(screen.getByText('Create beautiful data visualizations')).toBeDefined();
    expect(screen.getByText('data')).toBeDefined();
    expect(screen.getByText('charts')).toBeDefined();
    expect(screen.getByText('visualization')).toBeDefined();
  });

  it('shows Add button when not installed', () => {
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Add')).toBeDefined();
  });

  it('shows Added badge when installed', () => {
    render(<SkillCard skill={SKILL} isInstalled={true} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    expect(screen.getByText('Added')).toBeDefined();
  });

  it('calls onInstall when Add button is clicked', () => {
    const onInstall = vi.fn();
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={onInstall} onOpenDetail={vi.fn()} />);
    fireEvent.click(screen.getByText('Add'));
    expect(onInstall).toHaveBeenCalledWith(SKILL);
  });

  it('calls onOpenDetail when card body is clicked', () => {
    const onOpenDetail = vi.fn();
    render(<SkillCard skill={SKILL} isInstalled={false} onInstall={vi.fn()} onOpenDetail={onOpenDetail} />);
    fireEvent.click(screen.getByText('Data Viz'));
    expect(onOpenDetail).toHaveBeenCalledWith(SKILL);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/SkillCard.test.jsx`
Expected: FAIL (component doesn't exist)

- [ ] **Step 3: Implement SkillCard**

```jsx
// src/components/marketplace/SkillCard.jsx
import { Check, Plus, ExternalLink } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function SkillCard({ skill, isInstalled, onInstall, onOpenDetail }) {
  const { t } = useLanguage();
  const tags = (skill.tags || []).slice(0, 3);

  return (
    <div className="group relative flex flex-col rounded-xl border border-slate-800 bg-slate-900/50 p-4 transition hover:border-slate-700 hover:bg-slate-900">
      <div className="flex items-start justify-between gap-2">
        <h3
          className="cursor-pointer text-sm font-semibold text-slate-100 hover:text-indigo-400"
          onClick={() => onOpenDetail?.(skill)}
        >
          {skill.display_name || skill.name}
        </h3>
        <span className="shrink-0 rounded-full bg-slate-800 px-2 py-0.5 text-[10px] text-slate-400">
          {skill.category}
        </span>
      </div>

      <p className="mt-1 line-clamp-2 text-xs text-slate-400">{skill.description}</p>

      {tags.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {tags.map((tag) => (
            <span key={tag} className="rounded bg-slate-800/60 px-1.5 py-0.5 text-[10px] text-slate-500">
              #{tag}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex items-center justify-between">
        <span className="text-[10px] text-slate-500">
          {skill.author ? `${skill.author} · ` : ''}v{skill.version}
        </span>
        <button
          onClick={() => onInstall?.(skill)}
          disabled={isInstalled}
          className={`inline-flex items-center gap-1 rounded-lg px-3 py-1 text-xs font-medium transition ${
            isInstalled
              ? 'cursor-default bg-emerald-500/10 text-emerald-400'
              : 'bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500/20'
          }`}
        >
          {isInstalled ? <Check className="h-3 w-3" /> : <Plus className="h-3 w-3" />}
          {isInstalled ? (t.marketplace?.added || 'Added') : (t.marketplace?.add || 'Add')}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Implement SkillDetailSheet**

```jsx
// src/components/marketplace/SkillDetailSheet.jsx
import { X, Check, Plus } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function SkillDetailSheet({ skill, open, onClose, onInstall, isInstalled }) {
  const { t } = useLanguage();
  if (!open || !skill) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-xl border border-slate-800 bg-slate-900 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">{skill.display_name || skill.name}</h2>
            <p className="mt-1 text-sm text-slate-400">{skill.description}</p>
            <div className="mt-2 flex gap-3 text-xs text-slate-500">
              <span>Category: {skill.category}</span>
              <span>Version: {skill.version}</span>
              {skill.author && <span>Author: {skill.author}</span>}
            </div>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-slate-500 hover:bg-slate-800">
            <X className="h-4 w-4" />
          </button>
        </div>

        <pre className="mt-4 max-h-[50vh] overflow-auto rounded-lg bg-slate-950 p-4 text-xs text-slate-300 whitespace-pre-wrap">
          {skill.skill_md || 'No preview available'}
        </pre>

        <div className="mt-4 flex justify-end">
          <button
            onClick={() => onInstall?.(skill)}
            disabled={isInstalled}
            className={`inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium transition ${
              isInstalled
                ? 'cursor-default bg-emerald-500/10 text-emerald-400'
                : 'bg-indigo-500 text-white hover:bg-indigo-600'
            }`}
          >
            {isInstalled ? <Check className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
            {isInstalled ? 'Added to My Skills' : 'Add to My Skills'}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/SkillCard.test.jsx`
Expected: 5 PASSED

- [ ] **Step 6: Commit**

```bash
git add src/components/marketplace/SkillCard.jsx src/components/marketplace/SkillDetailSheet.jsx src/components/marketplace/SkillCard.test.jsx
git commit -m "feat: add SkillCard and SkillDetailSheet marketplace components"
```

---

## Task 7: SourceTab Component

**Files:**
- Create: `src/components/marketplace/SourceTab.jsx`
- Test: `src/components/marketplace/SourceTab.test.jsx`

**Interfaces:**
- Consumes: `listSourceSkills`, `syncSource` from `@/api/marketplace`; `SkillCard` (Task 6)
- Produces: `SourceTab` (props: `source`, `installedNames` set, `onInstall`, `onOpenDetail`)

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/marketplace/SourceTab.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('@/api/marketplace', () => ({
  listSourceSkills: vi.fn(),
  syncSource: vi.fn(),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { sync: 'Sync', remove: 'Remove', hide: 'Hide', search: 'Search skills...' } } }),
}));

import SourceTab from '@/components/marketplace/SourceTab';
import { listSourceSkills, syncSource } from '@/api/marketplace';

const SOURCE = { id: 'src1', name: 'Test Source', url: 'https://github.com/test/repo', source_type: 'github_repo', skill_count: 2, last_sync_status: 'success', last_synced_at: '2026-07-29T10:00:00Z' };
const SKILLS = [
  { id: 's1', name: 'skill-a', display_name: 'Skill A', description: 'desc A', category: 'data', version: '1.0.0', tags: ['x'] },
  { id: 's2', name: 'skill-b', display_name: 'Skill B', description: 'desc B', category: 'tools', version: '2.0.0', tags: [] },
];

describe('SourceTab', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('renders source header with name, url, and skill count', async () => {
    listSourceSkills.mockResolvedValueOnce({ skills: SKILLS, count: 2 });
    render(<SourceTab source={SOURCE} installedNames={new Set()} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Test Source')).toBeDefined());
    expect(screen.getByText('https://github.com/test/repo')).toBeDefined();
    expect(screen.getByText(/2 skills/)).toBeDefined();
  });

  it('renders skill cards after load', async () => {
    listSourceSkills.mockResolvedValueOnce({ skills: SKILLS, count: 2 });
    render(<SourceTab source={SOURCE} installedNames={new Set()} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Skill A')).toBeDefined());
    expect(screen.getByText('Skill B')).toBeDefined();
  });

  it('shows Sync button and triggers sync on click', async () => {
    listSourceSkills.mockResolvedValue({ skills: SKILLS, count: 2 });
    syncSource.mockResolvedValueOnce({ success: true, skill_count: 2 });
    render(<SourceTab source={SOURCE} installedNames={new Set()} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Sync')).toBeDefined());
    fireEvent.click(screen.getByText('Sync'));
    await waitFor(() => expect(syncSource).toHaveBeenCalledWith('src1'));
  });

  it('shows empty state when no skills', async () => {
    listSourceSkills.mockResolvedValueOnce({ skills: [], count: 0 });
    render(<SourceTab source={SOURCE} installedNames={new Set()} onInstall={vi.fn()} onOpenDetail={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/No skills found/)).toBeDefined());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/SourceTab.test.jsx`
Expected: FAIL (component doesn't exist)

- [ ] **Step 3: Implement SourceTab**

```jsx
// src/components/marketplace/SourceTab.jsx
import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Globe, Trash2, EyeOff, Search } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { listSourceSkills, syncSource } from '@/api/marketplace';
import SkillCard from './SkillCard';

export default function SourceTab({ source, installedNames, onInstall, onOpenDetail }) {
  const { t } = useLanguage();
  const [skills, setSkills] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [search, setSearch] = useState('');

  const loadSkills = useCallback(async (q = '') => {
    setLoading(true);
    try {
      const data = await listSourceSkills(source.id, { q, sort: 'name', limit: 100 });
      setSkills(data.skills || []);
      setCount(data.count || 0);
    } catch (e) {
      setSkills([]);
    } finally {
      setLoading(false);
    }
  }, [source.id]);

  useEffect(() => { loadSkills(); }, [loadSkills]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      await syncSource(source.id);
      await loadSkills(search);
    } finally {
      setSyncing(false);
    }
  };

  const handleSearch = (e) => {
    setSearch(e.target.value);
    loadSkills(e.target.value);
  };

  return (
    <div>
      {/* Source header */}
      <div className="flex items-center justify-between border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <Globe className="h-4 w-4 text-indigo-400" />
          <span>{source.url}</span>
          <span className="text-slate-600">·</span>
          <span>{source.skill_count} skills</span>
          <span className="text-slate-600">·</span>
          <span>{source.last_sync_status === 'success' ? `synced ${new Date(source.last_synced_at || Date.now()).toLocaleDateString()}` : source.last_sync_status}</span>
        </div>
        <div className="flex gap-2">
          <button onClick={handleSync} disabled={syncing} className="inline-flex items-center gap-1 rounded-lg px-3 py-1 text-xs text-slate-400 hover:bg-slate-800">
            <RefreshCw className={`h-3 w-3 ${syncing ? 'animate-spin' : ''}`} />
            {t.marketplace?.sync || 'Sync'}
          </button>
          {source.is_default ? (
            <button onClick={() => onDelete?.(source)} className="inline-flex items-center gap-1 rounded-lg px-3 py-1 text-xs text-slate-400 hover:bg-slate-800">
              <EyeOff className="h-3 w-3" /> {t.marketplace?.hide || 'Hide'}
            </button>
          ) : (
            <button onClick={() => onDelete?.(source)} className="inline-flex items-center gap-1 rounded-lg px-3 py-1 text-xs text-red-400 hover:bg-slate-800">
              <Trash2 className="h-3 w-3" /> {t.marketplace?.remove || 'Remove'}
            </button>
          )}
        </div>
      </div>

      {/* Search */}
      <div className="mt-3 flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
          <input
            value={search}
            onChange={handleSearch}
            placeholder={t.marketplace?.search || 'Search skills...'}
            className="w-full rounded-lg border border-slate-800 bg-slate-900 py-1.5 pl-8 pr-3 text-xs text-slate-300 placeholder:text-slate-600"
          />
        </div>
      </div>

      {/* Skill grid */}
      {loading ? (
        <div className="py-8 text-center text-sm text-slate-500">Loading skills...</div>
      ) : skills.length === 0 ? (
        <div className="py-8 text-center text-sm text-slate-500">No skills found in this source.</div>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {skills.map((sk) => (
            <SkillCard
              key={sk.id}
              skill={sk}
              isInstalled={installedNames.has(sk.name)}
              onInstall={onInstall}
              onOpenDetail={onOpenDetail}
            />
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/SourceTab.test.jsx`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/components/marketplace/SourceTab.jsx src/components/marketplace/SourceTab.test.jsx
git commit -m "feat: add SourceTab component with skill grid + sync + search"
```

---

## Task 8: AddMarketplaceDialog Component

**Files:**
- Create: `src/components/marketplace/AddMarketplaceDialog.jsx`
- Test: `src/components/marketplace/AddMarketplaceDialog.test.jsx`

**Interfaces:**
- Consumes: `addSource`, `syncSource`, `getSkill` from `@/api/marketplace`
- Produces: `AddMarketplaceDialog` (props: `open`, `onClose`, `onAdded(source)`)

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/marketplace/AddMarketplaceDialog.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('@/api/marketplace', () => ({
  addSource: vi.fn(),
  syncSource: vi.fn(),
  getSkill: vi.fn(),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { addMarketplace: 'Add Marketplace', url: 'URL', name: 'Name', add: 'Add', cancel: 'Cancel', done: 'Done' } } }),
}));

import AddMarketplaceDialog from '@/components/marketplace/AddMarketplaceDialog';
import { addSource } from '@/api/marketplace';

describe('AddMarketplaceDialog', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('renders inputs when open', () => {
    render(<AddMarketplaceDialog open={true} onClose={vi.fn()} onAdded={vi.fn()} />);
    expect(screen.getByPlaceholderText('https://github.com/owner/repo')).toBeDefined();
    expect(screen.getByText('Add Marketplace')).toBeDefined();
  });

  it('returns null when closed', () => {
    const { container } = render(<AddMarketplaceDialog open={false} onClose={vi.fn()} onAdded={vi.fn()} />);
    expect(container.innerHTML).toBe('');
  });

  it('submits URL and calls onAdded on success', async () => {
    const user = userEvent.setup();
    addSource.mockResolvedValueOnce({ id: 's1', name: 'Test', url: 'https://github.com/test/repo', source_type: 'github_repo', last_sync_status: 'syncing' });
    const onAdded = vi.fn();
    render(<AddMarketplaceDialog open={true} onClose={vi.fn()} onAdded={onAdded} />);

    await user.type(screen.getByPlaceholderText('https://github.com/owner/repo'), 'https://github.com/test/repo');
    await user.click(screen.getByText('Add'));

    await waitFor(() => expect(addSource).toHaveBeenCalledWith({ url: 'https://github.com/test/repo', name: 'github.com' }));
  });

  it('shows error on API failure', async () => {
    const user = userEvent.setup();
    addSource.mockRejectedValueOnce(new Error('Source already registered'));
    render(<AddMarketplaceDialog open={true} onClose={vi.fn()} onAdded={vi.fn()} />);

    await user.type(screen.getByPlaceholderText('https://github.com/owner/repo'), 'https://github.com/dup/repo');
    await user.click(screen.getByText('Add'));

    await waitFor(() => expect(screen.getByText('Source already registered')).toBeDefined());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/AddMarketplaceDialog.test.jsx`
Expected: FAIL (component doesn't exist)

- [ ] **Step 3: Implement AddMarketplaceDialog**

```jsx
// src/components/marketplace/AddMarketplaceDialog.jsx
import { useState } from 'react';
import { X, Globe, Loader2, CheckCircle, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { addSource, syncSource } from '@/api/marketplace';

export default function AddMarketplaceDialog({ open, onClose, onAdded }) {
  const { t } = useLanguage();
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [addedSource, setAddedSource] = useState(null);

  if (!open) return null;

  const handleSubmit = async () => {
    if (!url.trim()) { setError('Please enter a URL'); return; }
    setLoading(true);
    setError('');
    try {
      const source = await addSource({ url: url.trim(), name: name.trim() || undefined, description: description.trim() || undefined });
      setAddedSource(source);
      // Wait for sync to complete (poll)
      for (let i = 0; i < 10; i++) {
        await new Promise(r => setTimeout(r, 2000));
        const syncResult = await syncSource(source.id);
        if (syncResult.status === 'completed') break;
      }
      onAdded?.(source);
    } catch (e) {
      setError(e.message || 'Failed to add source');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setUrl(''); setName(''); setDescription(''); setError(''); setAddedSource(null);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={handleClose}>
      <div className="w-full max-w-md rounded-xl border border-slate-800 bg-slate-900 p-6" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <Globe className="h-4 w-4 text-indigo-400" />
            {t.marketplace?.addMarketplace || 'Add Marketplace'}
          </h2>
          <button onClick={handleClose} className="rounded-lg p-1 text-slate-500 hover:bg-slate-800">
            <X className="h-4 w-4" />
          </button>
        </div>

        {addedSource ? (
          <div className="mt-4 flex flex-col items-center gap-3 py-6">
            <CheckCircle className="h-8 w-8 text-emerald-400" />
            <p className="text-sm text-slate-300">Source "{addedSource.name}" added successfully!</p>
            <button onClick={handleClose} className="rounded-lg bg-indigo-500 px-4 py-2 text-xs text-white hover:bg-indigo-600">
              {t.marketplace?.done || 'Done'}
            </button>
          </div>
        ) : (
          <div className="mt-4 space-y-3">
            <div>
              <label className="text-xs text-slate-400">{t.marketplace?.url || 'URL'}</label>
              <input
                value={url}
                onChange={e => setUrl(e.target.value)}
                placeholder="https://github.com/owner/repo"
                className="mt-1 w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-300 placeholder:text-slate-600"
              />
            </div>
            <div>
              <label className="text-xs text-slate-400">{t.marketplace?.name || 'Name'} (optional)</label>
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="My Custom Source"
                className="mt-1 w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-300 placeholder:text-slate-600"
              />
            </div>
            <div>
              <label className="text-xs text-slate-400">Description (optional)</label>
              <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                rows={2}
                className="mt-1 w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-300 placeholder:text-slate-600"
              />
            </div>

            {error && (
              <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
                <AlertCircle className="h-4 w-4 text-red-400" />
                <span className="text-xs text-red-300">{error}</span>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button onClick={handleClose} className="rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-800">
                {t.marketplace?.cancel || 'Cancel'}
              </button>
              <button
                onClick={handleSubmit}
                disabled={loading}
                className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-500 px-4 py-1.5 text-xs text-white hover:bg-indigo-600 disabled:opacity-50"
              >
                {loading && <Loader2 className="h-3 w-3 animate-spin" />}
                {loading ? 'Adding...' : (t.marketplace?.add || 'Add')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/AddMarketplaceDialog.test.jsx`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/components/marketplace/AddMarketplaceDialog.jsx src/components/marketplace/AddMarketplaceDialog.test.jsx
git commit -m "feat: add AddMarketplaceDialog with URL input + sync progress"
```

---

## Task 9: MySkillsTab Component

**Files:**
- Create: `src/components/marketplace/MySkillsTab.jsx`
- Test: `src/components/marketplace/MySkillsTab.test.jsx`

**Interfaces:**
- Consumes: `listMySkills`, `removeMySkill` from `@/api/marketplace`
- Produces: `MySkillsTab` (no props; self-contained)

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/marketplace/MySkillsTab.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('@/api/marketplace', () => ({
  listMySkills: vi.fn(),
  removeMySkill: vi.fn(),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { marketplace: { mySkills: 'My Skills', remove: 'Remove', openAgent: 'Open in Skill Agent', viewExecs: 'View Executions' } } }),
}));

import MySkillsTab from '@/components/marketplace/MySkillsTab';
import { listMySkills, removeMySkill } from '@/api/marketplace';

const INSTALLED = [
  { id: 't1', name: 'installed-skill', description: 'An installed skill', category: 'marketplace', version: '1.0.0', created_date: '2026-07-29T10:00:00Z' },
];

describe('MySkillsTab', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('renders installed skill cards', async () => {
    listMySkills.mockResolvedValueOnce({ skills: INSTALLED });
    render(<MySkillsTab />);
    await waitFor(() => expect(screen.getByText('installed-skill')).toBeDefined());
    expect(screen.getByText('An installed skill')).toBeDefined();
  });

  it('shows empty state when no skills', async () => {
    listMySkills.mockResolvedValueOnce({ skills: [] });
    render(<MySkillsTab />);
    await waitFor(() => expect(screen.getByText(/No skills yet/)).toBeDefined());
  });

  it('calls removeMySkill when Remove is clicked', async () => {
    listMySkills.mockResolvedValueOnce({ skills: INSTALLED });
    removeMySkill.mockResolvedValueOnce({ success: true });
    render(<MySkillsTab />);
    await waitFor(() => expect(screen.getByText('Remove')).toBeDefined());
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(removeMySkill).toHaveBeenCalledWith('t1'));
  });

  it('shows error state on API failure', async () => {
    listMySkills.mockRejectedValueOnce(new Error('Network error'));
    render(<MySkillsTab />);
    await waitFor(() => expect(screen.getByText('Network error')).toBeDefined());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/MySkillsTab.test.jsx`
Expected: FAIL

- [ ] **Step 3: Implement MySkillsTab**

```jsx
// src/components/marketplace/MySkillsTab.jsx
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Trash2, ExternalLink, BarChart3, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { listMySkills, removeMySkill } from '@/api/marketplace';

export default function MySkillsTab() {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listMySkills();
      setSkills(data.skills || []);
    } catch (e) {
      setError(e.message || 'Failed to load skills');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleRemove = async (skill) => {
    try {
      await removeMySkill(skill.id);
      setSkills(prev => prev.filter(s => s.id !== skill.id));
    } catch (e) {
      setError(e.message);
    }
  };

  if (loading) return <div className="py-8 text-center text-sm text-slate-500">Loading...</div>;
  if (error) return (
    <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
      <AlertCircle className="h-4 w-4 text-red-400" />
      <span className="text-xs text-red-300">{error}</span>
    </div>
  );

  if (skills.length === 0) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-slate-400">No skills yet — browse the sources above and add some!</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {skills.map((sk) => (
        <div key={sk.id} className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">{sk.name}</h3>
          <p className="mt-1 line-clamp-2 text-xs text-slate-400">{sk.description}</p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <button
              onClick={() => navigate(`/skill-agent?skill=${sk.name}`)}
              className="inline-flex items-center gap-1 rounded-lg bg-indigo-500/10 px-2 py-1 text-[10px] text-indigo-400 hover:bg-indigo-500/20"
            >
              <ExternalLink className="h-3 w-3" /> {t.marketplace?.openAgent || 'Open in Skill Agent'}
            </button>
            <button
              onClick={() => navigate(`/skills/executions?skill=${sk.name}`)}
              className="inline-flex items-center gap-1 rounded-lg bg-slate-800 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-700"
            >
              <BarChart3 className="h-3 w-3" /> {t.marketplace?.viewExecs || 'View Executions'}
            </button>
            <button
              onClick={() => handleRemove(sk)}
              className="inline-flex items-center gap-1 rounded-lg bg-red-500/10 px-2 py-1 text-[10px] text-red-400 hover:bg-red-500/20"
            >
              <Trash2 className="h-3 w-3" /> {t.marketplace?.remove || 'Remove'}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/MySkillsTab.test.jsx`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/components/marketplace/MySkillsTab.jsx src/components/marketplace/MySkillsTab.test.jsx
git commit -m "feat: add MySkillsTab with installed grid + remove + navigate actions"
```

---

## Task 10: Marketplace Page + Sidebar Entry

**Files:**
- Modify (rewrite): `src/pages/Marketplace.jsx`
- Modify: `src/components/Sidebar.jsx`
- Test: `src/pages/Marketplace.tabs.test.jsx`

**Interfaces:**
- Consumes: `listSources`, `listMySkills`, `installSkill`, `deleteSource` from `@/api/marketplace`; `SourceTab`, `MySkillsTab`, `AddMarketplaceDialog`, `SkillDetailSheet` (Tasks 6-9)
- Produces: `Marketplace` page component at `/marketplace`

- [ ] **Step 1: Write the failing test**

```jsx
// src/pages/Marketplace.tabs.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('@/api/marketplace', () => ({
  listSources: vi.fn(),
  listMySkills: vi.fn(),
  installSkill: vi.fn(),
  deleteSource: vi.fn(),
}));
vi.mock('react-router-dom', () => ({
  ...vi.importActual('react-router-dom'),
  useNavigate: () => vi.fn(),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { sidebar: { marketplace: 'Marketplace' }, marketplace: { title: 'Skill Marketplace', addMarketplace: 'Add Marketplace', mySkills: 'My Skills' } } }),
}));

import Marketplace from '@/pages/Marketplace';
import { listSources, listMySkills } from '@/api/marketplace';

const SOURCES = [
  { id: 's1', name: 'Anthropic Skills', url: 'https://github.com/anthropics/skills', source_type: 'github_repo', skill_count: 5, last_sync_status: 'success', last_synced_at: '2026-07-29T10:00:00Z', is_default: true },
  { id: 's2', name: 'Custom Source', url: 'https://example.com/index.json', source_type: 'web_index', skill_count: 2, last_sync_status: 'success', last_synced_at: '2026-07-29T09:00:00Z', is_default: false },
];

describe('Marketplace page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listSources.mockResolvedValue({ sources: SOURCES });
    listMySkills.mockResolvedValue({ skills: [] });
  });

  it('renders tab bar with source names + My Skills tab', async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText('Anthropic Skills')).toBeDefined());
    expect(screen.getByText('Custom Source')).toBeDefined();
    expect(screen.getByText('My Skills')).toBeDefined();
  });

  it('renders Add Marketplace button', async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText('Add Marketplace')).toBeDefined());
  });

  it('switches to My Skills tab when clicked', async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText('My Skills')).toBeDefined());
    fireEvent.click(screen.getByText('My Skills'));
    await waitFor(() => expect(listMySkills).toHaveBeenCalled());
  });

  it('opens AddMarketplaceDialog when Add Marketplace is clicked', async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText('Add Marketplace')).toBeDefined());
    fireEvent.click(screen.getByText('Add Marketplace'));
    // Dialog should show URL input
    await waitFor(() => expect(screen.getByPlaceholderText('https://github.com/owner/repo')).toBeDefined());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/Marketplace.tabs.test.jsx`
Expected: FAIL (old Marketplace component doesn't have tab structure)

- [ ] **Step 3: Rewrite Marketplace.jsx**

```jsx
// src/pages/Marketplace.jsx
import { useState, useEffect, useCallback } from 'react';
import { Store, Plus, X } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { listSources, listMySkills, installSkill, deleteSource } from '@/api/marketplace';
import SourceTab from '@/components/marketplace/SourceTab';
import MySkillsTab from '@/components/marketplace/MySkillsTab';
import AddMarketplaceDialog from '@/components/marketplace/AddMarketplaceDialog';
import SkillDetailSheet from '@/components/marketplace/SkillDetailSheet';

export default function Marketplace() {
  const { t } = useLanguage();
  const [sources, setSources] = useState([]);
  const [activeTab, setActiveTab] = useState(null); // source.id | 'my-skills'
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [installedNames, setInstalledNames] = useState(new Set());
  const [detailSkill, setDetailSkill] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadSources = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listSources();
      const srcs = data.sources || [];
      setSources(srcs);
      if (!activeTab && srcs.length > 0) {
        setActiveTab(srcs[0].id);
      }
    } catch (e) {
      console.error('Failed to load sources:', e);
    } finally {
      setLoading(false);
    }
  }, [activeTab]);

  const loadInstalled = useCallback(async () => {
    try {
      const data = await listMySkills();
      setInstalledNames(new Set((data.skills || []).map(s => s.name)));
    } catch (e) {
      // Non-critical
    }
  }, []);

  useEffect(() => { loadSources(); loadInstalled(); }, []);

  const handleInstall = async (skill) => {
    try {
      await installSkill(skill.id);
      setInstalledNames(prev => new Set([...prev, skill.name]));
    } catch (e) {
      console.error('Install failed:', e);
    }
  };

  const handleDeleteSource = async (source) => {
    try {
      await deleteSource(source.id);
      setSources(prev => prev.filter(s => s.id !== source.id));
      if (activeTab === source.id) {
        setActiveTab(sources[0]?.id || 'my-skills');
      }
    } catch (e) {
      console.error('Delete failed:', e);
    }
  };

  const handleSourceAdded = (newSource) => {
    setSources(prev => [...prev, newSource]);
    setActiveTab(newSource.id);
    setAddDialogOpen(false);
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-800 px-6 py-3">
        <h1 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <Store className="h-4 w-4 text-indigo-400" />
          {t.marketplace?.title || 'Skill Marketplace'}
        </h1>
        <button
          onClick={() => setAddDialogOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-500/10 px-3 py-1.5 text-xs text-indigo-400 hover:bg-indigo-500/20"
        >
          <Plus className="h-3.5 w-3.5" />
          {t.marketplace?.addMarketplace || 'Add Marketplace'}
        </button>
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b border-slate-800 px-6">
        {sources.map((src) => (
          <button
            key={src.id}
            onClick={() => setActiveTab(src.id)}
            className={`border-b-2 px-3 py-2 text-xs font-medium transition ${
              activeTab === src.id
                ? 'border-indigo-500 text-indigo-400'
                : 'border-transparent text-slate-500 hover:text-slate-300'
            }`}
          >
            {src.name}
            <span className="ml-1.5 rounded-full bg-slate-800 px-1.5 py-0.5 text-[9px] text-slate-400">
              {src.skill_count}
            </span>
          </button>
        ))}
        <button
          onClick={() => setActiveTab('my-skills')}
          className={`border-b-2 px-3 py-2 text-xs font-medium transition ${
            activeTab === 'my-skills'
              ? 'border-indigo-500 text-indigo-400'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          }`}
        >
          {t.marketplace?.mySkills || 'My Skills'}
        </button>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto p-6">
        {loading ? (
          <div className="py-8 text-center text-sm text-slate-500">Loading marketplace...</div>
        ) : activeTab === 'my-skills' ? (
          <MySkillsTab />
        ) : (
          sources.filter(s => s.id === activeTab).map(src => (
            <SourceTab
              key={src.id}
              source={src}
              installedNames={installedNames}
              onInstall={handleInstall}
              onOpenDetail={setDetailSkill}
              onDelete={handleDeleteSource}
            />
          ))
        )}
      </div>

      {/* Dialogs */}
      <AddMarketplaceDialog
        open={addDialogOpen}
        onClose={() => setAddDialogOpen(false)}
        onAdded={handleSourceAdded}
      />
      <SkillDetailSheet
        skill={detailSkill}
        open={!!detailSkill}
        onClose={() => setDetailSkill(null)}
        onInstall={(sk) => { handleInstall(sk); setDetailSkill(null); }}
        isInstalled={detailSkill ? installedNames.has(detailSkill.name) : false}
      />
    </div>
  );
}
```

- [ ] **Step 4: Add Sidebar entry**

In `src/components/Sidebar.jsx`, add after the `/toolkit` entry:

```javascript
{ to: '/marketplace', label: t.sidebar.marketplace || 'Marketplace', icon: Store },
```

Ensure `Store` is imported from `lucide-react` (it's already imported — used by the `/market` entry). If not, add it to the import.

Add the language key: in the language file (e.g., `src/lib/translations.js` or wherever `t.sidebar` is defined), add `marketplace: 'Marketplace'` to the `sidebar` section.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/Marketplace.tabs.test.jsx`
Expected: 4 PASSED

- [ ] **Step 6: Run full frontend test suite for marketplace**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/marketplace/ src/pages/Marketplace.tabs.test.jsx`
Expected: All marketplace tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/pages/Marketplace.jsx src/components/Sidebar.jsx src/pages/Marketplace.tabs.test.jsx
git commit -m "feat: rewrite Marketplace page with source tabs + My Skills + Add dialog"
```

- [ ] **Step 8: Build frontend**

Run: `cd /root/zhanlu/frontend && npm run build`
Expected: Build succeeds

- [ ] **Step 9: Deploy**

Run: `/opt/rebuild-zhanlu.sh --force`
Expected: Frontend deployed successfully

---

## Self-Review

### 1. Spec coverage

| Spec Section | Task(s) |
|--------------|---------|
| §3 Data Model (SkillSource, ExternalSkill) | Task 1 |
| §3.3 No changes to Tool model | Confirmed — Tasks 4 uses existing fields |
| §3.4 Deprecation of old tables | Task 4 (router rewrite, old tables untouched) |
| §4 Source-Type Auto-Detection | Task 3 (`detect_source_type`) |
| §4.2.1 GitHub repo sync | Task 3 (`_sync_github_repo`) |
| §4.2.2 Web index sync | Task 3 (`_sync_web_index`) |
| §4.2.3 Web page sync | Task 3 (`_sync_web_page`) |
| §4.3 Sync lifecycle | Task 4 (`sync_source` endpoint) |
| §4.4 Error handling | Task 3 (try/except + status tracking) |
| §5 API Surface (10 endpoints) | Task 4 |
| §6 Frontend UI Layout | Tasks 6-10 |
| §6.3 Skill card | Task 6 |
| §6.4 Add Marketplace dialog | Task 8 |
| §6.5 My Skills tab | Task 9 |
| §7 My Skills Install Flow | Task 4 (`install_skill` endpoint) |
| §8 Curated Defaults | Task 3 (`seed_curated_sources`, `CURATED_SOURCES`) |
| §9 Files to Create/Modify | All tasks cover listed files |
| §10 Migration Plan | Task 4 (router replace), Task 10 (page rewrite + sidebar) |
| §11 Out of Scope (v2) | Correctly excluded from all tasks |

### 2. Placeholder scan

No TBDs, TODOs, or "implement later" found. All steps have actual code.

### 3. Type consistency

- `detect_source_type` used in Task 3 + Task 4 (add_source endpoint) — consistent name.
- `sync_source` used in Task 3 (defined) + Task 4 (called) — consistent.
- `seed_curated_sources` used in Task 3 (defined) + Task 4 (list_sources calls it) — consistent.
- `write_skill_md` signature matches Task 4 install endpoint call.
- `delete_skill_md(name, category)` matches Task 2 definition + Task 4 remove endpoint call.
- `installSkill`, `listSources`, `listMySkills`, etc. — consistent across Tasks 5, 7, 8, 9, 10.
- `SkillCard` props (`skill`, `isInstalled`, `onInstall`, `onOpenDetail`) — consistent across Tasks 6, 7, 10.
