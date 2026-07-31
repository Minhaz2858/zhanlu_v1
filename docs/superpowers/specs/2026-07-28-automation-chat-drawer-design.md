# Automation Chat Drawer + Manus-Style Run Panel — Design

**Date:** 2026-07-28
**Status:** Draft (pending user review)
**Owner:** Backend + Frontend
**Branch:** `feat/auth-hardening`

---

## 1. Context

The automation list page (`/automation`) currently shows each task with a row-level drawer that triggers an immediate run via `runNow(task)` and refreshes an `ExecutionLogsList`. Users report two related UX problems:

1. **"Run now"** silently calls the backend and shows the result as a list entry in the drawer. Users expect Manus-style behaviour: clicking Run now should **open the chatbot** with the task prefilled, the agent runs the task, and the result streams into the chat.
2. The drawer renders as a **modal-style popup with a dimmed backdrop** (`fixed inset-0 bg-black/20`). Users expect a Manus-style **right side panel** that slides in without obscuring the rest of the page, showing the task summary at the top and a **"Past runs"** list (dated cards with summaries) below.
3. The `ExecutionLogsList` shows "Failed to load runs" — the data fetch path is broken (the endpoint or payload shape doesn't match what the component expects).

This spec addresses the three issues above. The downstream DeepSeek 400 error in the v3 streaming LLM path is intentionally **out of scope** (separate investigation).

---

## 2. Goals & Non-Goals

### Goals
- "Run now" opens the chatbot with the task prefilled; the agent auto-executes the task via a new tool; the result streams into the chat.
- The drawer becomes a Manus-style right-side panel with no backdrop, no dimming.
- The right panel renders past runs as dated cards with summary text.
- "Failed to load runs" is fixed.

### Non-Goals
- Fixing the DeepSeek 400 in the v3 streaming path.
- Removing the `AutomationTaskDetail.jsx` page (kept as deep-link "Open full detail").
- Persisting the chat-drawer's open state across navigation.
- Mobile responsive behaviour beyond what the existing drawer already has.

---

## 3. Architecture

Two changes, one page, two new backend pieces:

```
Frontend                                            Backend
─────────────────────────────────────────────────   ──────────────────────────────────────
AutomationTasks.jsx                                 app/services/automation_chat_tool.py
  ├─ Task table                                     (NEW) execute_automation(task_id)
  ├─ RightSidePanel (no backdrop)                     ├─ validate ownership
  │   ├─ Task summary                                ├─ create AutomationExecution
  │   ├─ PastRuns list                               ├─ dispatcher.schedule_run(...)
  │   └─ [Run now] ──┐                               └─ poll up to 5s, return output
  │                   ▼
  └─ ChatDrawer (open with initialMessage)         app/services/automation_runtime.py
        └─ initialMessage =                          (existing) ensure runtime agent per
            "Run my <Task Name> now"                            (org, app) — unchanged
        └─ auto-submit on open
        └─ agent streams response
```

---

## 4. Frontend Changes

### 4.1 `AutomationTasks.jsx`

**Current state** (around lines 217–283):
- Drawer is a modal popup: `fixed inset-0 z-30 bg-black/20` backdrop + a right-side panel.
- `runNow(selectedTask)` calls `POST /api/automations/{id}/run` and bumps a refresh key.
- `ExecutionLogsList` shows "Failed to load runs".

**New state:**
- Replace the backdrop div with a Manus-style slide-in: the right panel itself has no dim overlay. The page stays interactive behind it.
  ```jsx
  // Before:
  <div className="fixed inset-0 z-30 bg-black/20" onClick={close}>
    <aside className="...right-side-panel...">...</aside>
  </div>

  // After:
  <aside className="fixed right-0 top-0 z-30 h-full w-[420px] bg-background shadow-xl">
    ...
  </aside>
  ```
- "Run now" no longer calls the API directly. It sets state that opens the ChatDrawer:
  ```jsx
  async function runNow(task) {
    setChatDrawerState({
      open: true,
      initialMessage: `Run my ${task.name} now`,
    });
  }
  ```
- The Past Runs section queries the **working endpoint** (see 4.2) and renders each execution as a card:
  ```jsx
  {executions.map(e => (
    <button key={e.id} onClick={() => openExecutionInChat(e)} className="...">
      <span className="text-xs text-muted-foreground">{formatDate(e.created_date)}</span>
      <span className={statusColor(e.status)}>{e.status}</span>
      <p className="text-sm text-foreground line-clamp-2">{e.output_text || e.error || '...'}</p>
    </button>
  ))}
  ```
  The "Open execution" action deep-links into the chat conversation that received the result.

### 4.2 Fix "Failed to load runs"

The `ExecutionLogsList` component fetches executions but the call shape doesn't match whatever the backend exposes. The implementation will locate the existing GET endpoint that returns `AutomationExecution` rows for a task (either `GET /api/automations/{task_id}/executions` in `routers/automations.py`, or the equivalent), confirm the response field names, and update the component to call that endpoint with matching query parameters and rendering. The endpoint is the source of truth — the component adapts to it.

### 4.3 `ChatDrawer.jsx` (or existing chat component)

- Add a new prop `initialMessage` (string | null). When `open` transitions from `false` to `true` with a non-null `initialMessage`:
  1. Set the chat input value to `initialMessage`.
  2. Auto-submit it.
- The drawer state is exposed via a simple module-level event emitter OR React context (`ChatDrawerContext`) so `AutomationTasks.jsx` can open it from anywhere without prop-drilling.

If a shared context feels heavy, the minimum viable alternative is a URL-based trigger: `/automation?runNow=<task_id>` and the chat drawer reads `task_id` from the URL on mount. The context approach is preferred for consistency with how the rest of the app surfaces modals.

---

## 5. Backend Changes

### 5.1 New tool module: `app/services/automation_chat_tool.py`

```python
def execute_automation_tool(args: dict, db: Session, user: User) -> dict:
    """Chat-agent tool: run an AutomationTask now and return its output.

    Validates:
      - user has access to the task (created_by_id == user.id OR shared/team)
      - task is not soft-deleted, status != 'archived'

    Does:
      - Calls dispatcher.schedule_run(task.id) to create an AutomationExecution
      - Polls execution.status every 500ms up to 5s
      - Returns {execution_id, status, output_text} on completion
      - Returns {execution_id, status: 'running'} if still pending after timeout
        (the existing chat notification will deliver the final result via SSE)
    """
```

The function is registered in the **chat agent's tool whitelist** (system agents like `general_assistant`) so any chat conversation can trigger an existing task by id or name. The tool resolves the task by `task_id` if provided, otherwise by `task_name` (case-insensitive match against the user's own tasks).

