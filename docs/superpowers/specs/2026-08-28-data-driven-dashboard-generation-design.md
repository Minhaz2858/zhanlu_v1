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

- Backend: new `profile_data` capability (service + tool registration). The
  profiler is **fully DB-agnostic** — it never references a specific database,
  table, or column name. All identifiers come from the bound datasource's own
  schema discovery at runtime.
- Re-scoped validation: **free byproduct of profiling, not a separate pass**.
  The profiler already executes each metric's SQL to compute stats, so it
  returns ok/empty/error from that same run. No dedicated per-widget dry-run
  machinery, no `_validation` persistence in config.json (YAGNI — 0/53 widgets
  broken across all current dashboards; add later only if a real failure appears).
- Template frontend: Empty widget shows the reason (no data / query failed).
  Header "As of <date>" freshness stamp only when the profiler exposes a clean
  primary date column.
- Agent prompt: chart-type rules driven by profile output.
- Tests: unit tests for profiler shape inference + validation classification on
  a seeded throwaway schema; e2e verify against the sales dashboard.

Out of scope (deferred): runtime data-adaptive chart swapping in the frontend,
auto-fixing SQL, profile caching across sessions (TTL cache within turn only),
dedicated post-build validation pass.

## DB-Agnostic Requirement (HARD RULE)

The entire feature must work against ANY datasource a user binds — MySQL,
PostgreSQL, or anything the QueryService supports. Concretely:

- **No hardcoded identifiers.** No table names, column names, or schema names
  from this project's ERP/Acme demo data (e.g. `v_sale_order`,
  `PLANDATE`, `FALLAMOUNT`) may appear in profiler code, prompts, or defaults.
- **Identifiers come from the schema, at runtime.** The agent obtains real table
  and column names via `describe_schema` (existing, already DB-agnostic), then
  passes them to `profile_data(table=..., columns=[...])`. The profiler builds
  SQL only from those caller-supplied identifiers.
- **Identifier quoting is dialect-safe.** Table/column names are quoted using
  the QueryService's existing quoting helpers (or ANSI double-quotes where the
  dialect allows) so reserved words and mixed-case names don't break on either
  MySQL or Postgres.
- **No dialect-specific SQL.** The profiler uses only standard SQL:
  `COUNT(*)`, `COUNT(DISTINCT col)`, `COUNT(col)`, `MIN(col)`, `MAX(col)`,
  `LIMIT n`. NO `DATE_FORMAT`, `to_char`, `::date`, backticks, or vendor
  functions. Type/date inspection uses the schema's own type metadata, not
  formatting functions.
- **Type mapping is dialect-tolerant.** Date detection accepts the union of
  common type names (date, datetime, timestamp, timestamp without time zone,
  and string columns that parse as ISO dates in sample values).
- **Policy respected.** The profiler goes through the same user KB policy
  checks as describe_schema (deny/restrict lists apply).
- **Any count of tables/columns.** Works for 1 table or 8, 1 column or 20 —
  caps are limits, not assumptions about a schema shape.

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

### B. Validation as a free byproduct of profiling

The profiler's per-column queries already exercise the tables; validation rides
the same QueryService path with no extra machinery:

1. `_describe_schema`-style KB resolution + policy check.
2. For each table, run the profile queries. If any query fails (syntax, bad
   column, timeout), classify that table's profile as `error` with the message;
   a 0-row table is `empty`; otherwise `ok`.
3. The profile payload per table carries:
   `{ "status": "ok|empty|error", "error_message": null | "..." , "row_count": N, ... }`
4. The agent prompt tells the LLM: if any table you profile comes back
   `empty` or `error`, do NOT build on it — tell the user why and propose the
   closest real alternative (or ask for another data source). This replaces the
   separate post-build dry-run entirely.
5. The frontend Empty card is still improved to show a reason — the widget
   payload already carries `error` from the runtime query, so `Empty` renders
   "Query failed: <message>" when present, "No data for this period" otherwise.
   No config.json `_validation` persistence needed.

Why this is enough: evidence shows 0/53 widgets broken across all current
dashboards (2026-08-28 audit). A dedicated validation pass would catch nothing
today. The profiler's status fields + prompt rule + Empty-reason cover the real
failure paths (agent builds on a bad table) at zero extra cost. If a broken
widget ever appears in production, promote this to a full post-build pass then.

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

- Profiler failures (unreachable DB, permission denied, dialect quirks) are
  non-fatal: log + return `{"success": False, "error": ...}`; the agent proceeds
  with describe_schema-only (today's behavior). A profiling failure must never
  block a dashboard build.
- Per-table query failures classify that table `error` with the message — the
  agent sees it in the profile and avoids building on it.
- All queries go through the QueryService path (bounded rows + timeout), so a
  runaway table can't hang the tool.
- Empty/error tables never fail the tool — they degrade to status fields the
  agent acts on.

## Testing

- `tests/unit/test_dashboard_profiler.py` — shape inference on a seeded
  throwaway schema (empty/sparse/time_series/category/continuous), null-pct
  math, column cap, timeout guard, and the DB-agnostic rule: profiler code must
  contain ZERO hardcoded table/column identifiers (assert no demo table names in
  source). Dialect-tolerant date detection (MySQL date/datetime, Postgres
  timestamp).
- e2e: profile `v_sale_order` through the tool handler against the
  bound Acme datasource; assert the JSON shape (row_count, per-column
  cardinality/null_pct/min/max/top_values/shape/status) and that a broken table
  name returns `error` status. Verify in browser that Empty cards show reasons.
- The validation-byproduct path is covered implicitly: profiling a table the
  agent would build on must yield ok/empty/error status the prompt can act on.

## Open decisions (resolved)

- Profile every table or only queried ones? → Only tables named in the agent's
  profile_data calls (agent decides; prompt says "for each table you will query").
- Should validation block builds? → No. Non-ok tables are surfaced + avoided by
  the agent, not fatal. (Matches user rule: minimal working version first, iterate.)
- Where does freshness stamp live? → MVE: only if profile exposes a clean primary
  date column; else skip (YAGNI).
