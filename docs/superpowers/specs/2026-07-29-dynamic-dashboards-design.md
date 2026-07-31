# Dynamic Live Dashboards — Design Spec

- **Date:** 2026-07-29
- **Status:** Draft (pending review)
- **Author:** brainstorming session (superpowers)
- **Related:** `2026-07-29-automation-quality-reliability-design.md`, `generation_orchestrator.py` dashboard fallback

## 1. Context & Goals

Users want an agent to build a **dashboard** for a project that already has a
database connection, and have that dashboard stay **live** (real-time data from
the database) rather than a one-time baked snapshot. The dashboard must:

1. Be created by the agent using available dashboard-creation **skills**.
2. **Dynamically update** with real-time data from the database (auto-poll).
3. **Persist** to the user's "My Space" (`/my-space`) so it is reachable later.
4. On revisit from My Space, still show **live** database data.
5. Appear **inline in the chat** as a right-side popup panel.

### Existing foundation (reused, not rebuilt)

- **Live query path:** `QueryService(db).execute(kb_id, sql, max_rows, timeout_s)`
  (`services/tool_handlers/db_tools.py:165`) → `get_connector(kb)` context manager
  (`services/db/connector_factory.py:80`) → dialect connectors
  (`services/db/{sqlite,postgres,mysql,mssql,oracle}.py`). Connectors are
  short-lived, **read-only**, cap rows at `max_rows`, apply a `timeout_s` statement
  timeout, and return rows as list-of-dicts. Already exercised by the
  `execute_query` / `ask_data_agent` tools.
- **Connection controls:** database `KnowledgeBase` rows carry connection config +
  query controls (`max_rows_per_query`, `timeout_seconds`). The standalone
  `Datasource` model (`models/datasource.py`) is a parallel connection store with
  the same fields + schema snapshot; see Open Questions §13 for reconciliation.
- **Static dashboards today:** `generation_orchestrator.py` already has a
  dashboard fallback (~line 709) that mines `ask_data_agent` rows and emits a
  **static** HTML dashboard (Chart.js + KPI cards). `DashboardArtifactPreview.jsx`
  is a static mock. Neither queries the DB at view-time.
- **Artifact / chat display:** `InlineArtifactPreview.jsx`,
  `partitionArtifacts.js`, `MessageBubble.jsx` render generated artifacts inline.
- **My Space:** `pages/MySpace.jsx` — tabbed layout (`project` / `agent` / `file` /
  `kb`), each tab lists entities for the current org/project.
- **Charting:** `recharts@^2.15.4` already in `frontend/package.json` — no new dep.
- **Auth:** frontend uses `authFetch` (Bearer JWT, silent 401 refresh-retry).
  Because live dashboards render in a **React** viewer (not an iframe), they can
  call the data API with `authFetch` directly — sidestepping the known
  "iframe can't send Bearer token" gap that blocks live-data in HTML artifacts.

## 2. Decisions (from brainstorming Q&A)

| Question | Decision |
|---|---|
| Real-time mode | **Auto-poll** — re-query every N seconds while open + fresh on open |
| Dashboard form | **Hybrid** — schema-driven React viewer now; self-contained HTML escape-hatch deferred |
| My Space display | **Popup on same page** — opening a dashboard shows an overlay popup, no route change |
| Data binding | **User explicitly picks ONE datasource** per dashboard (never auto-binds) |

## 3. Requirements

- **R1 (live):** While a dashboard popup is open, widgets re-query the bound
  datasource on a per-dashboard interval and update in place.
- **R2 (persist):** Dashboards are first-class persisted entities, listed in My
  Space, scoped to the user's org/project, and openable later with live data.
- **R3 (chat inline):** A created dashboard surfaces as an inline card in the chat
  message; clicking opens a right-side popup panel within the chat view.
- **R4 (skill-driven):** The agent builds the dashboard via a `dashboard-generation`
  skill package + a `create_dashboard` tool; it does not hand-write arbitrary HTML.
- **R5 (explicit datasource):** When the user's request does not name a datasource,
  the agent asks the user to choose exactly one from the project's datasources.
- **R6 (safety):** Widget SQL is read-only, single-statement, row-capped, and
  timeout-bounded; no DDL/DML ever runs.
- **R7 (security):** All endpoints are session-auth, org-scoped, fail-closed; a
  user can only query datasources/dashboards in their own org/project.

## 4. Non-goals (deferred)

