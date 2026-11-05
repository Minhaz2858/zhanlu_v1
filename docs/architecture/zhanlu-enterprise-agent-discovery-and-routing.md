# Zhanlu Enterprise Agent Discovery & Routing — Architecture Design (P1, Part B)

**Date:** 2026-10-31 · Companion to the GA audit (Part A). Design only — no implementation. Principle: **extend existing mechanisms; no second registry, no new orchestrator.**

---

## 1. Design goals

1. A user asks any enterprise question in one conversation; General Assistant resolves context, finds the right specialists, and coordinates them — the user never picks an agent manually.
2. Discovery is **capability-based**, served from data that already exists (published status, `capabilities`, portfolio, KB bindings), extended — not duplicated.
3. Routing reuses the existing ReAct + `delegate_task` + plan-DAG; nothing new executes tasks.
4. Security: department/project scoping must strengthen, never weaken, the existing fail-closed permission posture.

## 2. What already exists (reuse map)

| Need | Existing mechanism | Gap |
|---|---|---|
| Agent capability data | `AgentApp.capabilities` JSON (7 mfg agents carry real values), `description` (5-layer), skills, tool_config | Not exposed to GA at decision time |
| Publication/lifecycle | `status="published"`, `agent_lifecycle.py` revisions/canary, `GET /agents-catalog` | Catalog API not consumed by the agent loop |
| Business metadata | `AgentPortfolioEntry` (owner, KPI, lifecycle stage) | Not joined into discovery |
| Data scope | `AgentApp.knowledge_bases`, `data_source_runtime` project semantics | Not exposed in discovery payload |
| Multi-agent execution | `delegate_task` (single + same-agent batch), plan-DAG (agent node declared, not dispatched) | Agent-node dispatch; per-task agent in batch |
| Shared context | `ProjectMemory`, project context block | No department grouping |
| Frontend status | `LiveActivityStream.jsx` (plan steps, tool_progress, sub-agent activity pairs) | Parent-child tree view, evidence drill-down |

## 3. Target flow

```
User message
  → Context Resolver (EXISTING: project_id resolution llm_router.py:92-139 +
     data_source_runtime project semantics; NEW: department → project set expansion)
  → General Assistant (EXISTING ReAct loop, hardcoded prompt branch)
      + NEW: discovery tool in toolset + department directory prompt block
  → Agent Discovery (NEW capability: enrich existing list_available_agents)
  → Task Planner (NEW: prompt-level planner layer in GA's reasoning process;
     EXECUTION stays delegate_task + plan-DAG)
  → Specialized Agents (EXISTING: AgentApp-aware delegation from P0)
  → Evidence Validation (EXISTING parent grounding guardrail; NEW: child
     result contract check — success/truncated/figures-presented)
  → Final Answer (EXISTING artifact/report tools)
```

## 4. Component designs

### 4.1 Department context (smallest viable model)

**Do not create a Department entity yet.** Departments are labels, and the org structure already has the container: **Project**. Smallest change:
- Add a nullable `department` string column to `projects` (and expose on `AgentApp` via its home project + `ProjectAgent` memberships). One migration (`alembic/versions/1xx_project_department.py`), no new tables, no new APIs — the generic entity API carries it automatically.
- "All Departments" = `project_id=None` conversation; the Context Resolver expands to **all projects the user can access** (existing `ResourceShare` + NULL-creator visibility rules, `entity_service.py:70-138`) for **discovery only** — data access stays governed by each delegated child's own bindings and the existing anti-leak guard. Department selection = filter projects by `department`. Specific project = today's behavior unchanged.
- Security invariant: department expansion affects **which agents may be discovered**, never which data is readable — data authorization remains per-child (`knowledge_bases` + `ResourceAccessPolicy` + RLS).

### 4.2 Agent Discovery (extend `list_available_agents`, don't duplicate)

Enrich the existing tool (`delegate_tool.py:722-765`):
- Add to each published-AgentApp entry: `capabilities` (already there), `department` + `home_project` (from 4.1), `kb_count`/`kb_names` (bound data domains), `portfolio` (lifecycle stage + KPI from `AgentPortfolioEntry` when present), `version` (from `agent_lifecycle`).
- Optional params: `department` filter, `capability` keyword filter — server-side filtered from the same query.
- **Add the tool to general_assistant's seed toolset** (`system_agents.py:378-403`) — one-line fix with outsized effect.
- Capability matching stays **LLM-over-structured-catalog** (the ReAct loop reads the tool result and decides). An embedding-based matcher over `capabilities+description` (reusing bge-m3, already in the stack) is a **P2 option** — only if LLM-over-catalog proves insufficient; do not build it preemptively.

### 4.3 Task Planner (prompt layer, not code)

- GA's hardcoded prompt (`agent_prompts.py:1767+`) gains a **ROUTING PROCESS** section in its reasoning layer: decompose → for each sub-task pick specialist via `list_available_agents` (match capability + department + data domain) → delegate with self-contained task briefs (context digest already inherited via P0's `DELEGATE_CONTEXT_INHERITANCE`) → aggregate → validate evidence → answer.
- **Implement the declared plan-DAG `"agent"` node dispatch** (`capability_router.py:441-443` — declared, falls through today): an agent node executes via the same `_run_sub_agent_inner` used by `delegate_task`. This gives *sequential/parallel multi-specialist orchestration with dependency ordering* inside the existing DAG executor (parallel ready-batch already shipped, P0 audit) without a new orchestrator.
- Extend `delegate_task` batch with optional **per-task `agent` field** (`delegate_tool.py:203-211` — currently one agent for all tasks): enables one-call heterogeneous fan-out (Sales + Finance + Manufacturing in parallel) while keeping the existing gather/semantics.

