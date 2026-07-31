# Automation Chat Drawer + Manus-Style Run Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the automation list page's "Run now" open the chatbot (auto-execute the task via a new tool) and turn the task drawer into a Manus-style right panel showing past runs.

**Architecture:** Two-layer change. Backend gets a new tool that chat agents can call to run a user's own automation task. Frontend swaps the modal drawer for a right slide-in panel, wires "Run now" to open the chat drawer with a prefilled message, and fixes the broken past-runs list.

**Tech Stack:**
- Backend: Python 3.11, FastAPI, SQLAlchemy, existing `dispatcher.schedule_run` + `execute_automation` pipeline.
- Frontend: React 19 + Vite, existing chat input/drawer pattern, `ExecutionLogsList` component.

**Spec:** `docs/superpowers/specs/2026-07-28-automation-chat-drawer-design.md`

---

## Global Constraints

- **Frontend rebuild required.** The deployment is nginx + `python http.server` on port 5157 serving `/root/zhanlu/frontend/dist/`. Vite dev HMR does NOT apply. After any `.jsx`/`.js` change, run `cd /root/zhanlu/frontend && ./node_modules/.bin/vite build` before declaring "live."
- **Backend is a Docker bind-mount.** `/root/zhanlu/backend -> /app` and uvicorn runs with `--reload`. Source edits auto-pick up; no rebuild needed. Restart only if migrating the container.
- **DB has no Alembic for this project.** Schema changes use the runtime schema-ensure pattern in `ensure_system_agents` (auto `ALTER TABLE ADD COLUMN IF NOT EXISTS`). No new columns are added in this plan.
- **TDD discipline.** Each backend task writes a failing test FIRST, then makes it pass. Frontend tasks are verified with manual smoke tests described in the task.
- **No DB rows are deleted.** Tasks/agents live behind soft-delete (`is_deleted=true`). Tests must clean up.
- **Out of scope.** The downstream DeepSeek 400 in the v3 streaming path is a separate investigation and is NOT to be fixed in this plan.
- **Test DB setup.** Postgres at 172.22.0.4 is unreachable from the dev box. Run backend tests against a fresh SQLite file:
  ```bash
  cd /root/zhanlu/backend && rm -f test_runtime.db && \
    DATABASE_URL="sqlite:///./test_runtime.db" python3 -c \
    "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
    DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/<file> -v
  ```
  `test_runtime.db` is gitignored.

---

## File Structure

| File | Role |
|---|---|
| `backend/app/services/automation_chat_tool.py` (new) | Tool handler `execute_automation` for the chat agent. Validates ownership, creates execution, polls up to 5s, returns output. |
| `backend/app/services/system_agents.py` (modify) | Add `"execute_automation"` to the enabled-tools whitelist of system chat agents. |
| `backend/tests/test_automation_chat_tool.py` (new) | 4 unit tests: happy path, deny-by-ownership, resolve-by-name, running-on-timeout. |
| `frontend/src/pages/AutomationTasks.jsx` (modify) | Remove the backdrop, become a right panel. Replace `runNow`'s API call with a state setter that opens the chat drawer with a prefilled message. Render past runs as dated cards. |
| `frontend/src/pages/Chat.jsx` (modify) | Accept an `initialMessage` prop; when present, prefill the input and auto-submit on open. |
| `frontend/src/components/automation/ExecutionLogsList.jsx` (modify) | Fix the data fetch path; render each execution as a card (date, status, summary). |

---

### Task 1: Backend tool — `execute_automation` (TDD)

**Files:**
- Create: `backend/app/services/automation_chat_tool.py`
- Test: `backend/tests/test_automation_chat_tool.py`

