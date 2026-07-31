# Dashboard Phase 3 — Editable (Hybrid) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make zhanlu dashboards user-editable in place (widget CRUD, drag-reorder, inline SQL editor + live preview, save-as-copy) and give the agent an `update_dashboard` tool — the "Hybrid" editing model.

**Architecture:** Approach A (full-replace PATCH). Backend adds `PATCH /api/dashboards/{id}` (creator-only, reuses `_validate_definition`) + `POST /{id}/preview-sql` (creator-only, reuses `validate_widget_sql`+`render_widget_sql`+`QueryService.execute`) + an `update_dashboard` agent tool mirroring `create_dashboard`. Frontend adds an edit-mode toggle to `DashboardViewer` (draft state, explicit Save/Cancel, SQL drawer, `@dnd-kit` reorder, save-as-copy). `datasource_kb_id` is immutable. No model migration (`created_by_id` already on `TimestampedBase`).

**Tech Stack:** FastAPI + SQLAlchemy + pydantic v2 (backend); React + recharts + `@dnd-kit` + vitest (frontend); sqlite test DB (backend tests); real Aliyun RDS MySQL (E2E).

**Spec:** `docs/superpowers/specs/2026-07-29-dashboard-phase3-editable-design.md`

## Global Constraints

- **Backend test DB convention:** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"` then `python3 -m pytest tests/<file> -v` (explicit paths ONLY — never broad `tests/` or `-k`; no `-n` workers — RAM-limited box OOMs on full collection). sqlite enforces FKs → real Project/KB/User rows required.
- **SQLite connector** reads `kb.api_url` as the file path (fallback `:memory:`). For connectable-KB unit tests, set `api_url` to a temp `.db` file.
- **TimestampedBase** provides `id`/`created_by_id`/`org_id`/`app_id`/`is_deleted`/`created_date`/`updated_date` + `to_dict()`. Datetime cols are `created_date`/`updated_date` (NOT `created_at`).
- **UVICORN_RELOAD=false** — after backend edits: `docker restart zhanlu-backend` (volume-mounted `./backend:/app`). On `.env` change: `docker compose up -d backend` (recreate).
- **Frontend deploy:** `/opt/rebuild-zhanlu.sh --force` (dist can silently go stale vs worktree).
- **recharts test mock** must include `CartesianGrid`+`Legend`+`Tooltip` (chart.jsx references them) or collection fails. `vi.useFakeTimers()` hangs `waitFor` → use `vi.advanceTimersByTimeAsync(0)` + `getByText`.
- **DashboardViewer polling effect** (10–300s clamp + `visibilitychange` pause + `mountedRef`) must stay VERBATIM — do not add `editing` to its dependency array; pause via an `editingRef` guard inside `refresh`.
- **Chart colors** are `hsl(var(--chart-N))` (NEVER hex); tokens exist in `:root` AND `.dark`.
- **Commit:** per-task commits are accepted for dashboard phases (precedent: Phase 1/2 committed per task). Repo general convention is "commit only when asked" — if executing inline, confirm with user before the first commit.
- **RAM-limited:** never run the full backend test suite; per-file only.

---

## File Structure

**Backend:**
- `app/services/dashboard_query.py` — extract `_run_single_sql` shared helper (refactor; preview + drill + widgets share it).
- `app/routers/dashboards.py` — add `PATCH /{id}` + `POST /{id}/preview-sql` + `UpdateDashboardBody`/`PreviewSqlBody` pydantic models.
- `app/services/tool_handlers/dashboard_tools.py` — add `UpdateDashboardArgs` + `_update_dashboard` handler + `UPDATE_DASHBOARD_SCHEMA` + registration.
- `app/services/tool_handlers/__init__.py` — ensure `dashboard_tools` import registers `update_dashboard` (already imports the module; verify).
- `tests/test_dashboards_patch.py` (new), `tests/test_dashboards_preview_sql.py` (new), `tests/test_dashboard_query_single.py` (new), `tests/test_update_dashboard_tool.py` (new).

**Frontend:**
- `src/api/dashboards.js` — add `updateDashboard`, `previewWidgetSql`.
- `src/api/dashboards.test.js` — add tests for the two new functions.
- `src/components/dashboard/WidgetEditorDrawer.jsx` (new) — SQL editor slide-over with Run-preview.
- `src/components/dashboard/SortableWidgetCard.jsx` (new) — `@dnd-kit` sortable wrapper (drag handle + edit/delete).
- `src/components/dashboard/DashboardViewer.jsx` — edit mode (state, toolbar, drawer wiring, polling pause, Save/Cancel/Copy, unsaved guard, dnd context).
- `src/components/dashboard/DashboardViewer.edit.test.jsx` (new), `src/components/dashboard/WidgetEditorDrawer.test.jsx` (new).
- `src/components/dashboard/DashboardPopup.jsx` — pass `canEdit` + `onSaved`/`onCopied`.
- `src/pages/DashboardView.jsx` — pass `canEdit` + `onSaved`/`onCopied` (navigate on copy).
- `package.json` — add `@dnd-kit/core` + `@dnd-kit/sortable`.

**E2E:**
- `backend/tests/test_dashboard_e2e_edit_save.py` (new).

---

## Task 1: Extract `_run_single_sql` shared helper (backend refactor)

**Files:**
- Modify: `app/services/dashboard_query.py` (extract from `_run_one`; update `_run_drill` to use it)
- Test: `tests/test_dashboard_query_single.py`

**Interfaces:**
- Produces: `async def _run_single_sql(db, kb_id, sql, params, dimensions, max_rows, timeout_s) -> dict` returning `{columns, rows, error, truncated}`. Consumed by `run_dashboard_query._run_one`, `_run_drill`, and (Task 3) the preview endpoint.

- [ ] **Step 1: Write the failing test**

`tests/test_dashboard_query_single.py`:
```python
import os, sqlite3, tempfile, uuid
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import pytest
from app.database import Base, engine, SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.dashboard_query import _run_single_sql, run_dashboard_query
import app.models  # noqa: F401

@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)

@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

@pytest.fixture
def sqlite_kb(db):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE metrics (n INTEGER, label TEXT)")
    conn.execute("INSERT INTO metrics VALUES (1, 'a')")
    conn.commit(); conn.close()
    kb = KnowledgeBase(name="t", source_kind="database", db_type="sqlite",
                       api_url=f.name, org_id="default-org", app_id="default-app")
    db.add(kb); db.commit()
    yield kb
    os.unlink(f.name)

@pytest.mark.asyncio
async def test_run_single_sql_returns_rows(sqlite_kb, db):
    r = await _run_single_sql(db, sqlite_kb.id, "SELECT n, label FROM metrics", None, [], 1000, 10)
    assert r["error"] is None
    assert r["columns"] == ["n", "label"]
    assert r["rows"] == [{"n": 1, "label": "a"}]
    assert r["truncated"] is False

@pytest.mark.asyncio
async def test_run_single_sql_rejects_non_readonly(sqlite_kb, db):
    r = await _run_single_sql(db, sqlite_kb.id, "DELETE FROM metrics", None, [], 1000, 10)
    assert r["error"]  # validate_widget_sql raises → caught
    assert r["rows"] == []

@pytest.mark.asyncio
async def test_run_single_sql_unknown_token(sqlite_kb, db):
    r = await _run_single_sql(db, sqlite_kb.id, "SELECT :foo", None, [], 1000, 10)
    assert r["error"]
```

