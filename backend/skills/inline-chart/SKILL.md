# Inline Chart

## Purpose

When the user asks to SEE data — a chart, graph, plot, trend, breakdown,
图表 / 可视化 / 画个图 / 做个图 — the deliverable is a rendered chart, not a
description of numbers. This skill gives you the one call that renders it and
the document shape that call requires.

## Use when

- The user asks to visualise data: "show me a chart of X", "plot Y by Z",
  "画个柱状图", "可视化一下", "trend of X over time".
- The user wants the chart IN the chat (not a file to download).

If the user asks for a downloadable file (PPT / Word / Excel / PDF) instead,
see "Downloadable documents" at the end — the payload differs.

## The only way to render a chart inline

Call the `create_artifact` tool with `type="html"` and a self-contained
document in `payload.html_content`:

```
create_artifact(
  type="html",
  title="<short chart title>",
  payload={"html_content": "<!DOCTYPE html> …"}
)
```

`type` has NO "chart" value — the enum is `docx | pdf | pptx | html`. An
inline chart IS an html artifact. Writing `type="chart"` fails validation.

The returned artifact is previewed inline in the conversation in a sandboxed
iframe. That iframe is why the document must be self-contained.

## Self-contained document rules (hard)

The preview iframe has NO network access. A document that depends on an
external resource renders blank.

1. **No external resources.** No CDN `<script>`, no Google Fonts, no external
   stylesheet, no remote image. Zero `http://` / `https://` / `//` URLs.
2. **Inline the CSS** in a `<style>` block. Inline the drawing as SVG or
   Canvas. If you need a JS charting library you must inline the whole
   library — do NOT do that; draw SVG instead.
3. **Hard-code the data** as literal values in the markup. The preview does
   not re-run queries.
4. **Aggregate before you draw.** One mark per category, never one per raw
   row. Cap at 25 categories (top 25 by value); if there are more, aggregate
   the tail into an "Other" bar and say so in the subtitle.
5. **Size to the frame.** Set the root to `width:100%`, give the chart a
   fixed `viewBox` with `preserveAspectRatio="xMidYMid meet"` and let it
   scale. Never use a fixed pixel width that overflows.
6. **Label every axis and unit.** Axis title + value unit (¥ / t / % / units)
   and thousands separators. An unlabelled chart is not a deliverable.

## Chart type selection

| The question | Chart | Notes |
|---|---|---|
| How does X change over time? | line / area | x ascending left → right |
| Compare X across categories? | horizontal bar | sorted by value DESC |
| Share of a whole? | donut or stacked bar | ≤6 slices, else use bars |
| One headline figure? | big number + delta | not a chart |
| Two measures, one x-axis? | bars + line, dual axis | label both axes |

Sort bars by value descending unless the category has a natural order
(months, stages, grades). If a donut needs more than 6 slices, use bars.

## Colour and type

- Use CSS custom properties so the chart adapts to light/dark, and default to
  the token palette rather than inventing hex values:
  `--c1:#4f7cff; --c2:#22c55e; --c3:#f59e0b; --c4:#ef4444; --sample:#8b5cf6`.
- Neutral chrome: gridlines and axes in `rgba(127,127,127,.25)`, labels in
  `currentColor` at ~12px so they inherit the theme.
- System font stack only — `-apple-system, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", sans-serif` — because web fonts will not load.
- No 3D, no gradients-as-decoration, no drop shadows on data marks, no
  rainbow palettes, no emoji as icons.

## Worked pattern — horizontal bar chart

Adapt this. It is complete and needs no external resource.

```html
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<style>
  :root{--c1:#4f7cff;--ink:currentColor;--rule:rgba(127,127,127,.25)}
  *{box-sizing:border-box}
  body{margin:0;padding:16px;font:14px -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  h3{margin:0 0 2px;font-size:15px}
  p.sub{margin:0 0 14px;font-size:12px;opacity:.65}
  .row{display:grid;grid-template-columns:112px 1fr 92px;align-items:center;gap:10px;margin:7px 0}
  .lbl{font-size:12px;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .track{height:16px;background:var(--rule);border-radius:3px;overflow:hidden}
  .bar{height:100%;background:var(--c1);border-radius:3px}
  .val{font-size:12px;text-align:right;font-variant-numeric:tabular-nums}
  .foot{margin-top:12px;font-size:11px;opacity:.55}
</style></head><body>
  <h3>Line revenue by product</h3>
  <p class="sub">Top 8 products, aggregated from closed lines · FY2026</p>
  <div class="row"><span class="lbl">Product A</span><span class="track"><span class="bar" style="width:100%"></span></span><span class="val">¥1,240,000</span></div>
  <div class="row"><span class="lbl">Product B</span><span class="track"><span class="bar" style="width:62%"></span></span><span class="val">¥769,000</span></div>
  <div class="foot">Source: sales_lines, grouped by product_code · 8 of 23 categories shown</div>
</body></html>
```

Bar `width` is the value as a percentage of the largest value. Keep the label
column wide enough for real names — never truncate a category name in the
data, only in CSS with a title attribute if unavoidable.

For a time series, use the same document shell with an `<svg viewBox="0 0 640 260">`
containing a `<polyline>` (or `<path>`) plus axis `<line>` elements; for a
part-of-whole, emit one `<path>` arc per slice with a legend beside it.

## Hard rules

1. **Never draw a chart out of text characters.** ASCII bar charts made of
   `█ ▓ ░ # = ▬` box-drawing blocks, or a plot faked in a code fence, are NOT
   a chart. They are the failure mode this skill exists to prevent.
2. **Never substitute prose for the chart.** "Revenue was 1.2M, up 12%" is not
   what the user asked for when they asked to see a chart. Render it.
3. **Draw from real data only.** Every number in the document must come from a
   query or an attached file in this conversation. If you have no data, get it
   first (nl2sql / the attached file) — never invent figures to fill a chart.
4. **One artifact per request.** Do not emit two competing charts. If the user
   wants several angles, that is a dashboard, not this skill.
5. **Say the chart exists, briefly.** The answer text around the artifact is
   one or two sentences — the headline finding and the source. Never narrate
   the tool call, the plan, or the document internals.

## Downloadable documents

If the user wants a file rather than an inline preview, use the same
`create_artifact` tool with the matching type and a ReportCard-shaped payload:

```
create_artifact(
  type="docx",            # or pdf / pptx
  title="<document title>",
  payload={
    "summary": "<one paragraph>",
    "kpis": [{"label":"Revenue","value":"¥1,240,000","delta":"+12% YoY"}],
    "chart": {"title":"Revenue by product","type":"bar",
              "data":[{"product":"A","revenue":1240000}, …]},
    "insights":[{"text":"…"}]
  }
)
```

Here `chart.type` is a chart KIND (`bar | line | pie | area | combo`) and
`chart.data` is a list of row objects whose keys match the axis labels — the
renderer draws it natively. This path needs no HTML and no self-contained
rules; the export pipeline builds the document.

## Common mistakes

- `type="chart"` → invalid enum, the call is rejected.
- External CDN script → blank preview, no error shown to the user.
- One bar per raw row → unreadable; aggregate first.
- Fixed `width:900px` → overflows the frame.
- No unit on the axis → the reader cannot interpret the magnitude.
- Chart drawn, then the answer text re-lists every number → redundant.
