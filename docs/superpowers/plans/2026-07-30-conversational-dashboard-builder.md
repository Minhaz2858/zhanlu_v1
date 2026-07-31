# Conversational Dashboard Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing live-dashboard system into a base44-style conversational builder — chat creates/edits a dashboard, a dockable side panel shows it live and re-renders instantly on each agent edit, with one dashboard bound per conversation and one-click undo.

**Architecture:** A `dashboard_id` FK on `agent_conversations` binds one dashboard per conversation. `create_dashboard`/`update_dashboard` tools already receive `conversation_id` in their `context` dict (populated by `agents.py`) — they now use it to bind/resolve the target without an explicit ID. A `DashboardVersion` table snapshots the prior config on every edit for undo. The frontend lifts the dashboard panel state out of `MessageBubble` (which unmounts per-message) up to `Chat`, evolves `DashboardPopup` into a persistent resizable docked panel, and watches the message stream for `create/update_dashboard`/`undo` tool results to trigger an immediate refetch.

**Tech Stack:** FastAPI + SQLAlchemy (runtime `ALTER TABLE`, no Alembic), pydantic v2, React + Vite, vitest, `@/api/dashboards.js` (authFetch), recharts.

## Global Constraints

- **RAM limited**: never run the full pytest suite or broad `-k` filters; run explicit test-file paths only; no `-n` workers. Run `free -h` first if unsure.
- **Test DB setup**: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"` then run a specific test file.
- **Schema via runtime `ALTER TABLE`** (no Alembic): new columns go in `main.py:_run_auto_migrations()` `specific_migrations` dict; new tables are created by `Base.metadata.create_all()` at startup (just register the model in `app/models/__init__.py`).
- **`TimestampedBase`** provides `id`, `created_date`/`updated_date` (NOT `created_at`), `created_by_id`, `is_deleted`, `org_id`, `app_id`, and `to_dict()`.
- **Dual project binding**: `project_id` (FK) + legacy `project` (name str) — match BOTH where relevant.
- **DashboardViewer invariants MUST stay verbatim**: `mountedRef`, `visibleRef`, the `visibilitychange` listener, and the polling clamp `Math.max(10, Math.min(300, ...))` are load-bearing — do not alter their behavior.
- **Frontend has NO client-side user context**: edit-gating comes from the server-computed `can_edit` flag; never read a local user id.
- **E2E credentials**: real seeded admin is `admin@zhanlu.dev` / `admin123` (NOT `Test1234!`).
- **Backend deploy**: after backend edits, `docker restart zhanlu-backend`. Frontend deploy: `/opt/rebuild-zhanlu.sh --force`.
- **Chart colors**: `hsl(var(--chart-N))` tokens, NEVER hex.
- **Commit only when the user asks.**

---

## File Structure

**Backend (create):**
- `app/models/dashboard_version.py` — `DashboardVersion` model (config snapshots for undo).

**Backend (modify):**
- `app/models/agent_conversation.py` — add `dashboard_id` FK column.
- `app/models/__init__.py` — register `DashboardVersion`.
- `main.py` — add `dashboard_id` to the `agent_conversations` `specific_migrations` entry.
- `app/services/tool_handlers/dashboard_tools.py` — bind on create; resolve target on update; make `dashboard_id` optional; add `undo_dashboard_edit` tool.
- `app/routers/dashboards.py` — `GET /by-conversation/{conversation_id}`; `POST /{id}/undo`; `can_undo` in `_with_can_edit`.

**Frontend (create):**
- `src/components/dashboard/DashboardDockPanel.jsx` — persistent, resizable docked panel (evolved from `DashboardPopup`).
- `src/components/dashboard/DashboardDockPanel.test.jsx` — vitest.

**Frontend (modify):**
- `src/api/dashboards.js` — `getDashboardByConversation`, `undoDashboardEdit`.
- `src/components/dashboard/DashboardViewer.jsx` — `forwardRef` + `useImperativeHandle` exposing `refresh`.
- `src/pages/Chat.jsx` — lift `openDashboardId` state; mount `DashboardDockPanel`; tool-result watcher.
- `src/components/chat/MessageBubble.jsx` — remove local `openDashboardId`/`DashboardPopup`; delegate `onOpen` to a passed-in callback.

**Tests (create):**
- `backend/tests/test_dashboard_binding.py`
- `backend/tests/test_dashboard_versions.py`
- `frontend/src/components/dashboard/DashboardDockPanel.test.jsx`

---

## Phase A — Conversation ↔ Dashboard Binding + Resolution (backend)

### Task 1: Add `dashboard_id` FK to `AgentConversation` + migration

**Files:**
- Modify: `app/models/agent_conversation.py:22-24` (after `project_id`)
- Modify: `main.py:224-226` (the `agent_conversations` specific_migrations entry)
- Test: `backend/tests/test_dashboard_binding.py` (created here, extended in later tasks)

