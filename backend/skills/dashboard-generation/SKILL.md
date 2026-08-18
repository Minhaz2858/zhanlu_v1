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
   - `metrics`: 3–6 widgets, at least one KPI + one trend chart. Widget types:
     `kpi | line | bar | pie | table | area | gauge | radar`.
   - `refresh_interval_seconds`, `theme` (`light` | `dark`).
   The tool responds with `slug` + `app_url`. The app mounts a WebSocket
   channel at `{app_url}ws` and pushes updates when the underlying data changes.
5. **Deliver** — tell the user the dashboard is live at `app_url`, updates
   automatically (~2 s after DB changes), and is listed in My Files as a
   "Dashboard" artifact. Invite iteration ("tell me what to change — layout,
   metrics, theme, or data").
6. **Iterate** — user requests → `update_fullstack_dashboard(slug=..., ...)`.
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
