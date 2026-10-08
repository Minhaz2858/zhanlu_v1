# Plan: Replace WhatIfPanel UI with `forecast_what_if` Agent Tool

> **Status:** Ready to execute. Approved spec: `docs/superpowers/specs/2026-08-22-forecast-what-if-agent-tool-design.md`.
> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Commit after every task.

## Goal

Replace the Market Dashboard **What-If Analysis sidebar panel** (sliders + Run Simulation) with a first-class agent tool `forecast_what_if` so users get scenario answers in chat, then delete the panel UI.

**Approved architecture:**
- Extract the what-if math from `GET /forecast-ops/what-if/{product_key}` into a shared pure helper `compute_what_if(...)` that BOTH the router endpoint and the agent tool call (single source of truth, no drift).
- New tool `forecast_what_if(product_id, benchmark_a_delta_pct=0, product_c_delta_pct=0)` → text + inline-table dict (no chart artifact, no `Artifact` DB write).
- Wire into 3 agents + intent planner; remove the frontend panel.

## Context & Key Facts (all verified this session)

- What-if math body: `forecast_ops.py:710-792` inside `get_what_if_simulation` (endpoint at `:681`). Uses `compute_domain_signal_adjustment(product_id, as_of_date, product_c_pct_change)` from `app.services.forecasting.domain_signals` (returns `causal_pct`). Benchmark A is a 0.8-elasticity proxy to product_c (`product_c_eff = benchmark_a_delta_pct * 0.8 + product_c_delta_pct`) — **keep as-is**, do not extend.
- Tool handler pattern to mirror: `_forecast_list_skus` (`forecast_tool.py:375-380`, `async def ... (args, db, user_id, context=None) -> dict`); headline-SKU selection mirrors `forecast_tool.py:394-398` (`product_key.like(f"acme.{product_id}.%")`, `org_id==`, `is_deleted==False`, `order_by(report_order.asc())`).
- Tool schema/registration pattern to mirror: `FORECAST_BRIEF_SCHEMA` (`forecast_tool.py:1008-1031`) + registration tuple at `forecast_tool.py:1038-1102` (entries: `(name, schema, handler, enabled, description)`).
- `_resolve_org_context(context)` returns `(org_id, _app_id)` — used by all forecast handlers.
- **CORRECTION to spec §6** — `DEFAULT_TOOLS_BY_AGENT` (`tool_registry.py:409-621`) contains ONLY `agent_builder`, `skill_agent`, `automation_agent`, `general_assistant`. It does NOT contain `forecast_agent`/`acme_bi_assistant`. Correct wiring points:
  - `acme_bi_assistant`: `AgentDefinition.tools=None` (all tools from DB `AgentApp.tool_config`); its list is seeded in `system_agents.py:443-455` (`_tools_in_registry(registry, ALL_TOOL_NAMES + [ask_*..., "forecast_brief"])`). Add `"forecast_what_if"` there — `ensure_system_agents()` refreshes existing rows on every startup, so no DB migration needed.
  - `forecast_agent`: `AgentDefinition.tools` list at `agent_definitions/__init__.py:1159-1170` (has `"forecast_brief"` at `:1169`). Add there.
  - `general_assistant`: TWO places — DB-row seed `system_agents.py:329-345` (enabled_tools list) AND fallback `DEFAULT_TOOLS_BY_AGENT["general_assistant"]` (`tool_registry.py:420-442`).
- **CORRECTION to spec §6** — the acme_bi_assistant prompt lives at `BI_ASSISTANT_PROMPT` in `agent_definitions/__init__.py:892` (NOT `agent_prompts.py`; `agent_prompts.py` only has `_BI_SCHEMA_HINT_BLOCK` at `:194`). `FORECAST_AGENT_PROMPT` TOOLS section at `agent_definitions/__init__.py:557-568`.
- Intent planner: `stage_to_tools` mapping at `intent_planner.py:233-241`. Add `"what_if"` key.
- Frontend verified: import `MarketDashboardPage.jsx:17`; JSX wrapper+panel `:202-204` (grid `lg:grid-cols-4`, chart `lg:col-span-3`, panel `lg:col-span-1`); API `whatIf` method `marketDashboard.js:117-119` + `getWhatIf` export `:125`. Nothing else imports them (grep confirmed).
- No existing what-if endpoint test (`test_forecast_ops_router.py` covers status + run-evaluation only).

