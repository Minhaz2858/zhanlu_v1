# Zhanlu General Assistant — Intelligence Architecture

**Date:** 2026-10-31 · Status: design grounded in same-day code audit (see
`zhanlu-manus-chatgpt-capability-benchmark.md` for the audit evidence). Existing mechanisms are
named by file; nothing here requires a second orchestrator.

## 1. The GA as intelligent core — current truth

The GA is not a prompt-forwarding bot. Verified working-tree behavior:

| GA responsibility (mission §4) | Mechanism | State |
|---|---|---|
| 1 Intent understanding | LLM interpretation over system prompt assembled by `context_assembler.assemble_context` (conversation, project, department metadata, memory, KB bindings) | VERIFIED |
| 2 Context resolution | `context_assembler.py` + department block (`agents._append_department_block`) + conv metadata | VERIFIED |
| 3–4 Objective & requirement extraction | GA system prompt + goal-based task contract flow (`AgentTaskContract`) | VERIFIED |
| 5 Data-source discovery | KB/DS runtime prep, `bound_kb_ids`, auto-bind (`data_source_runtime.py`) | VERIFIED |
| 6–7 Tool/skill/agent discovery | `tool_registry.resolve_tools_for_agent` (task-scoped, NOT every-tool-every-call), `delegate_tool._list_available_agents` (AgentApp-aware, department-enriched) | VERIFIED |
| 8–9 Planning & decomposition | `turn_planner.build_turn_plan` (template + LLM `parse_dynamic_plan`), plan-DAG (`plan_dag.generate_plan` → `capability_router` DAG executor w/ parallel prepass) | VERIFIED (DAG not default) |
| 10 Strategy selection | ReAct direct execution vs delegation vs DAG dispatch — LLM chooses via tool surface; broker enforces governance | VERIFIED |
| 11 Direct execution | GA's own tools (NL2SQL, RAG, artifacts, web, sandbox) | VERIFIED |
| 12 Delegation | `delegate_tool._delegate_task/_delegate_batch` → broker.evaluate_delegation → `_run_sub_agent` | VERIFIED |
| 13 Peer collaboration | Pattern C (`delegation_mode="peer"`), nested, governed | VERIFIED |
| 14–15 Intermediate evaluation & replanning | observation-driven loop + `replan_policy.py` (budgeted replans) + adaptive-replan trigger | VERIFIED |
| 16 Evidence aggregation | shared evidence registry; GA consumes peer evidence references | VERIFIED |
| 17 Verification | reviewer/critic sub-agent + answer-grounding gate (nudge-retry) + P-SV source verification | VERIFIED |
| 18 Synthesis | `synthesize_report` / final response w/ grounding | VERIFIED |
| 19 Artifacts | artifact tools (xlsx/pptx/docx/pdf/html/md) + preview | VERIFIED |
| 20 Execution-state reporting | typed SSE + `AgentInvocation` + execution-tree REST | VERIFIED |

**Authoritative objective:** the original user message is carried in the loop context each
iteration and re-injected on replan (`replan_policy` preserves the objective; the loop budget
tracks progress against it).

## 2. Gaps against the mission (Part D focus) and the fix design

### 2.1 Intent understanding (mission §5) — PARTIALLY implicit
Intent is implicit in the LLM's tool choice; there is no explicit, persisted intent
classification, so planning/eval/telemetry can't reason about intent types (conversational /
retrieval / analysis / research / investigation / decision-support / report / operational /
multi-step / cross-department).

**Design (reuse, no new framework):** extend the existing plan payload. `turn_planner` already
produces a validated structured plan (JSON-schema-checked in `parse_dynamic_plan`). Add optional
fields `intent_type` (enum above) and `objective_statement` (restated goal). Schema-validated,
backward-compatible (absent → template path unchanged). Emit in the existing `plan_created`
live event. This gives §5's "distinguish intents" without a keyword router and without a new
model call — the planner LLM classifies while planning.

### 2.2 Dynamic planning engine (mission §6) — mostly EXISTS; enrich plan fields
The existing plan already carries goal, tasks, dependencies (`plan_dag` edges), parallel
opportunities (DAG ready-batch), expected results, evidence requirements (task contract),
budgets. Missing per §6: **constraints**, **acceptance criteria**, **risk level**,
**required approvals** as first-class plan fields.

**Design:** add these four optional fields to the plan schema; `plan_dag` maps risk/approvals
onto existing execution gates (`Execution.needs_approval`, broker risk tiers). No new table.

### 2.3 Adaptive planning (mission §7) — EXISTS with budget; sharpen criteria
`replan_policy` + adaptive-replan trigger already do controlled replanning without restart, and
loop budget adds progress-based extensions. The seven §7 evaluation questions map to the existing
post-observation evaluation in the ReAct loop. **Enhancement:** feed the reviewer verdict
(`needs_evidence` → missing-evidence list) into the replan decision so "is the evidence
sufficient?" is answered mechanically, not only by the LLM. Small, contained change in the
post-delegation evaluation path.

### 2.4 Strategy selection must not maximize agent count (mission §8)
Already true structurally: direct execution is the default path; delegation is LLM-initiated
and broker-governed; DAG dispatch requires planning triggers. **Guardrail to add:** log a
delegation_decision rationale (objective, expertise needed, data access, cost/risk) into the
invocation record for eval — cheap observability that makes "why delegate?" auditable.

## 3. What will NOT be built
- No keyword router (explicitly forbidden; LLM planning supersedes it).
- No second planner/orchestrator — all changes extend `turn_planner` / `plan_dag` /
  `replan_policy`.
- No new memory systems — §16 continuity maps onto existing conversation / project / enterprise
  knowledge tiers (already separated and permission-bounded; verified in audit).

## 4. Test strategy
Schema-validation tests for the extended plan payload (template + LLM paths), replan-criteria
unit tests (reviewer-verdict-driven), and delegation-decision observability test — all against
the existing suites' fixtures.

## 5. Implementation status (2026-10-31 — Phase 1 P1.1–P1.5 COMPLETE)

§2.1 (intent), §2.2 (plan enrichment), §2.3 (reviewer-driven replan decision) and §2.4
(delegation-decision observability) are IMPLEMENTED as designed — see
`zhanlu-phase1-intent-planning-implementation.md` for the full record, test evidence and
rollback. Deviation from §2.2 as first drafted: the enrichment fields live on `TurnPlan`
itself (kind/steps plus meta), not a separate plan document — smallest change, fully
backward compatible. Deviation from §2.4: the decision layer is
`evaluate_reviewer_findings` in the existing `synexia/replan_policy.py` (one policy, one
more consumer) rather than a new observability module.
