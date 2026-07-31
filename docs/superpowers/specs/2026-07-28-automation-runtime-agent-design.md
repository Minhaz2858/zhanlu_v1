# Automation Runtime Agent Design

**Date:** 2026-07-28
**Status:** Draft — awaiting user review
**Author:** CodeBuddy (brainstorming session)
**Related code:**
- `backend/app/services/system_agents.py` (existing system agent registry)
- `backend/app/services/automation_executor.py` (resolver + executor)
- `backend/app/services/automation_dispatcher.py` (scheduling)
- `backend/app/services/agent_tools.py` (create_automation)
- `backend/app/models/agent_app.py` (AgentApp model)
- `backend/app/models/automation_task.py` (AutomationTask model)
- `frontend/src/pages/AutomationTaskDetail.jsx` (UI detail page)
- `frontend/src/pages/AutomationTasks.jsx` (UI list page)

---

## 1. Problem

`AutomationTask` records can be created without a valid executor agent. When the dispatcher tries to run a task whose `agent_id` is `NULL` and the workspace contains more than one user Agent, the executor fails with:

> `No agent available: N agents exist in the workspace and none is pinned on the task`

This is an internal implementation detail being surfaced to the user. It also leaks the fact that automation execution is "agent-bound" when in reality, scheduled runs should be driven by a hidden, deterministic backend engine.

Goals:

1. Every `AutomationTask` always has a valid executor agent bound to it.
2. Users never see the runtime agent in any Agent list.
3. Project data is isolated by `project_id`, data-source bindings, and permissions — not by spawning one agent per project.
4. Existing tasks with `agent_id = NULL` are repaired automatically.
5. Failures are retried, escalated via chat, and observable.
6. The agent builds per-project memory across runs.

Non-goals:

- One agent per automation.
- Per-project custom model / credentials routing.
- Multi-tenant isolation beyond what `org_id` already provides.
- A new visual UI for the runtime agent.

---

## 2. Architecture

### 2.1 High-level model

```text
User creates an automation
          |
          v
   AutomationTask (in DB)
          |  agent_id  -- auto-bound -->
          v
   Hidden `automation_runtime_agent`
   (one per (org_id, app_id))
          |
          |  per-project conversation
          v
   AgentConversation (per project_id)
          |
          v
   Project data, KB, files, notifications
```

### 2.2 Key design rules

- **Creating agent != Executing agent.** The chat agent that creates a task is never the executor.
- **One runtime agent per `(org_id, app_id)`.** All projects in the same org/app share it.
- **Project isolation via conversation + permissions.** The runtime agent holds one `AgentConversation` per `project_id`; data access is gated by the project's data-source bindings and permission checks, not by agent records.
- **Hidden from UI.** `is_system = true` and a new `role = "automation_runtime"` field, plus API-level filtering, keep the agent out of user-facing lists.
- **Self-healing.** If the runtime agent is missing, recreate it on the next lookup.

---

## 3. Data model changes

### 3.1 `AgentApp` model (`backend/app/models/agent_app.py`)

Add a new column:

```python
role: Mapped[Optional[str]] = mapped_column(
    String(64), nullable=True, index=True,
    doc="Functional role of the agent. NULL = user agent. "
        "'automation_runtime' = hidden executor for scheduled automations.",
)
```

Backfill: any existing `automation_runtime_agent` records created by this design get `role = "automation_runtime"`. User agents remain `NULL`.

No change to `is_system` — that flag already means "ships with the platform"; the new `role` field is more specific.

### 3.2 `AutomationTask` model — no schema change

`agent_id` already exists. We just enforce that it is non-null and points at a runtime agent for new tasks, and we backfill old tasks.

### 3.3 `AgentConversation` — no schema change

We will reuse the existing `AgentConversation` model and add a deterministic lookup by `(agent_id, project_id)` for the runtime agent.

If the project-conversation does not exist, we create it on first run with a `system_init` message that records the runtime agent's mission and the project's tool/data scope.

---

## 4. Component design

### 4.1 `system_agents.ensure_automation_runtime_agent` (new function)

**File:** `backend/app/services/system_agents.py`

**Signature:**

```python
def ensure_automation_runtime_agent(
    db: Session,
    org_id: int,
    app_id: int,
) -> AgentApp:
    """Idempotently get-or-create the hidden automation runtime agent.

    The agent:
    - is_system = True
    - role = "automation_runtime"
    - status = "active"
    - is hidden from user-facing Agent lists
    - has a strict tool whitelist (read data, write files, notify, web)
    - uses a project-aware system prompt
    """
```

