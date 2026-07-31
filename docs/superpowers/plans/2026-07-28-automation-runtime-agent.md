# Automation Runtime Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee every `AutomationTask` has a valid hidden executor agent (one per `org_id`/`app_id`), never surface internal agent errors to users, retry transient failures, and build per-project memory across runs.

**Architecture:** Add a hidden `automation_runtime_agent` `AgentApp` (one per `(org_id, app_id)`, `is_system=True`, new `role="automation_runtime"` column). Auto-bind it at task creation, resolve it in the executor (replacing the random-workspace-agent fallback), classify transient errors so only they consume the existing dispatcher retry budget, and maintain a per-project `AgentConversation` memory ledger.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 (mapped columns), pytest, React/JSX frontend. SQLite (dev) + Postgres (prod). Runtime schema-ensure pattern (auto `ALTER TABLE ADD COLUMN` at startup) — matches the existing `_ensure_schema` in `automation_dispatcher.py`.

## Global Constraints

- All models inherit `org_id` + `app_id` (String(36), defaults `"default-org"`/`"default-app"`) from `TimestampedBase` in `backend/app/models/base.py`.
- `AgentConversation` is keyed by `agent_name` (not `agent_id`) + `project_id` + `org_id` + `app_id`.
- The dispatcher already has retry machinery: `execute_automation` → `_mark_failed` → `schedule_retry` (new `AutomationExecution` row, exponential backoff `min(60*2^attempt, 1800)`), driven by `task.max_retries` (stored as STRING, default `"2"`). Final failure already calls `_notify_chat_failure`. We INTEGRATE with this, not replace it.
- Runtime schema-ensure is the codebase convention for adding columns (see `automation_dispatcher._ensure_schema`). Alembic exists but the automation subsystem uses runtime ensure.
- Tests live in `backend/tests/` with `conftest.py` (pytest-asyncio auto mode). Run single test files by explicit path to avoid OOM.
- The runtime agent has `is_system=True`; the frontend `filterUserAgents()` already hides `is_system` agents, so no new frontend filter is strictly required — we verify and add a `role` check as defense-in-depth.

---

## File Structure

**Create:**
- `backend/app/services/automation_runtime.py` — `ensure_automation_runtime_agent()`, `get_or_create_project_conversation()`, `backfill_automation_runtime_agents()` (single focused module).
- `backend/tests/test_automation_runtime_agent.py` — unit tests for ensure/resolve/memory/backfill.
- `backend/tests/test_automation_runtime_integration.py` — end-to-end create→resolve→classify tests.

**Modify:**
- `backend/app/models/agent_app.py` — add `role` column.
- `backend/app/services/system_agents.py` — call runtime-agent provisioning from `ensure_system_agents`.
- `backend/app/services/automation_executor.py` — `_resolve_agent` fallback to runtime agent; transient-error classification in `_mark_failed`; per-project memory injection.
- `backend/app/services/agent_tools.py` — `_create_automation` auto-bind runtime agent.
- `backend/app/routers/agents.py` — filter `role="automation_runtime"` from user listings; add admin diagnostic endpoint.
- `frontend/src/pages/AutomationTaskDetail.jsx` — show "Backend engine: Connected" badge instead of agent id.

---

### Task 1: Add `role` column to AgentApp + runtime schema-ensure

**Files:**
- Modify: `backend/app/models/agent_app.py` (append after `is_system` column, ~line 88)
- Test: `backend/tests/test_automation_runtime_agent.py`

**Interfaces:**
- Produces: `AgentApp.role` — `Mapped[Optional[str]]`, `String(64)`, nullable, indexed. `NULL` for user agents; `"automation_runtime"` for the hidden executor.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_automation_runtime_agent.py`:

```python
"""Unit tests for the automation runtime agent subsystem."""
import pytest
from sqlalchemy import inspect

from app.database import SessionLocal, engine
from app.models.agent_app import AgentApp


def test_agent_app_has_role_column():
    """The AgentApp table must have a nullable 'role' column."""
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("agent_apps")}
    assert "role" in cols, "agent_apps table is missing the 'role' column"
    assert cols["role"]["nullable"] is True


def test_role_column_has_index():
    """The 'role' column should be indexed for fast filtering."""
    inspector = inspect(engine)
    indexes = inspector.get_indexes("agent_apps")
    index_cols = {tuple(ix["column_names"]) for ix in indexes}
    assert ("role",) in index_cols, "agent_apps.role should have an index"


def test_new_agent_role_defaults_to_none():
    """A freshly created user agent has role = None."""
    db = SessionLocal()
    try:
        a = AgentApp(name="test_role_agent", is_system=False, status="draft")
        db.add(a)
        db.commit()
        db.refresh(a)
        assert a.role is None
        db.delete(a)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_agent_app_has_role_column -v`
Expected: FAIL with "agent_apps table is missing the 'role' column" (column not yet defined).

- [ ] **Step 3: Add the `role` column to the model**

In `backend/app/models/agent_app.py`, after the `is_system` mapped_column block (after line 88), add:

```python
    # Functional role of the agent. NULL = a normal user agent.
    # "automation_runtime" = the hidden executor that runs scheduled
    # automations — never shown in user-facing agent lists, never used
    # for interactive chat. Scoped to (org_id, app_id) so every tenant
    # gets exactly one.
    role: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        doc="Functional role. NULL=user agent; "
            "'automation_runtime'=hidden scheduled-automation executor.",
    )
