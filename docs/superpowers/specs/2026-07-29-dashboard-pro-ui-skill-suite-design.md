# Dashboard Professional UI + Skill Suite Design

Date: 2026-07-29
Status: Approved (plan `dashboard-pro-ui-skill-suite`)

## Problem

The live-dashboard UI (chat card, popup, viewer widgets) looks basic: hardcoded
`#6366f1` chart colors that ignore the theme tokens, bare unformatted KPI numbers,
unstyled tables, no per-widget loading/empty states, and no dark-mode chart theming
(`--chart-1..5` exist only in `:root`, not `.dark`). Separately, the dashboard agent
has only one skill (`dashboard-generation`) and no guidance on visual design,
layout, polish, or interaction — so generated dashboards are functional but not
professional.

## Goals

1. **Skill suite**: four composable prompt-type skill packages the dashboard agent
   loads alongside `dashboard-generation`: `data-viz-design`,
   `dashboard-layout-architecture`, `dashboard-ui-polish`,
   `dashboard-interaction-state`.
2. **UI revamp**: apply those competencies to `DashboardViewer`, `DashboardPopup`,
   `DashboardCard` to reach a modern-SaaS (Linear/Stripe-like) standard, expressed
   through the existing warm shadcn token system.

## Non-goals

- No new npm dependencies (recharts + existing `ui/chart.jsx` suffice).
- No changes to polling cadence, component props, widget schema, or API contracts.
- No redesign of the app-wide theme; `--chart-*` tokens only.
- No backend logic changes beyond documenting optional KPI option fields.

## Part A — Skill suite

### Locations and formats (two-tier, matching existing architecture)

- **Template library** (product format, same as `dashboard-generation`):
  `docs/starter_templates/skill_library/system/visualization/<skill>/`
  with `manifest.yaml` (`skill_key`, `status: active`, `runtime: {type: prompt}`,
  `permissions`, `validation`) + `SKILL.md`.
- **Runtime bundled** (loader format, validated by
  `skills_loader/manifest.schema.json`, discovered by `ManifestIndex` scanning
  `backend/skills/` and by `SkillsRegistry` rglob): `backend/skills/<skill>/`
  with `manifest.yaml` (`name`, `version`, `description`, `summary`, `category:
  visualization`, `tags`, `artifact_types: [dashboard]`, `requires_sandbox: false`,
  `runtime: "prompt"`, `source: bundled`) + the same `SKILL.md` body.
  `dashboard-generation` itself is also mirrored into `backend/skills/` so the
  "compose with" references resolve at runtime (today it exists only in
  starter_templates, unreachable by `ManifestIndex`).

### The four skills

1. **`data-viz-design`** — chart-type selection heuristics (trend→line,
   comparison→bar, composition→pie/donut ≤6 slices, single number→kpi,
   records→table); visual encoding rules (one measure per chart, sorted bars,
   time ascending); color via theme tokens in categorical order
   `chart-1..5`, never hardcoded hex; number/date formatting rules
   (compact notation for large values, tabular numerals); axis/legend/grid
   conventions (subtle grid, muted ticks, legend when >1 series or pie).
2. **`dashboard-layout-architecture`** — information hierarchy (KPI row → trend
   charts → breakdown → detail table); 3–6 widget composition; responsive grid
   behavior (KPIs up to 4-across, charts 2-up on desktop, 1-up mobile); density
   and whitespace rules.
3. **`dashboard-ui-polish`** — token usage only (no hex); typography scale
   (widget title 14/500, KPI value 30/700 tabular-nums, meta 12 muted); spacing
   and card anatomy; dark-mode chart theming; empty/error/loading copy; a11y
   (contrast, aria-labels, non-color encodings).
4. **`dashboard-interaction-state`** — refresh/polling UX (last-updated,
   spin-while-refreshing, visibility pause); skeleton behavior; stale/error
   handling and retry; popup vs inline expectations; optional KPI semantics
   (`unit`, `compare_column`, `subtitle`).

### `dashboard-generation` update

Add a "Compose with" section to its `SKILL.md` (both copies) referencing the four
skill keys, and extend the widget schema docs with optional KPI options
(`unit`, `compare_column`, `subtitle`).

## Part B — Frontend revamp

### Tokens (`frontend/src/index.css`)

Add `--chart-1..5` to `.dark` (brightened counterparts of the `:root` values so
series stay legible on near-black cards).

### New modules

- `components/dashboard/format.js` — module-scope `Intl.NumberFormat` instances;
  `formatMetric(value, {unit, compact})`, `formatAxisTick(value)`.
- `components/dashboard/widgets/WidgetShell.jsx` — card frame: title bar, hover
  elevation, skeleton loader, styled error/empty states.
- `components/dashboard/widgets/KpiWidget.jsx` — formatted value (tabular-nums,
  compact), optional unit suffix, optional delta chip (up/down arrow,
  success/destructive tint) from `compare_column`, optional subtitle.
- `components/dashboard/widgets/TrendChartWidget.jsx` — line/bar via
  `ChartContainer` from `ui/chart.jsx`: subtle `CartesianGrid`,
  `ChartTooltipContent`, `ChartLegendContent`, line `strokeWidth={2}`, rounded
  bar `radius`, colors from `hsl(var(--chart-N))`.
- `components/dashboard/widgets/PieWidget.jsx` — donut (`innerRadius`) with
  chart-1..5 cells, legend, styled tooltip.
- `components/dashboard/widgets/TableWidget.jsx` — sticky header (12px muted
  uppercase), right-aligned numeric cells (tabular-nums), row hover, empty
  state, 50-row cap kept.

### Modified components

- `DashboardViewer.jsx` — slim orchestrator: refined header (name, meta line
  with widget count + "Updated HH:MM:SS", ghost refresh button with spinning
  icon), responsive grid (KPIs auto-fit up to 4, charts 2-up md+), per-widget
  routing to widget components with skeletons. Polling effect preserved
  verbatim (10–300s clamp, visibilitychange pause, mountedRef guard).
- `DashboardPopup.jsx` — refined header (dashboard name, datasource, pulsing
  "Live" badge, close), chat sheet widened `max-w-xl` → `max-w-2xl`.
- `DashboardCard.jsx` — minor polish (typography/hover) to match new standard.

### Backend (`dashboard_tools.py`)

`options` stays a free-form dict (already permits optional fields); update the
tool schema description to document per-type options including the optional KPI
fields. No validation change, no migration.

## Testing

- **New pytest** `backend/tests/test_dashboard_design_skills_load.py` (style of
  `test_dashboard_skill_loads.py`): validate the 4 starter_templates manifests
  (`skill_key`, `status: active`, runtime type `prompt`), validate the 4 runtime
  manifests against `manifest.schema.json`, assert `dashboard-generation`
  SKILL.md references all 4 skill keys.
- **Update 4 vitest files**: `DashboardViewer.test.jsx` (widgets, skeleton,
  error, empty), `DashboardPopup.test.jsx` (header/live badge),
  `DashboardCard.test.jsx`, `MySpace.dashboards.test.jsx` (keep green).
- RAM-limited box: run only these explicit files, no broad collection, no `-n`.

## Verification

Playwright screenshots of the revamped dashboard (chat popup + My Space) in
light and dark themes; code-reviewer subagent pass on token fidelity, contract
preservation, accessibility.

## Risks

- Saved dashboards render unchanged: widget schema untouched, viewer props
  unchanged — verified by existing tests.
- Dark-mode chart legibility depends on the new `.dark --chart-*` values;
  covered by visual verification.