When the user sends `"Run my Daily Sales Data Sync now"`:
1. The chat agent (LLM) calls `list_my_automations` (existing tool) to find the task id.
2. It calls `execute_automation(task_id=...)`.
3. The tool returns the output synchronously (or `running` + lets the existing chat-notification path deliver the async result).

### 5.2 Tool registration

Add `execute_automation` to the enabled-tools list of system chat agents. Existing tools list in `system_agents.py` is updated to include `"execute_automation"`. No database migration needed if the tool whitelist is read from `tool_config` at runtime (which it is — the agent's `tool_config.enabled_tools` is loaded fresh on each call).

For agents that were seeded before this change, re-run `ensure_system_agents(db)` during deployment (already happens on startup).

### 5.3 Ownership guard

```
def _user_can_run_task(db, user, task) -> bool:
    return task.created_by_id == user.id or task.shared_with_user_ids includes user.id
```

The first condition is sufficient for the current app scope (team sharing is not yet a feature). The function is intentionally simple and easy to extend later.

---

## 6. Data Flow (Run Now happy path)

```
1. User clicks "Run now" in the right-side panel
2. AutomationTasks.jsx sets { chatDrawer: { open: true, initialMessage: "Run my X now" } }
3. ChatDrawer opens, input populated, auto-submit
4. POST /api/apps/{app}/agents/conversations/v3/{id}/messages/stream
   body: { role: "user", content: "Run my Daily Sales Data Sync now" }
5. Chat agent LLM loop:
   - calls list_my_automations → { tasks: [{ id: "abc", name: "Daily Sales Data Sync" }] }
   - calls execute_automation(task_id="abc")
   - tool creates AutomationExecution, kicks dispatcher, returns { execution_id, status, output_text }
   - LLM composes final answer: "I ran your 'Daily Sales Data Sync' task. Here's the output: ..."
6. SSE streams the answer into the chat
7. AutomationTasks.jsx's past-runs list refetches (refreshKey++) so the new run appears
```

---

## 7. Error Handling

| Failure | User-visible behaviour |
|---|---|
| Task not found / soft-deleted | Tool returns `{ error: "Task X not found" }`; agent says "I couldn't find a task called X." |
| Task owned by another user | Tool returns `{ error: "Permission denied" }`; agent says "That task isn't yours." |
| Dispatcher fails to enqueue | Tool raises; chat surfaces "Sorry, I couldn't start that run." |
| Run times out (5 s poll) | Tool returns `{ execution_id, status: "running" }`; agent says "Started, you'll be notified when it completes." Existing `_notify_chat_failure` / success notification delivers the result. |
| "Failed to load runs" (legacy) | Endpoint mismatch fixed in 4.2; if it still occurs, the right panel shows a retry button with the endpoint URL in dev mode. |

---

## 8. Tests

### Backend (TDD)
- `test_automation_chat_tool.py` — new file
  - `test_execute_automation_runs_task_and_returns_output`
  - `test_execute_automation_denies_other_users_task`
  - `test_execute_automation_returns_running_after_timeout`
  - `test_execute_automation_resolves_task_by_name`
- All 23 existing automation tests must continue to pass.

### Frontend (manual smoke)
- Click Run now → chat drawer opens with prefilled message → agent response streams → result appears in chat.
- Open the right panel → past runs render as dated cards.
- Refresh the page → right panel state resets (drawer closed).
- Open from a deep link (`/automation?task=ID`) → right panel opens to that task.

### Regression
- Existing `runNow` callers in the code are updated; no other callers exist.
- `ExecutionLogsList` is updated to use the correct endpoint; no other consumers.

---

## 9. Rollout & Rollback

### Rollout
1. Deploy backend: `app/services/automation_chat_tool.py` + `system_agents.py` update + restart.
2. Deploy frontend: rebuilt bundle with new drawer + tool wiring. **Frontend must be rebuilt** — vite build required for the served bundle to update (no HMR in this environment).
3. No DB migration needed.

### Rollback
- Backend: revert the two file changes, restart. The existing `execute_automation` (the dispatcher entry point) is untouched, so the rest of the automation pipeline keeps working.
- Frontend: rebuild the previous bundle.

---

## 10. Open Questions (resolved during implementation, not blockers)

- **Q:** Does the existing chat already auto-delivers execution results via the `_notify_chat_failure` / success path? **A:** To be confirmed during Task 1. If yes, the tool's polling can return immediately and rely on the notification. If no, we'll need a small new "success notification" hook in the dispatcher.
- **Q:** Where does the past-runs endpoint live today? **A:** `GET /api/automations/{task_id}/executions?limit=20` in `routers/automations.py`. The implementation will reconfirm the exact response shape before writing the component.
