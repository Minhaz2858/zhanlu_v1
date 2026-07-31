# Chart Patterns & Data Visualization

Guidance for creating professional, modern charts in PptxGenJS. Covers color rules, data validation, chart styling defaults, and critical pitfalls.

---

## Color Rules: Categorical vs Semantic

Use two distinct color sets depending on whether color carries meaning:

### Categorical Palette (CAT)

For data series, groups, or categories where color is just a distinguisher — none implies "good" or "bad":

```javascript
const CAT = [
  "4E79A7",  // steel blue
  "F28E2B",  // orange
  "E15759",  // coral
  "76B7B2",  // teal
  "59A14F",  // green
  "EDC948",  // gold
  "B07AA1",  // mauve
  "FF9DA7",  // pink
  "9C755F",  // brown
  "BAB0AC",  // gray
];
```

> **Note**: PptxGenJS uses 6-char hex WITHOUT `#`. Never prefix with `#` — it corrupts the file.

### Semantic Colors (SEM)

Only when the color itself carries meaning (good/bad, success/failure, warning):

```javascript
const SEM_GREEN  = "2BA185";   // positive / success / good
const SEM_RED    = "F55459";   // negative / failure / bad
const SEM_AMBER  = "D4953A";   // warning / caution
```

### When to Use Which

| Scenario | Palette | Example |
|----------|---------|---------|
| Bar chart comparing 7 SKU types | `CAT[0]`–`CAT[6]` | Each SKU gets a distinct hue, none implies "good" or "bad" |
| Stacked area: accepted vs dropped | Semantic | green = accepted (good), red = dropped (bad) |
| Before/after comparison | `CAT[0]` / `CAT[1]` | Two neutral colors, neither implies better |
| Priority columns: Protect / Grow / Expand | Semantic | green = protect, amber = grow, red = expand gaps |
| Multiple org lines on same chart | `CAT` | Each org gets next color in sequence |

**Rule of thumb**: If you could swap two colors without losing information, use `CAT`. If swapping would confuse the meaning (e.g., making "errors" green), use semantic.

### Single-Series Rule

For a single-series bar chart where all bars represent the same metric, use a **single `CAT` color** uniformly — do NOT alternate colors per bar unless the bars represent distinct categories.

---

## Data Validation Rule

**CRITICAL: Only create charts for slides backed by real quantitative data from the source content.**

- **Has real data** → use a PptxGenJS chart (BAR, LINE, PIE, etc.)
- **Has no data** → use text-based layouts: cards, tables, bullet columns, diagrams, or quote blocks. Do NOT create a chart with made-up numbers.
- **Unsure if data is real or inferred** → do NOT create a chart. Use a text-based layout instead.

If the source content is purely qualitative (narrative, opinions, strategy, process descriptions), the presentation should use zero charts.

**Never fabricate, estimate, or invent data to populate a chart.**

---

## Modern Chart Styling Defaults

Default PptxGenJS charts render bare — no title, no data labels, dated palette. Apply these options for a modern, clean appearance:

```javascript
slide.addChart(pres.charts.BAR, chartData, {
  x: 0.5, y: 1, w: 9, h: 4, barDir: "col",

  // Custom colors (match your presentation palette)
  chartColors: [CAT[0], CAT[1], CAT[2]],

  // Clean background
  chartArea: { fill: { color: "FFFFFF" }, roundedCorners: true },

  // Muted axis labels
  catAxisLabelColor: "64748B",
  valAxisLabelColor: "64748B",
  catAxisLabelFontSize: 10,
  valAxisLabelFontSize: 10,

  // Subtle grid (value axis only)
  valGridLine: { color: "E2E8F0", size: 0.5 },
  catGridLine: { style: "none" },

  // Data labels on bars
  showValue: true,
  dataLabelPosition: "outEnd",
  dataLabelColor: "1E293B",
  dataLabelFontSize: 10,

  // Hide legend for single series
  showLegend: false,

  // Title
  showTitle: true,
  title: "Chart Title",
  titleColor: "1E293B",
  titleFontSize: 14,
  titleFontFace: "Arial",
});
```

### Key Styling Options

| Option | Purpose | Example |
|--------|---------|---------|
| `chartColors: [...]` | Hex colors for series/segments | `["4E79A7", "F28E2B"]` |
| `chartArea: { fill, border, roundedCorners }` | Chart background | `{ fill: { color: "FFFFFF" }, roundedCorners: true }` |
| `catGridLine/valGridLine` | Grid lines | `{ color: "E2E8F0", size: 0.5 }` or `{ style: "none" }` |
| `lineSmooth: true` | Curved lines (line charts) | `true` |
| `legendPos: "r"` | Legend position | `"b"`, `"t"`, `"l"`, `"r"`, `"tr"` |
| `showValue: true` | Data labels on bars/points | `true` |
| `dataLabelPosition` | Label placement | `"outEnd"`, `"inEnd"`, `"ctr"` |

---

## Chart Types Reference

### Bar / Column Chart

