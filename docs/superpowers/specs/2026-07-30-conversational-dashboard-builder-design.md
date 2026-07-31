# Conversational Dashboard Builder — Design

**Date:** 2026-07-30
**Status:** Design (brainstorming complete, ready for plan)
**Scope:** Dashboards only — chat-driven builder for the existing live-dashboard system.

## 1. Problem

Zhanlu already ships a capable live-dashboard system: the agent can call
`create_dashboard` / `update_dashboard`, and `DashboardViewer` renders widgets
with polling, filters, drill-down, and edit mode. But the *base44-style*
conversational loop is missing four things:

1. **No persistent live preview alongside chat.** `DashboardPopup` is rendered
   *inside* `MessageBubble` with per-message local state
   (`openDashboardId` in `MessageBubble.jsx:349`), so it unmounts the moment the
   user sends the next message. Every edit forces a close/reopen.
2. **No conversation ↔ dashboard binding.** Nothing records which dashboard a
   conversation is editing, so "come back tomorrow and keep refining" cannot
   reopen the right dashboard, and the agent cannot infer the target without the
   user pasting an ID.
3. **No live re-render on agent edits.** An `update_dashboard` tool call commits
   to the DB, but an already-open viewer only sees the change on its next poll
   tick (10–300 s) — not "visible instantly".
4. **No undo.** Auto-applied edits land with no gate and no way to revert.

## 2. Goals / Non-goals

**Goals**
- A dockable, resizable live-preview panel that stays open while the user chats.
- One dashboard bound per conversation; first `create_dashboard` binds it;
  subsequent edits target it automatically; reopening the chat reopens its
  dashboard.
- Agent edits (`update_dashboard`) re-render the open preview immediately.
- Lightweight undo of the last edit (and a short version history).

**Non-goals (this phase)**
- Multi-page app building, data-model creation, backend-endpoint generation
  (base44 full-app scope — explicitly deferred).
- Multiple dashboards per conversation (context-resolved disambiguation).
- Real-time multi-user co-editing of one dashboard.

## 3. Decisions (from brainstorming)

| Question | Choice |
|---|---|
| Workspace layout | **Dockable side panel** — evolve `DashboardPopup` into a persistent, resizable panel that stays open while chatting. |
| Edit application | **Auto-apply, visible instantly** — `update_dashboard` commits and the preview refreshes immediately; no propose/approve gate. |
| Conversation binding | **One dashboard per conversation** — conversation owns exactly one dashboard; agent resolves it from context. |

## 4. Existing assets (reused, not replaced)

- `Dashboard` model (`app/models/dashboard.py`) — binds one KB
  (`datasource_kb_id`), config JSON stores widgets/layout.
- `create_dashboard` + `update_dashboard` agent tools
  (`app/services/tool_handlers/dashboard_tools.py`) — pydantic-validated,
  IDOR-checked, creator-only edits.
- `DashboardViewer.jsx` — polling (10–300 s clamp, `visibilitychange` pause,
  `mountedRef` MUST stay verbatim), 10 widget types, filters, drill-down,
  edit mode, PNG export, share deep-link.
- `DashboardPopup.jsx` — current popup shell wrapping `DashboardViewer`.
- `DashboardCard.jsx` — inline chat card that opens the popup.
- `AgentConversation` (`app/models/agent_conversation.py`) — has a `metadata_`
  JSON column; `ChatSession.conversation_id` already links chat↔agent-conv.
- `create_dashboard` already emits an artifact with `dashboard_id` surfaced to
  the frontend (`MessageBubble.jsx:521`).

## 5. Design

### 5.1 Data model — conversation ↔ dashboard binding

Add a dedicated FK column to `agent_conversations` (queryable, vs. burying in
`metadata_` JSON):

```python
# app/models/agent_conversation.py
dashboard_id: Mapped[str | None] = mapped_column(
    String(36),
    ForeignKey("dashboards.id", ondelete="SET NULL"),
    nullable=True,
    index=True,
)
```

- **One dashboard per conversation**: a column (not a join table) enforces the
  1:1 cardinality at the schema level.
- `ON DELETE SET NULL`: deleting a dashboard forgets the binding without
  destroying the conversation (mirrors the existing `conversation_id` FK
  pattern in `chat_session.py:33`).
- Schema applied via runtime `ALTER TABLE` (project convention — no Alembic).
  Migration adds the column if absent on app startup.

### 5.2 Version snapshots — undo support

A new `DashboardVersion` model stores lightweight config snapshots so
auto-applied edits are reversible:

```python
# app/models/dashboard_version.py
class DashboardVersion(TimestampedBase):
    __tablename__ = "dashboard_versions"
    dashboard_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)   # full widget/layout snapshot
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)  # "agent" | "manual" | "undo"
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)  # human label, e.g. "changed KPI to bar chart"
```

