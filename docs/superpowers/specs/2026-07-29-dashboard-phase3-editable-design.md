# Dashboard Phase 3 — Editable (Hybrid) Design

- **Date:** 2026-07-29
- **Status:** Approved (proceed to plan); micro-choices reversible later
- **Author:** brainstorming session (using-superpowers → brainstorming)
- **Depends on:** Phase 1 (interactivity) + Phase 2 (design) — both done on master, undeployed
- **Related:** `specs/2026-07-29-dashboard-*` (Phase 1/2), `e2e/dashboard-e2e.md`

## 1. Context

zhanlu dashboards are React+recharts live dashboards bound to one database
KnowledgeBase. The backend (`routers/dashboards.py`) currently exposes
`POST ""`, `GET ""`, `GET /{id}`, `DELETE /{id}`, `POST /{id}/query`. There is
**no `PATCH /{id}`** — dashboards are immutable after creation. The agent can
create dashboards (`create_dashboard` tool) but cannot edit existing ones.

Phase 3 makes dashboards **user-editable in place** and gives the agent an
`update_dashboard` tool — the "Hybrid" editing model (human + agent).

### Existing building blocks (reused, not rewritten)
- `_scoped(db, user, id)` — org-scoped + soft-delete lookup (404 on miss).
- `_validate_definition(definition)` — validates `{widgets:[{id,type,title,sql,options}]}`: non-empty list, each widget has `sql`, `validate_widget_sql` on widget + drill SQL.
- `validate_widget_sql(sql)` — read-only SELECT/WITH, single-statement guard.
- `render_widget_sql(sql, params, dimensions)` — whitelisted token substitution (`:from`/`:to`/`:date`/`:dim_<token>`/`:drill_value`) + re-validation.
- `QueryService(db).execute(kb_id, sql, max_rows, timeout_s)` → `{rows, truncated}`.
- `run_dashboard_query(db, dashboard, params)` — runs every widget's SQL independently; KB controls (`max_rows_per_query`, `timeout_seconds`) are upper bounds.
- `WidgetSpec` (pydantic, in `dashboard_tools.py`) — 10 allowed types, validates sql + drill.
- `Dashboard` model (inherits `TimestampedBase`) → carries `id`, `org_id`, `app_id`, `created_by_id`, `is_deleted`, `created_date`, `updated_date`, plus `project_id`/`project` (dual binding), `datasource_kb_id`, `name`, `description`, `definition` (JSON), `refresh_interval_seconds`.
- Frontend `DashboardViewer.jsx` — polling effect (10–300s clamp + visibilitychange pause + `mountedRef` — **must stay verbatim**), toolbar (DateRangeControl/FilterChips/Export/Share/Refresh), KPI row + 2-col grid, drill panel. Renders in `DashboardPopup` (slide-over) + standalone `/dashboard/:id` page.

## 2. Goals

1. **In-place edit mode** — an Edit toggle in the `DashboardViewer` header swaps the viewer into an editable state (drag handles, per-widget edit/delete, add-widget, SQL drawer). Works in both the popup and the standalone page. No new route.
2. **Creator-only permissions** — only the user who created the dashboard (`created_by_id` match) can edit/preview; others stay read-only. Fail-closed (403).
3. **Explicit Save / Cancel** — edits accumulate in an in-memory draft; one `PATCH` fires on explicit Save; Cancel discards. Unsaved-changes guard on navigate/unload.
4. **Live SQL preview** — edit a widget's SQL and run it against the bound datasource *without* persisting, reusing the existing read-only/row-cap/timeout guards.
5. **Agent `update_dashboard` tool** — the agent can edit existing dashboards (rename, replace widgets, adjust refresh interval), consistent with `create_dashboard`.
6. **Save as copy** — duplicate the current draft as a new dashboard (creator = current user), leaving the original untouched.
7. **Drag-reorder** — reorder widgets within the KPI row and the others grid.

## 3. Non-goals (out of scope, v1)

- Changing `datasource_kb_id` (immutable — rebinding invalidates all widget SQL and is an IDOR risk).
- Partial-ops PATCH / `ops` array (Approach B) and agent intent-merge layer (Approach C) — deferred; can layer onto Approach A later without API change.
- Collaborative / multi-user concurrent editing (single-creator model makes conflict resolution moot).
- Version history / multi-step undo (Cancel is the only undo in v1).
- Widget template gallery (Add-widget starts blank).
- Monaco / CodeMirror SQL editor (textarea v1; upgradeable later).
- Deploy (Phase 3 lands on master undeployed, same as Phase 1/2).

