# Spec: Forecast What-If as an Agent Tool (replace WhatIfPanel UI)

**Date**: 2026-08-22
**Status**: Approved (brainstorming) — pending user review of this spec, then writing-plans phase
**Skill flow**: brainstorming → (this spec) → writing-plans → implementation

## 1. Problem Statement

The Market Dashboard has a "What-If Analysis" sidebar panel (`WhatIfPanel.jsx`) with two sliders (Benchmark A Crude, Product_C) and a `Run Simulation` button. User feedback: clicking `Run Simulation` "nothing happens" — the result renders inline but is easy to miss, and the button is disabled when both sliders are at 0, making the panel feel broken. The user requested moving this capability into the conversational AI agent so users can simply ask "what if Benchmark A jumps +10%?" / "如果基准A涨10%会怎样?" and get an answer in chat.

The what-if math already exists in the backend (`GET /forecast-ops/what-if/{product_key}`), but **no agent tool exposes it** — the agent currently cannot answer scenario questions even though it can answer forecast run/accuracy/report questions. This spec closes that gap.

## 2. Decisions (approved via brainstorming)

| Question | Decision |
|---|---|
| Scope | **Replace the UI panel with an agent tool** (remove `WhatIfPanel` from the dashboard, add `forecast_what_if` tool, wire into 3 agents). |
| Output format | **Text + inline table only** — no chart artifact, no `Artifact` DB write. |
| How the tool accesses the math | **Approach A — extract a shared helper** (`compute_what_if`) that both the router endpoint and the tool call. Single source of truth, no HTTP-in-process, no duplicated math. |

## 3. Architecture

### 3.1 Backend

```
                     ┌────────────────────────────────────────────┐
                     │        compute_what_if(product_key,        │
                     │          benchmark_a_delta_pct, product_c_delta_pct,│
                     │          db)  ← NEW pure function          │
                     └───────────────▲────────────┬───────────────┘
                                     │            │
              router delegates       │            │ tool calls directly
              (endpoint kept)        │            │
   ┌─────────────────────────┐       │            │    ┌──────────────────────────────┐
   │ GET /forecast-ops/      │───────┘            └───▶│ forecast_what_if handler      │
   │   what-if/{product_key} │  (forecast_ops.py)      │ (forecast_tool.py)            │
   └─────────────────────────┘                         │  - product_id → headline SKU  │
                                                        │  - calls compute_what_if      │
                                                        │  - returns text+table dict    │
                                                        └───────────────▲──────────────┘
                                                                        │
                                       agent_router / tool_handlers ────┘
                          (forecast_agent, acme_bi_assistant, general_assistant)
```

- **Helper extraction**: the body of `forecast_ops.py:get_what_if_simulation` (lines 710–792) moves into a pure function `compute_what_if(product_key, benchmark_a_delta_pct, product_c_delta_pct, db)`. The router endpoint remains (still useful for dashboards/tests) but delegates to it.
- **New tool**: `forecast_what_if` in `backend/app/services/tool_handlers/forecast_tool.py`.
- **Product resolution**: the tool accepts `product_id` (dashboard product family, e.g. `product_a`) — consistent with `forecast_get` / `forecast_run`. Internally it resolves to the **headline SKU**: the first active `ForecastTarget` row for that family ordered by `report_order.asc()` with `is_deleted=False` and matching `org_id` — the same selection used by `forecast_list_skus` (`forecast_tool.py:394-398`). Its `product_key` (format `acme.<product_id>.<material_code>`) is then passed to `compute_what_if`.
- **No artifact, no chart** — the tool returns a plain dict; the agent narrates it as a short paragraph + markdown table.

### 3.2 Gating

`FORECASTING_ENABLED` exists in `backend/app/config.py:586` (default `False`); its comment states it is meant to gate forecast tool registration + nightly cron, but **as of 2026-08-22 no code path reads it**. Actual gating today is the registry's `enabled_by_default` + the per-agent tool lists:

- Register `forecast_what_if` in the forecast module (`forecast_tool.py`) exactly like the other lightweight reads (e.g. `forecast_list_skus`): `enabled_by_default=True`.
- The `try/except` block at `tool_handlers/__init__.py:156-159` is a **dependency guard** (missing xgboost/statsmodels must not break startup), not a flag gate — `forecast_what_if` joins that module import and inherits the guard.
- Add the tool name to `DEFAULT_TOOLS_BY_AGENT` for the 3 agents (see §6).
- No new flag. If `FORECASTING_ENABLED` is later wired into tool registration, this tool is covered by it.

## 4. Tool Contract

### 4.1 Schema (`FORECAST_WHAT_IF_SCHEMA`)