```

- [ ] **Step 4: Add runtime schema-ensure for the column**

The codebase auto-adds missing columns at startup (see `automation_dispatcher._ensure_schema`). Add a similar ensure in `ensure_system_agents` (it runs on every startup). In `backend/app/services/system_agents.py`, inside `ensure_system_agents`, after the `try:` block opens (before the `for cfg in configs:` loop), add:

```python
        # Runtime schema-ensure: add the 'role' column to agent_apps if it
        # is missing, mirroring the _ensure_schema pattern in
        # automation_dispatcher. Works on SQLite + Postgres.
        try:
            from sqlalchemy import inspect as sa_inspect, text
            inspector = sa_inspect(db.bind)
            existing_cols = {c["name"] for c in inspector.get_columns("agent_apps")}
            if "role" not in existing_cols:
                db.execute(text("ALTER TABLE agent_apps ADD COLUMN role VARCHAR(64)"))
                db.commit()
                logger.info("Added 'role' column to agent_apps")
            # Index (best-effort; SQLite ignores IF NOT EXISTS on older versions)
            existing_ix = {ix["name"] for ix in inspector.get_indexes("agent_apps")}
            if "ix_agent_apps_role" not in existing_ix:
                try:
                    db.execute(text("CREATE INDEX ix_agent_apps_role ON agent_apps (role)"))
                    db.commit()
                except Exception:
                    db.rollback()  # index may already exist; non-fatal
        except Exception as _schema_err:
            db.rollback()
            logger.warning("ensure_system_agents: role-column ensure failed (non-fatal): %s", _schema_err)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/models/agent_app.py backend/app/services/system_agents.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: add role column to AgentApp for automation_runtime agents"
```

---

### Task 2: `ensure_automation_runtime_agent` — idempotent get-or-create

**Files:**
- Create: `backend/app/services/automation_runtime.py`
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: `ensure_automation_runtime_agent(db: Session, org_id: str, app_id: str) -> AgentApp` — idempotent; returns the hidden runtime agent for the given `(org_id, app_id)`, creating it if missing. The agent has `name="automation_runtime_agent"`, `is_system=True`, `role="automation_runtime"`, `status="active"`, `org_id`/`app_id` set, `tool_config` with the read/write/notify/web whitelist.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.services.automation_runtime import ensure_automation_runtime_agent


def test_ensure_runtime_agent_is_idempotent():
    """Calling twice with the same (org, app) returns the same record."""
    db = SessionLocal()
    try:
        a1 = ensure_automation_runtime_agent(db, "test-org", "test-app")
        a2 = ensure_automation_runtime_agent(db, "test-org", "test-app")
        assert a1.id == a2.id
        assert a1.name == "automation_runtime_agent"
        assert a1.is_system is True
        assert a1.role == "automation_runtime"
        assert a1.status == "active"
        assert a1.org_id == "test-org"
        assert a1.app_id == "test-app"
        # cleanup
        db.delete(a1)
        db.commit()
    finally:
        db.close()


def test_ensure_runtime_agent_separate_orgs_get_separate_agents():
    """Different (org, app) pairs get different runtime agents."""
    db = SessionLocal()
    try:
        a1 = ensure_automation_runtime_agent(db, "org-a", "app-a")
        a2 = ensure_automation_runtime_agent(db, "org-b", "app-b")
        assert a1.id != a2.id
        db.delete(a1)
        db.delete(a2)
        db.commit()
    finally:
        db.close()


def test_runtime_agent_tool_whitelist_excludes_automation_crud():
    """The runtime agent must NOT be able to create/update/delete automations."""
    db = SessionLocal()
    try:
        a = ensure_automation_runtime_agent(db, "test-org-wl", "test-app-wl")
        enabled = set((a.tool_config or {}).get("enabled_tools", []))
        forbidden = {"create_automation", "update_automation", "delete_automation",
                     "create_agent", "delete_agent"}
        assert not (enabled & forbidden), f"runtime agent has forbidden tools: {enabled & forbidden}"
        # Must have the 4 allowed categories
        assert "web_search" in enabled
        assert "create_artifact" in enabled
        assert "send_message" in enabled
        db.delete(a)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k runtime_agent`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.automation_runtime'`.

- [ ] **Step 3: Create the `automation_runtime.py` service module**

Create `backend/app/services/automation_runtime.py`:

