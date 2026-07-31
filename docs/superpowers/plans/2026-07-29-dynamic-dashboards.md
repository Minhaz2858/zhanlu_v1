# Dynamic Live Dashboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent build a live (auto-polling) database dashboard via a skill + tool, persist it, and render it as a right-side chat popup and a My Space same-page popup, with real-time data from the project's bound datasource.

**Architecture:** A new first-class `Dashboard` entity binds to ONE database `KnowledgeBase`. A `POST /api/dashboards/{id}/query` endpoint reuses the existing `QueryService` (read-only, row-capped, timeout-bounded connectors) to run all widget SQLs in one round-trip with per-widget error isolation. A schema-driven React `DashboardViewer` (recharts) calls that endpoint via `authFetch` and auto-polls. The agent builds dashboards through a `create_dashboard` tool (upfront pydantic validation + read-only SQL guard) guided by a `dashboard-generation` skill package.

**Tech Stack:** Python/FastAPI/SQLAlchemy (backend), React/Vite/recharts/vitest (frontend). No new backend or frontend dependencies.

## Global Constraints

- **Multi-tenant isolation:** every model extends `TimestampedBase` (provides `id`, `created_date`, `updated_date`, `created_by_id`, `is_deleted`, `org_id`, `app_id`, `to_dict()`). All reads/writes scoped by `user.org_id`; fail-closed on any auth/lookup error (audit C1/C2).
- **Dual project columns:** models carry BOTH `project_id` (FK → `projects.id`, nullable) AND legacy `project` (name string, default `"global"`). UI/backend must match id-OR-name.
- **No Alembic:** schema changes via runtime ensure; NEW tables are created by `Base.metadata.create_all()` at startup (sqlite) — the model just needs to be imported so it's registered on `Base.metadata`.
- **Tool registration:** `from app.services.tool_registry import registry`; `registry.register(name=..., schema=<JSON schema dict>, handler=<async fn>, category=..., toolset=..., description=..., emoji=..., max_result_size_chars=...)`. Handler signature: `async def _handler(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict`. Module must be imported in `services/tool_handlers/__init__.py`; wrap registration in try/except with `logger.exception` on failure.
- **Live query reuse:** `QueryService(db).execute(kb_id, sql, max_rows, timeout_s)` → returns `{"source":{id,name,db_type}, "sql", "rows":[...], "row_count", "truncated", "elapsed_ms"}`. Raises `ValueError` on bad KB/SQL, `DriverUnavailable` on missing driver.
- **Auth dependency:** `from app.deps import get_current_user_required` → `User` with `.id`, `.org_id`, `.role`. Use `Depends(get_current_user_required)`.
- **Frontend auth:** all API calls via `authFetch` (Bearer JWT, silent 401 refresh-retry). No iframes for live data — sidesteps the iframe-can't-send-Bearer gap.
- **RAM-limited dev box:** run only explicit single pytest files by path; never `python -m pytest tests/` or broad `-k`; no `-n` workers. Frontend tests: `npx vitest run <file>` only.
- **Test DB setup (sqlite):** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`. sqlite enforces FKs — create real Project + KnowledgeBase rows.
- **Refresh interval:** default 30s, clamp 10..300s at create time.
- **SQL safety:** read-only (leading `SELECT`/`WITH` only; reject DDL/DML), single statement (reject `;` + trailing non-whitespace), row cap = `min(widget request, kb.max_rows_per_query)`, timeout ≤ `kb.timeout_seconds`. Validated at create AND query time.

---

## File Structure

**Backend — new:**
- `backend/app/models/dashboard.py` — `Dashboard` model (binds to one DB KnowledgeBase).
- `backend/app/services/dashboard_query.py` — `validate_widget_sql()` guard + `run_dashboard_query()` orchestration over `QueryService`.
- `backend/app/routers/dashboards.py` — CRUD + `/query` API (session-auth, org-scoped, fail-closed).
- `backend/app/services/tool_handlers/dashboard_tools.py` — `create_dashboard` tool (pydantic validation, datasource access check, persist, return artifact).
- `backend/tests/test_dashboard_model.py`, `test_dashboard_query.py`, `test_dashboards_api.py`, `test_create_dashboard_tool.py`

**Backend — modify:**
- `backend/app/models/__init__.py` — import `Dashboard` so it registers on `Base.metadata`.
- `backend/app/services/tool_handlers/__init__.py` — import `dashboard_tools` to trigger registration.
- `backend/app/services/tool_registry.py` — add `"create_dashboard"` to `DEFAULT_USER_AGENT_TOOLS` and `general_assistant` default set (role-filtered via existing RBAC).
- `backend/main.py` — import + `include_router(dashboards_router, prefix="/api")`.

**Backend — skill package (new):**
- `backend/docs/starter_templates/dashboard-generation/manifest.json` + `prompt.md` (teaches the LLM the widget schema + `create_dashboard` contract).

**Frontend — new:**
- `frontend/src/api/dashboards.js` — client (`authFetch`): `listDashboards`, `getDashboard`, `createDashboard`, `deleteDashboard`, `queryDashboard`.
- `frontend/src/components/dashboard/DashboardViewer.jsx` — schema-driven live renderer (recharts) + poll lifecycle.
- `frontend/src/components/dashboard/DashboardPopup.jsx` — shared chrome, `variant="chat"|"myspace"`.
- `frontend/src/components/dashboard/DashboardCard.jsx` — chat message card.
- `frontend/src/components/dashboard/DashboardViewer.test.jsx`

**Frontend — modify:**
- `frontend/src/components/chat/partitionArtifacts.js` — route `source === 'dashboard'` artifacts to the inline dashboard surface.
- `frontend/src/components/chat/MessageBubble.jsx` — render `<DashboardCard>` for dashboard artifacts; mount `DashboardPopup` (chat variant) on click.
- `frontend/src/pages/MySpace.jsx` — add `dashboard` tab; clicking a row opens `DashboardPopup` (myspace variant) as same-page overlay.
- translation dictionaries — `mySpace.tabs.dashboard`.

---

## Task 1: Dashboard model

**Files:**
- Create: `backend/app/models/dashboard.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_dashboard_model.py`

**Interfaces:**
- Produces: `Dashboard` model (extends `TimestampedBase`); columns `project_id`, `project`, `datasource_kb_id`, `name`, `description`, `definition` (JSON), `refresh_interval_seconds`. `to_dict()` inherited.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_model.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
from app.models.dashboard import Dashboard
from app.models.base import TimestampedBase


def test_dashboard_inherits_timestamped_base():
    assert issubclass(Dashboard, TimestampedBase)


def test_dashboard_has_required_columns():
    cols = {c.name for c in Dashboard.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id", "is_deleted",
        "org_id", "app_id",  # from TimestampedBase
        "project_id", "project", "datasource_kb_id", "name", "description",
        "definition", "refresh_interval_seconds",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_dashboard_tablename():
    assert Dashboard.__tablename__ == "dashboards"


def test_dashboard_defaults():
    assert Dashboard.__table__.c.refresh_interval_seconds.default.arg == 30
    assert Dashboard.__table__.c.project.default.arg == "global"
    assert Dashboard.__table__.c.is_deleted.default.arg is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.dashboard'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/models/dashboard.py
"""Dashboard — a live, schema-driven dashboard bound to one database KnowledgeBase.

A dashboard stores a JSON ``definition`` of widgets (KPI / line / bar / pie /
table), each with its own read-only SQL. At view time the frontend polls
``POST /api/dashboards/{id}/query`` which runs every widget's SQL against the
bound datasource via the existing ``QueryService`` (read-only, row-capped,
timeout-bounded). One dashboard binds to exactly ONE database KnowledgeBase.
"""
from typing import Optional

from sqlalchemy import String, Integer, JSON, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Dashboard(TimestampedBase):
    __tablename__ = "dashboards"

    # Dual project binding (id-OR-name convention used across zhanlu).
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    project: Mapped[str] = mapped_column(String(200), default="global", nullable=False)

    # The ONE database KnowledgeBase this dashboard queries.
    datasource_kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # {"widgets": [{id, type, title, sql, options}]}
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)

    refresh_interval_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
```

