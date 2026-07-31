# 2026-07-28 — Run Now handoff to chat (direct API + one-task-one-session)

## Context

The Automation page's "Run Now" button needed to feel like a single
"click → see result in chat" flow, the way Manus AI does it. The user
clarified the requirement with the Manus screenshot: every automation
has a dedicated chat session, Run Now goes to that session, and the
chat history is the run history (auto-scheduled runs also appear in
the same chat).

## v1 attempt (reverted)

First attempt tried to auto-send a "Run my X now" prefill from the Run
Now button through the chat LLM (so the LLM would call the
`execute_automation` agent tool). **This did not work** — the chat is
usually using a *custom* agent (e.g. the user's "test" agent), NOT
`general_assistant`. Looking at `backend/app/services/system_agents.py:241`,
the `automation_agent` has `"forbidden": ["delete_automation",
"execute_automation", "access_user_data"]` — it's the create/configure
agent, not the run agent. So the LLM received "Run my X now", looked
for the right tool, couldn't find `execute_automation` (forbidden or
not in the active agent's toolset), and fell back to whatever tools
the active agent has (typically config-edit tools). The user saw
"I've completed the requested changes" instead of the run.

## Decision (v2, current)

**Never route a "Run my X now" message through the LLM in a chat using
a custom agent.** Reliable path:

1. **Run Now**: fire `POST /api/automations/{id}/run` directly
   (bypasses the LLM entirely). Navigate to `?session=<task.session_id>`.
   The dispatcher's `_notify_chat` writes the result to the session.
   The user watches the run stream into the chat.
2. **One task = one chat session**: in `_create_automation`, when
   `session_id` is None (task created outside a chat), auto-create a
   new `AgentConversation` + `ChatSession` pair named after the task
   and link them. Ensures every NEW task has a dedicated session.
3. **Auto-scheduled runs → same session**: the dispatcher's
   `_notify_chat` (success) and `_notify_chat_failure` (failure)
   already write to `task.session_id` if set. With auto-create, every
   new task has a session, so the run history naturally accumulates
   in the chat.

## Files touched

- `frontend/src/pages/Chat.jsx` — `isAutomationRequest` is now an
  alias for `isCreateAutomationRequest` (no longer a union). The
  prefill `useEffect` (line 197) reverts to the original 2-way logic
  (draft card + send vs. just prefill input). Kept
  `isCreateAutomationRequest` + `isRunAutomationRequest` helpers for
  future "intercept typed RUN requests in chat" work.
- `frontend/src/pages/AutomationTasks.jsx` — `runNow` calls the new
  `POST /api/automations/{id}/ensure-session` endpoint FIRST (non-fatal
  on failure), then fires `POST /api/automations/{id}/run`, then
  navigates to the returned `session_id`. No prefill, no LLM.
- `frontend/src/pages/AutomationTaskDetail.jsx` — same `runNow` change.
- `backend/app/routers/automation_api.py` (v3 addition) — new
  `POST /api/automations/{id}/ensure-session` endpoint + helper
  `_ensure_task_chat_session(db, task) -> (session_id, created)`.
  The helper adopts a dedicated session if the task's current one is
  missing, has a placeholder title (`"test"`, `"new task"`, …), or
  is shared with another task.
- `backend/app/services/agent_tools.py:_create_automation` (lines
  1271-1342) — auto-create-session logic for NEW tasks. Same shape
  as the ensure-session helper.
- `backend/scripts/backfill_automation_sessions.py` (v3) — bulk
  backfill script. Iterates all tasks, applies the same adopt logic
  as the ensure-session endpoint. Idempotent. `--dry-run` to preview.
- `backend/tests/test_automation_ensure_session.py` (v3) — 3 unit
  tests for the helper: creates-new-when-none, adopts-when-placeholder,
  noop-when-already-matches.

## Why bypass the LLM for Run Now

The chat is most often using a *custom* agent that the user has set
up for their own workflow. The `automation_agent` (system meta-agent
that the chat routes automation-flavored messages to) explicitly
forbids `execute_automation`. Custom agents may or may not have
`execute_automation` in their toolset. The reliable way to run an
automation is to fire the API directly and let the dispatcher's
`_notify_chat` stream the result into the task's session. The user
can still chat with the agent AFTER the run (e.g. "explain step 2")
— that path uses the LLM normally and works as long as the active
agent supports it.

## Why split the helpers (kept `isRunAutomationRequest`)

Even though we're not using the RUN prefill in the chat anymore, the
narrow helpers are useful for any future "intercept typed RUN requests
in chat" work (e.g. if we want to detect "Run my X now" typed manually
in the chat and fire the API directly, instead of routing through the
LLM). The split is a one-time refactor that costs nothing and pays off
later.

## Edge cases handled

- **Empty task name**: prefill is not used (direct API), so no issue.
- **Long task name**: prefill is not used, no truncation needed.
- **Task with no `session_id`** (old tasks created before this fix):
  frontend `runNow` calls `ensure-session` first, which adopts a
  dedicated session. Or run the bulk migration script.