Note: this test uses `pytest-asyncio`. Confirm it's installed (`pip show pytest-asyncio`); the existing `run_dashboard_query` is async and the `/query` endpoint test calls it via TestClient (sync wrapper). If `pytest-asyncio` is unavailable, mark with `@pytest.mark.asyncio` and ensure `asyncio_mode=auto` or install. Alternatively drive via `asyncio.run(...)` inside a sync test to avoid the plugin dependency — **use the `asyncio.run` form to stay plugin-free:**
```python
def test_run_single_sql_returns_rows(sqlite_kb, db):
    import asyncio
    r = asyncio.run(_run_single_sql(db, sqlite_kb.id, "SELECT n, label FROM metrics", None, [], 1000, 10))
    assert r["error"] is None
    assert r["columns"] == ["n", "label"]
    assert r["rows"] == [{"n": 1, "label": "a"}]
```
(Use the `asyncio.run` sync-wrapper form for all three tests to avoid the plugin dependency.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_dashboard_query_single.py -v`
Expected: FAIL with `ImportError: cannot import name '_run_single_sql'`.

- [ ] **Step 3: Implement the refactor**

In `app/services/dashboard_query.py`, add a module-level helper above `run_dashboard_query`:
```python
async def _run_single_sql(db, kb_id, sql, params, dimensions, max_rows, timeout_s) -> dict:
    """Render + validate + execute ONE widget SQL. Shared by widgets, drill, preview.

    Returns {columns, rows, error, truncated}. Render/validation/DB errors are
    caught and reported in `error` (never raised) so one bad query cannot abort
    a dashboard or a preview round-trip.
    """
    try:
        rendered = render_widget_sql(sql, params, dimensions)
    except ValueError as e:
        return {"columns": [], "rows": [], "error": str(e), "truncated": False}
    try:
        res = await asyncio.to_thread(
            lambda: QueryService(db).execute(kb_id, rendered, max_rows=max_rows, timeout_s=timeout_s)
        )
        return {
            "columns": list(res["rows"][0].keys()) if res["rows"] else [],
            "rows": res["rows"],
            "error": None,
            "truncated": res.get("truncated", False),
        }
    except Exception as e:
        return {"columns": [], "rows": [], "error": str(e), "truncated": False}
```

Replace the body of `_run_one` (inside `run_dashboard_query`) to delegate:
```python
    async def _run_one(widget: dict) -> tuple[str, dict]:
        wid = widget.get("id") or widget.get("title")
        dimensions = (widget.get("options") or {}).get("dimensions") or []
        return wid, await _run_single_sql(
            db, dashboard.datasource_kb_id, widget.get("sql") or "",
            params, dimensions, max_rows, timeout_s)
```

Replace `_run_drill`'s execute block to use `_run_single_sql` + attach metadata:
```python
async def _run_drill(db, dashboard, params, drill, max_rows, timeout_s) -> dict:
    src_id = drill.get("widget_id")
    value = drill.get("value", "")
    widgets = (dashboard.definition or {}).get("widgets", []) or []
    src = next((w for w in widgets if (w.get("id") or w.get("title")) == src_id), None)
    base = {"columns": [], "rows": [], "error": None, "truncated": False,
            "source_widget_id": src_id, "drill_value": value}
    if not src:
        base["error"] = "Drill source widget not found"
        return base
    drill_opts = (src.get("options") or {}).get("drill") or {}
    drill_sql = drill_opts.get("sql")
    if not drill_sql:
        base["error"] = "Widget has no drill SQL"
        return base
    dimensions = (src.get("options") or {}).get("dimensions") or []
    result = await _run_single_sql(
        db, dashboard.datasource_kb_id, drill_sql,
        {**params, "drill_value": value}, dimensions, max_rows, timeout_s)
    base.update(result)  # columns/rows/error/truncated from the shared helper
    return base
```
(Keep the `source_widget_id`/`drill_value` keys by setting them on `base` before `base.update(result)` — `result` does not contain those keys.)

- [ ] **Step 4: Run tests to verify pass + no regression**

Run: `python3 -m pytest tests/test_dashboard_query_single.py tests/test_dashboards_api.py tests/test_dashboard_query_tokens.py tests/test_dashboard_query_params_drill.py -v`
Expected: all PASS (the new tests pass; existing query/drill/api tests unchanged — the refactor preserves behavior).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/dashboard_query.py backend/tests/test_dashboard_query_single.py
git commit -m "refactor(dashboard): extract _run_single_sql shared helper"
```

---

## Task 2: `PATCH /api/dashboards/{id}` endpoint (creator-only)

**Files:**
- Modify: `app/routers/dashboards.py` (add `UpdateDashboardBody` + `PATCH /{id}`)
- Test: `tests/test_dashboards_patch.py`

**Interfaces:**
- Produces: `PATCH /api/dashboards/{dashboard_id}` accepting `UpdateDashboardBody{name?, description?, refresh_interval_seconds?, definition?}`; 200 on success (returns `to_dict()`), 404 (not found/wrong org), 403 (not creator), 400 (invalid definition). `datasource_kb_id` is NOT in the body schema (immutable).

- [ ] **Step 1: Write the failing test**

`tests/test_dashboards_patch.py`:
```python
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid, pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.routers.dashboards import router as dashboards_router
import app.models  # noqa: F401

@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)

@pytest.fixture
def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

def _client(db, user):
    app = FastAPI(); app.include_router(dashboards_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)

def _user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u); db.commit(); return u

def _kb(db):
    kb = KnowledgeBase(name="db", source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb); db.commit(); return kb

def _good(): return {"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}}

def _make(db, client):
    r = client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": _kb(db).id,
                    "definition": {"widgets": [_good()]}})
    return r.json()["id"]

def test_creator_can_rename(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"name": "renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"

def test_creator_can_replace_definition(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    new_w = {"id": "w2", "type": "line", "title": "B", "sql": "SELECT 2 AS n", "options": {}}
    r = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": [new_w]}})
    assert r.status_code == 200
    assert r.json()["definition"]["widgets"][0]["id"] == "w2"

def test_patch_rejects_invalid_definition(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": []}})
    assert r.status_code == 400
    r2 = c.patch(f"/api/dashboards/{did}", json={"definition": {"widgets": [
        {"id": "w", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}})
    assert r2.status_code == 400

def test_refresh_interval_clamped(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    r = c.patch(f"/api/dashboards/{did}", json={"refresh_interval_seconds": 5})
    assert r.json()["refresh_interval_seconds"] == 10
    r2 = c.patch(f"/api/dashboards/{did}", json={"refresh_interval_seconds": 999})
    assert r2.json()["refresh_interval_seconds"] == 300

def test_non_creator_forbidden(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other = _user(db)  # different user, same org
    c2 = _client(db, other)
    r = c2.patch(f"/api/dashboards/{did}", json={"name": "hacked"})
    assert r.status_code == 403

def test_wrong_org_404(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other = User(id=str(uuid.uuid4()), email="o@t.io", full_name="o", role="user",
                 password_hash="x", org_id="other-org", app_id="default-app")
    db.add(other); db.commit()
    c2 = _client(db, other)
    assert c2.patch(f"/api/dashboards/{did}", json={"name": "x"}).status_code == 404

def test_datasource_immutable(db):
    u = _user(db); c = _client(db, u); did = _make(db, c)
    other_kb = _kb(db)
    r = c.patch(f"/api/dashboards/{did}", json={"datasource_kb_id": other_kb.id})
    assert r.status_code == 200  # ignored, not 400
    assert r.json()["datasource_kb_id"] != other_kb.id  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_dashboards_patch.py -v`
Expected: FAIL with 405 Method Not Allowed (no PATCH route).

- [ ] **Step 3: Implement the endpoint**

In `app/routers/dashboards.py`, add the pydantic model + handler (after `delete_dashboard`, before `query_dashboard`):
```python
class UpdateDashboardBody(BaseModel):
    name: str | None = None
    description: str | None = None
    refresh_interval_seconds: int | None = None
    definition: dict | None = None


@router.patch("/{dashboard_id}")
def update_dashboard(dashboard_id: str, body: UpdateDashboardBody,
                     db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can edit this dashboard")
    if body.definition is not None:
        _validate_definition(body.definition)
        d.definition = body.definition
    if body.refresh_interval_seconds is not None:
        d.refresh_interval_seconds = clamp_refresh_interval(body.refresh_interval_seconds)
    if body.name is not None:
        d.name = body.name
    if body.description is not None:
        d.description = body.description
    # datasource_kb_id / org_id / app_id / project_id / project / created_by_id are immutable.
    db.commit()
    db.refresh(d)
    return d.to_dict()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_dashboards_patch.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/dashboards.py backend/tests/test_dashboards_patch.py
git commit -m "feat(dashboard): PATCH /{id} creator-only update endpoint"
```

---

## Task 3: `POST /api/dashboards/{id}/preview-sql` endpoint (creator-only, live SQL)

**Files:**
- Modify: `app/routers/dashboards.py` (add `PreviewSqlBody` + `POST /{id}/preview-sql`)
- Modify: `app/services/dashboard_query.py` (export `_run_single_sql` — already module-level from Task 1)
- Test: `tests/test_dashboards_preview_sql.py`

**Interfaces:**
- Produces: `POST /api/dashboards/{dashboard_id}/preview-sql` accepting `PreviewSqlBody{sql, params?}`; returns `{columns, rows, truncated, error}`. 404 (not found/wrong org), 403 (not creator), 400 (non-read-only/multi-stmt/unknown-token SQL), 200 (success OR execution error reported in `error` field).

- [ ] **Step 1: Write the failing test**

`tests/test_dashboards_preview_sql.py`:
```python
import os, sqlite3, tempfile
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid, pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.routers.dashboards import router as dashboards_router
import app.models  # noqa: F401

@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)

@pytest.fixture
def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

def _client(db, user):
    app = FastAPI(); app.include_router(dashboards_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)

def _user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u); db.commit(); return u

def _sqlite_kb(db):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE metrics (n INTEGER, label TEXT)")
    conn.execute("INSERT INTO metrics VALUES (1, 'a')"); conn.commit(); conn.close()
    kb = KnowledgeBase(name="t", source_kind="database", db_type="sqlite",
                       api_url=f.name, org_id="default-org", app_id="default-app")
    db.add(kb); db.commit(); return kb, f.name

def _make(db, client, kb_id):
    r = client.post("/api/dashboards", json={"name": "d", "datasource_kb_id": kb_id,
                    "definition": {"widgets": [{"id": "w1", "type": "kpi", "title": "A",
                    "sql": "SELECT 1 AS n", "options": {}}]}})
    return r.json()["id"]

def test_preview_returns_rows(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT n, label FROM metrics"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["n", "label"]
    assert body["rows"] == [{"n": 1, "label": "a"}]
    assert body["error"] is None
    os.unlink(path)

def test_preview_rejects_non_readonly(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "DELETE FROM metrics"})
    assert r.status_code == 400
    os.unlink(path)

def test_preview_rejects_multi_statement(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT 1; SELECT 2"})
    assert r.status_code == 400
    os.unlink(path)

def test_preview_non_creator_forbidden(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    other = _user(db)
    c2 = _client(db, other)
    r = c2.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT 1"})
    assert r.status_code == 403
    os.unlink(path)

def test_preview_db_error_is_200_with_error_field(db):
    u = _user(db); c = _client(db, u); kb, path = _sqlite_kb(db); did = _make(db, c, kb.id)
    # valid read-only SQL against a table that does not exist -> execution error
    r = c.post(f"/api/dashboards/{did}/preview-sql", json={"sql": "SELECT * FROM nope"})
    assert r.status_code == 200
    assert r.json()["error"]  # execution error reported, not 5xx
    os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_dashboards_preview_sql.py -v`
Expected: FAIL with 404 (no preview-sql route).

- [ ] **Step 3: Implement the endpoint**

In `app/routers/dashboards.py`, update the import line and add the model + handler (after `query_dashboard`):
```python
from app.services.dashboard_query import (
    validate_widget_sql, clamp_refresh_interval, run_dashboard_query,
    _run_single_sql,
)
```
```python
class PreviewSqlBody(BaseModel):
    sql: str
    params: dict | None = None


@router.post("/{dashboard_id}/preview-sql")
async def preview_widget_sql(dashboard_id: str, body: PreviewSqlBody,
                             db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can preview SQL on this dashboard")
    validate_widget_sql(body.sql)  # raises ValueError → 400 by FastAPI? No — raise HTTPException(400)
    # Render tokens (unknown token → ValueError → 400).
    # Resolve KB controls as upper bounds (same as run_dashboard_query).
    max_rows, timeout_s = 1000, 10
    try:
        from app.models.knowledge_base import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == d.datasource_kb_id).first()
        if kb is not None:
            max_rows = int(getattr(kb, "max_rows_per_query", 1000) or 1000)
            timeout_s = int(getattr(kb, "timeout_seconds", 10) or 10)
    except Exception:
        pass
    result = await _run_single_sql(db, d.datasource_kb_id, body.sql, body.params or {}, [], max_rows, timeout_s)
    return result  # {columns, rows, error, truncated}
```

**Note on 400 handling:** `validate_widget_sql` raises `ValueError`, not `HTTPException`. FastAPI returns 500 on uncaught `ValueError`. Wrap validation explicitly:
```python
    try:
        validate_widget_sql(body.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```
And `_run_single_sql` catches render errors internally (returns `error` field, not raise) — but `render_widget_sql` raises `ValueError` on unknown tokens, which `_run_single_sql` catches → returns `{error}`. That would make unknown-token a 200-with-error, but the test `test_preview_rejects_non_readonly` expects 400 for `DELETE`. Since `validate_widget_sql(body.sql)` runs FIRST (before `_run_single_sql`) and is wrapped to 400, `DELETE`/multi-stmt → 400. Unknown tokens pass `validate_widget_sql` (token isn't a leading-verb check) but fail in `render_widget_sql` inside `_run_single_sql` → 200-with-error. That's acceptable (unknown token is a runtime render issue, reported as error). The test does not assert unknown-token = 400, so this is fine. Keep the wrapped `validate_widget_sql` → 400 for read-only/multi-stmt; everything else flows through `_run_single_sql` → 200-with-error.

Final handler body:
```python
@router.post("/{dashboard_id}/preview-sql")
async def preview_widget_sql(dashboard_id: str, body: PreviewSqlBody,
                             db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can preview SQL on this dashboard")
    try:
        validate_widget_sql(body.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    max_rows, timeout_s = 1000, 10
    try:
        from app.models.knowledge_base import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == d.datasource_kb_id).first()
        if kb is not None:
            max_rows = int(getattr(kb, "max_rows_per_query", 1000) or 1000)
            timeout_s = int(getattr(kb, "timeout_seconds", 10) or 10)
    except Exception:
        pass
    return await _run_single_sql(db, d.datasource_kb_id, body.sql, body.params or {}, [], max_rows, timeout_s)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_dashboards_preview_sql.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/dashboards.py backend/tests/test_dashboards_preview_sql.py
git commit -m "feat(dashboard): POST /{id}/preview-sql creator-only live SQL preview"
```

---

## Task 4: `update_dashboard` agent tool

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py` (add `UpdateDashboardArgs` + `_update_dashboard` + `UPDATE_DASHBOARD_SCHEMA` + registration)
- Verify: `app/services/tool_handlers/__init__.py` imports `dashboard_tools` (it already does for `create_dashboard`)
- Test: `tests/test_update_dashboard_tool.py`

**Interfaces:**
- Produces: `update_dashboard` tool registered with `registry.register(name="update_dashboard", ...)`. Handler signature `async def _update_dashboard(args, db, user_id, context) -> dict`. Args: `UpdateDashboardArgs{dashboard_id, title?, description?, refresh_interval_seconds?, widgets?}` (widgets = full `list[WidgetSpec]`). Returns `{success, dashboard, artifact}` on success, `{success:false, error}` on failure.

- [ ] **Step 1: Write the failing test**

`tests/test_update_dashboard_tool.py`:
```python
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid, pytest
from app.database import Base, engine, SessionLocal
from app.models.dashboard import Dashboard
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.tool_handlers.dashboard_tools import _update_dashboard
import app.models  # noqa: F401

@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)

@pytest.fixture
def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

def _user(db, uid=None):
    u = User(id=uid or str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u); db.commit(); return u

def _kb(db):
    kb = KnowledgeBase(name="db", source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb); db.commit(); return kb

def _dashboard(db, user, kb):
    d = Dashboard(name="orig", datasource_kb_id=kb.id, project="global",
                  definition={"widgets": [{"id": "w1", "type": "kpi", "title": "A",
                  "sql": "SELECT 1 AS n", "options": {}}]},
                  refresh_interval_seconds=30, org_id="default-org", app_id="default-app",
                  created_by_id=user.id)
    db.add(d); db.commit(); return d

@pytest.mark.asyncio
async def test_creator_updates_title_and_widgets(db):
    # use asyncio.run form instead if pytest-asyncio absent:
    import asyncio
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    args = {"dashboard_id": d.id, "title": "new", "widgets": [
        {"id": "w2", "type": "line", "title": "B", "sql": "SELECT 2 AS n", "options": {}}]}
    r = await _update_dashboard(args, db, u.id, {"org_id": "default-org"})
    assert r["success"] is True
    assert r["dashboard"]["name"] == "new"
    assert r["dashboard"]["definition"]["widgets"][0]["id"] == "w2"
    assert r["artifact"]["widget_count"] == 1

def test_non_creator_denied(db):
    import asyncio
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    other = _user(db)
    r = asyncio.run(_update_dashboard({"dashboard_id": d.id, "title": "x"}, db, other.id, {"org_id": "default-org"}))
    assert r["success"] is False
    assert "creator" in r["error"].lower()

def test_invalid_widget_sql_rejected(db):
    import asyncio
    u = _user(db); kb = _kb(db); d = _dashboard(db, u, kb)
    args = {"dashboard_id": d.id, "widgets": [
        {"id": "w", "type": "kpi", "title": "x", "sql": "DROP TABLE t", "options": {}}]}
    r = asyncio.run(_update_dashboard(args, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is False
    assert "invalid" in r["error"].lower() or "sql" in r["error"].lower()

def test_not_found(db):
    import asyncio
    u = _user(db)
    r = asyncio.run(_update_dashboard({"dashboard_id": str(uuid.uuid4()), "title": "x"}, db, u.id, {"org_id": "default-org"}))
    assert r["success"] is False
```
(Use `asyncio.run(...)` for the non-async test functions; for the first test keep `async def` + `@pytest.mark.asyncio` ONLY if pytest-asyncio is installed — otherwise convert it to `asyncio.run` too. Prefer the `asyncio.run` form everywhere to stay plugin-free.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_update_dashboard_tool.py -v`
Expected: FAIL with `ImportError: cannot import name '_update_dashboard'`.

- [ ] **Step 3: Implement the tool**

In `app/services/tool_handlers/dashboard_tools.py`, add after `CreateDashboardArgs`/`_create_dashboard` (before the registration block):
```python
class UpdateDashboardArgs(BaseModel):
    dashboard_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    refresh_interval_seconds: Optional[int] = None
    widgets: Optional[list[WidgetSpec]] = None


async def _update_dashboard(args: dict, db, user_id, context: dict | None = None) -> dict:
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    try:
        parsed = UpdateDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid update args: {e}"}

    d = db.query(Dashboard).filter(
        Dashboard.id == parsed.dashboard_id,
        Dashboard.org_id == org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        return {"success": False, "error": "Dashboard not found."}
    if d.created_by_id != user_id:
        return {"success": False, "error": "Only the dashboard creator can edit it."}

    if parsed.title is not None:
        d.name = parsed.title
    if parsed.description is not None:
        d.description = parsed.description
    if parsed.refresh_interval_seconds is not None:
        d.refresh_interval_seconds = clamp_refresh_interval(parsed.refresh_interval_seconds)
    if parsed.widgets is not None:
        # WidgetSpec already validates each widget's sql + drill (read-only guard).
        d.definition = {"widgets": [w.model_dump() for w in parsed.widgets]}
    # datasource_kb_id is NOT changeable.
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
        },
    }
```

Add the schema + registration (after `CREATE_DASHBOARD_SCHEMA`/its `registry.register`):
```python
UPDATE_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_dashboard",
        "description": (
            "Edit an EXISTING live dashboard. Provide only the fields to change: "
            "title, description, refresh_interval_seconds, and/or widgets (FULL "
            "updated widget list — replaces all widgets). Fetch the current "
            "dashboard first (list_dashboards / get detail) to know existing "
            "widgets, then send the full updated list. datasource_kb_id CANNOT "
            "be changed (to rebind, create a new dashboard). Only the dashboard's "
            "CREATOR can edit it. If the user's edit intent is ambiguous (which "
            "widget, what change, new SQL), ask in ONE message with numbered "
            "options and WAIT — do NOT guess. Widget SQL must be read-only "
            "(SELECT/WITH only, single statement)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"type": "string", "description": "The id of the dashboard to edit."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "refresh_interval_seconds": {"type": "integer", "description": "Poll interval, 10..300."},
                "widgets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["kpi", "line", "bar", "pie", "table", "area", "stacked-bar", "scatter", "gauge", "radar"]},
                            "title": {"type": "string"},
                            "sql": {"type": "string", "description": "Read-only SELECT/WITH SQL for this widget."},
                            "options": {"type": "object"},
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
            },
            "required": ["dashboard_id"],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="update_dashboard",
        schema=UPDATE_DASHBOARD_SCHEMA,
        handler=_update_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Edit an existing live dashboard (creator only).",
        emoji="✏️",
        max_result_size_chars=4_000,
    )
except Exception:
    logger.exception("Failed to register update_dashboard tool")
```

Verify `app/services/tool_handlers/__init__.py` imports `dashboard_tools` (it already does for `create_dashboard`) — no change needed; confirm by grep. Optionally add `update_dashboard` to `DEFAULT_USER_AGENT_TOOLS` + `general_assistant` in `tool_registry.py` if `create_dashboard` is listed there (mirror it).

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_update_dashboard_tool.py tests/test_create_dashboard_tool.py -v`
Expected: all PASS (new tool tests + existing create-tool regression).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/tool_handlers/dashboard_tools.py backend/tests/test_update_dashboard_tool.py
git commit -m "feat(dashboard): update_dashboard agent tool (creator-only edit)"
```

---

## Task 5: Frontend API client — `updateDashboard` + `previewWidgetSql`

**Files:**
- Modify: `src/api/dashboards.js`
- Test: `src/api/dashboards.test.js`

**Interfaces:**
- Produces: `updateDashboard(id, body)` → `PATCH /api/dashboards/{id}` (returns dashboard dict); `previewWidgetSql(id, {sql, params})` → `POST /api/dashboards/{id}/preview-sql` (returns `{columns, rows, truncated, error}`).

- [ ] **Step 1: Write the failing tests**

Append to `src/api/dashboards.test.js` (add `updateDashboard, previewWidgetSql` to the import from `./dashboards`):
```javascript
  it('updateDashboard patches body', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd1', name: 'x' }) });
    await updateDashboard('d1', { name: 'x', definition: { widgets: [] } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1');
    expect(opts.method).toBe('PATCH');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ name: 'x', definition: { widgets: [] } });
  });

  it('previewWidgetSql posts sql + params', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false }) });
    const r = await previewWidgetSql('d1', { sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1/preview-sql');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } });
    expect(r.columns).toEqual(['n']);
  });

  it('previewWidgetSql defaults params to empty', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ columns: [], rows: [], error: null, truncated: false }) });
    await previewWidgetSql('d1', { sql: 'SELECT 1' });
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({ sql: 'SELECT 1' });
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/dashboards.test.js`
Expected: FAIL — `updateDashboard is not defined` (not imported).

- [ ] **Step 3: Implement**

Append to `src/api/dashboards.js`:
```javascript
export function updateDashboard(id, body) {
  return authFetch(`${BASE}/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(j);
}

export function previewWidgetSql(id, { sql, params } = {}) {
  const body = params ? { sql, params } : { sql };
  return authFetch(`${BASE}/${id}/preview-sql`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(j);
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/dashboards.test.js`
Expected: all PASS (existing 7 + 3 new = 10).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/api/dashboards.js frontend/src/api/dashboards.test.js
git commit -m "feat(dashboard): updateDashboard + previewWidgetSql API client"
```

---

## Task 6: `WidgetEditorDrawer.jsx` — SQL editor slide-over with live preview

**Files:**
- Create: `src/components/dashboard/WidgetEditorDrawer.jsx`
- Test: `src/components/dashboard/WidgetEditorDrawer.test.jsx`

**Interfaces:**
- Produces: default-export `WidgetEditorDrawer({ widget, dashboardId, params, onApply, onCancel })`. `widget` = the draft widget being edited (or `null` for add). `params` = `{from?, to?, filters?}` for preview. `onApply(widget)` / `onCancel()`. Internal fields: title, type (10 options), sql (textarea), options (JSON textarea). Run-preview calls `previewWidgetSql(dashboardId, {sql, params})` → renders result table + error. Apply disabled when sql empty or options JSON invalid.

- [ ] **Step 1: Write the failing test**

`src/components/dashboard/WidgetEditorDrawer.test.jsx`:
```javascript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
const previewWidgetSql = vi.fn();
vi.mock('@/api/dashboards', () => ({ previewWidgetSql: (...a) => previewWidgetSql(...a) }));

import WidgetEditorDrawer from './WidgetEditorDrawer';

beforeEach(() => { previewWidgetSql.mockReset(); });

const WIDGET = { id: 'w1', type: 'kpi', title: 'A', sql: 'SELECT 1 AS n', options: { unit: '%' } };

describe('WidgetEditorDrawer', () => {
  it('renders fields prefilled from widget', () => {
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    expect(screen.getByDisplayValue('A')).toBeInTheDocument();
    expect(screen.getByDisplayValue('SELECT 1 AS n')).toBeInTheDocument();
    expect(screen.getByText('%')).toBeInTheDocument(); // options JSON contains %
  });

  it('Run preview calls previewWidgetSql and renders columns + rows', async () => {
    previewWidgetSql.mockResolvedValue({ columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false });
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{ from: '2026-07-01' }} onApply={() => {}} onCancel={() => {}} />);
    fireEvent.click(screen.getByText('Run preview'));
    await waitFor(() => expect(previewWidgetSql).toHaveBeenCalledWith('d1', { sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } }));
    await waitFor(() => expect(screen.getByText('n')).toBeInTheDocument()); // column header
    expect(screen.getByText('1')).toBeInTheDocument(); // row value
  });

  it('preview error renders error line', async () => {
    previewWidgetSql.mockResolvedValue({ columns: [], rows: [], error: 'boom', truncated: false });
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    fireEvent.click(screen.getByText('Run preview'));
    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
  });

  it('Apply disabled when sql empty', () => {
    render(<WidgetEditorDrawer widget={null} dashboardId="d1" params={{}} onApply={() => {}} onCancel={() => {}} />);
    expect(screen.getByText('Apply')).toBeDisabled();
  });

  it('Apply calls onApply with widget (new id generated when blank)', () => {
    const onApply = vi.fn();
    render(<WidgetEditorDrawer widget={null} dashboardId="d1" params={{}} onApply={onApply} onCancel={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText(/title/i), { target: { value: 'New' } });
    fireEvent.change(screen.getByPlaceholderText(/sql/i), { target: { value: 'SELECT 1 AS n' } });
    fireEvent.click(screen.getByText('Apply'));
    expect(onApply).toHaveBeenCalledTimes(1);
    const w = onApply.mock.calls[0][0];
    expect(w.title).toBe('New');
    expect(w.sql).toBe('SELECT 1 AS n');
    expect(w.id).toMatch(/^w_/);
  });

  it('Cancel calls onCancel', () => {
    const onCancel = vi.fn();
    render(<WidgetEditorDrawer widget={WIDGET} dashboardId="d1" params={{}} onApply={() => {}} onCancel={onCancel} />);
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/WidgetEditorDrawer.test.jsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/components/dashboard/WidgetEditorDrawer.jsx`:
```jsx
import { useState } from 'react';
import { previewWidgetSql } from '@/api/dashboards';

const TYPES = ['kpi', 'line', 'bar', 'pie', 'table', 'area', 'stacked-bar', 'scatter', 'gauge', 'radar'];

export default function WidgetEditorDrawer({ widget, dashboardId, params, onApply, onCancel }) {
  const [title, setTitle] = useState(widget?.title || '');
  const [type, setType] = useState(widget?.type || 'table');
  const [sql, setSql] = useState(widget?.sql || '');
  const [optionsText, setOptionsText] = useState(JSON.stringify(widget?.options || {}, null, 2));
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);

  let optionsError = '';
  let parsedOptions = {};
  try { parsedOptions = JSON.parse(optionsText || '{}'); } catch (e) { optionsError = 'Invalid JSON: ' + e.message; }

  const canApply = sql.trim() !== '' && !optionsError;

  const runPreview = async () => {
    setPreviewing(true); setPreview(null);
    try { setPreview(await previewWidgetSql(dashboardId, { sql, params })); }
    catch (e) { setPreview({ columns: [], rows: [], error: String(e), truncated: false }); }
    finally { setPreviewing(false); }
  };

  const apply = () => {
    if (!canApply) return;
    onApply({
      id: widget?.id || ('w_' + Date.now()),
      type, title: title.trim() || 'Untitled', sql, options: parsedOptions,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex" role="dialog" aria-label="Widget editor">
      <div className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <div className="relative ml-auto flex h-full w-full max-w-md flex-col gap-3 overflow-auto bg-background p-4 shadow-xl">
        <h3 className="text-sm font-semibold">{widget ? 'Edit widget' : 'Add widget'}</h3>
        <label className="text-xs text-muted-foreground">Title
          <input className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="text-xs text-muted-foreground">Type
          <select className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">SQL (read-only SELECT/WITH)
          <textarea className="mt-1 h-40 w-full rounded-md border border-border bg-transparent p-2 font-mono text-xs" placeholder="SQL" spellCheck={false} value={sql} onChange={(e) => setSql(e.target.value)} />
        </label>
        <label className="text-xs text-muted-foreground">Options (JSON)
          <textarea className="mt-1 h-24 w-full rounded-md border border-border bg-transparent p-2 font-mono text-xs" value={optionsText} onChange={(e) => setOptionsText(e.target.value)} />
        </label>
        {optionsError && <p className="text-xs text-destructive">{optionsError}</p>}
        <button onClick={runPreview} disabled={previewing || !sql.trim()} className="rounded-md border border-border px-3 py-1 text-xs hover:bg-secondary">
          {previewing ? 'Running…' : 'Run preview'}
        </button>
        {preview && (
          <div className="rounded-md border border-border p-2 text-xs">
            {preview.error ? (
              <p className="text-destructive">{preview.error}</p>
            ) : (
              <div className="overflow-auto">
                <table className="w-full">
                  <thead><tr>{preview.columns.map((c) => <th key={c} className="text-left font-medium">{c}</th>)}</tr></thead>
                  <tbody>{preview.rows.slice(0, 20).map((r, i) => (
                    <tr key={i}>{preview.columns.map((c) => <td key={c}>{String(r[c])}</td>)}</tr>
                  ))}</tbody>
                </table>
                {preview.truncated && <p className="text-muted-foreground">Truncated…</p>}
              </div>
            )}
          </div>
        )}
        <div className="mt-auto flex gap-2">
          <button onClick={apply} disabled={!canApply} className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-50">Apply</button>
          <button onClick={onCancel} className="rounded-md border border-border px-3 py-1 text-xs">Cancel</button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/WidgetEditorDrawer.test.jsx`
Expected: all 6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/WidgetEditorDrawer.jsx frontend/src/components/dashboard/WidgetEditorDrawer.test.jsx
git commit -m "feat(dashboard): WidgetEditorDrawer with live SQL preview"
```

---

## Task 7: DashboardViewer edit-mode core (state, toolbar, drawer wiring, polling pause, Save/Cancel/Copy)

**Files:**
- Modify: `src/components/dashboard/DashboardViewer.jsx`
- Test: `src/components/dashboard/DashboardViewer.edit.test.jsx`

**Interfaces:**
- Produces: `DashboardViewer` gains props `canEdit = false`, `onSaved`, `onCopied`. New internal state `editing`/`draft`/`dirty`/`editingWidget`. Polling pauses via `editingRef` guard inside `refresh` (polling effect deps UNCHANGED). Edit-mode toolbar: Save/Cancel/Save-as-copy/Add-widget. Edit button (pencil) shown when `canEdit && !editing`. Widget cards get edit/delete affordances in edit mode. `WidgetEditorDrawer` wired via `editingWidget`. Unsaved-changes `beforeunload` guard while `editing && dirty`.

- [ ] **Step 1: Write the failing test**

`src/components/dashboard/DashboardViewer.edit.test.jsx`:
```javascript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
const queryDashboard = vi.fn();
const updateDashboard = vi.fn();
const createDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({
  queryDashboard: (...a) => queryDashboard(...a),
  updateDashboard: (...a) => updateDashboard(...a),
  createDashboard: (...a) => createDashboard(...a),
}));
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />, Legend: () => <div />, Tooltip: () => <div />,
  LineChart: () => <div />, BarChart: () => <div />, PieChart: () => <div />,
  AreaChart: () => <div />, ScatterChart: () => <div />, RadarChart: () => <div />,
  RadialBarChart: () => <div />, Line: () => <div />, Bar: () => <div />, Pie: () => <div />,
  Area: () => <div />, Scatter: () => <div />, Radar: () => <div />, PolarGrid: () => <div />,
  PolarAngleAxis: () => <div />, PolarRadiusAxis: () => <div />, Cell: () => <div />,
}));
import WidgetEditorDrawer from './WidgetEditorDrawer';
vi.mock('./WidgetEditorDrawer', () => ({ default: vi.fn(() => null) }));