## Global Constraints

- Mobile parity: `MobileChatPage.jsx` untouched.
- No new flag. Registration follows `enabled_by_default=True` + per-agent tool lists (`FORECASTING_ENABLED` at `config.py:586` is documented but read by no code path — out of scope).
- Tests (limited RAM — target specific files, NO parallel pytest): `cd backend && venv/bin/python -m pytest tests/test_forecast_what_if.py tests/test_what_if_helper.py tests/test_forecast_ops_router.py tests/test_intent_planner.py -v`.
- Build: `./node_modules/.bin/vite build --emptyOutDir=false` (capture to log + poll — the environment intercepts `vite build` as a watch command) + `docker restart zhanlu-frontend`. Backend: `docker restart zhanlu-backend` (bind-mount, no rebuild).
- Router/tool import errors are silent: verify with `docker exec zhanlu-backend python -c "from app.services.tool_handlers import forecast_tool"`.

---

## Task 1: Extract `compute_what_if` shared helper

**Files:**
- Create: `backend/app/services/forecasting/what_if.py`
- Modify: `backend/app/routers/forecast_ops.py:681-792`

**Interfaces:**
- New: `compute_what_if(product_key: str, benchmark_a_delta_pct: float, product_c_delta_pct: float, db) -> dict`
  - Moves lines 710-792 verbatim (math is **kept as-is** per spec §5).
  - Returns the same shape the endpoint returns today: `{product_key, base_forecast, adjusted_forecast, total_impact_pct, adjustments}`.
  - Raises `LookupError("no forecast target for '<product_key>'")` when no `ForecastTarget` matches.
- Endpoint `get_what_if_simulation` becomes a thin wrapper: parse params → call helper → map `LookupError` → `HTTPException(404, ...)`. (Keep endpoint — still used by dashboards/tests.)

- [ ] **Step 1: Write the failing test** — Create `backend/tests/test_what_if_helper.py` (flat, mirrors repo convention):
  - `test_compute_what_if_returns_dict_shape` — fake `ForecastTarget` row + monkeypatch `app.services.forecasting.what_if.compute_domain_signal_adjustment` to return `{"causal_pct": 2.34}`; assert all 5 keys present + `total_impact_pct == 2.34`.
  - `test_compute_what_if_raises_lookup_error_when_target_missing` — no rows → `pytest.raises(LookupError)`.
  - `test_compute_what_if_benchmark_a_proxy` — assert the helper calls `compute_domain_signal_adjustment` with `product_c_pct_change == benchmark_a*0.8 + product_c` (locks in the 0.8-elasticity proxy).
