# Full-Stack Dashboard Delete (Hard Cascade) — Design

Date: 2026-08-28
Status: Approved (user: "ok do it")
Author: Synexia Agent

## Problem

My Space → Dashboards shows full-stack (LIVE) dashboard cards that **cannot be
deleted**. The frontend hides the delete button for `kind === 'app'`
(`MySpace.jsx:279` renders it only for `d.kind !== 'app'`) and
`removeDashboard` (`MySpace.jsx:161`) skips apps entirely ("no delete endpoint
yet"). Users cannot clean up test dashboards — the current DB has 5 records,
4 of which are visible as duplicate "Sales Performance Dashboard" cards.

User requirement: a delete (or archive) button on dashboard cards.

## Decision: DELETE (hard cascade), not Archive

User's standing rule: **hard-deletes only**, complete cascade removal (DB rows +
generated dirs + conversation bindings), frustrated by soft-deletes and orphaned
data. Archive = soft-delete (`is_deleted` flag) — rejected.

## Architecture

### Backend — new endpoint `DELETE /api/dashboards/app-records/{slug_or_id}`

Registered in `backend/app/routers/dashboards.py` beside the other app-records
routes (BEFORE the legacy `/{dashboard_id}` delete route; FastAPI matches in
registration order).

Handler `delete_dashboard_app_record(slug_or_id, db, user)`:

1. **Resolve + authorize** — same pattern as `get_dashboard_app_record`:
   query DashboardApp by slug or id, filtered by `org_id == user.org_id`.
   Visibility gate (T10): `personal` → creator only; `company` → creator or
   `user.role == "admin"`. Deny → 404 (do not leak existence) or 403.
2. **Stop the realtime poller** — `dashboard_app_manager.stop_poller(slug)`
   (safe no-op when not running).
3. **Delete the generated app directory** — `get_generator().app_dir(slug)`
   = `apps_dir / module_name(slug)`; `shutil.rmtree(path, ignore_errors=True)`
   if it exists. This removes the generated backend modules (api/queries/
   realtime), config.json, and the copied frontend dist.
4. **Hard-delete the DB row** — `db.delete(record); db.commit()`.
5. **Cascade conversation bindings** — delete AgentConversation rows whose
   `metadata_` JSON has `mode == 'dashboard'` AND (`dashboard_slug == slug` OR
   `dashboard_id == record.id`). Query: SQLAlchemy JSON contains or a Python
   scan (metadata_ is JSON column; use `func.json_contains` where supported,
   else iterate — keep it simple and dialect-safe by loading matching rows with
   a text filter on metadata like `%dashboard%` then filtering in Python).
   Also clear `dashboard_id` FK on legacy `dashboard`-bound conversations? No —
   legacy Dashboard rows are separate entities; only fullstack conversations
   bound via metadata are in scope.
6. **Return 204.**

Implementation detail — reuse existing helpers:
- `dashboard_app_manager` singleton already imported in the router.
- `get_generator()` from `app.services.dashboard_app.generator`.
- `module_name(slug)` from the same module.

### Frontend — MySpace.jsx dashboard tab

1. Add API client fn in `frontend/src/api/dashboards.js`:
   `deleteDashboardApp(slugOrId)` → `DELETE /api/dashboards/app-records/{slugOrId}`.
2. `removeDashboard(item)`:
   - `item.kind === 'app'` → `await deleteDashboardApp(item.id)` (item.id is the
     slug — see MySpace.jsx:136 `id: a.slug`).
   - legacy → existing `deleteDashboard(item.id)`.
   - reload list.
3. Render the delete button for ALL kinds (remove the `d.kind !== 'app'` guard
   at line 279) — keep `ml-auto` layout so Run stays left, delete right.
4. Add a confirm dialog (destructive action): `window.confirm` is acceptable
   for MVE, or a small inline confirm state. Choose the simpler `window.confirm`
   with i18n label (`t.common.deleteConfirm` or hardcoded bilingual string) —
   match the file's existing bilingual pattern (`lang === 'en' ? ... : ...`).

### Tests

- Backend `backend/tests/routers/test_dashboard_app_delete.py` (or extend an
  existing router test file):
  - personal app, creator deletes → 204, row gone, dir gone, conversation rows
    with matching metadata gone
  - personal app, different user → 404/403, row intact
  - company app, creator deletes → 204
  - company app, non-creator non-admin → denied
  - company app, org admin (role=admin) → 204
  - unknown slug → 404
  - legacy delete route still works (regression)
- Frontend: check the delete button renders for both kinds and removeDashboard
  routes correctly (unit test or manual browser verification; prefer existing
  test file if MySpace has one — `frontend/src/pages/MySpace.test.jsx` if present).

## Error handling

- `stop_poller` and `rmtree` never raise fatal errors (both already safe no-ops
  / ignore_errors).
- DB delete failure → 500 with rollback (SQLAlchemy session handles it).
- Non-existent record → 404 (matches GET app-record behavior).

## Scope / non-goals

- NOT archive / soft-delete.
- NOT touching legacy Dashboard delete (already exists).
- NOT adding delete from the chat thread view — My Space card only (MVE; user
  asked for the card action).
- NOT undo/restore after delete.