import DashboardViewer from './DashboardViewer';

const DASHBOARD = {
  id: 'd1', name: 'Sales', datasource_kb_id: 'kb1', project_id: 'p1', project: 'p1',
  refresh_interval_seconds: 30, created_by_id: 'u1',
  definition: { widgets: [{ id: 'w1', type: 'table', title: 'T', sql: 'SELECT 1 AS n', options: {} }] },
};

beforeEach(() => {
  queryDashboard.mockReset(); updateDashboard.mockReset(); createDashboard.mockReset();
  queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
});

describe('DashboardViewer edit mode', () => {
  it('hides Edit button when canEdit=false', () => {
    render(<DashboardViewer dashboard={DASHBOARD} polling={false} />);
    expect(screen.queryByText('Edit')).not.toBeInTheDocument();
  });

  it('shows Edit button when canEdit=true', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    expect(screen.getByText('Edit')).toBeInTheDocument();
  });

  it('entering edit mode pauses polling (no queryDashboard calls after enter)', async () => {
    queryDashboard.mockClear();
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled()); // initial mount
    queryDashboard.mockClear();
    fireEvent.click(screen.getByText('Edit'));
    // no further polling while editing
    await new Promise((r) => setTimeout(r, 10));
    expect(queryDashboard).not.toHaveBeenCalled();
  });

  it('Save calls updateDashboard with draft and onSaved', async () => {
    updateDashboard.mockResolvedValue({ ...DASHBOARD, name: 'renamed' });
    const onSaved = vi.fn();
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} onSaved={onSaved} />);
    fireEvent.click(screen.getByText('Edit'));
    // rename via the editable header input
    fireEvent.change(screen.getByDisplayValue('Sales'), { target: { value: 'renamed' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(updateDashboard).toHaveBeenCalled());
    const [id, body] = updateDashboard.mock.calls[0];
    expect(id).toBe('d1');
    expect(body.name).toBe('renamed');
    expect(onSaved).toHaveBeenCalled();
  });

  it('Cancel with dirty shows confirm then discards', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.change(screen.getByDisplayValue('Sales'), { target: { value: 'x' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByText('Edit')).toBeInTheDocument(); // back to view mode
    confirmSpy.mockRestore();
  });

  it('Save as copy calls createDashboard with (copy) name + draft', async () => {
    createDashboard.mockResolvedValue({ id: 'd2', name: 'Sales (copy)' });
    const onCopied = vi.fn();
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} onCopied={onCopied} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByText('Save as copy'));
    await waitFor(() => expect(createDashboard).toHaveBeenCalled());
    const body = createDashboard.mock.calls[0][0];
    expect(body.name).toBe('Sales (copy)');
    expect(body.datasource_kb_id).toBe('kb1');
    expect(body.definition).toEqual(DASHBOARD.definition);
    expect(onCopied).toHaveBeenCalled();
  });

  it('delete widget removes from draft', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByLabelText('Delete widget'));
    expect(screen.queryByText('T')).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.edit.test.jsx`
Expected: FAIL — no Edit button / `canEdit` prop not honored.

- [ ] **Step 3: Implement**

In `src/components/dashboard/DashboardViewer.jsx`:

1. Update imports (add `Pencil`, `Plus`, `Trash2` from lucide-react; add `updateDashboard`, `createDashboard` to the api import; add `useRef` if not present; import `WidgetEditorDrawer`):
```javascript
import { useEffect, useRef, useState, useCallback } from 'react';
import { RefreshCw, Download, Share2, Pencil, Plus, Trash2 } from 'lucide-react';
import { queryDashboard, updateDashboard, createDashboard } from '@/api/dashboards';
import html2canvas from 'html2canvas';
import WidgetShell from './widgets/WidgetShell';
// ... existing widget imports ...
import WidgetEditorDrawer from './WidgetEditorDrawer';
```

2. Update the component signature + add edit state:
```javascript
export default function DashboardViewer({ dashboard, polling = true, initialDateRange, initialFilters, canEdit = false, onSaved, onCopied }) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateRange, setDateRange] = useState(() => initialDateRange || presetToRange('30d'));
  const timerRef = useRef(null);
  const visibleRef = useRef(true);
  const mountedRef = useRef(true);
  const initRef = useRef(false);
  const paramsRef = useRef({});

  const [filters, setFilters] = useState(() => initialFilters || {});
  const [drill, setDrill] = useState(null);

  // --- edit mode ---
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [editingWidget, setEditingWidget] = useState(null); // widget id or "__new__"
  const [headerName, setHeaderName] = useState('');
  const editingRef = useRef(false);
  useEffect(() => { editingRef.current = editing; }, [editing]);

  // beforeunload guard while dirty
  useEffect(() => {
    if (!editing || !dirty) return;
    const h = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', h);
    return () => window.removeEventListener('beforeunload', h);
  }, [editing, dirty]);