**Interfaces:**
- Produces: `AgentConversation.dashboard_id` (nullable `str`, FK → `dashboards.id`, `ON DELETE SET NULL`, indexed).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_dashboard_binding.py`:

```python
"""Conversation <-> dashboard binding: column exists + 1:1 FK semantics."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import inspect as sa_inspect


def test_agent_conversation_has_dashboard_id_column(tmp_path):
    # Fresh sqlite DB to inspect the column set.
    db_file = tmp_path / "t.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    from app.database import Base, engine
    import app.models  # noqa: registers all mappers
    Base.metadata.create_all(engine)
    cols = {c["name"] for c in sa_inspect(engine).get_columns("agent_conversations")}
    assert "dashboard_id" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_agent_conversation_has_dashboard_id_column -v`
Expected: FAIL — `dashboard_id` not in columns.

- [ ] **Step 3: Add the column to the model**

In `app/models/agent_conversation.py`, after the `project_id` block (lines 22-24), add:

```python
    # The ONE dashboard bound to this conversation (1:1). Set by
    # create_dashboard when invoked from chat; subsequent update_dashboard
    # calls resolve this binding automatically. ON DELETE SET NULL so
    # deleting a dashboard forgets the binding without destroying the chat.
    dashboard_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dashboards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
```

- [ ] **Step 4: Add the runtime ALTER TABLE migration**

In `main.py`, the `specific_migrations` dict, update the `"agent_conversations"` entry (lines 224-226) from:

```python
            "agent_conversations": [
                ("project_id", "VARCHAR(36)"),
            ],
```

to:

```python
            "agent_conversations": [
                ("project_id", "VARCHAR(36)"),
                ("dashboard_id", "VARCHAR(36)"),
            ],
```

(This runs for both SQLite and Postgres paths in `_run_auto_migrations`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_agent_conversation_has_dashboard_id_column -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/agent_conversation.py main.py tests/test_dashboard_binding.py
git commit -m "feat(dashboards): add dashboard_id FK on agent_conversations (1:1 binding)"
```

---

### Task 2: `create_dashboard` binds the dashboard to the active conversation

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py:90-128` (`_create_dashboard`)
- Test: `backend/tests/test_dashboard_binding.py` (extend)

**Interfaces:**
- Consumes: `context["conversation_id"]` (already populated by `agents.py` per `agent_tools.py:45-48`).
- Produces: `_create_dashboard` now sets `AgentConversation.dashboard_id` when a `conversation_id` is present in context.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_binding.py`:

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    import importlib
    import app.config as config
    importlib.reload(config)
    import app.database as database
    importlib.reload(database)
    from app.database import Base, engine
    import app.models  # noqa
    Base.metadata.create_all(engine)
    # Seed: org, user, project, a DB-type KnowledgeBase.
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.agent_conversation import AgentConversation
    with database.SessionLocal() as db:
        db.add(Organization(id="o1", name="org"))
        db.add(User(id="u1", email="a@b.c", org_id="o1", role="admin", password_hash="x"))
        db.add(Project(id="p1", name="proj", org_id="o1"))
        db.add(KnowledgeBase(id="kb1", name="salesdb", org_id="o1", project_id="p1"))
        db.add(AgentConversation(id="conv1", org_id="o1", app_id="default-app"))
        db.commit()
    import app.main as mainmod
    importlib.reload(mainmod)
    return TestClient(mainmod.app)


def _auth(client):
    # Issue a session token for the seeded admin via the login endpoint.
    r = client.post("/api/auth/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_dashboard_binds_to_conversation(client, monkeypatch):
    headers = _auth(client)
    # NOTE: the tool handler is unit-tested directly (not via the agent loop)
    # because wiring a full agent turn is out of scope for this task.
    from app.services.tool_handlers.dashboard_tools import _create_dashboard
    from app.database import SessionLocal
    from app.models.agent_conversation import AgentConversation
    from app.models.dashboard import Dashboard

    args = {
        "datasource_id": "kb1",
        "title": "Sales",
        "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS revenue"}],
    }
    with SessionLocal() as db:
        result = await _create_dashboard(args, db, "u1", context={
            "org_id": "o1", "project_id": "p1", "conversation_id": "conv1",
        })
    assert result["success"]
    with SessionLocal() as db:
        conv = db.get(AgentConversation, "conv1")
        assert conv.dashboard_id == result["dashboard"]["id"]
```

Add `import asyncio` at top and wrap the await: replace the direct `await _create_dashboard(...)` call with `result = asyncio.get_event_loop().run_until_complete(_create_dashboard(...))` if pytest is sync. (Prefer making the test async with `pytest-asyncio` if available; otherwise use `asyncio.run`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_create_dashboard_binds_to_conversation -v`
Expected: FAIL — `conv.dashboard_id` is None (binding not implemented).

- [ ] **Step 3: Implement binding in `_create_dashboard`**

In `app/services/tool_handlers/dashboard_tools.py`, inside `_create_dashboard`, after `db.refresh(d)` (line 117) and before the `return` (line 118), add:

```python
    # Bind this dashboard to the active conversation (1:1). If the
    # conversation already had a bound dashboard, the new one replaces the
    # binding — the old dashboard is NOT deleted (still findable in MySpace).
    conv_id = ctx.get("conversation_id")
    if conv_id:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
        if conv is not None:
            conv.dashboard_id = d.id
            db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_create_dashboard_binds_to_conversation -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/tool_handlers/dashboard_tools.py tests/test_dashboard_binding.py
git commit -m "feat(dashboards): create_dashboard binds to active conversation"
```

---

### Task 3: `update_dashboard` resolves the target from the conversation when `dashboard_id` is omitted

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py:131-191` (`UpdateDashboardArgs`, `_update_dashboard`)
- Modify: `app/services/tool_handlers/dashboard_tools.py:276-317` (`UPDATE_DASHBOARD_SCHEMA`)
- Test: `backend/tests/test_dashboard_binding.py` (extend)

**Interfaces:**
- Produces: `UpdateDashboardArgs.dashboard_id` is now `Optional[str] = None`; `_update_dashboard` resolves it from `context["conversation_id"]` → `AgentConversation.dashboard_id` when absent. Returns a structured error when no binding exists.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_binding.py`:

```python
def test_update_dashboard_resolves_from_conversation(client):
    headers = _auth(client)
    from app.services.tool_handlers.dashboard_tools import _create_dashboard, _update_dashboard
    from app.database import SessionLocal

    create_args = {
        "datasource_id": "kb1", "title": "Sales",
        "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS revenue"}],
    }
    ctx = {"org_id": "o1", "project_id": "p1", "conversation_id": "conv1"}
    import asyncio
    with SessionLocal() as db:
        created = asyncio.get_event_loop().run_until_complete(
            _create_dashboard(create_args, db, "u1", context=ctx))
    assert created["success"]

    # Update WITHOUT passing dashboard_id — resolution should target the
    # conversation's bound dashboard.
    update_args = {"title": "Sales — Q3"}
    with SessionLocal() as db:
        updated = asyncio.get_event_loop().run_until_complete(
            _update_dashboard(update_args, db, "u1", context=ctx))
    assert updated["success"]
    assert updated["dashboard"]["name"] == "Sales — Q3"


def test_update_dashboard_without_binding_returns_structured_error(client):
    headers = _auth(client)
    from app.services.tool_handlers.dashboard_tools import _update_dashboard
    from app.database import SessionLocal
    import asyncio
    # conv1 has NO bound dashboard yet in a fresh fixture path: use a new conv.
    from app.models.agent_conversation import AgentConversation
    with SessionLocal() as db:
        db.add(AgentConversation(id="conv_empty", org_id="o1", app_id="default-app"))
        db.commit()
    with SessionLocal() as db:
        result = asyncio.get_event_loop().run_until_complete(
            _update_dashboard({"title": "x"}, db, "u1",
                              context={"org_id": "o1", "conversation_id": "conv_empty"}))
    assert not result["success"]
    assert "no dashboard" in result["error"].lower() or "create" in result["error"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_update_dashboard_resolves_from_conversation tests/test_dashboard_binding.py::test_update_dashboard_without_binding_returns_structured_error -v`
Expected: FAIL — `dashboard_id` is required (`ValidationError`), or resolution not implemented.

- [ ] **Step 3: Make `dashboard_id` optional + add resolution**

In `app/services/tool_handlers/dashboard_tools.py`, change `UpdateDashboardArgs` (line 139):

```python
    dashboard_id: Optional[str] = None
```

In `_update_dashboard`, replace the block that validates/loads the dashboard (lines 155-168) with resolution logic:

```python
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    try:
        parsed = UpdateDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid update args: {e}"}

    # Resolve the target dashboard: explicit dashboard_id wins; otherwise
    # fall back to the conversation's bound dashboard.
    target_id = parsed.dashboard_id
    if not target_id:
        conv_id = ctx.get("conversation_id")
        if conv_id:
            from app.models.agent_conversation import AgentConversation
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conv_id
            ).first()
            target_id = conv.dashboard_id if conv else None
    if not target_id:
        return {
            "success": False,
            "error": "No dashboard bound to this conversation. Call create_dashboard first, or pass dashboard_id explicitly.",
        }

    d = db.query(Dashboard).filter(
        Dashboard.id == target_id,
        Dashboard.org_id == org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        return {"success": False, "error": "Dashboard not found."}
    if d.created_by_id != user_id:
        return {"success": False, "error": "Only the dashboard creator can edit it."}
```

(The rest of `_update_dashboard` — applying fields, commit, return — stays unchanged.)

- [ ] **Step 4: Update the JSON schema so `dashboard_id` is optional**

In `UPDATE_DASHBOARD_SCHEMA` (line 292), change `"required": ["dashboard_id"]` (line 314) to `"required": []`, and update the `dashboard_id` property description (line 295) to:

```json
"description": "The id of the dashboard to edit. OPTIONAL: when omitted, the dashboard bound to the active conversation is used."
```

Also update the tool `description` (lines 280-291) — append: `"If dashboard_id is omitted, the dashboard bound to the current conversation is edited automatically."`

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py -v`
Expected: PASS (all three binding tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/tool_handlers/dashboard_tools.py tests/test_dashboard_binding.py
git commit -m "feat(dashboards): update_dashboard resolves target from conversation binding"
```

---

### Task 4: `GET /api/dashboards/by-conversation/{conversation_id}` endpoint

**Files:**
- Modify: `app/routers/dashboards.py` (add endpoint after `get_dashboard`, ~line 115)
- Test: `backend/tests/test_dashboard_binding.py` (extend)

**Interfaces:**
- Produces: `GET /api/dashboards/by-conversation/{conversation_id}` → `_with_can_edit` payload of the bound dashboard, or 404. Ownership: the conversation must belong to the requesting user's org (the `AgentConversation.org_id` wall).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_binding.py`:

```python
def test_by_conversation_returns_bound_dashboard(client):
    headers = _auth(client)
    # Create a dashboard bound to conv1 via the tool, then hit the endpoint.
    from app.services.tool_handlers.dashboard_tools import _create_dashboard
    from app.database import SessionLocal
    import asyncio
    ctx = {"org_id": "o1", "project_id": "p1", "conversation_id": "conv1"}
    with SessionLocal() as db:
        created = asyncio.get_event_loop().run_until_complete(
            _create_dashboard({"datasource_id": "kb1", "title": "S",
                "widgets": [{"id": "w1", "type": "kpi", "title": "R", "sql": "SELECT 1 AS v"}]},
                db, "u1", context=ctx))
    r = client.get("/api/dashboards/by-conversation/conv1", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"] == created["dashboard"]["id"]
    assert r.json()["can_edit"] is True


def test_by_conversation_404_when_unbound(client):
    headers = _auth(client)
    r = client.get("/api/dashboards/by-conversation/conv1", headers=headers)
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py::test_by_conversation_returns_bound_dashboard tests/test_dashboard_binding.py::test_by_conversation_404_when_unbound -v`
Expected: FAIL — 404 for the route itself (endpoint doesn't exist).

- [ ] **Step 3: Implement the endpoint**

In `app/routers/dashboards.py`, add an import at the top (after line 8):

```python
from app.models.agent_conversation import AgentConversation
```

After `get_dashboard` (line 114), add:

```python
@router.get("/by-conversation/{conversation_id}")
def get_dashboard_by_conversation(conversation_id: str,
                                  db: Session = Depends(get_db),
                                  user=Depends(get_current_user_required)):
    """Return the dashboard bound to a conversation, or 404.

    Used by the chat UI to reopen the docked panel on conversation load.
    Org-scoped via the AgentConversation.org_id wall.
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.org_id == user.org_id,
    ).first()
    if conv is None or not conv.dashboard_id:
        raise HTTPException(status_code=404, detail="No dashboard bound to this conversation")
    return _with_can_edit(_scoped(db, user, conv.dashboard_id), user)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py -v`
Expected: PASS (all binding tests).

- [ ] **Step 5: Commit**

```bash
git add app/routers/dashboards.py tests/test_dashboard_binding.py
git commit -m "feat(dashboards): GET /by-conversation/{id} to reopen bound dashboard"
```

---

## Phase B — Version Snapshots + Undo (backend)

### Task 5: `DashboardVersion` model + registration + table creation

**Files:**
- Create: `app/models/dashboard_version.py`
- Modify: `app/models/__init__.py` (add import + `__all__` entry near `Dashboard`)
- Test: `backend/tests/test_dashboard_versions.py` (created here)

**Interfaces:**
- Produces: `DashboardVersion(dashboard_id, version_number, config, source, summary)` inheriting `TimestampedBase`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_dashboard_versions.py`:

```python
"""DashboardVersion table exists with the expected columns."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import inspect as sa_inspect


def test_dashboard_versions_table(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    from app.database import Base, engine
    import app.models  # noqa
    Base.metadata.create_all(engine)
    cols = {c["name"] for c in sa_inspect(engine).get_columns("dashboard_versions")}
    assert {"dashboard_id", "version_number", "config", "source", "summary"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py::test_dashboard_versions_table -v`
Expected: FAIL — table doesn't exist.

- [ ] **Step 3: Create the model**

Create `app/models/dashboard_version.py`:

```python
"""DashboardVersion — lightweight config snapshots enabling undo of edits."""
from typing import Optional

from sqlalchemy import String, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class DashboardVersion(TimestampedBase):
    __tablename__ = "dashboard_versions"

    dashboard_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full snapshot of the dashboard definition + scalar fields at the time
    # this version was superseded: {"name","description","refresh_interval_seconds","definition"}.
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # agent|manual|undo
    summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
```

- [ ] **Step 4: Register the model**

In `app/models/__init__.py`, add after the `Dashboard` import (there is no explicit `from app.models.dashboard import Dashboard` line currently — `Dashboard` is exported via `__all__` at line 128 but not imported at top; add both). After line 61 (`from app.models.hook_rule import HookRule`), add:

```python
from app.models.dashboard import Dashboard
from app.models.dashboard_version import DashboardVersion
```

And in `__all__`, replace the standalone `"Dashboard",` (line 128) with:

```python
    "Dashboard",
    "DashboardVersion",
```

