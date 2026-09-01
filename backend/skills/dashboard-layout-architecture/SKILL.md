# Dashboard Layout Architecture

## Purpose

Compose widgets into a layout that reads like a story: headline numbers first,
trends next, breakdowns after, raw detail last. Layout is information
hierarchy, not decoration.

## Use when

- Deciding how many widgets a dashboard needs and in what order
  (compose with `dashboard-generation`).
- Choosing widget types so the grid balances KPIs, charts, and tables.

## Information hierarchy

Order widgets so the grid fills top-left → bottom-right in this sequence:

1. **KPI row** — up to 4 headline numbers answering "how are we doing?".
2. **Trend charts** — 1–2 line/bar charts answering "which direction?".
3. **Breakdown** — 1 pie/donut or bar answering "what is it made of?".
4. **Detail table** — at most 1, always last, for drill-down.

The most important widget goes top-left. Never open with a table.

## Composition rules

- 3–6 widgets total. More than 6 → split into two dashboards.
- At least one KPI and one trend chart (per `dashboard-generation`).
- One measure per chart; don't cram everything into one mega-widget.

## Responsive grid behavior

- KPIs: auto-fit, up to 4 across on desktop, 2 across on mobile.
- Charts: 2-up on desktop (`md:grid-cols-2`), 1-up on mobile.
- Table: full width of its grid cell; keep it the last widget.
- Charts keep a consistent min-height (~200px) so rows align.

## Titles

- Short noun phrases: `Monthly revenue`, `Signups by plan`.
- Put units in the title (`Revenue (USD)`), not on every axis tick.
- No sentences, no "Chart of…", no repeating the dashboard name.

## Density & whitespace

- Generous padding inside cards; consistent gaps between them.
- Whitespace is what makes a dashboard feel professional — do not fill
  every cell just because the grid has room.

## Common mistakes

- Detail table first → users see noise before signal.
- 8–12 widgets → cognitive overload; split into focused dashboards.
- All KPIs, no trend → numbers without direction.
- KPIs buried at the bottom → headline numbers must lead.