```

3. Guard `refresh` so polling no-ops while editing (add `force` opt; keep deps UNCHANGED):
```javascript
  const refresh = useCallback(async (overrideParams, opts) => {
    const force = opts && opts.force;
    if (!dashboard?.id || !visibleRef.current) return;
    if (!force && editingRef.current) return; // pause polling while editing
    setLoading(true);
    try {
      const res = await queryDashboard(dashboard.id, overrideParams || paramsRef.current);
      if (!mountedRef.current) return;
      setResults(res.results || {});
      setRefreshedAt(res.refreshed_at);
    } catch (e) {
      if (mountedRef.current) setResults((r) => ({ ...r, __error: String(e) }));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [dashboard?.id]);
```

4. Add edit-mode handlers:
```javascript
  const enterEdit = useCallback(() => {
    setDraft(JSON.parse(JSON.stringify(dashboard?.definition || { widgets: [] })));
    setHeaderName(dashboard?.name || '');
    setDirty(false);
    setEditing(true);
  }, [dashboard?.definition, dashboard?.name]);

  const exitEdit = useCallback(() => {
    setEditing(false);
    setDraft(null);
    setDirty(false);
    setEditingWidget(null);
    refresh(undefined, { force: true });
  }, [refresh]);

  const handleSave = useCallback(async () => {
    const body = {};
    if (headerName !== dashboard.name) body.name = headerName;
    if (dirty) body.definition = draft;
    if (!body.name && !body.definition) { exitEdit(); return; }
    try {
      const updated = await updateDashboard(dashboard.id, body);
      onSaved?.(updated);
      setEditing(false); setDraft(null); setDirty(false); setEditingWidget(null);
      refresh(undefined, { force: true });
    } catch (e) { /* toast / leave in edit mode */ }
  }, [headerName, dashboard?.name, dashboard?.id, dirty, draft, onSaved, refresh, exitEdit]);

  const handleCancel = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return;
    exitEdit();
  }, [dirty, exitEdit]);

  const handleSaveAsCopy = useCallback(async () => {
    try {
      const created = await createDashboard({
        name: (headerName || dashboard?.name || 'Dashboard') + ' (copy)',
        description: dashboard?.description,
        datasource_kb_id: dashboard?.datasource_kb_id,
        project_id: dashboard?.project_id,
        project: dashboard?.project,
        definition: draft,
        refresh_interval_seconds: dashboard?.refresh_interval_seconds,
      });
      onCopied?.(created);
    } catch (e) { /* toast */ }
  }, [headerName, dashboard, draft, onCopied]);

  const deleteWidget = useCallback((id) => {
    setDraft((d) => ({ ...d, widgets: d.widgets.filter((w) => w.id !== id) }));
    setDirty(true);
  }, []);

  const applyWidget = useCallback((widget) => {
    setDraft((d) => {
      const exists = d.widgets.some((w) => w.id === widget.id);
      const widgets = exists ? d.widgets.map((w) => w.id === widget.id ? widget : w) : [...d.widgets, widget];
      return { ...d, widgets };
    });
    setDirty(true);
    setEditingWidget(null);
  }, []);