- On **every** `update_dashboard` (agent or manual edit), push the *previous*
  config as a new `DashboardVersion` before writing the new one.
- Cap history at N (e.g. 20) versions per dashboard; oldest pruned.
- "Undo last edit" = pop the latest version, restore its `config`, and record a
  new version with `source="undo"` (so redo is also possible by undoing the
  undo).
- `create_dashboard` records version 1 (`source="manual"`).

### 5.3 Backend — binding + resolution

**Bind on create.** `create_dashboard` gains an optional `conversation_id`
parameter (injected by the harness from the active conversation context, like
other tools receive session context). On success, it sets
`agent_conversation.dashboard_id = new_dashboard.id`. If the conversation
already had a bound dashboard, the new one **replaces** the binding (the old
dashboard is not deleted — it just becomes unbound from this chat; the user can
still find it in MySpace).

**Resolve on update.** `update_dashboard` gains an optional `conversation_id`
parameter. When `dashboard_id` is omitted, the tool resolves the target from
the conversation's `dashboard_id`. If none is bound → the agent is told "no
dashboard bound to this conversation yet; create one first" (a structured tool
error the agent can recover from). If `dashboard_id` is supplied explicitly, it
is honored (and must pass the existing IDOR/ownership check).

**New read endpoint.**
`GET /api/dashboards/by-conversation/{conversation_id}` → returns the bound
dashboard (or 404) with `can_edit`, used by the frontend to reopen the panel on
chat load. Auth/ownership checked against the conversation's owner.

**Undo endpoint.**
`POST /api/dashboards/{id}/undo` → pops the latest `DashboardVersion`, restores
config, returns the updated dashboard. Creator-only. Also exposed as an agent
tool `undo_dashboard_edit` (optional `conversation_id` resolution) so the user
can say "undo that" in chat.

### 5.4 Frontend — persistent docked panel

**Lift state out of `MessageBubble`.** The panel's open-state and
`dashboardId` move from `MessageBubble.jsx:349` (per-message, unmounts) up to
the `Chat` page level so they survive across messages. `MessageBubble` keeps
only the `DashboardCard` trigger, whose `onOpen` now calls a lifted
`openDashboard(id)`.

**Evolve `DashboardPopup` → `DashboardDockPanel`.** Same shell, but:
- Docked to the right edge of the chat layout (not a modal overlay), resizable
  via a drag handle (min/max width clamped), collapsible to a slim rail with a
  "expand" button.
- Stays mounted for the lifetime of the chat session.
- Auto-opens (collapsed or expanded per user preference) when the conversation
  has a bound dashboard on chat load (calls the new
  `by-conversation` endpoint).
- Reuses `DashboardViewer` verbatim — no changes to its polling/`mountedRef`
  contract.

**Live re-render on agent edits.** The chat already consumes an SSE/tool-result
stream. Add a watcher: when a tool result for `update_dashboard` (or
`create_dashboard`, or `undo_dashboard_edit`) arrives in the active
conversation, call a new `refetch()` exposed by `DashboardViewer` (via ref /
imperative handle) to refresh immediately — bypassing the poll interval. This
is the "visible instantly" path. If no panel is open yet but a
`create_dashboard` result arrives, auto-open the docked panel to the new
dashboard.

**Undo control.** A small "↶ Undo last edit" button in the panel header (next
to Edit) calls `POST /api/dashboards/{id}/undo`, then triggers the same
`refetch()`. Disabled when no version history > 1 exists (the endpoint can
return `can_undo` in the dashboard payload).

### 5.5 Agent context — "this conversation's dashboard"

The agent must know, without being told, which dashboard it is editing. Two
pieces:

1. **System prompt injection.** When a conversation has a bound dashboard, the
   agent's system/context prompt includes a compact block:
   `Bound dashboard: <id> "<title>" (N widgets, datasource: <kb name>)`. This
   lets the agent reason about "the current dashboard" and call
   `update_dashboard` without an explicit ID.
2. **Tool defaulting.** As in 5.3, `update_dashboard` resolves the target from
   `conversation_id` when `dashboard_id` is absent, so the agent never *needs*
   to track the ID — but it may pass it for precision.

## 6. Interaction flow (happy path)

1. User: *"Build me a sales dashboard — KPI for revenue, bar chart by region,
   table of top 10 deals."*
2. Agent calls `create_dashboard(conversation_id=<ctx>, …)` → dashboard v1
   created, **bound** to the conversation. Tool result streams back.
3. Frontend watcher sees `create_dashboard` result → **auto-opens the docked
   panel** to the new dashboard. `DashboardViewer` renders v1.
