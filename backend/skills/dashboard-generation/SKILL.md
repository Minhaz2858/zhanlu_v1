# Dashboard Generation (Full-Stack Pipeline)

## Purpose

Build a DEPLOYABLE FULL-STACK REAL-TIME dashboard application: a generated
FastAPI sub-router + pre-built React frontend (Tailwind + Recharts) connected
to a real database with a WebSocket live-data channel. A dashboard request is
an **app-build request** with a design step in front of it — never a static
page, never fabricated data.

## Use when

- The user asks for a "dashboard", "live metrics", "KPI view", "metrics board",
  "仪表盘", "看板", "数据面板", "数据看板", "仪表板".
- The project has a connected database datasource.

## Hard rules

1. **Design FIRST** — ALWAYS call `uiux_design_system(query=..., persist=True)`
   before building. Pass the returned `design_system_ref` to the build tool.
   (Use `uiux_search(domain="chart")` when the chart choice is unclear.)
2. **Data contract** — BEFORE writing any code, confirm with the user: which
   datasource, which metrics/KPIs, and what each KPI means. Inspect the schema
   with `describe_schema`. If the DB is unreachable, ASK the user for
   connection details — NEVER fabricate data.
3. **NEVER hand-write code files.** Submit a DashboardSpec to
   `create_fullstack_dashboard`. The system (Jinja2 template generator) writes
   the app: `api.py`, `queries.py`, `realtime.py`, `config.json` + pre-built
   React dist.
4. Every metric `sql` MUST be read-only: a single `SELECT` or `WITH` statement.
   No INSERT/UPDATE/DELETE/DDL. Stored SQL is re-validated read-only at query
   time — never trusted.
5. After delivery, keep iterating conversationally: to add/change/remove/
   restyle/break-down a metric, call `update_fullstack_dashboard(slug=..., ...)`
   with the slug returned by `create_fullstack_dashboard`. Do NOT answer such
   requests in chat alone.

## Professional visual quality standard (Aug 2026)

Every delivered dashboard must look like a product, not a prototype. The
create/update tools return a `quality` report (grade A/B/C + counts +
recommendations). READ IT. If grade is B/C or any recommendation is a hard
gap (missing trend, <2 KPI cards, <5 widgets, no insight strip, no
design_system_ref), immediately call `update_fullstack_dashboard` to fix it
in the SAME turn — never finish a turn knowing the dashboard is thin.

Layout hierarchy (always):
- **Sections, not a flat grid**: KPI row first (2–4 `kpi` cards with
  `options.delta` vs-prev-period, `options.sparkline: true` when the KPI SQL
  returns a label+value series) → time trend (line/area/combo,
  `options.span:"wide"` for the hero) → comparison/composition
  (bar top-N / pie ≤6 slices / stacked bar) → detail table. Emit
  `layout: [{"title": "...", "widgets": [metric_id, ...]}, ...]` so the
  board reads as a BI story.
- 5–8 widgets, every widget a DIFFERENT analytical angle (no two charts
  showing the same thing). At least: 2 KPI + 1 trend + 1 breakdown + 1 table.

Design tokens:
- Always pass the `design_system_ref` from `uiux_design_system` — the
  generator derives the chart palette + typography + dark overrides from it.
- `theme` must match the design system's mode (dark OLED → `theme:"dark"`).
- For China-region / Chinese-language users set `style:"chinese_bi"` (navy
  glow 大屏; RED = up, GREEN = down — the template flips badge colors).

Number & text polish:
- Format every KPI value with units (¥ / t / % / kg) and thousands
  separators in the SQL or label (`ROUND(SUM(x),2)`, `CONCAT('¥', FORMAT(...))`).
- Table headers uppercase, short; never truncate entity names client-side
  (full names always, wrap naturally).
- KPI labels are short noun phrases ("Total Order Amount"), not sentences.

Chart anti-patterns (never):
- Pie/donut with >6 slices (use horizontal bar instead).
- >8 category bars vertical (flip horizontal).
- 3D charts, rainbow palettes, chart junk, emoji icons (use SVG).
- Red = bad / green = good is the default semantic; keep it consistent.

Domain design skills (load when the project industry matches):
- petrochemical / chemical / oil & gas → apply `petrochemical-dashboard-design`
  patterns: supply-chain hierarchy views (upstream→midstream→downstream),
  gradient-header table cards, tables-as-charts, AI narrative under charts,
  price transmission chains. Never a generic flat grid for these projects.
- Commercial/industrial in general → `edia-dashboard-design` patterns
  (KPI sparkline cards, drill-down, dark sidebar for market indicators).

## Workflow