(Remove the now-duplicate bare `"Dashboard"` if it was the only reference — ensure exactly one `"Dashboard"` and one `"DashboardVersion"` remain.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py::test_dashboard_versions_table -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/dashboard_version.py app/models/__init__.py tests/test_dashboard_versions.py
git commit -m "feat(dashboards): DashboardVersion model for edit-undo snapshots"
```

---

### Task 6: Snapshot-on-edit helper + push versions in `update_dashboard` and `PATCH`

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py` (`_update_dashboard`)
- Modify: `app/routers/dashboards.py` (`update_dashboard` PATCH, ~line 139)
- Create: a small helper inline in each (or a shared function in `dashboard_tools.py`)
- Test: `backend/tests/test_dashboard_versions.py` (extend)

**Interfaces:**
- Produces: a module-level `def _push_version(db, dashboard, source, summary=None) -> None` in `dashboard_tools.py` that snapshots the CURRENT config (before the edit is applied) as the next `version_number`, capped at 20. Both the tool `_update_dashboard` and the router `PATCH update_dashboard` call it BEFORE mutating.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_versions.py`:

```python
def test_update_dashboard_pushes_prior_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    import importlib
    import app.config as config; importlib.reload(config)
    import app.database as database; importlib.reload(database)
    from app.database import Base, engine, SessionLocal
    import app.models  # noqa
    Base.metadata.create_all(engine)
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.dashboard import Dashboard
    from app.models.dashboard_version import DashboardVersion
    from app.services.tool_handlers.dashboard_tools import _create_dashboard, _update_dashboard
    import asyncio
    with SessionLocal() as db:
        db.add(Organization(id="o1", name="o")); db.add(User(id="u1", email="a@b.c", org_id="o1", role="admin", password_hash="x"))
        db.add(Project(id="p1", name="p", org_id="o1")); db.add(KnowledgeBase(id="kb1", name="k", org_id="o1", project_id="p1"))
        db.commit()
        ctx = {"org_id": "o1", "project_id": "p1"}
        created = asyncio.get_event_loop().run_until_complete(
            _create_dashboard({"datasource_id": "kb1", "title": "S",
                "widgets": [{"id": "w1", "type": "kpi", "title": "R", "sql": "SELECT 1 AS v"}]},
                db, "u1", context=ctx))
        did = created["dashboard"]["id"]
        # First edit -> version 1 (the initial config) is pushed.
        asyncio.get_event_loop().run_until_complete(
            _update_dashboard({"title": "S2"}, db, "u1", context=ctx | {"conversation_id": None}))
        versions = db.query(DashboardVersion).filter_by(dashboard_id=did).order_by(DashboardVersion.version_number).all()
        assert len(versions) == 1
        assert versions[0].config["name"] == "S"  # the PRIOR (pre-edit) title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py::test_update_dashboard_pushes_prior_version -v`
Expected: FAIL — no versions pushed.

- [ ] **Step 3: Implement `_push_version` + call it in `_update_dashboard`**

In `app/services/tool_handlers/dashboard_tools.py`, near the top (after the `ALLOWED_WIDGET_TYPES` set, ~line 24), add:

```python
MAX_VERSIONS = 20


def _push_version(db, dashboard, source: str, summary: str | None = None) -> None:
    """Snapshot the dashboard's CURRENT config as a new version (BEFORE an edit).

    Called right before mutating the dashboard. The snapshot captures the
    pre-edit state so a later undo restores it. History is capped at
    MAX_VERSIONS per dashboard (oldest pruned).
    """
    from app.models.dashboard_version import DashboardVersion
    last = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == dashboard.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    next_no = (last.version_number + 1) if last else 1
    db.add(DashboardVersion(
        dashboard_id=dashboard.id,
        version_number=next_no,
        config={
            "name": dashboard.name,
            "description": dashboard.description,
            "refresh_interval_seconds": dashboard.refresh_interval_seconds,
            "definition": dashboard.definition,
        },
        source=source,
        summary=summary,
    ))
    # Prune oldest beyond cap.
    all_v = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == dashboard.id
    ).order_by(DashboardVersion.version_number.desc()).all()
    for old in all_v[MAX_VERSIONS:]:
        db.delete(old)
```

Then in `_update_dashboard`, immediately AFTER the ownership check (`if d.created_by_id != user_id: ...`) and BEFORE applying any field, add:

```python
    _push_version(db, d, source="agent", summary=args.get("_summary"))
```

Wait — `args` is the raw dict; better to derive a summary from the parsed fields. Replace that line with:

```python
    _push_version(db, d, source="agent",
                  summary="agent edit" if parsed.widgets else (parsed.title or "agent edit"))
```

- [ ] **Step 4: Call `_push_version` in the router PATCH handler too**

In `app/routers/dashboards.py`, import the helper at the top:

```python
from app.services.tool_handlers.dashboard_tools import _push_version
```

In `update_dashboard` (the PATCH, ~line 139), immediately after the creator check (`if d.created_by_id != user.id: raise ...`) and BEFORE applying fields, add:

```python
    _push_version(db, d, source="manual", summary="manual edit")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/tool_handlers/dashboard_tools.py app/routers/dashboards.py tests/test_dashboard_versions.py
git commit -m "feat(dashboards): push prior-config version snapshot on every edit"
```

---

### Task 7: `POST /api/dashboards/{id}/undo` endpoint + `can_undo` flag + `undo_dashboard_edit` tool

**Files:**
- Modify: `app/routers/dashboards.py` (add `POST /{id}/undo`; extend `_with_can_edit`)
- Modify: `app/services/tool_handlers/dashboard_tools.py` (add `_undo_dashboard_edit` + schema/registration)
- Test: `backend/tests/test_dashboard_versions.py` (extend)

**Interfaces:**
- Produces:
  - `_with_can_edit` payload now includes `can_undo: bool`.
  - `POST /api/dashboards/{id}/undo` (creator-only) → restored `_with_can_edit` payload; 409 if no history.
  - Agent tool `undo_dashboard_edit(conversation_id?)` → resolves bound dashboard (like update), pops the latest version, restores config, records a `source="undo"` version.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_versions.py`:

```python
def test_undo_restores_prior_config_and_records_undo_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    import importlib
    import app.config as config; importlib.reload(config)
    import app.database as database; importlib.reload(database)
    from app.database import Base, engine, SessionLocal
    import app.models  # noqa
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    import asyncio
    with SessionLocal() as db:
        db.add(Organization(id="o1", name="o")); db.add(User(id="u1", email="a@b.c", org_id="o1", role="admin", password_hash="x"))
        db.add(Project(id="p1", name="p", org_id="o1")); db.add(KnowledgeBase(id="kb1", name="k", org_id="o1", project_id="p1"))
        db.commit()
    from app.services.tool_handlers.dashboard_tools import _create_dashboard, _update_dashboard, _undo_dashboard_edit
    ctx = {"org_id": "o1", "project_id": "p1"}
    with SessionLocal() as db:
        created = asyncio.get_event_loop().run_until_complete(
            _create_dashboard({"datasource_id": "kb1", "title": "Original",
                "widgets": [{"id": "w1", "type": "kpi", "title": "R", "sql": "SELECT 1 AS v"}]},
                db, "u1", context=ctx))
        did = created["dashboard"]["id"]
        asyncio.get_event_loop().run_until_complete(
            _update_dashboard({"title": "Changed"}, db, "u1", context=ctx))
        # Undo -> title reverts to "Original".
        undone = asyncio.get_event_loop().run_until_complete(
            _undo_dashboard_edit({}, db, "u1", context={**ctx, "conversation_id": None, "dashboard_id": did}))
    assert undone["success"]
    assert undone["dashboard"]["name"] == "Original"
    # An undo itself records a version (source="undo") so it can be redone.
    from app.models.dashboard_version import DashboardVersion
    with SessionLocal() as db:
        undo_v = db.query(DashboardVersion).filter_by(dashboard_id=did, source="undo").all()
        assert len(undo_v) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py::test_undo_restores_prior_config_and_records_undo_version -v`
Expected: FAIL — `_undo_dashboard_edit` doesn't exist.

- [ ] **Step 3: Add `can_undo` to `_with_can_edit`**

In `app/routers/dashboards.py`, extend `_with_can_edit` (lines 65-75) — after `data["can_edit"] = ...`, add:

```python
    from app.models.dashboard_version import DashboardVersion
    can_undo = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).count() > 0
    data["can_undo"] = can_undo
    return data
