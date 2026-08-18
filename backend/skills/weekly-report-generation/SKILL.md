# Weekly Market Report Generation

## Purpose

Generate a CEO-level weekly market report for C5/C9 petrochemical products
(piperylene, isoprene, dcpd, c5_resin, SIS, etc.) using the Ecisco BI data
pipeline. The report must be data-grounded, structured markdown with tables
and explicit risk flags — ready to read in chat, no file generation overhead.

## Use when

- The user asks for a "weekly report", "market report", "weekly summary",
  "weekly review", or "周报 / 本周报告".
- The project is Ecisco BI (C5/C9 products).

## Critical rules

1. **Use the batch tools.** Step 1 MUST use
   `ask_perception_intelligence_diagnosis("<product>")` — ONE parallel tool
   call that runs ask_perception + ask_intelligence + ask_diagnosis
   concurrently (~3x faster than three serial ask_* calls). Step 3 MUST use
   `ask_forecast_pricing("<product>")` — ONE parallel tool call that runs
   ask_forecast_agent + ask_pricing concurrently (~2x faster than two serial
   ask_* calls). NEVER run these sub-agents serially.
2. **Never fabricate numbers.** If a pipeline returns no data, write
   "No data available" — never invent prices, volumes, or forecasts.
3. **Schema pitfall:** `erp_product_sales_details` uses `FNAME` (NOT
   `material_name`) for the product name. Never use `material_name`.
   Prefer the LIVE tables `erp_t_sal_outstock` / `erp_v_sale_orderentry`
   (current through 2026) over the STALE `erp_product_sales_details`
   (data ends 2025-12-31).
4. **Data freshness check.** Before claiming "last 30 days" / recent-period
   coverage, confirm the latest date with MAX(<date_column>). If the freshest
   data is stale (>30 days), state it explicitly: "Latest data is from
   2025-12-31; the analysis uses the available data." Never claim coverage
   the data does not have.
5. **Prioritize.** If the run exceeds ~60s, prioritize Steps 1 + 2 + 4
   (market context + data + synthesis) over Step 3 (forecast/pricing).
6. **Cite sources.** Every section names the tool/pipeline that produced
   its data (e.g. "source: ask_perception_intelligence_diagnosis").
7. **Tool budget discipline.** Exactly two `ask_data_agent` calls are
   budgeted for Step 2: one for sales, one for inventory. Never exhaust the
   tool loop before Step 4 synthesis.

## Pipeline

### Step 1 — Market context (BATCH, one tool call)

Call `ask_perception_intelligence_diagnosis("<product>")` once. It runs all
three independent sub-agents in parallel and returns a combined answer:

- perception — current market events/signals
- intelligence — news/intelligence layer
- diagnosis — root-cause diagnosis

Do NOT call `ask_perception`, `ask_intelligence`, or `ask_diagnosis`
separately — the batch tool is faster and uses one tool call instead of
three.

### Step 2 — Data deep-dive (TWO `ask_data_agent` calls — sales + inventory)

Query ALL C5/C9 products in ONE query using the unified LIVE views. Do NOT
query product-specific views one at a time (`sale_erp_v_抽余碳五_data`,
`sale_erp_v_双环戊二烯_data`, …) — each holds a single product and silently
produces a one-product report. The C5/C9 portfolio covers: 碳五石油树脂,
双环戊二烯, 异戊二烯, 戊烷发泡剂, 抽余碳五, 间戊二烯, 工业用裂解碳九.

**Call A — Sales (ALL products):**
`ask_data_agent("weekly sales volume, shipment quantity and revenue for ALL
C5/C9 products in ONE query from erp_v_sale_orderentry (or erp_t_sal_outstock)
grouped by product name; include latest date via MAX(date_column); products:
碳五石油树脂, 双环戊二烯, 异戊二烯, 戊烷发泡剂, 抽余碳五, 间戊二烯,
工业用裂解碳九 — do not restrict to a single product")`

**Call B — Inventory (ALL products):**
`ask_data_agent("current inventory for ALL C5/C9 products from
erp_v_stk_inventory (or erp_t_stk_inventory) — inventory quantity/position by
product name; same product list as the sales query")`

Rules:
- ALWAYS use the unified tables (`erp_v_sale_orderentry` / `erp_t_sal_outstock`
  for sales; `erp_v_stk_inventory` / `erp_t_stk_inventory` for inventory).
  Never a single product-specific view — that yields a one-product report.
- If the user says "C5/C9 products" (or the weekly report covers the C5/C9
  portfolio), the report MUST cover all seven products listed above.
- Prefer the LIVE tables over the STALE `erp_product_sales_details` (note the
  product name column is FNAME there).
- Verify freshness with MAX(date_column) before reporting date-bounded numbers.

### Step 3 — Forward view (BATCH, one tool call)

Call `ask_forecast_pricing("<product>")` once. It runs both sub-agents in
parallel and returns a combined answer:

- forecast — 3d/7d/30d ensemble forecast
- pricing — 9-step pricing recommendation/context

Do NOT call `ask_forecast_agent` or `ask_pricing` separately — the batch
tool is faster and uses one tool call instead of two.

### Step 4 — Synthesize the CEO report

Produce structured markdown with these sections (skip a section only when
the data is genuinely unavailable — then state "No data available"):

```markdown
## Executive Summary
- 3-5 bullet takeaways; lead with the single most important move for the week.

## Price Dashboard
| Product | Price | WoW Δ | Trend | Source |
|---------|-------|-------|-------|--------|

## Volume & Sales
- Shipment volume vs prior week; notable customer/region moves.

## Inventory Status
- Inventory position, days-of-stock signal, build/drawdown flags.

## Key Risks & Opportunities
- **Risks:** bullet list with severity flags (⚠️ high / 🟡 medium)
- **Opportunities:** bullet list

## Forward Outlook
- Forecast direction (3d/7d/30d), key catalysts, confidence level.
- If below-baseline forecast exists, surface it prominently (honesty gate).
```

## Honesty gate

- Never fabricate prices, forecasts, or market events from training data.
- If data is insufficient for confidence, say so explicitly.
- If a forecast has below_naive_baseline=True, surface it prominently.
- If the underlying data is stale, disclose it — do not present stale
  numbers as the current picture.

## Safety rules

- Do not access raw credentials or unrestricted DB connections.
- All data comes from the Ecisco BI delegation tools (ask_*), never from
  direct SQL written by the agent.