**Behavior:**

1. Look up an existing `AgentApp` with `name = "automation_runtime_agent"`, `org_id = org_id`, `app_id = app_id`, `is_system = True`, `role = "automation_runtime"`, `is_deleted = False`.
2. If found, return it.
3. If not found, create it with:
   - `name = "automation_runtime_agent"`
   - `description = "Hidden executor for scheduled automations. Not user-editable."`
   - `manifest_json` with mission, allowed/forbidden tools, and project-isolation rules
   - `tool_config.enabled_tools` containing the four tool categories (read, write, notify, web)
   - `system_prompt` instructing the agent to honor the task's `project_id`, treat each run as stateless-from-its-own-perspective but read the per-project memory, and never invoke `create_automation`/`update_automation`/`delete_automation`
   - `created_by_id = NULL` (system-owned)
   - `is_system = True`, `role = "automation_runtime"`, `status = "active"`
4. Return the agent.

### 4.2 Eager provisioning

**File:** `backend/app/services/system_agents.py` and any org/app provisioning paths.

When an Organization or App is created, call `ensure_system_agents(db, org_id, app_id)`. The existing `ensure_system_agents` already provisions the management-side `automation_agent`; we extend it to also call `ensure_automation_runtime_agent`.

For existing orgs/apps in the database, add a one-time backfill at server startup (in `app/main.py` startup hook) that iterates all `(org_id, app_id)` pairs and ensures the runtime agent exists.

### 4.3 `get_or_create_project_conversation` (new function)

**File:** `backend/app/services/automation_executor.py` (or a new `services/automation_memory.py`)

**Signature:**

```python
def get_or_create_project_conversation(
    db: Session,
    runtime_agent: AgentApp,
    project_id: int,
) -> AgentConversation:
    """Idempotently get or create the per-project memory conversation
    for the automation runtime agent.
    """
```

**Behavior:**

1. Look up `AgentConversation` by `(agent_id = runtime_agent.id, project_id = project_id)`.
2. If found, return it.
3. If not found, create one with a `system` role message that records the project context, the tool whitelist, and the runtime mission.
4. Return it.

### 4.4 Auto-binding at creation

**File:** `backend/app/services/agent_tools.py` — modify `_create_automation`.

```python
# After computing org_id, app_id, project_id, status...
runtime_agent = ensure_automation_runtime_agent(db, org_id, app_id)
agent_id = args.get("agent_id") or runtime_agent.id
# If user supplied an agent_id, validate it belongs to the same org/app
# and is not deleted; otherwise fall back to runtime_agent.
```

The user-supplied `agent_id` is honored only if it is valid and matches the org/app. The chat agent (`TOOL_CONTEXT["agent_app_id"]`) is **no longer** used as the default executor.

### 4.5 `_resolve_agent` update

**File:** `backend/app/services/automation_executor.py`

**New resolution order:**

```text
1. task.agent_id, validated:
   - same org_id
   - same app_id
   - not is_deleted
   - status = active
2. ensure_automation_runtime_agent(org_id, app_id)  -- self-healing fallback
3. Fail with: "AutomationTask <id> has no usable executor.
   The runtime engine is unavailable. Please contact support."
```

The current code's behavior of "if 1 candidate, use it; if many, fail" is removed. The current "random workspace agent" fallback is removed.

The failure reason is never exposed to end users; it is logged, and a clean message goes to the user via chat notification.

### 4.6 Retry and failure handling

**File:** `backend/app/services/automation_executor.py`

```python
RETRY_DELAYS_SECONDS = [30, 120, 600]  # 30s, 2m, 10m

def _execute_with_retry(task, run):
    last_error = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            return _run_once(task, run)
        except _TransientError as e:
            last_error = e
            task.last_error = str(e)
            task.retry_count = attempt
            db.commit()
            continue
        except Exception as e:
            # Non-transient: fail immediately
            last_error = e
            break
    _mark_failed_and_notify(task, run, last_error)
    raise
```

A `_TransientError` wrapper is introduced. It is raised by the executor wrapper around any of these underlying exceptions: `httpx.HTTPError` (5xx only, not 4xx), `openai.APITimeoutError`, `openai.RateLimitError`, and SQLAlchemy `OperationalError` (connection drops). Other exceptions (validation, permission denied, 4xx) are treated as non-transient.

