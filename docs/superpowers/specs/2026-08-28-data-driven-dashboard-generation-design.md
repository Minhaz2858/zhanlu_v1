# Data-Driven Dashboard Generation — Data Profiling Gate + Post-Build Validation

Date: 2026-08-28
Status: Approved (approach A+B)
Author: Synexia Agent

## Problem

Fullstack dashboard generation today is design-first, data-last. The agent calls
`describe_schema` (column names + types only), proposes metrics + SQL, and builds.
Nobody profiles the actual data before design, so the agent cannot know:

- Row counts — is the table even populated?
- Cardinality — 3 distinct dates (line chart is wrong) vs 300 (line chart is right)
- Null ratio — half a column is empty, KPI card shows garbage
- Date range — all 2020 data = stale "trend"
- Freshness — no "as of" stamp in the header

Consequences: empty widgets discovered only after the user opens the dashboard,
chart types guessed from names instead of data shape, and a bare "No data" card
with no reason.

## Goal

Make dashboard generation depend on real data, not guesses:

- **A. Data Profiling Gate** — profile actual data before design so every
  chart-type, KPI, and layout decision is grounded in data shape.
- **B. Post-Build Validation** — dry-run every metric SQL at build time; surface
  ok/empty/error per widget; Empty cards explain WHY.

## Scope

- Backend: new `profile_data` capability (service + tool registration), build-time
  metric dry-run validation in `_create_fullstack_dashboard` / `_update_fullstack_dashboard`.
- Template frontend: Empty widget shows the reason; optional "as of" freshness stamp.
- Agent prompt: chart-type rules driven by profile output.
- Tests: unit tests for profiler + validation; e2e verify against sales dashboard.

Out of scope (C, deferred): runtime data-adaptive chart swapping in the frontend,
auto-fixing SQL, profile caching across sessions (TTL cache within turn only).

## Architecture

### A. Data Profiling Gate

New service `backend/app/services/dashboard_profiler.py`:

```
profile_datasource(db, kb_id, tables: list[str], max_columns=20, timeout_s=12)
  -> { "tables": [
        { "name": "...", "row_count": N,
          "columns": [
            { "name": "...", "type": "text|number|date|unknown",
              "cardinality": N, "null_pct": 0.0-1.0,
              "min": ..., "max": ..., "top_values": [...3],
              "shape": "time_series|category|continuous|sparse|empty" } ] } ] }
```

Queries (cheap, bounded):
1. `SELECT COUNT(*)` per table.
2. Per selected column: `COUNT(DISTINCT col)`, `COUNT(col)/COUNT(*)` for null
   ratio, `MIN/MAX` for numeric/date, `SELECT col ... LIMIT 3` for samples.
3. Shape inference:
   - `empty` — row_count == 0 or column is all NULL
   - `sparse` — null_pct > 0.5
   - `time_series` — date/timestamp type OR string that parses as ISO date with
     cardinality >= 2
   - `category` — cardinality <= 30 and <= 60% of rows
   - `continuous` — numeric with high cardinality
4. Runs through the existing QueryService path (`dashboard_query.QueryService`)
   so it is DB-agnostic (MySQL/Postgres alike), respects user KB policy, and
   inherits row/timeout caps.

New tool registration in `db_tools.py` (sibling of `describe_schema`):

```
profile_data { "table": "orders" }   # or omit table for all (capped at 8)
```

Reuses the same KB resolution + policy checks as describe_schema. Response is
compact JSON the LLM can reason over; heavy tables are capped (first 20 columns,
top 3 sample values).

### B. Post-Build Validation

In `dashboard_tools.py` after the app is generated (both create and update):

1. For each metric in the spec, call the existing `analytics._run_metric_sql`
   with `LIMIT 1` wrapper applied to a copy of the SQL (never mutate the stored
   SQL) — infra already exists and is non-raising.
2. Classify:
   - `ok` — returned >= 1 row
   - `empty` — 0 rows returned (no error)
   - `error` — SQL failed (syntax, bad column, timeout)
3. Attach to the tool result:

```
"validation": [
  { "widget_id": "revenue_trend", "status": "ok", "rows": 120 },
  { "widget_id": "orders_region", "status": "empty", "rows": 0 },
  { "widget_id": "margin", "status": "error", "message": "column 'margin_pct' does not exist" }
]
```

4. The agent prompt tells the LLM to report non-`ok` widgets to the user and
   fix them via `update_fullstack_dashboard` (not silently leave them).
5. Also persist `_validation` into config.json per metric (runtime status for
   the Empty card), but generation NEVER fails the build on validation — a
   dashboard with a broken widget is still better than no dashboard. Empty/error
   widgets render with the reason instead of a blank box.

### Template frontend (Empty card with reason)

`widgets/index.jsx` `Empty` component: accept an optional `reason` prop
(`no_data` | `error` | `stale`). Renders:
- `no_data` — "No data for this period" (neutral)
- `error` — "Query failed: <message>" (red, from metric `error` in payload)
- `stale` — "Latest data: <date> (N days ago)" (amber)

Header freshness: when config includes `data_as_of` (derived from profile min/max
of the primary date column), render "As of <date>" in the header. Deferred to
MVE phase 2 — only if cheap.

### Agent prompt changes (`agent_prompts.py`)

Add to the BUILD step (after DATA CONTRACT, before create_fullstack_dashboard):

```
DATA-DRIVEN CHART RULES (HARD RULE — call `profile_data` before building):
After describe_schema and before create_fullstack_dashboard, call
`profile_data(table=...)` for each table you will query. Use the profile to
choose chart types:
- time_series column -> line/area trend chart
- category (2-8 values) -> bar or donut breakdown
- category (>8 values) -> top-N bar + detail table
- continuous numeric -> KPI card (with delta) or histogram
- sparse/empty column -> EXCLUDE from the dashboard; tell the user why
- 0-row table -> do not build on it; ask for another source
```

## Error handling

- Profiler failures (unreachable DB, permission denied) are non-fatal: log +
  return `{"success": False, "error": ...}`; the agent proceeds with
  describe_schema-only (today's behavior). A profiling failure must never block
  a dashboard build.
- Validation dry-runs never raise: `_run_metric_sql` is already non-raising and
  capped (max rows, timeout). Extra safety: wrap the LIMIT-1 copy in its own
  try/except.
- Empty/error widgets never fail the build — they degrade to a reason-bearing
  Empty card.

## Testing

- `tests/unit/test_dashboard_profiler.py` — shape inference on a small seeded
  table set (empty/sparse/time_series/category/continuous), null-pct math,
  column cap, timeout guard.
- `tests/unit/test_dashboard_validation.py` — metric SQL classified ok/empty/
  error; stored SQL never mutated; build not failed on validation.
- e2e: regenerate `sales-performance-dashboard-v2` with profiling enabled; assert
  config.json carries `_validation` and every widget reports ok (or a fix loop
  runs). Verify in browser that Empty cards show reasons.

## Open decisions (resolved)

- Profile every table or only queried ones? → Only tables named in the agent's
  profile_data calls (agent decides; prompt says "for each table you will query").
- Should validation block builds? → No. Non-ok widgets are surfaced + fixable,
  not fatal. (Matches user rule: minimal working version first, iterate.)
- Where does freshness stamp live? → MVE: only if profile exposes a clean primary
  date column; else skip (YAGNI).