1. **Design system** — call `uiux_design_system(query="<dashboard topic>",
   persist=True, project="<project name>")`. It returns `design_system_ref`
   (e.g. `design-system/{org}/MASTER.md`). If the CLI is unavailable the tool
   still returns a built-in design system and persists it.
2. **Clarify the data contract** — confirm with the user, in ONE message:
   - **Datasource** — which connected database (if >1, ask the user to pick ONE).
     One dashboard app binds to exactly ONE datasource.
   - **Key metrics & columns** — what to measure (e.g. revenue + order count,
     revenue by region, top products).
   - **Chart intent** — how to visualize: KPI row + line trend + bar breakdown
     + detail table is the recommended layout.
   - **Refresh cadence** — `refresh_interval_seconds` (default 30).
   Only when the contract is resolved (explicitly stated or unambiguous) proceed.
3. **Inspect the schema** — use `list_data_sources` + `describe_schema` to
   confirm column names before writing SQL. At most two exploratory
   `execute_query` calls. Do NOT exhaust the tool loop validating every query.
   Common pitfall: `erp_product_sales_details` uses `FNAME` (NOT
   `material_name`), `material_id` / `FMATERIALID`, `shipment_date`,
   `shipment_quantity`, `contract_price`, `partner_name`, `shipment_grade`.
4. **Build** — call `create_fullstack_dashboard` with a DashboardSpec:
   - `name`, `slug` (lowercase `[a-z0-9_-]`, URL-safe, unique),
   - `datasource_id` (the confirmed KB),
   - `design_system_ref` from step 1,
   - `metrics`: 3–8 widgets, at least one KPI + one trend chart. Widget types:
     `kpi | line | bar | pie | table | area | gauge | radar | combo`.
     `combo` = bars on the left axis + a line on the right axis sharing one
     x-axis (the classic BI chart: volume bars + price/revenue line). Declare
     series via options: `{"bars": ["qty"], "lines": ["amount"]}` (defaults:
     first numeric key = bar, second = line).
   - `refresh_interval_seconds`, `theme` (`light` | `dark`).
   The tool responds with `slug` + `app_url`. The app mounts a WebSocket
   channel at `{app_url}ws` and pushes updates when the underlying data changes.

5. **BI layout (the professional multi-angle pattern)** — for BI/analytics
   dashboards, cover multiple analytical angles, not just one chart:
   - KPI row (2–4 cards) with `options.delta` (vs prev period) — and
     `options.sparkline: true` when the KPI SQL returns a label+value series.
   - Trend angle: line/area over time (`options.span: "wide"` for the hero).
   - Comparison angle: bar (top-N) or `combo` (bars + line on dual axes).
   - Composition angle: pie/donut for a breakdown, or bar with
     `options.stacked: true` for composition-over-time.
   - Distribution/detail angle: table (sortable/search/CSV built-in).
   - 3–8 widgets total; every widget covers a different analytical angle.

6. **Cross-widget filters (BI slicing)** — declare dimensions per metric via
   `options.filters: [{"key": "product", "column": "FNAME", "label": "Product"}]`.
   The metric SQL then uses the BARE safe token `:dim_product` in the WHERE
   clause, e.g. `... WHERE :dim_product` (the backend expands it to
   `FNAME = 'value'`, or `1=1` when the filter is unset). NEVER write
   `col = :dim_product` — an unset filter expands to `1=1` and breaks the SQL.
   The template renders a filter bar (dropdowns for every declared dim + a
   date range) and every widget refetches through it. Use `:from` / `:to` /
   `:date` tokens in time-window SQL so the date range actually filters
   (default when absent: last 30 days). Declare a dim ONLY on metrics whose
   SQL uses the matching token.

7. **AI insight strip (Python-computed, LLM-narrated)** — add a top-level
   `insights` array to the spec: 1–3 `{"title": "...", "body": "..."}` pairs
   that summarize what the numbers mean (e.g. "Revenue up 12% MoM driven by
   Product A"). Base every number on the actual query results you inspected —
   NEVER fabricate insights or invent figures.

8. **Deliver** — tell the user the dashboard is live at `app_url`, updates
   automatically (~2 s after DB changes), and is listed in My Files as a
   "Dashboard" artifact. Invite iteration ("tell me what to change — layout,
   metrics, theme, filters, or data").
9. **Iterate** — user requests → `update_fullstack_dashboard(slug=..., ...)`.
   The app regenerates and hot-reloads.

## Anti-fake-data guarantee

- Every metric is a real read-only query against the confirmed datasource.
- If the DB is unreachable at build time, return an explicit error explaining
  the missing connection and ask for connection details — do NOT ship
  placeholder numbers.

## Safety rules

- Do not access raw credentials.
- Do not access unrestricted database connections outside the bound datasource.
- All SQL must be read-only and single-statement.
