# Ecisco BI — Forecasting End-to-End with Truth-Anchored Answers

- **Date:** 2026-07-31
- **Status:** Draft (pending review)
- **Author:** brainstorming session (superpowers)
- **Related prior work:** `2026-07-30` forecasting debug session; partially-ported `app/services/forecasting/` and `app/services/tool_handlers/forecast_tool.py` from `EDIA_5.1.2/backend/src/service/`

## 1. Context & Goal

A user reported that the Ecisco BI project chat agent could not answer forecasting questions truthfully. Investigation revealed three layered issues:

1. The `ecisco_c5c9_v1` forecast pack references a `monthly_prices` table that does not exist in the warehouse.
2. The heavy forecast tools (`forecast_run`, `forecast_discover`) are gated at the registry level with `enabled_by_default=False`, so neither the user-facing chat LLM nor the `forecast_agent` subagent can call them.
3. No mechanism forces the LLM to show the source data behind a forecast, so a partially-working forecast path (which returned "feasibility snapshot" for LZ001) looked complete but was ungrounded.

**Goal:** A user of the Ecisco BI project can ask a forecasting question in chat, the orchestrator delegates to the `forecast_agent` subagent, the subagent calls the heavy forecast tools, and the response is a truth-anchored forecast (or an explicit insufficient-data verdict) — never a hallucinated number.

### Decisions (from brainstorming Q&A)

| Question | Decision |
|---|---|
| Scope | **Forecasting end-to-end** (smallest viable; not full EDIA rebuild, not methodology refit) |
| Data source | **Discover existing warehouse** — repoint pack to whatever the diagnose script finds, no new data loading |
| Tool access | **Enable heavy tools in the registry** so the existing `forecast_agent` subagent (already defined) can use them directly |
| Approach | **B (truth guarantees)** — data-anchor + insufficient-data verdict + structured response shape |
| Architecture | **Use existing subagent wiring** — `forecast_agent` is already defined at `agent_definitions/__init__.py:782` with all 5 forecast tools; `orchestrator_agent` already has `ask_forecast_agent` delegation |

## 2. Architecture

The Ecisco BI project already defines the subagent wiring we need:

- `forecast_agent` (line 782) — specialist subagent with `FORECAST_AGENT_PROMPT` (line 432) which already contains the **Honesty Gate** rules and an **Autonomy Contract**.
- `orchestrator_agent` (line 822) — entry point with `ask_forecast_agent` delegation tool (line 835).
- `report_agent` (line 803) — downstream of forecast_agent for report/PPT outputs.

**Nothing in the agent graph changes.** What changes:

1. The registry-level `enabled_by_default=False` for `forecast_run` and `forecast_discover` is flipped to `True` so the subagent's listed tools actually work.
2. The pack's `table` field is repointed to a real warehouse table.
3. A thin `truth_gate` module wraps the engine's return value, attaching a `data_anchor` (source rows) and converting thin-data cases to an explicit `insufficient_data` verdict.

### Data flow (success path)

```
User: "Predict LZ001's price for the next 7 days"
   │
   ▼
orchestrator_agent (intent: forecast_only)
   │  ask_forecast_agent(product="LZ001")
   ▼
forecast_agent (subagent, FORECAST_AGENT_PROMPT)
   │  tool call: forecast_run(target_id="<resolved>")
   ▼
forecast_tool._forecast_run
   │  engine.compute_target(...)
   ▼
ForecastEngine → truth_gate.wrap_forecast_result(...)
   │  checks sample_size, attaches data_anchor
   ▼
return wrapped payload to forecast_agent
   │  LLM formats prose citing data_anchor
   ▼
back to orchestrator_agent → user
```

### Data flow (insufficient-data path)

Same as above, but `truth_gate` returns:

```json
{
  "success": false,
  "reason": "insufficient_data",
  "source_table": "actual_price",
  "sample_size": 3,
  "message": "Need ≥5 data points in actual_price; found 3. Cannot produce a reliable forecast."
}
```

The subagent's `FORECAST_AGENT_PROMPT` Honesty Gate rules (line 448-454) already require it to surface this kind of failure — we just need to ensure the verdict is *reached* in the first place.

## 3. Components

Five files touched, one new module, no new dependencies.