```

5. Switch the widget source + toolbar. Replace the `widgets`/`kpis`/`others` lines and the toolbar JSX:
```javascript
  const sourceWidgets = editing && draft ? draft.widgets : (dashboard?.definition?.widgets || []);
  const widgets = sourceWidgets;
  const kpis = widgets.filter((w) => w.type === 'kpi');
  const others = widgets.filter((w) => w.type !== 'kpi');
```

Toolbar (replace the right-side button cluster):
```jsx
        <div className="flex items-center gap-1.5">
          {editing ? (
            <>
              <input value={headerName} onChange={(e) => { setHeaderName(e.target.value); setDirty(true); }}
                className="rounded-md border border-border bg-transparent px-2 py-1 text-sm" aria-label="Dashboard name" />
              <button onClick={handleSave} className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground">Save</button>
              <button onClick={handleCancel} className="rounded-md border border-border px-3 py-1 text-xs">Cancel</button>
              <button onClick={handleSaveAsCopy} className="rounded-md border border-border px-3 py-1 text-xs">Save as copy</button>
              <button onClick={() => setEditingWidget('__new__')} className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs"><Plus className="h-3 w-3" />Add widget</button>
            </>
          ) : (
            <>
              {canEdit && (
                <button onClick={enterEdit} aria-label="Edit dashboard" title="Edit dashboard"
                  className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground">
                  <Pencil className="h-3 w-3" />Edit
                </button>
              )}
              <button onClick={handleExportPng} aria-label="Export PNG" title="Export PNG" className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"><Download className="h-3 w-3" />Export</button>
              <button onClick={handleShare} aria-label="Share" title="Copy shareable link" className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"><Share2 className="h-3 w-3" />Share</button>
              <button onClick={refresh} aria-label="Refresh dashboard" className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"><RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />Refresh</button>
            </>
          )}
        </div>
