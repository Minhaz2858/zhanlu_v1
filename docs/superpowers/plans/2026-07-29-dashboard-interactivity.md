# Dashboard Interactivity (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add drill-down, date-range selector, KPI threshold coloring, and cross-widget filtering to the live dashboard, plus a creation-time agent clarification gate.

**Architecture:** One server-side whitelisted-token render layer (`render_widget_sql`) backs all four features; `POST /dashboards/{id}/query` accepts an optional params body (backward compatible). All new widget behavior is opt-in via `options` (no schema migration). The frontend viewer gains date-range / filter / drill state and re-queries on change; the polling effect stays intact via a `paramsRef`. The `create_dashboard` tool description + `dashboard-generation` skill enforce a "clarify before building" gate.

**Tech Stack:** Python/FastAPI/Pydantic v2 (backend), React + recharts + shadcn `chart.jsx` (frontend), vitest (frontend tests), pytest (backend tests).

## Global Constraints

- **Stored widget SQL is never trusted.** Every rendered SQL string is re-validated by `validate_widget_sql` (read-only SELECT/WITH, single-statement) before execution. This invariant already exists and MUST be preserved.
- **Chart colors use `hsl(var(--chart-N))` only.** NEVER hardcode hex. KPI threshold colors use existing tokens: `text-[hsl(var(--chart-2))]` (green/good), `text-amber-500` (warn), `text-destructive` (bad).
- **Polling effect in `DashboardViewer.jsx` stays verbatim** (10–300s clamp, `visibilitychange` pause, `mountedRef` guard). New params flow through a `paramsRef` read inside `refresh`, so the effect body is unchanged.
- **RAM-limited dev box:** run backend tests by explicit file path only, no `-n` workers. Frontend tests scoped to `src/components/dashboard`.
- **Repo convention:** commit only when the user asks. TDD red→green per task.
- **No new dependencies.** recharts + shadcn already bundled.
- **Backward compatibility:** `POST /{id}/query` with no body / empty body MUST behave exactly as today (last-30-days window, no filters, no drill).

---

## File Structure

**Backend (modify):**
- `app/services/dashboard_query.py` — add `render_widget_sql`, `_resolve_window`, `_literal`, `_TOKEN_RE`; extend `run_dashboard_query(db, dashboard, params=None)` with per-widget render + drill branch.
- `app/routers/dashboards.py` — add `QueryParams` model; `query_dashboard` accepts optional body; `_validate_definition` validates `options.drill.sql`.
- `app/services/tool_handlers/dashboard_tools.py` — `WidgetSpec` `model_validator` for `drill.sql`; extend tool `description` (clarification contract) + `options.description` (thresholds/drill/dimensions/tokens).

**Backend (modify skill docs):**
- `backend/skills/dashboard-generation/SKILL.md` — "Clarify before building" step 1 + widget-schema docs for new `options`.
- `docs/starter_templates/skill_library/system/visualization/dashboard-generation/SKILL.md` — mirror.

**Backend (new tests):**
- `backend/tests/test_dashboard_query_tokens.py`
- `backend/tests/test_dashboard_query_params_drill.py`
- `backend/tests/test_dashboard_clarification_guidance.py`

**Frontend (modify):**
- `src/api/dashboards.js` — `queryDashboard(id, params)`.
- `src/components/dashboard/DashboardViewer.jsx` — dateRange/filters/drill state, `paramsRef`, control bar, drill panel slot, `onAction` wiring.
- `src/components/dashboard/widgets/KpiWidget.jsx` — threshold coloring.
- `src/components/dashboard/widgets/PieWidget.jsx` — slice `onClick`.
- `src/components/dashboard/widgets/TableWidget.jsx` — row `onClick`.
- `src/components/dashboard/widgets/TrendChartWidget.jsx` — bar `onClick`.

**Frontend (new):**
- `src/components/dashboard/DateRangeControl.jsx`
- `src/components/dashboard/FilterChips.jsx`
- `src/components/dashboard/DrillPanel.jsx`
- `src/components/dashboard/interactions.js` (pure `resolveClick`)

**Frontend (new tests):**
- `src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx`
- `src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx`
- `src/components/dashboard/__tests__/interactions.test.js`

---

### Task 1: Backend token render layer

**Files:**
- Modify: `app/services/dashboard_query.py`
- Test: `backend/tests/test_dashboard_query_tokens.py`

**Interfaces:**
- Produces: `render_widget_sql(sql: str, params: dict | None, dimensions: list[dict] | None = None) -> str` (raises `ValueError` on unknown token or post-render validation failure); `_resolve_window(params) -> (datetime, datetime)`; `_literal(s) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_query_tokens.py
import datetime as dt
import pytest
from app.services.dashboard_query import render_widget_sql, _resolve_window


def test_from_to_date_tokens_become_iso_literals():
    params = {"from": "2026-07-01T00:00:00", "to": "2026-07-29T00:00:00"}
    out = render_widget_sql("SELECT * FROM t WHERE created_at >= :from AND created_at < :to AND d <= :date", params, [])
    assert "'2026-07-01 00:00:00'" in out
    assert "'2026-07-29 00:00:00'" in out
    assert "'2026-07-29'" in out
    assert ":from" not in out and ":to" not in out and ":date" not in out


def test_unset_dim_token_becomes_noop():
    out = render_widget_sql("SELECT * FROM t WHERE :dim_region", {}, [{"token": "region", "column": "region"}])
    assert out == "SELECT * FROM t WHERE 1=1"


def test_set_dim_token_becomes_column_equals_literal():
    out = render_widget_sql("SELECT * FROM t WHERE :dim_region", {"filters": {"region": "Asia"}},
                            [{"token": "region", "column": "region"}])
    assert out == "SELECT * FROM t WHERE region = 'Asia'"


def test_dim_value_single_quotes_escaped():
    out = render_widget_sql("WHERE :dim_name", {"filters": {"name": "O'Brien"}},
                            [{"token": "name", "column": "full_name"}])
    assert "full_name = 'O''Brien'" in out


def test_drill_value_token():
    out = render_widget_sql("SELECT * FROM t WHERE cat = :drill_value", {"drill_value": "Books"}, [])
    assert "cat = 'Books'" in out


def test_unknown_dim_token_raises():
    with pytest.raises(ValueError, match="Unknown dimension token"):
        render_widget_sql("WHERE :dim_region", {}, [])  # no dimensions declared


def test_unknown_token_shape_raises():
    with pytest.raises(ValueError, match="Unknown token"):
        render_widget_sql("WHERE :foobar", {}, [])


def test_default_window_last_30_days_when_absent():
    frm, to = _resolve_window({})
    assert (to - frm).days == 30


def test_default_window_explicit_dates():
    frm, to = _resolve_window({"from": "2026-01-01T00:00:00", "to": "2026-02-01T00:00:00"})
    assert frm == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def test_render_revalidates_rejects_injection_via_drill():
    # drill_value is escaped to a literal, so a '; DROP' cannot become a 2nd statement
    out = render_widget_sql("SELECT * FROM t WHERE x = :drill_value", {"drill_value": "a'; DROP TABLE t; --"}, [])
    assert "DROP" not in out.upper() or "'a''; DROP TABLE t; --'" in out  # fully inside one literal
    # post-render validate_widget_sql passes (still single SELECT)
    assert out.upper().startswith("SELECT")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_tokens.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_widget_sql'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/services/dashboard_query.py` (after the existing `validate_widget_sql`, before `run_dashboard_query`):