```python
"""Automation runtime agent — the hidden executor for scheduled automations.

One hidden ``automation_runtime_agent`` AgentApp exists per ``(org_id, app_id)``.
It is never shown in user-facing agent lists and never used for interactive
chat. ``ensure_automation_runtime_agent`` is idempotent and self-healing:
called at task-creation time, at executor-resolve time, and during startup
provisioning.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_app import AgentApp
from app.models.agent_conversation import AgentConversation

logger = logging.getLogger(__name__)

RUNTIME_AGENT_NAME = "automation_runtime_agent"
RUNTIME_ROLE = "automation_runtime"

# Tool whitelist for the runtime agent: read data, write files/reports,
# notify chat, and enrich with web search. Forbidden: automation CRUD,
# agent CRUD, admin tools.
RUNTIME_ENABLED_TOOLS = [
    # Read project data
    "list_knowledge_bases", "list_data_sources", "describe_schema",
    "execute_query", "answer_from_database", "ask_data_agent",
    # Write / generate outputs
    "create_artifact", "write_file", "read_file",
    # Notify
    "send_message",
    # Web enrichment
    "web_search", "web_extract",
    # Memory (per-project ledger writes)
    "memory",
]

_FORBIDDEN_TOOLS = frozenset({
    "create_automation", "update_automation", "delete_automation",
    "create_agent", "update_agent", "delete_agent",
    "docker_compose_restart", "update_env_config", "cronjob",
})


def _runtime_tool_config() -> dict:
    """Build the tool_config for the runtime agent."""
    return {
        "enabled_tools": [t for t in RUNTIME_ENABLED_TOOLS if t not in _FORBIDDEN_TOOLS],
        "disabled_tools": sorted(_FORBIDDEN_TOOLS),
    }


def _runtime_manifest() -> dict:
    return {
        "agent_name": RUNTIME_AGENT_NAME,
        "version": "1.0.0",
        "mission": (
            "Execute scheduled automation tasks: read project data, "
            "generate reports/artifacts, and notify the user. Honor the "
            "task's project_id for data isolation."
        ),
        "task_scope": ["scheduled_automation_execution", "report_generation", "data_read"],
        "boundaries": {
            "allowed": list(RUNTIME_ENABLED_TOOLS),
            "forbidden": sorted(_FORBIDDEN_TOOLS),
        },
        "risk_tier": "low",
        "created_by": "system",
    }


_RUNTIME_SYSTEM_PROMPT = (
    "You are the automation runtime engine. You execute scheduled tasks on "
    "behalf of the user. For each run: read only data bound to the task's "
    "project_id, produce the requested output (report/file), and send the "
    "result back to the user's chat. Never create, update, or delete "
    "automations or agents. Treat each run as stateless from your own "
    "perspective, but read the per-project memory for continuity."
)


def ensure_automation_runtime_agent(
    db: Session, org_id: str, app_id: str
) -> AgentApp:
    """Idempotently get-or-create the hidden automation runtime agent.

    Returns the existing ``AgentApp`` with ``name=RUNTIME_AGENT_NAME``,
    ``org_id``, ``app_id``, ``role=RUNTIME_ROLE`` if one exists; otherwise
    creates and returns it. Safe to call on every task creation and every
    executor resolve.
    """
    existing = db.query(AgentApp).filter(
        AgentApp.name == RUNTIME_AGENT_NAME,
        AgentApp.org_id == org_id,
        AgentApp.app_id == app_id,
        AgentApp.role == RUNTIME_ROLE,
        AgentApp.is_deleted == False,  # noqa: E712
    ).first()
    if existing:
        return existing

    agent = AgentApp(
        name=RUNTIME_AGENT_NAME,
        description="Hidden executor for scheduled automations. Not user-editable.",
        project="global",
        capabilities=["automation_execution", "report_generation"],
        model="gpt-4o-mini",
        agent_type="sequential",
        topology="standalone",
        status="active",
        tool_config=_runtime_tool_config(),
        manifest_json=_runtime_manifest(),
        prompt_identity=_RUNTIME_SYSTEM_PROMPT,
        is_system=True,
        role=RUNTIME_ROLE,
        memory_scope="app_shared",
        org_id=org_id,
        app_id=app_id,
        # created_by_id stays None — system-owned
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    logger.info(
        "Created automation_runtime_agent for (org=%s, app=%s) id=%s",
        org_id, app_id, agent.id,
    )
    return agent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k runtime_agent`
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_runtime.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: add ensure_automation_runtime_agent (idempotent per org/app)"
```

---

### Task 3: Eager provisioning + startup backfill

**Files:**
- Modify: `backend/app/services/system_agents.py` (extend `ensure_system_agents`)
- Modify: `backend/app/services/automation_runtime.py` (add `backfill_automation_runtime_agents`)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: `backfill_automation_runtime_agents(db: Session) -> int` — iterates all distinct `(org_id, app_id)` pairs that have at least one `AutomationTask`, ensures a runtime agent exists for each, and rebinds tasks with NULL/invalid `agent_id`. Returns the number of tasks rebound.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.models.automation_task import AutomationTask
from app.services.automation_runtime import backfill_automation_runtime_agents


def test_backfill_rebinds_null_agent_id():
    """Tasks with agent_id=NULL get rebound to the runtime agent."""
    db = SessionLocal()
    try:
        org, app = "backfill-org", "backfill-app"
        t = AutomationTask(
            name="test backfill", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=None, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.agent_id is None

        rebound = backfill_automation_runtime_agents(db)
        assert rebound >= 1
        db.refresh(t)
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert t.agent_id == runtime.id
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_backfill_preserves_valid_user_agent_id():
    """Tasks with a valid non-system agent_id are left alone."""
    db = SessionLocal()
    try:
        org, app = "preserve-org", "preserve-app"
        user_agent = AgentApp(name="my_user_agent", is_system=False, status="active",
                              org_id=org, app_id=app)
        db.add(user_agent)
        db.commit()
        db.refresh(user_agent)
        t = AutomationTask(
            name="test preserve", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=user_agent.id, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        backfill_automation_runtime_agents(db)
        db.refresh(t)
        assert t.agent_id == user_agent.id  # unchanged
        db.delete(t)
        db.delete(user_agent)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k backfill`
Expected: FAIL with `ImportError: cannot import name 'backfill_automation_runtime_agents'`.

- [ ] **Step 3: Implement `backfill_automation_runtime_agents`**

Append to `backend/app/services/automation_runtime.py`:

```python
def backfill_automation_runtime_agents(db: Session) -> int:
    """Ensure a runtime agent exists for every (org, app) that has automation
    tasks, and rebind tasks whose ``agent_id`` is NULL or points at a
    missing/deleted/non-runtime agent.

    Idempotent and safe to re-run. Returns the number of tasks rebound.
    """
    from app.models.automation_task import AutomationTask

    pairs = db.query(
        AutomationTask.org_id, AutomationTask.app_id
    ).filter(
        AutomationTask.is_deleted == False,  # noqa: E712
    ).distinct().all()

    rebound = 0
    for org_id, app_id in pairs:
        runtime = ensure_automation_runtime_agent(db, org_id, app_id)
        # Find tasks in this (org, app) with NULL or invalid agent_id.
        tasks = db.query(AutomationTask).filter(
            AutomationTask.org_id == org_id,
            AutomationTask.app_id == app_id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).all()
        for task in tasks:
            if task.agent_id:
                # Validate the pinned agent is still usable.
                pinned = db.query(AgentApp).filter(
                    AgentApp.id == task.agent_id,
                    AgentApp.is_deleted == False,  # noqa: E712
                ).first()
                if pinned is not None:
                    continue  # valid user/system agent — leave it
            task.agent_id = runtime.id
            rebound += 1
        db.commit()
    if rebound:
        logger.info("backfill_automation_runtime_agents: rebound %d tasks", rebound)
    return rebound
```

- [ ] **Step 4: Wire eager provisioning into `ensure_system_agents`**

In `backend/app/services/system_agents.py`, at the END of the `try:` block in `ensure_system_agents` (after the `if created or updated:` / `else:` block, before the `except`), add a call to backfill existing tasks:

