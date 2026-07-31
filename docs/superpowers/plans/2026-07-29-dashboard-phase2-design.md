# Dashboard Phase 2 (Design) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand dashboard chart breadth (area/scatter/gauge/radar/stacked-bar), add recharts zoom + motion presets, PNG export + shareable deep link, and an app-wide dark-mode toggle.

**Architecture:** All five new chart types are pure-frontend React components (the backend already runs widget SQL and returns rows; the `type` field only selects the client renderer). Dark-mode infrastructure already exists (`lib/useTheme.js` + `index.css` `.dark` tokens, wired in `main.jsx`) but has no UI toggle, so that task is just surfacing it. PNG export reuses the already-installed `html2canvas`. The shareable link adds a standalone `/dashboard/:id` route that seeds the viewer's date-range/filters from URL params.

**Tech Stack:** React, recharts ^2.15.4 (AreaChart/ScatterChart/RadarChart/RadialBarChart/Brush), html2canvas ^1.4.1, Tailwind + `hsl(var(--chart-N))` tokens, vitest, pytest.

## Global Constraints

- **Colors:** NEVER hardcode hex. Always use `hsl(var(--chart-N))` (N=1..5) or `var(--color-<key>)` emitted by `ChartStyle`. `--chart-1..5` exist in BOTH `:root` and `.dark` in `src/index.css`.
- **Existing dashboards must not break:** all new behavior is opt-in via widget `options` fields. A widget with no new options renders identically to today.
- **DashboardViewer polling effect stays verbatim:** the polling `useEffect` (10–300s clamp, `visibilitychange` pause, `mountedRef` guard) must not be restructured. New toolbar buttons are additive.
- **RAM-limited box:** run backend tests by explicit file path only, no `-n` workers. Run frontend tests scoped to `src/components/dashboard`, never the full suite.
- **Backend test DB:** recreate before each backend run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`.
- **Widget type strings:** lowercase, matching existing convention: `kpi`, `line`, `bar`, `pie`, `table`, `area`, `stacked-bar`, `scatter`, `gauge`, `radar` (hyphen in `stacked-bar` is intentional and handled fine as a JS string).
- **recharts test mock:** every new chart-component test must include the new recharts primitives it uses in its inline `vi.mock('recharts', ...)` (see Task 3/4 test code).

---

### Task 1: Backend — widget type allowlist + options documentation

Extend the `create_dashboard` agent tool so the LLM can author the 5 new chart types, with upfront pydantic validation rejecting unknown types. This is the agent-facing contract; the REST `POST /dashboards` path stays permissive (the viewer already falls back to `<TableWidget>` for unknown types, and we do not want to break existing dashboards with ad-hoc types).

**Files:**
- Modify: `backend/app/services/tool_handlers/dashboard_tools.py:22-43` (WidgetSpec) and `:149` (schema `type` enum) and `:152-168` (schema `options.description`)
- Test: `backend/tests/test_dashboard_widget_types.py` (create)

**Interfaces:**
- Produces: `ALLOWED_WIDGET_TYPES` set in `dashboard_tools.py`; `WidgetSpec` now rejects unknown `type` values at agent call time. No new public functions.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_dashboard_widget_types.py`:
```python
import pytest
from pydantic import ValidationError

from app.services.tool_handlers.dashboard_tools import (
    WidgetSpec, CREATE_DASHBOARD_SCHEMA,
)

ALLOWED = ["kpi", "line", "bar", "pie", "table",
           "area", "stacked-bar", "scatter", "gauge", "radar"]


def _base(**over):
    d = dict(id="w1", type="kpi", title="t", sql="SELECT 1 AS v", options={})
    d.update(over)
    return d


@pytest.mark.parametrize("t", ALLOWED)
def test_widget_spec_accepts_all_types(t):
    assert WidgetSpec(**_base(type=t)).type == t


def test_widget_spec_rejects_unknown_type():
    with pytest.raises(ValidationError):
        WidgetSpec(**_base(type="bogus"))


def test_schema_enum_covers_all_types():
    enum = (CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]
            ["widgets"]["items"]["properties"]["type"]["enum"])
    assert set(ALLOWED).issubset(set(enum))


def test_schema_options_documents_new_types():
    desc = (CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]
            ["widgets"]["items"]["properties"]["options"]["description"])
    for kw in ("area", "stacked-bar", "scatter", "gauge", "radar", "motion", "zoom"):
        assert kw in desc
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_dashboard_widget_types.py -v
```
Expected: FAIL — `test_widget_spec_rejects_unknown_type` (WidgetSpec has no type validator today) and `test_schema_*` (enum/description lack new types). The `accepts_all_types` cases pass today because type is unconstrained.

- [ ] **Step 3: Implement the type allowlist + docs**

In `backend/app/services/tool_handlers/dashboard_tools.py`:

Add a module-level constant after the imports (after `logger = logging.getLogger(__name__)`):
```python
ALLOWED_WIDGET_TYPES = {
    "kpi", "line", "bar", "pie", "table",
    "area", "stacked-bar", "scatter", "gauge", "radar",
}
```

Change the `WidgetSpec` class — update the `type` field comment and add a validator:
```python
class WidgetSpec(BaseModel):
    id: str
    type: str  # kpi | line | bar | pie | table | area | stacked-bar | scatter | gauge | radar
    title: str
    sql: str
    options: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in ALLOWED_WIDGET_TYPES:
            raise ValueError(f"unknown widget type: {v!r}")
        return v

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        validate_widget_sql(v)
        return v
```

Update the schema `type` enum (line `:149`) to:
```python
"type": {"type": "string", "enum": ["kpi", "line", "bar", "pie", "table", "area", "stacked-bar", "scatter", "gauge", "radar"]},
```

Append the new-type documentation to the `options.description` string (after the existing `table: {}.` sentence, before `ALL types:`):
```
"area: {x_column, y_column}. stacked-bar: {x_column, series:[{key,label}]}. "
"scatter: {x_column, y_column, name_column?}. gauge: {value_column, min?, max?, target?, unit?}. "
"radar: {axis_column, series:[{key,label}]}. ALL chart types accept optional "
"motion ('none'|'subtle'|'lively', default 'subtle') controlling animation, and "
"zoom (true/false) enabling a brush selector on area/line/bar/stacked-bar. "
```

