# Dashboard Generation

## Purpose

Build a LIVE dashboard that queries a database in real time via the
`create_dashboard` tool. The dashboard auto-polls the bound datasource while
open and persists to the user's My Space for later access.

## Use when

- The user asks for a "dashboard", "live metrics", "KPI view", "metrics board".
- The project has a connected database datasource.

## Hard rules

1. NEVER hand-write HTML for a live dashboard. ALWAYS call `create_dashboard`.
2. If the user has NOT named a datasource, STOP and ask them to pick ONE
   datasource from the project's connected databases before calling the tool.
   One dashboard binds to exactly ONE datasource.
3. Every widget's `sql` MUST be read-only: a single `SELECT` or `WITH`
   statement. No INSERT/UPDATE/DELETE/DDL. Alias numeric/date columns so the
   viewer can map them to axes.
4. Stored SQL is re-validated read-only at query time — never trusted.

## Widget schema

Each widget is `{id, type, title, sql, options}`.

- `type`: `kpi` | `line` | `bar` | `pie` | `table`
- `options`:
  - `kpi`: `{value_column}` — the column holding the single headline number.
    Optional: `unit` (suffix like `%`, `ms`, `USD`), `compare_column`
    (second numeric column → renders a ▲/▼ delta chip), `subtitle`
    (muted context line under the value).
  - `line` / `bar`: `{x_column, y_column}` — x is the category/axis, y is numeric
  - `pie`: `{name_column, value_column}`
  - `table`: `{}` — renders all returned rows/columns
  - ALL types, optional `dimensions`: `[{token, column}]` — enables cross-widget
    filtering. Use `:dim_<token>` in the widget SQL (renders `column = 'value'`
    when the filter is active, `1=1` when not). Example:
    `SELECT region, SUM(amount) FROM orders WHERE :dim_region GROUP BY region`
    with `dimensions: [{token: 'region', column: 'region'}]`.
  - ALL types, optional `drill`: `{value_column, sql, title}` — clicking a
    bar/slice/row runs `sql` with `:drill_value` set to the clicked value, shown
    as an inline detail table. `sql` must be read-only SELECT/WITH.
  - `kpi`, optional `thresholds`: `{direction:'higher_is_worse'|'lower_is_worse',
    warning:<n>, critical:<n>}` — value colors green (good) / amber (warning) /
    red (critical).
  - ANY widget SQL may use `:from` / `:to` / `:date` tokens to bind to the
    viewer's date-range selector (default last-30-days).

## Compose with

### Design research (tool-backed — call BEFORE designing widgets)

- `ui-ux-pro-max` — general design intelligence: palettes, typography,
  styles, chart-type guidance, and UX guidelines across 22 stacks. Query it
  via the `uiux_search` and `uiux_design_system` tools (NOT `load_skill_body`)
  to gather palette/chart/UX recommendations, then translate them into the
  zhanlu token system using the four prompt companions below.

### Implementation spec (load via `load_skill_body`)

Load these prompt companions BEFORE designing widgets — they define the
professional standard the viewer renders to and translate the recommendations
above into zhanlu-specific tokens:

- `data-viz-design` — chart-type selection, visual encoding, token colors,
  number formatting, axis/legend conventions.
- `dashboard-layout-architecture` — information hierarchy (KPI row → trends
  → breakdown → detail table), responsive grid, ordering, density.
- `dashboard-ui-polish` — theme tokens only, typography scale, card anatomy,
  dark-mode theming, empty/error/loading states, accessibility.
- `dashboard-interaction-state` — polling UX, skeletons, stale/error
  handling, KPI delta semantics.

## Workflow

1. **Clarify before building.** Before designing widgets, confirm these five
   items with the user. If ANY is ambiguous or unspecified, ask in ONE message
   with numbered options (A/B/C) and WAIT for the reply — do NOT guess:
   - **Datasource** — which connected database (if >1 available, ask the user
     to pick ONE). One dashboard binds to exactly ONE datasource.
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
2. Optionally call `uiux_design_system(query="analytics dashboard")` and/or
   `uiux_search(domain="chart", query="<request>")` to gather general palette
  and chart-type recommendations. If those tools fail or are unavailable,
  continue with the built-in professional dashboard rules below instead of
  stopping.
3. Load the four implementation companions via `load_skill_body`, then design
   3–6 widgets covering the request — at least one KPI + one trend chart —
   translating the recommendations into zhanlu tokens (`hsl(var(--chart-N))`,
   `bg-card`, `text-muted-foreground`).
4. Do not call `ask_data_agent` for dashboard creation. Use direct datasource
  and schema tools (`list_data_sources`, `describe_schema`). At most two exploratory `execute_query` calls to verify column names or date ranges.
  Do not exhaust the tool loop validating every widget query. If the schema is
  sufficient, proceed with read-only SQL widgets and let the dashboard viewer
  isolate any per-widget query errors.
5. Call `create_dashboard` with `datasource_id`, `title`, `widgets[]`, and
   `refresh_interval_seconds` (default 30; clamped to 10..300).
  You must call `create_dashboard` before the tool loop budget is exhausted.
6. Tell the user the dashboard is live, can be reopened from My Space, and
   will refresh automatically while open.

## Safety rules

- Do not access raw credentials.
- Do not access unrestricted database connections outside the bound datasource.
- All SQL must be read-only and single-statement.