```

6. Add edit affordances to widget cards. In the `others.map` block, wrap each widget with edit/delete buttons when editing:
```jsx
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {others.map((w) => (
          <div key={w.id} className={`flex flex-col gap-3 ${editing ? 'opacity-80' : ''}`}>
            {editing && (
              <div className="flex justify-end gap-1">
                <button onClick={() => setEditingWidget(w.id)} aria-label="Edit widget" className="rounded border border-border p-1"><Pencil className="h-3 w-3" /></button>
                <button onClick={() => deleteWidget(w.id)} aria-label="Delete widget" className="rounded border border-border p-1"><Trash2 className="h-3 w-3" /></button>
              </div>
            )}
            <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
            {results['__drill__'] && results['__drill__'].source_widget_id === (w.id || w.title) && (
              <DrillPanel result={results['__drill__']} onClose={closeDrill} />
            )}
          </div>
        ))}
      </div>
```
(Do the same edit/delete affordance in the KPI row `kpis.map`.)

7. Mount the drawer at the end of the returned JSX (before the closing `</div>`):
```jsx
      {editingWidget && (
        <WidgetEditorDrawer
          widget={editingWidget === '__new__' ? null : (draft.widgets.find((w) => w.id === editingWidget))}
          dashboardId={dashboard.id}
          params={params}
          onApply={applyWidget}
          onCancel={() => setEditingWidget(null)}
        />
      )}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardViewer.edit.test.jsx src/components/dashboard/DashboardViewer.test.jsx`
Expected: all PASS (new edit tests + existing viewer regression).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/DashboardViewer.edit.test.jsx
git commit -m "feat(dashboard): DashboardViewer edit mode (Save/Cancel/Copy/drawer/polling pause)"
```

