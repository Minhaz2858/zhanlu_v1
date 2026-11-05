# P0 Implementation Progress Log — 2026-10-31

**Objective:** make the 7 manufacturing agents genuinely operational (roadmap P0). **No new agents, no new orchestrator, no rebuilds** — per directive.

## Phase 1 — Collaboration fixes (all flag-gated, verified)

| Fix | Where | Flag (default) |
|---|---|---|
| ① Publish gate | Manufacturing agents set `status="published"` (delegation directory visibility, `delegate_tool.py:662`) | n/a (data) |
| ② AgentApp-aware delegation | Child prompt+toolset resolved from the AgentApp (`delegate_tool.py:297-306`, `:407`) | `DELEGATE_AGENTAPP_AWARE` (false) |
| ③ Binding inheritance | Child ctx carries parent `conversation_id` + `bound_kb_ids`; child identity NOT inherited (privilege leak guard) (`delegate_tool.py:422-437`) | `DELEGATE_INHERIT_BINDINGS` (false) |
| ⑤ Invocation linking | Delegation `record_invocation(conversation_id=parent)` (`delegate_tool.py:322-325`) | n/a |
| ④ Risk tiers | PO-drafting agents: `policy_profile` unchanged (low→medium deferred — see "Deferred") | n/a |

Both flags enabled in the deployment `.env` (rollback = flip off). Tests: `tests/test_delegate_agentapp_aware.py`.

## Phase 2 — Pilot: Demand Forecasting Agent

- Sandbox: `zhanlu_sandbox_auto` (postgres, 5 tables; seed script `scripts/seed_pilot_sandbox.py`), KB `Auto Sales Sandbox (Pilot)` → catalog `ready`, **live forecast discovery: 4 candidates** (36 monthly points, 98.5% coverage).
- Ontology: 4 objects (`VehicleModel`, `Dealer`, `SalesOrder`, `MonthlySales`) **human-bound via PATCH /ontology/objects/{id}/bind** → evidence `verified` (design-correct flow); agent domain binding `read`.
- **G1 PASS**: "highest 2025 revenue" → Falcon SUV ¥332,538,514.00 — **all four figures in the answer match direct SQL exactly** (Falcon/Aurora × 2024/2025).
- **R1 PASS**: HR question → "This data is not available in the bound sources…" (availability-first refusal; the scope-refusal rule was added to the seed prompts after the first attempt led with a misleading "zero employees").
- Runtime proof: production v3 SSE; `tool_progress` events; invocation rows `route=react` linked to the conversation (observability evidence).

**Bugs found & fixed (all with regression tests):**

| Bug | Root cause | Fix | Test |
|---|---|---|---|
| Forecast discovery found 0 candidates on postgres | `_quote` hardcoded MySQL backticks; `_time_bucket_expr` emitted `DATE_TRUNC` on MySQL | dialect-aware quoting/bucketing threaded from KB `db_type` | `test_forecast_discovery_dialect.py` |
| Any agent with `universal_*` tools 400'd on DashScope | 6 tools registered **bare nameless parameter schemas**; + 2 flat-form tools | envelope wrap at registration; `tool_schema_sanitize.py` (nested bare objects + flat forms) wired into v2, v3, delegate payloads | `test_tool_registry_envelopes.py` (all-200-tools invariant), `test_tool_schema_sanitize.py` |
| Scope-refusal led with "zero" | prompt rule gap | SCOPE REFUSAL rule in `GROUNDING_RULES` (seed script) | golden R1 |
| `SYNEXIA_FSM_ENABLED=true` in deployment vs documented default | env drift; also FSM turns dropped answers during debugging | deployment env set false (documented default; production = ReAct); docstrings made truthful | — |
| Pre-existing: `fake_prompt` mock missing `db=` kwarg | prior uncommitted `_ask_data_agent` change | test mock updated | nl2sql planner suite green |

## Phase 3 — Publish, golden eval, rollout

- `scripts/publish_manufacturing_agents.py`: 7 agents **published** + sandbox KB bound + `llm_model_id` pinned to the funded catalog row.
- **Golden suite** `tests/test_manufacturing_agent_golden.py`: unit tier (fixture↔seed consistency) green in CI; live tier (env `PILOT_E2E=1`) drives production v3 — G1/R1 **passed in the first full run**; observability test polls for the post-disconnect commit.
- **Rollout smoke** `scripts/smoke_manufacturing_agents.py` (ground truth: 144 orders 2025, ¥652,434,616.40, 4 dealers): **5/6 PASS** grounded (Predictive Maintenance, Quality Control, Supply Chain, Production Scheduling, Inventory). Autonomous Vehicle Systems: mechanism identical, blocked by provider quota (below).

## Phase 4 — Hygiene

- Deleted dead `ensureAgentApp` (frontend, zero importers); FSM docstrings made truthful; payload diagnostic downgraded to debug; malformed-tool warning retained.

## BLOCKER (external, needs account action)

**Both DashScope accounts are quota-exhausted** (`FreeTierOnly` / `insufficient_quota` 429). The keyed `deepseek-v4.1-flash` and `glm-5.3` catalog rows are all `is_deleted=true`. Consequence: live-tier reruns and the AV smoke fail with empty/truncated answers — a billing matter, not a platform defect. **Action needed:** fund a DashScope account (or add a funded provider key as an active `llm_models` row), then rerun:
`docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python /app/scripts/smoke_manufacturing_agents.py Autonomous`
and `PILOT_E2E=1 pytest tests/test_manufacturing_agent_golden.py`.

## Deferred (recorded, not forgotten)

- Risk-tier `medium` for PO-drafting agents (Supply Chain, Inventory) — requires a product decision on confirmation UX; enforcement already exists.
- `SYNEXIA` data-bound override: routing log prints before the flag check (cosmetic); the override correctly honors `is_fsm_enabled()`.
- Org default model still points at the exhausted qwen3.7-flash catalog row — all OTHER agents are affected by the same quota blocker.

## Regression status at close

- P0 set: 36 passed, 3 skipped (live tier)
- `tests/nl2sql`: 208 passed (1 pre-existing failure fixed)
- Ontology/session/RLS suites: green