```javascript
slide.addChart(pres.charts.BAR, [{
  name: "Sales",
  labels: ["Q1", "Q2", "Q3", "Q4"],
  values: [4500, 5500, 6200, 7100]
}], {
  x: 0.5, y: 0.6, w: 6, h: 3,
  barDir: "col",  // "col" for vertical, "bar" for horizontal
  chartColors: [CAT[0]],
  showValue: true,
  dataLabelPosition: "outEnd",
  showLegend: false,
  valGridLine: { color: "E2E8F0", size: 0.5 },
  catGridLine: { style: "none" },
});
```

### Line Chart

```javascript
slide.addChart(pres.charts.LINE, [{
  name: "Revenue",
  labels: ["Jan", "Feb", "Mar", "Apr", "May"],
  values: [32, 35, 42, 38, 51]
}], {
  x: 0.5, y: 4, w: 6, h: 3,
  lineSize: 3,
  lineSmooth: true,
  chartColors: [CAT[0]],
  showLegend: false,
});
```

### Pie / Doughnut Chart

```javascript
slide.addChart(pres.charts.DOUGHNUT, [{
  name: "Market Share",
  labels: ["Product A", "Product B", "Other"],
  values: [35, 45, 20]
}], {
  x: 7, y: 1, w: 3, h: 3,
  showPercent: true,
  chartColors: [CAT[0], CAT[1], CAT[9]],
  showLegend: true,
  legendPos: "b",
});
```

### Combo Chart (Bar + Line)

```javascript
slide.addChart(pres.charts.BAR, [
  {
    type: pres.charts.BAR,
    name: "Revenue",
    labels: ["Q1", "Q2", "Q3", "Q4"],
    values: [4500, 5500, 6200, 7100],
    options: { chartColors: [CAT[0]] }
  },
  {
    type: pres.charts.LINE,
    name: "Growth %",
    labels: ["Q1", "Q2", "Q3", "Q4"],
    values: [10, 22, 13, 15],
    options: {
      chartColors: [SEM_AMBER],
      secondaryValAxis: true,
      lineSize: 3,
      lineDashedSize: 0,
    }
  }
], {
  x: 0.5, y: 1, w: 9, h: 4,
  valAxes: [
    { valAxisTitle: "Revenue ($K)", valAxisLabelColor: "64748B" },
    { valAxisTitle: "Growth %", valAxisLabelColor: "64748B" }
  ],
  catAxes: [
    { catAxisLabelColor: "64748B" },
    { catAxisLabelColor: "64748B" }
  ],
});
```

---

## Critical Chart Pitfalls

### 1. Stacked bar `dataLabelPosition` must be `ctr`, `inEnd`, or `inBase`

```javascript
// ✅ CORRECT — for stacked bars
dataLabelPosition: "ctr"

// ❌ WRONG — "outEnd" CORRUPTS THE FILE for stacked bars
dataLabelPosition: "outEnd"
```

> `outEnd` is fine for non-stacked bars. Only stacked bars corrupt with `outEnd`.

### 2. Combo charts with secondary axis need BOTH `valAxes` and `catAxes`

When using `secondaryValAxis` or `secondaryCatAxis`, you **must** supply both `valAxes` (two entries) and `catAxes` (two entries). Without them, pptxgenjs writes axis *ids* it never declares, and PowerPoint **discards that chart** and reports the file as corrupt.

```javascript
// ✅ CORRECT — both arrays supplied with 2 entries each
{
  valAxes: [
    { valAxisTitle: "Primary" },
    { valAxisTitle: "Secondary" }
  ],
  catAxes: [
    { catAxisLabelColor: "64748B" },
    { catAxisLabelColor: "64748B" }
  ]
}

// ❌ WRONG — only valAxes, no catAxes → corrupt file
{
  valAxes: [
    { valAxisTitle: "Primary" },
    { valAxisTitle: "Secondary" }
  ]
  // missing catAxes!
}
```

### 3. Never prefix hex colors with `#` in chart options

```javascript
// ✅ CORRECT
chartColors: ["4E79A7", "F28E2B"]

// ❌ WRONG — corrupts the file
chartColors: ["#4E79A7", "#F28E2B"]
```

### 4. Keep charts native — don't fall back to images

Use `addChart()` for everything PowerPoint can chart. For PowerPoint-native features the library doesn't expose (trendlines, error bars), compute the extra series yourself or post-process the generated OOXML — do not fall back to a rendered image.

Only chart types PowerPoint has no native form for (Sankey, network, chord) go in as images.

### 5. Always validate after writing

```bash
python scripts/office/validate.py deck.pptx
```

The validator reports chart faults and slide-XML defects PowerPoint refuses, and names the fix for each.

---

## Color-Blind Safety

When using color to differentiate data series, ensure the chart is still readable without color:

- **Add data labels** — show values directly on bars/points, not just in legend
- **Use shape or pattern** — in addition to color, use different fill patterns (solid, dashed, dotted) for line charts
- **Avoid red-green as the ONLY differentiator** — 8% of men have red-green color blindness
- **Test with a simulator** — run the deck through a color-blindness simulator if using a custom palette

The `CAT[]` palette above is Tableau-inspired and designed for maximum distinguishability across common color blindness types.