Then register it on `Base.metadata` by importing in the models package:

```python
# backend/app/models/__init__.py  — add near the other model imports:
from app.models.dashboard import Dashboard  # noqa: F401
```

(If `__init__.py` already has a block of `from app.models.X import Y`, append this line in the same block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_model.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/dashboard.py backend/app/models/__init__.py backend/tests/test_dashboard_model.py
git commit -m "feat(dashboards): add Dashboard model bound to one DB KnowledgeBase"
```

---

## Task 2: Widget-SQL safety guard

**Files:**
- Create: `backend/app/services/dashboard_query.py`
- Test: `backend/tests/test_dashboard_query.py` (guard cases only in this task)

**Interfaces:**
- Produces: `validate_widget_sql(sql: str) -> None` — raises `ValueError` on non-read-only / multi-statement SQL; returns None on success. Also `CLAMP_REFRESH` constant logic lives here.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_query.py  (append more cases in Task 3)
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import pytest
from app.services.dashboard_query import validate_widget_sql, clamp_refresh_interval


def test_select_allowed():
    validate_widget_sql("SELECT 1")  # no raise
    validate_widget_sql("  with cte as (select 1) select * from cte")


@pytest.mark.parametrize("bad", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN x INT",
    "CREATE TABLE t (x INT)",
    "TRUNCATE t",
    "GRANT SELECT ON t TO u",
    "REVOKE ALL ON t FROM u",
])
def test_ddl_dml_rejected(bad):
    with pytest.raises(ValueError):
        validate_widget_sql(bad)


def test_multi_statement_rejected():
    with pytest.raises(ValueError):
        validate_widget_sql("SELECT 1; DROP TABLE t")


def test_trailing_semicolon_ok():
    validate_widget_sql("SELECT 1;")  # single stmt + trailing ; is fine


def test_empty_rejected():
    with pytest.raises(ValueError):
        validate_widget_sql("   ")


def test_clamp_refresh_interval():
    assert clamp_refresh_interval(None) == 30
    assert clamp_refresh_interval(5) == 10
    assert clamp_refresh_interval(1000) == 300
    assert clamp_refresh_interval(45) == 45
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.dashboard_query'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/dashboard_query.py
"""Live-dashboard query orchestration + widget-SQL safety guard.

The guard is applied at create time (in the ``create_dashboard`` tool) AND at
query time (in the ``/query`` endpoint) — stored SQL is never trusted.
"""
from __future__ import annotations

import re

# DDL/DML/DDL-admin keywords that must never appear as a statement-leading verb.
_FORBIDDEN_LEADING = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|REPLACE|CALL|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

# A semicolon followed by more non-whitespace SQL = multiple statements.
_MULTI_STMT = re.compile(r";\s*\S")

MIN_REFRESH = 10
MAX_REFRESH = 300
DEFAULT_REFRESH = 30


def validate_widget_sql(sql: str) -> None:
    """Raise ValueError if ``sql`` is not a single read-only SELECT/WITH statement."""
    if sql is None or not sql.strip():
        raise ValueError("Widget SQL is empty")
    if _FORBIDDEN_LEADING.match(sql):
        raise ValueError("Widget SQL must be read-only (SELECT/WITH only)")
    if _MULTI_STMT.search(sql):
        raise ValueError("Widget SQL must be a single statement")
    # Must start with SELECT or WITH after stripping.
    stripped = sql.strip().lstrip("(").strip()
    if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise ValueError("Widget SQL must start with SELECT or WITH")


def clamp_refresh_interval(seconds: int | None) -> int:
    if seconds is None:
        return DEFAULT_REFRESH
    return max(MIN_REFRESH, min(MAX_REFRESH, int(seconds)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_query.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/dashboard_query.py backend/tests/test_dashboard_query.py
git commit -m "feat(dashboards): add read-only widget-SQL guard + refresh clamp"
```

---

## Task 3: Dashboard query orchestration (per-widget isolation)

**Files:**
- Modify: `backend/app/services/dashboard_query.py`
- Modify: `backend/tests/test_dashboard_query.py`

**Interfaces:**
- Consumes: `QueryService(db).execute(kb_id, sql, max_rows, timeout_s)` → `{"source", "sql", "rows", "row_count", "truncated", "elapsed_ms"}`; raises `ValueError` / `DriverUnavailable`.
- Produces: `async def run_dashboard_query(db, dashboard) -> dict` → `{"dashboard_id", "refreshed_at", "results": {widget_id: {"columns","rows","error","truncated"}}}`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_dashboard_query.py`:

```python
import asyncio
from datetime import datetime, timezone
from app.services.dashboard_query import run_dashboard_query


class _FakeKb:
    id = "kb-1"
    name = "sales"
    db_type = "sqlite"
    source_kind = "database"
    max_rows_per_query = 5000
    timeout_seconds = 20


class _FakeDashboard:
    id = "dash-1"
    datasource_kb_id = "kb-1"
    definition = {
        "widgets": [
            {"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}},
            {"id": "w2", "type": "line", "title": "B", "sql": "BAD SQL NOT SELECT", "options": {}},
        ]
    }


class _FakeQueryService:
    def __init__(self, db):
        pass

    def execute(self, kb_id, sql, max_rows, timeout_s):
        # Simulate the real service: guard already strips trailing ';'
        assert sql.strip().upper().startswith("SELECT") or sql.strip().upper().startswith("WITH")
        return {"source": {"id": kb_id}, "sql": sql, "rows": [{"n": 1}], "row_count": 1, "truncated": False, "elapsed_ms": 2}


def test_run_dashboard_query_isolates_widget_errors(monkeypatch):
    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _FakeQueryService)
    monkeypatch.setattr(dq, "KnowledgeBase", type("K", (), {"id": "kb-1"}))  # not used by fake svc

    result = asyncio.get_event_loop().run_until_complete(run_dashboard_query(db=None, dashboard=_FakeDashboard()))

    assert result["dashboard_id"] == "dash-1"
    assert "refreshed_at" in result
    w1 = result["results"]["w1"]
    assert w1["error"] is None and w1["rows"] == [{"n": 1}]
    w2 = result["results"]["w2"]
    assert w2["error"] and w2["rows"] == []  # bad SQL -> isolated error, not blank dashboard


def test_run_dashboard_query_uses_kb_controls(monkeypatch):
    captured = {}
    class _Svc(_FakeQueryService):
        def execute(self, kb_id, sql, max_rows, timeout_s):
            captured["max_rows"] = max_rows
            captured["timeout_s"] = timeout_s
            return {"rows": [], "row_count": 0, "truncated": False, "elapsed_ms": 1, "sql": sql, "source": {"id": kb_id}}
    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _Svc)
    # dashboard with a good widget; kb controls should cap max_rows/timeout
    asyncio.get_event_loop().run_until_complete(run_dashboard_query(db=None, dashboard=_FakeDashboard()))
    assert captured["max_rows"] <= 5000
    assert captured["timeout_s"] <= 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_query.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_dashboard_query'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/dashboard_query.py`:

```python
import asyncio
from datetime import datetime, timezone
from typing import Any

from app.services.db.query_service import QueryService


async def run_dashboard_query(db, dashboard) -> dict:
    """Run every widget's SQL against the dashboard's bound datasource.

    Each widget is validated (read-only guard) and executed independently; a
    failing widget sets that widget's ``error`` and does NOT abort the others
    (one bad SQL = one red widget, not a blank dashboard). All widgets use the
    bound KB's query controls as upper bounds.
    """
    widgets = (dashboard.definition or {}).get("widgets", []) or []
    # Resolve KB controls as upper bounds (fail open to defaults if KB missing).
    max_rows = DEFAULT_MAX_ROWS
    timeout_s = DEFAULT_TIMEOUT_S
    try:
        from app.models.knowledge_base import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == dashboard.datasource_kb_id).first()
        if kb is not None:
            max_rows = int(getattr(kb, "max_rows_per_query", DEFAULT_MAX_ROWS) or DEFAULT_MAX_ROWS)
            timeout_s = int(getattr(kb, "timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    except Exception:
        pass

    async def _run_one(widget: dict) -> tuple[str, dict]:
        wid = widget.get("id") or widget.get("title")
        sql = widget.get("sql") or ""
        try:
            validate_widget_sql(sql)
        except ValueError as e:
            return wid, {"columns": [], "rows": [], "error": str(e), "truncated": False}
        try:
            res = await asyncio.to_thread(
                lambda: QueryService(db).execute(
                    dashboard.datasource_kb_id, sql, max_rows=max_rows, timeout_s=timeout_s
                )
            )
            return wid, {
                "columns": list(res["rows"][0].keys()) if res["rows"] else [],
                "rows": res["rows"],
                "error": None,
                "truncated": res.get("truncated", False),
            }
        except Exception as e:  # ValueError, DriverUnavailable, DB error, etc.
            return wid, {"columns": [], "rows": [], "error": str(e), "truncated": False}

    pairs = await asyncio.gather(*[_run_one(w) for w in widgets])
    return {
        "dashboard_id": dashboard.id,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "results": {wid: payload for wid, payload in pairs},
    }


DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_S = 10
```