**Interfaces:**
- Consumes: `models.automation_task.AutomationTask`, `models.automation_execution.AutomationExecution`, dispatcher path (existing).
- Produces: `execute_automation_tool(args, db, user) -> dict` — returns `{ execution_id, status, output_text?, error? }`.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the execute_automation chat-agent tool."""
from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.user import User
from app.services.automation_chat_tool import execute_automation_tool


def _make_task(db, owner_id, name="Weekly report", task_id=None):
    t = AutomationTask(
        id=task_id or "tsk-test-001",
        name=name,
        type="custom",
        prompt="summarise",
        schedule="weekly",
        status="active",
        created_by_id=owner_id,
        org_id="default-org",
        app_id="default-app",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_execute_automation_runs_task_and_returns_output():
    """Happy path: an authorized owner can run their own task."""
    db = SessionLocal()
    try:
        owner = User(id="u-owner", email="o@x.com", password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner")
        result = execute_automation_tool({"task_id": task.id}, db, owner)
        assert "execution_id" in result
        assert result["status"] in ("completed", "running")  # depends on dispatcher
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_denies_other_users_task():
    """A different user (not the owner) must be rejected."""
    db = SessionLocal()
    try:
        owner = User(id="u-owner-2", email="o2@x.com", password_hash="x", role="user")
        attacker = User(id="u-attacker", email="a@x.com", password_hash="x", role="user")
        db.add_all([owner, attacker])
        db.commit()
        task = _make_task(db, "u-owner-2")
        result = execute_automation_tool({"task_id": task.id}, db, attacker)
        assert result.get("error") and "not yours" in result["error"].lower()
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_resolves_by_name_when_no_id():
    """When task_id is missing, fall back to a case-insensitive name match."""
    db = SessionLocal()
    try:
        owner = User(id="u-owner-3", email="o3@x.com", password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner-3", name="Daily Sales Data Sync")
        result = execute_automation_tool({"name": "daily sales"}, db, owner)
        assert "execution_id" in result
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_returns_running_after_timeout(monkeypatch):
    """If the run hasn't finished in 5 s, return running (the chat will be
    notified via the existing _notify_chat_failure / success path)."""
    db = SessionLocal()
    try:
        owner = User(id="u-owner-4", email="o4@x.com", password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner-4")

        def fake_poll(execution_id, timeout=5.0):
            return {"status": "running"}

        monkeypatch.setattr(
            "app.services.automation_chat_tool._poll_execution_status",
            fake_poll,
        )
        result = execute_automation_tool({"task_id": task.id}, db, owner)
        assert result["status"] == "running"
        db.delete(task)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run the test, confirm it fails on the missing import**

Run:
```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -c \
  "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_automation_chat_tool.py -v
```

Expected: `ImportError: cannot import name 'execute_automation_tool' from 'app.services.automation_chat_tool'`.

- [ ] **Step 3: Implement the minimal tool**

Create `backend/app/services/automation_chat_tool.py`:

```python
"""Chat-agent tool: run an AutomationTask now and return its output.

Used by the chat agent when the user types something like
"Run my Daily Sales Data Sync now". Validates ownership, enqueues the
run via the existing dispatcher, polls briefly, and returns the output.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.models.automation_task import AutomationTask
from app.models.user import User

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5
_MAX_POLL_S = 5.0


def _user_can_run(db: Session, user: User, task: AutomationTask) -> bool:
    """Owner check. Team-share is not implemented yet."""
    return task.created_by_id == user.id


def _resolve_task(db: Session, args: dict, user: User) -> Optional[AutomationTask]:
    if args.get("task_id"):
        return db.query(AutomationTask).filter(
            AutomationTask.id == args["task_id"],
            AutomationTask.is_deleted == False,  # noqa: E712
        ).first()
    if args.get("name"):
        # Case-insensitive match on the user's own tasks.
        like = args["name"].strip().lower()
        candidates = db.query(AutomationTask).filter(
            AutomationTask.created_by_id == user.id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).all()
        for t in candidates:
            if like in (t.name or "").lower():
                return t
    return None