## 4. Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Editing surface | In-place edit-mode toggle | No new route; works in popup + standalone |
| D2 | Permissions | Creator only (`created_by_id`) | Aligns with fail-closed security stance |
| D3 | Save model | Explicit Save / Cancel | Predictable, low API churn, safe SQL experimentation |
| D4 | Agent tooling | Add `update_dashboard` tool | "Hybrid" = human + agent editing |
| D5 | PATCH granularity | Full-replace (Approach A) | Simplest, atomic, reuses `_validate_definition`, consistent with `create_dashboard` |
| D6 | Reorder lib | `@dnd-kit` | Accessible, keyboard-supporting, ~30KB gz |
| D7 | SQL editor | Textarea + preview table | Zero heavy dep; upgradeable to CodeMirror later |

D1–D4 are user-confirmed. D5 is the recommended approach (B/C rejected/deferred — see §5). D6–D7 are reversible micro-choices.

## 5. Approaches considered

**A — Full-replace PATCH + single in-memory draft + full-definition agent tool (CHOSEN).**
PATCH accepts provided fields; `definition` is the full widget list, re-validated via `_validate_definition`. Frontend deep-clones `definition` into a draft on edit-enter; Save = one PATCH; Cancel = discard. Agent `update_dashboard` mirrors `create_dashboard` (full widget list). Preview = new `POST /preview-sql` reusing `validate_widget_sql` + `render_widget_sql` + `QueryService.execute`.
*Pros:* simplest backend, atomic, consistent with create, fully testable. *Cons:* full payload per save (trivial for 6–12 widgets); agent re-sends full list for tiny edits (acceptable — it already does for create).

**B — Partial-ops PATCH (`ops:[{add_widget,remove_widget,reorder,update_widget}]`).** Server-side ops executor. *Rejected:* big backend complexity, over-engineered for small dashboards, diverges from create's contract.

**C — Full-replace PATCH + agent intent-merge layer.** PATCH = A; agent tool accepts high-level intents resolved server-side. *Deferred:* two agent code paths for marginal benefit; can layer onto A later without API change.

## 6. Backend design

### 6.1 `PATCH /api/dashboards/{dashboard_id}`

```
Body: UpdateDashboardBody {
  name?: str,
  description?: str,
  refresh_interval_seconds?: int,
  definition?: dict,   # full {widgets:[...]}
}
```

Flow:
1. `d = _scoped(db, user, dashboard_id)` → 404 if not found / wrong org / soft-deleted.
2. **Creator check:** `if d.created_by_id != user.id: raise HTTPException(403, "Only the creator can edit this dashboard")`.
3. If `definition` provided → `_validate_definition(definition)` (reuse; raises 400 on bad widgets/SQL/drill).
4. If `refresh_interval_seconds` provided → `clamp_refresh_interval(...)`.
5. Update only the provided fields. `datasource_kb_id`, `org_id`, `app_id`, `project_id`, `project`, `created_by_id` are **never** updated (immutable; not in the body schema — a caller sending them is ignored).
6. `db.commit()`; `db.refresh(d)`; return `d.to_dict()`.

Status code `200`. Errors: 404 (not found), 403 (not creator), 400 (invalid definition), 422 (pydantic body shape).

### 6.2 `POST /api/dashboards/{dashboard_id}/preview-sql`

Live preview of arbitrary (validated) widget SQL against the bound KB, **without persistence**.

```
Body: PreviewSqlBody {
  sql: str,
  params?: { from?: str, to?: str, filters?: dict[str,str] },
}
```