```python
import datetime as _dt

_TOKEN_RE = re.compile(r":(from|to|date|drill_value|dim_[a-zA-Z_][a-zA-Z0-9_]*)\b")


def _literal(s) -> str:
    """Quote a value as a SQL string literal, escaping embedded single quotes."""
    return "'" + str(s).replace("'", "''") + "'"


def _resolve_window(params: dict | None) -> tuple[_dt.datetime, _dt.datetime]:
    """Return (from, to) datetimes. Defaults to last-30-days when absent."""
    p = params or {}

    def _parse(v):
        try:
            return _dt.datetime.fromisoformat(v) if v else None
        except (TypeError, ValueError):
            return None

    to = _parse(p.get("to")) or _dt.datetime.now(_dt.timezone.utc)
    frm = _parse(p.get("from")) or (to - _dt.timedelta(days=30))
    return frm, to


def render_widget_sql(sql: str, params: dict | None, dimensions: list[dict] | None = None) -> str:
    """Substitute whitelisted tokens, then re-validate the rendered SQL.

    Tokens: :from :to :date (date window), :dim_<token> (cross-widget filter),
    :drill_value (drill-down). Unknown tokens raise ValueError. The rendered
    SQL is passed through validate_widget_sql again before returning (defense
    in depth — stored SQL is never trusted).
    """
    params = params or {}
    dimensions = dimensions or []
    frm, to = _resolve_window(params)
    dim_map = {d["token"]: d["column"] for d in dimensions}
    active_dims = params.get("filters") or {}

    def _sub(m):
        tok = m.group(1)
        if tok == "from":
            return _literal(frm.strftime("%Y-%m-%d %H:%M:%S"))
        if tok == "to":
            return _literal(to.strftime("%Y-%m-%d %H:%M:%S"))
        if tok == "date":
            return _literal(to.strftime("%Y-%m-%d"))
        if tok == "drill_value":
            return _literal(params.get("drill_value", ""))
        if tok.startswith("dim_"):
            name = tok[4:]
            if name not in dim_map:
                raise ValueError(f"Unknown dimension token :{tok}")
            val = active_dims.get(name)
            if val is None or val == "":
                return "1=1"
            return f"{dim_map[name]} = {_literal(val)}"
        raise ValueError(f"Unknown token :{tok}")

    rendered = _TOKEN_RE.sub(_sub, sql)
    validate_widget_sql(rendered)  # defense in depth
    return rendered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_tokens.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/dashboard_query.py tests/test_dashboard_query_tokens.py && git commit -m "feat(dashboard): add whitelisted token render layer for widget SQL"
```

---

### Task 2: Backend query params + backward compat

**Files:**
- Modify: `app/services/dashboard_query.py` (`run_dashboard_query` signature + per-widget render)
- Modify: `app/routers/dashboards.py` (`QueryParams` + `query_dashboard` body)
- Test: `backend/tests/test_dashboard_query_params_drill.py`

**Interfaces:**
- Consumes: `render_widget_sql` (Task 1).
- Produces: `run_dashboard_query(db, dashboard, params=None) -> dict` (results shape unchanged; empty/None params = current behavior). `QueryParams` pydantic model with fields `from_`, `to`, `filters`, `drill`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_query_params_drill.py
import asyncio
from types import SimpleNamespace
import pytest


class _FakeQueryService:
    """Captures the rendered SQL it receives instead of hitting a DB."""
    def __init__(self, db):
        self.db = db
        self.calls = []

    def execute(self, kb_id, sql, max_rows=1000, timeout_s=10):
        self.calls.append(sql)
        return {"rows": [{"rendered": sql}], "truncated": False}


@pytest.fixture
def patched_qs(monkeypatch):
    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _FakeQueryService)
    return dq


def _dash(widgets):
    return SimpleNamespace(id="d1", datasource_kb_id="kb1", definition={"widgets": widgets})


def test_run_query_renders_date_tokens(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT * FROM t WHERE created_at >= :from"}])
    out = asyncio.run(patched_qs.run_dashboard_query(None, dash, {"from": "2026-07-01T00:00:00", "to": "2026-07-29T00:00:00"}))
    rendered = patched_qs.QueryService(None).calls[-1] if patched_qs.QueryService(None).calls else out["results"]["w1"]
    # The captured SQL lives in the fake; re-fetch via results row
    assert "'2026-07-01 00:00:00'" in out["results"]["w1"]["rows"][0]["rendered"]


def test_run_query_none_params_backward_compat(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT 1 AS n"}])
    out = asyncio.run(patched_qs.run_dashboard_query(None, dash, None))
    assert out["results"]["w1"]["error"] is None
    assert out["results"]["w1"]["rows"][0]["n"] == 1


def test_run_query_renders_dim_filter(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT * FROM t WHERE :dim_region",
                   "options": {"dimensions": [{"token": "region", "column": "region"}]}}])
    out = asyncio.run(patched_qs.run_dashboard_query(None, dash, {"filters": {"region": "Asia"}}))
    assert "region = 'Asia'" in out["results"]["w1"]["rows"][0]["rendered"]


def test_router_query_with_empty_body(patched_qs, tmp_path, monkeypatch):
    # Minimal app + overrides pattern (mirrors existing test_dashboards_api.py)
    from fastapi import FastAPI
    from app.routers.dashboards import router
    from app.database import get_db
    from app.deps import get_current_user_required
    from app.models.dashboard import Dashboard

    # in-memory sqlite + create tables
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    d = Dashboard(name="D", datasource_kb_id="kb1", definition={"widgets": [{"id": "w1", "sql": "SELECT 1 AS n"}]},
                  org_id="o1", app_id="a1")
    db.add(d); db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(id="u1", org_id="o1", app_id="a1")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(f"/api/dashboards/{d.id}/query", json={})
    assert r.status_code == 200
    assert r.json()["results"]["w1"]["error"] is None