After the final retry, `_mark_failed_and_notify`:

1. Sets `task.status = "failed"`, `task.last_error = str(e)`, `task.finished_at = now`.
2. Calls `send_message` to the original `task.session_id` with a structured failure message:
   > `[Zhanlu Automation] "Daily sales summary" failed after 3 retries. Reason: <error>. View execution history for details.`
3. Increments a `consecutive_failure_count` on the task. (Future enhancement: auto-pause after N; not in this scope.)

### 4.7 Memory write policy

When a run finishes successfully, append a summary message to the per-project conversation:

- For successful runs: `[run <run_id> at <iso>] prompt: <short> | output: <short summary or file path> | status: ok`
- For failed runs: `[run <run_id> at <iso>] prompt: <short> | status: failed | reason: <short>`

The conversation is trimmed to a configurable window (default 50 messages or 16k tokens, whichever is smaller) to keep LLM context bounded.

### 4.8 UI changes

**File:** `frontend/src/pages/AutomationTaskDetail.jsx`

- Replace any visible agent id / picker with a static badge: `Backend engine: Connected` (green) or `Backend engine: Unavailable` (red).
- On error display, show: `This automation could not run because the backend execution engine is unavailable. Check execution history for details.`
- The detailed internal error goes to the Execution History accordion.

**File:** `frontend/src/pages/MySpace.jsx` (already filters system agents)

- Verify the new `role = "automation_runtime"` agent is filtered. The existing `filterUserAgents()` checks `is_system`; we add a check for `role === "automation_runtime"`.

**File:** `backend/app/routers/agents.py`

- The user-facing `GET /agents` endpoint filters out agents where `is_system = true OR role = "automation_runtime"`.
- Add a `GET /agents/runtime` (platform-admin only, gated by the existing `is_platform_admin` user flag) endpoint for diagnostics that returns the runtime agent's status and last-used timestamp.

### 4.9 Backfill migration

**File:** `backend/app/migrations/<timestamp>_backfill_automation_runtime_agent.py` (Alembic)

```python
def upgrade():
    # 1. Add `role` column to AgentApp if not present
    op.add_column("agent_app", sa.Column("role", sa.String(64), nullable=True))
    op.create_index("ix_agent_app_role", "agent_app", ["role"])

    # 2. For every (org_id, app_id) that has an AutomationTask,
    #    ensure_automation_runtime_agent exists.
    # 3. For every AutomationTask with agent_id IS NULL
    #    OR agent_id pointing at a non-runtime agent,
    #    re-bind to the runtime agent.
    #    Skip if the user explicitly stored a non-system agent in agent_id
    #    AND that agent still exists and is valid.
```

This is idempotent and safe to re-run.

---

## 5. Data flow

### 5.1 Create automation

```text
User chat → agent_tools._create_automation
  1. Parse args (name, schedule, prompt, project_id, ...)
  2. Resolve org_id, app_id from session context
  3. runtime_agent = ensure_automation_runtime_agent(db, org_id, app_id)
  4. agent_id = validate(args.agent_id) OR runtime_agent.id
  5. Insert AutomationTask(..., agent_id = agent_id, status = "active")
  6. Return task to chat
```

### 5.2 Run automation (scheduler-triggered)

```text
automation_dispatcher.tick
  1. Find tasks where next_run_at <= now AND status = "active"
  2. For each task: create AutomationRun, call execute_automation
       a. _resolve_agent(task) → runtime_agent
       b. get_or_create_project_conversation(runtime_agent, project_id)
       c. _execute_with_retry(task, run)
            i.   Build prompt: task.prompt + recent conversation + tool list
            ii.  Invoke LLM with agent's tool config
            iii. On tool calls: enforce whitelist, scope by project_id
            iv.  On success: write artifacts, append summary to conversation, send_message
            v.   On failure: retry per policy
       d. _mark_failed_and_notify on final failure
```

### 5.3 User deletes automation

- `DELETE /api/automation/<id>` — only deletes the `AutomationTask` and its `AutomationRun` history.
- The hidden runtime agent is **not** deleted; it is shared.

---

## 6. Error handling