def _poll_execution_status(db: Session, execution_id: str, timeout: float = _MAX_POLL_S):
    """Poll the execution row until status is final or timeout."""
    from app.models.automation_execution import AutomationExecution
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ex = db.query(AutomationExecution).filter(
            AutomationExecution.id == execution_id,
        ).first()
        if ex and ex.status in ("completed", "failed"):
            return {
                "status": ex.status,
                "output_text": (ex.output_text or "")[:2000],
                "error": ex.error,
            }
        time.sleep(_POLL_INTERVAL_S)
    return {"status": "running"}


def execute_automation_tool(args: dict, db: Session, user: User) -> dict:
    """Tool entrypoint: validate, enqueue, poll, return."""
    task = _resolve_task(db, args, user)
    if task is None:
        return {"error": f"Task {args.get('task_id') or args.get('name') or ''} not found"}
    if not _user_can_run(db, user, task):
        return {"error": "That task isn't yours."}

    try:
        from app.services.automation_dispatcher import dispatcher
        execution_id = dispatcher.schedule_run(db, task.id)
    except Exception as e:  # pragma: no cover - dispatcher is exercised in prod
        logger.exception("execute_automation_tool: dispatcher failed: %s", e)
        return {"error": f"Failed to start run: {e}"}

    result = _poll_execution_status(db, execution_id)
    return {"execution_id": execution_id, **result}
```

- [ ] **Step 4: Confirm tests pass**

Run the same `pytest` command from Step 2. Expected: 4 passed (the three error-path tests pass immediately; the happy-path test depends on `dispatcher.schedule_run` existing — if it doesn't, the test will surface a real implementation gap to address before this task is fully complete).

If `dispatcher.schedule_run` does not exist with that exact signature, run a quick check:

```bash
grep -nE "def schedule_run|def enqueue|class Dispatcher" /root/zhanlu/backend/app/services/automation_dispatcher.py
```

…and adjust the call in `execute_automation_tool.py` to whichever enqueue method actually exists, then re-run the tests.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_chat_tool.py backend/tests/test_automation_chat_tool.py && \
  git commit -m "feat(backend): execute_automation chat-agent tool (TDD)"
```

---

### Task 2: Register the tool in system-agent whitelist

**Files:**
- Modify: `backend/app/services/system_agents.py` — add `"execute_automation"` to the enabled tools list that gets written to system agents' `tool_config.enabled_tools`.

**Interfaces:**
- Consumes: `RUNTIME_ENABLED_TOOLS` (existing) for the automation runtime agent. This task adds an analogous `CHAT_SYSTEM_AGENT_ENABLED_TOOLS` block, or extends the existing helper if there is one for the chat system agent (`general_assistant`).
- Produces: all system chat agents (currently `general_assistant`) end up with `"execute_automation"` in `tool_config.enabled_tools` on next restart.

- [ ] **Step 1: Locate the chat-agent tool whitelist**

```bash
grep -nE "general_assistant|enabled_tools|CHAT_AGENT|tool_config" /root/zhanlu/backend/app/services/system_agents.py | head -20
```

- [ ] **Step 2: Add the tool to the whitelist**

Find the dict that becomes `general_assistant`'s `tool_config` (it has a `tool_config` key inside the configs list / dict the function iterates). Add `"execute_automation"` to that list.

If the whitelist is built from a shared constant like `RUNTIME_ENABLED_TOOLS`, locate it (it should be in `automation_runtime.py` or `system_agents.py`) and add `"execute_automation"` to it ONLY for the chat-agent config — keep the runtime-agent's whitelist unchanged (runtime agent must NOT be able to spawn more runs; chat agent can).

Do NOT touch the runtime agent's tool config from Task 2 of the previous plan.

- [ ] **Step 3: Confirm the existing tests still pass**

```bash
cd /root/zhanlu/backend && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_automation_chat_tool.py tests/test_automation_runtime_agent.py \
  tests/test_automation_runtime_integration.py tests/test_automation_failure_notify.py \
  tests/test_automation_failure_recovery.py -v
```

Expected: 5 files, all green (no regressions).

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/system_agents.py && \
  git commit -m "feat(backend): register execute_automation in chat-agent whitelist"