def test_router_query_no_body_backward_compat(patched_qs, tmp_path):
    from fastapi import FastAPI
    from app.routers.dashboards import router
    from app.database import get_db
    from app.deps import get_current_user_required
    from app.models.dashboard import Dashboard
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    eng = create_engine(f"sqlite:///{tmp_path}/t2.db")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    d = Dashboard(name="D", datasource_kb_id="kb1", definition={"widgets": [{"id": "w1", "sql": "SELECT 1 AS n"}]},
                  org_id="o1", app_id="a1")
    db.add(d); db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(id="u1", org_id="o1", app_id="a1")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(f"/api/dashboards/{d.id}/query")  # no body, no content-type
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_params_drill.py -v`
Expected: FAIL — `run_dashboard_query() takes 2 positional arguments but 3 were given` (signature not yet updated)

- [ ] **Step 3: Write minimal implementation**

In `app/services/dashboard_query.py`, replace the body of `run_dashboard_query`:

```python
async def run_dashboard_query(db, dashboard, params: dict | None = None) -> dict:
    """Run every widget's SQL (token-rendered) against the dashboard's datasource.

    params may carry: from/to (ISO), filters {token: value}, drill {widget_id, value}.
    None/empty params = current behavior (last-30-days default, no filters).
    Each widget is rendered + re-validated + executed independently; a failing
    widget sets that widget's ``error`` and does NOT abort the others.
    """
    params = params or {}
    widgets = (dashboard.definition or {}).get("widgets", []) or []
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
        dimensions = (widget.get("options") or {}).get("dimensions") or []
        try:
            rendered = render_widget_sql(sql, params, dimensions)
        except ValueError as e:
            return wid, {"columns": [], "rows": [], "error": str(e), "truncated": False}
        try:
            res = await asyncio.to_thread(
                lambda: QueryService(db).execute(
                    dashboard.datasource_kb_id, rendered, max_rows=max_rows, timeout_s=timeout_s
                )
            )
            return wid, {
                "columns": list(res["rows"][0].keys()) if res["rows"] else [],
                "rows": res["rows"],
                "error": None,
                "truncated": res.get("truncated", False),
            }
        except Exception as e:
            return wid, {"columns": [], "rows": [], "error": str(e), "truncated": False}

    pairs = await asyncio.gather(*[_run_one(w) for w in widgets])
    results = {wid: payload for wid, payload in pairs}

    drill = params.get("drill")
    if isinstance(drill, dict) and drill.get("widget_id"):
        results["__drill__"] = await _run_drill(db, dashboard, params, drill, max_rows, timeout_s)

    return {
        "dashboard_id": dashboard.id,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


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
    try:
        rendered = render_widget_sql(drill_sql, {**params, "drill_value": value}, dimensions)
        res = await asyncio.to_thread(
            lambda: QueryService(db).execute(
                dashboard.datasource_kb_id, rendered, max_rows=max_rows, timeout_s=timeout_s
            )
        )
        base["columns"] = list(res["rows"][0].keys()) if res["rows"] else []
        base["rows"] = res["rows"]
        base["truncated"] = res.get("truncated", False)
    except Exception as e:
        base["error"] = str(e)
    return base
```

In `app/routers/dashboards.py`, add `QueryParams` and update `query_dashboard`:

```python
from pydantic import BaseModel

class QueryParams(BaseModel):
    from_: str | None = None
    to: str | None = None
    filters: dict[str, str] | None = None
    drill: dict | None = None


@router.post("/{dashboard_id}/query")
async def query_dashboard(dashboard_id: str, body: QueryParams | None = None,
                          db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    params = None
    if body is not None:
        params = {}
        if body.from_:
            params["from"] = body.from_
        if body.to:
            params["to"] = body.to
        if body.filters:
            params["filters"] = body.filters
        if body.drill:
            params["drill"] = body.drill
    return await run_dashboard_query(db, d, params)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_params_drill.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/dashboard_query.py app/routers/dashboards.py tests/test_dashboard_query_params_drill.py && git commit -m "feat(dashboard): query endpoint accepts date/filter/drill params (backward compatible)"
```

---

### Task 3: Backend drill-down create-time validation

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py` (`WidgetSpec` drill validator)
- Modify: `app/routers/dashboards.py` (`_validate_definition` drill check)
- Test: `backend/tests/test_dashboard_query_params_drill.py` (extend)

**Interfaces:**
- Produces: `WidgetSpec` rejects `options.drill` without `sql`+`value_column` or with non-read-only drill SQL; router `_validate_definition` mirrors for the REST path.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dashboard_query_params_drill.py`)

```python
def test_widget_spec_rejects_drill_without_sql():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WidgetSpec(id="w1", type="bar", title="T", sql="SELECT 1 AS n",
                   options={"drill": {"value_column": "c"}})


def test_widget_spec_rejects_non_readonly_drill_sql():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WidgetSpec(id="w1", type="bar", title="T", sql="SELECT 1 AS n",
                   options={"drill": {"value_column": "c", "sql": "DELETE FROM t"}})


def test_widget_spec_accepts_valid_drill():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    w = WidgetSpec(id="w1", type="bar", title="T", sql="SELECT cat, n FROM t",
                   options={"drill": {"value_column": "cat",
                                      "sql": "SELECT * FROM t WHERE cat = :drill_value"}})
    assert w.options["drill"]["value_column"] == "cat"


def test_router_validates_drill_sql(tmp_path):
    from fastapi import FastAPI
    from app.routers.dashboards import router
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(router, prefix="/api")
    c = TestClient(app)
    r = c.post("/api/dashboards", json={
        "name": "D", "datasource_kb_id": "kb1",
        "definition": {"widgets": [{"id": "w1", "type": "bar", "title": "T", "sql": "SELECT 1 AS n",
                     "options": {"drill": {"value_column": "c", "sql": "DELETE FROM t"}}}]}})
    assert r.status_code == 400
    assert "drill" in r.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_params_drill.py -k "widget_spec or router_validates_drill" -v`
Expected: FAIL — no drill validation yet (accepts invalid drill)

- [ ] **Step 3: Write minimal implementation**

In `app/services/tool_handlers/dashboard_tools.py`, add import + validator to `WidgetSpec`:

```python
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
```

```python
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

    @model_validator(mode="after")
    def _validate_drill(self):
        drill = (self.options or {}).get("drill")
        if drill:
            if not drill.get("sql") or not drill.get("value_column"):
                raise ValueError("options.drill requires 'sql' and 'value_column'")
            validate_widget_sql(drill["sql"])
        return self
```

In `app/routers/dashboards.py`, extend `_validate_definition`:

```python
def _validate_definition(definition: dict) -> None:
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("widgets"), list)
            or not definition["widgets"]):
        raise HTTPException(status_code=400, detail="definition.widgets must be a non-empty list")
    for w in definition["widgets"]:
        if not w.get("sql"):
            raise HTTPException(status_code=400, detail="each widget needs sql")
        try:
            validate_widget_sql(w["sql"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: {e}")
        drill = (w.get("options") or {}).get("drill")
        if drill:
            if not drill.get("sql") or not drill.get("value_column"):
                raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: options.drill requires 'sql' and 'value_column'")
            try:
                validate_widget_sql(drill["sql"])
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: drill sql: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_query_params_drill.py -v`
Expected: PASS (all, incl. the 4 new)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/tool_handlers/dashboard_tools.py app/routers/dashboards.py tests/test_dashboard_query_params_drill.py && git commit -m "feat(dashboard): validate drill SQL at create time (tool + REST)"
```

---

### Task 4: Agent clarification gate + tool schema docs

**Files:**
- Modify: `app/services/tool_handlers/dashboard_tools.py` (tool `description` + `options.description`)
- Modify: `backend/skills/dashboard-generation/SKILL.md`
- Modify: `docs/starter_templates/skill_library/system/visualization/dashboard-generation/SKILL.md`
- Test: `backend/tests/test_dashboard_clarification_guidance.py`

**Interfaces:**
- Produces: tool `description` containing the 5-item clarify contract + "do not guess"; SKILL.md "Clarify before building" step; `options.description` documents thresholds/drill/dimensions/tokens.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dashboard_clarification_guidance.py
"""Source-inspect guard: the agent-facing guidance must tell the agent to clarify
datasource / time window / metrics / dimensions / chart intent before building,
and must NOT guess. Catches silent regression of the §5.5 clarification gate."""

def test_tool_description_has_clarification_contract():
    from app.services.tool_handlers.dashboard_tools import CREATE_DASHBOARD_SCHEMA
    desc = CREATE_DASHBOARD_SCHEMA["function"]["description"].lower()
    for keyword in ["datasource", "time window", "metrics", "dimensions", "chart", "do not guess"]:
        assert keyword in desc, f"tool description missing '{keyword}'"


def test_skill_md_has_clarify_before_building():
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "skills" / "dashboard-generation" / "SKILL.md"
    body = p.read_text(encoding="utf-8").lower()
    assert "clarify before building" in body
    for keyword in ["datasource", "time window", "metrics", "dimensions", "chart intent"]:
        assert keyword in body, f"SKILL.md missing '{keyword}'"


def test_options_description_documents_new_fields():
    from app.services.tool_handlers.dashboard_tools import CREATE_DASHBOARD_SCHEMA
    desc = CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]["widgets"]["items"]["properties"]["options"]["description"].lower()
    for keyword in ["thresholds", "drill", "dimensions", ":from", ":dim_"]:
        assert keyword in desc, f"options.description missing '{keyword}'"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_clarification_guidance.py -v`
Expected: FAIL — keywords not present yet

- [ ] **Step 3: Write minimal implementation**

In `app/services/tool_handlers/dashboard_tools.py`, replace the `description` value in `CREATE_DASHBOARD_SCHEMA`:

```python
        "description": (
            "Build a LIVE dashboard bound to ONE database datasource. The dashboard "
            "auto-polls the database while open. Each widget needs read-only SQL "
            "(SELECT/WITH only). BEFORE calling this tool, confirm with the user: "
            "(1) datasource, (2) time window / duration, (3) key metrics & columns, "
            "(4) filter/drill dimensions, (5) chart intent. If ANY of these is "
            "ambiguous or unspecified, ask the user in ONE message with numbered "
            "options (e.g. 'A/B/C') and WAIT for their reply — do NOT guess. One "
            "dashboard binds to exactly ONE datasource; if the user has not named "
            "one, ask them to pick from the project's connected databases first."
        ),
```

Replace the `options.description` value:

```python
                                "description": (
                                    "Per-type options. kpi: {value_column} required; optional "
                                    "unit (suffix), compare_column (delta chip), subtitle, "
                                    "thresholds {direction:'higher_is_worse'|'lower_is_worse', "
                                    "warning:<n>, critical:<n>} -> green/amber/red coloring. "
                                    "line/bar: {x_column, y_column}. pie: {name_column, value_column}. "
                                    "table: {}. ALL types: optional dimensions "
                                    "[{token,column}] enables cross-widget filtering via "
                                    ":dim_<token> tokens in sql (renders col='value' or 1=1). "
                                    "Optional drill {value_column, sql, title} enables click-to-"
                                    "drill; drill sql uses :drill_value. Date tokens :from/:to/:date "
                                    "in any widget sql bind to the viewer's date-range selector "
                                    "(default last-30-days)."
                                ),
```

In BOTH `backend/skills/dashboard-generation/SKILL.md` and `docs/starter_templates/skill_library/system/visualization/dashboard-generation/SKILL.md`, replace the "## Workflow" section's step 1 and the "## Hard rules" + "## Widget schema" with the clarified versions:

Replace step 1 line `1. Confirm the datasource (ask the user if unspecified).` with:

```markdown
1. **Clarify before building.** Before designing widgets, confirm these five
   items with the user. If ANY is ambiguous or unspecified, ask in ONE message
   with numbered options (A/B/C) and WAIT for the reply — do NOT guess:
   - **Datasource** — which connected database (if >1 available, ask the user
     to pick ONE).
   - **Time window / duration** — e.g. last 7d / 30d / 90d / YTD / custom. This
     drives the `:from`/`:to` tokens you put in widget SQL and the default the
     viewer opens with.
   - **Key metrics & columns** — what to measure (e.g. revenue + order count vs
     revenue by region).
   - **Filter / drill dimensions** — which dimensions the user wants to
     filter/drill by; declare them in `options.dimensions` and use `:dim_<token>`
     tokens in SQL.
   - **Chart intent** — how to visualize (KPI + trend / pie + table / bar
     comparison).
   Only when all five are resolved (explicitly stated or unambiguous) proceed.
```

Add to the "## Widget schema" `options` section (after the existing per-type bullets):

```markdown
  - ALL types, optional `dimensions`: `[{token, column}]` — enables cross-widget
    filtering. Use `:dim_<token>` in the widget SQL (renders `column = 'value'`
    when the filter is active, `1=1` when not). Example:
    `SELECT region, SUM(amount) FROM orders WHERE :dim_region GROUP BY region`
    with `dimensions: [{token: 'region', column: 'region'}]`.
  - ALL types, optional `drill`: `{value_column, sql, title}` — clicking a
    bar/slice/row runs `sql` with `:drill_value` set to the clicked value, shown
    as an inline detail table. `sql` must be read-only SELECT/WITH.
  - kpi, optional `thresholds`: `{direction:'higher_is_worse'|'lower_is_worse',
    warning:<n>, critical:<n>}` — value colors green (good) / amber (warning) /
    red (critical).
  - ANY widget SQL may use `:from` / `:to` / `:date` tokens to bind to the
    viewer's date-range selector (default last-30-days).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_dashboard_clarification_guidance.py -v`
Expected: PASS (3 tests). Also re-run `tests/test_dashboard_design_skills_load.py -v` to confirm the skill still loads.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/tool_handlers/dashboard_tools.py backend/skills/dashboard-generation/SKILL.md docs/starter_templates/skill_library/system/visualization/dashboard-generation/SKILL.md backend/tests/test_dashboard_clarification_guidance.py && git commit -m "feat(dashboard): agent clarification gate (5-item) + options docs"
```

---

### Task 5: Frontend API client `queryDashboard(id, params)`

**Files:**
- Modify: `src/api/dashboards.js`
- Test: `src/api/__tests__/dashboards.test.js`

**Interfaces:**
- Produces: `queryDashboard(id, params = {})` POSTs a JSON body.

- [ ] **Step 1: Write the failing test**

```jsx
// src/api/__tests__/dashboards.test.js
import { describe, it, expect, vi, beforeEach } from 'vitest';

const json = vi.fn();
const authFetch = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch }));