| Error | Handling | User-visible message |
|---|---|---|
| Runtime agent missing on resolver call | Self-heal: call `ensure_automation_runtime_agent`. Retry once. | "Backend engine reconnecting…" |
| Transient LLM / DB error | Retry with backoff (30s, 2m, 10m). | (none until final) |
| Non-transient error | Fail fast, mark `failed`, notify user. | "This automation could not run. See execution history." |
| Final retry exhausted | Mark `failed`, notify user in chat. | "[Zhanlu Automation] '<name>' failed after 3 retries…" |
| Project data source not found | Fail the run with clear error. | (logged) |
| User agent_id supplied but invalid | Fall back to runtime agent; log a warning. | (none) |
| Conversation write fails | Log error, continue run; do not fail user-visible task. | (none) |

---

## 7. Security & permissions

- The runtime agent is `is_system = True` and `role = "automation_runtime"`. It cannot be edited through user-facing Agent APIs.
- The runtime agent can only access data sources bound to the task's `project_id`. Data-source access checks already exist in the executor and in tool implementations (e.g. `query_database`); during implementation we will verify that these checks apply when the calling agent is the runtime agent and that the existing `org_id`/`app_id` scope on the agent is respected. If any check is missing, we add it as part of this work and add a regression test.
- The runtime agent is not allowed to invoke `create_automation`, `update_automation`, `delete_automation`, `create_agent`, or `delete_agent` even if those tools exist in the broader tool registry.
- The runtime agent's `created_by_id = NULL` (system-owned) and the user cannot take ownership of it.
- The runtime agent is excluded from all `GET /agents` listing endpoints except the admin diagnostic one.

---

## 8. Testing

### 8.1 Unit tests

- `test_ensure_automation_runtime_agent.py`
  - Idempotent: calling twice with same (org, app) returns same record.
  - Creates with `is_system=True`, `role="automation_runtime"`.
  - Returns existing agent if already present (no duplicate).
- `test_resolve_agent.py`
  - Honors `task.agent_id` when valid.
  - Falls back to runtime agent when `task.agent_id` is NULL.
  - Self-heals when runtime agent is missing.
  - Fails cleanly (not random workspace agent) when nothing exists.
- `test_get_or_create_project_conversation.py`
  - Returns same conversation on repeat calls.
  - Different `project_id`s get different conversations.
- `test_backfill_migration.py`
  - Migration is idempotent.
  - Old NULL `agent_id` tasks are rebound to runtime agent.
  - User agents with valid `agent_id` are preserved.

### 8.2 Integration tests

- `test_create_automation_auto_binds.py`
  - User creates automation via chat tool. Resulting `AutomationTask.agent_id` is the runtime agent, not the chat agent.
- `test_execute_automation_end_to_end.py`
  - Scheduled run resolves runtime agent, retrieves project memory, runs LLM with tool calls, writes artifact, notifies chat.
- `test_failure_retry_and_notify.py`
  - Run fails 3x → task marked failed → user receives chat notification.
- `test_hidden_from_ui.py`
  - `GET /agents` (user scope) does not return the runtime agent.
  - `GET /agents/runtime` (admin scope) does.

### 8.3 Regression tests

- Existing automations with `agent_id = NULL` are auto-bound on next run.
- Existing `automation_agent` (management) keeps working for `create_automation` and `update_automation` tool calls.

---

## 9. Migration & rollout

1. Ship the `role` column migration.
2. Deploy `ensure_automation_runtime_agent` and the eager provisioning hook.
3. Run backfill migration to:
   - Create runtime agent for every (org, app) that has any `AutomationTask`.
   - Re-bind `agent_id` on tasks where the existing value is NULL or invalid.
4. Deploy `_resolve_agent` update + retry logic.
5. Deploy UI changes.
6. Monitor for one week; if clean, remove the old "fall back to first workspace agent" code path entirely.

Rollback plan:

- The runtime agent is additive; if the resolver change causes regressions, the previous resolver is still in git history and can be restored.
- The backfill is reversible: we can record the original `agent_id` of each rebound task in a side table and restore it.

---

## 10. Out of scope (future work)

- Auto-pause after N consecutive failures (mentioned in the design summary, not built in this iteration; the counter is recorded).
- Per-project runtime agents for projects with different models/credentials.
- A "test run" UI action that previews a scheduled run without waiting for the next tick.
- A runtime agent admin dashboard.
- Cross-project memory.

---

## 11. Open questions

- None. All clarifying questions answered by the user on 2026-07-28.
