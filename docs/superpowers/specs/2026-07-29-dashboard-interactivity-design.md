# Dashboard Interactivity — Phase 1 Design Spec

**Date:** 2026-07-29
**Status:** Approved (Approach A — Interactive → Design → Editable; Phase 1 scope locked)
**Owner:** zhanlu

## 1. Context

The dashboard is already a live React + recharts + AJAX-polling app (NOT static HTML):

- Backend: `create_dashboard` tool + `POST /dashboards/{id}/query` → `run_dashboard_query` runs each widget's read-only SQL via `QueryService.execute`, isolated per-widget.
- Frontend: `DashboardViewer.jsx` polls (10–300s clamp, pauses on tab-hidden) and routes to 5 widget components (KPI, line, bar, pie, table) backed by shadcn `chart.jsx` + `hsl(var(--chart-N))` tokens.

**Gap vs `ui-ux-pro-max` recommendations** (Analytics Dashboard = Data-Dense + Drill-Down): no drill-down, no chart zoom, no filter animations, no date-range selector, no KPI threshold coloring, no cross-widget filtering, no export; only 5 chart types vs 25.

This spec covers **Phase 1 — Interactivity** (4 features). Phases 2 (design breadth) and 3 (editability) are out of scope here.

## 2. Goals

1. **Date-range selector** with parameterized SQL tokens (`:from`/`:to`/`:date`), default last-30-days.
2. **Drill-down**: click a bar/slice/table-row → filtered detail table renders inline below the source widget.
3. **KPI threshold coloring**: KPI value turns green/amber/red based on `options.thresholds`.
4. **Cross-widget filtering**: click a pie slice / table row → all widgets that declare that dimension re-query filtered to it.

## 3. Non-goals (Phase 1)

- Chart zoom, chart-type expansion, export, dark-mode toggle (Phase 2).
- Widget CRUD / reorder / inline SQL edit (Phase 3).
- Dashboard definition schema migration — all new behavior is opt-in via `options` fields; existing dashboards keep working unchanged.

## 4. Unified design: whitelisted token render layer

All four features share one safe substitution mechanism. **Stored widget SQL is never trusted** (existing invariant); tokens are rendered server-side from a closed whitelist and the rendered SQL is re-validated before execution.

### 4.1 Token grammar

| Token | Source | Renders to (set) | Renders to (unset) | Example widget SQL |
|-------|--------|-------------------|--------------------|--------------------|
| `:from` | date window start | `'YYYY-MM-DD HH:MM:SS'` | `'1970-01-01 00:00:00'` | `WHERE created_at >= :from` |
| `:to` | date window end | `'YYYY-MM-DD HH:MM:SS'` | `now` literal | `AND created_at < :to` |
| `:date` | window end date | `'YYYY-MM-DD'` | today | `GROUP BY DATE(created_at) HAVING DATE(created_at) <= :date` |
| `:dim_<token>` | cross-widget filter | `<trusted_column> = 'escaped_value'` | `1=1` | `WHERE ... AND :dim_region` |
| `:drill_value` | clicked cell value | `'escaped_value'` | (drill SQL always sets it) | `WHERE category = :drill_value` |

**Safety model:**
- Only the above token shapes are recognized (`:from`, `:to`, `:date`, `:dim_<identifier>`, `:drill_value`). Unknown `:xxx` → render error (widget shows error, does NOT execute).
- Date values are constructed from parsed `datetime` → ISO format; never user-supplied raw strings.
- `:dim_<token>` column names come from **trusted widget `options.dimensions`** (authored by the agent at create time, not user input); only the *value* is user-supplied and is escaped by doubling single quotes.
- After substitution, the rendered SQL is passed through the existing `validate_widget_sql` (read-only + single-statement guard) **again**. If it fails, the widget returns an error payload, never executes.

### 4.2 `render_widget_sql(sql, params)` (new, `dashboard_query.py`)

```python
import datetime as _dt

_TOKEN_RE = re.compile(r":(from|to|date|drill_value|dim_[a-zA-Z_][a-zA-Z0-9_]*)\b")

def _literal(s: str) -> str:
    """Quote a string as a SQL literal, escaping embedded single quotes."""
    return "'" + str(s).replace("'", "''") + "'"

def _default_window():
    to = _dt.datetime.now(_dt.timezone.utc)
    frm = to - _dt.timedelta(days=30)
    return frm, to

def render_widget_sql(sql: str, params: dict, dimensions: list[dict]) -> str:
    """Substitute whitelisted tokens. Raises ValueError on unknown token."""
    frm, to = _resolve_window(params)
    dim_map = {d["token"]: d["column"] for d in (dimensions or [])}
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
    # Defense in depth: re-validate the rendered SQL before it reaches the DB.
    validate_widget_sql(rendered)
    return rendered
```