### 4.4 Evidence validation (close the trust gap)

- Turn **on-by-default a lightweight child-result contract check** (code, not LLM): a delegated result must carry `success:true` and non-empty `response`; `truncated:true` results must be surfaced to the parent prompt explicitly ("PARTIAL — child hit its wall clock"); failed children are re-plannable, never silently dropped. This extends the existing shapes, no new mechanism.
- Keep `DELEGATE_VERIFY_ENABLED` (LLM-judge) opt-in for high-stakes routes; the figure-grounding guardrail already covers the parent's final text (P0 audit §8).
- Stretch (P2): propagate the grounding guardrail into child loops via a shared checker — only if the contract check proves insufficient.

### 4.5 Chat UI (preserve the conversational interface)

- Keep GA as the silent default (existing behavior, `Chat.jsx:1372-1389`).
- Add a **context chip row** (existing header area): All Departments / Department / Project — driven by the same `?project=` URL mechanism, plus a `?department=` param handled by the Context Resolver. No agent picker is added — routing stays automatic.
- Extend `LiveActivityStream.jsx`: render the existing per-sub-agent `activity_step` pairs as a **parent-child task tree** (data already emitted, `delegate_tool.py:214-236`), and surface each child's `truncated`/`success` status from the delegate result payload.
- Evidence drill-down: the tool_result payloads (ask_data_agent results carry citations/truncation flags already) — expose a "sources" affordance on the final message reading from the persisted message trace (data already persisted, `Chat.jsx:2812-2813`).

## 5. Scenario walkthroughs (current vs target)

Legend: ✅ works today · 🔶 works with P1 gaps closed · ❌ missing pieces.

### A — "Why is production below target?" (Production shortage)
- **Today:** GA answers from bound data if KBs are bound (REPLACE semantics) via ask_data_agent; likely plain ReAct (audit §2.4). No specialist coordination; Production Scheduling invisible (GA lacks discovery).
- **Target:** GA decomposes → delegates to Production Scheduling (root-cause vs plan), Inventory (material availability), Supply Chain (supplier delays) — parallel batch fan-out → aggregates. 
- **Needs:** 4.2 discovery + 4.3 per-task agent + department labels; bindings: production/inventory/supply KBs; ontology: WorkOrder→BOM→InventoryItem→SupplierOrder links (mostly exist as ontology links).
- **Missing agents:** Production Monitoring (today: shared `universal_trend` capability — acceptable initially).

### B — "Which vehicle model generates the highest profit?"
- **Today:** single-datasource revenue answers work (pilot-proven). **Profit = revenue − cost** requires joining sales + cost/procurement data — cross-KB joins don't exist (G1 identity resolution pending per ontology PRD).
- **Target:** GA delegates to Finance Controller (margin model) with Sales + Procurement inputs; or (better pre-G1) one agent with both KBs bound and ontology metric backing (`MetricDefinition`, PRD P5) defining "profit" once.
- **Needs:** metric backing (PRD P5) is the honest path — otherwise cross-agent arithmetic risks figure-mixing; grounding guardrail tolerances (0.5%) would catch mismatches.

### C — "Why did defects increase this month?"
- **Today:** Quality Control Agent exists (published, sandbox-bound in P0 pilot scope); SPC via code exec.
- **Target:** GA → Quality Control (defect trend) ∥ Production Scheduling (process changes) ∥ Supply Chain (incoming material lots) → correlation narrative.
- **Needs:** same discovery/planner gaps as A; ontology Defect→Batch→SupplierLot links; QMS data binding per deployment.

### D — "Generate the monthly enterprise performance report"
- **Today:** automation_agent + report tools handle single-domain reports; grounding guardrail active.
- **Target:** GA (or automation schedule) → parallel fan-out to Sales/Finance/Manufacturing/Supply Chain agents → each returns a section with cited figures → GA consolidates into one artifact (`create_artifact` docx/pptx).
- **Needs:** per-task-agent batch (4.3), section-level result contract (4.4), artifact assembly (exists).

## 6. Implementation dependencies (ordered)

1. **D1 (one line):** add `list_available_agents` to GA toolset → immediate capability-aware delegation.
2. **D2:** enrich `list_available_agents` payload (department/project/KB/portfolio join).
3. **D3:** `department` column migration + Context Resolver (department→project expansion, discovery-only).
4. **D4:** plan-DAG agent-node dispatch + per-task agent in batch.
5. **D5:** child-result contract check in parent loop.
6. **D6:** UI context chips + parent-child tree + evidence affordance.
7. **D7 (P2):** embedding-based capability matcher; grounding propagation to children; metric backing for profit-class questions (PRD P5).

## 7. Test & acceptance criteria

- **T1:** GA with no agent knowledge in-prompt answers "who can help with X" by calling `list_available_agents` and naming the right specialist (capability match, not name match).
- **T2:** Scenario A ends with ≥2 delegated children from different departments, parallel execution observed (activity pairs), aggregated narrative, zero fabricated figures (grounding guardrail green).
- **T3:** Department-scoped conversation discovers ONLY that department's agents; All-Departments discovers all published agents the user can access; data authorization unchanged (RLS/policy tests stay green).
- **T4:** A child returning `success:false` / `truncated:true` is surfaced in the parent answer (explicitly), never silently dropped.
- **T5:** Plan-DAG with agent nodes executes multi-specialist chains with dependency ordering; wall-clock caps still bound total runtime.
- **T6:** Cross-tenant: department/project expansion never leaks another org's agents (extend `tests/test_entity_tenant_scoping.py` + `tests/test_tenant_rls.py`).