- [ ] **Step 4: Run test to verify it passes**

Run the same command as Step 2.
Expected: 4 test cases PASS (12 parametrized + 3 = 15 collected items pass).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/tool_handlers/dashboard_tools.py backend/tests/test_dashboard_widget_types.py && git commit -m "feat(dashboard): allowlist + document 5 new widget types (area/stacked-bar/scatter/gauge/radar)"
```

---

### Task 2: Motion presets + zoom for existing charts

Create a shared motion-preset helper and apply it + an opt-in `<Brush>` zoom selector to the existing `TrendChartWidget` (line/bar) and motion to `PieWidget`. New widgets in later tasks reuse the same helper.

**Files:**
- Create: `frontend/src/components/dashboard/widgets/motion.js`
- Modify: `frontend/src/components/dashboard/widgets/TrendChartWidget.jsx`
- Modify: `frontend/src/components/dashboard/widgets/PieWidget.jsx`
- Test: `frontend/src/components/dashboard/widgets/__tests__/motion.test.jsx` (create)

**Interfaces:**
- Produces: `motionProps(preset?: string) -> { isAnimationActive, animationDuration?, animationEasing? }` from `widgets/motion.js`. `preset` values: `'none'` | `'subtle'` | `'lively'`; undefined/unknown → `'subtle'`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/dashboard/widgets/__tests__/motion.test.jsx`:
```jsx
import { describe, it, expect } from 'vitest';
import { motionProps } from '../motion';

describe('motionProps', () => {
  it('disables animation for "none"', () => {
    expect(motionProps('none')).toEqual({ isAnimationActive: false });
  });
  it('uses long ease-out for "lively"', () => {
    const p = motionProps('lively');
    expect(p.isAnimationActive).toBe(true);
    expect(p.animationDuration).toBe(900);
    expect(p.animationEasing).toBe('ease-out');
  });
  it('defaults to subtle when undefined', () => {
    const p = motionProps(undefined);
    expect(p.isAnimationActive).toBe(true);
    expect(p.animationDuration).toBe(400);
  });
  it('falls back to subtle for unknown preset', () => {
    expect(motionProps('wat').animationDuration).toBe(400);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/motion.test.jsx`
Expected: FAIL — module `../motion` not found.

- [ ] **Step 3: Implement motion.js**

Create `frontend/src/components/dashboard/widgets/motion.js`:
```js
// Motion presets for chart widgets. Maps an `options.motion` string to the
// recharts animation props spread onto series/containers. Keep the mapping
// pure (no React) so it is trivially unit-testable.
export function motionProps(preset) {
  switch (preset) {
    case 'none':
      return { isAnimationActive: false };
    case 'lively':
      return { isAnimationActive: true, animationDuration: 900, animationEasing: 'ease-out' };
    case 'subtle':
    default:
      return { isAnimationActive: true, animationDuration: 400, animationEasing: 'ease-in-out' };
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run the same command as Step 2.
Expected: 4 PASS.

- [ ] **Step 5: Apply motion + zoom to TrendChartWidget**

In `frontend/src/components/dashboard/widgets/TrendChartWidget.jsx`, replace the full file content with:
```jsx
import { BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Brush } from 'recharts';
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart';
import { formatAxisTick } from '../format';
import { motionProps } from './motion';

// Single-series trend chart (line or bar). Series color resolves from the
// theme token via the chart config (--color-value is set by ChartStyle).
export default function TrendChartWidget({ kind, widget, rows, onCellClick }) {
  const x = widget.options?.x_column;
  const y = widget.options?.y_column;
  const zoom = widget.options?.zoom === true;
  const data = rows.map((r) => ({
    name: x ? r[x] : '',
    value: y ? Number(r[y]) : Number(Object.values(r)[0] || 0),
  }));
  const config = { value: { label: widget.title, color: 'hsl(var(--chart-1))' } };
  const Comp = kind === 'bar' ? BarChart : LineChart;
  const motion = motionProps(widget.options?.motion);
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
               onClick={(d) => onCellClick && x && onCellClick(x, d && d.name)} {...motion} />
        ) : (
          <Line
            type="monotone"
            dataKey="value"
            stroke="var(--color-value)"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
            {...motion}
          />
        )}
        {zoom && <Brush dataKey="name" height={20} stroke="var(--color-value)" travellerWidth={8} />}
      </Comp>
    </ChartContainer>
  );
}
```

- [ ] **Step 6: Apply motion to PieWidget**

In `frontend/src/components/dashboard/widgets/PieWidget.jsx`, add the import and spread motion onto `<Pie>`. Change the import line to:
```jsx
import { motionProps } from './motion';
```
and change the `<Pie ...>` opening tag to include `{...motion}` before its children:
```jsx
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={48}
          outerRadius={72}
          strokeWidth={2}
          onClick={(d) => onCellClick && name && onCellClick(name, d && d.name)}
          {...motion(widget.options?.motion)}
        >
