# Dashboard UI Polish

## Purpose

Make dashboards look designed, not generated. Polish comes from tokens,
typography, spacing, and well-crafted states — never from ad-hoc colors.

## Use when

- Writing or reviewing dashboard frontend components.
- Choosing colors, type sizes, spacing, or state copy for any widget.

## Tokens only

- Series colors: `hsl(var(--chart-1))` … `hsl(var(--chart-5))`.
- Chrome: `bg-card`, `border-border`, `text-muted-foreground`,
  `bg-background`, `text-destructive`.
- NEVER hardcode hex/rgb in components. One token edit must re-theme
  every chart.
- Dark mode: every token used must have a `.dark` definition. If you add a
  chart color to `:root`, add its `.dark` counterpart in the same change.

## Typography scale

| Element | Style |
|---|---|
| Dashboard name | font-heading, 18px, 600 |
| Widget title | 14px, 500 |
| KPI value | 30px, 700, tabular-nums |
| Meta / labels / timestamps | 12px, text-muted-foreground |
| Table header | 12px, muted, uppercase, tracking-wide |

Numbers in KPIs, tables, and tooltips always use tabular numerals so
columns align.

## Card anatomy

`rounded-xl border border-border bg-card p-4` + subtle shadow; hover
elevation (`hover:shadow-md` + transition). Hairline dividers, not heavy
borders. Padding stays consistent across all widgets.

## States (every widget needs all three)

- **Loading**: skeleton matching the widget's aspect (pulse bars), fixed
  min-height so nothing jumps.
- **Empty**: centered, dashed border, one helpful line
  (`No data yet — this widget will fill in on the next refresh.`).
- **Error**: destructive tint card, short message, retry hint. A failing
  widget never blanks the whole dashboard.

## Accessibility

- Text contrast ≥ 4.5:1 in both themes.
- Icon-only buttons get `aria-label` (refresh, close).
- Don't encode meaning by color alone — deltas pair color with ▲/▼ arrows.

## Common mistakes

- Hardcoded `#6366f1`-style hex → use `hsl(var(--chart-N))`.
- Missing `.dark` chart tokens → series invisible on dark cards.
- Bare huge KPI number with no label/unit → always add context.
- No empty state → a blank card looks broken, not empty.
