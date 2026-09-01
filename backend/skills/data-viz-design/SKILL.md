# Data Viz Design

## Purpose

Choose the right widget type and visual encoding for every metric on a
dashboard. The wrong chart makes good data unreadable; the right one makes the
point instantly.

## Use when

- Designing widgets for `create_dashboard` (compose with `dashboard-generation`).
- Deciding between kpi / line / bar / pie / table for a given query result.

## Chart selection

| Question the user asks | Widget | Encoding |
|---|---|---|
| How is X trending over time? | `line` | x = date/time ASC, y = measure |
| Compare X across categories? | `bar` | sorted DESC unless natural order |
| Share of a total? | `pie` | ≤6 slices, parts sum to 100% |
| One headline number? | `kpi` | single value, optional delta |
| Raw records / drill-down? | `table` | all columns, ≤50 rows |

If a pie would need >6 slices, use a `bar` instead. If the user wants both
trend and composition, prefer two widgets over a combo chart.

## Encoding rules

1. ONE measure per chart. Alias columns in SQL (`AS revenue`) so axes map
   cleanly to `x_column` / `y_column` / `value_column`.
2. Time always ascending left → right.
3. Bars sorted descending unless the category has a natural order
   (months, stages, age bands).
4. Pie/donut only for parts-of-whole that sum to 100%, ≤6 categories.
5. No 3D, no dual y-axes, no stacked charts unless the user asks.

## Color

- Series colors come from the theme tokens in categorical order:
  `hsl(var(--chart-1))` … `hsl(var(--chart-5))`. NEVER hardcode hex values.
- Reuse the same token for the same entity across widgets (consistency).
- Never encode meaning by red/green alone — pair with icons or labels.

## Number & date formatting

- Compact notation for large values: 12,400 → `12.4K`; 3,200,000 → `3.2M`.
- Max 2 fraction digits; use tabular numerals so columns of numbers align.
- KPI values may append a `unit` suffix (%, ms, USD) from widget options.
- Axis ticks: short dates (`Jan 5`), compact numbers.

## Axes, grid, legend

- Grid: subtle, horizontal lines only for line/bar.
- Ticks: small, muted; put units in the widget title, not on every tick.
- Legend: required for pie and for any multi-series chart; omit for
  single-series line/bar (the title says it).

## Common mistakes

- Pie with 10+ slices → use sorted bar.
- Unformatted raw numbers (`1243922.337`) → compact + max 2dp.
- Hardcoded brand hex in charts → always theme tokens.
- Legend on a single-series chart → noise, remove it.