Flow:
1. `d = _scoped(db, user, dashboard_id)` → 404.
2. **Creator check:** `d.created_by_id == user.id` else 403. (Preview runs user-supplied SQL — restrict to creator, matching the edit stance and tightening beyond `/query` which only runs stored SQL.)
3. `validate_widget_sql(body.sql)` → 400 on non-read-only / multi-statement.
4. Resolve KB controls as upper bounds (same as `run_dashboard_query`): `max_rows = kb.max_rows_per_query or 1000`, `timeout_s = kb.timeout_seconds or 10`. Fall back to defaults if KB missing.
5. `rendered = render_widget_sql(body.sql, body.params or {}, dimensions=[])` (token substitution + re-validation). 400 on unknown token.
6. `res = await asyncio.to_thread(lambda: QueryService(db).execute(d.datasource_kb_id, rendered, max_rows=max_rows, timeout_s=timeout_s))`.
7. Return `{columns, rows, truncated, error}` where `columns = list(rows[0].keys()) if rows else []`. On any exception return `{columns:[], rows:[], truncated:false, error:str(e)}` with HTTP 200 (preview errors are user-facing, not 5xx — mirrors how `_run_one` reports per-widget errors).

**Refactor (minor):** extract the body of `run_dashboard_query._run_one` into a module-level `async def _run_single_sql(db, kb_id, sql, params, dimensions, max_rows, timeout_s) -> dict` so preview, drill, and widget execution share one code path. `run_dashboard_query` and `_run_drill` call it; behavior unchanged.

### 6.3 Agent tool `update_dashboard` (in `dashboard_tools.py`)

Pydantic args:
```python
class UpdateDashboardArgs(BaseModel):
    dashboard_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    refresh_interval_seconds: Optional[int] = None
    widgets: Optional[list[WidgetSpec]] = None   # full list, replaces
```

