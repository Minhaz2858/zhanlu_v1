# Zhanlu Enterprise Agent Platform — Prioritized Development Roadmap

**Date:** 2026-10-31 · Feeds off Doc A (audit), Doc B (coverage matrix), Doc C (target architecture). Every item: what, why (necessity), dependencies. No item duplicates an existing capability.

---

## P0 — Essential platform foundations
*(Without these, department agents cannot answer from real data or collaborate. All are small, surgical, on verified seams.)*

| # | Item | What | Why necessary | Dependencies |
|---|---|---|---|---|
| P0-1 | **AgentApp-aware delegation** | Resolve `delegate_task` children through the AgentApp (real 5-layer prompt + its `tool_config`); flag-gated | Breakpoint ② — without it, delegation runs generic shells and the whole collaboration story is fiction | None (seam: `delegate_tool.py:321` + toolset builder) |
| P0-2 | **Binding inheritance to sub-agents** | Thread parent `context`/`bound_kb_ids` into child-loop `execute_tool` | Breakpoint ③ — child data agents otherwise query the wrong scope or refuse | None (`delegate_tool.py:467-482`) |
| P0-3 | **Publish + bind the 7 manufacturing agents** | Guided binding wizard over existing endpoints; set `published` after binding; per-agent smoke scripts | Agents are unbound shells today [live-verified]; publishing without binding is useless, binding without publishing makes them undelegatable | P0-1, P0-2 for delegation use; KB connect + ontology bind endpoints exist |
| P0-4 | **Policy profile correction** | `risk_tier=medium` + confirmation for PO-drafting agents (Supply Chain, Inventory); read-only agents stay low | PO drafts are real writes; `low` understates risk. Enforcement already exists (`agent_tools.py:536-577`) — only the seed is wrong | None |
| P0-5 | **Cross-agent invocation linking** | Stamp `AgentInvocation.conversation_id` on delegations (parent conversation id) | Breakpoint ④ (minimal fix) — observability of multi-agent runs; prerequisite for any enterprise trust review | None |
| P0-6 | **Debt hygiene batch** | Fix SYNEXIA flag doc/config conflict; delete dead `ensureAgentApp`; decide `universal_*` family scope; hide/enforce decorative builder fields | Stops config from lying to builders; prevents future agents being built on stored-only fields | None |
| P0-7 | **Agent evaluation harness (v1)** | Golden-question sets per agent vs sandbox datasources (extends `tests/nl2sql/` golden-eval pattern) | No quality gate exists for agent answers; shipping P1 agents without it repeats the fabrication incident class | P0-3 |

**P0 exit criterion:** a COO can open one manufacturing agent, it answers from the company's bound test DB with grounded figures, and a delegated sibling run is visible in observability.

---

## P1 — Core manufacturing and enterprise intelligence
*(First real department value; assumes P0.)*

| # | Item | What | Why | Dependencies |
|---|---|---|---|---|
| P1-1 | **Data onboarding packs (automotive reference)** | Seedable catalog+ontology+binding templates for the 6 P1 departments (Doc B §3 tables) | Enterprise onboarding is currently manual archaeology; templates make "connect your own DB" a day, not a month | P0-3 wizard |
| P1-2 | **Finance Controller agent** (new) | Prompt+bindings on shared layer; reuses `match_invoice`, report generator, grounding guardrail | Scenario B demand; finance is the first cross-table consumer (profitability) | P1-1; G5 metric backing for "margin/profit" definitions |
| P1-3 | **Ontology metric backing (PRD P5 scope)** | `MetricDefinition` becomes the `metric` backing kind, reusing NL2SQL gates | One revenue/OEE definition across agents; without it Finance and Production compute differently | PRD P3 prerequisites respected; user-approved sequencing |
| P1-4 | **Domain skills: SPC + OEE + supplier scorecard** | Shared registry tools/skills consumed by QC/Production/Supply Chain agents | Dedupes analysis logic before a fourth agent copies it (Doc C W2) | None |
| P1-5 | **Scenario A/B/C enablement** | After P0-1/2/3: exercise Scenario A (delegate fan-out via existing DAG), B (single-agent multi-KB join), C (Scheduling→Forecast/Inventory delegation chain); document gaps found | Proves cross-department collaboration on real seams, not demos | P0-1, P0-2, P0-3, P1-1 |
| P1-6 | **Freshness on by default (enterprise profile)** | `KG_COVERAGE_PROBE_ENABLED=True` + coverage refresh loop active in enterprise deployments | Stale catalogs misroute recent-window questions (audit B8 class); loop exists but is inert | Ops decision only |

