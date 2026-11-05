# Zhanlu Hybrid Multi-Agent Execution Design (Patterns A/B/C — preserved and hardened)

**Date:** 2026-10-31 · Grounded in the 2026-10-31 code audit. This document records the
preserved architecture (mission Part E) and the targeted hardening plan. No second
orchestration framework is introduced.

## 1. Preserved architecture (verified)

| Component | Anchor | State |
|---|---|---|
| GA primary supervisor | `agents.py` v3 loop | VERIFIED |
| ReAct runtime | `agents.py:7563+` (interleave, observe, caps, wrap-up, reflexion, compaction) | VERIFIED |
| DAG executor | `plan_dag.py` + `capability_router.py` (parallel prepass, adaptive-replan isolation) | VERIFIED (not default) |
| Collaboration Broker | `agent_collaboration/broker.evaluate_delegation` — publication, dedup, cycle, depth, children, concurrency, allowlist | VERIFIED |
| Agent Task Contract | `agent_collaboration/contract.py` (objective, deliverable, evidence requirements, max_iterations, forbidden actions) | VERIFIED |
| Agent discovery | `_list_available_agents` + department enrichment | VERIFIED |
| Governance store | `agent_collaboration/governance.py` (depth/children/token budgets, Redis-or-memory) | VERIFIED |
| Invocation + audit | `AgentInvocation` hierarchy (`span_id` parent links), execution-tree REST | VERIFIED |

## 2. Three execution patterns (mission §8) — how they work today

- **Pattern A — Direct execution:** GA executes with its own tools (NL2SQL, RAG, web,
  artifacts, sandbox). Default path; no delegation panel is rendered when no children exist
  (`AgentExecutionTree` renders nothing without roots — mission §22 requirement satisfied).
- **Pattern B — Supervisor-led delegation:** GA → `delegate_task` (single or heterogeneous
  batch, bounded `DELEGATE_MAX_CONCURRENT=4`, context inheritance to sub-agents).
- **Pattern C — Peer collaboration:** a specialist's result triggers it to call
  `delegate_task` itself with `delegation_mode="peer"`; broker enforces depth/child budgets,
  duplicate suppression and cycle prevention; evidence is exchanged by reference through the
  shared registry; GA synthesizes.

**Strategy selection (§8)** is dynamic: the LLM chooses direct vs delegate vs plan-DAG; the
broker is the governance boundary either way. Cost/risk/latency enter through the task
contract (`max_iterations`, token budgets) and reviewer verification. Design principle honored:
*maximize successful task completion, not agent count* — direct execution requires no special
trigger.

## 3. Hardening plan (targeted, TDD; no rebuilds)

| # | Item | Root evidence | Change |
|---|---|---|---|
| H1 | **Lifecycle consistency (mission §23):** GA must not produce a final answer while required children are unresolved; every started child reaches a truthful terminal state; partial stays partial | `_run_sub_agent` persists invocation rows at COMPLETION only; a crashed/cancelled batch leaves no row (invisible), and live-event synthesized nodes can show stale "running" | (a) persist a `running` invocation row at delegation start, update at completion (single row, status transitions); (b) at loop end, any `running` child invocation with no live sub-agent gets a truthful terminal status (`failed`/`cancelled`), never fabricated `completed`; (c) dedup/ordering already prevents duplicate branches and terminal regression (`dedupeAndSortEvents`) |
| H2 | **Peer relationship displayed as real** (§22) | tree nodes carry `delegation_mode` + real `agent_name` from AgentApp labels | already satisfied; keep "sub-agent" only as last-resort fallback when identity is genuinely absent |
| H3 | **Task contracts enriched** with acceptance criteria + risk/approval (§6 plan fields) | contract schema | optional fields; map risk → broker tier, approvals → existing `Execution.needs_approval` gate |
| H4 | **Delegation-decision observability** | no rationale recorded | record objective/expertise/cost rationale on the invocation for evaluation (§25) |
| H5 | **Timeout + cancellation truthfulness** | child timeouts already supported | ensure timeout maps to `timeout` status in tree (exists) and partial batches surface `[PARTIAL RESULT]` (exists) |

## 4. A2A mapping (mission Part F)

Internal delegation is NOT claimed as A2A compliance. Verified mapping surface:
`routers/a2a.py` (inbound: Agent Card, tasks, artifacts, streaming, push notifications;
lifecycle `submitted/working/input-required/completed/failed/cancelled` in
`services/a2a/tasks.py`) and `routers/acp.py`. Internal broker states map onto these
protocol states at the A2A boundary. **Outbound federation is a separate feature-gated
capability** (benchmark R7) — not built here.

## 5. Enterprise boundaries (mission Part I)

Department/project context is discovery context, never an authorization bypass (verified:
department block is prompt-only; KB bindings, RLS and ontology permissions unchanged).
Shared intelligence (ontology, metrics, NL2SQL, evidence, reporting, approvals, audit) is
consumed by department agents via authorized bindings. Rule preserved: **LLMs coordinate and
explain; deterministic services compute authoritative business figures** (NL2SQL contract,
metric binding gates, reviewer verification).

## 6. Test strategy

H1 is the defect-class item → reproduce first (adversarial test: cancel/abort mid-batch →
tree must show truthful terminal states, no phantom `running`), then fix, then re-run the
orchestration suites (p1_orchestration, p1p2_hybrid, p2_phase2_collaboration,
p3_execution_tree).