| # | File | Change |
|---|------|--------|
| 1 | `app/services/tool_handlers/forecast_tool.py` | Flip `_enabled` from `False` → `True` for `forecast_discover` (line 754) and `forecast_run` (line 761) — 2 lines |
| 2 | `app/services/forecasting/packs/ecisco_c5c9_v1.json` | Repoint `"table": "monthly_prices"` → real table the diagnose script finds — 1 field |
| 3 | `app/services/forecasting/truth_gate.py` | **NEW** — `wrap_forecast_result(...)` function and `TruthGateConfig` dataclass, ~60 lines |
| 4 | `app/services/forecasting/engine.py` | Wrap `compute_target()` and `compute_all()` return values with `truth_gate.wrap_forecast_result(...)` — ~10 lines |
| 5 | `tests/test_forecasting_truth_gate.py` | **NEW** — unit tests + integration tests + end-to-end acceptance test, ~120 lines |

### `truth_gate.py` interface

```python
@dataclass
class TruthGateConfig:
    min_sample_size: int = 5            # below → "insufficient_data"
    max_anchor_rows: int = 10           # rows shown to LLM in the data_anchor
    confidence_threshold: float = 0.5   # below → "low_confidence" warning


def wrap_forecast_result(
    raw_runs: list[dict],
    anchor_rows: list[dict],         # first/last N rows from the warehouse
    source_table: str,
    sample_size: int,
    config: TruthGateConfig = TruthGateConfig(),
) -> dict:
    """Post-process a forecast engine result into a truth-gated response."""
```

Returns one of two shapes:

**Success** (sample_size ≥ min_sample_size):
```json
{
  "success": true,
  "data_anchor": {
    "source_table": "actual_price",
    "sample_size": 365,
    "first_5": [{"date": "...", "value": 5400}, ...],
    "last_5":  [{"date": "...", "value": 5757}, ...]
  },
  "runs": [...]
}
```

**Failure** (sample_size < min_sample_size):
```json
{
  "success": false,
  "reason": "insufficient_data",
  "source_table": "actual_price",
  "sample_size": 3,
  "message": "Need ≥5 data points in actual_price; found 3. Cannot produce a reliable forecast."
}
```

## 4. Error Handling

| # | Failure | Where caught | User-visible behavior |
|---|---------|--------------|----------------------|
| 1 | Empty warehouse (0 rows) | `truth_gate` — sample check | "No historical data found. Connect a data source or load the EDIA CSV first." |
| 2 | Thin warehouse (1–4 rows) | `truth_gate` — sample check | Same as #1, with `sample_size` shown |
| 3 | Registry gating still on | Tests + smoke test | Test suite fails before deploy |
| 4 | Pack points to non-existent table | `ForecastEngine` query fails | Engine returns `success=False, error="table not found"`. Subagent surfaces it. **Diagnose before the design lands.** |
| 5 | Engine crash (statsmodels missing, NaN) | Existing try/except in `forecast_tool._forecast_run` | `{"success": false, "error": str(exc)}` envelope preserved |
| 6 | `below_naive_baseline=True` | Engine computes; `runs[i].below_naive_baseline=True` | Subagent **must** surface per Honesty Gate rule. |
| 7 | Product with no `ForecastTarget` row | `forecast_run` returns "ForecastTarget 'X' not found" (existing) | Subagent: "No target registered. Run `forecast_discover` first." |
| 8 | LLM ignores insufficient_data verdict | Tested explicitly — assertion: response does not contain numeric forecast when verdict is `insufficient_data` | If violated, test fails → strengthen prompt rule. |
| 9 | Cross-org data leakage | Existing `engine.compute_target(org_id, ...)` filter | Not a new risk. |

## 5. Testing

### Layer 1 — Unit tests for `truth_gate.py`

```python
def test_wrap_returns_insufficient_data_when_empty()
def test_wrap_returns_insufficient_data_when_thin(n=1,2,3,4)
def test_wrap_returns_success_at_min_threshold(n=5)
def test_wrap_includes_first_and_last_anchor_rows()
def test_wrap_passes_through_runs_unchanged()
def test_wrap_does_not_leak_anchor_when_failure()
def test_config_min_sample_size_is_honored()
```

7 tests, no DB, <1s.

### Layer 2 — Integration tests for engine + truth-gate

```python
def test_engine_with_empty_db_returns_insufficient_data(db_session)
def test_engine_with_three_rows_returns_insufficient_data(db_session)
def test_engine_with_thirty_rows_returns_success_with_anchor(db_session)
def test_engine_preserves_below_naive_baseline_flag(db_session)
def test_pack_with_real_table_loads_without_error()
def test_pack_with_missing_table_raises_clear_error()
```

6 tests, in-memory SQLite, ~3s.

### Layer 3 — End-to-end acceptance test

```python
def test_user_forecast_question_returns_data_grounded_answer(test_client, mock_llm)
```