```
forecast_what_if(product_id, benchmark_a_delta_pct=0, product_c_delta_pct=0)

  product_id        str   required  — dashboard product family ('product_a', 'feedstock_a', 'product_b', ...)
  benchmark_a_delta_pct   float optional — Benchmark A price shock in percent (10 = +10%); default 0
  product_c_delta_pct float optional — Product_C price shock in percent (-3 = -3%); default 0
```

Rule: **at least one delta must be non-zero**; otherwise the tool returns `{success: False, error: "specify at least one price shock (benchmark_a_delta_pct or product_c_delta_pct)"}`.

### 4.2 Return shape (text + table, no artifact)

```python
{
  "success": True,
  "product_id": "product_a",
  "product_key": "acme.product_a.201000018",   # the SKU simulated
  "base_forecast":      [p0, p1, ..., p6],
  "adjusted_forecast":  [a0, a1, ..., a6],
  "total_impact_pct": 2.34,
  "drivers": [
    {"driver": "benchmark_a",   "delta_pct": 10.0, "impact_pct": 1.80, "note": "Benchmark A +10% -> product_c +8% -> +1.80%"},
    {"driver": "product_c", "delta_pct": 0.0,  "impact_pct": 0.00, "note": "no shock"}
  ],
  "horizon_table": [      # ready for the agent to render as a markdown table
    {"day": 1, "base": p0, "adjusted": a0, "delta_pct": ...},
    ...
    {"day": 7, "base": p6, "adjusted": a6, "delta_pct": ...}
  ],
  "narration_hint": "Benchmark A +10% -> product_a +2.34% on day 7 (X -> Y)."
}
```

The agent formats `narration_hint` + `horizon_table` into a short paragraph and a compact markdown table. No `Artifact` row is written.

## 5. Math Contract (kept as-is from the router)

- `compute_domain_signal_adjustment(product_id, as_of_date, product_c_pct_change)` from `app.services.forecasting.domain_signals` returns a dict containing a `causal_pct` key — the per-driver impact. The what-if math uses this.
- **Benchmark A is a 0.8-elasticity proxy to product_c** (existing simplification in `forecast_ops.py:763-778`): a Benchmark A shock is converted to a product_c-equivalent shock (`product_c_eff = benchmark_a_delta_pct * 0.8 + product_c_delta_pct`) before applying the domain-signal adjustment. **This is kept as-is; not extended in this task.**
- The total impact is applied to the day-7 base forecast, and the horizon table (`day 1..7`) shows base vs adjusted per horizon. The horizon conventions must match `format_run_answer` / `select_headline_day` from `app.services.forecasting.answer_format` for parity with the dashboard and the existing `forecast_get`/`forecast_run` tools.

## 6. Wiring

| File | Change |
|---|---|
| `backend/app/services/tool_registry.py` (`DEFAULT_TOOLS_BY_AGENT`, lines 409–621) | Add `"forecast_what_if"` to the tool lists of **`forecast_agent`**, **`acme_bi_assistant`**, and **`general_assistant`**. (general_assistant can call directly to avoid a sub-agent hop for a trivial read-only query.) |
| `backend/app/services/tool_handlers/forecast_tool.py` | Add `FORECAST_WHAT_IF_SCHEMA` after `FORECAST_BRIEF_SCHEMA` (schema def at line ~1008; tool-list registration tuple at lines 1088–1098, `enabled_by_default=True`). |
| `backend/app/services/tool_handlers/__init__.py:156-159` | The new tool registers when the forecast module imports successfully (dependency guard, not a flag gate — see §3.2). |
| `backend/app/services/agent_definitions/__init__.py:557-595` | forecast_agent TOOLS section: add a `forecast_what_if` bullet + usage rule. |
| `backend/app/services/agent_prompts.py` | acme_bi_assistant prompt: add a one-liner pointing to `forecast_what_if` for scenario questions (matches the existing delegation pattern). |
| `backend/app/services/intent_planner.py:236-238` | Add a `"what_if"` intent bucket → `["forecast_what_if", "ask_forecast"]` so natural-language "what if..." queries route correctly. |

### 6.1 System prompt text (forecast_agent)

```
- forecast_what_if(product_id, benchmark_a_delta_pct, product_c_delta_pct): simulate a forecast under
  upstream price shocks (Benchmark A, Product_C) and return base vs adjusted forecast per horizon.
  Call this for "what if ..." / "如果...涨...%会怎样" questions about price scenarios.
  Resolve product_id from conversation context, or ask the user which product.
  Do NOT call when both deltas are 0.
```

### 6.2 Intent bucket (intent_planner.py)

```python
"what_if": ["forecast_what_if", "ask_forecast"],
```