## 7. Authoritative lifecycle contract (H1 end-to-end closure, 2026-10-31)

### 7.1 State transitions — who owns which status

| Status | AgentInvocation (child rows) | Parent turn / batch result | A2A task state |
|---|---|---|---|
| `pending` | reserved (unused today — rows are born `running`) | — | `submitted` |
| `running` | set at delegation start (`open_invocation=True`) | parent is mid-tool-call (no final answer yet) | `working` |
| `completed` | terminal update in `_run_sub_agent.finally` | item `success=true` | `completed` |
| `partial` | (batch-level roll-up, not per-row) | batch status when ≥1 success + ≥1 failure | `completed` (with partial flag in message parts) |
| `failed` | exception / broker-block-after-start / **orphan recovery** | item `success=false` | `failed` |
| `timeout` | wall-clock cap hit (`asyncio.wait_for` cancel) | item `success=false, status=timeout` | `failed` (timed-out) |
| `cancelled` | parent cancelled (`CancelledError` path) | item `success=false, status=cancelled` | `cancelled` |
| `blocked` | broker refused (audited `_audit_block` row, never started) | item `success=false, status=blocked` | `rejected` (auth) |

Rules: **first terminal write wins** (`update_invocation_terminal` refuses non-terminal
targets and already-terminal rows); recovery may only move `running → failed` with
`output_json.recovery` metadata; `blocked` rows are born terminal (never ran).

### 7.2 Required vs optional child tasks

**Today every delegated child is REQUIRED by construction**: `_delegate_batch` awaits all
items and the batch returns only after the slowest resolves — the parent physically cannot
produce its final answer while a required child runs. The task contract has no `optional`
flag yet; introducing one is a documented Phase-3 extension. Policy when it arrives: optional
children run detached, are excluded from the batch roll-up, appear in the tree with their own
truthful status, and the parent's answer must state that optional work continues in the
background. (Deliberately NOT implemented now — no caller, no fabrication of need.)

### 7.3 Parent completion criteria / partial-result criteria

Parent may complete only when: the delegation tool call returned (all required children
terminal) AND synthesis succeeded. Batch status: all-success → `completed`; ≥1 success +
≥1 failure → `partial` (honest scenario-F semantics, `[PARTIAL RESULT]` prefix + machine-
readable per-item statuses); zero successes → `failed`. A parent-level timeout never implies
children completed — each child row carries its own terminal state recorded in its own
finally block, independent of the parent's fate.

### 7.4 Cancellation propagation

Parent cancel → `CancelledError` → `_run_sub_agent.finally` records the child `cancelled`
and releases the broker concurrency slot; the raising path propagates so the parent loop
ends without a final answer. DAG-node cancellation behaves the same via its own node
executor.

### 7.5 Crash recovery (H1-B — implemented)

Gap found during this closure: a worker crash/restart between the start-row insert and the
finally-block update left a `running` row forever (verified: no code path created `running`
rows before H1; after H1 they exist and could orphan). Fix:
`recover_orphaned_invocations(db, max_age_seconds=3600)` — `running` rows older than 1h
(delegation wall-caps are minutes) become **`failed`** with
`error_message="orphaned: worker terminated before completion …"` and
`output_json.recovery={reason: worker_lost, recovered_at, max_age_seconds}`. Never completed.
Idempotent (second pass recovers 0). Scheduled every 10 min via the existing scheduler
(single advisory-lock holder); tz-safe (SQLite naive vs aware normalized).

### 7.6 Parent-child synchronization invariants (H1-A) — verified by test

| Invariant | Evidence |
|---|---|
| Batch waits for the slowest required child (parallel, not serialized) | `test_batch_waits_for_all_required_children` (4 staggered children, 0.30s ≥ elapsed < 0.60s) |
| Mixed running/terminal tree during execution | `test_tree_shows_mixed_running_and_terminal_children` |
| Failed child → truthful partial parent outcome | `test_batch_status_failed_and_partial` (p1_orchestration) |
| Terminal never regresses (incl. late `failed` after `completed`) | `test_terminal_update_never_regresses`, `test_out_of_order_failed_after_completed_wins` |
| Orphaned running row → truthful recovery, fresh row untouched, idempotent | `test_orphaned_running_invocation_recovered`, `test_recovery_is_idempotent` |
| Frontend renders authoritative in-flight `running` (H1-D) | `AgentExecutionTree.test.jsx::renders an in-flight running node truthfully` |
| Duplicate SSE frames / reordering cannot mislead UI | `LiveActivityStream.test.jsx` (dedupeAndSortEvents) + tree-API priority over synthesized events |

### 7.7 Remaining limitations

- No optional-child contract flag (documented policy §7.2; Phase-3 extension).
- Recovery threshold is time-based (1h), not lease/heartbeat-based — adequate because
  delegation wall-caps (≤ COLLAB_TIMEOUT_SECONDS) are far below it; a heartbeat lease is
  unnecessary machinery today and would have to integrate with the broker's Redis/memory
  governance store.
- The DAG parallel-prepass writes child rows through the same `_run_sub_agent` path, so H1
  coverage applies there identically; DAG node-level statuses beyond invocation rows are the
  PlanNode model's own (pre-existing).
- Live multi-agent E2E remains BLOCKED (funded provider); all evidence above is
  integration-tested with deterministic delayed mocks — explicitly NOT live-verified.
