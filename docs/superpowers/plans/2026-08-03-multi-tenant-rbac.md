# Multi-Tenant RBAC — Company vs Personal Resources

**Date:** 2026-08-03
**Status:** Design (pending implementation approval)
**Scope:** Add role-based access control with two roles (Admin / Normal User), resource classification (Company / Personal), user-to-user sharing, and separate admin/normal views.

---

## 1. Requirements (confirmed with user)

| # | Question | Decision |
|---|----------|----------|
| 1 | Company vs Personal classification | **Derived from creator role** — Admin creates → Company; Normal user creates → Personal. Automatic, no manual toggle. |
| 2 | Can Normal users share Personal resources? | **Yes** — any owner can share their resource. Edit/delete reserved to the owner. |
| 3 | Admin count | **Single super-admin** — one account bootstrapped at deploy. No promote/demote flow. |
| 4 | Shared-user access level | **View + Use** — shared users can view the resource and run/chat with agents, but cannot edit, delete, or re-share. |

### Derived rules
- **Owner** (`resource.created_by_id == user.id`): full control (view, use, edit, delete, share).
- **Shared-with** (row in `resource_shares` for this user): view + use only. No edit / delete / re-share.
- **Admin** is simply the owner of Company resources (admin creates them). Admin does **not** get edit rights over other users' Personal resources — owner-only edit keeps the separation clean.
- **Edit/delete authority = owner only**, uniformly. This single rule covers both "Normal users cannot edit Company resources" (they aren't the owner) and "Personal resources fully manageable by their creator".

---

## 2. Current Architecture (what exists)

| Concern | Status | Location |
|---------|--------|----------|
| `User.role` field | Exists, default `"user"` | `backend/app/models/user.py:14` |
| `authz/` RBAC module | Exists but governs only tools/models/skills — **not** projects/agents | `backend/app/services/authz/` |
| Multi-tenant columns | `org_id`, `app_id` on every entity via `TimestampedBase` | `backend/app/models/base.py:42-48` |
| Ownership scoping | `_apply_owner()` in `entity_service.py` — user sees rows they created + system rows | `backend/app/services/entity_service.py` |
| Sharing / ACL | **None** — no `shared_with`, no ACL table | — |
| Admin user-management endpoint | **None** — registration is self-service, hardcodes `role="user"` | `backend/app/routers/` |
| Frontend role | `AuthContext` exposes `user.role` | `frontend/src/lib/AuthContext.jsx` |
| Frontend resource list | `MySpace.jsx` lists projects/agents | `frontend/src/pages/MySpace.jsx` |
| Latest migration | `037_ensure_12_acme_forecast_targets.py` | `backend/alembic/versions/` |

---

## 3. Target Architecture

### 3.1 Data Model

#### `User` — no schema change
Existing `role` column (`'admin' | 'user'`) is sufficient. The single super-admin is one row with `role='admin'`, bootstrapped at deploy via env vars `SUPERADMIN_EMAIL` / `SUPERADMIN_PASSWORD` (seed script, idempotent).

#### New column: `resource_type` on `projects` + `agent_apps`
```
resource_type: String(20), nullable=False, default='personal', index=True
   values: 'company' | 'personal'
```
Set at creation time from `creator.role == 'admin' ? 'company' : 'personal'`. Stored explicitly (not derived via JOIN) for query efficiency and stable classification even if a user's role ever changed.

**Backfill (migration 038):**
- `agent_apps` rows with `is_system = true` → `'company'`.
- All other existing rows → `'personal'` (they were created by normal users under the old self-service model).

#### New table: `resource_shares` (the ACL / sharing model)
```
resource_shares
  id                  String(36) PK
  resource_type       String(20)  NOT NULL   -- 'project' | 'agent'
  resource_id         String(36)  NOT NULL   -- FK → projects.id | agent_apps.id
  shared_with_user_id String(36)  NOT NULL   -- FK → users.id
  shared_by_user_id   String(36)  NOT NULL   -- FK → users.id (owner at time of share)
  access_level        String(20)  NOT NULL DEFAULT 'use'  -- 'use' (view+run); 'view' reserved
  created_date        DateTime
  org_id              String(36)  NOT NULL   -- multi-tenant wall
  app_id              String(36)  NOT NULL

  UNIQUE(resource_type, resource_id, shared_with_user_id)
  INDEX(resource_type, resource_id)
  INDEX(shared_with_user_id)
```
Polymorphic by `resource_type` + `resource_id` (avoids two near-identical tables; both `Project` and `AgentApp` share the same UUID PK shape from `TimestampedBase`).

### 3.2 Access-Control Matrix

| Action | Owner | Shared-with user | Other user | Admin (on Personal resources) |
|--------|:-----:|:----------------:|:----------:|:-----------------------------:|
| View / list | ✅ | ✅ | ❌ | ❌ (admin doesn't see others' Personal) |
| Use / run agent | ✅ | ✅ | ❌ | ❌ |
| Edit | ✅ | ❌ | ❌ | ❌ |
| Delete | ✅ | ❌ | ❌ | ❌ |
| Share / revoke | ✅ | ❌ | ❌ | ❌ |
| Create | ✅ (any user) | — | ✅ (any user) | — |

**Single enforcement rule:** edit / delete / share require `resource.created_by_id == current_user.id`. Everything else falls out naturally.

### 3.3 Visibility (list queries)

A user's visible resources = UNION of:
1. **Owned** — `created_by_id == user.id` (their Personal resources +, for admin, their Company resources).
2. **Shared with me** — `resource_id IN (SELECT resource_id FROM resource_shares WHERE shared_with_user_id == user.id)`.
3. **System** — `is_system == true` (agents only; existing behavior preserved).

Each returned row carries:
- `resource_type`: `'company' | 'personal'`
- `is_shared_with_me`: bool (true for category 2)
- `can_edit`: bool = `created_by_id == current_user.id`

### 3.4 Backend Changes

#### Migration `038_resource_shares_and_type.py`
1. `ALTER TABLE projects ADD COLUMN resource_type VARCHAR(20) NOT NULL DEFAULT 'personal'` + index.
2. `ALTER TABLE agent_apps ADD COLUMN resource_type VARCHAR(20) NOT NULL DEFAULT 'personal'` + index.
3. Backfill: `UPDATE agent_apps SET resource_type='company' WHERE is_system=true`.
4. `CREATE TABLE resource_shares (...)` with constraints/indices above.

#### Models (`backend/app/models/`)
- `project.py`, `agent_app.py`: add `resource_type` mapped column.
- New `resource_share.py`: `ResourceShare` model.

#### `entity_service.py` — update `_apply_owner()` → `_apply_visibility()`
- Replace single-owner filter with the UNION described in §3.3.
- Add `is_shared_with_me` / `can_edit` flags to serialized output.
- Edit/delete paths: enforce `created_by_id == current_user.id` → 403 otherwise.

#### New routers
- `backend/app/routers/admin_users.py` (admin-only, guarded by `require_admin` dep):
  - `POST   /api/admin/users` — create user (email, full_name, password). Role always `'user'` (single super-admin model). Returns created user (no password_hash).
  - `GET    /api/admin/users` — list users (id, email, full_name, role, created_date).
  - `DELETE /api/admin/users/{id}` — delete user (cascades: their resources become orphaned → also deleted or reassigned; default: delete).
- `backend/app/routers/resource_shares.py` (owner-only mutations):
  - `POST   /api/resources/{resource_type}/{resource_id}/shares` — share with a user (by user_id or email). Owner only.
  - `GET    /api/resources/{resource_type}/{resource_id}/shares` — list current shares.
  - `DELETE /api/resources/{resource_type}/{resource_id}/shares/{user_id}` — revoke. Owner only.
  - `GET    /api/resources/shared-with-me` — list resources shared with current user.
- `backend/app/routers/entities.py`: create path sets `resource_type` from creator role; edit/delete enforce ownership.

#### Dependencies / guards (`backend/app/services/authz/`)
- `require_admin` — checks `current_user.role == 'admin'`, else 403.
- `require_resource_owner(resource_type, resource_id)` — loads resource, checks `created_by_id == current_user.id`, else 403.
- Reuse existing `get_current_user` dependency.

#### Auth / registration
- Keep self-service registration behind a config flag `ALLOW_SELF_REGISTRATION` (default `true` for backward compat). When `false`, only admin can create accounts. Single super-admin deploy typically sets this `false`.

#### Super-admin bootstrap
- Seed script `backend/app/scripts/ensure_superadmin.py` (idempotent): reads `SUPERADMIN_EMAIL` / `SUPERADMIN_PASSWORD` from env, creates/updates the one `role='admin'` row. Run on container start (entrypoint) or via `docker exec`.

### 3.5 Frontend Changes

#### `AuthContext.jsx`
- Add `isAdmin = user?.role === 'admin'` to context value.

#### Navigation (`AppLayout` / sidebar)
- **Admin** sees: `Company Resources`, `User Management`, `Sharing` (manage shares of company resources).
- **Normal user** sees: `My Resources` (personal), `Shared with me`.
- System agents remain visible to all (existing behavior).

#### New / updated pages (`frontend/src/pages/`)
- **`AdminUserManagement.jsx`** (admin-only route):
  - Table of users (email, name, role, created).
  - "Create user" form (email, full_name, password) → `POST /api/admin/users`.
  - Delete user button.
- **`ResourceShareDialog.jsx`** (reusable modal):
  - Triggered by a share icon on any owned resource.
  - User picker (search by email), access level fixed to "View + Use".
  - Lists current shares with revoke buttons.
- **`MySpace.jsx`** (updated):
  - Resource cards show `Company` / `Personal` badge.
  - Share icon + share count on owned resources.
  - Filter tabs: owned vs shared-with-me.
  - Edit/Delete buttons **disabled** (or hidden) on shared resources (`can_edit === false`).
- **`SharedWithMe.jsx`**:
  - Lists resources shared with current user (projects + agents).
  - Actions: "Open" (project) / "Chat" (agent). No edit/delete/share.

#### API client (`frontend/src/lib/api.js` or equivalent)
- `adminApi.createUser/listUsers/deleteUser`.
- `shareApi.shareResource/listShares/revokeShare/listSharedWithMe`.
- Existing entity API calls updated to read `can_edit` / `is_shared_with_me` / `resource_type` from response.

#### Routing
- `/admin/users` → `AdminUserManagement` (guarded: redirect non-admin to `/`).
- `/shared` → `SharedWithMe`.
- Existing `/myspace` updated in place.

---

## 4. Implementation Phases

### Phase 1 — Backend data model & guards (foundation)
1. Migration `038` (resource_type + resource_shares table + backfill).
2. Models: `resource_type` on Project/AgentApp; new `ResourceShare`.
3. `authz` guards: `require_admin`, `require_resource_owner`.
4. `entity_service._apply_visibility()` rewrite + ownership enforcement on edit/delete.
5. Create path sets `resource_type` from creator role.
6. Super-admin seed script.

### Phase 2 — Backend API surface
7. `admin_users` router (create/list/delete).
8. `resource_shares` router (share/list/revoke/shared-with-me).
9. Wire guards into `entities.py` edit/delete.
10. `ALLOW_SELF_REGISTRATION` flag.

### Phase 3 — Frontend
11. `AuthContext.isAdmin`.
12. `AdminUserManagement` page + route guard.
13. `ResourceShareDialog` modal.
14. `MySpace` badges + tabs + disabled edit/delete on shared.
15. `SharedWithMe` page.
16. Navigation updates (role-based menu).

### Phase 4 — Testing & verification
17. Backend tests: ownership enforcement, sharing, admin guards, visibility UNION.
18. Manual: admin creates user → user logs in → creates personal resource → admin shares company resource → user sees & uses but can't edit.
19. `docker compose restart backend` after service changes (no `--reload`).

---

## 5. Edge Cases & Decisions

- **Orphaned resources on user deletion:** when admin deletes a user, that user's Personal resources are deleted alongside (cascade). Company resources owned by the deleted admin don't arise (single super-admin is never deleted). Shared resources pointing at a deleted user → their `resource_shares` rows cascade-delete.
- **Sharing with the owner:** rejected (no self-share).
- **Duplicate share:** `UNIQUE(resource_type, resource_id, shared_with_user_id)` prevents duplicates; upsert or 409.
- **System agents:** remain `is_system=true`, visible to all. Their `resource_type` set to `'company'` in backfill; not shareable (no owner to share them) but already universally visible.
- **Resource type immutability:** once set at creation, `resource_type` never changes (an admin's resource stays Company even if — hypothetically — the admin role moved; single super-admin makes this moot).
- **Multi-tenant:** all queries keep the existing `org_id`/`app_id` wall. `resource_shares` inherits these columns and queries are scoped identically.

---

## 6. Files Touched (summary)

**Backend (new):**
- `backend/alembic/versions/038_resource_shares_and_type.py`
- `backend/app/models/resource_share.py`
- `backend/app/routers/admin_users.py`
- `backend/app/routers/resource_shares.py`
- `backend/app/scripts/ensure_superadmin.py`

**Backend (modified):**
- `backend/app/models/project.py`, `backend/app/models/agent_app.py`
- `backend/app/services/entity_service.py` (`_apply_visibility`)
- `backend/app/services/authz/` (guards)
- `backend/app/routers/entities.py`
- `backend/app/main.py` (register new routers)
- `.env` / config (`ALLOW_SELF_REGISTRATION`, `SUPERADMIN_*`)

**Frontend (new):**
- `frontend/src/pages/AdminUserManagement.jsx`
- `frontend/src/pages/SharedWithMe.jsx`
- `frontend/src/components/ResourceShareDialog.jsx`

**Frontend (modified):**
- `frontend/src/lib/AuthContext.jsx` (`isAdmin`)
- `frontend/src/pages/MySpace.jsx` (badges, tabs, disabled actions)
- `frontend/src/lib/api.js` (admin + share clients)
- `frontend/src/App.jsx` / router config (new routes + guards)
- Sidebar/navigation component (role-based menu)