## 7. Frontend Cleanup

| File | Change |
|---|---|
| `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx:17` | Remove `import WhatIfPanel from './components/WhatIfPanel';` |
| `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx:203` | Remove the `<WhatIfPanel productKey={selectedProductId} lang={lang} />` JSX block. The sidebar slot collapses; existing `BacktestReplayChart` / `DriverWaterfallChart` stay untouched. |
| `frontend/src/api/marketDashboard.js:118-119` | Remove the `whatIf: (productKey, benchmark_aDelta, product_cDelta) => getJson(...)` method. |
| `frontend/src/api/marketDashboard.js:125` | Remove `export const getWhatIf = marketDashboardApi.whatIf;` (verified: only used by the panel). |
| `frontend/src/pages/MarketDashboard/components/WhatIfPanel.jsx` | **Delete the file.** |

Note: `getWhatIf` was verified this session as referenced only by `WhatIfPanel.jsx`, so removing it breaks nothing else.

## 8. Error Handling

| Condition | Return |
|---|---|
| No active SKU target for `product_id` | `{success: False, error: "no forecast target for '<product_id>'"}` |
| No latest forecast run | Mirror the router's existing "No forecast available for simulation" message. |
| Both deltas are 0 | `{success: False, error: "specify at least one price shock (benchmark_a_delta_pct or product_c_delta_pct)"}` |
| `compute_domain_signal_adjustment` raises | Caught + logged; return `{success: False, error: "simulation failed: <exc>"}` |
| Forecast module fails to import (missing xgboost/statsmodels) | Tool not registered (invisible to agents) — existing dependency-guard behavior, no extra handling needed. |

## 9. Testing Plan

New file: `backend/tests/services/tool_handlers/test_forecast_what_if.py`

| Case | Assertion |
|---|---|
| Happy path | Mock `compute_what_if` + SKU resolution; result contains `horizon_table` (7 rows) + `narration_hint` + `success: True`. |
| Both deltas zero | Returns error dict, `success: False`. |
| Unknown `product_id` | Returns `no forecast target for ...`, `success: False`. |
| Not registered on import failure | Assert the registry has no `forecast_what_if` entry when the forecast module fails to import (dependency guard). |

Regression: the existing forecast_ops what-if test (if any) is pointed at the extracted `compute_what_if` helper to prove no behavioral regression. No frontend test changes needed (panel removed).

## 10. Out of Scope

- **No chart artifact / inline chart rendering** (user chose text-only output).
- **No new what-if drivers** beyond Benchmark A + Product_C (the backend only supports these; extending drivers is a separate task).
- **No changes to `BacktestReplayChart`, `DriverWaterfallChart`, or the rest of the dashboard.**
- No new feature flags (gating follows the existing `enabled_by_default` + `DEFAULT_TOOLS_BY_AGENT` pattern; `FORECASTING_ENABLED` is not consumed by any code path today — see §3.2).
- Mobile variant untouched.

## 11. Deploy / Verification Notes (for implementer)

- **Backend**: bind-mount means Python changes are picked up by `docker restart zhanlu-backend` (no rebuild).
- **Frontend**: `./node_modules/.bin/vite build --emptyOutDir=false` (capture output to a log file and poll it — the environment intercepts `vite build` as a watch command), then `docker restart zhanlu-frontend`.
- Manual smoke test: ask the agent "如果基准A涨10%, product_a 会怎样?" and verify a text answer + 7-day comparison table; also verify the dashboard no longer shows the What-If panel. (The tool is visible because it is registered `enabled_by_default=True` and listed in `DEFAULT_TOOLS_BY_AGENT` — no flag flip required.)

## 12. File Manifest

**Modify**
- `backend/app/routers/forecast_ops.py` — extract `compute_what_if` helper; router delegates.
- `backend/app/services/tool_handlers/forecast_tool.py` — add `forecast_what_if` handler + `FORECAST_WHAT_IF_SCHEMA`.
- `backend/app/services/tool_registry.py` — register tool in 3 agents' `DEFAULT_TOOLS_BY_AGENT`.
- `backend/app/services/agent_definitions/__init__.py` — forecast_agent prompt text.
- `backend/app/services/agent_prompts.py` — acme_bi prompt one-liner.
- `backend/app/services/intent_planner.py` — `what_if` intent bucket.
- `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` — remove panel import + JSX.
- `frontend/src/api/marketDashboard.js` — remove `whatIf` / `getWhatIf`.

**Create**
- `backend/tests/services/tool_handlers/test_forecast_what_if.py`

**Delete**
- `frontend/src/pages/MarketDashboard/components/WhatIfPanel.jsx`