```python
        # Eager provisioning: ensure a runtime agent exists for every
        # (org, app) that already has automation tasks, and rebind any
        # tasks with a NULL/invalid agent_id. Idempotent + safe on every
        # startup.
        try:
            from app.services.automation_runtime import backfill_automation_runtime_agents
            backfill_automation_runtime_agents(db)
        except Exception as _bf_err:
            db.rollback()
            logger.warning("ensure_system_agents: runtime backfill failed (non-fatal): %s", _bf_err)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k backfill`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_runtime.py backend/app/services/system_agents.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: eager provisioning + startup backfill of runtime agents"
```

---

### Task 4: Auto-bind runtime agent in `_create_automation`

**Files:**
- Modify: `backend/app/services/agent_tools.py:1231` (the `agent_id=` line in `_create_automation`)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Consumes: `ensure_automation_runtime_agent(db, org_id, app_id)` from Task 2.
- Produces: new `AutomationTask` records have `agent_id` set to the runtime agent (unless the caller supplied a valid custom `agent_id`). The chat agent (`TOOL_CONTEXT["agent_app_id"]`) is NO LONGER the default executor.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.services.agent_tools import _create_automation
from app.services.tool_context import TOOL_CONTEXT  # adjust import if needed


def test_create_automation_binds_runtime_agent_not_chat_agent():
    """_create_automation must default agent_id to the runtime agent,
    NOT to the chat agent from TOOL_CONTEXT."""
    db = SessionLocal()
    try:
        org, app = "create-org", "create-app"
        # Simulate a chat agent in context — it must NOT become the executor.
        chat_agent = AgentApp(name="chat_agent_test", is_system=False, status="active",
                              org_id=org, app_id=app)
        db.add(chat_agent)
        db.commit()
        db.refresh(chat_agent)
        TOOL_CONTEXT["agent_app_id"] = chat_agent.id
        TOOL_CONTEXT["chat_session_id"] = None

        result = _create_automation(
            {"name": "auto-bind test", "type": "custom", "prompt": "hi",
             "schedule": "manual", "project_id": None,
             "org_id": org, "app_id": app},
            db, user_id=None,
        )
        assert result["success"] is True
        task = db.query(AutomationTask).filter(
            AutomationTask.id == result["id"]
        ).first()
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert task.agent_id == runtime.id
        assert task.agent_id != chat_agent.id
        # cleanup
        db.delete(task)
        db.delete(chat_agent)
        db.delete(runtime)
        db.commit()
    finally:
        TOOL_CONTEXT.pop("agent_app_id", None)
        db.close()
```

**Note on `TOOL_CONTEXT` import:** if `tool_context` is not a module, import from where `agent_tools.py` imports it. Check `agent_tools.py` top imports — if `TOOL_CONTEXT` is defined in `agent_tools.py` itself, import via `from app.services.agent_tools import TOOL_CONTEXT`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_create_automation_binds_runtime_agent_not_chat_agent -v`
Expected: FAIL — `task.agent_id == chat_agent.id` (current code defaults to the chat agent).

- [ ] **Step 3: Modify `_create_automation` to auto-bind the runtime agent**

In `backend/app/services/agent_tools.py`, locate the `AutomationTask(...)` constructor call in `_create_automation` (around line 1219-1236). Replace the `agent_id` line:

Old (line ~1231):
```python
        agent_id=args.get("agent_id") or TOOL_CONTEXT.get("agent_app_id"),
```

New:
```python
        agent_id=_resolve_task_executor_agent(db, args),
```

Then add this helper function ABOVE `_create_automation` (before line 1173):

```python
def _resolve_task_executor_agent(db: Session, args: dict) -> str:
    """Resolve the agent_id for a new AutomationTask.

    Priority:
    1. A user-supplied ``agent_id`` that is valid (exists, same org/app,
       not deleted) — honored so power users can pin a custom executor.
    2. The hidden automation_runtime_agent for the task's (org_id, app_id).
    The chat agent (TOOL_CONTEXT['agent_app_id']) is NEVER used as the
    default executor — it creates the task but does not run it.
    """
    from app.services.automation_runtime import ensure_automation_runtime_agent
    from app.models.agent_app import AgentApp as _AA

    org_id = args.get("org_id") or "default-org"
    app_id = args.get("app_id") or "default-app"

    supplied = args.get("agent_id")
    if supplied:
        valid = db.query(_AA).filter(
            _AA.id == supplied,
            _AA.is_deleted == False,  # noqa: E712
        ).first()
        if valid is not None:
            return supplied
        logger.warning(
            "_resolve_task_executor_agent: supplied agent_id=%s not found; "
            "falling back to runtime agent", supplied,
        )

    runtime = ensure_automation_runtime_agent(db, org_id, app_id)
    return runtime.id
```

Also ensure `org_id`/`app_id` are passed into the `AutomationTask(...)` constructor. Check that the constructor includes `org_id=org_id, app_id=app_id` — if not, add them (resolve from `args` or `TOOL_CONTEXT`). If the existing constructor does not set org_id/app_id explicitly, add after the `agent_id` line:

```python
        org_id=args.get("org_id") or "default-org",
        app_id=args.get("app_id") or "default-app",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_create_automation_binds_runtime_agent_not_chat_agent -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/agent_tools.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: auto-bind runtime agent in _create_automation (no chat-agent default)"
```

---

### Task 5: Update `_resolve_agent` — runtime agent fallback

**Files:**
- Modify: `backend/app/services/automation_executor.py:146-190` (the `_resolve_agent` function)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Consumes: `ensure_automation_runtime_agent(db, org_id, app_id)` from Task 2.
- Produces: `_resolve_agent(db, task)` returns `(runtime_agent, "ok")` when `task.agent_id` is NULL or stale — instead of the current "N agents exist, none pinned" failure. Fails cleanly only if the runtime agent cannot be created.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.services.automation_executor import _resolve_agent


def test_resolve_agent_falls_back_to_runtime_when_agent_id_null():
    """When task.agent_id is NULL, _resolve_agent returns the runtime agent."""
    db = SessionLocal()
    try:
        org, app = "resolve-org", "resolve-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        t = AutomationTask(
            name="resolve test", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=None, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        agent, reason = _resolve_agent(db, t)
        assert agent is not None
        assert agent.id == runtime.id
        assert reason == "ok"
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_resolve_agent_falls_back_to_runtime_when_pinned_agent_deleted():
    """When task.agent_id points at a deleted agent, fall back to runtime."""
    db = SessionLocal()
    try:
        org, app = "resolve-del-org", "resolve-del-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        t = AutomationTask(
            name="resolve deleted test", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id="nonexistent-agent-id", org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        agent, reason = _resolve_agent(db, t)
        assert agent is not None
        assert agent.id == runtime.id
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k resolve_agent`
Expected: FAIL — current `_resolve_agent` returns `(None, "no agents exist...")` or `(... "N agents exist ...")`.