`_resolve_window(params)` parses `params.from`/`params.to` (ISO 8601); if absent, uses last-30-days default.

### 4.3 `run_dashboard_query` change

Signature becomes `run_dashboard_query(db, dashboard, params=None)`. Each widget:
1. Reads `dimensions = widget.options.get("dimensions", [])`.
2. `rendered = render_widget_sql(widget.sql, params or {}, dimensions)`.
3. `validate_widget_sql(rendered)` (render already does this; kept explicit for clarity).
4. `QueryService.execute(..., rendered, ...)`.

A render error sets that widget's `error` and does NOT abort others (same isolation as today).

## 5. Feature details

### 5.1 Date-range selector

**Backend:** token render layer (§4). No new endpoint; `query` accepts params.

**Router** (`app/routers/dashboards.py`):
```python
from pydantic import BaseModel

class QueryParams(BaseModel):
    from_: str | None = None   # ISO 8601
    to: str | None = None
    filters: dict[str, str] | None = None     # cross-widget: {token: value}
    drill: dict | None = None                 # {widget_id, value} for drill-down

@router.post("/{dashboard_id}/query")
async def query_dashboard(dashboard_id, body: QueryParams | None = None, db=..., user=...):
    d = _scoped(db, user, dashboard_id)
    return await run_dashboard_query(db, d, body.model_dump(exclude_none=True) if body else None)
```
`body` is optional → existing callers that POST empty still work.