- [ ] **Step 2: Run — fails** (module `what_if.py` doesn't exist).
- [ ] **Step 3: Create `backend/app/services/forecasting/what_if.py`** — move the 710-792 body into `compute_what_if`; replace the endpoint's inline `HTTPException(404, ...)` for missing target with `raise LookupError(...)`. Keep `compute_domain_signal_adjustment` import module-local.
- [ ] **Step 4: Slim the endpoint in `forecast_ops.py`** — keep route decorator + param parsing; delegate to `compute_what_if`; wrap `LookupError` → `HTTPException(404, detail=str(exc))`. Remove now-unused local imports if safe.
- [ ] **Step 5: Add endpoint regression test** — in `test_forecast_ops_router.py`, add `test_what_if_endpoint_delegates_to_helper` (monkeypatch `app.routers.forecast_ops.compute_what_if` returning a canned dict; assert 200 + shape passthrough). Proves no behavioral regression.
- [ ] **Step 6: Run backend tests — pass** (3 helper + 1 router).
- [ ] **Step 7: Commit** — `git add backend/app/services/forecasting/what_if.py backend/app/routers/forecast_ops.py backend/tests/test_what_if_helper.py backend/tests/test_forecast_ops_router.py && git commit -m "refactor(forecast): extract compute_what_if helper shared by API + agent tool"`

---

## Task 2: Add `forecast_what_if` tool handler + registration

**Files:**
- Modify: `backend/app/services/tool_handlers/forecast_tool.py`

**Interfaces (per spec §4):**
- Schema `FORECAST_WHAT_IF_SCHEMA` (mirror `FORECAST_BRIEF_SCHEMA` at `:1008-1031`): `product_id` (str, required), `benchmark_a_delta_pct` (float, default 0), `product_c_delta_pct` (float, default 0). Description: simulate a forecast under upstream price shocks (Benchmark A/Product_C) — use for "what if"/scenario questions.
- Handler `async def _forecast_what_if(args, db, user_id, context=None) -> dict`:
  1. `product_id = (args.get("product_id") or "").strip().lower()`; empty → `{"success": False, "error": "no forecast target for '<product_id>'"}` (product_id empty → same message).
  2. `benchmark_a_delta_pct = float(args.get("benchmark_a_delta_pct") or 0.0)`; `product_c_delta_pct = float(args.get("product_c_delta_pct") or 0.0)`; wrap `float()` in try → `{"success": False, "error": "simulation failed: <exc>"}`.
  3. **Both deltas 0** → `{"success": False, "error": "specify at least one price shock (benchmark_a_delta_pct or product_c_delta_pct)"}` (spec §8).
  4. `org_id, _ = _resolve_org_context(context)`.
  5. Headline SKU (mirror `:394-398`): `db.query(ForecastTarget).filter(product_key.like(f"acme.{product_id}.%"), org_id==org_id, is_deleted==False).order_by(report_order.asc()).first()`. None → `{"success": False, "error": "no forecast target for '<product_id>'"}`.
  6. `try: result = compute_what_if(target.product_key, benchmark_a_delta_pct, product_c_delta_pct, db) except LookupError as e: → {"success": False, "error": str(e)} except Exception as e: log + {"success": False, "error": f"simulation failed: {e}"}` (spec §8).
  7. If `result.get("adjusted_forecast")` empty → mirror router's `{"success": False, "error": "No forecast available for simulation"}`.
  8. Build spec §4.2 return: `drivers` (normalize `result["adjustments"]` → `[{"driver", "delta_pct", "impact_pct", "note"}]`; include both benchmark_a + product_c entries even when one is zero — "no shock" note), `horizon_table` (7 rows `{day: 1..7, base, adjusted, delta_pct}` from the 7 base/adjusted points, `delta_pct = (a-b)/b*100` guarded for b==0 → None), `narration_hint` = `"<Driver> <sign>% -> {product_id} <total_impact>% on day 7 (X -> Y)."` using the day-7 headline (per `answer_format.select_headline_day` convention).
  9. Return `{"success": True, "product_id": product_id, "product_key": target.product_key, "base_forecast": [...], "adjusted_forecast": [...], "total_impact_pct": ..., "drivers": [...], "horizon_table": [...], "narration_hint": "..."}`.
- Register in the tuple list at `:1038-1102`:
  ```python
  ("forecast_what_if", FORECAST_WHAT_IF_SCHEMA, _forecast_what_if, True,
   "Simulate a forecast under upstream price shocks (Benchmark A/Product_C) for scenario analysis"),
  ```
- Add `from app.services.forecasting.what_if import compute_what_if` at top.

- [x] **Step 1: Write the failing test** — Create `backend/tests/test_forecast_what_if.py` (flat; mirrors `test_forecast_brief_tool.py` style — `asyncio.run(forecast_tool._forecast_what_if(args, db=None, user_id=None, context=None))`, monkeypatch-based):
  - `test_what_if_happy_path` — patch `_resolve_org_context` → `("default-org", "default-app")`, patch `compute_what_if` (imported name in `forecast_tool`) → canned dict with 7 base/adjusted points; fake `ForecastTarget` returned by patched `db.query` chain. Assert: `success is True`, `horizon_table` has exactly 7 rows (day 1..7), `narration_hint` non-empty, `drivers` has 2 entries.
  - `test_what_if_both_deltas_zero` — assert error message matches spec §8.
  - `test_what_if_unknown_product` — no target row → `success is False`, `"no forecast target for"` in error.
  - `test_what_if_no_forecast_data` — `compute_what_if` returns empty `adjusted_forecast` → `"No forecast available for simulation"`.
  - `test_what_if_simulation_exception` — `compute_what_if` raises `RuntimeError("boom")` → `success is False`, `"simulation failed"` in error (no raise).
  - `test_registered_in_registry` — `from app.services.tool_registry import registry; assert registry.get_handler("forecast_what_if") is not None` (mirrors `test_forecast_brief_registered`).
- [x] **Step 2: Run — fails** (handler/schema not defined; also 7 tests were blocked at collection by `engine.py` importing deleted `explain_forecast` — fixed by restoring `_compute_summary`+`explain_forecast` in `explain.py`).
- [x] **Step 3: Implement** per interfaces above (note: "both deltas zero" check fires only when a delta key is explicitly present — required by `test_what_if_unknown_product`).
- [x] **Step 4: Run — pass** (`venv/bin/python -m pytest tests/test_forecast_what_if.py -v` → 7/7; regression batch 25/25 incl. `test_forecast_engine_as_of.py` previously blocked by the engine import).
- [x] **Step 5: Verify import in container** — `docker exec zhanlu-backend python -c "from app.services.tool_handlers import forecast_tool; from app.services.tool_registry import registry; assert registry.get_handler('forecast_what_if')"` → CONTAINER_OK.
- [ ] **Step 6: Commit** — DEFERRED (user declined WIP-commit decision earlier; files staged-ready: `backend/app/services/tool_handlers/forecast_tool.py backend/tests/test_forecast_what_if.py backend/app/services/forecasting/explain.py`). Commit cmd: `git add ... && git commit -m "feat(forecast): add forecast_what_if agent tool for scenario analysis"`

---

## Task 3: Expose the tool on the three agents

**Files (corrected from spec §6 — see Context & Key Facts):**
- Modify: `backend/app/services/system_agents.py`
- Modify: `backend/app/services/tool_registry.py`
- Modify: `backend/app/services/agent_definitions/__init__.py`

- [ ] **Step 1: `acme_bi_assistant`** — in `system_agents.py:443-455`, add `"forecast_what_if"` right after `"forecast_brief"` inside the `_tools_in_registry(registry, [...])` call. `ensure_system_agents` refreshes the DB row on startup — verify after restart.
- [ ] **Step 2: `forecast_agent`** — in `agent_definitions/__init__.py:1159-1170`, add `"forecast_what_if"` after `"forecast_brief"` in `AgentDefinition.tools`.
- [ ] **Step 3: `general_assistant` (DB row)** — in `system_agents.py:329-345`, add `"forecast_what_if"` to the `enabled_tools` list (near `"ask_data_agent"`).
- [ ] **Step 4: `general_assistant` (fallback)** — in `tool_registry.py:420-442`, add `"forecast_what_if"` to `DEFAULT_TOOLS_BY_AGENT["general_assistant"]`.
- [ ] **Step 5: Verify** — `docker restart zhanlu-backend`; then `docker exec zhanlu-backend python -c "from app.services.tool_registry import DEFAULT_TOOLS_BY_AGENT; print('forecast_what_if' in DEFAULT_TOOLS_BY_AGENT['general_assistant'])"` → True; and a DB snippet confirming the AgentApp rows' `enabled_tools` include `forecast_what_if` for both `acme_bi_assistant` and `general_assistant`.
- [ ] **Step 6: Commit** — `git add backend/app/services/system_agents.py backend/app/services/tool_registry.py backend/app/services/agent_definitions/__init__.py && git commit -m "feat(agents): expose forecast_what_if on acme_bi/forecast/general agents"`

---

## Task 4: Prompt updates (agent knows when to call the tool)

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py` (prompt constants live here, NOT `agent_prompts.py` — corrected from spec §6)

**Interfaces:** Text-only additions.

- [ ] **Step 1: `FORECAST_AGENT_PROMPT` TOOLS section (`:557-568`)** — after the `forecast_brief` bullet, add (verbatim from spec §6.1):
  ```
  - forecast_what_if(product_id, benchmark_a_delta_pct, product_c_delta_pct): simulate a forecast under
    upstream price shocks (Benchmark A, Product_C) and return base vs adjusted forecast per horizon.
    Call this for "what if ..." / "如果...涨...%会怎样" questions about price scenarios.
    Resolve product_id from conversation context, or ask the user which product.
    Do NOT call when both deltas are 0.
  ```
- [ ] **Step 2: `BI_ASSISTANT_PROMPT` (`:892+`)** — add one line to the routing/FAST-PATH section (near the forecast tool rules, ~`:965-991`):
  ```
  - "what if" / "如果...涨...%" scenario questions → forecast_what_if (product_id + price deltas).
  ```
- [ ] **Step 3: Verify no syntax break** — `docker exec zhanlu-backend python -c "from app.services.agent_definitions import get_agent_definition"`.
- [ ] **Step 4: Commit** — `git add backend/app/services/agent_definitions/__init__.py && git commit -m "feat(prompts): teach forecast agents when to call forecast_what_if"`

---

## Task 5: Intent planner `"what_if"` bucket

**Files:**
- Modify: `backend/app/services/intent_planner.py:233-241`

**Interfaces:** `stage_to_tools` mapping gains a `"what_if"` key (spec §6.2).

- [ ] **Step 1: Add the bucket** — after the `"forecast"` entry, add:
  ```python
  "what_if": ["forecast_what_if", "ask_forecast"],
  ```
- [ ] **Step 2: Add a test** — find `backend/tests/test_intent_planner.py` (or create if absent, mirroring repo flat-test convention): `assert "what_if" in stage_to_tools` and `"forecast_what_if" in stage_to_tools["what_if"]`.
- [ ] **Step 3: Run the planner test — pass**
- [ ] **Step 4: Commit** — `git add backend/app/services/intent_planner.py backend/tests/test_intent_planner.py && git commit -m "feat(planner): route what-if/scenario questions to forecast_what_if"`

---

## Task 6: Remove the WhatIfPanel UI (frontend)

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Modify: `frontend/src/api/marketDashboard.js`
- Delete: `frontend/src/pages/MarketDashboard/components/WhatIfPanel.jsx`

**Interfaces:** Remove `marketDashboardApi.whatIf` + `getWhatIf` export (verified: only `WhatIfPanel.jsx` consumes them). `BacktestReplayChart` / `DriverWaterfallChart` stay untouched.

- [ ] **Step 1: Remove the import** — delete line 17 (`import WhatIfPanel from './components/WhatIfPanel';`).
- [ ] **Step 2: Remove the JSX + widen the chart** — replace the grid block containing the panel:
  ```jsx
  <div className="grid grid-cols-1 lg:grid-cols-4 gap-0">
    <div className="lg:col-span-3">
      <ForecastTrendChart productId={selectedProductId} productName={productName} language={lang} />
    </div>
    <div className="lg:col-span-1 border-t lg:border-t-0 lg:border-l border-slate-100 dark:border-slate-800 px-4 py-4">
      <WhatIfPanel productKey={selectedProductId} lang={lang} />
    </div>
  </div>
  ```
  with a single full-width chart (sidebar slot collapses — no empty 1/4 gutter):
  ```jsx
  <div className="grid grid-cols-1">
    <ForecastTrendChart productId={selectedProductId} productName={productName} language={lang} />
  </div>
  ```
- [ ] **Step 3: Remove the API client method** — delete lines 117-119 (`whatIf: ...`) and line 125 (`export const getWhatIf = marketDashboardApi.whatIf;`).
- [ ] **Step 4: Delete the panel file** — `git rm frontend/src/pages/MarketDashboard/components/WhatIfPanel.jsx`.
- [ ] **Step 5: Grep for stragglers** — `grep -rn "WhatIfPanel\|getWhatIf\|\.whatIf" frontend/src` → no matches.
- [ ] **Step 6: Frontend tests** — `cd frontend && npx vitest run src/pages/MarketDashboard/MarketDashboardPage.test.jsx`. Note: 2 pre-existing failures (EN vs ZH text) are expected and unrelated — do NOT fix in this task.
- [ ] **Step 7: Build + restart** — `cd frontend && ./node_modules/.bin/vite build --emptyOutDir=false > /tmp/vite-build.log 2>&1 &` then poll `/tmp/vite-build.log` until done; `docker restart zhanlu-frontend`.
- [ ] **Step 8: Commit** — `git add -A && git commit -m "feat(ui): remove WhatIfPanel, move scenario analysis to chat agent tool"`

---

## Task 7: Manual verification (smoke test)

- [ ] **Step 1: Restart backend** — `docker restart zhanlu-backend`.
- [ ] **Step 2: Market Dashboard (`/market-dashboard/upstream`)** — right sidebar GONE, chart spans full width, no console errors.
- [ ] **Step 3: Acme BI chat (`acme_bi_assistant`)** — ask `"如果布伦特涨10%, product_a 会怎样?"` → agent calls `forecast_what_if`; verify a tool step shows `product_id`/base/adjusted/total_impact; final answer = short paragraph + 7-day markdown table (spec §11).
- [ ] **Step 4: General chat** — same question on a default project → `general_assistant` calls the tool directly; confirm no tool-not-found error.
- [ ] **Step 5: Negative cases** — `"what if product_c -5%?"` with no product → agent asks for clarification or returns the spec §8 error (never 500s); both-deltas-zero returns the error string.
- [ ] **Step 6: Backend logs** — `docker logs zhanlu-backend --tail 50` shows tool dispatch, no tracebacks.

---

## Acceptance Criteria (Definition of Done)

- [ ] `compute_what_if` unit tests pass; router endpoint still returns the identical shape (regression test in Task 1 Step 5).
- [ ] `forecast_what_if` handler tests pass (7 cases); tool visible via `registry.get_handler("forecast_what_if")`.
- [ ] Tool exposed on `acme_bi_assistant`, `forecast_agent`, and both `general_assistant` paths (DB row + `DEFAULT_TOOLS_BY_AGENT` fallback).
- [ ] `"what_if"` intent bucket routes to the tool.
- [ ] WhatIfPanel fully removed (import, JSX, API client, file) — grep clean.
- [ ] Market Dashboard renders full-width chart, no sidebar gutter, no console errors.
- [ ] Live chat smoke test: what-if question answered conversationally with base/adjusted numbers + 7-day table.
- [ ] Backend + frontend rebuilt; no silent tool_handlers/router import errors.

## Risks & Mitigations

- **Stale spec citations** (DEFAULT_TOOLS_BY_AGENT for forecast_agent/acme_bi_assistant; prompt location; test path) → corrected in this plan using verified locations; spec file kept as-is.
- **DB tool_config staleness** → `ensure_system_agents` refreshes `enabled_tools` every startup; verify via DB snippet in Task 3 Step 5.
- **`float()` ValueError in handler** → explicit guard returning `simulation failed: <exc>` (tested).
- **`horizon_table` day-7 headline** must match `answer_format.select_headline_day` convention → narration uses day 7, table rows day 1..7.
- **Frontend pre-existing test failures** (EN vs ZH) → out of scope, documented.
- **Vite build intercepted as watch** → run `./node_modules/.bin/vite build --emptyOutDir=false` with output redirected to a log and poll it.

## Out of Scope

- `MobileChatPage.jsx` (mobile parity — untouched).
- Chart artifact / inline chart rendering for what-if (spec §10).
- New what-if drivers beyond Benchmark A + Product_C.
- `BacktestReplayChart`, `DriverWaterfallChart`, rest of the dashboard.
- `FORECASTING_ENABLED` wiring (no code path reads it today — spec §3.2).
- Fixing the 2 pre-existing EN-vs-ZH frontend test failures.