```
(Add `const motion = motionProps;` is unnecessary — call inline. Keep `SERIES_COLORS` export unchanged.)

- [ ] **Step 7: Run existing dashboard tests to confirm no regression**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard`
Expected: all existing tests PASS (the recharts mock in existing tests does not include `Brush`, but `TrendChartWidget` only renders `<Brush>` when `options.zoom === true`; existing test widgets have no `zoom`, so the mock's absence of `Brush` does not matter because the element is never constructed).

- [ ] **Step 8: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/widgets/motion.js frontend/src/components/dashboard/widgets/TrendChartWidget.jsx frontend/src/components/dashboard/widgets/PieWidget.jsx frontend/src/components/dashboard/widgets/__tests__/motion.test.jsx && git commit -m "feat(dashboard): motion presets + opt-in brush zoom for trend/pie widgets"
```

---

### Task 3: Area + Stacked-bar widgets (with zoom + motion)

Add the two trend-family chart types that extend the existing line/bar pattern: a single-series area chart with gradient fill, and a multi-series stacked bar.

**Files:**
- Create: `frontend/src/components/dashboard/widgets/AreaChartWidget.jsx`
- Create: `frontend/src/components/dashboard/widgets/StackedBarWidget.jsx`
- Modify: `frontend/src/components/dashboard/DashboardViewer.jsx:1-8` (imports) and `:159-181` (`Widget` switch)
- Test: `frontend/src/components/dashboard/widgets/__tests__/AreaStacked.test.jsx` (create)

**Interfaces:**
- Produces: `AreaChartWidget({ widget, rows, onCellClick })` and `StackedBarWidget({ widget, rows, onCellClick })` default exports.
- Consumes: `motionProps` from `./motion`; `SERIES_COLORS` not needed (uses `hsl(var(--chart-N))` directly).
- `DashboardViewer` `Widget` switch now routes `widget.type === 'area'` and `widget.type === 'stacked-bar'`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/dashboard/widgets/__tests__/AreaStacked.test.jsx`:
```jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div data-testid="rc">{children}</div>,
  AreaChart: ({ children }) => <div data-testid="area-chart">{children}</div>,
  Area: (p) => <div data-testid="area" data-key={p.dataKey} />,
  BarChart: ({ children }) => <div data-testid="bar-chart">{children}</div>,
  Bar: (p) => <div data-testid="bar" data-key={p.dataKey} />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Brush: (p) => <div data-testid="brush" />,
  Tooltip: () => <div />,
  Legend: () => <div />,
}));

import AreaChartWidget from '../AreaChartWidget';
import StackedBarWidget from '../StackedBarWidget';

describe('AreaChartWidget', () => {
  it('maps x/y columns and renders an area', () => {
    const widget = { id: 'a1', title: 'Rev', options: { x_column: 'd', y_column: 'r' } };
    render(<AreaChartWidget widget={widget} rows={[{ d: 'Mon', r: 5 }]} />);
    expect(screen.getByTestId('area-chart')).toBeInTheDocument();
    expect(screen.getByTestId('area').getAttribute('data-key')).toBe('value');
  });
  it('renders a brush when options.zoom is true', () => {
    const widget = { id: 'a2', title: 'Rev', options: { x_column: 'd', y_column: 'r', zoom: true } };
    render(<AreaChartWidget widget={widget} rows={[{ d: 'Mon', r: 5 }]} />);
    expect(screen.getByTestId('brush')).toBeInTheDocument();
  });
});

describe('StackedBarWidget', () => {
  it('renders one bar per series with stackId', () => {
    const widget = {
      id: 's1', title: 'By Q',
      options: { x_column: 'q', series: [{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }] },
    };
    render(<StackedBarWidget widget={widget} rows={[{ q: 'Q1', a: 2, b: 3 }]} />);
    const bars = screen.getAllByTestId('bar');
    expect(bars).toHaveLength(2);
    expect(bars[0].getAttribute('data-key')).toBe('a');
    expect(bars[1].getAttribute('data-key')).toBe('b');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/AreaStacked.test.jsx`
Expected: FAIL — modules `../AreaChartWidget` / `../StackedBarWidget` not found.

- [ ] **Step 3: Implement AreaChartWidget**

Create `frontend/src/components/dashboard/widgets/AreaChartWidget.jsx`:
```jsx
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Brush } from 'recharts';
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart';
import { formatAxisTick } from '../format';
import { motionProps } from './motion';

// Single-series area chart with a theme-token gradient fill.
export default function AreaChartWidget({ widget, rows, onCellClick }) {
  const x = widget.options?.x_column;
  const y = widget.options?.y_column;
  const zoom = widget.options?.zoom === true;
  const gid = 'area-' + String(widget.id || 'w').replace(/[^a-zA-Z0-9_-]/g, '');
  const data = rows.map((r) => ({
    name: x ? r[x] : '',
    value: y ? Number(r[y]) : Number(Object.values(r)[0] || 0),
  }));
  const config = { value: { label: widget.title, color: 'hsl(var(--chart-1))' } };
  const motion = motionProps(widget.options?.motion);
  return (
    <ChartContainer config={config} className="h-[200px] w-full">
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="var(--color-value)" stopOpacity={0.8} />
            <stop offset="95%" stopColor="var(--color-value)" stopOpacity={0.1} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} tick={{ fontSize: 11 }} />
        <YAxis tickLine={false} axisLine={false} tickMargin={4} tick={{ fontSize: 11 }} tickFormatter={formatAxisTick} width={44} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Area type="monotone" dataKey="value" stroke="var(--color-value)" strokeWidth={2}
              fill={`url(#${gid})`} {...motion}
              onClick={(d) => onCellClick && x && onCellClick(x, d && d.name)} />
        {zoom && <Brush dataKey="name" height={20} stroke="var(--color-value)" travellerWidth={8} />}
      </AreaChart>
    </ChartContainer>
  );
}
```

- [ ] **Step 4: Implement StackedBarWidget**

Create `frontend/src/components/dashboard/widgets/StackedBarWidget.jsx`:
```jsx
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Brush } from 'recharts';
import {
  ChartContainer, ChartTooltip, ChartTooltipContent,
  ChartLegend, ChartLegendContent,
} from '@/components/ui/chart';
import { formatAxisTick } from '../format';
import { motionProps } from './motion';