```

(Add a `db: Session` param to `_with_can_edit` if not already present — it currently takes `(d, user)`; change signature to `_with_can_edit(db, d, user)` and update all call sites: `create_dashboard`, `list_dashboards`, `get_dashboard`, `update_dashboard`, `get_dashboard_by_conversation`.)

- [ ] **Step 4: Add `POST /{id}/undo` endpoint**

In `app/routers/dashboards.py`, after `preview_widget_sql` (end of file), add:

```python
@router.post("/{dashboard_id}/undo")
def undo_dashboard(dashboard_id: str,
                   db: Session = Depends(get_db),
                   user=Depends(get_current_user_required)):
    """Restore the dashboard's config to its state before the last edit.

    Pops the latest DashboardVersion, applies its config, and records a new
    version with source='undo' (so the undo itself can be reverted = redo).
    Creator-only. 409 when there is no version history.
    """
    from app.models.dashboard_version import DashboardVersion
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can undo edits on this dashboard")
    latest = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    if latest is None:
        raise HTTPException(status_code=409, detail="No edit history to undo")
    # Snapshot the CURRENT (pre-undo) state as an "undo" version first.
    from app.services.tool_handlers.dashboard_tools import _push_version
    _push_version(db, d, source="undo", summary="undo")
    cfg = latest.config
    d.name = cfg.get("name", d.name)
    d.description = cfg.get("description", d.description)
    d.refresh_interval_seconds = cfg.get("refresh_interval_seconds", d.refresh_interval_seconds)
    d.definition = cfg.get("definition", d.definition)
    db.delete(latest)  # the restored version is consumed
    db.commit()
    db.refresh(d)
    return _with_can_edit(db, d, user)
```

- [ ] **Step 5: Add the `undo_dashboard_edit` agent tool**

In `app/services/tool_handlers/dashboard_tools.py`, after the `update_dashboard` registration block (end of file), add:

```python
class UndoDashboardEditArgs(BaseModel):
    dashboard_id: Optional[str] = None  # optional: resolves from conversation