```

---

### Task 3: Chat component accepts `initialMessage` and auto-submits

**Files:**
- Modify: `frontend/src/pages/Chat.jsx` (or whichever file renders the chat input the drawer uses; verify with `grep -n "drawer" /root/zhanlu/frontend/src` if unsure)
- Modify: the chat drawer wrapper (likely `frontend/src/components/ChatDrawer.jsx` — verify) to accept and forward the prop.

**Interfaces:**
- Consumes: a new prop `initialMessage` (string | null).
- Produces: when `initialMessage` is non-null and the drawer transitions from closed → open, the input is populated with that string and `handleSend` is called automatically.

- [ ] **Step 1: Locate the chat input and submit handler**

```bash
grep -nE "onSubmit|handleSend|setInput|input.*setInput|chat-input|textarea" \
  /root/zhanlu/frontend/src/pages/Chat.jsx | head -10
```

Also locate the chat drawer (if separate):

```bash
grep -nE "ChatDrawer|ChatPanel" /root/zhanlu/frontend/src -r | head -10
```

- [ ] **Step 2: Add the `initialMessage` prop**

```jsx
export default function Chat({ initialMessage = null }) {
  const [input, setInput] = useState("");
  const inputRef = useRef(null);
  const prevOpenRef = useRef(false);
  const [isOpen, setIsOpen] = useState(false);

  // ... existing code ...

  // When the drawer opens with an initialMessage, prefill + auto-submit.
  useEffect(() => {
    if (initialMessage && isOpen && !prevOpenRef.current) {
      setInput(initialMessage);
      // Defer to next tick so the input is mounted before submit reads from it.
      setTimeout(() => {
        handleSend(initialMessage);
        setInput("");
      }, 50);
    }
    prevOpenRef.current = isOpen;
  }, [isOpen, initialMessage]);
```

(Adjust to the actual variable names in the file. The key invariants are:
   - a flag tracked in a `useRef` to detect the closed→open transition
   - the submit handler is called with the prefilled text
   - input is cleared after submit so the user can type a follow-up)

- [ ] **Step 3: Manual smoke**

Open the running dev page (`http://zhanlu.ai:8000/chat`) in a browser. There is no UI yet for triggering this prop, but the prop accepts a default of `null` and behaves identically to today. Confirm the page still loads and the chat still sends/receives messages.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/Chat.jsx frontend/src/components/ChatDrawer.jsx 2>/dev/null && \
  git commit -m "feat(frontend): Chat accepts initialMessage and auto-submits"
```

(Commit only the files you actually changed; ignore the rest of the chain.)

---

### Task 4: Fix "Failed to load runs" in `ExecutionLogsList`

**Files:**
- Modify: `frontend/src/components/automation/ExecutionLogsList.jsx`

**Interfaces:**
- Consumes: `taskId` prop (the AutomationTask id). The component renders past runs.
- Produces: a fetch to the working endpoint (Task's manual reconnaissance will confirm the path during implementation), rendering each as a card (date, status, summary).

- [ ] **Step 1: Locate the existing fetch path**

```bash
grep -nE "fetch|axios|apiFetch|executions|run.?history" \
  /root/zhanlu/frontend/src/components/automation/ExecutionLogsList.jsx
```

- [ ] **Step 2: Confirm the backend endpoint that returns executions for a task**

```bash
grep -nE "executions|automation_executions" \
  /root/zhanlu/backend/app/routers/automations.py | head -20
```

Likely candidate: `GET /api/automations/{task_id}/executions?limit=20`. Verify with a `curl`:

```bash
TASK_ID=$(docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -t -c \
  "SELECT id FROM automation_tasks WHERE name='Daily Sales Data Sync' AND is_deleted=false LIMIT 1;" \
  | tr -d ' \n')
curl -s "http://localhost:5002/api/automations/${TASK_ID}/executions?limit=5" | head -c 800
```

- [ ] **Step 3: Update the component to use the working endpoint + render cards**

```jsx
// Before (sketch — actual code varies):
useEffect(() => {
  axios.get(`/some/wrong/path/${taskId}`).then(...)
}, [taskId]);

// After:
useEffect(() => {
  let cancelled = false;
  fetch(`/api/automations/${taskId}/executions?limit=20`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  })
    .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
    .then(data => { if (!cancelled) setExecutions(data.items || data || []); })
    .catch(err => { if (!cancelled) setError(err.message); });
  return () => { cancelled = true; };
}, [taskId, refreshKey]);