---

## Task 8: `@dnd-kit` drag-reorder

**Files:**
- Modify: `package.json` (add `@dnd-kit/core`, `@dnd-kit/sortable`)
- Create: `src/components/dashboard/SortableWidgetCard.jsx`
- Modify: `src/components/dashboard/DashboardViewer.jsx` (wrap edit-mode grids in `DndContext`+`SortableContext`)
- Test: extend `src/components/dashboard/DashboardViewer.edit.test.jsx`

**Interfaces:**
- Produces: `SortableWidgetCard({ id, children, onEdit, onDelete })` using `useSortable`. The viewer wraps the KPI row + others grid each in a `DndContext`/`SortableContext`; `onDragEnd` reorders `draft.widgets` (KPIs first, then others) and sets dirty.

- [ ] **Step 1: Install the dependency**

Run: `cd /root/zhanlu/frontend && npm install @dnd-kit/core @dnd-kit/sortable`
Verify `package.json` now lists both under `dependencies`.

- [ ] **Step 2: Write the failing test**

Append to `src/components/dashboard/DashboardViewer.edit.test.jsx`:
```javascript
  it('reorder moves a widget within draft (simulate onDragEnd via dnd-kit mock)', () => {
    // Mock @dnd-kit so DndContext renders children + exposes onDragEnd via context is complex;
    // instead assert the reorder handler by checking draft order after a synthetic drag.
    // Simplest: mock @dnd-kit/sortable arrayMove + DndContext to call onDragEnd with {active, over}.
    vi.doMock('@dnd-kit/core', () => ({
      DndContext: ({ children, onDragEnd }) => (
        <div data-testid="dnd" onClick={() => onDragEnd({ active: { id: 'w1' }, over: { id: 'w2' } })}>{children}</div>
      ),
    }));
    // NOTE: because top-level vi.mock is hoisted, prefer a dedicated test file or use the real
    // dnd-kit + fireEvent. For determinism, assert via the real library's DndSensor is flaky in jsdom.
    // PRAGMATIC: assert that entering edit mode renders drag handles.
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    expect(screen.getAllByLabelText('Drag')[0]).toBeInTheDocument();
  });
```
**Simpler, robust approach** (use this instead of the doMock above): assert drag handles render in edit mode, and unit-test the reorder pure function separately. Add a pure `reorderWidgets(widgets, fromId, toId)` export to the viewer (or a `lib`):
- Add test: drag handle renders in edit mode; `reorderWidgets([w1,w2], 'w1', 'w2')` → `[w2,w1]`.

Append (robust form):
```javascript
  it('renders drag handles in edit mode', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    expect(screen.getAllByLabelText('Drag').length).toBeGreaterThan(0);
  });
```
And create `src/components/dashboard/reorder.js` with a pure helper + its own test:
```javascript
// reorder.js
export function reorderWidgets(widgets, fromId, toId) {
  const from = widgets.findIndex((w) => w.id === fromId);
  const to = widgets.findIndex((w) => w.id === toId);
  if (from === -1 || to === -1 || from === to) return widgets;
  const next = [...widgets];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
```
`src/components/dashboard/reorder.test.js`:
```javascript
import { describe, it, expect } from 'vitest';
import { reorderWidgets } from './reorder';
describe('reorderWidgets', () => {
  it('moves widget from earlier to later position', () => {
    const ws = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    expect(reorderWidgets(ws, 'a', 'c').map((w) => w.id)).toEqual(['b', 'c', 'a']);
  });
  it('no-op when same id', () => {
    const ws = [{ id: 'a' }, { id: 'b' }];
    expect(reorderWidgets(ws, 'a', 'a')).toBe(ws);
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/reorder.test.js src/components/dashboard/DashboardViewer.edit.test.jsx`
Expected: FAIL — `reorder.js` not found; no drag handles.

- [ ] **Step 4: Implement**

Create `src/components/dashboard/reorder.js` (above).

Create `src/components/dashboard/SortableWidgetCard.jsx`:
```jsx
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical, Pencil, Trash2 } from 'lucide-react';

export default function SortableWidgetCard({ id, onEdit, onDelete, children }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1 };
  return (
    <div ref={setNodeRef} style={style} className="flex flex-col gap-3">
      <div className="flex items-center gap-1">
        <button aria-label="Drag" {...attributes} {...listeners} className="cursor-grab rounded border border-border p-1"><GripVertical className="h-3 w-3" /></button>
        <button onClick={onEdit} aria-label="Edit widget" className="rounded border border-border p-1"><Pencil className="h-3 w-3" /></button>
        <button onClick={onDelete} aria-label="Delete widget" className="rounded border border-border p-1"><Trash2 className="h-3 w-3" /></button>
      </div>
      {children}
    </div>
  );
}
```

In `DashboardViewer.jsx`, import + use in edit mode:
```javascript
import { DndContext, closestCenter } from '@dnd-kit/core';
import { SortableContext, rectSortingStrategy } from '@dnd-kit/sortable';
import SortableWidgetCard from './SortableWidgetCard';
import { reorderWidgets } from './reorder';
```
Replace the edit-mode `others.map` block (only when editing) with a sortable grid; keep the non-edit path as-is:
```jsx
      {editing ? (
        <DndContext collisionDetection={closestCenter} onDragEnd={(e) => {
          const { active, over } = e; if (!over || active.id === over.id) return;
          setDraft((d) => ({ ...d, widgets: reorderWidgets(d.widgets, active.id, over.id) }));
          setDirty(true);
        }}>
          <SortableContext items={others.map((w) => w.id)} strategy={rectSortingStrategy}>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {others.map((w) => (
                <SortableWidgetCard key={w.id} id={w.id} onEdit={() => setEditingWidget(w.id)} onDelete={() => deleteWidget(w.id)}>
                  <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
                </SortableWidgetCard>
              ))}
            </div>
          </SortableContext>
        </DndContext>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {others.map((w) => (
            <div key={w.id} className="flex flex-col gap-3">
              <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
              {results['__drill__'] && results['__drill__'].source_widget_id === (w.id || w.title) && (
                <DrillPanel result={results['__drill__']} onClose={closeDrill} />
              )}
            </div>
          ))}
        </div>
      )}
```
(For the KPI row in edit mode, use a separate `DndContext`+`SortableContext` with `horizontalListSortingStrategy`; on its `onDragEnd`, reorder only the KPI slice. To keep Task 8 contained, KPI reorder can use the same `reorderWidgets` over the full `draft.widgets` then re-split — since KPIs are filtered by type, reordering within the KPI slice via full-list reorder works if `active`/`over` ids are KPI ids.)

- [ ] **Step 5: Run tests to verify pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/reorder.test.js src/components/dashboard/DashboardViewer.edit.test.jsx src/components/dashboard/DashboardViewer.test.jsx`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add frontend/package.json frontend/package-lock.json frontend/src/components/dashboard/SortableWidgetCard.jsx frontend/src/components/dashboard/reorder.js frontend/src/components/dashboard/reorder.test.js frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/DashboardViewer.edit.test.jsx
git commit -m "feat(dashboard): @dnd-kit drag-reorder in edit mode"
```

---

## Task 9: Wire `canEdit` + `onSaved`/`onCopied` in DashboardPopup + DashboardView

**Files:**
- Modify: `src/components/dashboard/DashboardPopup.jsx`
- Modify: `src/pages/DashboardView.jsx`
- Test: `src/components/dashboard/DashboardPopup.test.jsx` (extend) + `src/pages/DashboardView.test.jsx` (new or extend)

