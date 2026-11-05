# P0 Phase 2 — Pilot: Demand Forecasting Agent on a Sandbox Enterprise DB

**Goal:** Prove one manufacturing agent end-to-end on real runtime: sandbox DB → KB connection → catalog discovery → ontology mapping → agent binding → real NL questions → grounded answers + honest refusals → observability evidence.

**Pilot:** Demand Forecasting Agent (id `f0261dee-393a-4abc-8448-eb55a9b528f4` — richest toolset, forecast_* family, zero writes).

## Steps

1. **Sandbox DB** — new script `scripts/seed_sandbox_auto.py` run in `zhanlu-postgres`: creates database `zhanlu_sandbox_auto` with automotive sales schema (`vehicle_model`, `dealer`, `sales_order`, `monthly_sales`, `production_plan`) + 36 months of deterministic seasonal data (2023-10 → 2026-09). Idempotent. Falcon SUV constructed as the 2025 revenue leader (verifiable).
2. **KB connection** — via the existing API flow (template: `scripts/e2e_catalog_autotrigger.py`): login → `POST /entities/KnowledgeBase` (postgresql, host=`postgres`, db=`zhanlu_sandbox_auto`) → auto-index trigger → watch `catalog_status` → verify catalog tables/columns/stats.
3. **Ontology mapping** — `POST /knowledge_bases/{kb_id}/discover` → verify proposed objects/links for the sales domain.
4. **Agent binding** — `PUT /entities/AgentApp/{id}` sets `knowledge_bases=[kb_id]` on the pilot (existing binding mechanism, `data_source_runtime.py`).
5. **Runtime verification** (real HTTP, v2 conversation endpoint):
   - G1: "Which vehicle model generated the highest total revenue in 2025?" → must answer Falcon SUV with the exact number (cross-checked against SQL on the sandbox DB).
   - G2: monthly trend question → series grounded in tool results.
   - R1 (refusal): question about data that does not exist (HR salaries) → honest refusal, no fabrication.
   - Evidence: `agent_invocations` row for the turn with `conversation_id` set; answer follows the agent's prompt_output contract.
6. **Flags**: `DELEGATE_AGENTAPP_AWARE`/`DELEGATE_INHERIT_BINDINGS` already true in env (not needed for single-agent pilot but confirms live).

## Files
- Create: `scripts/seed_sandbox_auto.py`, `scripts/pilot_demand_forecast_e2e.py`
- No backend code changes expected. Binding via API only.

## Exit criteria
- catalog_status=ready with ≥4 tables described; ontology objects proposed; agent bound; G1/G2 numerically correct vs SQL; R1 refuses honestly; invocation row linked.