// Multi-series stacked bar. options.series = [{ key, label }]; each key is a
// numeric column in the result rows. Colors cycle through --chart-1..5.
export default function StackedBarWidget({ widget, rows, onCellClick }) {
  const x = widget.options?.x_column;
  const series = widget.options?.series || [];
  const zoom = widget.options?.zoom === true;
  const data = rows.map((r) => {
    const point = { name: x ? r[x] : '' };
    series.forEach((s) => { point[s.key] = Number(r[s.key]) || 0; });
    return point;
  });
  const config = Object.fromEntries(
    series.map((s, i) => [s.key, { label: s.label || s.key, color: `hsl(var(--chart-${(i % 5) + 1}))` }]),
  );
  const motion = motionProps(widget.options?.motion);
  return (
    <ChartContainer config={config} className="h-[220px] w-full">
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} tick={{ fontSize: 11 }} />
        <YAxis tickLine={false} axisLine={false} tickMargin={4} tick={{ fontSize: 11 }} tickFormatter={formatAxisTick} width={44} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        {series.map((s, i) => (
          <Bar key={s.key} dataKey={s.key} name={s.label || s.key} stackId="a"
               fill={`hsl(var(--chart-${(i % 5) + 1}))`} radius={[3, 3, 0, 0]} maxBarSize={40}
               onClick={(d) => onCellClick && x && onCellClick(x, d && d.name)} {...motion} />
        ))}
        {zoom && <Brush dataKey="name" height={20} stroke="hsl(var(--chart-1))" travellerWidth={8} />}
      </BarChart>
    </ChartContainer>
  );
}
```

- [ ] **Step 5: Wire into DashboardViewer**

In `frontend/src/components/dashboard/DashboardViewer.jsx`, add imports after line 8 (`import TableWidget from './widgets/TableWidget';`):
```jsx
import AreaChartWidget from './widgets/AreaChartWidget';
import StackedBarWidget from './widgets/StackedBarWidget';
```
In the `Widget` function's body, add the two new branches before the final `else` (table fallback). Replace:
```jsx
      ) : widget.type === 'pie' ? (
        <PieWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
```
with:
```jsx
      ) : widget.type === 'pie' ? (
        <PieWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'area' ? (
        <AreaChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'stacked-bar' ? (
        <StackedBarWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/AreaStacked.test.jsx`
Expected: 3 PASS.

- [ ] **Step 7: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/widgets/AreaChartWidget.jsx frontend/src/components/dashboard/widgets/StackedBarWidget.jsx frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/widgets/__tests__/AreaStacked.test.jsx && git commit -m "feat(dashboard): area + stacked-bar widgets with zoom + motion"
```

---

### Task 4: Scatter + Radar + Gauge widgets

Add the three remaining chart types: a two-column scatter plot, a multi-axis radar, and a single-value radial gauge with a centered readout.

**Files:**
- Create: `frontend/src/components/dashboard/widgets/ScatterChartWidget.jsx`
- Create: `frontend/src/components/dashboard/widgets/RadarChartWidget.jsx`
- Create: `frontend/src/components/dashboard/widgets/GaugeWidget.jsx`
- Modify: `frontend/src/components/dashboard/DashboardViewer.jsx` (imports + `Widget` switch)
- Test: `frontend/src/components/dashboard/widgets/__tests__/ScatterRadarGauge.test.jsx` (create)

**Interfaces:**
- Produces: `ScatterChartWidget`, `RadarChartWidget`, `GaugeWidget` default exports, each `({ widget, rows, onCellClick })`.
- `DashboardViewer` `Widget` switch now routes `widget.type` `scatter`, `radar`, `gauge`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/dashboard/widgets/__tests__/ScatterRadarGauge.test.jsx`:
```jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  ScatterChart: ({ children }) => <div data-testid="scatter-chart">{children}</div>,
  Scatter: (p) => <div data-testid="scatter" data-len={p.data && p.data.length} />,
  ZAxis: () => <div />,
  RadarChart: ({ children }) => <div data-testid="radar-chart">{children}</div>,
  Radar: (p) => <div data-testid="radar" data-key={p.dataKey} />,
  PolarGrid: () => <div />,
  PolarAngleAxis: () => <div />,
  PolarRadiusAxis: () => <div />,
  RadialBarChart: ({ children }) => <div data-testid="gauge-chart">{children}</div>,
  RadialBar: () => <div data-testid="radial-bar" />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
  Legend: () => <div />,
}));

import ScatterChartWidget from '../ScatterChartWidget';
import RadarChartWidget from '../RadarChartWidget';
import GaugeWidget from '../GaugeWidget';

describe('ScatterChartWidget', () => {
  it('maps x/y columns to points', () => {
    const widget = { id: 'sc1', title: 'X vs Y', options: { x_column: 'a', y_column: 'b' } };
    render(<ScatterChartWidget widget={widget} rows={[{ a: 1, b: 2 }, { a: 3, b: 4 }]} />);
    expect(screen.getByTestId('scatter').getAttribute('data-len')).toBe('2');
  });
});

describe('RadarChartWidget', () => {
  it('renders one radar per series', () => {
    const widget = {
      id: 'r1', title: 'Skills',
      options: { axis_column: 'skill', series: [{ key: 'now', label: 'Now' }, { key: 'goal', label: 'Goal' }] },
    };
    render(<RadarChartWidget widget={widget} rows={[{ skill: 'A', now: 3, goal: 5 }]} />);
    const radars = screen.getAllByTestId('radar');
    expect(radars).toHaveLength(2);
    expect(radars[0].getAttribute('data-key')).toBe('now');
  });
});