- Self-contained HTML-artifact dashboards (hybrid phase 2 — needs signed-token
  data API because iframes can't carry the Bearer token).
- True push via SSE/WebSocket.
- Multi-datasource dashboards (one dashboard = one datasource).
- Visual drag-and-drop widget editor (definition is produced by the agent).
- Dashboard sharing / publishing beyond the owner's org.
- Reconciliation of the `Datasource` model with the KB-based query path (see §13).

## 5. Architecture (end-to-end flow)

```
Project chat:
  User: "build me a sales dashboard"
    -> Agent sees no datasource named
    -> asks user to pick ONE datasource (chips listing the project's DB KBs)
    -> User picks "sales_db"
    -> Agent designs widgets (KPI / line / bar / pie / table), one SQL each
       (guided by the dashboard-generation skill's prompt + examples)
    -> create_dashboard tool:
         * pydantic-validates the definition
         * read-only / single-statement / row-cap validation on every widget SQL
         * persists Dashboard (bound to the chosen DB KB)
         * returns a dashboard artifact reference for the chat message
    -> Chat message renders a Dashboard card (live, not a static mock)
    -> User clicks card -> right-side DashboardPopup opens
         * fetches live data via POST /api/dashboards/{id}/query (authFetch)
         * re-polls every refresh_interval_seconds; pauses when tab hidden
         * closes -> polling stops

My Space (/my-space), "Dashboards" tab:
  -> lists dashboards for current org/project (name, datasource, last refreshed)
  -> click -> DashboardPopup overlay on the SAME page (no navigation)
       * same live viewer + polling as chat
```

## 6. Data model

New table `dashboards` (runtime `ALTER TABLE ... ensure`, per zhanlu convention —
no Alembic). Extends `TimestampedBase` so it inherits `org_id` / `app_id` /
`created_at` / `updated_at`.

```
dashboards
  id              str (pk, uuid)
  org_id          str   (from TimestampedBase)
  app_id          str   (from TimestampedBase)
  project_id      str   (FK -> projects.id, nullable)        # dual-column
  project         str   (legacy name, default "global")       # convention!
  datasource_kb_id str  (FK -> knowledge_bases.id, NOT NULL) # the ONE DB KB
  name            str
  description     str (nullable)
  definition      json  # widgets[], see below
  refresh_interval_seconds int (default 30, clamp 10..300)
  created_by      str   (user_id)
  timestamps      from TimestampedBase
```

`definition` JSON shape:

```jsonc
{
  "widgets": [
    {
      "id": "w1",
      "type": "kpi" | "line" | "bar" | "pie" | "table",
      "title": "Revenue this month",
      "sql": "SELECT sum(amount) AS revenue FROM orders WHERE created_at >= date_trunc('month', now())",
      "options": {
        "value_column": "revenue",          // kpi
        "x_column": "day", "y_column": "revenue", // line/bar
        "name_column": "category", "value_column": "cnt" // pie
      }
    }
  ]
}
```

**Binding note:** the dashboard binds to a database **KnowledgeBase** (`kb_id`),
the same entity `execute_query` / `ask_data_agent` operate on, so the proven
`QueryService` + connector path is reused unchanged. The standalone `Datasource`
model is *not* the binding target in v1 (see §13).

## 7. Backend APIs

All endpoints under `app/routers/dashboards.py`, session-auth via the existing
`get_current_user` dependency, org-scoped by `user.org_id`, fail-closed on any
auth/lookup error (per audit conventions C1/C2). Pagination cap (≤1000) applied
to list.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/dashboards` | Create (used by the agent tool; also usable from UI) |
| `GET`  | `/api/dashboards?project_id=…` | List for current org/project (My Space) |
| `GET`  | `/api/dashboards/{id}` | Fetch definition + metadata |
| `PUT`  | `/api/dashboards/{id}` | Rename / update definition / refresh interval |
| `DELETE` | `/api/dashboards/{id}` | Delete |
| `POST` | `/api/dashboards/{id}/query` | Execute ALL widget SQLs in one round-trip |

`POST /api/dashboards/{id}/query` response:

```jsonc
{
  "dashboard_id": "...",
  "refreshed_at": "2026-07-29T12:00:00Z",
  "results": {
    "w1": { "columns": ["revenue"], "rows": [{"revenue": 123450}], "error": null },
    "w2": { "columns": [...], "rows": [...], "error": null }
  }
}
```

Implementation reuses `QueryService(db).execute(kb_id, sql, max_rows, timeout_s)`
per widget, gathered concurrently via `asyncio.to_thread`. `max_rows` /
`timeout_s` come from the bound KB's query controls, optionally tightened by the
dashboard. A failing widget sets that widget's `error` and does **not** abort the
whole response (one bad SQL = one red widget, not a blank dashboard).

## 8. SQL safety (defense in depth)

Validated **at create time** (in the `create_dashboard` tool) **and at query
time** (in `/query`, never trusting stored SQL):

- **Read-only:** only `SELECT` / `WITH` leading keyword allowed. Reject anything
  containing DDL/DML keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`,
  `CREATE`, `TRUNCATE`, `GRANT`, `REVOKE`, etc.) as statement-leading or
  top-level. Implement via a tokenizer/AST guard (sqlparse or a dialect-aware
  regex pre-check), not a naive substring match.
