# Zhanlu Enterprise Agent Platform — Recommended Target Architecture

**Date:** 2026-10-31 · Companion to the audit (`zhanlu-current-enterprise-agent-architecture-audit.md`) and coverage matrix. Principle: **extend what exists; rebuild nothing that works.** Every recommendation names the existing mechanism it reuses.

---

## 1. Design principles

1. **One execution loop, many agents.** The v2/v3 ReAct loop + `ask_data_agent` + `delegate_task` already are the runtime. Do not add a fourth orchestrator (ReAct, FSM/DAG, and swarm already coexist; keep FSM/DAG flag-gated as-is).
2. **Agents are thin; capabilities are shared.** A department agent = 5-layer prompt + `tool_config` + data bindings. Department *intelligence* (SPC math, OEE formulas, margin logic) belongs in **shared tools/skills**, not duplicated prompts — prevents the "20 agents re-implementing the same analysis" failure mode.
3. **Data binds per deployment; prompts stay generic.** The platform's grounding rules (figures only from tool results; refuse unbound sources) are the honesty contract — target architecture keeps agents schema-agnostic (already the house rule: "keep the data layer dynamic, never DB-specific").
4. **Writes stay gated.** Every operational write = ontology action or approval-gated draft (`create_po_draft`, `execute_approved_action`). No new write path.
5. **Governance fail-closed.** Extend the ontology P0 permission pattern to any new execution surface.

---

## 2. Target state (18 months out, automotive-first)

```
                        ┌──────────────────────────────────────────────┐
                        │              Enterprise Data Layer            │
                        │  KnowledgeBase datasources (6 engines) →      │
                        │  Catalog (tables/columns/relations/domains) → │
                        │  Ontology (domains/objects/links/actions +    │
                        │  evidence + revisions + sensing) →            │
                        │  Semantic store (metrics, synonyms, workload) │
                        └───────────────┬──────────────────────────────┘
                                        │ grounded reads (1 KB / query,
                                        │ cross-source via ontology G1 later)
                        ┌───────────────┴──────────────────────────────┐
                        │           Shared Capability Layer             │
                        │  NL2SQL pipeline · RAG · forecasting ·        │
                        │  domain skills (SPC, OEE, margin, scorecards) │
                        │  drafts/approvals · escalation · sensing      │
                        └───────────────┬──────────────────────────────┘
                                        │ tool_config + skills
                        ┌───────────────┴──────────────────────────────┐
                        │              Agent Layer (~10-14 agents)      │
                        │  7 manufacturing (existing) + Sales, Finance, │
                        │  After-Sales, CAD, Compliance …               │
                        │  5-layer prompts · policy profiles · RLS      │
                        └───────────────┬──────────────────────────────┘
                                        │ delegate_task (AgentApp-aware)
                        ┌───────────────┴──────────────────────────────┐
                        │           Collaboration Layer                 │
                        │  delegation directory · plan DAG (existing) · │
                        │  cross-agent trace (harness run-tree) ·       │
                        │  HITL approvals · automation schedules        │
                        └──────────────────────────────────────────────┘
```

---

## 3. Evolution workstreams (each maps to existing code)

### W1 — Make the 7 agents actually work (highest leverage, smallest change)
1. **Publish flow:** flip the manufacturing agents to `status="published"` after binding review (or seed them published) so `list_available_agents` sees them (`delegate_tool.py:662`).
2. **AgentApp-aware delegation:** pass the resolved AgentApp into `get_system_prompt(agent_name)` and derive the child toolset from the AgentApp's `tool_config` instead of the default toolset. This is one seam: `delegate_tool.py:321` + the child-loop toolset builder. The 5-layer prompts already assemble from an AgentApp object (`agent_prompts.py:2441-2454`).
3. **Binding inheritance:** thread parent `context` (at minimum `bound_kb_ids`) into the child loop's `execute_tool` calls (`delegate_tool.py:467-482`).
4. **Per-deployment binding UX:** the binding act stays what exists today — KB connect → catalog index → ontology bind → `AgentApp.knowledge_bases`. Add a guided "Connect your enterprise" wizard on the Market/detail page that drives the existing endpoints (no new backend).