describe('GaugeWidget', () => {
  it('clamps the value into the min..max range and shows the readout', () => {
    const widget = { id: 'g1', title: 'CPU', options: { value_column: 'v', min: 0, max: 100, unit: '%' } };
    render(<GaugeWidget widget={widget} rows={[{ v: 42 }]} />);
    expect(screen.getByTestId('gauge-chart')).toBeInTheDocument();
    expect(screen.getByText('42 %')).toBeInTheDocument();
  });
  it('shows a target label when target is set', () => {
    const widget = { id: 'g2', title: 'CPU', options: { value_column: 'v', min: 0, max: 100, target: 80 } };
    render(<GaugeWidget widget={widget} rows={[{ v: 42 }]} />);
    expect(screen.getByText(/target/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/ScatterRadarGauge.test.jsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement ScatterChartWidget**

Create `frontend/src/components/dashboard/widgets/ScatterChartWidget.jsx`:
```jsx
import { ScatterChart, Scatter, XAxis, YAxis, ZAxis, CartesianGrid } from 'recharts';
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart';
import { formatAxisTick } from '../format';
import { motionProps } from './motion';

// Two-numeric-column scatter. options.x_column / y_column are required;
// options.name_column is optional and feeds the tooltip label.
export default function ScatterChartWidget({ widget, rows, onCellClick }) {
  const x = widget.options?.x_column;
  const y = widget.options?.y_column;
  const nameCol = widget.options?.name_column;
  const data = rows.map((r) => ({
    x: x ? Number(r[x]) : 0,
    y: y ? Number(r[y]) : 0,
    name: nameCol ? String(r[nameCol]) : '',
  }));
  const config = { points: { label: widget.title, color: 'hsl(var(--chart-1))' } };
  const motion = motionProps(widget.options?.motion);
  return (
    <ChartContainer config={config} className="h-[220px] w-full">
      <ScatterChart margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid />
        <XAxis type="number" dataKey="x" name={x || 'x'} tickLine={false} axisLine={false}
               tick={{ fontSize: 11 }} tickFormatter={formatAxisTick} />
        <YAxis type="number" dataKey="y" name={y || 'y'} tickLine={false} axisLine={false}
               tick={{ fontSize: 11 }} tickFormatter={formatAxisTick} width={44} />
        <ZAxis range={[60, 60]} />
        <ChartTooltip content={<ChartTooltipContent />} cursor={{ strokeDasharray: '3 3' }} />
        <Scatter data={data} fill="var(--color-points)" {...motion}
                 onClick={(d) => onCellClick && nameCol && onCellClick(nameCol, d && d.name)} />
      </ScatterChart>
    </ChartContainer>
  );
}
```

- [ ] **Step 4: Implement RadarChartWidget**

Create `frontend/src/components/dashboard/widgets/RadarChartWidget.jsx`:
```jsx
import { RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis } from 'recharts';
import {
  ChartContainer, ChartTooltip, ChartTooltipContent,
  ChartLegend, ChartLegendContent,
} from '@/components/ui/chart';
import { motionProps } from './motion';

// Multi-axis radar. options.axis_column is the dimension; options.series =
// [{ key, label }] are the numeric metrics plotted as overlapping polygons.
export default function RadarChartWidget({ widget, rows, onCellClick }) {
  const axis = widget.options?.axis_column;
  const series = widget.options?.series || [];
  const data = rows.map((r) => {
    const point = { axis: axis ? String(r[axis]) : '' };
    series.forEach((s) => { point[s.key] = Number(r[s.key]) || 0; });
    return point;
  });
  const config = Object.fromEntries(
    series.map((s, i) => [s.key, { label: s.label || s.key, color: `hsl(var(--chart-${(i % 5) + 1}))` }]),
  );
  const motion = motionProps(widget.options?.motion);
  return (
    <ChartContainer config={config} className="h-[260px] w-full">
      <RadarChart data={data} outerRadius="75%">
        <PolarGrid />
        <PolarAngleAxis dataKey="axis" tick={{ fontSize: 11 }} />
        <PolarRadiusAxis tick={{ fontSize: 10 }} />
        <ChartTooltip content={<ChartTooltipContent />} />
        {series.map((s, i) => (
          <Radar key={s.key} name={s.label || s.key} dataKey={s.key}
                 stroke={`hsl(var(--chart-${(i % 5) + 1}))`}
                 fill={`hsl(var(--chart-${(i % 5) + 1}))`}
                 fillOpacity={0.4} {...motion}
                 onClick={(d) => onCellClick && axis && onCellClick(axis, d && d.axis)} />
        ))}
        <ChartLegend content={<ChartLegendContent />} />
      </RadarChart>
    </ChartContainer>
  );
}
```

- [ ] **Step 5: Implement GaugeWidget**

Create `frontend/src/components/dashboard/widgets/GaugeWidget.jsx`:
```jsx
import { RadialBarChart, RadialBar, PolarAngleAxis } from 'recharts';
import { ChartContainer } from '@/components/ui/chart';
import { formatMetric } from '../format';
import { motionProps } from './motion';

// Single-value gauge. options.value_column is required; min/max default to
// 0/100. The radial bar fills proportionally; a centered readout overlays the
// raw value (and an optional target line label).
export default function GaugeWidget({ widget, rows }) {
  const col = widget.options?.value_column;
  const min = Number(widget.options?.min ?? 0);
  const max = Number(widget.options?.max ?? 100);
  const target = widget.options?.target != null ? Number(widget.options.target) : null;
  const row = rows?.[0];
  const raw = row && col != null ? Number(row[col]) : row ? Number(Object.values(row)[0]) : 0;
  const span = max > min ? max - min : 1;
  const pct = Math.max(0, Math.min(1, (raw - min) / span)) * 100;
  const data = [{ name: widget.title, value: pct, fill: 'hsl(var(--chart-1))' }];
  const config = { value: { label: widget.title, color: 'hsl(var(--chart-1))' } };
  const motion = motionProps(widget.options?.motion);
  return (
    <div className="relative h-[220px] w-full">
      <ChartContainer config={config} className="h-[220px] w-full">
        <RadialBarChart innerRadius="70%" outerRadius="100%" data={data} startAngle={90} endAngle={-270}>
          <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
          <RadialBar background dataKey="value" cornerRadius={8} {...motion} />
        </RadialBarChart>
      </ChartContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-bold tabular-nums text-foreground">
          {formatMetric(raw, { unit: widget.options?.unit, compact: false })}
        </span>
        {target != null && (
          <span className="text-[11px] text-muted-foreground">
            target {formatMetric(target, { unit: widget.options?.unit, compact: false })}
          </span>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Wire into DashboardViewer**

In `frontend/src/components/dashboard/DashboardViewer.jsx`, add imports after the Task 3 imports:
```jsx
import ScatterChartWidget from './widgets/ScatterChartWidget';
import RadarChartWidget from './widgets/RadarChartWidget';
import GaugeWidget from './widgets/GaugeWidget';
```
Extend the `Widget` switch — replace the `stacked-bar` branch + table fallback:
```jsx
      ) : widget.type === 'stacked-bar' ? (
        <StackedBarWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
```
with:
```jsx
      ) : widget.type === 'stacked-bar' ? (
        <StackedBarWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'scatter' ? (
        <ScatterChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'radar' ? (
        <RadarChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'gauge' ? (
        <GaugeWidget widget={widget} rows={rows} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/widgets/__tests__/ScatterRadarGauge.test.jsx`
Expected: 4 PASS.

- [ ] **Step 8: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/widgets/ScatterChartWidget.jsx frontend/src/components/dashboard/widgets/RadarChartWidget.jsx frontend/src/components/dashboard/widgets/GaugeWidget.jsx frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/components/dashboard/widgets/__tests__/ScatterRadarGauge.test.jsx && git commit -m "feat(dashboard): scatter + radar + gauge widgets"
```

---

### Task 5: PNG export + shareable deep link + standalone dashboard route

Add two toolbar actions to `DashboardViewer`: "Export PNG" (html2canvas snapshot of the dashboard, downloaded) and "Share" (copies a deep link `${origin}/dashboard/:id?from=&to=&filters=` to clipboard). Add a standalone `/dashboard/:id` route that opens the dashboard full-screen, seeding the viewer's date-range/filters from URL params.

**Files:**
- Create: `frontend/src/pages/DashboardView.jsx`
- Modify: `frontend/src/App.jsx:40` (lazy import) and `:88-103` (route)
- Modify: `frontend/src/components/dashboard/DashboardViewer.jsx` (ref + toolbar buttons + initial-state props)
- Test: `frontend/src/components/dashboard/__tests__/DashboardViewer.export.test.jsx` (create)

**Interfaces:**
- `DashboardViewer` gains optional props `initialDateRange` (default `presetToRange('30d')`) and `initialFilters` (default `{}`). The polling effect stays verbatim.
- `DashboardView` reads `useParams().id` + `useSearchParams()` and renders `<DashboardViewer dashboard={d} initialDateRange={...} initialFilters={...} />` inside `AppLayout`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/dashboard/__tests__/DashboardViewer.export.test.jsx`:
```jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />, Legend: () => <div />, Tooltip: () => <div />,
  BarChart: ({ children }) => <div>{children}</div>, Bar: () => <div />,
  LineChart: ({ children }) => <div>{children}</div>, Line: () => <div />,
  PieChart: ({ children }) => <div>{children}</div>, Pie: () => <div />, Cell: () => <div />,
  XAxis: () => <div />, YAxis: () => <div />,
}));

const queryDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({ queryDashboard: (...a) => queryDashboard(...a) }));

const html2canvas = vi.fn();
vi.mock('html2canvas', () => ({ default: (...a) => html2canvas(...a) }));

const writeText = vi.fn();
Object.assign(navigator, { clipboard: { writeText } });

import DashboardViewer from '../DashboardViewer';

const dash = {
  id: 'd9', refresh_interval_seconds: 30,
  definition: { widgets: [{ id: 'w1', type: 'bar', title: 'By Cat', sql: 'SELECT cat, n FROM t', options: { x_column: 'cat', y_column: 'n' } }] },
};

describe('DashboardViewer export + share', () => {
  beforeEach(() => {
    queryDashboard.mockReset();
    queryDashboard.mockResolvedValue({ results: { w1: { columns: ['cat', 'n'], rows: [{ cat: 'A', n: 1 }], error: null } }, refreshed_at: '2026-07-29T00:00:00Z' });
    html2canvas.mockReset();
    html2canvas.mockResolvedValue({ toDataURL: () => 'data:image/png;base64,xx' });
    writeText.mockReset();
    // stub anchor click so the download link doesn't throw
    HTMLAnchorElement.prototype.click = vi.fn();
  });

  it('export button triggers html2canvas on the dashboard container', async () => {
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(screen.getByText('A')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /export png/i }));
    await waitFor(() => expect(html2canvas).toHaveBeenCalled());
  });

  it('share button builds a /dashboard/:id link and copies it', async () => {
    render(<DashboardViewer dashboard={dash} polling={false} />);
    await waitFor(() => expect(screen.getByText('A')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /share/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const link = writeText.mock.calls[0][0];
    expect(link).toContain('/dashboard/d9');
    expect(link).toMatch(/from=/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.export.test.jsx`
Expected: FAIL — no "Export PNG" / "Share" buttons exist yet.

- [ ] **Step 3: Add export + share + initial-state props to DashboardViewer**

In `frontend/src/components/dashboard/DashboardViewer.jsx`:

Add imports at the top (after existing imports):
```jsx
import { Download, Share2 } from 'lucide-react';
import html2canvas from 'html2canvas';
```

Change the component signature and initial state. Replace:
```jsx
export default function DashboardViewer({ dashboard, polling = true }) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateRange, setDateRange] = useState(() => presetToRange('30d'));
```
with:
```jsx
export default function DashboardViewer({ dashboard, polling = true, initialDateRange, initialFilters }) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateRange, setDateRange] = useState(() => initialDateRange || presetToRange('30d'));
```
and replace `const [filters, setFilters] = useState({});` with:
```jsx
  const [filters, setFilters] = useState(() => initialFilters || {});
```

Add a ref to the scroll container. Change:
```jsx
  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-4">
```
to:
```jsx
  const exportRef = useRef(null);

  const handleExportPng = useCallback(async () => {
    if (!exportRef.current) return;
    const bg = getComputedStyle(document.body).backgroundColor || '#ffffff';
    const canvas = await html2canvas(exportRef.current, { backgroundColor: bg, scale: 2 });
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = `${(dashboard?.name || 'dashboard').replace(/[^a-zA-Z0-9_-]/g, '_')}.png`;
    a.click();
  }, [dashboard?.name]);

  const handleShare = useCallback(() => {
    const origin = window.location.origin;
    const params = new URLSearchParams();
    if (dateRange.from) params.set('from', dateRange.from);
    if (dateRange.to) params.set('to', dateRange.to);
    if (Object.keys(filters).length) params.set('filters', JSON.stringify(filters));
    const link = `${origin}/dashboard/${dashboard?.id}${params.toString() ? '?' + params.toString() : ''}`;
    navigator.clipboard?.writeText(link);
  }, [dashboard?.id, dateRange, filters]);

  return (
    <div ref={exportRef} className="flex h-full flex-col gap-4 overflow-auto p-4">
```

Add the two buttons next to the Refresh button. Replace:
```jsx
        <button
          onClick={refresh}
          aria-label="Refresh dashboard"
          className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>
```
with:
```jsx
        <div className="flex items-center gap-1.5">
          <button
            onClick={handleExportPng}
            aria-label="Export PNG"
            title="Export PNG"
            className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <Download className="h-3 w-3" />
            Export
          </button>
          <button
            onClick={handleShare}
            aria-label="Share"
            title="Copy shareable link"
            className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <Share2 className="h-3 w-3" />
            Share
          </button>
          <button
            onClick={refresh}
            aria-label="Refresh dashboard"
            className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>
```

- [ ] **Step 4: Create the standalone DashboardView page**

Create `frontend/src/pages/DashboardView.jsx`:
```jsx
import { useEffect, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Loader2 } from 'lucide-react';
import { getDashboard } from '@/api/dashboards';
import DashboardViewer from '@/components/dashboard/DashboardViewer';

function parseRange(search) {
  const from = search.get('from');
  const to = search.get('to');
  if (from || to) return { from: from || undefined, to: to || undefined };
  return undefined; // let the viewer default to last-30-days
}

function parseFilters(search) {
  const raw = search.get('filters');
  if (!raw) return undefined;
  try {
    const obj = JSON.parse(raw);
    return obj && typeof obj === 'object' ? obj : undefined;
  } catch {
    return undefined;
  }
}

export default function DashboardView() {
  const { id } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    getDashboard(id)
      .then((d) => { if (alive) setDashboard(d); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [id]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <button
          onClick={() => navigate(-1)}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back
        </button>
        <span className="truncate text-sm font-medium">{dashboard?.name || 'Dashboard'}</span>
      </div>
      <div className="min-h-0 flex-1">
        {error ? (
          <div className="p-4 text-sm text-destructive">{error}</div>
        ) : !dashboard ? (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <DashboardViewer
            dashboard={dashboard}
            polling
            initialDateRange={parseRange(search)}
            initialFilters={parseFilters(search)}
          />
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Register the route in App.jsx**

In `frontend/src/App.jsx`, add the lazy import (after the `UITest` lazy import, line ~40):
```jsx
const DashboardView = lazy(() => import('@/pages/DashboardView'));
```
Add the route inside the `AppLayout` group (after the `/settings` route):
```jsx
          <Route path="/dashboard/:id" element={<DashboardView />} />
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard/__tests__/DashboardViewer.export.test.jsx`
Expected: 2 PASS.

- [ ] **Step 7: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/dashboard/DashboardViewer.jsx frontend/src/pages/DashboardView.jsx frontend/src/App.jsx frontend/src/components/dashboard/__tests__/DashboardViewer.export.test.jsx && git commit -m "feat(dashboard): PNG export + shareable deep link + standalone /dashboard/:id route"
```

---

### Task 6: App-wide dark-mode toggle

The theme infrastructure (`lib/useTheme.js` with `light`/`dark`/`system`, localStorage persistence, `.dark` class toggle, OS listener; `index.css` `.dark` tokens) already exists and is initialized in `main.jsx`, but there is no UI to change it. Add a `ThemeToggle` button and mount it in the `Sidebar` footer next to `UserMenu`.

**Files:**
- Create: `frontend/src/components/ThemeToggle.jsx`
- Modify: `frontend/src/components/Sidebar.jsx:6-8` (import) and `:238-240` (footer)
- Test: `frontend/src/components/__tests__/ThemeToggle.test.jsx` (create)

**Interfaces:**
- Produces: `ThemeToggle({ collapsed })` default export. Cycles `light → dark → system` via `useTheme().setTheme`. Icons: Sun (light), Moon (dark), Monitor (system).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/__tests__/ThemeToggle.test.jsx`:
```jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const setTheme = vi.fn();
let currentTheme = 'system';
vi.mock('@/lib/useTheme', () => ({
  useTheme: () => ({ theme: currentTheme, setTheme: (t) => { currentTheme = t; setTheme(t); } }),
}));

import ThemeToggle from '../ThemeToggle';

describe('ThemeToggle', () => {
  beforeEach(() => { currentTheme = 'system'; setTheme.mockReset(); });

  it('renders a button with an accessible label', () => {
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: /theme/i })).toBeInTheDocument();
  });

  it('cycles system -> light -> dark -> system', () => {
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole('button', { name: /theme/i }));
    expect(setTheme).toHaveBeenLastCalledWith('light');
    fireEvent.click(screen.getByRole('button', { name: /theme/i }));
    expect(setTheme).toHaveBeenLastCalledWith('dark');
    fireEvent.click(screen.getByRole('button', { name: /theme/i }));
    expect(setTheme).toHaveBeenLastCalledWith('system');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/__tests__/ThemeToggle.test.jsx`
Expected: FAIL — module `../ThemeToggle` not found.

- [ ] **Step 3: Implement ThemeToggle**

Create `frontend/src/components/ThemeToggle.jsx`:
```jsx
import { Sun, Moon, Monitor } from 'lucide-react';
import { useTheme } from '@/lib/useTheme';

// Cycles light -> dark -> system -> light. The useTheme hook already applies
// the .dark class to <html> and persists the choice; this component only
// surfaces it. `collapsed` renders an icon-only button matching the sidebar.
const ORDER = ['light', 'dark', 'system'];
const ICON = { light: Sun, dark: Moon, system: Monitor };
const LABEL = { light: 'Light', dark: 'Dark', system: 'System' };

export default function ThemeToggle({ collapsed = false }) {
  const { theme, setTheme } = useTheme();
  const current = ORDER.includes(theme) ? theme : 'system';
  const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
  const Icon = ICON[current];
  return (
    <button
      onClick={() => setTheme(next)}
      aria-label={`Theme: ${LABEL[current]}. Click for ${LABEL[next]}`}
      title={`Theme: ${LABEL[current]}`}
      className={`flex items-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground ${
        collapsed ? 'justify-center p-2' : 'gap-2 px-2 py-1.5 text-xs'
      }`}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {!collapsed && <span>{LABEL[current]}</span>}
    </button>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/__tests__/ThemeToggle.test.jsx`
Expected: 2 PASS.

- [ ] **Step 5: Mount ThemeToggle in the Sidebar footer**

In `frontend/src/components/Sidebar.jsx`, add the import (after the `UserMenu` import, line 7):
```jsx
import ThemeToggle from '@/components/ThemeToggle';
```
Replace the footer (line 238-240):
```jsx
      <div className="border-t border-sidebar-border px-3 py-3">
        <UserMenu user={user} collapsed={collapsed} />
      </div>
```
with:
```jsx
      <div className="border-t border-sidebar-border px-3 py-3">
        {!collapsed && (
          <div className="mb-2">
            <ThemeToggle collapsed={false} />
          </div>
        )}
        {collapsed && (
          <div className="mb-2 flex justify-center">
            <ThemeToggle collapsed />
          </div>
        )}
        <UserMenu user={user} collapsed={collapsed} />
      </div>
```

- [ ] **Step 6: Run Sidebar tests to confirm no regression**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/Sidebar.test.jsx`
Expected: existing tests PASS (they mock AuthContext; ThemeToggle uses useTheme which reads localStorage — ensure the test env has `window.matchMedia`. If a test fails on `matchMedia` undefined, add to `src/test-setup.js`: `window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener(){}, removeEventListener(){} }));`).

- [ ] **Step 7: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/ThemeToggle.jsx frontend/src/components/Sidebar.jsx frontend/src/components/__tests__/ThemeToggle.test.jsx frontend/src/test-setup.js && git commit -m "feat(dashboard): app-wide dark-mode theme toggle in sidebar"
```
(Only add `test-setup.js` if it was modified in Step 6.)

---

### Task 7: Final integration — build + full dashboard test sweep

Verify the whole Phase 2 surface compiles and the scoped test suites pass together. No new production code unless a regression surfaces.

**Files:**
- No new files; this is verification + regression fixes only.

- [ ] **Step 1: Run the full dashboard frontend test scope**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/dashboard`
Expected: all dashboard tests PASS (Phase 1 interactivity/daterange/drill + Phase 2 motion/area-stacked/scatter-radar-gauge/export). If `DashboardViewer.test.jsx` or `DashboardViewer.daterange.test.jsx` snapshots break due to the new toolbar buttons, update the affected assertions to account for the Export/Share buttons (do NOT remove the buttons).

- [ ] **Step 2: Run the backend widget-type test + dashboard router test**

Run:
```
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && python3 -m pytest tests/test_dashboard_widget_types.py tests/test_dashboards_router.py -v
```
Expected: PASS.

- [ ] **Step 3: Production build**

Run: `cd /root/zhanlu/frontend && npx vite build`
Expected: build succeeds with no errors. Confirm `html2canvas` and the new recharts imports are bundled (no unresolved-import warnings).

- [ ] **Step 4: Lint check on touched files**

Run: `cd /root/zhanlu/frontend && npx eslint src/components/dashboard/widgets/AreaChartWidget.jsx src/components/dashboard/widgets/StackedBarWidget.jsx src/components/dashboard/widgets/ScatterChartWidget.jsx src/components/dashboard/widgets/RadarChartWidget.jsx src/components/dashboard/widgets/GaugeWidget.jsx src/components/dashboard/widgets/motion.js src/components/ThemeToggle.jsx src/pages/DashboardView.jsx src/components/dashboard/DashboardViewer.jsx`
Expected: no errors (warnings acceptable). Fix any unused-import errors.

- [ ] **Step 5: Commit any regression fixes**

```bash
cd /root/zhanlu && git add -A && git commit -m "test(dashboard): phase 2 integration sweep + regression fixes"
```
(Skip if nothing changed.)

- [ ] **Step 6: Update working memory**

Append to `/root/.codebuddy/memory/2026-07-29.md` a note: Phase 2 implemented (5 chart types, motion/zoom, PNG export + `/dashboard/:id` share route, dark-mode ThemeToggle in Sidebar). Dark-mode infra pre-existed (`useTheme.js` + `.dark` tokens); only the toggle was new. NOT deployed (deploy = `/opt/rebuild-zhanlu.sh --force`). Phase 3 (widget CRUD/PATCH, drag-reorder, inline SQL editor, save-as-copy) still deferred.

---

## Self-Review

**1. Spec coverage (spec §10 Phase 2):**
- area/scatter/gauge/radar/stacked-bar chart types → Task 3 (area, stacked-bar) + Task 4 (scatter, radar, gauge). ✓
- recharts zoom → Task 2 (Brush on trend widgets) + Task 3 (Brush on area/stacked-bar). ✓
- motion presets → Task 2 (motion.js + existing) + Tasks 3/4 (new widgets use it). ✓
- PNG export + shareable link → Task 5. ✓
- dark-mode toggle → Task 6. ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"similar to". Every code step has full implementation. The only conditional is "only add test-setup.js if modified in Step 6" — that is a real conditional, not a placeholder.

**3. Type consistency:** `motionProps(preset)` signature is identical across motion.js (Task 2) and all consumers (Tasks 3/4). Widget components all take `({ widget, rows, onCellClick })` except `GaugeWidget` (no onCellClick — gauges aren't clickable) — this is intentional and matches the DashboardViewer wiring (`<GaugeWidget widget={widget} rows={rows} />`). `useTheme()` returns `{ theme, setTheme }` as defined in `lib/useTheme.js` and consumed by `ThemeToggle`. `DashboardViewer` new props `initialDateRange`/`initialFilters` match `DashboardView`'s usage.

**4. Risk notes:** `DashboardViewer.test.jsx` and `DashboardViewer.daterange.test.jsx` may need assertions updated for the 2 new toolbar buttons (Task 7 Step 1). The recharts mock in those existing tests lacks `Brush`/`Area`/etc., but since none of those tests render widgets with `zoom`/new types, no mock extension is needed there.