// Render:
{executions.map(e => (
  <div key={e.id} className="rounded-lg border border-border p-3 mb-2">
    <div className="flex items-center justify-between text-xs text-muted-foreground">
      <span>{formatDate(e.created_date)}</span>
      <span className={statusColor(e.status)}>{e.status}</span>
    </div>
    <p className="mt-1 text-sm line-clamp-2">
      {e.output_text || e.error || t.automation.running}
    </p>
  </div>
))}
```

(Adapt to the component's existing style — use the same Tailwind conventions and the same `t` i18n helper it already uses.)

- [ ] **Step 4: Manual smoke**

Reload `/automation` in the browser, open the right panel for the "Daily Sales Data Sync" task. The previous "Failed to load runs" should be replaced by the dated cards from the DB. Confirm at least one execution (there are 5+ in the DB from earlier runs) shows up.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/automation/ExecutionLogsList.jsx && \
  git commit -m "fix(frontend): ExecutionLogsList fetches the right endpoint, renders cards"
```

---

### Task 5: Drop the drawer backdrop — Manus-style right panel

**Files:**
- Modify: `frontend/src/pages/AutomationTasks.jsx`

**Interfaces:**
- Consumes: existing `selectedTask` state (the task whose panel is open).
- Produces: a `<aside>` on the right edge of the screen with no dim overlay; the page underneath is visible and clickable.

- [ ] **Step 1: Locate the backdrop + aside**

```bash
grep -nE "fixed inset-0|bg-black|backdrop|drawer|aside|<aside" \
  /root/zhanlu/frontend/src/pages/AutomationTasks.jsx
```

- [ ] **Step 2: Replace the backdrop with a plain slide-in**

```jsx
// Before:
{selectedTask && (
  <>
    <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setSelectedTask(null)} />
    <aside className="fixed right-0 top-0 z-40 h-full w-[420px] bg-background shadow-xl">
      {/* panel content */}
    </aside>
  </>
)}

// After:
{selectedTask && (
  <aside className="fixed right-0 top-0 z-30 h-full w-[440px] bg-background border-l border-border shadow-2xl overflow-y-auto">
    {/* panel content */}
  </aside>
)}
```

Keep the close affordance inside the panel (an X button in the top-right corner of the `<aside>`), not via backdrop click.

- [ ] **Step 3: Manual smoke**

Open `/automation`, click a task. The modal-with-backdrop look should be gone. The page behind should stay visible and interactive, with the right panel sliding in.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/AutomationTasks.jsx && \
  git commit -m "refactor(frontend): automation drawer becomes Manus-style right panel"
```

---

### Task 6: "Run now" opens the chat drawer with a prefilled message

**Files:**
- Modify: `frontend/src/pages/AutomationTasks.jsx`

**Interfaces:**
- Consumes: `selectedTask`, the chat-drawer state (see below).
- Produces: clicking "Run now" no longer calls the API; instead it sets chat-drawer state to `{ open: true, initialMessage: "Run my <Task Name> now" }`.

- [ ] **Step 1: Locate `runNow` in the panel**

```bash
grep -nE "Run now|runNow|/api/automations/.*run" \
  /root/zhanlu/frontend/src/pages/AutomationTasks.jsx