(Place the two `DEFAULT_*` constants near the top of the file instead of the bottom if preferred; they must be defined before `run_dashboard_query` references them at call time.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_query.py -v`
Expected: PASS (all guard + orchestration tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/dashboard_query.py backend/tests/test_dashboard_query.py
git commit -m "feat(dashboards): per-widget isolated live query over QueryService"
```

---

## Task 4: Dashboards CRUD + query API

**Files:**
- Create: `backend/app/routers/dashboards.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_dashboards_api.py`

**Interfaces:**
- Consumes: `Dashboard` model (Task 1), `run_dashboard_query` (Task 3), `validate_widget_sql` + `clamp_refresh_interval` (Task 2), `get_current_user_required`.
- Produces: `router` (APIRouter) mounted at `/api/dashboards`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboards_api.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.models.dashboard import Dashboard
from app.models.project import Project
from app.models.user import User
import app.models  # noqa  register all models

@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    from main import app
    return TestClient(app)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _auth_header(db):
    # Create a real user + a session/JWT the app accepts. The app's auth dep
    # reads a Bearer token; reuse the existing test-auth helper if present.
    # Minimal: monkeypatch get_current_user_required via dependency overrides.
    from app.deps import get_current_user_required
    from main import app
    user = User(email="t@t.io", first_name="t", org_id="default-org", app_id="default-app")
    db.add(user); db.commit()
    app.dependency_overrides[get_current_user_required] = lambda: user
    return {"Authorization": "Bearer test"}


def test_create_and_get_dashboard(client, db):
    h = _auth_header(db)
    proj = Project(name="p1", org_id="default-org", app_id="default-app")
    db.add(proj); db.commit()
    body = {
        "name": "Sales", "datasource_kb_id": "kb-x", "project_id": proj.id,
        "definition": {"widgets": [{"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}}]},
        "refresh_interval_seconds": 45,
    }
    r = client.post("/api/dashboards", json=body, headers=h)
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    g = client.get(f"/api/dashboards/{did}", headers=h)
    assert g.status_code == 200
    assert g.json()["name"] == "Sales"
    assert g.json()["refresh_interval_seconds"] == 45


def test_list_scoped_to_project(client, db):
    h = _auth_header(db)
    proj = Project(name="p2", org_id="default-org", app_id="default-app")
    db.add(proj); db.commit()
    client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": "kb-x", "project_id": proj.id, "definition": {"widgets": []}}, headers=h)
    r = client.get(f"/api/dashboards?project_id={proj.id}", headers=h)
    assert r.status_code == 200
    assert any(d["name"] == "d" for d in r.json())


def test_create_rejects_non_readonly_sql(client, db):
    h = _auth_header(db)
    body = {"name": "bad", "datasource_kb_id": "kb-x", "definition": {"widgets": [{"id": "w1", "type": "kpi", "title": "A", "sql": "DROP TABLE t", "options": {}}]}}
    r = client.post("/api/dashboards", json=body, headers=h)
    assert r.status_code == 400


def test_unauth_rejected(client):
    assert client.get("/api/dashboards").status_code in (401, 403)


def test_delete(client, db):
    h = _auth_header(db)
    r = client.post("/api/dashboards", json={"name": "del", "datasource_kb_id": "kb-x", "definition": {"widgets": []}}, headers=h)
    did = r.json()["id"]
    assert client.delete(f"/api/dashboards/{did}", headers=h).status_code in (200, 204)
    assert client.get(f"/api/dashboards/{did}", headers=h).status_code == 404
```

Note: if the project already has a test-auth helper (search `tests/` for `dependency_overrides[get_current_user_required]`), reuse it inside `_auth_header` instead of re-implementing.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboards_api.py -v`
Expected: FAIL — 404 from unknown route (router not mounted yet).

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/routers/dashboards.py
"""Dashboards API — session-auth, org-scoped, fail-closed."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.models.dashboard import Dashboard
from app.services.dashboard_query import (
    validate_widget_sql, clamp_refresh_interval, run_dashboard_query,
)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


def _scoped(db: Session, user, dashboard_id: str) -> Dashboard:
    d = db.query(Dashboard).filter(
        Dashboard.id == dashboard_id,
        Dashboard.org_id == user.org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return d


def _validate_definition(definition: dict) -> None:
    if not isinstance(definition, dict) or not isinstance(definition.get("widgets"), list) or not definition["widgets"]:
        raise HTTPException(status_code=400, detail="definition.widgets must be a non-empty list")
    for w in definition["widgets"]:
        if not w.get("sql"):
            raise HTTPException(status_code=400, detail="each widget needs sql")
        try:
            validate_widget_sql(w["sql"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: {e}")


@router.post("", status_code=201)
def create_dashboard(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    _validate_definition(payload.get("definition") or {})
    d = Dashboard(
        name=payload["name"],
        description=payload.get("description"),
        datasource_kb_id=payload["datasource_kb_id"],
        project_id=payload.get("project_id"),
        project=payload.get("project") or "global",
        definition=payload["definition"],
        refresh_interval_seconds=clamp_refresh_interval(payload.get("refresh_interval_seconds")),
        org_id=user.org_id,
        app_id=getattr(user, "app_id", "default-app"),
        created_by_id=user.id,
    )
    db.add(d); db.commit(); db.refresh(d)
    return d.to_dict()


@router.get("")
def list_dashboards(project_id: str | None = Query(None), db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    q = db.query(Dashboard).filter(Dashboard.org_id == user.org_id, Dashboard.is_deleted == False)  # noqa: E712
    if project_id:
        q = q.filter(Dashboard.project_id == project_id)
    return [d.to_dict() for d in q.order_by(Dashboard.updated_date.desc()).limit(1000)]


@router.get("/{dashboard_id}")
def get_dashboard(dashboard_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    return _scoped(db, user, dashboard_id).to_dict()


@router.delete("/{dashboard_id}", status_code=204)
def delete_dashboard(dashboard_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    d.is_deleted = True
    db.commit()
    return None


@router.post("/{dashboard_id}/query")
async def query_dashboard(dashboard_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    return await run_dashboard_query(db, d)
```

Register the router in `backend/main.py`:

```python
# near the other router imports (after knowledge_bases_router):
from app.routers.dashboards import router as dashboards_router
# ...
# near the other include_router calls (after knowledge_bases):
app.include_router(dashboards_router, prefix="/api")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboards_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/dashboards.py backend/main.py backend/tests/test_dashboards_api.py
git commit -m "feat(dashboards): CRUD + live query API (org-scoped, fail-closed)"
```

---

## Task 5: `create_dashboard` agent tool

**Files:**
- Create: `backend/app/services/tool_handlers/dashboard_tools.py`
- Modify: `backend/app/services/tool_handlers/__init__.py`
- Modify: `backend/app/services/tool_registry.py`
- Test: `backend/tests/test_create_dashboard_tool.py`

**Interfaces:**
- Consumes: `Dashboard` model, `validate_widget_sql` + `clamp_refresh_interval`, the project-binding resolver used elsewhere (id-OR-name union) to verify the user can access `datasource_kb_id`.
- Produces: `create_dashboard` tool registered on the registry; handler returns `{"success": True, "dashboard": {...}, "artifact": {"source": "dashboard", "dashboard_id", "title", "datasource_name", "widget_count"}}` so the chat layer renders a live card.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_create_dashboard_tool.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import pytest
from app.database import Base, engine, SessionLocal
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
import app.models  # noqa
from app.services.tool_handlers.dashboard_tools import _create_dashboard


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    Base.metadata.create_all(engine)


def _setup(db):
    user = User(email="t@t.io", first_name="t", org_id="default-org", app_id="default-app")
    db.add(user)
    proj = Project(name="sales-proj", org_id="default-org", app_id="default-app")
    db.add(proj)
    kb = KnowledgeBase(name="sales_db", db_type="sqlite", source_kind="database",
                       org_id="default-org", app_id="default-app", project_id=proj.id)
    db.add(kb); db.commit()
    return user, proj, kb


def test_create_dashboard_success(db_session):
    # db_session fixture: a SessionLocal() yield (create per repo convention)
    user, proj, kb = _setup(db_session)
    args = {
        "datasource_id": kb.id,
        "title": "Revenue",
        "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS n", "options": {}}],
        "refresh_interval_seconds": 20,
    }
    res = asyncio.get_event_loop().run_until_complete(
        _create_dashboard(args, db_session, user.id, context={"project_id": proj.id, "org_id": user.org_id})
    )
    assert res["success"] is True
    assert res["artifact"]["source"] == "dashboard"
    assert res["artifact"]["widget_count"] == 1
    assert res["dashboard"]["datasource_kb_id"] == kb.id


def test_create_dashboard_rejects_ddl(db_session):
    user, proj, kb = _setup(db_session)
    args = {"datasource_id": kb.id, "title": "bad",
            "widgets": [{"id": "w1", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}
    res = asyncio.get_event_loop().run_until_complete(
        _create_dashboard(args, db_session, user.id, context={"project_id": proj.id, "org_id": user.org_id})
    )
    assert res["success"] is False
    assert "read-only" in res.get("error", "").lower() or "select" in res.get("error", "").lower()


def test_create_dashboard_rejects_foreign_datasource(db_session):
    user, proj, kb = _setup(db_session)
    # a KB in a different project/org
    other = KnowledgeBase(name="other", db_type="sqlite", source_kind="database",
                          org_id="other-org", app_id="default-app")
    db_session.add(other); db_session.commit()
    args = {"datasource_id": other.id, "title": "x", "widgets": [{"id": "w1", "type": "kpi", "title": "x", "sql": "SELECT 1", "options": {}}]}
    res = asyncio.get_event_loop().run_until_complete(
        _create_dashboard(args, db_session, user.id, context={"project_id": proj.id, "org_id": user.org_id})
    )
    assert res["success"] is False  # cross-project/cross-org rejected (no IDOR)
```

Note: `db_session` is the repo's existing session fixture; if absent, add `s = SessionLocal(); yield s; s.close()` in a conftest. `KnowledgeBase` may require additional non-null columns — add only the minimum the model enforces.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_create_dashboard_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: ... dashboard_tools`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/tool_handlers/dashboard_tools.py
"""create_dashboard agent tool — builds a live dashboard bound to one DB KB.

Upfront pydantic validation closes the 'no upfront tool-arg validation' gap.
The datasource is resolved against the project's bound KBs (id-OR-name union)
to prevent cross-project/cross-org IDOR. Stored widget SQL is re-validated at
query time (never trusted).
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.models.dashboard import Dashboard
from app.models.knowledge_base import KnowledgeBase
from app.services.dashboard_query import validate_widget_sql, clamp_refresh_interval

logger = logging.getLogger(__name__)


class WidgetSpec(BaseModel):
    id: str
    type: str  # kpi | line | bar | pie | table
    title: str
    sql: str
    options: dict = Field(default_factory=dict)

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        validate_widget_sql(v)
        return v


class CreateDashboardArgs(BaseModel):
    datasource_id: str
    title: str
    widgets: list[WidgetSpec] = Field(min_length=1)
    refresh_interval_seconds: Optional[int] = None
    description: Optional[str] = None


def _user_can_access_kb(db, user_id, org_id, project_id, kb_id) -> KnowledgeBase | None:
    """Resolve the KB and verify it belongs to the user's org AND the project."""
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.org_id == org_id,
        KnowledgeBase.is_deleted == False,  # noqa: E712
    ).first()
    if kb is None:
        return None
    # id-OR-name project match: kb.project_id == project_id OR kb.project == project name
    if project_id and kb.project_id and str(kb.project_id) == str(project_id):
        return kb
    if project_id:
        from app.models.project import Project
        proj = db.query(Project).filter(Project.id == project_id, Project.org_id == org_id).first()
        if proj and (kb.project or "").lower() == (proj.name or "").lower():
            return kb
    # No project context: allow if org-scoped (chat without active project).
    if not project_id:
        return kb
    return None


async def _create_dashboard(args: dict, db, user_id, context: dict | None = None) -> dict:
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    project_id = ctx.get("project_id")
    try:
        parsed = CreateDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid dashboard definition: {e}"}

    kb = _user_can_access_kb(db, user_id, org_id, project_id, parsed.datasource_id)
    if kb is None:
        return {"success": False, "error": "Datasource not accessible in this project/org."}

    d = Dashboard(
        name=parsed.title,
        description=parsed.description,
        datasource_kb_id=kb.id,
        project_id=project_id,
        project=ctx.get("project_name") or "global",
        definition={"widgets": [w.model_dump() for w in parsed.widgets]},
        refresh_interval_seconds=clamp_refresh_interval(parsed.refresh_interval_seconds),
        org_id=org_id,
        app_id=ctx.get("app_id") or "default-app",
        created_by_id=user_id,
    )
    db.add(d); db.commit(); db.refresh(d)
    return {
        "success": True,
        "dashboard": d.to_dict(),
        "artifact": {
            "source": "dashboard",
            "dashboard_id": d.id,
            "title": d.name,
            "datasource_name": kb.name,
            "widget_count": len(parsed.widgets),
        },
    }


# --- registration --------------------------------------------------------
CREATE_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_dashboard",
        "description": (
            "Build a LIVE dashboard bound to ONE database datasource. The dashboard "
            "auto-polls the database while open. Each widget needs read-only SQL "
            "(SELECT/WITH only). If the user has not named a datasource, ask them to "
            "pick one before calling this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "datasource_id": {"type": "string", "description": "The chosen DB datasource (KnowledgeBase) id."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "refresh_interval_seconds": {"type": "integer", "description": "Poll interval, 10..300 (default 30)."},
                "widgets": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["kpi", "line", "bar", "pie", "table"]},
                            "title": {"type": "string"},
                            "sql": {"type": "string", "description": "Read-only SELECT/WITH SQL for this widget."},
                            "options": {"type": "object"},
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
            },
            "required": ["datasource_id", "title", "widgets"],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="create_dashboard",
        schema=CREATE_DASHBOARD_SCHEMA,
        handler=_create_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Build a live database dashboard that auto-polls while open.",
        emoji="📊",
        max_result_size_chars=4_000,
    )
except Exception:
    logger.exception("Failed to register create_dashboard tool")
```

Add the import to `backend/app/services/tool_handlers/__init__.py` (in the existing import block):

```python
from app.services.tool_handlers import dashboard_tools  # noqa: F401  (registers create_dashboard)
```

Add `create_dashboard` to the default tool sets in `backend/app/services/tool_registry.py`:
- In `DEFAULT_USER_AGENT_TOOLS` (around line 483), append `"create_dashboard"`.
- In `DEFAULT_TOOLS_BY_AGENT["general_assistant"]` (around line 414), append `"create_dashboard"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_create_dashboard_tool.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/dashboard_tools.py backend/app/services/tool_handlers/__init__.py backend/app/services/tool_registry.py backend/tests/test_create_dashboard_tool.py
git commit -m "feat(dashboards): create_dashboard agent tool (pydantic validation, IDOR-safe datasource binding)"
```

---

## Task 6: `dashboard-generation` skill package

**Files:**
- Create: `backend/docs/starter_templates/dashboard-generation/manifest.json`
- Create: `backend/docs/starter_templates/dashboard-generation/prompt.md`
- Test: `backend/tests/test_dashboard_skill_loads.py`

**Interfaces:**
- Produces: an installable skill package consumable by `skills_loader` (manifest + prompt). No runtime behavior of its own; it teaches the LLM the widget schema + `create_dashboard` contract.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_skill_loads.py
import json
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1] / "docs" / "starter_templates" / "dashboard-generation"


def test_manifest_valid():
    m = json.loads((SKILL_DIR / "manifest.json").read_text())
    assert m["name"] == "dashboard-generation"
    assert "create_dashboard" in m.get("tools_used", []) or "create_dashboard" in (SKILL_DIR / "prompt.md").read_text()


def test_prompt_describes_widget_schema():
    p = (SKILL_DIR / "prompt.md").read_text()
    for token in ["kpi", "line", "bar", "pie", "table", "create_dashboard", "SELECT", "datasource"]:
        assert token in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_skill_loads.py -v`
Expected: FAIL — files missing.

- [ ] **Step 3: Write minimal implementation**

```json
// backend/docs/starter_templates/dashboard-generation/manifest.json
{
  "name": "dashboard-generation",
  "version": "1.0.0",
  "description": "Design a live database dashboard and build it via the create_dashboard tool.",
  "tools_used": ["create_dashboard"],
  "trigger_words": ["dashboard", "live dashboard", "metrics", "kpi"]
}
```

```markdown
<!-- backend/docs/starter_templates/dashboard-generation/prompt.md -->
# Dashboard Generation Skill

You build LIVE dashboards that query a database in real time.

## When to use
The user asks for a "dashboard", "metrics view", "live KPIs", etc.

## Hard rules
1. NEVER hand-write HTML for a live dashboard. ALWAYS call `create_dashboard`.
2. If the user has NOT named a datasource, STOP and ask them to pick ONE
   datasource from the project's connected databases before calling the tool.
3. Every widget's `sql` MUST be read-only: a single `SELECT` or `WITH`
   statement. No INSERT/UPDATE/DELETE/DDL. Alias numeric/date columns.
4. One dashboard binds to exactly ONE datasource.

## Widget schema
Each widget: `{id, type, title, sql, options}`.
- `type`: `kpi` | `line` | `bar` | `pie` | `table`
- `options`:
  - kpi: `{value_column}`
  - line/bar: `{x_column, y_column}`
  - pie: `{name_column, value_column}`
  - table: `{}` (renders all returned rows/columns)

## Workflow
1. Confirm the datasource (ask if unspecified).
2. Design 3–6 widgets covering the request (at least one KPI + one trend chart).
3. Call `create_dashboard` with `datasource_id`, `title`, `widgets[]`,
   `refresh_interval_seconds` (default 30).
4. Tell the user the dashboard is live and can be reopened from My Space.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_skill_loads.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/docs/starter_templates/dashboard-generation/ backend/tests/test_dashboard_skill_loads.py
git commit -m "feat(dashboards): dashboard-generation skill package"
```

---

## Task 7: Frontend API client

**Files:**
- Create: `frontend/src/api/dashboards.js`
- Test: `frontend/src/api/dashboards.test.js`

**Interfaces:**
- Produces: `listDashboards(projectId)`, `getDashboard(id)`, `createDashboard(body)`, `deleteDashboard(id)`, `queryDashboard(id)` — all via `authFetch`, base `/api/dashboards`.

- [ ] **Step 1: Write the failing test**

```js
// frontend/src/api/dashboards.test.js
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { listDashboards, getDashboard, createDashboard, deleteDashboard, queryDashboard } from './dashboards';

global.fetch = vi.fn();
const authFetch = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...a) => authFetch(...a) }));

beforeEach(() => { authFetch.mockReset(); });

describe('dashboards api', () => {
  it('listDashboards appends project_id', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => [] });
    await listDashboards('p1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards?project_id=p1', expect.objectContaining({ method: 'GET' }));
  });
  it('getDashboard', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd1' }) });
    const d = await getDashboard('d1');
    expect(d.id).toBe('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1', expect.objectContaining({ method: 'GET' }));
  });
  it('createDashboard posts body', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd2' }) });
    await createDashboard({ name: 'x' });
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards', expect.objectContaining({ method: 'POST', body: '{"name":"x"}' }));
  });
  it('queryDashboard posts empty', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ results: {} }) });
    await queryDashboard('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1/query', expect.objectContaining({ method: 'POST' }));
  });
  it('deleteDashboard', async () => {
    authFetch.mockResolvedValue({ ok: true });
    await deleteDashboard('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1', expect.objectContaining({ method: 'DELETE' }));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/dashboards.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```js
// frontend/src/api/dashboards.js
import { authFetch } from '@/api/authFetch';

const BASE = '/api/dashboards';

async function j(res) {
  if (!res.ok) throw new Error(`dashboards API ${res.status}`);
  if (res.status === 204) return null;
  return res.json();
}

export function listDashboards(projectId) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  return authFetch(`${BASE}${qs}`, { method: 'GET' }).then(j);
}
export function getDashboard(id) {
  return authFetch(`${BASE}/${id}`, { method: 'GET' }).then(j);
}
export function createDashboard(body) {
  return authFetch(BASE, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j);
}
export function deleteDashboard(id) {
  return authFetch(`${BASE}/${id}`, { method: 'DELETE' }).then(j);
}
export function queryDashboard(id) {
  return authFetch(`${BASE}/${id}/query`, { method: 'POST' }).then(j);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/dashboards.test.js`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/dashboards.js frontend/src/api/dashboards.test.js
git commit -m "feat(dashboards): frontend API client"
```

---

## Task 8: `DashboardViewer` (live, schema-driven, auto-poll)

**Files:**
- Create: `frontend/src/components/dashboard/DashboardViewer.jsx`
- Create: `frontend/src/components/dashboard/DashboardViewer.test.jsx`

**Interfaces:**
- Consumes: `queryDashboard(id)` (Task 7), `recharts` (already bundled).
- Produces: `<DashboardViewer dashboard={{id, definition, refresh_interval_seconds}} polling />` — renders widgets, polls while mounted+visible, cleans up on unmount, per-widget error states.

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/dashboard/DashboardViewer.test.jsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup, waitFor } from '@testing-library/react';
import DashboardViewer from './DashboardViewer';
import * as dashApi from '@/api/dashboards';

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); cleanup(); });