- [ ] **Step 3: Rewrite `_resolve_agent`**

In `backend/app/services/automation_executor.py`, replace the body of `_resolve_agent` (lines 146-190) with:

```python
def _resolve_agent(db: Session, task: AutomationTask) -> Tuple[Optional[AgentApp], str]:
    """Pick the agent to run. Returns ``(agent, reason)``.

    Resolution order:
    1. ``task.agent_id`` if it is valid (exists, not deleted, active).
    2. The hidden ``automation_runtime_agent`` for the task's
       ``(org_id, app_id)`` — created on demand (self-healing).
    3. ``(None, reason)`` only if the runtime agent cannot be created.

    The old behavior ("if 1 workspace agent use it; if many, fail") is
    removed — the runtime agent is always the deterministic fallback.
    """
    if task.agent_id:
        agent = db.query(AgentApp).filter(
            AgentApp.id == task.agent_id,
            AgentApp.is_deleted == False,  # noqa: E712
        ).first()
        if agent and agent.status == "active":
            return agent, "ok"
        logger.warning(
            "automation task %s pins agent_id=%s but it's missing/deleted/inactive; "
            "falling back to runtime agent", task.id, task.agent_id,
        )

    # Deterministic fallback: the hidden runtime agent for this (org, app).
    try:
        from app.services.automation_runtime import ensure_automation_runtime_agent
        runtime = ensure_automation_runtime_agent(db, task.org_id, task.app_id)
        return runtime, "ok"
    except Exception as e:
        logger.exception(
            "_resolve_agent: failed to ensure runtime agent for task %s: %s",
            task.id, e,
        )
        return None, (
            "The backend execution engine is unavailable. "
            "Please contact support."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k resolve_agent`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: _resolve_agent falls back to runtime agent (no random workspace pick)"
```

---

### Task 6: Transient-error classification (integrate with existing retry)

**Files:**
- Modify: `backend/app/services/automation_executor.py` (add `_is_transient_error`; route non-transient errors through `_mark_failed_no_retry`)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: `_is_transient_error(error: Exception) -> bool` — returns `True` for HTTP 5xx, model timeout, rate-limit, and DB OperationalError; `False` otherwise. The executor's failure path uses it to decide: transient → `_mark_failed` (which schedules a retry via the existing dispatcher); non-transient → `_mark_failed_no_retry` (fail fast, no retry wasted).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.services.automation_executor import _is_transient_error


def test_is_transient_http_5xx():
    """A 503-style error is transient."""
    class FakeHTTPError(Exception):
        pass
    assert _is_transient_error(FakeHTTPError("server returned 503 service unavailable"))


def test_is_transient_timeout():
    """A timeout error is transient."""
    assert _is_transient_error(Exception("connection timed out after 30s"))


def test_is_transient_rate_limit():
    """A rate-limit error is transient."""
    assert _is_transient_error(Exception("429 rate limit exceeded"))


def test_not_transient_validation_error():
    """A validation/4xx error is NOT transient — retrying wastes budget."""
    assert not _is_transient_error(ValueError("invalid prompt: empty"))


def test_not_transient_permission_denied():
    """A 403 permission error is NOT transient."""
    assert not _is_transient_error(PermissionError("403 forbidden: no access"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k transient`
Expected: FAIL with `ImportError: cannot import name '_is_transient_error'`.

- [ ] **Step 3: Add `_is_transient_error` and route the executor failure path**

In `backend/app/services/automation_executor.py`, after the `NETWORK_FAILURE_PATTERNS` tuple (after line ~82), add:

```python
# Patterns that mark an error as TRANSIENT (worth retrying). Anything
# else is treated as non-transient and fails fast via _mark_failed_no_retry
# so we don't waste the retry budget on errors that will reproduce.
_TRANSIENT_PATTERNS = (
    # HTTP 5xx
    "500", "502", "503", "504", "internal server error", "bad gateway",
    "service unavailable", "gateway timeout",
    # Timeouts
    "timed out", "timeout", "read timeout", "connection reset",
    "connection aborted", "upstream timeout",
    # Rate limits
    "429", "rate limit", "rate_limit", "rate-limit", "too many requests",
    # DB connection drops
    "operationalerror", "connection refused", "connection lost",
    "server closed the connection", "database is locked",
)


def _is_transient_error(error: BaseException) -> bool:
    """Return True if ``error`` is a transient failure worth retrying.

    Classifies by message patterns (stable + reviewable, no LLM call).
    HTTP 4xx (except 429), validation errors, and permission errors are
    non-transient — retrying them just reproduces the same failure.
    """
    text = (str(error) or "").lower()
    if not text:
        return False
    # Explicit non-transient: 4xx (except 429 which is rate-limit)
    for pat in ("400", "401", "403", "404", "422", "forbidden", "not found",
                "unauthorized", "validation"):
        if pat in text and "429" not in text:
            return False
    return any(p in text for p in _TRANSIENT_PATTERNS)
```

Then modify the failure handlers to USE the classifier. In `execute_automation`, the generic `except Exception as e:` block (line ~468-472) currently calls `_mark_failed` unconditionally. Change it to:

Old:
```python
        except Exception as e:
            pool.shutdown(wait=False, cancel_futures=True)
            logger.exception("execute_automation: agent run failed: %s", e)
            _mark_failed(db, execution, f"Agent run failed: {e}")
            return
```