**Interfaces:**
- Consumes: the auth context's current user id (`useAuth()` or wherever `user.id` lives — confirm the project's auth hook). Computes `canEdit = dashboard.created_by_id === currentUser.id`.
- Produces: `DashboardPopup`/`DashboardView` pass `canEdit`, `onSaved` (update local dashboard state + refresh), `onCopied` (navigate to new dashboard / open new popup).

- [ ] **Step 1: Write the failing test**

First, confirm the auth hook. Run: `cd /root/zhanlu/frontend && grep -rn "useAuth\|currentUser\|user.id" src/ | head -20` to find how the current user id is accessed (e.g. `useAuth().user.id` or a context). Use the discovered API in the implementation.

Extend `src/components/dashboard/DashboardPopup.test.jsx` — assert that when the auth user is the creator, the viewer receives `canEdit=true`; when not, `canEdit=false`. (Mock the auth context the same way existing tests stub it.)

```javascript
  it('passes canEdit=true when current user is the creator', () => {
    // mock auth context to return user id === dashboard.created_by_id
    // render DashboardPopup with a dashboard whose created_by_id matches
    // assert the Edit button is present (proxy for canEdit=true)
  });
  it('passes canEdit=false when current user is not the creator', () => {
    // mock auth user id !== created_by_id → Edit button absent
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardPopup.test.jsx`
Expected: FAIL — `canEdit` not passed (Edit button absent in both cases).

- [ ] **Step 3: Implement**

In `DashboardPopup.jsx`: obtain the current user id from the auth hook (e.g. `const { user } = useAuth();`), compute `const canEdit = dashboard?.created_by_id && user?.id && dashboard.created_by_id === user.id;`, pass `canEdit`, `onSaved={(updated) => setDashboard(updated)}`, `onCopied={(created) => { setDashboard(created); /* or navigate */ }}` to `<DashboardViewer>`.

In `pages/DashboardView.jsx` (standalone `/dashboard/:id` route): same `canEdit` computation; `onSaved` updates local state + `refresh`; `onCopied` calls `navigate('/dashboard/' + created.id)`.

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/DashboardPopup.test.jsx src/components/dashboard/`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/DashboardPopup.jsx frontend/src/pages/DashboardView.jsx frontend/src/components/dashboard/DashboardPopup.test.jsx
git commit -m "feat(dashboard): wire canEdit + onSaved/onCopied in popup + standalone page"
```

---

## Task 10: E2E test — creator edits, previews, saves

**Files:**
- Create: `backend/tests/test_dashboard_e2e_edit_save.py`
- Reference: `backend/tests/_dashboard_e2e_helpers.py` (reuse `get_mysql_kb_config`, `rds_reachable`, `require_live_or_skip`)

**Interfaces:**
- Consumes: real Aliyun RDS MySQL KB config + deployed app (login via `POST /api/apps/default-app/auth/login`). Reuses helpers from `_dashboard_e2e_helpers.py`.

- [ ] **Step 1: Write the test**

`backend/tests/test_dashboard_e2e_edit_save.py`:
```python
"""E2E: creator edits a dashboard widget (title + SQL), previews, saves, reloads.

Per-file run only (user rejects combined-suite runs). Skips if RDS/app unavailable.
Real Aliyun RDS MySQL — NO writes (read-only SELECTs).
"""
import os, pytest, httpx
from tests._dashboard_e2e_helpers import get_mysql_kb_config, rds_reachable, require_live_or_skip, NOW_WIDGET

APP = os.environ.get("E2E_APP_URL", "http://localhost:8000")
EMAIL = os.environ.get("E2E_EMAIL", "admin@zhanlu.dev")
PASSWORD = os.environ.get("E2E_PASSWORD", "Test1234!")


@pytest.fixture(scope="module")
def client():
    require_live_or_skip()
    with httpx.Client(base_url=APP, timeout=30) as c:
        r = c.post("/api/apps/default-app/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_creator_edits_previews_saves(client):
    cfg = get_mysql_kb_config()
    # seed a dashboard owned by the logged-in user
    seed = client.post("/api/dashboards", json={
        "name": "E2E Edit", "datasource_kb_id": cfg["kb_id"],
        "definition": {"widgets": [NOW_WIDGET]},
    })
    assert seed.status_code == 201, seed.text
    did = seed.json()["id"]
    try:
        # PATCH: rename + replace widget SQL (still read-only, table-free)
        new_sql = "SELECT VERSION() AS v"
        patch = client.patch(f"/api/dashboards/{did}", json={
            "name": "E2E Edited", "definition": {"widgets": [
                {"id": "w1", "type": "table", "title": "Version", "sql": new_sql, "options": {}}]}})
        assert patch.status_code == 200, patch.text
        assert patch.json()["name"] == "E2E Edited"
        # preview the new SQL
        prev = client.post(f"/api/dashboards/{did}/preview-sql", json={"sql": new_sql})
        assert prev.status_code == 200, prev.text
        assert prev.json()["error"] is None
        assert prev.json()["rows"]  # VERSION() returns a row
        # reload confirms persistence
        got = client.get(f"/api/dashboards/{did}")
        assert got.json()["name"] == "E2E Edited"
        assert got.json()["definition"]["widgets"][0]["sql"] == new_sql
    finally:
        client.delete(f"/api/dashboards/{did}")
```

- [ ] **Step 2: Run the test (per-file only)**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_e2e_edit_save.py -v`
Expected: PASS (1 test) against the real RDS + deployed app. Skip if RDS/app unreachable.

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/tests/test_dashboard_e2e_edit_save.py
git commit -m "test(dashboard): E2E creator edit/preview/save"
```

---

## Final Verification

- [ ] **Backend sweep (per-file, RAM-safe):**
```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"
python3 -m pytest tests/test_dashboard_query_single.py tests/test_dashboards_patch.py tests/test_dashboards_preview_sql.py tests/test_update_dashboard_tool.py tests/test_dashboards_api.py tests/test_dashboard_query_tokens.py tests/test_dashboard_query_params_drill.py tests/test_create_dashboard_tool.py -v
```
Expected: all PASS.

- [ ] **Frontend sweep:**
```bash
cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/ src/api/dashboards.test.js
```
Expected: all PASS. Then `npm run build` → clean (only pre-existing chunk warning).

- [ ] **Lint:** `npx eslint src/components/dashboard/ src/api/dashboards.js --max-warnings 0` (0 errors).

- [ ] **Backend restart + smoke:** `docker restart zhanlu-backend` then `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/dashboards` → 401 (route mounted, fail-closed).

- [ ] **Deploy (when user asks):** `/opt/rebuild-zhanlu.sh --force`.

## Self-Review

**1. Spec coverage:**
- §6.1 PATCH → Task 2 ✓
- §6.2 preview-sql → Task 3 ✓ (+ `_run_single_sql` refactor Task 1 ✓)
- §6.3 update_dashboard tool → Task 4 ✓
- §7.1 viewer edit mode → Task 7 ✓ (polling pause via editingRef, no effect-dep change ✓)
- §7.2 SQL drawer → Task 6 ✓
- §7.3 dnd-kit reorder → Task 8 ✓
- §7.4 save-as-copy → Task 7 (handleSaveAsCopy) ✓
- §7.5 api client → Task 5 ✓
- §8 security (creator-only, datasource immutable) → Tasks 2/3/4 enforce ✓
- §9 testing → Tasks 1–10 each have tests ✓
- §10 file touch list → all files covered ✓

**2. Placeholder scan:** No TBD/TODO. The auth-hook discovery in Task 9 Step 1 is a concrete `grep` command (not a placeholder) — the implementer runs it to find the exact API. The KPI-reorder note in Task 8 is a concrete strategy, not a deferral.

**3. Type consistency:** `_run_single_sql(db, kb_id, sql, params, dimensions, max_rows, timeout_s) -> {columns, rows, error, truncated}` — used identically in Task 1 (definition), Task 3 (preview). `updateDashboard(id, body)` / `previewWidgetSql(id, {sql, params})` — consistent across Task 5 (client) + Tasks 6/7 (callers). `WidgetEditorDrawer({widget, dashboardId, params, onApply, onCancel})` — consistent Task 6 (def) + Task 7 (mount). `reorderWidgets(widgets, fromId, toId)` — consistent Task 8 (def + caller). `UpdateDashboardBody`/`PreviewSqlBody` field names match the spec §6.1/6.2.

**4. Note on `pytest-asyncio`:** the plan uses `asyncio.run(...)` sync wrappers in backend tests to avoid the plugin dependency (the existing `/query` test uses TestClient's sync wrapper, not async). If `pytest-asyncio` is already installed, `async def` tests also work — either is fine; the `asyncio.run` form is the safe default.