Handler `_update_dashboard(args, db, user_id, context)`:
1. Resolve `org_id`/`project_id` from context.
2. Scoped lookup: `Dashboard.id == args.dashboard_id and org_id == ctx.org_id and is_deleted == False` → else `{success:false, error:"Dashboard not found"}`.
3. **Creator check:** `if d.created_by_id != user_id: return {success:false, error:"Only the dashboard creator can edit it."}`.
4. Apply provided fields: `title→name`, `description`, `refresh_interval_seconds` (clamped), `widgets→definition={"widgets":[w.model_dump() for w in args.widgets]}` (WidgetSpec already runs `validate_widget_sql` on each widget + drill). `datasource_kb_id` unchanged.
5. If `widgets` is None, `definition` is untouched. If provided, full replace + validation already done by WidgetSpec.
6. `db.commit()`; `db.refresh(d)`; return `{success:true, dashboard:d.to_dict(), artifact:{source:"dashboard", dashboard_id:d.id, title:d.name, widget_count:len(d.definition["widgets"])}}` (mirrors `create_dashboard`'s return shape).

Schema (`UPDATE_DASHBOARD_SCHEMA`):
- `description`: instructs the agent to first fetch the current dashboard (via `list_dashboards`/detail or prior context) to know existing widgets, then send the **full** updated widget list for any widget change. Clarification gate: if the user's edit intent is ambiguous (which widget, what change, new SQL), ask in ONE batched message with numbered options and WAIT — do not guess. `datasource_kb_id` cannot be changed; to rebind, create a new dashboard.
- `parameters.properties`: `dashboard_id` (required), `title`, `description`, `refresh_interval_seconds`, `widgets` (same item schema as `create_dashboard`). `required: ["dashboard_id"]`.
- Registered: `name="update_dashboard"`, `toolset="dashboards"`, `category="dashboards"`, `emoji="✏️"`, `max_result_size_chars=4_000`. Import in `tool_handlers/__init__.py` (per zhanlu convention for modules outside `tool_handlers/` — though this file is inside, confirm registration import path).

## 7. Frontend design

### 7.1 `DashboardViewer.jsx` — edit mode

New props: `canEdit: boolean` (default `false`). Parent (`DashboardPopup` + `pages/DashboardView.jsx`) computes `canEdit = dashboard.created_by_id === currentUser.id` from the auth context and passes it down.

New internal state:
- `editing: boolean` (default false)
- `draft: definition` (deep clone of `dashboard.definition` on enter; `null` when not editing)
- `dirty: boolean` (draft differs from original)

**Edit button** (pencil icon, `lucide-react` `Pencil`) in the toolbar, shown only when `canEdit && !editing`. On click:
- `setDraft(structuredClone(dashboard.definition))` (or `JSON.parse(JSON.stringify(...))` for older-runtime safety)
- `setEditing(true)`, `setDirty(false)`
- **Pause polling:** clear `timerRef.current`; the visibility listener stays but `refresh` is a no-op while editing (guard: `if (editing) return` at top of `refresh`, OR keep polling but it only re-reads stored SQL which doesn't change — simplest is to pause to avoid results flicker mid-edit). Resume on exit.

**Edit-mode toolbar** (replaces the right-side Export/Share/Refresh cluster while editing): **Save** (primary button), **Cancel** (ghost), **Save as copy** (ghost, secondary), **Add widget** (ghost). DateRangeControl/FilterChips remain visible (they drive preview params).

**Widget cards in edit mode** (both KPI row + others grid):
- Drag handle (top-left, `GripVertical` icon) — only in edit mode.
- Edit button (pencil) → opens SQL drawer for that widget.
- Delete button (trash) → inline confirm → removes from `draft.widgets`, `setDirty(true)`.
- Widget still renders its last `results` (greyed via opacity-60) so the user sees context; results are NOT re-fetched while editing.

**Save:**
- `await updateDashboard(dashboard.id, { definition: draft, name: headerName })` (header rename optional v1 — include an editable title field in edit-mode header; if unchanged, omit `name`).
- On success: call `onSaved(updatedDashboard)` prop (parent updates its dashboard state) → `setEditing(false)`, `setDraft(null)`, `setDirty(false)`, resume polling + `refresh()`.
- On error: toast (`{success:false}`), stay in edit mode, keep draft.

**Cancel:**
- If `dirty`: show confirm dialog ("Discard unsaved changes?"). If confirmed (or not dirty): `setEditing(false)`, `setDraft(null)`, `setDirty(false)`, resume polling + `refresh()`.

**Unsaved-changes guard:**
- `beforeunload` handler attached while `editing && dirty` (prevent default).
- React-router navigate blocker while `editing && dirty` (use the project's existing blocker pattern; if none, a `useBlocker`/prompt equivalent). Blocks popup-close + route change.

**Polling-effect preservation:** the existing polling `useEffect` (lines ~89–107) must stay verbatim. The edit-mode pause is achieved by guarding `refresh` (`if (editingRef.current) return;` at top) — `editingRef` mirrors `editing` state to avoid re-running the effect. Do NOT add `editing` to the effect's dependency array (it would tear down/rebuild the interval on every toggle).

### 7.2 SQL editor drawer (right slide-over)

Component `WidgetEditorDrawer` (new, `components/dashboard/WidgetEditorDrawer.jsx`):
- Props: `widget` (the draft widget being edited, or blank for add), `dashboardId`, `params` (current dateRange/filters for preview), `onApply(widget)`, `onCancel`.
- Fields:
  - `title` (text input)
  - `type` (select, 10 options: kpi/line/bar/pie/table/area/stacked-bar/scatter/gauge/radar)
  - `sql` (monospace `<textarea>`, min-height ~160px, spellcheck off)
  - `options` (collapsed raw-JSON `<textarea>` with a hint listing the per-type option keys — mirrors the `create_dashboard` schema docs). Invalid JSON shows an inline error; Apply disabled.
- **Run preview** button: calls `previewWidgetSql(dashboardId, { sql: textareaValue, params })` → renders a result table (`columns` + first 20 `rows`) + `truncated` flag + `error` line. Preview runs whatever is currently in the textarea (independent of Apply). Loading state on the button. Debounce not required (explicit button).
- **Apply:** validates client-side (sql non-empty, options valid JSON) → `onApply({id, type, title, sql, options})` → drawer closes. For add-widget, generate `id = "w_" + Date.now()` if blank.
- **Cancel:** close without applying (draft unaffected).
- Drawer is a right-side slide-over (consistent with `DashboardPopup`'s slide-over styling); overlay click = Cancel.

`DashboardViewer` holds `editingWidget` state (the widget id being edited, or `"__new__"`). Edit-pencil sets it; Add-widget sets `"__new__"`. `onApply` merges the widget into `draft.widgets` (replace by id, or append if new) and `setDirty(true)`.

### 7.3 Drag-reorder (`@dnd-kit`)

New dep: `@dnd-kit/core` + `@dnd-kit/sortable`. Two `SortableContext`s:
- KPI row (horizontal).
- Others grid (2-col; items keep their grid cells, reorder reorders the source array).

`DndContext` wraps the edit-mode grid. On `onDragEnd`: reorder the relevant slice of `draft.widgets` (KPIs stay first, others after) and `setDirty(true)`. Each sortable item = the widget card wrapper with a `GripVertical` drag handle (`useSortable` listeners on the handle).

Non-edit mode renders the plain grid (no dnd-kit wrappers) to avoid any overhead on the read path.

### 7.4 Save as copy

Button in edit-mode toolbar. Flow:
- `await createDashboard({ name: (headerName || dashboard.name) + " (copy)", description: dashboard.description, datasource_kb_id: dashboard.datasource_kb_id, project_id: dashboard.project_id, project: dashboard.project, definition: draft, refresh_interval_seconds: dashboard.refresh_interval_seconds })` (reuse existing `createDashboard` API client → `POST /api/dashboards`).
- On success: navigate to `/dashboard/{newId}` (the new dashboard's `created_by_id` = current user → immediately editable). Original dashboard untouched.
- On error: toast, stay in edit mode.

### 7.5 API client (`api/dashboards.js`)

Add:
- `updateDashboard(id, body)` → `PATCH /api/dashboards/{id}` with `body` (`{name?, description?, refresh_interval_seconds?, definition?}`). Returns the updated dashboard dict.
- `previewWidgetSql(id, { sql, params })` → `POST /api/dashboards/{id}/preview-sql`. Returns `{columns, rows, truncated, error}`.

`createDashboard`, `queryDashboard`, `getDashboard`, `listDashboards`, `deleteDashboard` already exist (reused for copy/view/delete).

## 8. Security

- **Creator-only on all 3 write paths** (PATCH, preview, agent tool): `d.created_by_id == user.id` else 403 / `{success:false}`. Fail-closed (deny, never ignore). This is tighter than the existing read paths (org-scoped) — intentional, matches the audit's fail-closed stance.
- **`datasource_kb_id` immutable** in PATCH + agent tool — prevents rebinding a dashboard to a KB the editor shouldn't access via an existing dashboard (IDOR prevention).
- **Preview SQL** runs full `validate_widget_sql` (read-only/single-stmt) + `render_widget_sql` re-validation + `QueryService` row-cap/timeout. No new data-access surface beyond `/query` (and tighter, since creator-only + arbitrary user SQL is confined to the creator who could already edit).
- **No XSS surface:** dnd-kit operates on local React state (no `dangerouslySetInnerHTML`); the SQL/options textareas render as plain text; result table renders cell values as text. Existing DOMPurify hardening (audit C4) unaffected.
- **No new auth endpoints** — all reuse `get_current_user_required`.

## 9. Testing

### Backend (sqlite test-DB convention; explicit paths only; no `-n`)
- `tests/test_dashboards_patch.py`:
  - Creator PATCH renames → 200, `name` updated.
  - Creator PATCH replaces `definition` → 200, widgets updated; invalid definition (empty widgets, bad SQL, bad drill) → 400.
  - Creator PATCH `refresh_interval_seconds` clamped (5→10, 999→300).
  - Non-creator (same org) PATCH → 403.
  - Wrong-org user PATCH → 404 (not 403 — org scope hides existence).
  - `datasource_kb_id` in body is ignored (unchanged).
  - Soft-deleted dashboard → 404.
- `tests/test_dashboards_preview_sql.py`:
  - Creator preview returns `{columns, rows}` for valid SELECT.
  - Non-read-only SQL (`DELETE ...`) → 400.
  - Multi-statement → 400.
  - Unknown token (`:foo`) → 400.
  - Non-creator → 403.
  - DB error → 200 with `error` field (not 5xx).
- `tests/test_update_dashboard_tool.py`:
  - Agent (creator) updates title + widgets → `{success:true, dashboard}`.
  - Non-creator agent → `{success:false, error}`.
  - Invalid widget (bad SQL) → pydantic ValidationError → `{success:false}`.
  - `datasource_kb_id` not in args (cannot change).

### Frontend (vitest; recharts mock needs CartesianGrid+Legend+Tooltip)
- `DashboardViewer.edit.test.jsx`:
  - Edit button hidden when `canEdit=false`; shown when `canEdit=true`.
  - Enter edit mode → `draft` cloned; polling paused (no `queryDashboard` calls).
  - Add widget → drawer opens blank → Apply appends to draft → dirty.
  - Edit widget → drawer opens with widget → Apply updates draft.
  - Delete widget → confirm → removed from draft → dirty.
  - Reorder (mock dnd-kit `onDragEnd`) → draft order changed → dirty.
  - Save → calls `updateDashboard` with draft → `onSaved` called → exits edit mode.
  - Cancel (dirty) → confirm dialog → discards → exits.
  - Save as copy → calls `createDashboard` with draft + "(copy)" name.
  - Unsaved guard: `beforeunload` + navigate block while dirty.
- `WidgetEditorDrawer.test.jsx`:
  - Run preview → calls `previewWidgetSql` → renders columns/rows.
  - Preview error → renders error line.
  - Apply disabled when sql empty / options invalid JSON.

### E2E (real Aliyun RDS MySQL + browser; per-file only — user rejects combined runs)
- Extend `test_dashboard_e2e_*.py` with `test_dashboard_edit_save.py`: creator opens dashboard → Edit → edits a widget title + SQL → Run preview (assert rows) → Save → asserts dashboard reflects new title/SQL on reload. One file.

## 10. File touch list

**Backend (new/edited):**
- `backend/app/routers/dashboards.py` — add `PATCH /{id}` + `POST /{id}/preview-sql`; add `UpdateDashboardBody`, `PreviewSqlBody` pydantic models.
- `backend/app/services/dashboard_query.py` — extract `_run_single_sql` shared helper (minor refactor).
- `backend/app/services/tool_handlers/dashboard_tools.py` — add `UpdateDashboardArgs` + `_update_dashboard` + `UPDATE_DASHBOARD_SCHEMA` + registration.
- `backend/app/services/tool_handlers/__init__.py` — ensure `update_dashboard` registered/imports (confirm path).
- `backend/tests/test_dashboards_patch.py` (new)
- `backend/tests/test_dashboards_preview_sql.py` (new)
- `backend/tests/test_update_dashboard_tool.py` (new)

**Frontend (new/edited):**
- `frontend/src/components/dashboard/DashboardViewer.jsx` — edit mode, draft state, toolbar, unsaved guard.
- `frontend/src/components/dashboard/WidgetEditorDrawer.jsx` (new)
- `frontend/src/components/dashboard/EditableWidgetCard.jsx` (new — drag handle + edit/delete affordances wrapper)
- `frontend/src/api/dashboards.js` — add `updateDashboard`, `previewWidgetSql`.
- `frontend/src/components/dashboard/DashboardPopup.jsx` — pass `canEdit`.
- `frontend/src/pages/DashboardView.jsx` — pass `canEdit`.
- `frontend/src/components/dashboard/DashboardViewer.edit.test.jsx` (new)
- `frontend/src/components/dashboard/WidgetEditorDrawer.test.jsx` (new)
- `frontend/package.json` — add `@dnd-kit/core`, `@dnd-kit/sortable`.

**E2E:**
- `backend/tests/test_dashboard_e2e_edit_save.py` (new)

**No model migration** — `Dashboard` already has `created_by_id` (via `TimestampedBase`); no new columns. No Alembic (zhanlu uses runtime `ALTER TABLE`, none needed here).

## 11. Open questions / deferred

- **D6 dnd-kit dep:** if the dep is objectionable, swap to native HTML5 DnD (rougher UX, no keyboard a11y). Reversible in plan/impl without API change.
- **D7 SQL editor richness:** textarea v1; upgrade to CodeMirror/Monaco later if users want syntax highlighting/autocomplete.
- **Approach C (agent intent-merge):** add later *only if* agents struggle to emit full widget lists for small edits. No API change required (layered in the tool handler).
- **Header rename in edit mode:** v1 includes an editable title field; if it complicates the toolbar layout, defer title-edit to a follow-up (definition-only PATCH first).

## 12. Rollout

Lands on master undeployed (same as Phase 1/2). After backend edits: `docker restart zhanlu-backend` (`UVICORN_RELOAD=false`). Frontend: `/opt/rebuild-zhanlu.sh --force` when ready to deploy. Commit only when the user asks (zhanlu convention).