New:
```python
        except Exception as e:
            pool.shutdown(wait=False, cancel_futures=True)
            logger.exception("execute_automation: agent run failed: %s", e)
            err_msg = f"Agent run failed: {e}"
            if _is_transient_error(e):
                _mark_failed(db, execution, err_msg)  # schedules a retry
            else:
                _mark_failed_no_retry(db, execution, err_msg)  # fail fast
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k transient`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: classify transient vs non-transient errors for retry decisions"
```

---

### Task 7: Per-project memory conversation ledger

**Files:**
- Modify: `backend/app/services/automation_runtime.py` (add `get_or_create_project_conversation`, `append_run_summary`)
- Modify: `backend/app/services/automation_executor.py` (inject memory context before run; append summary after)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: `get_or_create_project_conversation(db, runtime_agent, project_id) -> AgentConversation` — idempotent; one shared `AgentConversation` per `(agent_name="automation_runtime_agent", project_id, org_id, app_id)`. `append_run_summary(db, conv, run_id, status, summary)` — appends a compact ledger message. The executor reads the last N ledger entries as context and writes one after each run.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
from app.services.automation_runtime import (
    get_or_create_project_conversation, append_run_summary,
)


def test_project_conversation_is_idempotent():
    """Repeated calls return the same conversation for the same project."""
    db = SessionLocal()
    try:
        org, app = "mem-org", "mem-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        c1 = get_or_create_project_conversation(db, runtime, "proj-1")
        c2 = get_or_create_project_conversation(db, runtime, "proj-1")
        assert c1.id == c2.id
        db.delete(c1)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_different_projects_get_different_conversations():
    """Different project_ids get different conversations."""
    db = SessionLocal()
    try:
        org, app = "mem2-org", "mem2-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        c1 = get_or_create_project_conversation(db, runtime, "proj-a")
        c2 = get_or_create_project_conversation(db, runtime, "proj-b")
        assert c1.id != c2.id
        db.delete(c1)
        db.delete(c2)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_append_run_summary_grows_messages():
    """append_run_summary adds a ledger entry to the conversation."""
    db = SessionLocal()
    try:
        org, app = "mem3-org", "mem3-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        conv = get_or_create_project_conversation(db, runtime, "proj-s")
        before = len(conv.messages or [])
        append_run_summary(db, conv, "run-1", "ok", "Sales up 12%")
        db.refresh(conv)
        after = len(conv.messages or [])
        assert after == before + 1
        db.delete(conv)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k project_conversation`
Expected: FAIL with `ImportError: cannot import name 'get_or_create_project_conversation'`.

- [ ] **Step 3: Implement the memory functions**

Append to `backend/app/services/automation_runtime.py`:

```python
# Max ledger entries kept in the per-project conversation's messages list.
# Older entries are trimmed to bound the LLM context window.
_MAX_MEMORY_ENTRIES = 50


def get_or_create_project_conversation(
    db: Session, runtime_agent: AgentApp, project_id: Optional[str]
) -> AgentConversation:
    """Idempotently get-or-create the per-project memory ledger conversation.

    All automations in the same project share this conversation so the
    runtime agent builds up project knowledge across runs (Notion-AI style).
    Keyed by (agent_name, project_id, org_id, app_id).
    """
    pid = project_id or "global"
    existing = db.query(AgentConversation).filter(
        AgentConversation.agent_name == RUNTIME_AGENT_NAME,
        AgentConversation.project_id == pid,
        AgentConversation.org_id == runtime_agent.org_id,
        AgentConversation.app_id == runtime_agent.app_id,
        AgentConversation.is_deleted == False,  # noqa: E712
    ).first()
    if existing:
        return existing

    conv = AgentConversation(
        agent_name=RUNTIME_AGENT_NAME,
        title=f"Automation memory · project {pid}",
        messages=[{
            "role": "system",
            "content": (
                "Per-project automation memory ledger. Each entry records "
                "one scheduled run's prompt, status, and output summary. "
                "The runtime agent reads this for cross-run continuity."
            ),
        }],
        status="active",
        project_id=pid,
        org_id=runtime_agent.org_id,
        app_id=runtime_agent.app_id,
        metadata_={"kind": "automation_runtime_memory", "project_id": pid},
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def append_run_summary(
    db: Session,
    conv: AgentConversation,
    run_id: str,
    status: str,
    summary: str,
) -> None:
    """Append a compact ledger entry for one run, trimming old entries."""
    from datetime import datetime
    msgs = list(conv.messages or [])
    msgs.append({
        "role": "assistant",
        "content": f"[run {run_id} @ {datetime.utcnow().isoformat()}] "
                    f"status: {status} | {summary[:500]}",
    })
    # Trim to the most recent N entries (keep the system message at index 0).
    if len(msgs) > _MAX_MEMORY_ENTRIES:
        msgs = [msgs[0]] + msgs[-(_MAX_MEMORY_ENTRIES - 1):]
    conv.messages = msgs
    db.add(conv)
    db.commit()
```

- [ ] **Step 4: Wire memory into the executor**

In `backend/app/services/automation_executor.py`, inside `execute_automation`, AFTER `_resolve_agent` succeeds (after line ~408) and BEFORE building `user_prompt`, inject recent memory context. Add:

```python
        # Per-project memory: read recent ledger entries for cross-run
        # continuity (the runtime agent builds project knowledge over time).
        _memory_context = ""
        try:
            from app.services.automation_runtime import (
                get_or_create_project_conversation,
            )
            if getattr(agent, "role", None) == "automation_runtime":
                _mem_conv = get_or_create_project_conversation(
                    db, agent, task.project_id,
                )
                _recent = (_mem_conv.messages or [])[-6:]  # last 6 entries
                if len(_recent) > 1:  # more than just the system init
                    _memory_context = (
                        "\n\n## Recent project memory\n"
                        + "\n".join(
                            m.get("content", "") for m in _recent
                            if m.get("role") != "system"
                        )
                    )
        except Exception as _mem_err:
            logger.debug("execute_automation: memory read failed (non-fatal): %s", _mem_err)
```