vi.mock('@/api/dashboards', () => ({ queryDashboard: vi.fn() }));

const dash = {
  id: 'd1',
  refresh_interval_seconds: 30,
  definition: { widgets: [
    { id: 'w1', type: 'kpi', title: 'Revenue', sql: 'SELECT 1', options: { value_column: 'n' } },
    { id: 'w2', type: 'line', title: 'Trend', sql: 'SELECT 1', options: { x_column: 'd', y_column: 'v' } },
  ] },
};

it('renders widgets and fetches on mount', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 42 }], error: null, truncated: false },
    w2: { columns: ['d', 'v'], rows: [{ d: '2026-01', v: 5 }], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={dash} polling />);
  expect(await screen.findByText('Revenue')).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText('42')).toBeInTheDocument());
});

it('isolates per-widget errors', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 7 }], error: null, truncated: false },
    w2: { columns: [], rows: [], error: 'bad sql', truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={dash} polling />);
  await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument());
  expect(screen.getByText(/bad sql/i)).toBeInTheDocument();
});

it('polls on interval', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false },
    w2: { columns: [], rows: [], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={dash} polling />);
  await waitFor(() => expect(dashApi.queryDashboard).toHaveBeenCalledTimes(1));
  await act(async () => { vi.advanceTimersByTime(30000); });
  await waitFor(() => expect(dashApi.queryDashboard).toHaveBeenCalledTimes(2));
});