- **Task with `session_id` set to a generic placeholder** (e.g. `"test"`):
  same — `ensure-session` adopts a new dedicated session; old
  session is left intact (other tasks may use it).
- **Task with `session_id` set to a non-matching, non-placeholder
  title** (shared with another task): same — adopts a new dedicated
  session.
- **Task already on a properly-named session**: no-op.
- **Special chars in task name**: not relevant for direct API.

## Verification

- `vitest run src/pages/Chat.test.jsx src/pages/Chat.sessionReuse.test.jsx
  src/pages/Chat.convRehydrate.test.jsx` — 16/16 pass.
- `pytest tests/test_automation_chat_tool.py tests/test_automation_runtime_agent.py
  tests/test_automation_failure_notify.py tests/test_automation_ensure_session.py`
  — 37/37 pass (3 new tests cover the helper's three branches).
- `vite build` → new bundle `index-Dauu05Jz.js` (2,181.68 kB /
  642.13 kB gzip, 8.99s). Served by nginx on `localhost:8000`.
- Bundle contains the new direct-API code (`/api/automations/${D.id}/run`,
  `Execution started` / `已开始执行`, `/api/automations/${D.id}/ensure-session`).
- `read_lints` on all modified files → clean.

## How to trigger the backfill on existing tasks

**Option A — Lazy (per task, no script needed):**
Click Run Now on each task in `/automation`. The frontend calls
`ensure-session` before firing; the task gets a dedicated chat
named after it; the user lands on the new chat. The new chat shows
past runs (linked from the old AgentConversation) + the new run's
result. **Manus UX achieved.**

**Option B — Bulk (one-off, requires backend machine):**
On the machine with Postgres access (NOT the dev box — Postgres
`172.22.0.4:5432` is unreachable from the dev box):
```
cd /root/zhanlu/backend
PYTHONPATH=. python scripts/backfill_automation_sessions.py --dry-run   # preview
PYTHONPATH=. python scripts/backfill_automation_sessions.py             # apply
```
Idempotent — re-running reports "0 tasks adopted".

## Critical detail: per-task chat REUSES the old AgentConversation

The v3 design created a fresh `AgentConversation` for every adopted
session, which left the new chat empty (no past runs). The user
then had to choose between the old (has history) and the new
(has the right name) chat — neither was the Manus UX.

The v4 fix is to REUSE the existing `AgentConversation` when
adopting a new `ChatSession`. The new ChatSession is created
with `conversation_id = old_conv_id` instead of a fresh
`conv.id`. This way:
- Past runs (already in the old conversation) are visible in the new chat.
- Future runs go to the same conversation (via the new ChatSession).
- The OLD ChatSession ("test") is left in place as an alias — the user
  can delete it manually if they want a cleaner sidebar.

Falls back to a fresh `AgentConversation` only when the task has no
prior conversation at all (brand-new task or orphaned session).
Covered by `test_ensure_session_reuses_old_conversation_for_history`
in `backend/tests/test_automation_ensure_session.py`.

## Why bypass the LLM for Run Now

The chat is most often using a *custom* agent that the user has set
up for their own workflow. The `automation_agent` (system meta-agent
that the chat routes automation-flavored messages to) explicitly
forbids `execute_automation`. Custom agents may or may not have
`execute_automation` in their toolset. The reliable way to run an
automation is to fire the API directly and let the dispatcher's
`_notify_chat` stream the result into the task's session. The user
can still chat with the agent AFTER the run (e.g. "explain step 2")
— that path uses the LLM normally and works as long as the active
agent supports it.

## Why split the helpers (kept `isRunAutomationRequest`)

Even though we're not using the RUN prefill in the chat anymore, the
narrow helpers are useful for any future "intercept typed RUN requests
in chat" work (e.g. if we want to detect "Run my X now" typed manually
in the chat and fire the API directly, instead of routing through the
LLM). The split is a one-time refactor that costs nothing and pays off
later.

## Edge cases handled

- **Empty task name**: prefill is not used (direct API), so no issue.
- **Long task name**: prefill is not used, no truncation needed.
- **Task with no `session_id`** (old tasks created before this fix):
  dispatcher's fallback (user's most recent session) handles it. A
  backfill migration is a follow-up.
- **Special chars in task name**: not relevant for direct API.

## Verification

- `vitest run src/pages/Chat.test.jsx src/pages/Chat.sessionReuse.test.jsx
  src/pages/Chat.convRehydrate.test.jsx` — 16/16 pass.
- `pytest tests/test_automation_chat_tool.py tests/test_automation_runtime_agent.py
  tests/test_automation_failure_notify.py` — 34/34 pass.
- `vite build` → new bundle `index-DGd5eY0q.js` (2,181.37 kB /
  642.04 kB gzip, 8.97s). Served by nginx on `localhost:8000`.
- Bundle contains the new direct-API code (`/api/automations/${D.id}/run`,
  `Execution started` / `已开始执行`). The old prefill strings
  (`Run my `, `Running in chat`) are GONE.
- `read_lints` on all 4 modified files → clean.