4. User: *"Make the revenue KPI bigger and change the bar chart to red."*
5. Agent calls `update_dashboard(conversation_id=<ctx>, …)` (no ID needed) →
   previous config pushed to `dashboard_versions`, new config written.
6. Frontend watcher sees `update_dashboard` result → `DashboardViewer.refetch()`
   → panel re-renders instantly with red bars and a bigger KPI.
7. User: *"Hmm, undo that — keep the bigger KPI but revert the color."* →
   agent calls `undo_dashboard_edit(conversation_id=<ctx>)` (or the user clicks
   the header Undo button) → config restored → instant refetch.
8. User closes the tab. Next day, reopens the chat → frontend calls
   `GET /api/dashboards/by-conversation/{id}` → docked panel auto-opens to the
   bound dashboard. User: *"Now add a line chart for weekly trend."* → loop
   continues.

## 7. Edge cases

- **Conversation already bound, user says "create a new dashboard"** → new
  dashboard replaces the binding; the previously-bound dashboard remains
  accessible from MySpace. Panel switches to the new dashboard.
- **`update_dashboard` with no bound dashboard** → tool returns a structured
  error ("no dashboard bound to this conversation; call create_dashboard
  first"); agent explains to the user.
- **Bound dashboard deleted out-of-band** (e.g. from MySpace) → `dashboard_id`
  FK becomes NULL via `ON DELETE SET NULL`; `by-conversation` returns 404;
  panel closes gracefully; next `create_dashboard` rebinds.
- **Panel closed when an edit lands** → edit still commits; a subtle "dashboard
  updated" affordance appears on the `DashboardCard` / chat so the user can
  reopen to see it.
- **Undo with no history** → endpoint returns 409 / `can_undo=false`; button
  disabled.
- **Concurrent manual edit (edit mode) while agent edits** → last-write-wins on
  config JSON (existing behavior); both paths push a version, so either can be
  undone. No merge logic this phase.

## 8. Testing

- **Backend (pytest, explicit paths, no `-n`):**
  - Binding: `create_dashboard` with `conversation_id` sets
    `agent_conversation.dashboard_id`; re-create replaces.
  - Resolution: `update_dashboard` without `dashboard_id` resolves from
    conversation; missing binding → structured error.
  - `GET /api/dashboards/by-conversation/{id}` auth + ownership + 404.
  - Versions: each `update_dashboard` pushes a version; cap enforcement; undo
    restores + records `source="undo"`.
  - `POST /api/dashboards/{id}/undo` creator-only; 409 when no history.
- **Frontend (vitest):**
  - Docked panel state survives across messages (no unmount on send).
  - `create_dashboard` tool-result watcher auto-opens panel.
  - `update_dashboard` tool-result watcher calls `refetch`.
  - Auto-open on chat load via `by-conversation` (mocked).
  - Undo button calls endpoint + refetch; disabled when `can_undo=false`.
- **E2E (live, vs Postgres):** create → bind → edit → instant re-render → undo
  → close → reopen → continue. Reuse the `admin@zhanlu.dev`/`admin123`
  credentials convention (NOT `Test1234!`).

## 9. Risks

- **State-lift refactor in `Chat`/`MessageBubble`** is the largest frontend
  change; `MessageBubble.jsx` is already large. Keep the lift minimal — only
  `openDashboardId` + `openDashboard`/`closeDashboard` move up; the card
  trigger stays in the bubble.
- **`DashboardViewer` refetch contract** — must add an imperative `refetch`
  without disturbing the polling/`mountedRef`/`visibilitychange` invariants
  (these are load-bearing, per project memory). Expose via `useImperativeHandle`
  and call the existing fetch path.
- **Version storage growth** — cap at 20/dashboard; `config` JSON is small
  (widget defs + SQL), acceptable. Monitor if dashboards grow huge widget
  counts.
- **Agent mis-resolution** — if the agent passes a stale/wrong `dashboard_id`
  explicitly, the existing IDOR/ownership check already rejects it; no new
  risk.

## 10. Phasing (for the implementation plan)

- **Phase A — Binding + resolution (backend):** `dashboard_id` column +
  migration; `create_dashboard`/`update_dashboard` `conversation_id` param +
  resolution; `by-conversation` endpoint. Tests.
- **Phase B — Versions + undo (backend):** `DashboardVersion` model +
  migration; snapshot-on-edit; `undo` endpoint + `undo_dashboard_edit` tool;
  `can_undo` in payload. Tests.
- **Phase C — Docked panel (frontend):** lift state; evolve `DashboardPopup` →
  docked/resizable; auto-open on chat load. Tests.
- **Phase D — Live re-render + undo UI (frontend):** tool-result watcher →
  `refetch`; header Undo button. Tests + E2E.

Each phase is independently shippable and testable.