it('stops polling on unmount', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false },
    w2: { columns: [], rows: [], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  const { unmount } = render(<DashboardViewer dashboard={dash} polling />);
  await waitFor(() => expect(dashApi.queryDashboard).toHaveBeenCalled());
  unmount();
  const calls = dashApi.queryDashboard.mock.calls.length;
  await act(async () => { vi.advanceTimersByTime(60000); });
  expect(dashApi.queryDashboard.mock.calls.length).toBe(calls);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.test.jsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/components/dashboard/DashboardViewer.jsx
import { useEffect, useRef, useState, useCallback } from 'react';
import { BarChart, Bar, LineChart, Line, PieChart, Pie, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { RefreshCw } from 'lucide-react';
import { queryDashboard } from '@/api/dashboards';

const PIE_COLORS = ['#6366f1', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'];

export default function DashboardViewer({ dashboard, polling = true }) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);
  const visibleRef = useRef(true);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    if (!dashboard?.id || !visibleRef.current) return;
    setLoading(true);
    try {
      const res = await queryDashboard(dashboard.id);
      if (!mountedRef.current) return;
      setResults(res.results || {});
      setRefreshedAt(res.refreshed_at);
    } catch (e) {
      // keep last results; surface a top-level error
      if (mountedRef.current) setResults((r) => ({ ...r, __error: String(e) }));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [dashboard?.id]);

  useEffect(() => {
    mountedRef.current = true;
    visibleRef.current = true;
    refresh();
    if (polling) {
      const ms = Math.max(10, Math.min(300, dashboard?.refresh_interval_seconds || 30)) * 1000;
      timerRef.current = setInterval(refresh, ms);
    }
    const onVis = () => {
      visibleRef.current = !document.hidden;
      if (visibleRef.current && !document.hidden) refresh();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearInterval(timerRef.current);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [refresh, polling, dashboard?.refresh_interval_seconds]);

  const widgets = dashboard?.definition?.widgets || [];
  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-heading text-lg font-semibold">{dashboard?.name}</h3>
        <button onClick={refresh} className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-secondary">
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          {refreshedAt ? new Date(refreshedAt).toLocaleTimeString() : '—'}
        </button>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {widgets.map((w) => <Widget key={w.id} widget={w} result={results[w.id]} />)}
      </div>
    </div>
  );
}

function Widget({ widget, result }) {
  const err = result?.error;
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  return (
    <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
      <div className="mb-2 text-sm font-medium">{widget.title}</div>
      {err ? (
        <div className="rounded-md bg-red-500/10 p-2 text-xs text-red-600">{err}</div>
      ) : widget.type === 'kpi' ? (
        <Kpi widget={widget} rows={rows} />
      ) : widget.type === 'line' ? (
        <Chart widget={widget} rows={rows} Comp={LineChart} Series={Line} />
      ) : widget.type === 'bar' ? (
        <Chart widget={widget} rows={rows} Comp={BarChart} Series={Bar} />
      ) : widget.type === 'pie' ? (
        <PieW widget={widget} rows={rows} />
      ) : (
        <Table cols={cols} rows={rows} />
      )}
    </div>
  );
}

function Kpi({ widget, rows }) {
  const col = widget.options?.value_column;
  const val = rows[0] && col != null ? rows[0][col] : (rows[0] ? Object.values(rows[0])[0] : '—');
  return <div className="text-3xl font-bold">{String(val)}</div>;
}

function Chart({ widget, rows, Comp, Series }) {
  const x = widget.options?.x_column; const y = widget.options?.y_column;
  const data = rows.map((r) => ({ name: x ? r[x] : '', value: y ? Number(r[y]) : Number(Object.values(r)[0] || 0) }));
  return (
    <ResponsiveContainer width="100%" height={180}>
      <Comp data={data}><XAxis dataKey="name" tick={{ fontSize: 10 }} /><YAxis tick={{ fontSize: 10 }} /><Tooltip /><Series dataKey="value" stroke="#6366f1" fill="#6366f1" /></Comp>
    </ResponsiveContainer>
  );
}

function PieW({ widget, rows }) {
  const name = widget.options?.name_column; const val = widget.options?.value_column;
  const data = rows.map((r) => ({ name: name ? r[name] : '', value: val ? Number(r[val]) : 0 }));
  return (
    <ResponsiveContainer width="100%" height={180}>
      <PieChart><Pie data={data} dataKey="value" nameKey="name" outerRadius={60}>{data.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}</Pie><Tooltip /></PieChart>
    </ResponsiveContainer>
  );
}

function Table({ cols, rows }) {
  return (
    <div className="max-h-40 overflow-auto">
      <table className="w-full text-xs"><thead><tr>{cols.map((c) => <th key={c} className="text-left">{c}</th>)}</tr></thead>
        <tbody>{rows.slice(0, 50).map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{String(r[c] ?? '')}</td>)}</tr>)}</tbody></table>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.test.jsx`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/DashboardViewer.test.jsx
git commit -m "feat(dashboards): live schema-driven DashboardViewer with auto-poll"
```

---

## Task 9: `DashboardPopup` (chat + My Space variants)

**Files:**
- Create: `frontend/src/components/dashboard/DashboardPopup.jsx`
- Create: `frontend/src/components/dashboard/DashboardPopup.test.jsx`

**Interfaces:**
- Consumes: `DashboardViewer` (Task 8), `getDashboard(id)` (Task 7).
- Produces: `<DashboardPopup dashboardId={id} variant="chat"|"myspace" onClose={...} />`.

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/dashboard/DashboardPopup.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import DashboardPopup from './DashboardPopup';
import * as dashApi from '@/api/dashboards';

vi.mock('@/api/dashboards', () => ({ getDashboard: vi.fn(), queryDashboard: vi.fn() }));

beforeEach(() => { dashApi.getDashboard.mockReset(); dashApi.queryDashboard.mockReset(); });

it('chat variant slides from right', async () => {
  dashApi.getDashboard.mockResolvedValue({ id: 'd1', name: 'Sales', refresh_interval_seconds: 30, definition: { widgets: [] } });
  dashApi.queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardPopup dashboardId="d1" variant="chat" onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  expect(screen.getByTestId('dashboard-popup').className).toMatch(/right-0/);
});

it('myspace variant is centered overlay', async () => {
  dashApi.getDashboard.mockResolvedValue({ id: 'd1', name: 'Sales', refresh_interval_seconds: 30, definition: { widgets: [] } });
  dashApi.queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardPopup dashboardId="d1" variant="myspace" onClose={() => {}} />);
  await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  expect(screen.getByTestId('dashboard-popup').className).toMatch(/items-center/);
});