---

## P2 — Cross-department collaboration

| # | Item | What | Why | Dependencies |
|---|---|---|---|---|
| P2-1 | **Sales / Dealer agent** | Prompt+bindings; reuses `score_leads`, `segment_records`, ontology Customer | Coverage gap #7; feeds Demand Forecasting | P1-1 pack for CRM/DMS |
| P2-2 | **After-Sales / Warranty agent** | Claims analysis over warranty DB + service-parts linkage to Inventory; RAG on manuals | Coverage gap #9 | P1-1; parts data |
| P2-3 | **Executive Briefing agent** | Orchestration consumer: delegates to department agents, aggregates into briefings | High-visibility value after collaboration works | P1-5 scenarios passing |
| P2-4 | **Cross-agent trace tree (harness-lite)** | Enable AgentRun/Step trail for delegated runs only (flag-scoped); replay in Observability UI | Root-cause multi-agent failures; break. ④ fully | P0-5 |
| P2-5 | **Ontology P3 identity resolution (approved PRD)** | Cross-source record-level identity (G1) with 4 separated matching tiers | Cross-source joins for Scenario B-class questions across differently-named keys | PRD P0-P2 accepted; P3a structured ingestion |
| P2-6 | **Org-scoped skill namespaces** | Registry reads filtered by org | Removes the last cross-tenant surface (audit D1 full fix) | Careful migration; live verification |

---

## P3 — Advanced automation and proactive intelligence

| # | Item | What | Why | Dependencies |
|---|---|---|---|---|
| P3-1 | **Proactive sensing→agent chains** | Extend ontology sensing: threshold → fan-out → department agent proposes mitigation draft (PO draft, schedule proposal) | Moves from Q&A to operations; sensors + fan-out + drafts all exist | P1-5; approval gates unchanged |
| P3-2 | **Scheduled cross-department automations** | Monthly reviews: Executive agent runs delegation DAG on schedule with `_previous_run_context` ledger | Recurring executive/plant reporting without a human driving | P2-3 |
| P3-3 | **Schedule/write-back actions (gated)** | MES/CMMS write-back as ontology `write` actions requiring `permission_scope=write` + approval | Closes the loop from recommendation to execution — only after trust is earned | P2 governance patterns; customer MES APIs |
| P3-4 | **Ontology P4 provenance / P6 evolution loops (approved PRD)** | Doc-level evidence; binding-health loop replacing dead drift path | Full auditability for regulated automotive contexts | PRD sequencing |
| P3-5 | **Swarm/harness enablement decision** | Turn on `AGENT_HARNESS_ENABLED` only if P2 trace shows real need | Avoid third-orchestrator complexity without evidence | P2-4 data |

---

## Dependency graph (simplified)

```
P0-1 ─┐
P0-2 ─┼─→ P0-3 ─→ P0-7 ─→ P1-1 ─→ P1-2 ─→ P1-5 ─→ P2-3 ─→ P3-2
P0-4 ─┘                 ↘ P1-4            ↘ P2-1/P2-2   ↘ P3-1
P0-5 ─→ P2-4 ─→ P3-5
P0-6 (independent)
P1-3 ─→ P1-2 ; P2-5 ─→ P3-4 ; P2-6 (independent)
```

## Suggested sequencing rhythm
- **P0:** ~1-2 weeks of engineering (all seams verified; P0-3 depends on customer data availability, use sandbox DBs).
- **P1:** 2-4 weeks (P1-1 templates are the long pole; P1-3 follows the approved PRD gate).
- **P2/P3:** feature-by-feature behind flags, each preceded by the anti-duplication check from Doc B.

**Standing rule:** no new agent, tool, or orchestrator starts until the Coverage Matrix anti-duplication check and the evaluation harness (P0-7) cover its domain.