async def _undo_dashboard_edit(args: dict, db, user_id, context: dict | None = None) -> dict:
    """Undo the last edit on the conversation's bound dashboard (or explicit id)."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    try:
        parsed = UndoDashboardEditArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid undo args: {e}"}

    target_id = parsed.dashboard_id
    if not target_id:
        conv_id = ctx.get("conversation_id")
        if conv_id:
            from app.models.agent_conversation import AgentConversation
            conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
            target_id = conv.dashboard_id if conv else None
    if not target_id:
        return {"success": False, "error": "No dashboard bound to this conversation to undo."}

    from app.models.dashboard import Dashboard
    from app.models.dashboard_version import DashboardVersion
    d = db.query(Dashboard).filter(
        Dashboard.id == target_id, Dashboard.org_id == org_id, Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        return {"success": False, "error": "Dashboard not found."}
    if d.created_by_id != user_id:
        return {"success": False, "error": "Only the dashboard creator can undo edits."}
    latest = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    if latest is None:
        return {"success": False, "error": "No edit history to undo."}
    _push_version(db, d, source="undo", summary="undo")
    cfg = latest.config
    d.name = cfg.get("name", d.name)
    d.description = cfg.get("description", d.description)
    d.refresh_interval_seconds = cfg.get("refresh_interval_seconds", d.refresh_interval_seconds)
    d.definition = cfg.get("definition", d.definition)
    db.delete(latest)
    db.commit()
    db.refresh(d)
    return {
        "success": True,
        "dashboard": d.to_dict(),
        "artifact": {
            "source": "dashboard",
            "dashboard_id": d.id,
            "title": d.name,
            "widget_count": len((d.definition or {}).get("widgets", [])),
            "undone": True,
        },
    }


UNDO_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "undo_dashboard_edit",
        "description": (
            "Undo the last edit made to the dashboard bound to the current "
            "conversation (or an explicit dashboard_id). Restores the previous "
            "widget list, title, and layout. Use when the user says 'undo "
            "that' or 'revert'. Can be called repeatedly to step back through "
            "edit history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"type": "string", "description": "Optional. Defaults to the conversation's bound dashboard."},
            },
            "required": [],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="undo_dashboard_edit",
        schema=UNDO_DASHBOARD_SCHEMA,
        handler=_undo_dashboard_edit,
        category="dashboards",
        toolset="dashboards",
        description="Undo the last edit on the bound dashboard.",
        emoji="↶",
        max_result_size_chars=2_000,
    )
except Exception:
    logger.exception("Failed to register undo_dashboard_edit tool")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_versions.py tests/test_dashboard_binding.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routers/dashboards.py app/services/tool_handlers/dashboard_tools.py tests/test_dashboard_versions.py
git commit -m "feat(dashboards): undo endpoint + undo_dashboard_edit tool + can_undo flag"
```

---

## Phase C — Persistent Docked Panel (frontend)

### Task 8: Add `getDashboardByConversation` + `undoDashboardEdit` to the API client

**Files:**
- Modify: `src/api/dashboards.js` (append)
- Test: none (trivial wrapper; covered by panel tests)

**Interfaces:**
- Produces: `getDashboardByConversation(conversationId)`, `undoDashboardEdit(id)`.

- [ ] **Step 1: Add the two functions**

Append to `src/api/dashboards.js`:

```javascript
export function getDashboardByConversation(conversationId) {
  return authFetch(`${BASE}/by-conversation/${conversationId}`, { method: 'GET' }).then(j);
}

export function undoDashboardEdit(id) {
  return authFetch(`${BASE}/${id}/undo`, { method: 'POST' }).then(j);
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/api/dashboards.js
git commit -m "feat(dashboards): add getDashboardByConversation + undoDashboardEdit client fns"
```

---

### Task 9: Expose `refresh` from `DashboardViewer` via `forwardRef` + `useImperativeHandle`

**Files:**
- Modify: `src/components/dashboard/DashboardViewer.jsx:22` (signature) + add handle
- Test: `src/components/dashboard/DashboardViewer.test.jsx` (extend, if exists; else minimal new)

**Interfaces:**
- Produces: `DashboardViewer` is now a `forwardRef` exposing `{ refresh }` (calls the internal `refresh(undefined, { force: true })`). Props/signature otherwise unchanged. Polling/`mountedRef`/`visibilitychange` invariants untouched.

- [ ] **Step 1: Write the failing test**

Create `src/components/dashboard/DashboardViewer.ref.test.jsx`:

```jsx
import { render, screen, act } from '@testing/vitest-react';
import { vi } from 'vitest';
import DashboardViewer from './DashboardViewer';

vi.mock('@/api/dashboards', () => ({
  queryDashboard: vi.fn().mockResolvedValue({ results: {}, refreshed_at: '2026-07-30T00:00:00Z' }),
  updateDashboard: vi.fn(),
  createDashboard: vi.fn(),
}));

describe('DashboardViewer imperative ref', () => {
  it('exposes refresh() via ref', async () => {
    const { queryDashboard } = await import('@/api/dashboards');
    const ref = { current: null };
    const dash = { id: 'd1', name: 'D', definition: { widgets: [] }, refresh_interval_seconds: 30 };
    render(<DashboardViewer ref={ref} dashboard={dash} polling={false} />);
    await act(async () => { /* allow mount refresh */ });
    expect(queryDashboard).toHaveBeenCalledTimes(1);
    await act(async () => { ref.current.refresh(); });
    expect(queryDashboard).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.ref.test.jsx`
Expected: FAIL — `DashboardViewer` doesn't accept a `ref` (not a forwardRef).

- [ ] **Step 3: Convert to forwardRef + expose handle**

In `src/components/dashboard/DashboardViewer.jsx`, change the signature (line 22) from:

```javascript
export default function DashboardViewer({ dashboard, polling = true, initialDateRange, initialFilters, canEdit = false, onSaved, onCopied }) {
```

to:

```javascript
const DashboardViewer = forwardRef(function DashboardViewer({ dashboard, polling = true, initialDateRange, initialFilters, canEdit = false, onSaved, onCopied }, ref) {
```

Add `forwardRef` to the React import on line 1:

```javascript
import { useEffect, useRef, useState, useCallback, forwardRef, useImperativeHandle } from 'react';
```

After the `refresh` callback definition (after line 96), add:

```javascript
  // Expose an imperative refresh so a parent (docked panel) can trigger an
  // immediate re-query when the agent edits the dashboard, bypassing the poll
  // interval. Does NOT touch the polling/mountedRef/visibilitychange loop.
  useImperativeHandle(ref, () => ({
    refresh: () => refresh(undefined, { force: true }),
  }), [refresh]);
```

At the end of the component (the final `}` before the `Widget` function, ~line 360), change `}` to `});` to close the forwardRef.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.ref.test.jsx src/components/dashboard/DashboardPopup.test.jsx`
Expected: PASS (ref test + existing popup test still passes — `DashboardPopup` renders `DashboardViewer` without a ref, which is fine).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/DashboardViewer.ref.test.jsx
git commit -m "feat(dashboards): expose DashboardViewer.refresh via forwardRef imperative handle"
```

---

### Task 10: Create `DashboardDockPanel` (persistent, resizable, auto-opens bound dashboard)

**Files:**
- Create: `src/components/dashboard/DashboardDockPanel.jsx`
- Create: `src/components/dashboard/DashboardDockPanel.test.jsx`
- Test: `src/components/dashboard/DashboardDockPanel.test.jsx`

**Interfaces:**
- Consumes: `getDashboard`, `getDashboardByConversation`, `undoDashboardEdit` from `@/api/dashboards`; `DashboardViewer` (forwardRef).
- Produces: `<DashboardDockPanel conversationId={...} dashboardId={...} onClose={...} viewerRef={...} />` — a resizable right-docked panel rendering `DashboardViewer`. Props:
  - `conversationId` (optional): on mount, calls `getDashboardByConversation` to auto-open the bound dashboard.
  - `dashboardId` (optional): if set, fetches that dashboard (explicit open from a `DashboardCard`).
  - `onClose`: close handler.
  - `viewerRef`: a ref forwarded into `DashboardViewer` so the parent can call `refresh()`.
  - exposes a header with Edit/Undo/Close + a drag-resize handle.

- [ ] **Step 1: Write the failing test**

Create `src/components/dashboard/DashboardDockPanel.test.jsx`:

```jsx
import { render, screen, waitFor, act } from '@testing/vitest-react';
import { vi } from 'vitest';
import DashboardDockPanel from './DashboardDockPanel';

vi.mock('@/api/dashboards', () => ({
  getDashboard: vi.fn().mockResolvedValue({ id: 'd1', name: 'Sales', can_edit: true, can_undo: false, definition: { widgets: [] }, refresh_interval_seconds: 30 }),
  getDashboardByConversation: vi.fn().mockResolvedValue({ id: 'd1', name: 'Sales', can_edit: true, can_undo: false, definition: { widgets: [] }, refresh_interval_seconds: 30 }),
  undoDashboardEdit: vi.fn().mockResolvedValue({ id: 'd1', name: 'Sales', can_undo: false }),
  queryDashboard: vi.fn().mockResolvedValue({ results: {}, refreshed_at: '2026-07-30T00:00:00Z' }),
  updateDashboard: vi.fn(),
  createDashboard: vi.fn(),
}));

describe('DashboardDockPanel', () => {
  it('auto-opens the bound dashboard by conversation id', async () => {
    const { getDashboardByConversation } = await import('@/api/dashboards');
    render(<DashboardDockPanel conversationId="conv1" onClose={() => {}} />);
    await waitFor(() => expect(getDashboardByConversation).toHaveBeenCalledWith('conv1'));
    await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  });

  it('shows an Undo button enabled only when can_undo', async () => {
    const { getDashboardByConversation } = await import('@/api/dashboards');
    getDashboardByConversation.mockResolvedValueOnce({ id: 'd1', name: 'Sales', can_edit: true, can_undo: true, definition: { widgets: [] }, refresh_interval_seconds: 30 });
    render(<DashboardDockPanel conversationId="conv1" onClose={() => {}} />);
    const undo = await screen.findByRole('button', { name: /undo/i });
    expect(undo).not.toBeDisabled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardDockPanel.test.jsx`
Expected: FAIL — component doesn't exist.

- [ ] **Step 3: Implement `DashboardDockPanel`**

Create `src/components/dashboard/DashboardDockPanel.jsx`:

```jsx
import { useEffect, useRef, useState, useCallback } from 'react';
import { X, Undo2, Pencil } from 'lucide-react';
import { getDashboard, getDashboardByConversation, undoDashboardEdit } from '@/api/dashboards';
import DashboardViewer from './DashboardViewer';

/**
 * Persistent, resizable docked live-preview panel for the conversational
 * dashboard builder. Stays mounted across chat messages (unlike the old
 * per-message DashboardPopup). Auto-opens the conversation's bound dashboard.
 *
 * The parent passes `viewerRef` so it can call `viewerRef.current.refresh()`
 * when an agent edit lands in the stream (see Chat.jsx watcher).
 */
export default function DashboardDockPanel({ conversationId, dashboardId, onClose, viewerRef }) {
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState(null);
  const [width, setWidth] = useState(560);
  const localRef = useRef(null);
  const ref = viewerRef || localRef;

  const load = useCallback(async () => {
    setError(null);
    try {
      let d;
      if (dashboardId) d = await getDashboard(dashboardId);
      else if (conversationId) d = await getDashboardByConversation(conversationId);
      if (d) setDashboard(d);
      else { setDashboard(null); }
    } catch (e) {
      // 404 (no binding) is not fatal — panel simply stays empty.
      if (!String(e).includes('404')) setError(String(e));
      setDashboard(null);
    }
  }, [dashboardId, conversationId]);

  useEffect(() => { load(); }, [load]);

  // Re-load when dashboardId changes (e.g. agent created a new one).
  useEffect(() => { load(); }, [dashboardId]);

  const handleUndo = useCallback(async () => {
    if (!dashboard?.id) return;
    try {
      const restored = await undoDashboardEdit(dashboard.id);
      setDashboard(restored);
      ref.current?.refresh?.();
    } catch (e) { setError(String(e)); }
  }, [dashboard, ref]);

  // Drag-to-resize handle (right edge). Min 360, max 900.
  const onResize = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    const move = (ev) => {
      const next = Math.max(360, Math.min(900, startW + (startX - ev.clientX)));
      setWidth(next);
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  }, [width]);

  return (
    <div className="flex h-full flex-col border-l border-border bg-background" style={{ width }}>
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="truncate text-sm font-medium">{dashboard?.name || 'Dashboard'}</span>
        <div className="flex items-center gap-1">
          {dashboard?.can_edit && (
            <button onClick={handleUndo} disabled={!dashboard?.can_undo}
              aria-label="Undo last edit" title="Undo last edit"
              className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-40">
              <Undo2 className="h-3 w-3" />Undo
            </button>
          )}
          <button onClick={onClose} aria-label="Close panel" title="Close"
            className="rounded-md border border-border p-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div className="relative flex-1 overflow-hidden">
        {error && <div className="p-3 text-xs text-destructive">{error}</div>}
        {dashboard ? (
          <DashboardViewer ref={ref} dashboard={dashboard} canEdit={dashboard.can_edit}
            onSaved={setDashboard} />
        ) : (
          !error && <div className="p-4 text-sm text-muted-foreground">No dashboard yet — ask the agent to build one.</div>
        )}
      </div>
      {/* resize handle on the left edge */}
      <div onMouseDown={onResize}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-primary/30" />
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardDockPanel.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/DashboardDockPanel.jsx frontend/src/components/dashboard/DashboardDockPanel.test.jsx
git commit -m "feat(dashboards): DashboardDockPanel persistent resizable live-preview panel"
```

---

### Task 11: Lift dashboard panel state out of `MessageBubble` into `Chat`; mount `DashboardDockPanel`

**Files:**
- Modify: `src/pages/Chat.jsx` (add docked-panel state + mount)
- Modify: `src/components/chat/MessageBubble.jsx:349,519-541` (remove local `openDashboardId`/`DashboardPopup`; accept `onOpenDashboard` prop)
- Test: manual + existing Chat tests must still pass

**Interfaces:**
- Consumes: `DashboardDockPanel`, `getDashboardByConversation`.
- Produces: `Chat` owns `dockedDashboardId` + `dockedOpen` state and a `viewerRef`; passes `onOpenDashboard(id)` down to `MessageBubble` (replacing its internal handler). `MessageBubble` no longer renders `DashboardPopup`.

- [ ] **Step 1: Add state + mount to `Chat.jsx`**

In `src/pages/Chat.jsx`, near the other `useState` declarations (around line 169-170), add:

```javascript
  const [dockedDashboardId, setDockedDashboardId] = useState(null);
  const [dockedOpen, setDockedOpen] = useState(false);
  const dashboardViewerRef = useRef(null);
```

Add `useRef` to the React import if not already imported. Add an import near the top:

```javascript
import DashboardDockPanel from '@/components/dashboard/DashboardDockPanel';
```

Define an open handler (near other handlers):

```javascript
  const openDashboard = useCallback((id) => {
    setDockedDashboardId(id);
    setDockedOpen(true);
  }, []);
  const closeDashboard = useCallback(() => {
    setDockedOpen(false);
    setDockedDashboardId(null);
  }, []);
```

Pass `onOpenDashboard={openDashboard}` to every `<MessageBubble ... />` render site in `Chat.jsx` (search for `<MessageBubble` and add the prop).

Finally, render the docked panel alongside the chat layout. Find the main chat layout container (the flex row that holds the message list). Add the panel as a sibling that shows when `dockedOpen`:

```jsx
{dockedOpen && (
  // The parent chat layout is a flex row; this panel is a flex child that
  // fills the available height (h-full) and sets its own width internally.
  <div className="h-full shrink-0">
    <DashboardDockPanel
      dashboardId={dockedDashboardId}
      conversationId={activeId || streamingConvId}
      onClose={closeDashboard}
      viewerRef={dashboardViewerRef}
    />
  </div>
)}
```

(Use the active conversation id available in `Chat.jsx` — `activeId` / the `?conv=` id / `streamingConvId`; pick the variable that holds the current `AgentConversation` id. If the chat uses a `ChatSession` id distinct from the `AgentConversation` id, use the conversation id that the backend bound — verify by checking which id `streamAgentResponse` / `createAgentConversation` returns.)

- [ ] **Step 2: Remove the per-message popup from `MessageBubble.jsx`**

In `src/components/chat/MessageBubble.jsx`:
- Remove the `import DashboardPopup from '@/components/dashboard/DashboardPopup';` (line 18).
- Remove the `const [openDashboardId, setOpenDashboardId] = useState(null);` (line 349).
- Change the `DashboardCard` `onOpen` (line 523) from `() => setOpenDashboardId(artifact.dashboard_id)` to `() => onOpenDashboard?.(artifact.dashboard_id)`.
- Add `onOpenDashboard` to the component's destructured props.
- Remove the entire `{openDashboardId && (<DashboardPopup ... />)}` block (lines 535-541).

- [ ] **Step 3: Run existing Chat tests to verify nothing broke**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/Chat.stopButton.test.jsx src/components/chat/MessageBubble`
Expected: PASS (adjust any test that asserted on the removed internal popup state — none expected, since the popup was internal).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/Chat.jsx frontend/src/components/chat/MessageBubble.jsx
git commit -m "feat(dashboards): lift dashboard panel to Chat; mount persistent docked panel"
```

---

## Phase D — Live Re-render on Agent Edits + Auto-open (frontend)

### Task 12: Watch the message stream for dashboard tool results → trigger refetch + auto-open

**Files:**
- Modify: `src/pages/Chat.jsx` (add a `useEffect` watching `messages`)

**Interfaces:**
- Consumes: `messages` state in `Chat`; `dashboardViewerRef`; `getDashboard` from `@/api/dashboards`.
- Produces: when a new assistant message contains a tool result for `create_dashboard` / `update_dashboard` / `undo_dashboard_edit`, the docked panel (a) auto-opens if closed (on create) and (b) refetches the dashboard object + calls `dashboardViewerRef.current.refresh()`.

- [ ] **Step 1: Write the failing test**

Add to `src/components/dashboard/DashboardDockPanel.test.jsx` (or a focused `Chat` integration test). A minimal, reliable unit test: extract the watcher into a pure helper and test it. Create `src/pages/chatDashboardWatcher.js`:

```javascript
// Pure helper: given the latest assistant message, returns the dashboard tool
// event to act on (or null). Exported for unit testing.
export function detectDashboardEvent(message) {
  if (!message || message.role !== 'assistant') return null;
  const tcs = message.tool_calls || [];
  for (const tc of tcs) {
    const name = tc.name || tc.tool_name;
    if (name === 'create_dashboard' || name === 'update_dashboard' || name === 'undo_dashboard_edit') {
      // Only act on successful results (the tool result block).
      const ok = tc.results?.success !== false && tc.error == null;
      if (!ok) return null;
      const dashId = tc.results?.dashboard?.id || tc.results?.artifact?.dashboard_id || tc.results?.dashboard_id;
      return { tool: name, dashboardId: dashId || null };
    }
  }
  // Also scan artifact blocks embedded in the message (create_dashboard emits one).
  const artifacts = message.artifacts || [];
  for (const a of artifacts) {
    if (a.source === 'dashboard' && a.dashboard_id) {
      return { tool: 'create_dashboard', dashboardId: a.dashboard_id };
    }
  }
  return null;
}
```

Create `src/pages/chatDashboardWatcher.test.js`:

```javascript
import { describe, it, expect } from 'vitest';
import { detectDashboardEvent } from './chatDashboardWatcher';

describe('detectDashboardEvent', () => {
  it('detects a successful update_dashboard tool result', () => {
    const msg = { role: 'assistant', tool_calls: [{ name: 'update_dashboard', results: { success: true, dashboard: { id: 'd9' } } }] };
    expect(detectDashboardEvent(msg)).toEqual({ tool: 'update_dashboard', dashboardId: 'd9' });
  });
  it('detects a create_dashboard artifact', () => {
    const msg = { role: 'assistant', artifacts: [{ source: 'dashboard', dashboard_id: 'd2' }] };
    expect(detectDashboardEvent(msg)).toEqual({ tool: 'create_dashboard', dashboardId: 'd2' });
  });
  it('ignores failed tool calls', () => {
    const msg = { role: 'assistant', tool_calls: [{ name: 'update_dashboard', results: { success: false, error: 'x' } }] };
    expect(detectDashboardEvent(msg)).toBeNull();
  });
  it('ignores user messages', () => {
    expect(detectDashboardEvent({ role: 'user' })).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/chatDashboardWatcher.test.js`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create the helper (code above in Step 1)**

Create `src/pages/chatDashboardWatcher.js` with the code from Step 1.

- [ ] **Step 4: Run the helper test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/chatDashboardWatcher.test.js`
Expected: PASS.

- [ ] **Step 5: Wire the watcher into `Chat.jsx`**

In `src/pages/Chat.jsx`, import the helper:

```javascript
import { detectDashboardEvent } from './chatDashboardWatcher';
import { getDashboard } from '@/api/dashboards';
```

Add a `useEffect` that runs on `messages` change, inspecting only the LAST message once (track the last processed message id in a ref to avoid re-firing):

```javascript
  const lastProcessedMsgRef = useRef(null);
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last || last.id === lastProcessedMsgRef.current) return;
    lastProcessedMsgRef.current = last.id;
    const evt = detectDashboardEvent(last);
    if (!evt) return;
    // create_dashboard: ensure the panel is open to this dashboard.
    if (evt.tool === 'create_dashboard' && evt.dashboardId) {
      setDockedDashboardId(evt.dashboardId);
      setDockedOpen(true);
    }
    // For all dashboard tools: refetch the (possibly new) dashboard object
    // so the viewer re-renders widgets/layout, then force a data refresh.
    const targetId = evt.dashboardId || dockedDashboardId;
    if (targetId) {
      getDashboard(targetId)
        .then((d) => {
          // Update the docked panel's dashboard by re-keying it onto state.
          // DashboardDockPanel reloads on dashboardId change; bump it.
          setDockedDashboardId(d.id);
          // give the viewer a tick to mount the new dashboard, then refresh data
          setTimeout(() => dashboardViewerRef.current?.refresh?.(), 0);
        })
        .catch(() => { /* best-effort; polling will catch up */ });
    }
  }, [messages, dockedDashboardId]);
```

- [ ] **Step 6: Run the full dashboard frontend test suite (explicit paths only)**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard src/pages/chatDashboardWatcher.test.js src/pages/Chat.stopButton.test.jsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/chatDashboardWatcher.js frontend/src/pages/chatDashboardWatcher.test.js frontend/src/pages/Chat.jsx
git commit -m "feat(dashboards): live re-render docked panel on agent create/update/undo events"
```

---

### Task 13: End-to-end smoke test (live, vs Postgres)

**Files:**
- Create: `backend/tests/test_dashboard_conversational_e2e.py`

**Goal:** Verify the full loop against the running stack: create (binds) → update (resolves from conversation) → undo → by-conversation reopens.

- [ ] **Step 1: Write the E2E test**

Create `backend/tests/test_dashboard_conversational_e2e.py` (modeled on `test_dashboard_e2e_edit_save.py`). Use `E2E_ADMIN_PASS=admin123` and `admin@zhanlu.dev`. The test:

1. Logs in as admin.
2. Creates an `AgentConversation` (or uses the chat send endpoint to start one) and captures its id.
3. Calls the agent tools directly via the internal handlers (or via an authenticated tool-execute endpoint if one exists) with `conversation_id` set: `create_dashboard` → assert `agent_conversation.dashboard_id` set.
4. Calls `update_dashboard` WITHOUT `dashboard_id` (resolution) → assert title changed.
5. Calls `undo_dashboard_edit` → assert title reverted.
6. `GET /api/dashboards/by-conversation/{id}` → 200, returns the dashboard.

If no authenticated tool-execute endpoint exists, drive the tools through the internal handlers with a `SessionLocal` and the admin user id (mirroring the unit-test fixtures), then assert via the HTTP endpoints for `by-conversation` and `GET /{id}`.

- [ ] **Step 2: Run the E2E test**

Run: `cd /root/zhanlu/backend && E2E_ADMIN_PASS=admin123 E2E_ADMIN_EMAIL=admin@zhanlu.dev python3 -m pytest tests/test_dashboard_conversational_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_dashboard_conversational_e2e.py
git commit -m "test(dashboards): conversational builder e2e (create->update->undo->reopen)"
```

---

## Final Verification

- [ ] **Backend:** `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_binding.py tests/test_dashboard_versions.py tests/test_dashboard_query.py -v` (explicit paths; no `-n`).
- [ ] **Frontend:** `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard src/pages/chatDashboardWatcher.test.js src/pages/Chat.stopButton.test.jsx`
- [ ] **Deploy:** `docker restart zhanlu-backend` then `/opt/rebuild-zhanlu.sh --force`.
- [ ] **Manual smoke:** In the chat, ask the agent to build a dashboard → docked panel auto-opens → ask to change a widget → panel re-renders instantly → click Undo → reverts → close chat, reopen → panel auto-opens the bound dashboard.

## Notes for the implementer

- The `conversation_id` is ALREADY in the tool `context` dict (`agent_tools.py:45-48, 485`) — do NOT add any new context-wiring; just read `ctx.get("conversation_id")` in the dashboard handlers.
- `_with_can_edit` currently takes `(d, user)`; Task 7 changes it to `(db, d, user)` — update ALL call sites in `dashboards.py` in the same edit.
- `DashboardViewer`'s polling/`mountedRef`/`visibilitychange` logic is load-bearing — the `forwardRef` change only ADDS `useImperativeHandle`; do not touch the `useEffect` at lines 108-126.
- If `Chat.jsx`'s active conversation id variable isn't obviously `activeId`/`streamingConvId`, grep for `createAgentConversation` / `?conv=` handling to find the `AgentConversation` id (distinct from the `ChatSession` id) before wiring `conversationId` into `DashboardDockPanel`.
- `pytest-asyncio` may not be installed; the backend tests use `asyncio.get_event_loop().run_until_complete(...)` for the async tool handlers to avoid that dependency. If `pytest-asyncio` IS available, prefer `async def test_...` + `@pytest.mark.asyncio`.