**Frontend** (`api/dashboards.js`):
```js
export function queryDashboard(id, params) {
  return authFetch(`${BASE}/${id}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params || {}),
  }).then(j);
}
```

**Frontend** (`DashboardViewer.jsx`): add a date-range control bar above the KPI grid.
- Presets: 7d / 30d / 90d / Custom (two `<input type="date">`).
- State: `const [window, setWindow] = useState({ preset: '30d' })`.
- `refresh` includes computed `from`/`to` ISO strings in the query body.
- Changing the window calls `refresh()` immediately (debounced 250ms for custom typing).
- Polling continues to pass the current window.
- The control is **always present**; widgets without date tokens ignore the params (no behavior change).

### 5.2 KPI threshold coloring (pure frontend)

Extend `KpiWidget` to read `options.thresholds`:
```js
// options.thresholds = {
//   direction: 'higher_is_worse' | 'lower_is_worse',
//   warning: <number>,
//   critical: <number>,
// }
```
Logic:
- `higher_is_worse`: value ≥ critical → red (`text-destructive`); value ≥ warning → amber (`text-amber-500`); else green (`text-chart-2`).
- `lower_is_worse`: value ≤ critical → red; value ≤ warning → amber; else green.
- If `thresholds` absent → no coloring (current behavior).
- Also tint the delta chip border subtly to match.

No backend change. The `create_dashboard` tool schema `options` description is extended to document `thresholds`.

**Widget schema doc update** (`dashboard_tools.py` schema `options.description`): append
`"kpi also supports thresholds: {direction:'higher_is_worse'|'lower_is_worse', warning:<n>, critical:<n>} → value colors green/amber/red."`

### 5.3 Drill-down

A widget opts into drill-down via `options.drill`:
```js
options.drill = {
  // value_column: which returned column supplies the drill value when a bar/slice/row is clicked
  value_column: 'category',
  // sql: read-only SELECT using :drill_value token. Also re-rendered with current date window.
  sql: 'SELECT product, SUM(amount) FROM orders WHERE category = :drill_value AND created_at >= :from GROUP BY product ORDER BY 2 DESC LIMIT 50',
  // title: label for the inline detail panel
  title: 'Drill: details for {value}',
}
```

**Interaction:**
- `TrendChartWidget` (bar) / `PieWidget`: `onClick` on a bar/slice reads the datum's `value_column` field, sets `drillState = { widgetId, value }`, and the viewer re-queries with `drill: { widget_id, value }`.
- `TableWidget`: row click does the same using the row's `value_column`.
- The backend, when `params.drill` is present, runs ONLY the drill widget's `options.drill.sql` (rendered with `:drill_value` + current window) and returns it as an extra result keyed `__drill__`. The viewer renders it as an inline `<TableWidget>` in a collapsible panel directly below the source widget.
- Clearing drill = clicking a "close" button on the panel → `drillState = null`.

**Backend:** `run_dashboard_query` — if `params.drill`:
1. Find the widget with `id == params.drill.widget_id`.
2. Read `widget.options.drill.sql`; `validate_widget_sql` it at create time (extend `_validate_definition` / `WidgetSpec` to validate drill SQL too).
3. `render_widget_sql(drill_sql, {**params, 'drill_value': params.drill.value}, dimensions)`.
4. Execute; return result as `results['__drill__'] = { columns, rows, error, drill_value, source_widget_id }`.
- Normal widgets still run (so the dashboard stays live); drill is additive.

**Create-time validation:** `WidgetSpec` gets a `@model_validator` that, if `options.drill` present, calls `validate_widget_sql(options.drill.sql)` and requires `options.drill.value_column` + `options.drill.sql`. The router's `_validate_definition` mirrors this for the REST path.

### 5.4 Cross-widget filtering

A widget opts into being filterable by declaring dimensions:
```js
options.dimensions = [
  { token: 'region', column: 'region' },
  { token: 'status', column: 'status' },
]
```
Widget SQL uses the corresponding tokens:
```sql
SELECT region, SUM(amount) AS total
FROM orders
WHERE created_at >= :from AND :dim_region AND :dim_status
GROUP BY region
```

**Interaction:**
- `PieWidget`: clicking a slice sets a global filter `{ token: <widget's first dimension>, value: <slice name> }`.
- `TableWidget`: clicking a row sets a filter using the row's value for a declared dimension column (heuristic: if a table column name matches a declared `column`, clicking the row filters on it).
- `TrendChartWidget` (bar): clicking a bar sets a filter on the x-axis category if the widget declares a matching dimension.
- The viewer holds `filters: { [token]: value }` in state. Setting a filter re-queries all widgets with `filters` in the body. Setting the same token again toggles it off.
- A filter chip bar below the date-range control shows active filters with × to clear.

**Backend:** token render layer (§4.2) handles `:dim_<token>`. No new endpoint.

### 5.5 Agent clarification gate (creation-time guarantee)

**Problem:** Today the `create_dashboard` tool description only nudges the agent to ask about the datasource. A vague request ("make me a sales dashboard") can be silently fulfilled with guessed metrics, a guessed time window, and no filter dimensions — producing a dashboard that technically runs but isn't what the user wanted. The user explicitly wants the agent to **fall back to asking a question or presenting selectable options** whenever it is unsure, instead of guessing.

**Rule (hard):** Before calling `create_dashboard`, the agent MUST resolve these five items. For each, if the answer is ambiguous or unspecified, it asks the user (plain-language question + numbered options when discrete choices exist) and WAITS for the reply — it does not call the tool on a guess.

| # | Item | When to ask | Example options to present |
|---|------|-------------|----------------------------|
| 1 | **Datasource** | >1 DB KB available and none named (or none bound to the active project) | "A) ERP  B) test2  C) aipdp_data_warehouse" |
| 2 | **Time window / duration** | User didn't state a range | "A) Last 7 days  B) Last 30 days  C) Last 90 days  D) This year  E) Custom" — also drives the `:from`/`:to` default the viewer opens with |
| 3 | **Key metrics & columns** | Request is vague about what to measure ("show sales") | "Which metrics? A) Revenue + order count  B) Revenue by region  C) Top products by units" |
| 4 | **Filter / drill dimensions** | Not stated | "Which dimensions do you want to filter/drill by? A) Region  B) Category  C) Status  D) Date" — drives `options.dimensions` + `:dim_<token>` authoring |
| 5 | **Chart intent** | Ambiguous how to visualize | "A) KPI tiles + trend line  B) Pie breakdown + table  C) Bar comparison" |

**Only when all five are resolved** (explicitly stated by the user, or unambiguous from context, or only one viable choice) does the agent call `create_dashboard`.

**How the agent asks:** a single natural-language reply — batch the unresolved items into one message with lettered options, not five separate turns. The user picks by replying ("1A, 2B, 3B"). **No new frontend UI is needed**; this is the standard chat clarification pattern (same as the existing "ask which datasource" nudge, generalized).

**Where this is enforced (two touchpoints):**
1. **Tool description** (`dashboard_tools.py` `CREATE_DASHBOARD_SCHEMA.function.description`) — extend the existing one-line datasource nudge into an explicit "Before calling this tool, confirm: datasource, time window, key metrics, filter dimensions, chart intent. If any is ambiguous, ask the user with numbered options and wait — do not guess." This is the LLM-facing contract.
2. **`dashboard-generation` skill** (`backend/skills/dashboard-generation/SKILL.md`, step 1 of the workflow) — add a "Clarify before building" step that mirrors the five-item table above + the batch-ask pattern + a worked example. The skill is what the agent loads when designing dashboards, so the guidance is reinforced at the point of authoring.

**Self-heal alignment:** if `create_dashboard` returns a validation error (bad SQL, unknown token, etc.), the agent's existing self-heal loop already re-prompts the LLM with the error. The clarification gate is the *pre*-call analog: prevent the bad call rather than fix it after.

**Test:** source-inspect test (`tests/test_dashboard_clarification_guidance.py`) asserting both the tool description and the SKILL.md body contain the five item keywords (datasource, time window, metrics, dimensions, chart) + a "do not guess / wait" phrase. Keeps the guidance from silently regressing.

## 6. API contract (summary)

`POST /api/dashboards/{id}/query` — body (all optional):
```json
{
  "from": "2026-07-01T00:00:00",
  "to": "2026-07-29T00:00:00",
  "filters": { "region": "Asia" },
  "drill": { "widget_id": "w1", "value": "Books" }
}
```
Response unchanged shape: `{ dashboard_id, refreshed_at, results: { widgetId: {columns,rows,error,truncated}, "__drill__": {...} } }`.

Empty body `{}` → last-30-days window, no filters, no drill (current behavior preserved).

## 7. Widget `options` extensions (all opt-in, no migration)

| Feature | `options` field | Required keys |
|--------|-----------------|---------------|
| Thresholds (kpi) | `thresholds` | `direction`, `warning`, `critical` |
| Drill-down | `drill` | `value_column`, `sql`, `title` |
| Cross-widget filter | `dimensions` | array of `{token, column}` |
| Date-range | (none — uses `:from`/`:to`/`:date` tokens in `sql`) | — |

`create_dashboard` tool schema `options.description` is updated to document all of the above so the agent authors compliant widgets.

## 8. Files touched

**Backend:**
- `app/services/dashboard_query.py` — add `render_widget_sql`, `_resolve_window`, `_literal`, `_TOKEN_RE`; change `run_dashboard_query` signature + per-widget render.
- `app/routers/dashboards.py` — add `QueryParams` model; `query_dashboard` accepts body; extend `_validate_definition` to validate `drill.sql`.
- `app/services/tool_handlers/dashboard_tools.py` — `WidgetSpec` model_validator for `drill.sql`; extend schema `options.description`; **extend tool `description` with the §5.5 clarification contract**.
- `backend/skills/dashboard-generation/SKILL.md` — add "Clarify before building" step 1 (five-item table + batch-ask pattern + worked example); mirror into `docs/starter_templates/skill_library/system/visualization/dashboard-generation/SKILL.md`.

**Frontend:**
- `src/api/dashboards.js` — `queryDashboard(id, params)`.
- `src/components/dashboard/DashboardViewer.jsx` — date-range control, filter chip bar, drill panel, pass `params` through `refresh`; **polling effect stays verbatim** except it now closes over `params` (add to `refresh` deps).
- `src/components/dashboard/widgets/KpiWidget.jsx` — threshold coloring.
- `src/components/dashboard/widgets/TrendChartWidget.jsx` — bar `onClick` → drill/filter.
- `src/components/dashboard/widgets/PieWidget.jsx` — slice `onClick` → drill/filter.
- `src/components/dashboard/widgets/TableWidget.jsx` — row `onClick` → drill/filter.
- New: `src/components/dashboard/DateRangeControl.jsx`, `src/components/dashboard/FilterChips.jsx`, `src/components/dashboard/DrillPanel.jsx`.

**Tests:**
- `backend/tests/test_dashboard_query_tokens.py` (new) — token render: known/unknown tokens, unset dim → `1=1`, date defaults, re-validation rejects injection (`:from`-shaped multi-statement attempts), drill render.
- `backend/tests/test_dashboards_router.py` (extend) — `/query` with body, empty body backward-compat, drill path.
- `backend/tests/test_dashboard_clarification_guidance.py` (new) — source-inspect: tool description + SKILL.md contain the five item keywords + a "do not guess / wait" phrase (guards §5.5 against silent regression).
- `frontend/src/components/dashboard/__tests__/DashboardViewer.interactivity.test.jsx` (new) — date-range change re-queries with params; filter chip set/clear; drill panel open/close; polling includes params.
- `frontend/src/components/dashboard/widgets/__tests__/KpiWidget.thresholds.test.jsx` (new) — green/amber/red for both directions; no-thresholds = no color.

## 9. Test plan

**Backend (explicit file paths, no `-n`, per RAM constraint):**
1. `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`
2. `python3 -m pytest tests/test_dashboard_query_tokens.py -v`
3. `python3 -m pytest tests/test_dashboards_router.py -v`

**Frontend:**
- `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard` (scoped, not full suite).

**Acceptance:**
- Existing dashboards (no tokens, no new options) render identically.
- A widget with `WHERE created_at >= :from` reflects the selected window.
- KPI with thresholds changes color across the bands.
- Clicking a pie slice with a declared dimension filters every other widget that declares the same dimension.
- Clicking a bar on a widget with `options.drill` opens an inline detail table; closing it removes the panel.

## 10. Phase 2 / 3 (out of scope, recorded)

- **Phase 2 (Design):** area/scatter/gauge/radar/stacked-bar chart types; recharts zoom plugin; motion presets; PNG export + shareable link; dark-mode toggle.
- **Phase 3 (Editable):** `PATCH /dashboards/{id}` widget CRUD; drag-and-drop grid reorder; inline SQL editor with live preview; "save as copy".