it('onClose fires on close button', async () => {
  dashApi.getDashboard.mockResolvedValue({ id: 'd1', name: 'Sales', refresh_interval_seconds: 30, definition: { widgets: [] } });
  dashApi.queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
  const onClose = vi.fn();
  render(<DashboardPopup dashboardId="d1" variant="myspace" onClose={onClose} />);
  await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  screen.getByRole('button', { name: /close/i }).click();
  expect(onClose).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardPopup.test.jsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/components/dashboard/DashboardPopup.jsx
import { useEffect, useState } from 'react';
import { X, Loader2 } from 'lucide-react';
import { getDashboard } from '@/api/dashboards';
import DashboardViewer from './DashboardViewer';

export default function DashboardPopup({ dashboardId, variant = 'chat', onClose }) {
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    getDashboard(dashboardId).then((d) => { if (alive) setDashboard(d); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [dashboardId]);

  const overlay = variant === 'chat'
    ? 'fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-border bg-background shadow-xl'
    : 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4';

  const inner = variant === 'chat' ? 'flex h-full flex-col' : 'flex max-h-[90vh] w-full max-w-4xl flex-col rounded-xl border border-border bg-background shadow-xl';

  return (
    <div data-testid="dashboard-popup" className={overlay} onClick={variant === 'myspace' ? onClose : undefined}>
      <div className={inner} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <span className="text-xs text-muted-foreground">{variant === 'chat' ? 'Live Dashboard' : 'Dashboard'}</span>
          <button onClick={onClose} aria-label="Close" className="rounded p-1 hover:bg-secondary"><X className="h-4 w-4" /></button>
        </div>
        <div className="min-h-0 flex-1">
          {error ? <div className="p-4 text-sm text-red-600">{error}</div>
            : !dashboard ? <div className="flex h-full items-center justify-center"><Loader2 className="h-5 w-5 animate-spin" /></div>
            : <DashboardViewer dashboard={dashboard} polling />}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardPopup.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/DashboardPopup.jsx frontend/src/components/dashboard/DashboardPopup.test.jsx
git commit -m "feat(dashboards): DashboardPopup (chat right-slide + My Space overlay)"
```

---

## Task 10: Chat integration (dashboard card + popup)

**Files:**
- Create: `frontend/src/components/dashboard/DashboardCard.jsx`
- Modify: `frontend/src/components/chat/partitionArtifacts.js`
- Modify: `frontend/src/components/chat/MessageBubble.jsx`

**Interfaces:**
- Consumes: the chat message's artifact list (where the `create_dashboard` tool's returned `artifact` lands). Dashboard artifacts have `source === 'dashboard'` and carry `dashboard_id`, `title`, `datasource_name`, `widget_count`.
- Produces: a clickable dashboard card in the message; clicking mounts `DashboardPopup` (chat variant).

- [ ] **Step 1: Write the failing test (DashboardCard)**

```jsx
// frontend/src/components/dashboard/DashboardCard.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DashboardCard from './DashboardCard';

vi.mock('@/api/dashboards', () => ({ getDashboard: vi.fn(), queryDashboard: vi.fn() }));

it('renders metadata and fires onOpen', () => {
  const onOpen = vi.fn();
  render(<DashboardCard artifact={{ title: 'Sales', datasource_name: 'sales_db', widget_count: 4 }} onOpen={onOpen} />);
  expect(screen.getByText('Sales')).toBeInTheDocument();
  expect(screen.getByText(/sales_db/)).toBeInTheDocument();
  expect(screen.getByText(/4/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /open/i }));
  expect(onOpen).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardCard.test.jsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation (DashboardCard)**

```jsx
// frontend/src/components/dashboard/DashboardCard.jsx
import { LayoutDashboard } from 'lucide-react';

export default function DashboardCard({ artifact, onOpen }) {
  return (
    <div className="my-2 flex items-center justify-between rounded-lg border border-border bg-card p-3 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10"><LayoutDashboard className="h-4 w-4 text-primary" /></div>
        <div>
          <div className="text-sm font-medium">{artifact.title}</div>
          <div className="text-xs text-muted-foreground">{artifact.datasource_name} · {artifact.widget_count} widgets · live</div>
        </div>
      </div>
      <button onClick={onOpen} className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground hover:bg-primary/90">Open</button>
    </div>
  );
}
```

Then wire it into the chat. In `frontend/src/components/chat/partitionArtifacts.js`, route dashboard artifacts to the inline surface so they render as cards (not generic cards):

```js
export function partitionArtifacts(artifacts) {
  const list = Array.isArray(artifacts) ? artifacts.filter(Boolean) : [];
  const inline = list.filter(
    (a) => a.source === 'automation_file' || a.source === 'dashboard' || a.has_preview,
  );
  const inlineSet = new Set(inline);
  const cards = list.filter((a) => !inlineSet.has(a));
  return { inline, cards };
}
```

In `frontend/src/components/chat/MessageBubble.jsx`, where inline artifacts are rendered (the section that maps over `partitioned.inline`), add a branch for dashboard artifacts and popup state. Add at the top of the component:

```jsx
import DashboardCard from '@/components/dashboard/DashboardCard';
import DashboardPopup from '@/components/dashboard/DashboardPopup';
// ...inside the component:
const [openDashboardId, setOpenDashboardId] = useState(null);
```

In the inline-artifacts map:

```jsx
{partitioned.inline.map((a, i) =>
  a.source === 'dashboard' ? (
    <DashboardCard key={i} artifact={a} onOpen={() => setOpenDashboardId(a.dashboard_id)} />
  ) : (
    <InlineArtifactPreview key={i} artifact={a} /* existing props */ />
  )
)}
{openDashboardId && (
  <DashboardPopup dashboardId={openDashboardId} variant="chat" onClose={() => setOpenDashboardId(null)} />
)}
```

(Use the exact existing `InlineArtifactPreview` props that the surrounding code already passes; this task only adds the dashboard branch + the conditional popup mount.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardCard.test.jsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/DashboardCard.jsx frontend/src/components/dashboard/DashboardCard.test.jsx frontend/src/components/chat/partitionArtifacts.js frontend/src/components/chat/MessageBubble.jsx
git commit -m "feat(dashboards): chat integration — dashboard card + right-side popup"
```

---

## Task 11: My Space Dashboards tab (same-page popup)

**Files:**
- Modify: `frontend/src/pages/MySpace.jsx`
- Modify: translation dictionaries (find via the `useTranslate`/`LanguageProvider` the `mySpace.tabs` keys).

**Interfaces:**
- Consumes: `listDashboards(projectId)` (Task 7), `DashboardPopup` myspace variant (Task 9), the current project context already available in MySpace.
- Produces: a `dashboard` tab that lists dashboards; clicking opens `DashboardPopup` as a same-page overlay (no navigation).

- [ ] **Step 1: Write the failing test (MySpace dashboards tab)**

```jsx
// frontend/src/pages/MySpace.dashboards.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import MySpace from './MySpace';

vi.mock('@/api/dashboards', () => ({ listDashboards: vi.fn(), getDashboard: vi.fn(), queryDashboard: vi.fn() }));
vi.mock('@/lib/AuthContext', () => ({ useAuth: () => ({ user: { id: 'u1', org_id: 'o1' } }) }));
vi.mock('@/lib/LanguageProvider', () => ({ useLanguage: () => ({ lang: 'en', t: { mySpace: { tabs: { project: 'Projects', agent: 'Agents', file: 'Files', kb: 'KB', dashboard: 'Dashboards' } } } }) }));
vi.mock('@/lib/useTranslate', () => ({ useTranslate: () => ({ t: { mySpace: { tabs: { project: 'Projects', agent: 'Agents', file: 'Files', kb: 'KB', dashboard: 'Dashboards' } } } }) }));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn(), useSearchParams: () => [new URLSearchParams(), vi.fn()] }));

import * as dashApi from '@/api/dashboards';

beforeEach(() => { dashApi.listDashboards.mockReset(); dashApi.getDashboard.mockReset(); dashApi.queryDashboard.mockReset(); });

it('lists dashboards in the Dashboards tab and opens popup on click', async () => {
  dashApi.listDashboards.mockResolvedValue([{ id: 'd1', name: 'Sales', datasource_kb_id: 'kb1', refresh_interval_seconds: 30 }]);
  dashApi.getDashboard.mockResolvedValue({ id: 'd1', name: 'Sales', refresh_interval_seconds: 30, definition: { widgets: [] } });
  dashApi.queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<MySpace />);
  fireEvent.click(screen.getByText('Dashboards'));
  await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  fireEvent.click(screen.getByText('Sales'));
  await waitFor(() => expect(dashApi.getDashboard).toHaveBeenCalledWith('d1'));
});
```

Note: adapt the `AuthContext`/`LanguageProvider`/`useTranslate` mocks to the repo's actual exports (see `Sidebar.test.jsx` which stubs `@/lib/AuthContext`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/MySpace.dashboards.test.jsx`
Expected: FAIL — no Dashboards tab.

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/pages/MySpace.jsx`:
1. Add the import: `import { listDashboards } from '@/api/dashboards';` and `import DashboardPopup from '@/components/dashboard/DashboardPopup';` and `import { LayoutDashboard } from 'lucide-react';` (already imported — verify).
2. Add the tab to `TABS` (around line 68):
```js
{ key: 'dashboard', label: t.mySpace.tabs.dashboard || 'Dashboards', icon: LayoutDashboard, entity: 'Dashboard' },
```
3. Add state: `const [openDashboardId, setOpenDashboardId] = useState(null);`
4. In the `load()` function, add a branch for the dashboard tab:
```js
if (tab === 'dashboard') {
  setItems(await listDashboards(/* current project id if tracked */ undefined));
  setLoading(false);
  return;
}
```
5. In the tab body render (where the current tab's list is mapped), add a dashboard rendering branch:
```jsx
{tab === 'dashboard' && (
  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
    {items.map((d) => (
      <button key={d.id} onClick={() => setOpenDashboardId(d.id)} className="rounded-xl border border-border bg-card p-4 text-left shadow-sm hover:border-primary">
        <div className="flex items-center gap-2 text-sm font-medium"><LayoutDashboard className="h-4 w-4 text-primary" />{d.name}</div>
        <div className="mt-1 text-xs text-muted-foreground">{d.description || 'Live dashboard'}</div>
      </button>
    ))}
    {items.length === 0 && <div className="col-span-full rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">No dashboards yet.</div>}
  </div>
)}
{openDashboardId && <DashboardPopup dashboardId={openDashboardId} variant="myspace" onClose={() => setOpenDashboardId(null)} />}
```
6. Add `dashboard: 'Dashboards'` (and the Chinese equivalent `'仪表盘'`) to the translation dictionaries wherever `mySpace.tabs` is defined (search `useTranslate`/`LanguageProvider` for `tabs:`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/MySpace.dashboards.test.jsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MySpace.jsx frontend/src/pages/MySpace.dashboards.test.jsx frontend/src/lib/<translation files>
git commit -m "feat(dashboards): My Space Dashboards tab with same-page popup"
```

---

## Task 12: End-to-end build + smoke verification

**Files:** none (verification only)

- [ ] **Step 1: Backend smoke**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine); from app.services.tool_registry import registry; assert 'create_dashboard' in registry._tools, 'tool not registered'; print('OK')"`
Expected: prints `OK` (table created + tool registered).

- [ ] **Step 2: Run all new backend tests together (RAM-safe: 4 small files)**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_model.py tests/test_dashboard_query.py tests/test_dashboards_api.py tests/test_create_dashboard_tool.py tests/test_dashboard_skill_loads.py -v`
Expected: all PASS.

- [ ] **Step 3: Frontend build**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/dashboards.test.js src/components/dashboard/ src/pages/MySpace.dashboards.test.jsx && npm run build`
Expected: tests pass; build succeeds.

- [ ] **Step 4: Deploy dist (if target uses the rebuild script)**

Run: `/opt/rebuild-zhanlu.sh --force` (only if the dev server serves the built bundle; dist can silently go stale vs worktree — see MEMORY.md).
Expected: new bundle rebuilt + backend restarted.

- [ ] **Step 5: Final commit (if any lint fixes)**

```bash
git add -A
git commit -m "chore(dashboards): build + smoke verification"
```

---

## Self-Review

**1. Spec coverage:**
- R1 live (auto-poll) → Task 8 (DashboardViewer poll lifecycle) + Task 3 (query endpoint). ✓
- R2 persist (My Space) → Task 1 (model) + Task 4 (CRUD) + Task 11 (My Space tab). ✓
- R3 chat inline right popup → Task 9 (popup chat variant) + Task 10 (card + mount). ✓
- R4 skill-driven → Task 5 (tool) + Task 6 (skill package). ✓
- R5 explicit datasource → Task 5 (tool requires `datasource_id`; skill prompt instructs to ask if unspecified). ✓
- R6 safety → Task 2 (guard) + Task 3 (query-time re-validate) + Task 5 (create-time validate). ✓
- R7 security → Task 4 (`_scoped` org + fail-closed 404) + Task 5 (`_user_can_access_kb` IDOR check). ✓
- Deferred items (HTML escape-hatch, SSE push, multi-datasource, visual editor) → explicitly out of scope. ✓

**2. Placeholder scan:** No "TBD"/"implement later". The MessageBubble.jsx integration (Task 10) references "existing InlineArtifactPreview props" — that is a pointer to the surrounding existing code the implementer reads in-file, not a placeholder (the dashboard branch + popup mount are fully specified). ✓

**3. Type consistency:**
- `validate_widget_sql(sql) -> None` (raises) — used identically in Task 2, 3, 4, 5. ✓
- `clamp_refresh_interval(seconds) -> int` — Task 2, 4, 5. ✓
- `run_dashboard_query(db, dashboard) -> dict` with `results[wid] = {columns, rows, error, truncated}` — Task 3 produces; Task 7 (`/query`) returns it; Task 8 (`DashboardViewer`) reads `result.error`/`result.rows`/`result.columns`. ✓
- `create_dashboard` tool artifact shape `{source:"dashboard", dashboard_id, title, datasource_name, widget_count}` — Task 5 produces; Task 10 (`DashboardCard`) reads `artifact.title/datasource_name/widget_count` + `onOpen` uses `artifact.dashboard_id`. ✓
- `DashboardPopup` props `{dashboardId, variant, onClose}` — Task 9 defines; Task 10 + Task 11 consume identically. ✓
- API client fn names `listDashboards/getDashboard/createDashboard/deleteDashboard/queryDashboard` — Task 7 defines; Tasks 8, 9, 11 consume. ✓

No gaps found. Plan ready for execution.