Then append `_memory_context` to the `agent_prompt` string (add `+ (_memory_context or "")` to the existing concatenation around line 429-437).

After a SUCCESSFUL run (in the `else:` block after `fut.result`, around line 473-474), append a summary:

```python
        else:
            pool.shutdown(wait=False)
            # Append a ledger entry to the per-project memory conversation.
            try:
                from app.services.automation_runtime import (
                    get_or_create_project_conversation, append_run_summary,
                )
                if getattr(agent, "role", None) == "automation_runtime":
                    _mem_conv = get_or_create_project_conversation(
                        db, agent, task.project_id,
                    )
                    append_run_summary(
                        db, _mem_conv, execution.id, "ok",
                        (assistant_text or "")[:300],
                    )
            except Exception as _mem_err:
                logger.debug("execute_automation: memory write failed (non-fatal): %s", _mem_err)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py -v -k "project_conversation or run_summary"`
Expected: PASS (all 3 tests).

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_runtime.py backend/app/services/automation_executor.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: per-project shared memory ledger for the runtime agent"
```

---

### Task 8: Hide runtime agent from API listings + UI badge

**Files:**
- Modify: `backend/app/routers/agents.py` (filter `role="automation_runtime"` from user-facing agent queries)
- Modify: `frontend/src/pages/AutomationTaskDetail.jsx` (show "Backend engine: Connected" badge)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: user-facing agent listings exclude `role="automation_runtime"` agents (defense-in-depth on top of the existing `is_system=True` filter). The automation detail page shows a static "Backend engine: Connected" badge instead of the agent id.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
def test_runtime_agent_is_excluded_from_user_listing():
    """A direct query for user-visible agents must not return the runtime agent.

    This tests the filtering predicate we apply in the agents router. Since
    the router file is large, we test the predicate directly.
    """
    db = SessionLocal()
    try:
        org, app = "hide-org", "hide-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        # The user-facing listing predicate: is_system=False AND role IS NULL
        # (or role != 'automation_runtime').
        visible = db.query(AgentApp).filter(
            AgentApp.org_id == org,
            AgentApp.app_id == app,
            AgentApp.is_deleted == False,  # noqa: E712
            AgentApp.is_system == False,  # noqa: E712
        ).all()
        assert runtime.id not in {a.id for a in visible}
        db.delete(runtime)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it passes (is_system=True already hides it)**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_runtime_agent_is_excluded_from_user_listing -v`
Expected: PASS (the runtime agent has `is_system=True`, so the `is_system == False` filter already excludes it). This test is a regression guard.

- [ ] **Step 3: Add defense-in-depth `role` filter to the agents router**

Search `backend/app/routers/agents.py` for any endpoint that lists agents visible to users (queries returning multiple `AgentApp` rows without an `is_system` filter). For each such query, add `AgentApp.role != RUNTIME_ROLE` (or `AgentApp.role.is_(None)`) to the filter. If the listing already filters `is_system == False`, the runtime agent is already excluded — add a comment noting the `role` defense-in-depth. If no user-listing endpoint filters by `is_system`, add the filter.

**Implementation note:** The agents.py router is 297KB. Search for `AgentApp.is_deleted == False` queries that return `.all()` and are NOT already filtered by `is_system`. Add `AgentApp.is_system == False` where missing. This is a verification + patch step.

- [ ] **Step 4: Add the "Backend engine: Connected" badge to the detail page**

In `frontend/src/pages/AutomationTaskDetail.jsx`, find where agent information (if any) is displayed. Add a static badge component:

```jsx
{/* Backend engine status — the runtime agent is hidden; show a clean badge */}
<div className="flex items-center gap-2 text-sm text-green-600 dark:text-green-400">
  <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
  Backend engine: Connected
</div>
```

Place it near the task status/header area. If the page shows an error state for a missing agent, replace that error with: `This automation could not run because the backend execution engine is unavailable. Check execution history for details.`

- [ ] **Step 5: Run test + verify**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_runtime_agent_is_excluded_from_user_listing -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/agents.py frontend/src/pages/AutomationTaskDetail.jsx backend/tests/test_automation_runtime_agent.py
git commit -m "feat: hide runtime agent from listings; add Backend engine badge"
```

---

### Task 9: Admin diagnostic endpoint

**Files:**
- Modify: `backend/app/routers/agents.py` (add `GET /agents/runtime` endpoint)
- Test: `backend/tests/test_automation_runtime_agent.py` (append)

**Interfaces:**
- Produces: `GET /agents/runtime` — platform-admin-only endpoint returning the runtime agents' id, (org_id, app_id), status, and created_date for diagnostics. Gated by the existing `is_platform_admin` user flag.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_automation_runtime_agent.py`:

```python
def test_runtime_agent_has_role_for_diagnostics():
    """The runtime agent carries role='automation_runtime' so the admin
    diagnostic endpoint can find it."""
    db = SessionLocal()
    try:
        org, app = "diag-org", "diag-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert runtime.role == "automation_runtime"
        # The diagnostic query (what the admin endpoint runs):
        runtimes = db.query(AgentApp).filter(
            AgentApp.role == "automation_runtime",
            AgentApp.is_deleted == False,  # noqa: E712
        ).all()
        assert runtime.id in {a.id for a in runtimes}
        db.delete(runtime)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_runtime_agent_has_role_for_diagnostics -v`
Expected: PASS (role was set in Task 2).

- [ ] **Step 3: Add the diagnostic endpoint**

In `backend/app/routers/agents.py`, add a new endpoint near the other agent endpoints. First find how the existing endpoints authenticate users and check `is_platform_admin` (search for `is_platform_admin` in the router or auth deps). Then add:

```python
@router.get("/agents/runtime")
async def list_runtime_agents(
    db: Session = Depends(get_db),
    # Reuse the existing auth dependency that provides the current user.
    # Adjust the import/dependency to match the codebase convention.
):
    """Platform-admin diagnostic: list all automation runtime agents.

    Returns id, org_id, app_id, status, created_date for each hidden
    runtime agent. Gated by is_platform_admin.
    """
    # Auth check — adjust to the codebase's actual user-dependency pattern.
    # Example: if a `current_user` dependency exists, check current_user.is_platform_admin.
    # If not admin, raise HTTPException(403).
    from app.models.agent_app import AgentApp as _AA
    agents = db.query(_AA).filter(
        _AA.role == "automation_runtime",
        _AA.is_deleted == False,  # noqa: E712
    ).all()
    return [
        {
            "id": a.id,
            "org_id": a.org_id,
            "app_id": a.app_id,
            "status": a.status,
            "created_date": a.created_date.isoformat() if a.created_date else None,
        }
        for a in agents
    ]
```

**Important:** Before writing the final code, read the top of `agents.py` to find the exact auth dependency name (e.g. `get_current_user`, `require_admin`) and the `is_platform_admin` attribute path. Wire the 403 gate to it. If no platform-admin concept exists yet, gate on the existing admin check used by other admin endpoints in the file.

- [ ] **Step 4: Run test + verify**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py::test_runtime_agent_has_role_for_diagnostics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/agents.py backend/tests/test_automation_runtime_agent.py
git commit -m "feat: admin diagnostic endpoint for runtime agents"
```

---

### Task 10: Full regression sweep + integration smoke test

**Files:**
- Test: `backend/tests/test_automation_runtime_integration.py` (create)

**Interfaces:**
- Produces: an end-to-end test that creates an automation (via `_create_automation`), resolves its agent (via `_resolve_agent`), verifies it's the runtime agent, and confirms the per-project memory conversation is created.

- [ ] **Step 1: Write the integration test**

Create `backend/tests/test_automation_runtime_integration.py`:

```python
"""End-to-end smoke test for the automation runtime agent subsystem."""
from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.agent_app import AgentApp
from app.services.agent_tools import _create_automation
from app.services.automation_executor import _resolve_agent
from app.services.automation_runtime import (
    ensure_automation_runtime_agent,
    get_or_create_project_conversation,
)


def test_full_create_resolve_memory_flow():
    """Create a task → resolve its executor → verify runtime agent + memory."""
    db = SessionLocal()
    try:
        org, app, project = "e2e-org", "e2e-app", "e2e-project"
        result = _create_automation(
            {"name": "e2e test", "type": "custom", "prompt": "weekly sales",
             "schedule": "0 9 * * 1", "project_id": project,
             "org_id": org, "app_id": app},
            db, user_id=None,
        )
        assert result["success"] is True

        task = db.query(AutomationTask).filter(
            AutomationTask.id == result["id"]
        ).first()
        # 1. agent_id is bound (not NULL, not the chat agent)
        assert task.agent_id is not None

        # 2. _resolve_agent returns the runtime agent
        agent, reason = _resolve_agent(db, task)
        assert agent is not None
        assert reason == "ok"
        assert agent.role == "automation_runtime"
        assert agent.is_system is True

        # 3. Per-project memory conversation exists
        conv = get_or_create_project_conversation(db, agent, project)
        assert conv is not None
        assert conv.project_id == project

        # cleanup
        db.delete(task)
        db.delete(conv)
        db.delete(agent)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run the full test suite for this feature**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_automation_runtime_agent.py tests/test_automation_runtime_integration.py -v`
Expected: ALL PASS.

- [ ] **Step 3: Run a targeted regression on existing automation tests**

Run (single files, no parallel, to respect RAM): 
`cd /root/zhanlu/backend && python -m pytest tests/test_automation_failure_notify.py tests/test_automation_failure_recovery.py -v`
Expected: PASS — existing retry/notify behavior is unchanged (we only ADDED classification on top).

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/tests/test_automation_runtime_integration.py
git commit -m "test: end-to-end smoke test for automation runtime agent"
```

---

## Self-Review

**1. Spec coverage:**
- §3.1 `role` column → Task 1 ✓
- §4.1 `ensure_automation_runtime_agent` → Task 2 ✓
- §4.2 Eager provisioning + backfill → Task 3 ✓
- §4.3 `get_or_create_project_conversation` → Task 7 ✓
- §4.4 Auto-binding at creation → Task 4 ✓
- §4.5 `_resolve_agent` update → Task 5 ✓
- §4.6 Retry + failure handling → Task 6 (adapted: integrates with existing `schedule_retry`/`_mark_failed`/`_notify_chat_failure` instead of `time.sleep`-based loop, per codebase reality) ✓
- §4.7 Memory write policy → Task 7 ✓
- §4.8 UI changes → Task 8 ✓
- §4.9 Backfill migration → Task 3 ✓
- §7 Security/permissions → runtime agent is `is_system=True` + `role` filter + forbidden tool list (Task 2) ✓
- §8 Testing → Tasks 1-10 cover unit + integration + regression ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"add appropriate" patterns. The agents.py router search in Task 8 Step 3 and the auth-dependency lookup in Task 9 Step 3 are explicit investigation steps (the file is 297KB so exact line numbers can't be pre-stated) — these are guided, not placeholder.

**3. Type consistency:** `ensure_automation_runtime_agent(db, org_id, app_id)` signature is consistent across Tasks 2/3/4/5/7/10. `get_or_create_project_conversation(db, runtime_agent, project_id)` consistent across Tasks 7/10. `RUNTIME_ROLE = "automation_runtime"` and `RUNTIME_AGENT_NAME = "automation_runtime_agent"` consistent throughout. `_is_transient_error(error)` consistent in Task 6.

**Spec→reality adaptation note:** The spec's §4.6 proposed a synchronous `_execute_with_retry` with `time.sleep`. The actual codebase uses async per-execution retry (`schedule_retry` creates a new `AutomationExecution` row with backoff). Task 6 therefore adds `_is_transient_error` classification and routes non-transient errors to the existing `_mark_failed_no_retry` — achieving the same goal (don't waste retries on non-transient errors; notify on final failure) without breaking the async dispatcher.
