# Skill Marketplace — Multi-Source Aggregator Design

**Date**: 2026-07-29
**Status**: Approved (Approach A — Synced catalog with typed sources)
**Route**: `/marketplace` (replaces existing community marketplace page)

## 1. Goal

Build a Skill Marketplace that aggregates skills from various external websites (GitHub repos, JSON indexes, and arbitrary web pages). The marketplace is organized with tabs per source website; each tab shows all available skills from that source in a card grid (CodeBuddy Plugins style). Users browse, select skills, and add them to a personal "My Skills" collection — which installs the skill to runtime (filesystem + Tool row) so it's immediately available to all agents.

## 2. Key Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| Source management | Curated defaults + user-added (CodeBuddy-style "Add Marketplace") |
| Source formats | Structured (GitHub repo / JSON index) + web scraping (agent-browser + LLM) |
| Existing `/marketplace` page | Replace entirely (community-publish flow deprecated) |
| My Skills install | Install to runtime (write SKILL.md + Tool row, immediately agent-available) |

## 3. Data Model

### 3.1 `SkillSource` (new table `skill_sources`)

Stores registered external skill sources (one per website/repo/index).

| Field | Type | Notes |
|-------|------|-------|
| `id` | String(36) PK | UUID |
| `name` | String(255) | Display name, e.g. "Anthropic Skills" |
| `url` | String(2048), unique | The source URL |
| `source_type` | String(20) | `github_repo` \| `web_index` \| `web_page` (auto-detected) |
| `description` | Text, nullable | Optional user-provided description |
| `added_by` | String(36), FK→users.id | User who added (null for curated defaults) |
| `is_default` | Boolean, default false | True for curated sources (can't be hard-deleted) |
| `is_hidden` | Boolean, default false | User can hide default sources |
| `last_synced_at` | DateTime, nullable | Last successful sync timestamp |
| `last_sync_status` | String(20) | `syncing` \| `success` \| `failed` \| `never` |
| `last_sync_error` | Text, nullable | Error message if last sync failed |
| `skill_count` | Integer, default 0 | Denormalized count for tab badges |
| `+ TimestampedBase` | | `created_date`, `updated_date`, `is_deleted`, `org_id`, `app_id` |

**Constraints**: `UNIQUE(url)` — one registration per source URL. `is_default=true` sources can only be soft-hidden (`is_hidden=true`), not hard-deleted.

### 3.2 `ExternalSkill` (new table `external_skills`)

Cached catalog entries — one row per skill discovered in a source.

| Field | Type | Notes |
|-------|------|-------|
| `id` | String(36) PK | UUID |
| `source_id` | String(36), FK→skill_sources.id, CASCADE | Parent source |
| `name` | String(255) | Kebab-case skill name |
| `display_name` | String(255) | Original name from frontmatter |
| `description` | Text | Skill description |
| `summary` | String(200), nullable | First 200 chars of description |
| `category` | String(100) | Skill category |
| `version` | String(50) | Semantic version |
| `author` | String(255), nullable | Author from frontmatter |
| `skill_md` | Text | Full SKILL.md content (cached for instant card + install) |
| `tags` | JSON | List of tag strings |
| `source_url` | String(2048) | Exact URL/path within the source |
| `github_url` | String(2048), nullable | Link back to the skill's repo page (github sources) |
| `install_count` | Integer, default 0 | Denormalized for popularity sort |
| `last_synced_at` | DateTime | When this entry was last refreshed |
| `+ TimestampedBase` | | standard fields |

**Constraints**: `UNIQUE(source_id, name)` — one entry per skill per source.

### 3.3 No changes to existing `Tool` model

My Skills installs create Tool rows with `source="external"` and `category="marketplace"`, reusing the existing schema. No migration needed for the Tool table.

### 3.4 Deprecation of old tables

The existing `MarketplaceSkill` + `MarketplaceRating` tables remain in the DB (no destructive drop) but are no longer read/written by any code path. `test_marketplace.py` tests are replaced.

## 4. Source-Type Auto-Detection + Sync Pipeline

### 4.1 Auto-detection (on source-add)

```python
def _detect_source_type(url: str) -> str:
    if "github.com" in url:
        return "github_repo"
    if url.endswith(".json"):
        return "web_index"
    # Fallback: try fetching and check content-type
    return "web_page"
```

Users can override the auto-detected type in the Add Marketplace dialog.

### 4.2 `SkillSourceService.sync_source(source_id, db)` — async

Runs in background via `asyncio.create_task`. Updates `last_sync_status` → `"syncing"` at start, `"success"` or `"failed"` at end.

#### 4.2.1 `github_repo` sync

1. Parse `owner/repo` and optional `branch` (default `main`) from URL via regex.
2. `GET https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1` (GitHub API).
   - Optional `GITHUB_TOKEN` env var for higher rate limit (60/hr → 5000/hr).
   - 404 → source not found → `last_sync_error = "Repository not found"`.
   - 403 rate limit → `last_sync_error = "GitHub API rate limit exceeded"`.
3. Filter tree paths ending in `/SKILL.md` or `SKILL.md` (top-level or nested).
4. For each matching path:
   - `GET /repos/{owner}/{repo}/contents/{path}` → base64-decode `content`.
   - Parse YAML frontmatter (`name`, `description`, `category`, `version`, `author`, `tags`).
   - Body = everything after frontmatter.
   - Upsert `ExternalSkill` row (match on `source_id + name`).
5. Mark stale entries (present in DB but not in this sync's result set) as `is_deleted=true`.
6. Update `skill_count` on the source.

#### 4.2.2 `web_index` sync

1. `httpx.get(url)` with timeout (15s).
2. Parse JSON. Expected schema:
   ```json
   {
     "skills": [
       {
         "name": "my-skill",
         "description": "...",
         "skill_md": "## Overview\n...",   // OR
         "skill_url": "https://...",       // fetch lazily if skill_md absent
         "category": "data",
         "version": "1.0.0",
         "author": "...",
         "tags": ["a", "b"]
       }
     ]
   }
   ```
3. For entries with `skill_url` but no `skill_md`: fetch each (parallel via `asyncio.gather`, max 10 concurrent).
4. Upsert ExternalSkill rows; mark stale as deleted.

#### 4.2.3 `web_page` sync (single-skill extraction)

1. Reuse `SkillCollectionService._extract_page(url)` → agent-browser extract.
2. LLM structure → `{name, description, body}`.
3. Create/update one `ExternalSkill` row.
4. **v1 scope**: single-skill extraction only. Multi-page crawling (detecting a listing page and following links to individual skill pages) is deferred to v2.

### 4.3 Sync lifecycle

- **On add**: async sync triggered immediately after source creation.
- **Manual**: "Sync" button on source header → `POST /sources/{id}/sync`.
- **Lazy re-sync**: if `last_synced_at` is older than 24h when the source tab is opened, frontend triggers a background re-sync (non-blocking; shows stale data until sync completes).
- **Status polling**: frontend polls `GET /sources/{id}` every 3s while `last_sync_status === "syncing"`.

### 4.4 Error handling

- Sync failures set `last_sync_status="failed"` + `last_sync_error`.
- The source tab shows stale cached skills (if any) with a warning banner + Retry button.
- Partial success (some skills fetched, some failed) is acceptable — failed skills are logged but don't block the tab. The sync returns `"success"` if at least one skill was fetched.
- GitHub API rate-limit (403): `last_sync_error = "GitHub API rate limit exceeded. Set GITHUB_TOKEN env var for higher limits."`.

## 5. API Surface (replaces `/api/marketplace/*`)

All endpoints require authenticated user (`get_current_user` dependency).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/marketplace/sources` | List all sources (default + user-added) with `skill_count` + `last_sync_status` |
| `POST` | `/api/marketplace/sources` | Add source `{url, name?, source_type?, description?}` → auto-detect + async sync. Returns 201 + source object |
| `DELETE` | `/api/marketplace/sources/{id}` | Remove user-added source + cascade-delete ExternalSkill rows. Default sources: set `is_hidden=true` |
| `POST` | `/api/marketplace/sources/{id}/sync` | Trigger re-sync. Returns 202 + current status |
| `GET` | `/api/marketplace/sources/{id}/skills` | Paginated list of ExternalSkill for a source (`?q=search&sort=popular&limit=20&offset=0`) |
| `GET` | `/api/marketplace/skills/{id}` | External skill detail (full `skill_md`) |
| `POST` | `/api/marketplace/skills/{id}/install` | Add to My Skills: `write_skill_md` + Tool(`source="external"`) + increment `install_count`. Idempotent |
| `GET` | `/api/marketplace/my-skills` | List Tool rows where `source="external"` + `created_by_id=current_user` |
| `DELETE` | `/api/marketplace/my-skills/{tool_id}` | Remove from My Skills: `delete_skill_md` + soft-delete Tool + `reload_skills_registry()` |
| `GET` | `/api/marketplace/curated` | List curated default source definitions (for seeding reference) |

**Scoping**: Sources are shared across the org (all users see the same sources). Installs are per-user (`Tool.created_by_id`). Source add/delete is permitted by any authenticated user (sources are collaborative). Default sources can only be hidden, not deleted.

### 5.1 Request/response shapes

**POST /sources** request:
```json
{ "url": "https://github.com/anthropics/skills", "name": "Anthropic Skills", "description": "Official Anthropic skill examples" }
```
Response 201:
```json
{ "id": "...", "name": "Anthropic Skills", "url": "...", "source_type": "github_repo", "last_sync_status": "syncing", "skill_count": 0 }
```

**POST /skills/{id}/install** response:
```json
{ "success": true, "tool_id": "...", "skill_name": "my-skill", "already_installed": false }
```

## 6. Frontend UI Layout

### 6.1 Route + sidebar

- Route: `/marketplace` (replaces existing page).
- Sidebar: new top-level entry "Marketplace" (icon: `Store` from lucide-react), positioned after "Toolkit".
- Existing `/toolkit` "Browse Marketplace" button → navigates to `/marketplace` (no change needed, same route).

### 6.2 Page structure

```
┌──────────────────────────────────────────────────────────────┐
│ [Store] Skill Marketplace                       [+ Add Marketplace]
├──────────────────────────────────────────────────────────────┤
│ [Source 1] [Source 2] ... [My Skills]   ← tab bar             │
├──────────────────────────────────────────────────────────────┤
│ Source tab content:                                            │
│  Source header:                                                │
│    🌐 {url} · {skill_count} skills · synced {relative_time}   │
│    [Sync ↻] [Remove/Hide]                                      │
│  Toolbar:                                                      │
│    [🔍 search skills...] [Category ▾] [Sort: Popular ▾]       │
│  Skill card grid (responsive, 3-4 cols):                       │
│    ┌───────────┐ ┌───────────┐ ┌───────────┐                  │
│    │ Skill Name│ │ Skill Name│ │ Skill Name│                  │
│    │ 2-line desc│ │ 2-line desc│ │ 2-line desc│                 │
│    │ [category]│ │ [category]│ │ [category]│                  │
│    │ author v1.0│ │ author v2.3│ │ author v1.0│                 │
│    │ #tag #tag │ │ #tag      │ │ #tag #tag │                  │
│    │  [Add →]  │ │ [Added ✓] │ │  [Add →]  │                  │
│    └───────────┘ └───────────┘ └───────────┘                  │
│                                                                │
│ My Skills tab:                                                 │
│  Empty: "No skills yet — browse the sources above and add     │
│          some!"                                                │
│  Installed: same card grid but with actions:                   │
│    [Open in Skill Agent] [View Executions] [Remove]            │
└──────────────────────────────────────────────────────────────┘
```

### 6.3 Skill card component

Props: `skill` (ExternalSkill dict), `isInstalled` (bool), `onInstall` (callback), `onOpenDetail` (callback).

- **Name**: bold, single-line clamp.
- **Description**: 2-line clamp (CSS `line-clamp-2`).
- **Category badge**: colored chip (hash → color mapping).
- **Meta**: `author · v{version}` in muted text.
- **Tags**: up to 3 chips, "+N more" if excess.
- **Provenance badge**: 🟦 (structured: github/web_index) or 🟧 (scraped: web_page). Small, top-right.
- **Action button**: "Add →" (primary) → flips to "Added ✓" (green, disabled) when `isInstalled`. Clicking opens a detail sheet first (full skill_md preview), then user confirms install from the sheet.

### 6.4 Add Marketplace dialog

- **URL input** (required): placeholder `https://github.com/owner/repo`.
- **Name input** (optional): auto-filled from URL hostname/path; user can override.
- **Source type selector**: dropdown auto-set by `_detect_source_type(url)` on URL blur; user can override.
- **Description** (optional): textarea.
- **Submit**: `POST /sources` → dialog transitions to sync progress view:
  - Stages: `Detecting type → Fetching index → Parsing skills → Done`
  - Polls `GET /sources/{id}` every 3s for status.
  - On `success`: dialog closes, navigates to the new source's tab.
  - On `failed`: shows error + Retry button.

### 6.5 My Skills tab

- Fetches `GET /my-skills`.
- Cards show installed Tools (source="external"): name, description, category.
- Actions per card:
  - **Open in Skill Agent**: navigates to `/skill-agent?skill={tool.name}`.
  - **View Executions**: navigates to `/skills/executions?skill={tool.name}`.
  - **Remove**: `DELETE /my-skills/{tool_id}` → optimistic update (removes card from grid).
- Empty state: friendly message + link to browse sources.

## 7. My Skills Install Flow (detailed)

```
User clicks "Add" on external skill card
  → POST /api/marketplace/skills/{external_skill_id}/install
  → Backend:
      1. Fetch ExternalSkill by id
      2. Check if Tool already exists (name + created_by_id + source="external")
         → if yes: return {already_installed: true} (idempotent)
      3. skill_sync.write_skill_md(
           name=external_skill.name,
           description=external_skill.description,
           body=external_skill.skill_md,
           category="marketplace",
           version=external_skill.version,
           author=external_skill.author,
           tags=external_skill.tags,
         )
      4. Create Tool row: source="external", created_by_id=user.id,
         name=external_skill.name, skill_md=external_skill.skill_md
      5. reload_skills_registry()
      6. Increment ExternalSkill.install_count
      7. Return {success: true, tool_id, skill_name, already_installed: false}
  → Frontend: card button → "Added ✓" (green, disabled)
  → Skill is immediately runtime-available to all agents via skills_tool
```

**Remove flow**:
```
User clicks "Remove" on My Skills card
  → DELETE /api/marketplace/my-skills/{tool_id}
  → Backend:
      1. Fetch Tool by id (verify created_by_id == current_user)
      2. skill_sync.delete_skill_md(tool.name)
      3. Soft-delete Tool (is_deleted = true)
      4. reload_skills_registry()
      5. Return {success: true}
  → Frontend: optimistic remove from grid
```

## 8. Curated Defaults

Seeded on first `GET /sources` call if no `is_default=true` sources exist.

```python
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
```

These URLs are verifiable during implementation. If a curated repo doesn't have SKILL.md files at the expected paths, the sync will return 0 skills — that's acceptable (the source tab shows "No skills found" with the sync status).

## 9. Files to Create / Modify

### 9.1 Backend (new)

| File | Purpose |
|------|---------|
| `app/models/skill_source.py` | `SkillSource` model |
| `app/models/external_skill.py` | `ExternalSkill` model |
| `app/services/skill_source_service.py` | `SkillSourceService`: auto-detect, sync (3 types), CRUD |
| `app/routers/marketplace.py` | **Rewrite**: 10 new endpoints replacing old marketplace router |

### 9.2 Backend (modify)

| File | Change |
|------|--------|
| `app/models/__init__.py` | Register `SkillSource`, `ExternalSkill` |
| `app/services/skill_sync.py` | Add `delete_skill_md(name)` function |
| `app/main.py` | Ensure new models imported for `create_all` |

### 9.3 Frontend (new)

| File | Purpose |
|------|---------|
| `src/pages/Marketplace.jsx` | Main marketplace page (tabs + content) |
| `src/components/marketplace/SourceTab.jsx` | Source header + skill grid for a source tab |
| `src/components/marketplace/SkillCard.jsx` | Individual skill card component |
| `src/components/marketplace/SkillDetailSheet.jsx` | Full skill_md preview + install confirmation |
| `src/components/marketplace/AddMarketplaceDialog.jsx` | Add source dialog with sync progress |
| `src/components/marketplace/MySkillsTab.jsx` | Installed skills grid with actions |
| `src/api/marketplace.js` | API client (all 10 endpoints via authFetch) |

### 9.4 Frontend (modify)

| File | Change |
|------|--------|
| `src/App.jsx` | Update `/marketplace` route to new component (lazy) |
| `src/components/Sidebar.jsx` | Add "Marketplace" entry (icon: Store) |

### 9.5 Frontend (remove/deprecate)

| File | Action |
|------|--------|
| `src/pages/Marketplace.jsx` (old) | **Rewritten** (same path) |
| `src/components/marketplace/PublishDialog.jsx` | **Removed** (no community-publish in new design) |

### 9.6 Tests (new)

| File | Scope |
|------|-------|
| `backend/tests/test_skill_source_service.py` | Auto-detection, 3 sync types, error handling, upsert, stale cleanup |
| `backend/tests/test_skill_marketplace_api.py` | All 10 endpoints, auth, install/remove, curated seeding |
| `backend/tests/test_skill_marketplace_install.py` | Install writes SKILL.md + Tool, reload registry, idempotent, remove |
| `frontend/src/pages/Marketplace.tabs.test.jsx` | Tab bar render, switching, source header |
| `frontend/src/components/marketplace/AddMarketplaceDialog.test.jsx` | Dialog flow, auto-detect, sync progress |
| `frontend/src/components/marketplace/MySkillsTab.test.jsx` | Installed grid, remove, empty state |

## 10. Migration Plan

1. **New tables**: `skill_sources`, `external_skills` created via runtime `ALTER TABLE` / `Base.metadata.create_all` (project convention — sqlite test DB gets them automatically).
2. **Old `/api/marketplace` router**: fully replaced. Old `MarketplaceSkill` + `MarketplaceRating` tables left in DB (no destructive drop).
3. **Old `test_marketplace.py`**: replaced by new test files. Old tests deleted.
4. **Old frontend `Marketplace.jsx` + `PublishDialog.jsx`**: rewritten/removed.
5. **Sidebar**: add "Marketplace" entry.
6. **Existing `/toolkit` "Browse Marketplace" button**: unchanged (navigates to `/marketplace`).

## 11. Out of Scope (deferred to v2)

- Multi-page web crawling (following links from a listing page to individual skill pages).
- Source sync scheduling (cron-like automatic periodic sync).
- Skill version comparison / update notifications ("a newer version is available").
- Skill ratings / reviews (the old community rating system is deprecated, not replaced).
- Per-org private sources (all sources are org-shared in v1).
- GitHub authentication for private repos (public repos only in v1; `GITHUB_TOKEN` for higher rate limits only).

## 12. Risks + Mitigations

| Risk | Mitigation |
|------|------------|
| GitHub API rate limit (60/hr unauthenticated) | Support `GITHUB_TOKEN` env var; cache sync results aggressively; show rate-limit error clearly |
| Web page scraping unreliability | Reuse proven `SkillCollectionService` pipeline; show sync errors gracefully; cached data remains browsable |
| Large repos with many SKILL.md files | Pagination on `GET /sources/{id}/skills`; sync fetches in parallel with `asyncio.gather` (max 10 concurrent) |
| Source URL already registered | `UNIQUE(url)` constraint; API returns 409 Conflict with existing source id |
| Skill name collisions across sources | `ExternalSkill` keyed by `(source_id, name)` — no collision. My Skills installs use `category="marketplace"` to namespace |