### W2 — Shared capability layer (deduplicate before adding agents)
- **Domain skills not agents:** implement SPC math, OEE, supplier scorecards, margin-by-model as registry tools or skills consumed by any agent (Quality/Production/Finance/Supply Chain all reuse). Registry already supports this (200 tools; skill→tool_config translation exists).
- **Retire/merge the `universal_*` family into the data-agent path** or clearly scope it (avoid two competing "answer from data" surfaces — audit §14).
- **Metric backing (ontology G5):** make `MetricDefinition` the single source for the ontology `metric` backing kind (PRD already specifies reusing the NL2SQL gates) — this is what lets Finance and Production agree on "revenue" and "OEE".

### W3 — Collaboration layer
- Reuse the **plan DAG agent nodes** for cross-department scenarios (Scenario A/C), with the W1 fixes making `agent` nodes resolve real AgentApps.
- Keep **swarm/harness flags off** until a concrete need; when enabled, extend spawn to AgentApps (currently builtin-only).
- **Cross-agent trace:** link delegation `AgentInvocation.conversation_id` to the parent conversation (one field) and enable the harness run-tree only for delegated runs (flag-scoped rollout) — gives a trace tree without turning on the whole harness.

### W4 — Data architecture evolution (already spec'd, do not re-design)
- Follow the approved **ontology PRD** (P3 identity resolution → P4 provenance → P5 metric backing), which fills G1/G2/G5 — the three data gaps that block cross-department joins (e.g., Scenario B "profit by vehicle model" spans finance + sales + manufacturing tables that share entity keys but not naming).
- Structured row ingestion for xlsx/csv (prerequisite already noted in P3a) before spreadsheet data joins identity resolution.
- Turn on `KG_COVERAGE_PROBE_ENABLED` in enterprise deployments so the freshness loop is real; replace dead `drift.py` with the binding-health loop per PRD P6.

### W5 — Trust, evaluation, operations
- **Agent evaluation harness:** golden-question sets per department agent (question → expected tables/figures), run in CI against sandbox datasources. Extends the existing `tests/nl2sql/` golden-eval pattern.
- **Policy profiles:** raise `risk_tier` to `medium` for agents that draft POs; keep `requires_confirmation=False` only for pure-read agents (one seed-script change + the existing enforcement).
- **Fix debt flagged in the audit:** SYNEXIA flag doc/config conflict; dead `ensureAgentApp`; `crypto.py` fail-open branch; decorative-field cleanup (either enforce or hide `data_write`, `trace_enabled`, etc. from the builder UI to stop false promises).

### W6 — Department expansion (only per Coverage Matrix priorities)
Sales, Finance, After-Sales agents as prompts+bindings on the same shared layer; Marketing/EHS capabilities stay read-tier. HR only on demand.

---

## 4. What we deliberately do NOT build

- A second ontology engine or graph DB (PRD decision, user-approved 2026-10-30).
- A new multi-agent framework (swarm/harness stay off; DAG + delegation suffice after W1).
- Real-time vehicle/machine control (AV agent stays data/simulation).
- Per-department NL2SQL variants (the pipeline is department-agnostic by design).
- Agent-specific hardcoded schemas/table vocabularies (house rule).

## 5. Risks

| Risk | Mitigation |
|---|---|
| AgentApp-aware delegation changes child behavior for existing users | Flag-gate (`DELEGATE_AGENTAPP_AWARE`), default off, A/B via rollout_percent once enforced |
| Published agents answer wrongly without bindings | Publish only after binding wizard completes; honesty refusals already cover the gap state |
| Skills/tools sprawl duplicates logic | W2 dedup review before every new department agent (Coverage Matrix anti-duplication rule) |
| Cross-agent runs cost/latency blowups | Existing walls: 600 s delegate wall, batch concurrency 4, per-turn SQL budget — extend per-scenario budgets rather than new mechanisms |