```

- [ ] **Step 2: Decide how `AutomationTasks` reaches the chat**

Two viable options:

(A) **Lift state into a shared `ChatDrawerContext`** (recommended): create
    `frontend/src/components/ChatDrawerContext.jsx` with `useChatDrawer()` returning `{ open, initialMessage, openChat(opts) }`. Wrap `<App>` (or whatever top-level wrapper renders the chat drawer at `/automation`) in the provider. `AutomationTasks` calls `openChat({ initialMessage: "Run my X now" })`.

(B) **URL trigger**: `AutomationTasks` does `navigate('/automation?runNow=<task_id>')`; the chat drawer reads the param and self-opens. Lighter weight, no new context.

Pick the option that matches the existing drawer plumbing in this repo. If unsure, use the simpler URL trigger (B) first; if the chat drawer ignores search params, fall back to (A).

- [ ] **Step 3: Replace `runNow`'s body**

```jsx
async function runNow(task) {
  if (!task) return;
  openChat({
    initialMessage: `Run my ${task.name} now`,
    conversationContext: { task_id: task.id },
  });
}
```

`openChat` is from option (A) above; the equivalent for option (B) is `navigate(`/automation?runNow=${task.id}`)` and the chat drawer effect handles the rest.

- [ ] **Step 4: Manual smoke**

Open `/automation`, click "Run now". The chat drawer should open with `"Run my Daily Sales Data Sync now"` already typed and submitted. The chat agent should pick it up — the actual execution might still 400 (separate DeepSeek streaming issue), but the chat-side wiring should be visibly working (request fires, user message appears in the chat).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/AutomationTasks.jsx && \
  git commit -m "feat(frontend): Run now opens chat drawer with prefilled task prompt"
```

---

### Task 7: Rebuild frontend, run all tests, manual end-to-end smoke

**Files:** none (verification task).

- [ ] **Step 1: Rebuild the frontend**

```bash
cd /root/zhanlu/frontend && ./node_modules/.bin/vite build 2>&1 | tail -10
```

- [ ] **Step 2: Confirm the new bundle contains the changes**

```bash
grep -o 'Run my' /root/zhanlu/frontend/dist/assets/*.js | head -1
grep -o 'ExecutionLogsList\|execution' /root/zhanlu/frontend/dist/assets/*.js | head -3
```

Expected: at least one hit for `Run my` (from the prefilled message) and a hits for execution-related code.

- [ ] **Step 3: Confirm bundle is served**

```bash
curl -s http://localhost:5152/ | grep -o 'index-[A-Za-z0-9_]*\.js' | head -1
```

Expected: matches `index-*.js` (the same hash as the freshly built bundle in Step 1).

- [ ] **Step 4: Run all backend tests**

```bash
cd /root/zhanlu/backend && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_automation_chat_tool.py tests/test_automation_runtime_agent.py \
  tests/test_automation_runtime_integration.py tests/test_automation_failure_notify.py \
  tests/test_automation_failure_recovery.py -v
```

Expected: all green.

- [ ] **Step 5: Manual end-to-end smoke**

1. Hard-refresh `/automation` in the browser (Ctrl/Cmd+Shift+R).
2. Click a scheduled task — confirm the new right panel (no backdrop) appears.
3. Confirm past runs render as cards (the previously-broken "Failed to load runs" is fixed).
4. Click "Run now" — confirm the chat drawer opens with the prefilled prompt and auto-submits.
5. Check the chat for the agent's response (it may still 400 from the unrelated DeepSeek streaming issue — that's a separate bug).

- [ ] **Step 6: Final commit (only if any incidental fixes came up)**

```bash
cd /root/zhanlu && git status --short && \
  git add -A && git commit -m "chore: post-smoke cleanups for automation chat drawer" || echo "nothing to commit"
```

(If nothing changed in this final task, skip the commit.)

---

## Execution Notes

- Task 1 may surface a real gap if `dispatcher.schedule_run` doesn't exist with that exact name. The mitigation is documented in Step 4 of Task 1 — adapt the call to whatever enqueue method the dispatcher actually exposes, then re-run.
- Tasks 3-6 are sequential (state shared). Tasks 1-2 (backend) and Task 3 (frontend, isolated) can be done in parallel, then 4-5-6 in sequence.
- Final `vite build` is required (no HMR in this environment). Skipping it leaves the user seeing the old bundle.