import { queryDashboard } from '../dashboards';

describe('queryDashboard', () => {
  beforeEach(() => { authFetch.mockReset(); json.mockReset(); });

  it('POSTs a JSON body with params', async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ results: {} }) });
    await queryDashboard('d1', { from: '2026-07-01', filters: { region: 'Asia' } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1/query');
    expect(opts.method).toBe('POST');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ from: '2026-07-01', filters: { region: 'Asia' } });
  });

  it('defaults to empty body when no params', async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ results: {} }) });
    await queryDashboard('d1');
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({});
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/__tests__/dashboards.test.js`
Expected: FAIL — `queryDashboard` ignores params (sends no body)

- [ ] **Step 3: Write minimal implementation**

In `src/api/dashboards.js`, replace `queryDashboard`:

```js
export function queryDashboard(id, params = {}) {
  return authFetch(`${BASE}/${id}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params || {}),
  }).then(j);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/api/__tests__/dashboards.test.js`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/api/dashboards.js frontend/src/api/__tests__/dashboards.test.js && git commit -m "feat(dashboard): queryDashboard posts params body"
```

---

### Task 6: Frontend KPI threshold coloring

**Files:**
- Modify: `src/components/dashboard/widgets/KpiWidget.jsx`
- Test: `src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx`

**Interfaces:**
- Consumes: `widget.options.thresholds = {direction, warning, critical}`.
- Produces: KPI value span gets a color class based on thresholds.

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import KpiWidget from '../KpiWidget';

// recharts not used by KpiWidget — no mock needed
const w = (opts, value) => ({ options: { value_column: 'v', ...opts } });

describe('KpiWidget thresholds', () => {
  it('colors red when higher_is_worse and value >= critical', () => {
    render(<KpiWidget widget={w({ thresholds: { direction: 'higher_is_worse', warning: 50, critical: 90 } })} rows={[{ v: 95 }]} />);
    const val = screen.getByText(/95/);
    expect(val.className).toContain('text-destructive');
  });

  it('colors amber when higher_is_worse and warning <= value < critical', () => {
    render(<KpiWidget widget={w({ thresholds: { direction: 'higher_is_worse', warning: 50, critical: 90 } })} rows={[{ v: 60 }]} />);
    expect(screen.getByText(/60/).className).toContain('text-amber-500');
  });

  it('colors green when higher_is_worse and value < warning', () => {
    render(<KpiWidget widget={w({ thresholds: { direction: 'higher_is_worse', warning: 50, critical: 90 } })} rows={[{ v: 30 }]} />);
    expect(screen.getByText(/30/).className).toContain('text-[hsl(var(--chart-2))]');
  });

  it('colors red when lower_is_worse and value <= critical', () => {
    render(<KpiWidget widget={w({ thresholds: { direction: 'lower_is_worse', warning: 50, critical: 10 } })} rows={[{ v: 5 }]} />);
    expect(screen.getByText(/5/).className).toContain('text-destructive');
  });

  it('no thresholds = no threshold color class', () => {
    render(<KpiWidget widget={w({}, 42)} rows={[{ v: 42 }]} />);
    const c = screen.getByText(/42/).className;
    expect(c).not.toContain('text-destructive');
    expect(c).not.toContain('text-amber-500');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx`
Expected: FAIL — no threshold coloring

- [ ] **Step 3: Write minimal implementation**

In `src/components/dashboard/widgets/KpiWidget.jsx`, add a threshold resolver and apply the class to the value span:

```jsx
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { formatMetric, formatDelta } from '../format';

function thresholdClass(thresholds, raw) {
  if (!thresholds) return '';
  const num = Number(raw);
  if (!Number.isFinite(num)) return '';
  const { direction, warning, critical } = thresholds;
  const higherIsWorse = direction === 'higher_is_worse';
  const isBad = higherIsWorse ? num >= critical : num <= critical;
  const isWarn = higherIsWorse ? num >= warning : num <= warning;
  if (isBad) return 'text-destructive';
  if (isWarn) return 'text-amber-500';
  return 'text-[hsl(var(--chart-2))]';
}

export default function KpiWidget({ widget, rows }) {
  const col = widget.options?.value_column;
  const row = rows?.[0];
  const raw = row && col != null ? row[col] : row ? Object.values(row)[0] : null;
  const delta =
    widget.options?.compare_column && row
      ? formatDelta(raw, row[widget.options.compare_column])
      : null;
  const tClass = thresholdClass(widget.options?.thresholds, raw);
  return (
    <div className="flex min-h-[80px] flex-col justify-center">
      <div className="flex items-baseline gap-2">
        <span className={`text-3xl font-bold tabular-nums tracking-tight ${tClass}`}>
          {formatMetric(raw, { unit: widget.options?.unit })}
        </span>
        {delta && <DeltaChip delta={delta} />}
      </div>
      {widget.options?.subtitle ? (
        <div className="mt-1 text-xs text-muted-foreground">{widget.options.subtitle}</div>
      ) : null}
    </div>
  );
}

function DeltaChip({ delta }) {
  const Icon =
    delta.direction === 'up' ? TrendingUp : delta.direction === 'down' ? TrendingDown : Minus;
  const color =
    delta.direction === 'up'
      ? 'text-[hsl(var(--chart-2))]'
      : delta.direction === 'down'
        ? 'text-destructive'
        : 'text-muted-foreground';
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium tabular-nums ${color}`}
    >
      <Icon className="h-3 w-3" />
      {delta.pct}%
    </span>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/widgets/KpiWidget.jsx frontend/src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx && git commit -m "feat(dashboard): KPI threshold coloring (green/amber/red)"
```

---

### Task 7: Frontend date-range control

**Files:**
- Create: `src/components/dashboard/DateRangeControl.jsx`
- Modify: `src/components/dashboard/DashboardViewer.jsx`
- Test: `src/components/dashboard/__tests__/DashboardViewer.daterange.test.jsx`

**Interfaces:**
- Produces: `DateRangeControl` (presets 7d/30d/90d/YTD/custom + two date inputs); viewer holds `dateRange` state, a `paramsRef` updated each render, and calls `refresh()` on change. Polling effect body unchanged.

- [ ] **Step 1: Write the failing test**

```jsx
// src/components/dashboard/__tests__/DashboardViewer.daterange.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />,
  Legend: () => <div />,
  LineChart: () => <div />,
  BarChart: () => <div />,
  Line: () => <div />,
  Bar: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  PieChart: () => <div />,
  Pie: () => <div />,
  Cell: () => <div />,
}));

const queryDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({ queryDashboard: (...a) => queryDashboard(...a) }));

import DashboardViewer from '../DashboardViewer';

const dash = {
  id: 'd1', refresh_interval_seconds: 30,
  definition: { widgets: [{ id: 'w1', type: 'table', title: 'T', sql: 'SELECT 1 AS n' }] },
};

describe('DashboardViewer date-range', () => {
  beforeEach(() => {
    queryDashboard.mockReset();
    queryDashboard.mockResolvedValue({ results: { w1: { columns: ['n'], rows: [{ n: 1 }], error: null } }, refreshed_at: '2026-07-29T00:00:00Z' });
  });

  it('queries with a from/to on mount', async () => {
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled());
    const body = JSON.parse(queryDashboard.mock.calls[0][1].body);
    expect(body.from).toBeTruthy();
    expect(body.to).toBeTruthy();
  });

  it('re-queries when preset changes', async () => {
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled());
    queryDashboard.mockClear();
    fireEvent.change(screen.getByLabelText(/range/i), { target: { value: '7d' } });
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled());
    const body = JSON.parse(queryDashboard.mock.calls[0][1].body);
    // 7d window: roughly 7 days between from and to
    expect(new Date(body.to) - new Date(body.from)).toBeGreaterThan(6 * 86400000);
    expect(new Date(body.to) - new Date(body.from)).toBeLessThan(8 * 86400000);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.daterange.test.jsx`
Expected: FAIL — no date-range control / params not passed

- [ ] **Step 3: Write minimal implementation**

Create `src/components/dashboard/DateRangeControl.jsx`:

```jsx
export function presetToRange(preset) {
  const to = new Date();
  let from = new Date();
  if (preset === '7d') from.setDate(to.getDate() - 7);
  else if (preset === '30d') from.setDate(to.getDate() - 30);
  else if (preset === '90d') from.setDate(to.getDate() - 90);
  else if (preset === 'ytd') from = new Date(to.getFullYear(), 0, 1);
  return { preset, from: from.toISOString(), to: to.toISOString() };
}

export default function DateRangeControl({ value, onChange }) {
  return (
    <div className="flex items-center gap-2">
      <select
        aria-label="Date range"
        value={value.preset}
        onChange={(e) => onChange(e.target.value === 'custom' ? { ...value, preset: 'custom' } : presetToRange(e.target.value))}
        className="rounded-md border border-border bg-background px-2 py-1 text-xs"
      >
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
        <option value="90d">Last 90 days</option>
        <option value="ytd">Year to date</option>
        <option value="custom">Custom</option>
      </select>
      {value.preset === 'custom' && (
        <>
          <input
            type="date"
            aria-label="From date"
            value={value.from ? value.from.slice(0, 10) : ''}
            onChange={(e) => onChange({ ...value, from: new Date(e.target.value).toISOString() })}
            className="rounded-md border border-border bg-background px-2 py-1 text-xs"
          />
          <input
            type="date"
            aria-label="To date"
            value={value.to ? value.to.slice(0, 10) : ''}
            onChange={(e) => onChange({ ...value, to: new Date(e.target.value).toISOString() })}
            className="rounded-md border border-border bg-background px-2 py-1 text-xs"
          />
        </>
      )}
    </div>
  );
}
```

Modify `src/components/dashboard/DashboardViewer.jsx` — add imports, state, paramsRef, control bar, and re-query effect. The polling effect stays verbatim. The full new file:

```jsx
import { useEffect, useRef, useState, useCallback } from 'react';
import { RefreshCw } from 'lucide-react';
import { queryDashboard } from '@/api/dashboards';
import WidgetShell from './widgets/WidgetShell';
import KpiWidget from './widgets/KpiWidget';
import TrendChartWidget from './widgets/TrendChartWidget';
import PieWidget from './widgets/PieWidget';
import TableWidget from './widgets/TableWidget';
import DateRangeControl, { presetToRange } from './DateRangeControl';

export default function DashboardViewer({ dashboard, polling = true }) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateRange, setDateRange] = useState(() => presetToRange('30d'));
  const timerRef = useRef(null);
  const visibleRef = useRef(true);
  const mountedRef = useRef(true);
  const initRef = useRef(false);
  const paramsRef = useRef({});

  // filters (cross-widget) and drill are added in Tasks 8/9; stubbed here.
  const [filters, setFilters] = useState({});
  const [drill, setDrill] = useState(null);

  const params = {
    from: dateRange.from || undefined,
    to: dateRange.to || undefined,
    filters: Object.keys(filters).length ? filters : undefined,
    drill: drill ? { widget_id: drill.widgetId, value: drill.value } : undefined,
  };
  paramsRef.current = params;

  const refresh = useCallback(async () => {
    if (!dashboard?.id || !visibleRef.current) return;
    setLoading(true);
    try {
      const res = await queryDashboard(dashboard.id, paramsRef.current);
      if (!mountedRef.current) return;
      setResults(res.results || {});
      setRefreshedAt(res.refreshed_at);
    } catch (e) {
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

  // Re-query immediately when an interaction changes params (skip initial mount;
  // the polling effect already refreshes on mount).
  useEffect(() => {
    if (!initRef.current) { initRef.current = true; return; }
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dateRange, filters, drill, refresh]);

  const widgets = dashboard?.definition?.widgets || [];
  const kpis = widgets.filter((w) => w.type === 'kpi');
  const others = widgets.filter((w) => w.type !== 'kpi');

  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
        <div className="flex items-center gap-3">
          <DateRangeControl value={dateRange} onChange={setDateRange} />
          <p className="text-xs text-muted-foreground">
            {widgets.length} widget{widgets.length === 1 ? '' : 's'}
            {' · '}
            Updated {refreshedAt ? new Date(refreshedAt).toLocaleTimeString() : '—'}
          </p>
        </div>
        <button
          onClick={refresh}
          aria-label="Refresh dashboard"
          className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>
      {kpis.length > 0 && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {kpis.map((w) => (
            <Widget key={w.id} widget={w} result={results[w.id]} loading={loading} />
          ))}
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {others.map((w) => (
          <Widget key={w.id} widget={w} result={results[w.id]} loading={loading} />
        ))}
      </div>
    </div>
  );
}

function Widget({ widget, result, loading }) {
  const err = result?.error;
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  const firstLoad = loading && result === undefined;
  const empty = !err && !firstLoad && rows.length === 0;
  return (
    <WidgetShell title={widget.title} loading={firstLoad} error={err} empty={empty}>
      {widget.type === 'kpi' ? (
        <KpiWidget widget={widget} rows={rows} />
      ) : widget.type === 'line' ? (
        <TrendChartWidget kind="line" widget={widget} rows={rows} />
      ) : widget.type === 'bar' ? (
        <TrendChartWidget kind="bar" widget={widget} rows={rows} />
      ) : widget.type === 'pie' ? (
        <PieWidget widget={widget} rows={rows} />
      ) : (
        <TableWidget cols={cols} rows={rows} />
      )}
    </WidgetShell>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.daterange.test.jsx`
Expected: PASS (2 tests). Also re-run `src/components/dashboard/__tests__/DashboardViewer.test.jsx` to confirm no regression.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/DateRangeControl.jsx frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/__tests__/DashboardViewer.daterange.test.jsx && git commit -m "feat(dashboard): date-range selector with parameterized query"
```

---

### Task 8: Frontend cross-widget filtering

**Files:**
- Create: `src/components/dashboard/interactions.js`
- Create: `src/components/dashboard/FilterChips.jsx`
- Modify: `src/components/dashboard/widgets/PieWidget.jsx`, `TableWidget.jsx`, `TrendChartWidget.jsx`
- Modify: `src/components/dashboard/DashboardViewer.jsx` (wire `onAction` + FilterChips)
- Test: `src/components/dashboard/__tests__/interactions.test.js`

**Interfaces:**
- Produces: `resolveClick(widget, column, value) -> null | {kind:'drill',widgetId,value} | {kind:'filter',token,value}`. Widgets accept an `onCellClick(column, value)` prop. Viewer `handleAction(action)` toggles filters.

- [ ] **Step 1: Write the failing test**

```js
// src/components/dashboard/__tests__/interactions.test.js
import { describe, it, expect } from 'vitest';
import { resolveClick } from '../interactions';

const widget = (options) => ({ id: 'w1', options });

describe('resolveClick', () => {
  it('drills when widget has drill matching the column', () => {
    const a = resolveClick(widget({ drill: { value_column: 'cat', sql: 'SELECT 1' } }), 'cat', 'Books');
    expect(a).toEqual({ kind: 'drill', widgetId: 'w1', value: 'Books' });
  });

  it('filters when a declared dimension matches the column', () => {
    const a = resolveClick(widget({ dimensions: [{ token: 'region', column: 'region' }] }), 'region', 'Asia');
    expect(a).toEqual({ kind: 'filter', token: 'region', value: 'Asia' });
  });

  it('returns null when no drill and no matching dimension', () => {
    expect(resolveClick(widget({}), 'region', 'Asia')).toBeNull();
  });

  it('returns null for empty value', () => {
    expect(resolveClick(widget({ dimensions: [{ token: 'r', column: 'r' }] }), 'r', '')).toBeNull();
  });

  it('drill takes precedence over dimension', () => {
    const a = resolveClick(widget({ drill: { value_column: 'cat', sql: 'SELECT 1' }, dimensions: [{ token: 'cat', column: 'cat' }] }), 'cat', 'X');
    expect(a.kind).toBe('drill');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/interactions.test.js`
Expected: FAIL — `resolveClick` not defined

- [ ] **Step 3: Write minimal implementation**

Create `src/components/dashboard/interactions.js`:

```js
// Decide what a widget click should do. Pure — unit-tested in isolation.
// Returns null, {kind:'drill',widgetId,value}, or {kind:'filter',token,value}.
export function resolveClick(widget, column, value) {
  if (value == null || value === '') return null;
  const opts = widget?.options || {};
  const drill = opts.drill;
  if (drill && drill.value_column === column) {
    return { kind: 'drill', widgetId: widget.id, value: String(value) };
  }
  const dims = opts.dimensions || [];
  const dim = dims.find((d) => d.column === column);
  if (dim) return { kind: 'filter', token: dim.token, value: String(value) };
  return null;
}
```

Create `src/components/dashboard/FilterChips.jsx`:

```jsx
import { X } from 'lucide-react';

export default function FilterChips({ filters, onClear }) {
  const entries = Object.entries(filters || {});
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {entries.map(([token, value]) => (
        <button
          key={token}
          onClick={() => onClear(token)}
          className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground transition-colors hover:bg-secondary/70"
        >
          {token}: {value}
          <X className="h-3 w-3" />
        </button>
      ))}
    </div>
  );
}
```

In `PieWidget.jsx`, add an `onCellClick` prop and a click handler:

```jsx
export default function PieWidget({ widget, rows, onCellClick }) {
  const name = widget.options?.name_column;
  const val = widget.options?.value_column;
  const data = rows.map((r, i) => ({
    name: name ? String(r[name]) : `slice ${i + 1}`,
    value: val ? Number(r[val]) : 0,
    fill: SERIES_COLORS[i % SERIES_COLORS.length],
  }));
  const config = Object.fromEntries(data.map((d) => [d.name, { label: d.name }]));
  return (
    <ChartContainer config={config} className="h-[220px] w-full">
      <PieChart>
        <ChartTooltip content={<ChartTooltipContent nameKey="name" hideLabel />} />
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={48}
          outerRadius={72}
          strokeWidth={2}
          onClick={(d) => onCellClick && name && onCellClick(name, d && d.name)}
        >
          {data.map((d, i) => (
            <Cell key={i} fill={d.fill} />
          ))}
        </Pie>
        <ChartLegend content={<ChartLegendContent nameKey="name" />} />
      </PieChart>
    </ChartContainer>
  );
}
```

In `TrendChartWidget.jsx`, add `onCellClick` prop and bar click (bar only):

```jsx
export default function TrendChartWidget({ kind, widget, rows, onCellClick }) {
  const x = widget.options?.x_column;
  const y = widget.options?.y_column;
  const data = rows.map((r) => ({
    name: x ? r[x] : '',
    value: y ? Number(r[y]) : Number(Object.values(r)[0] || 0),
  }));
  const config = { value: { label: widget.title, color: 'hsl(var(--chart-1))' } };
  const Comp = kind === 'bar' ? BarChart : LineChart;
  return (
    <ChartContainer config={config} className="h-[200px] w-full">
      <Comp data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} tick={{ fontSize: 11 }} />
        <YAxis
          tickLine={false}
          axisLine={false}
          tickMargin={4}
          tick={{ fontSize: 11 }}
          tickFormatter={formatAxisTick}
          width={44}
        />
        <ChartTooltip content={<ChartTooltipContent />} />
        {kind === 'bar' ? (
          <Bar dataKey="value" fill="var(--color-value)" radius={[4, 4, 0, 0]} maxBarSize={40}
               onClick={(d) => onCellClick && x && onCellClick(x, d && d.name)} />
        ) : (
          <Line type="monotone" dataKey="value" stroke="var(--color-value)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} />
        )}
      </Comp>
    </ChartContainer>
  );
}
```

In `TableWidget.jsx`, add `widget` + `onCellClick` props; row click filters on the first declared dimension column present in the row:

```jsx
export default function TableWidget({ cols, rows, widget, onCellClick }) {
  const numeric = new Set(
    cols.filter((c) => rows.some((r) => r[c] !== null && r[c] !== '' && Number.isFinite(Number(r[c])))),
  );
  const dims = widget?.options?.dimensions || [];
  const clickCol = dims.find((d) => cols.includes(d.column));
  return (
    <div className="max-h-56 overflow-auto rounded-md border border-border/60">
      <table className="w-full text-xs">
        <thead className="sticky top-0 z-10 bg-muted/90 backdrop-blur">
          <tr>
            {cols.map((c) => (
              <th key={c} className={`px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground ${numeric.has(c) ? 'text-right' : 'text-left'}`}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 50).map((r, i) => (
            <tr key={i}
                onClick={() => onCellClick && clickCol && onCellClick(clickCol.column, r[clickCol.column])}
                className={`border-t border-border/50 transition-colors hover:bg-muted/50 ${clickCol ? 'cursor-pointer' : ''}`}>
              {cols.map((c) => (
                <td key={c} className={`px-3 py-1.5 ${numeric.has(c) ? 'text-right tabular-nums' : ''}`}>{formatCell(r[c], numeric.has(c))}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

In `DashboardViewer.jsx`, wire `handleAction` + `FilterChips`. Add to the component (the `filters`/`drill` state already exist from Task 7):

```jsx
import FilterChips from './FilterChips';
import { resolveClick } from './interactions';

// inside component, after setDrill/setFilters declarations:
const handleAction = useCallback((action) => {
  if (!action) return;
  if (action.kind === 'drill') {
    setDrill({ widgetId: action.widgetId, value: action.value });
  } else if (action.kind === 'filter') {
    setFilters((f) => {
      if (f[action.token] === action.value) {
        const next = { ...f };
        delete next[action.token];
        return next; // toggle off
      }
      return { ...f, [action.token]: action.value };
    });
  }
}, []);
```

Update the `Widget` component to accept `onAction` and pass `onCellClick` down:

```jsx
function Widget({ widget, result, loading, onAction }) {
  const err = result?.error;
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  const firstLoad = loading && result === undefined;
  const empty = !err && !firstLoad && rows.length === 0;
  const onCellClick = (column, value) => onAction && onAction(resolveClick(widget, column, value));
  return (
    <WidgetShell title={widget.title} loading={firstLoad} error={err} empty={empty}>
      {widget.type === 'kpi' ? (
        <KpiWidget widget={widget} rows={rows} />
      ) : widget.type === 'line' ? (
        <TrendChartWidget kind="line" widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'bar' ? (
        <TrendChartWidget kind="bar" widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'pie' ? (
        <PieWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
    </WidgetShell>
  );
}
```

Pass `onAction={handleAction}` to both `<Widget .../>` usages (KPI + others). Add `<FilterChips filters={filters} onClear={(t) => setFilters((f) => { const n = {...f}; delete n[t]; return n; })} />` in the header row next to the date control.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/interactions.test.js`
Expected: PASS (5 tests). Also re-run the daterange + existing viewer tests to confirm no regression.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/interactions.js frontend/src/components/dashboard/FilterChips.jsx frontend/src/components/dashboard/widgets/ frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/__tests__/interactions.test.js && git commit -m "feat(dashboard): cross-widget filtering via dimension tokens"
```

---

### Task 9: Frontend drill-down panel

**Files:**
- Create: `src/components/dashboard/DrillPanel.jsx`
- Modify: `src/components/dashboard/DashboardViewer.jsx` (render `__drill__` below source widget)
- Test: extend `src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx`

**Interfaces:**
- Consumes: `results['__drill__']` = `{columns, rows, error, source_widget_id, drill_value}`.
- Produces: an inline `DrillPanel` below the source widget with a close button.

- [ ] **Step 1: Write the failing test** (append to `DashboardViewer.interactivity.test.jsx`)

```jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />,
  Legend: () => <div />,
  LineChart: () => <div />,
  BarChart: () => <div />,
  Line: () => <div />,
  Bar: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  PieChart: () => <div />,
  Pie: () => <div />,
  Cell: () => <div />,
}));

const queryDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({ queryDashboard: (...a) => queryDashboard(...a) }));
import DashboardViewer from '../DashboardViewer';

const dash = {
  id: 'd1', refresh_interval_seconds: 30,
  definition: { widgets: [{ id: 'w1', type: 'bar', title: 'By Category', sql: 'SELECT cat, n FROM t',
    options: { x_column: 'cat', y_column: 'n', drill: { value_column: 'cat', sql: 'SELECT product, n FROM t WHERE cat = :drill_value', title: 'Drill' } } }] },
};

describe('DashboardViewer drill-down', () => {
  beforeEach(() => {
    queryDashboard.mockReset();
    queryDashboard.mockResolvedValue({ results: { w1: { columns: ['cat', 'n'], rows: [{ cat: 'Books', n: 5 }], error: null } }, refreshed_at: '2026-07-29T00:00:00Z' });
  });

  it('opens drill panel and queries with drill param on bar click', async () => {
    const { container } = render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled());
    queryDashboard.mockResolvedValueOnce({
      results: {
        w1: { columns: ['cat', 'n'], rows: [{ cat: 'Books', n: 5 }], error: null },
        __drill__: { columns: ['product', 'n'], rows: [{ product: 'Dune', n: 2 }], error: null, source_widget_id: 'w1', drill_value: 'Books' },
      },
      refreshed_at: '2026-07-29T00:00:00Z',
    });
    // The bar is rendered by the mocked BarChart; its onClick won't fire in jsdom.
    // Instead, invoke the viewer's action directly via the exposed prop path:
    // find the bar element by recharts mock — here we assert the panel renders
    // once __drill__ is present in results by re-rendering.
    render(<DashboardViewer dashboard={dash} polling={false} />);
    // Clicking is covered by the unit test for resolveClick (Task 8). Here we
    // verify that when __drill__ is in results, a DrillPanel title shows.
    // (queryDashboard is mocked; we simulate by setting state via a 2nd render
    //  with the resolved payload already returned.)
    await waitFor(() => {
      // The drill result table includes 'Dune' once the panel renders.
      expect(screen.queryByText('Dune') || true).toBeTruthy();
    });
  });
});
```

Note: jsdom cannot fire recharts `onClick` (SVG mock). The drill *trigger* logic is covered by `resolveClick` unit tests (Task 8). This test asserts the panel renders a close button when `__drill__` is present. Replace the above body with the focused assertion:

```jsx
  it('renders DrillPanel with close button when __drill__ present', async () => {
    queryDashboard.mockResolvedValue({
      results: {
        w1: { columns: ['cat', 'n'], rows: [{ cat: 'Books', n: 5 }], error: null },
        __drill__: { columns: ['product', 'n'], rows: [{ product: 'Dune', n: 2 }], error: null, source_widget_id: 'w1', drill_value: 'Books' },
      },
      refreshed_at: '2026-07-29T00:00:00Z',
    });
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(screen.getByText('Dune')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /close drill/i })).toBeInTheDocument();
  });

  it('clears drill when close button clicked', async () => {
    queryDashboard.mockResolvedValue({
      results: {
        w1: { columns: ['cat', 'n'], rows: [{ cat: 'Books', n: 5 }], error: null },
        __drill__: { columns: ['product', 'n'], rows: [{ product: 'Dune', n: 2 }], error: null, source_widget_id: 'w1', drill_value: 'Books' },
      },
      refreshed_at: '2026-07-29T00:00:00Z',
    });
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(screen.getByText('Dune')).toBeInTheDocument());
    queryDashboard.mockClear();
    fireEvent.click(screen.getByRole('button', { name: /close drill/i }));
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled());
    const body = JSON.parse(queryDashboard.mock.calls[0][1].body);
    expect(body.drill).toBeUndefined();
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx`
Expected: FAIL — no DrillPanel rendered

- [ ] **Step 3: Write minimal implementation**

Create `src/components/dashboard/DrillPanel.jsx`:

```jsx
import { X } from 'lucide-react';
import TableWidget from './widgets/TableWidget';

export default function DrillPanel({ drill, result, onClose }) {
  if (!drill) return null;
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  const err = result?.error;
  return (
    <div className="rounded-xl border border-border bg-card p-3 shadow-sm">
      <div className="mb-2 flex items-center justify-between">
        <p className="text-xs font-medium text-muted-foreground">
          Drill: {drill.value} {rows.length ? `· ${rows.length} row${rows.length === 1 ? '' : 's'}` : ''}
        </p>
        <button
          onClick={onClose}
          aria-label="Close drill"
          className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {err ? (
        <p className="text-xs text-destructive">{err}</p>
      ) : rows.length ? (
        <TableWidget cols={cols} rows={rows} />
      ) : (
        <p className="text-xs text-muted-foreground">No rows.</p>
      )}
    </div>
  );
}
```

In `DashboardViewer.jsx`, import `DrillPanel` and render it below the source widget in the `others` grid:

```jsx
import DrillPanel from './DrillPanel';
```

Replace the `others.map` block:

```jsx
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {others.map((w) => (
          <div key={w.id} className="flex flex-col gap-3">
            <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
            {drill && drill.widgetId === w.id && (
              <DrillPanel drill={drill} result={results['__drill__']} onClose={() => setDrill(null)} />
            )}
          </div>
        ))}
      </div>
```

Also add `onAction={handleAction}` to the KPI `<Widget>` usages (KPIs don't drill, but the prop is harmless and keeps the signature uniform).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx`
Expected: PASS (2 tests). Re-run the full dashboard test dir to confirm no regression.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/DrillPanel.jsx frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx && git commit -m "feat(dashboard): inline drill-down panel"
```

---

### Task 10: Verification + build

**Files:** none (verification only)

- [ ] **Step 1: Run all scoped backend tests**

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"
python3 -m pytest tests/test_dashboard_query_tokens.py tests/test_dashboard_query_params_drill.py tests/test_dashboard_clarification_guidance.py tests/test_dashboard_design_skills_load.py tests/test_dashboards_api.py tests/test_dashboard_query.py -v
```
Expected: all PASS.

- [ ] **Step 2: Run all scoped frontend tests**

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/dashboard src/api/__tests__/dashboards.test.js
```
Expected: all PASS (incl. pre-existing `DashboardViewer.test.jsx`, `DashboardPopup.test.jsx`).

- [ ] **Step 3: Lint check**

```bash
cd /root/zhanlu/frontend && npx eslint src/components/dashboard src/api/dashboards.js
```
Expected: 0 errors.

- [ ] **Step 4: Production build**

```bash
cd /root/zhanlu/frontend && npm run build
```
Expected: build succeeds (only pre-existing >500kB chunk warning, if any).

- [ ] **Step 5: Report**

Summarize: tasks completed, test counts, any deviations. Do NOT deploy unless the user asks (deploy = `/opt/rebuild-zhanlu.sh --force`).

---

## Self-Review

**1. Spec coverage:**
- §4 token render layer → Task 1 ✓
- §4.3 `run_dashboard_query` params → Task 2 ✓
- §5.1 date-range selector (backend tokens + router + frontend control) → Tasks 1, 2, 7 ✓
- §5.2 KPI threshold coloring → Task 6 ✓
- §5.3 drill-down (backend exec + create validation + frontend panel) → Tasks 2, 3, 9 ✓
- §5.4 cross-widget filtering → Task 8 ✓
- §5.5 agent clarification gate → Task 4 ✓
- §6 API contract (optional body, backward compat) → Task 2 ✓
- §7 widget `options` extensions documented → Task 4 ✓
- §8 files touched → all mapped across tasks ✓
- §9 test plan → Tasks 1, 2, 3, 4, 6, 7, 8, 9 + Task 10 verification ✓

**2. Placeholder scan:** No TBD/TODO/"add appropriate" — all steps contain real test code and real implementation code. The Task 9 jsdom recharts-onClick limitation is explicitly handled by (a) asserting the panel renders from `__drill__` results and (b) delegating trigger logic to the `resolveClick` unit test in Task 8.

**3. Type consistency:**
- `render_widget_sql(sql, params, dimensions)` — same signature in Tasks 1, 2, 3.
- `run_dashboard_query(db, dashboard, params=None)` — Task 2 defines, Task 3 drill branch uses same `params` shape.
- `QueryParams` fields `from_`/`to`/`filters`/`drill` — router maps `from_`→`"from"` key, which `render_widget_sql` reads via `params.get("from")`. Consistent.
- `resolveClick(widget, column, value)` — same in Task 8 test + Task 8 impl + Task 9 viewer wiring.
- `handleAction(action)` with `action.kind` ∈ `'drill'`/`'filter'` — consistent across Tasks 8, 9.
- Drill result key `'__drill__'` — consistent in backend Task 2 + frontend Task 9.
- `drill` state shape `{widgetId, value}` in viewer; API body `{widget_id, value}` — viewer explicitly maps (`drill: drill ? { widget_id: drill.widgetId, value: drill.value } : undefined`). Consistent.
- Threshold tokens: `text-[hsl(var(--chart-2))]` (green), `text-amber-500` (warn), `text-destructive` (bad) — consistent in Task 6 test + impl.

No gaps found. Plan is ready.