- **Single statement:** reject `;` followed by additional non-whitespace.
- **Row cap:** enforce `LIMIT` injection/cap = `min(widget_max_rows, kb.max_rows_per_query)`.
- **Timeout:** per-widget `timeout_s` ≤ `kb.timeout_seconds`.
- The connectors themselves are already read-only and cap rows/timeout — the
  above is belt-and-suspenders at the application layer.

## 9. Agent integration

### 9.1 `create_dashboard` tool
New tool registered in `services/tool_handlers/dashboard_tools.py`, imported in
`tool_handlers/__init__.py` (per the existing registration convention; logger
on `_register()` failure).

- **Function name:** `create_dashboard`
- **Arguments** (pydantic-validated **upfront** — closes the known "no upfront
  pydantic tool-arg validation" harness gap for this tool):
  - `datasource_id` (str, required — the chosen DB KB id; resolved against the
    project's bound datasources to prevent cross-project IDOR)
  - `title` (str, required)
  - `widgets` (list[Widget], required, min 1) where `Widget = {type, title, sql, options}`
  - `refresh_interval_seconds` (int, default 30, clamped 10..300)
  - `description` (str, optional)
- **Behavior:**
  1. Validate the user actually has access to `datasource_id` (org/project scope).
  2. Pydantic-validate the definition.
  3. Read-only / single-statement / row-cap validation on each widget SQL (§8).
  4. On any validation failure, return a **corrective** error so the LLM can
     self-heal and retry (consistent with the existing self-heal pattern).
  5. Persist the `Dashboard` row.
  6. Return a dashboard artifact reference (`{type:"dashboard", dashboard_id, title}`)
     that the chat layer renders as a live card.
- A companion `list_datasources` read tool (or reuse an existing project-binding
  introspection) lets the agent enumerate the project's DB KBs to present the picker.

### 9.2 `dashboard-generation` skill package
Installable skill (`docs/starter_templates/` already has a starter) with a
manifest consumed by `skills_loader`, teaching the LLM:
- how to design a coherent set of widgets for a user request,
- the widget schema + `options` contract per chart type,
- to **always** call `create_dashboard` (never hand-write HTML for live dashboards),
- to ask for a datasource when one isn't named (R5),
- SQL authoring guidance (read-only, portable, aliased columns).

This makes "the agent uses available dashboard creation skills" literal.

## 10. Frontend

### 10.1 `DashboardViewer` (schema-driven, live)
`components/dashboard/DashboardViewer.jsx`:
- Props: `dashboard` (definition + refresh interval), `polling` (bool).
- Renders KPI cards, line/bar/pie charts (recharts — already bundled), and tables
  from `definition.widgets`.
- Data flow: `POST /api/dashboards/{id}/query` via `authFetch`.
- Polling lifecycle:
  - fetch on mount (fresh data on open),
  - `setInterval(refresh, refresh_interval_seconds * 1000)` while `polling && visible`,
  - pause when `document.hidden` (`visibilitychange` listener), resume on focus,
  - clear interval + listeners on unmount (no leaked timers / queries),
  - manual "Refresh now" button + "last updated" timestamp,
  - per-widget error state (red widget, message) — never a fully-blank dashboard.
- Reused identically in both the chat popup and the My Space popup.

### 10.2 `DashboardPopup`
`components/dashboard/DashboardPopup.jsx` — shared chrome, two variants:
- **Chat variant:** right-side slide-in panel anchored to the chat panel (the
  "right side of the chat panel as a popup"). Opens when a dashboard card is
  clicked; closes on backdrop click / close button.
- **My Space variant:** centered overlay modal on the same page (no route change),
  opened from the Dashboards tab list.
- Both wrap `<DashboardViewer polling />`.

### 10.3 Chat integration
- A live dashboard in an assistant message is a **dashboard message part**
  (`{type:"dashboard", dashboard_id, title}`), wired through `partitionArtifacts.js`
  and rendered by `MessageBubble.jsx` as a clickable card (title, datasource name,
  widget count, "Open" affordance). Clicking sets chat-level state that mounts
  `DashboardPopup` (chat variant).
- The existing **static** `DashboardArtifactPreview.jsx` mock is left for legacy
  static HTML artifacts; live dashboards use the new card + popup path.

### 10.4 My Space integration
- Add a `dashboard` entry to the `TABS` array in `pages/MySpace.jsx:68`
  (`{ key:'dashboard', label: t.mySpace.tabs.dashboard, icon: LayoutDashboard, entity:'Dashboard' }`).
- The tab lists dashboards for the current org/project (name, datasource, last
  refreshed). Clicking a row mounts `DashboardPopup` (My Space variant) as an
  overlay on the same page — no navigation, per the user's answer.
- Add `t.mySpace.tabs.dashboard` to the translation dictionaries.

*Assumption:* a list entry point is still needed so dashboards are discoverable;
the existing tab pattern is the natural fit. The user's "just pop up on same page"
answer is honored by making **opening** a popup (not a route change).

## 11. Real-time polling (R1 detail)

- Default interval 30s; per-dashboard `refresh_interval_seconds`, clamped to
  10s..300s at create time.
- Polling runs **only** while the popup is open (mounted). Closing the popup
  stops polling immediately.
- Pauses when the tab is hidden to avoid wasted queries; resumes on focus.
- Each poll is a single `POST /api/dashboards/{id}/query` (all widgets, one
  round-trip) — not one request per widget.
- No client-side caching of query results; every poll hits the DB for true
  real-time data. Server-side caching is explicitly **not** added in v1 (would
  defeat "real-time").

## 12. Security

- Every endpoint requires a valid session; the user's `org_id` scopes all reads.
- `create_dashboard` resolves `datasource_id` against the project's bound
  datasources for the user's org — cross-project/cross-org IDs are rejected
  (no IDOR, per audit C1).
- Fail-closed: any auth/lookup exception returns 403/404, never falls open
  (per the audit finding that perms previously failed open).
- Stored widget SQL is re-validated read-only at query time (§8) — never trusted.
- No new static-file mount / world-readable path is introduced (unlike the
  existing `/api/uploads` static mount flagged in prior audits); dashboards are
  served via authenticated JSON APIs only.

## 13. Open questions (for the planning phase)

1. **`Datasource` model vs KB binding:** v1 binds to a database `KnowledgeBase`
   to reuse `QueryService` directly. If the `Datasource` model
   (`models/datasource.py`) is the intended source-of-truth for connections, the
   `/query` endpoint needs a thin adapter from `Dashboard.datasource_kb_id` to
   whichever store `QueryService` expects. Decide during planning by confirming
   which entity the project's "datasources" picker should enumerate.
2. **SQL guard implementation:** sqlparse-based AST check vs. a curated
   allow-list regex. Pick the one that's robust across the supported engines
   without adding a heavy dependency.
3. **Picker UX:** chips/quick-reply vs. a small select. Confirm the chat-side
   affordance that fits the existing quick-reply patterns.

## 14. Testing

- **Backend:** explicit single pytest files (RAM-safe; no full-suite runs):
  - `tests/test_dashboards_api.py` — CRUD + org/project scoping + fail-closed.
  - `tests/test_dashboard_query.py` — `/query` happy path, per-widget error
    isolation, row cap, timeout, read-only rejection (DDL/DML rejected, multi-
    statement rejected).
  - `tests/test_create_dashboard_tool.py` — pydantic validation, SQL guard,
    cross-project datasource rejection, self-heal error shape.
- **Frontend:** vitest:
  - `DashboardViewer` poll lifecycle (mount→fetch, interval, visibility pause,
    unmount cleanup, per-widget error render).
  - `DashboardPopup` both variants open/close.
- DB-backed tests use the sqlite test-runtime pattern (see MEMORY.md); create
  real Project + KB rows.

## 15. File inventory

**New:**
- `backend/app/models/dashboard.py` — `Dashboard` model
- `backend/app/routers/dashboards.py` — API
- `backend/app/services/tool_handlers/dashboard_tools.py` — `create_dashboard` (+ `list_datasources` if needed) tool
- `backend/app/services/dashboard_query.py` — `/query` orchestration over `QueryService` + SQL guard
- `frontend/src/components/dashboard/DashboardViewer.jsx`
- `frontend/src/components/dashboard/DashboardPopup.jsx`
- `frontend/src/components/dashboard/DashboardCard.jsx` (chat message part)
- `frontend/src/api/dashboards.js` — client (authFetch)
- `docs/starter_templates/dashboard-generation/` — skill package (manifest + prompt)
- `backend/tests/test_dashboards_api.py`, `test_dashboard_query.py`, `test_create_dashboard_tool.py`
- `frontend/src/components/dashboard/DashboardViewer.test.jsx`

**Modified:**
- `backend/app/tool_handlers/__init__.py` — import `dashboard_tools`
- `backend/app/main.py` — register `dashboards` router + `ALTER TABLE` ensure for `dashboards`
- `backend/app/services/tool_registry.py` — register `create_dashboard` schema (role-filtered per RBAC Phase 1)
- `backend/app/services/generation_orchestrator.py` — route live-dashboard requests to the new tool instead of the static HTML fallback
- `frontend/src/pages/MySpace.jsx` — add `dashboard` tab
- `frontend/src/components/chat/MessageBubble.jsx`, `partitionArtifacts.js` — dashboard message part + card
- translation dictionaries — `mySpace.tabs.dashboard`