Sends `POST /api/chat` with "predict LZ001 next 7 days", stubs the LLM to echo the tool result faithfully. Asserts the response is one of:
- `value + source_table + sample_size + methodology` (success path), **OR**
- `reason: "insufficient_data"` (failure path)

Never both. Never neither.

### Layer 4 — Smoke test (manual, not CI)

```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -c "
from app.services.tool_registry import registry
assert registry.get('forecast_run').enabled_by_default
assert registry.get('forecast_discover').enabled_by_default
print('OK: heavy forecast tools enabled')
"
```

### Acceptance checklist (user verifies in Ecisco BI chat)

| # | Question | Expected response |
|---|---------|-------------------|
| 1 | "Predict LZ001's price for the next 7 days" | Forecast + `source_table` + `sample_size` + methodology, OR `insufficient_data` with `sample_size` shown |
| 2 | "Show me forecast accuracy for LZ001" | MAPE and naive comparison from `forecast_accuracy` tool |
| 3 | "What forecast targets are registered?" | Active target list from `forecast_get`, or "none yet — run forecast_discover" |

## 6. Out of Scope (Explicit Non-Goals)

- Loading the EDIA CSV into the warehouse (user chose "discover existing" not "load CSV")
- Porting `forecasting_service.py`, `forecast_supervisor.py`, `forecast_snapshot_adapter.py`, `forecast_policy_service.py`, `diagnosis_service.py` (Approach C territory)
- Caching or warming strategies
- LLM jailbreak prevention (Honesty Gate is a prompt rule, not a hard guard)
- Drift detection between warehouse and pack config
- Cross-product forecasting in one tool call

## 7. Pre-flight Discovery Step

Before this design lands, one diagnostic must run to determine what `ecisco_c5c9_v1.json` should point at. The `scripts/diagnose_warehouse_data.py` script from the prior session (blocked on ask mode) needs to be executed in craft mode to:

1. List all tables/views in `aipdp_data_warehouse_prod`
2. Sample `actual_price`, `v_actual_price`, `v_lz_data`, `v_forecast_price`, `v_partner_forecast_price`, `v_market_data`, `monthly_prices` for row count, date range, distinct products
3. Scan all tables for forecastability (date + numeric cols)
4. Check zhanlu forecast cache state (`ForecastTarget`, `ForecastRun`)

Three predicted outcomes drive the pack-repoint step:

- **A:** `actual_price` has many dates → repoint pack to `actual_price`
- **B:** Only `v_lz_data` has dates → repoint pack to `v_lz_data`, scope pack to LZ-prefixed product keys only
- **C:** All tables have 1 date or 0 rows → cannot ship without first loading EDIA CSV (escalate to user, possibly reopen scope)

## 8. Risks

| Risk | Mitigation |
|---|---|
| Pack repoint picks wrong table and engine returns no rows | Pre-flight discovery step (Section 7) checks before code lands |
| LLM ignores Honesty Gate and fabricates when `insufficient_data` is returned | Layer 3 test asserts no numeric forecast in the failure-path response; if test fails, tighten prompt |
| Enabling `forecast_run` for chat creates a new perf vector | Engine is already used by other paths; the iteration budget in the agent runtime (`max_turns=15`) bounds it |
| `below_naive_baseline=True` cases are uncomfortable to show users | Honesty Gate already requires surfacing; we add test coverage for this path so we know it works |
| Real DB rows have date formats the engine can't parse | Existing engine handles pandas date parsing; pre-flight check confirms sample_size is what we expect |

## 9. Rollout

1. **Pre-flight (in craft mode, 30 min):** Run `diagnose_warehouse_data.py`. Identify which warehouse table to repoint to. Capture result in this spec.
2. **Code change #1 (1 hour):** Repoint `ecisco_c5c9_v1.json` based on pre-flight result. Validate pack loads.
3. **Code change #2 (1 hour):** Create `truth_gate.py`.
4. **Code change #3 (1 hour):** Wrap `engine.compute_target()` and `engine.compute_all()` return values with `truth_gate.wrap_forecast_result(...)`.
5. **Code change #4 (15 min):** Flip `enabled_by_default` for `forecast_run` and `forecast_discover` in `forecast_tool.py`.
6. **Tests (2 hours):** Write Layer 1, 2, 3 tests. Run all. Iterate until green.
7. **Smoke (15 min):** Run the registry-gating smoke check.
8. **Acceptance (15 min):** Run the 3 manual questions in Ecisco BI chat.

Total: ~6 hours of focused work. Single PR.
