# P1 Implementation Progress — Automatic Enterprise Agent Orchestration

**Date:** 2026-10-31 · **Branch:** workspace working tree (uncommitted) · **Spec:** `zhanlu-enterprise-orchestration-p1-roadmap.md` (Phases 1–4 of this implementation; Phase 5 A2A deferred per its own flag-gated roadmap)

## Product principle

"Select a department. Ask anything. Zhanlu orchestrates everything." — the user never picks agents, skills or tools. Departments scope specialist DISCOVERY only; they never gate data access (KB bindings, RLS, permission policy unchanged).

## What shipped

### Phase 1 — Connect discovery to GA + agent-node dispatch

| Item | Where | Notes |
|---|---|---|
| `list_available_agents` in GA toolset | `backend/app/services/system_agents.py` (GA seed `enabled_tools`) | Live-verified against the container DB |
| Discovery enrichment | `backend/app/services/tool_handlers/delegate_tool.py::_list_available_agents` | Per published agent: `department`, `home_project`, `data_domains` (KB names, ≤5), `lifecycle_stage`, `business_owner`; `department` (case-insensitive) + `capability` filters; default department from `conv.metadata_.department`; returns `department_filter`. Schema gains both params |
| Plan-DAG agent-node dispatch | `backend/app/services/synexia/capability_router.py::_execute_single_node` (`node_type == "agent"`) | Routes through the SAME governed path (`execute_tool_with_retry("delegate_task", …)`) — no new orchestrator. Falls back task ← objective ← node.name; truncated children get `[PARTIAL — child hit its wall-clock budget]` prefix; failures recorded as failed observations |
| Heterogeneous batch delegation | `delegate_tool.py::_delegate_task/_delegate_batch` | `tasks` accepts strings or `{task, agent_name, max_iterations}` items — one agent per task. Batch check now PRECEDES the empty-task rejection (pre-existing production bug: every batch-only call failed with "task is required") |
| Child-result contract (Q3) | `delegate_tool.py` | Every result item carries machine-readable `status` (completed/partial/failed — forced via `setdefault` in `_delegate_batch`); batch-level `status` (failed > partial > completed) + `summary` ("N completed, M partial, K failed"); truncated child response carries unavoidable `[PARTIAL RESULT — …]` prefix |

### Phase 2 — Department metadata + context resolution + selector UI

| Item | Where | Notes |
|---|---|---|
| `projects.department` column | `backend/alembic/versions/110_project_department.py` (VARCHAR 120, nullable, indexed) | Applied to the live container DB (`alembic upgrade head`, container is at 111_metric_registry_governance) and model column in `backend/app/models/project.py` |
| Department context block | `backend/app/routers/agents.py::_append_department_block` | Reads `conv.metadata_.department`, appends an `ACTIVE DEPARTMENT: …` block to the GA system prompt on BOTH v2 and v3 prompt sites; instructs prefer-that-department-then-expand; exception-safe |
| Department selector UI | `frontend/src/components/chat/PlusMenu.jsx` (+ menu → "Select department"), `ChatInput.jsx` (blue chip with clear button), `pages/Chat.jsx` (`pendingDepartment` state, `?department=` deep-link round-trip, conv metadata write, departments list derived from non-archived projects) | Selection flows: chip → conv metadata → backend prompt block + discovery default filter. Project fetch is try/catch-guarded so partial base44 mocks / SDK failures never block chat |

### Phase 3 — Scenario harness (M2)

`backend/tests/test_p1_scenarios.py` — 5 golden scenarios, LLM boundary mocked, everything around it real:
- **A Production shortage** — cross-department plan-DAG (Inventory + Procurement agent nodes) → both dispatch to their own specialist.
- **B Quality investigation** — batch with a truncated child → `status=partial`, exact summary, `[PARTIAL RESULT]` prefix present.
- **C Monthly report** — department-scoped discovery default; capability filter composes as AND; cross-department expansion is EXPLICIT (`department` param) per the prompt rule "expand only when capabilities are absent".
- **D Failure honesty** — raising child → failed DAG observation (no fake success); error child → batch `status=failed`.
- **E Cross-department batch** — one batch, one agent per department, per-item attribution preserved.

**Live E2E (real LLM) remains blocked on the P0 provider-quota issue (429 insufficient_quota) — external billing action.**

### Phase 4 — Parent-child execution tree + evidence inspection

| Item | Where | Notes |
|---|---|---|
| delegate_task gets the sub-agent branch badge | `backend/app/routers/agents.py` (both `tn.startswith("ask_")` gates → `or tn == "delegate_task"`) | `subagent_invoked` / `subagent_returned` now fire for delegate_task → the existing `LiveActivityStream` branch renderer shows the delegation header with outcome + duration. Per-child activity steps ("{agent} #N: task") already stream via `progress_emitter` |
| Evidence inspection card | NEW `frontend/src/components/chat/DelegateBatchCard.jsx`, wired into `MessageBubble.jsx` | Renders the persisted delegate_task tool_call payload: specialist count, batch `summary` chip, per-item status badges (with legacy fallback from success/truncated), amber warning banner when any child is partial/failed, expandable full child response. Defensive JSON.parse for stringified results |

## Tests

- `backend/tests/test_p1_orchestration.py` — 12 passing (toolset, enrichment/filters, batch dispatch, normalization, statuses, prefix, agent-node dispatch/failure, department block).
- `backend/tests/test_p1_scenarios.py` — 5 passing (A–E).
- `backend/tests/test_multiagent_mas.py::test_batch_fanout_is_bounded` — updated to the new `_delegate_batch(tasks: list[dict], db, user_id, endpoint, context)` signature (missed during the P1 signature change; concurrency bound still asserted).
- Frontend: `DelegateBatchCard.test.jsx` 6 passing; ChatInput/PlusMenu window-regex tests widened (contract unchanged); full suites: 1052 passed / 28 failed — **all 28 pre-existing** (files untouched this session: translations key drift, ProjectDetail data_map/recentChats, steer wiring vs. steerMarkers removal 2026-08-31, previewToken, Sidebar missing PersistentStreamProvider, LiveActivityStream pickEvents).
- Backend regression: 309 passed / 4 skipped (manufacturing golden, tenant/RLS/auth, ontology ×4, MAS, P1, nl2sql).
- Frontend production build: green.

## Live verification (container, post-restart)

- GA `enabled_tools` contains `list_available_agents` ✓
- `projects.department` column present ✓
- Discovery returns 16 live agents with enrichment ✓
- `DELEGATE_TASK_SCHEMA.tasks` = anyOf[string, {task, agent_name, max_iterations}] ✓; `LIST_AVAILABLE_AGENTS_SCHEMA` has department + capability ✓
- Live LLM orchestration turns: **blocked by provider quota (P0, external)**

## Engineering-rule compliance

- One execution runtime (ReAct), one DAG executor, one delegation mechanism, one discovery mechanism — everything extended, nothing duplicated.
- No error swallowed silently: all new paths either surface failures in results (`status`/`error`) or log; UI fetches fail open with an empty picker (never blocks chat — that is a deliberate product decision, documented in-code).
- Feature flags: none added — all changes are additive and backward-compatible (schema `anyOf` accepts legacy string tasks; batch results are a superset; department is nullable).

## Rollback

| Change | Rollback |
|---|---|
| GA discovery tool | Remove `"list_available_agents"` from the GA seed row's `enabled_tools` (DB) — no code revert needed |
| Department block | Delete the two `_append_department_block(system_prompt, conv)` call sites in `agents.py` (v2 ~L7238, v3 ~L13325); helper is inert otherwise |
| Heterogeneous batch | Legacy string `tasks` items still work; to fully revert, restore `_delegate_batch`'s old single-agent signature + `test_multiagent_mas.py` original call |
| Agent-node dispatch | Remove the `elif node_type == "agent":` branch in `capability_router._execute_single_node` (falls through to tool node, the pre-P1 behavior) |
| Department column/UI | `alembic downgrade -1` past 110 (inspector-guarded); hide the chip by removing the `pendingDepartment` props from the two `<ChatInput>` render sites in `Chat.jsx` |
| delegate_task badges | Revert the two gate conditions in `agents.py` to `tn.startswith("ask_")` |
| Evidence card | Remove the `delegateBatchResult` block + `<DelegateBatchCard>` render in `MessageBubble.jsx`; delete `DelegateBatchCard.jsx` |

## Known open items

- Provider quota funding → then rerun live golden E2E + AV smoke + PILOT_E2E (P0 leftover).
- Phase 5 A2A federation — separate flag-gated roadmap (`zhanlu-a2a-integration-design.md`), deliberately not started.
- `department` values are free text today; a controlled vocabulary can be added later without schema change (VARCHAR indexed).
- Pre-existing frontend test failures (28) predate this work — separate cleanup.

---

# P1/P2 Hybrid Extension (2026-10-31, same branch)

Adds the **Hybrid** layer to the prior P1 foundation: Pattern C peer-to-peer
collaboration, the structured **Agent Task Contract**, the **Collaboration
Broker** (logical service inside the existing `delegate_task` runtime — no new
orchestrator/registry), and governance (cycle detection, depth/child-count/
concurrency/timeout/token budgets, duplicate prevention, audit).

## New module — `backend/app/services/agent_collaboration/`

- `__init__.py` — package doc (broker is a logical service, not a new runtime).
- `contract.py` — `AgentTaskContract` dataclass + `build_contract(...)`. Reuses
  existing canonical IDs (conversation_id, organization_id, user_id, project_id,
  invocation/trace ids); adds negotiation fields (task_id, parent_task_id,
  requesting_agent_id, target_agent_id, delegation_path, status,
  execution_budget, deadline). **The user's identity is never replaced by the
  requesting agent's** (spec §3 / §8).
- `broker.py` — `evaluate_delegation(target, objective, context, db,
  parent_invocation_id) -> (allowed, reason, extended_context)`. Pure policy
  layer (LLM-free, unit-testable) enforcing, in order:
  1. target publication status (only `status=="published"` AgentApps delegate; builtin agents legacy-allowed);
  2. **duplicate-task prevention** (same requester+target+objective in the threaded context);
  3. **cycle detection** (target already on `delegation_path` → block, no crash);
  4. **depth limit** (`COLLAB_MAX_DEPTH`, caps runaway recursion);
  5. **child-count per parent** (`COLLAB_MAX_CHILDREN_PER_PARENT`, no unbounded sub-teams);
  6. requesting-agent allowlist (`COLLAB_DELEGATORS_ALLOWLIST`, empty = all).
  On block, records an observable `invocation` row (`status="blocked"`) via
  `_audit_block` — fail-closed + audited (spec Scenario 7). Returns an extended
  `context` (path extended, requesting/parent/trace ids stamped) so NESTED
  Pattern-C delegations are themselves governed.

## Governance settings (`backend/app/config.py`, all additive/opt-out)

`COLLAB_POLICY_ENFORCED` (master kill-switch → pure legacy delegation),
`COLLAB_MAX_DEPTH=4`, `COLLAB_MAX_CHILDREN_PER_PARENT=8`,
`COLLAB_MAX_CONCURRENT=4` (reuses `DELEGATE_MAX_CONCURRENT`),
`COLLAB_TIMEOUT_SECONDS=600.0` (reuses `DELEGATE_WALL_SECONDS`),
`COLLAB_TOKEN_BUDGET=0` (soft), `COLLAB_DEDUP_ENABLED=True`,
`COLLAB_CYCLE_DETECTION=True`, `COLLAB_DEDUP_ENABLED`,
`COLLAB_DELEGATORS_ALLOWLIST=""`.

## Integration points

- `delegate_tool._run_sub_agent` — calls `evaluate_delegation` BEFORE spawning
  the child. On block returns `{"success": False, "status": "blocked", ...}`
  (no silent swallow, no crash). On allow, passes the extended `context` down
  and records the invocation with `parent_invocation_id`, `trace_id`,
  `delegation_mode` ("peer" when requesting ≠ general_assistant) so the
  parent→child link is observable. Forces a machine-readable `status` on EVERY
  single-task result (was batch-only before) — consistent contract.
- `_run_sub_agent_inner` — threads the governance lineage
  (`delegation_path`, `requesting_agent_id`, `parent_invocation_id`, `trace_id`,
  identity ids, `_collab_children`/`_collab_seen`) into the child context so a
  specialist-initiated `delegate_task` (Pattern C) is itself governed. **SECURITY:
  only lineage keys are copied — never permissions or agent identity override.**
- DAG agent-node dispatch (`capability_router._execute_single_node`, "agent"
  branch) already routes through `delegate_task` → broker, so it is governed.
- Evidence card + department block + discovery enrichment from the prior P1
  work remain; they are the UI surface for the orchestration above.

## Tests

- `backend/tests/test_p1p2_hybrid.py` — **12 passing**: Scenario 1 (single
  specialist, no fan-out), Scenario 3 (Supply Chain → Inventory peer via
  broker, requesting-agent captured), Scenario 5 (A→B→A cycle blocked + depth
  limit), Scenario 7 (unpublished target blocked + audited), broker pure-policy
  checks (allow/builtin, unpublished block, cycle, child-count, duplicate,
  user-identity preservation, master kill-switch).
- Combined regression: **319 passed / 1 skipped** (P1 + scenarios + hybrid +
  MAS + ontology ×4 + tenant/RLS/auth + nl2sql). Frontend build green.

## Live verification (container, post-restart)

- `broker.evaluate_delegation` ALLOW for a published target (path extended);
  CYCLE BLOCK when target already on the path — both confirmed against live DB.
- Live LLM end-to-end orchestration still BLOCKED on provider quota (P0).

## Rollback additions

| Change | Rollback |
|---|---|
| Collaboration Broker | Set `COLLAB_POLICY_ENFORCED=False` (one switch → pure legacy delegation); or delete the `evaluate_delegation` call + early-return in `_run_sub_agent` and the lineage-copy block in `_run_sub_agent_inner` |
| New settings | Remove the COLLAB_* block from `config.py` (all have safe getattr defaults in code) |
| Agent Task Contract | Delete `agent_collaboration/` package; no DB columns added (reuses existing IDs) |

## Remaining P2/P3 (not started this pass)

- P2: critic/reviewer workflows, conditional specialist collaboration, richer
  execution-tree visualization (existing LiveActivityStream + DelegateBatchCard
  already cover the basic tree + evidence).
- P3: A2A external federation — separate flag-gated roadmap.
- Provider quota funding → live golden E2E for all 7 enterprise scenarios.

---

# Hybrid Production-Readiness Audit & Targeted Hardening (2026-10-31, same branch)

Audit of the Hybrid layer against actual source (no rewrite). Confirmed defects
fixed; remaining items classified PASS/PARTIAL/FAIL/DEFERRED.

## Confirmed defects fixed (targeted only)

1. **Unreachable dead duplicate code** — `_run_sub_agent` had 57 lines of
   duplicated function body AFTER a `return result` (lines 442–498). Removed.
   No behavior change; eliminates a maintenance/divergence hazard.
2. **Dedup + child-count never shared** — `evaluate_delegation` did
   `ctx = dict(context)`, so `_collab_seen`/`_collab_children` lived on a
   per-call COPY. Sibling batch children and concurrent calls each got a fresh
   copy → `COLLAB_MAX_CHILDREN_PER_PARENT` never bounded batch fan-out and
   dedup never collided across siblings. **Fix:** per-conversation shared store
   (`_CONV_STATE` keyed by `conversation_id`, threading.Lock per conv). Within a
   conversation, sibling + nested delegations now share the counter/seen set.
   Conversations without an id stay ephemeral (intentionally NOT cross-deduped).
3. **Race condition (TOCTOU)** on dedup/child-count under concurrency — now
   serialized under the per-conversation lock.
4. **`COLLAB_MAX_CONCURRENT` was a no-op** — never referenced. **Fix:** added a
   deadlock-free global top-level concurrency gate (`acquire_concurrency`/
   `release_concurrency` in broker; wired into `_run_sub_agent`). Only
   TOP-LEVEL (supervisor-initiated) delegations consume the budget; nested
   Pattern-C delegations are excluded (bounded by depth/children/dedup) so an
   ancestor slot can never deadlock its own descendant.
5. **`COLLAB_TIMEOUT_SECONDS` was a no-op** — inner loop read only
   `DELEGATE_WALL_SECONDS`. **Fix:** inner now prefers `COLLAB_TIMEOUT_SECONDS`
   when `COLLAB_POLICY_ENFORCED` (mirrors 600 by default).
6. **Single long call escaped the wall-clock** — the final-text early-return at
   iteration 0 bypassed the post-loop `_wall_exceeded` check, so a 0.2s call
   under a 0.01s budget was reported `completed`. **Fix:** if wall exceeded by
   the single call, break so the unified return marks `truncated=True`/
   `status=partial`.
7. **Contract dropped before child execution + never recorded** — `agent_task_
   contract` was stamped on context but not copied into the nested child context
   and not persisted. **Fix:** (a) `_run_sub_agent_inner` now threads
   `agent_task_contract`; (b) the sub-agent invocation record includes
   `output_json.task_contract` (auditable parent→child link).

## Audit classification (per spec §8)

| Capability | Status | Enforcement point | Notes |
|---|---|---|---|
| Agent Task Contract built | PASS | `broker.build_contract` | all §3 fields; user identity preserved |
| Contract threaded to child + recorded | PASS (fixed) | `_run_sub_agent_inner` copy + `record_invocation` | observable in invocations |
| Runtime still passes dicts | PASS (by design) | `context` param | no duplicate registry; contract is the governance source of truth |
| Cycle detection | PASS | broker (pre-dedup) | A→B→A blocked, no crash |
| Depth limit | PASS | broker `len(path) >= MAX_DEPTH` | A→B→C→D→E blocked |
| Child-count per parent | PASS (fixed) | per-conversation shared store | now bounds batch fan-out |
| Duplicate prevention | PARTIAL→PASS | per-conversation shared store + lock | sequential + concurrent within a conversation; **NOT** across conversations (intentional — no DB persistence, per audit §4) |
| Max concurrent tasks | PARTIAL→PASS | global top-level gate (new) | real now; nested excluded (deadlock-free) |
| Execution timeout | PASS (fixed) | inner wall-clock (`COLLAB_TIMEOUT_SECONDS`) | single-call truncation fixed |
| Token/cost budget | DEFERRED → **PASS (fixed, 2026-10-31 Phase 1)** | broker `conv_token_total` + `delegate_tool` metering | now MEASURED (`token_usage` + `cost_amount` populated per sub-agent) AND ENFORCED (cumulative per-conversation; blocks further delegation once budget already consumed). Unknown model → cost `None`. Zero budget = no cap (unchanged). |
| Unpublished target block | PASS | broker `_target_publication_state` | fail-closed + audit row |
| User identity authoritative | PASS | contract + child context copies user_id/org/project | verified: child runs as itself, with USER's id |
| No privilege escalation via nested delegation | PASS | P0-2 (parent agent_name never inherited) + broker policy | child permission-checked as itself |
| Cross-department auth | PASS (by design) | RLS on child run | GA passes department; child uses its own bindings |
| Blocked attempts auditable | PASS | `_audit_block` (policy) + invocation record (concurrency) | status="blocked" rows |
| Prompt cannot override auth | PASS | governance is runtime, not prompt-driven | |
| Failure: timeout/exception/invalid-output/quota | PASS | try/except + finally records | honest failure, no false success |
| Partial completion | PASS | batch summary + per-item status | verified |
| Parent cancellation | PASS | asyncio finally records; no false success | (no explicit cancel status) |
| Child cancellation | PASS | CancelledError propagates, finally runs | |

## Tests

- `tests/test_p1p2_hybrid_hardening.py` (NEW, 10 adversarial tests, LLM mocked
  only): circular A→B→A; depth-exceeding chain; per-parent child limit;
  concurrent duplicate dedup race; global concurrency gate across conversations;
  cross-project identity preservation; child timeout survives; partial batch;
  provider quota exhaustion; invalid/incomplete evidence.
- Regression: **331 passed / 4 skipped** (manufacturing golden, tenant/RLS/auth,
  ontology ×4, MAS, P1, scenarios, hybrid ×2, nl2sql). Frontend build green.
- All 10 hardening tests pass; 22 hybrid + 10 hardening.

## Deferred (intentional, not defects)

- **Cross-conversation dedup / persisted idempotency** — would need DB; audit §4
  says establish idempotency guarantees first; current per-conversation scope is
  the deliberate boundary (enforced by `governance_store` keyed on
  `conversation_id`; ephemeral callers stay isolated by design).
- **A2A external federation (P3)** — untouched, separate roadmap.

> **Note (2026-10-31, Phase 1):** the token/cost budget item previously listed
> here as deferred is now IMPLEMENTED — see the "P2 Advanced Hybrid —
> Production Hardening (Phase 1)" section below. It was moved out of DEFERRED
> because consumption is now measured (provider `usage` surfaced through
> `call_llm_with_reliability`), so the audit §3 "don't claim enforcement without
> measurement" condition is satisfied.

---

# P2 Advanced Hybrid — Production Hardening (Phase 1) (2026-10-31, TDD)

Three audit-flagged gaps that the prior audit classified as DEFERRED are now
closed with test-driven development. This is **Phase 1 of the P2 mandate** — the
production-hardening prerequisite foundation. Phases 2–5 (critic/reviewer
workflows, conditional peer collaboration, shared evidence, teams,
cross-department, execution-tree visualization) are **NOT started** (see the
honest status at the end).

## Part 1.1 — Distributed governance (no longer in-process only)

New module `backend/app/services/agent_collaboration/governance_store.py`.

* `GovernanceStore` abstraction with two impls: `_RedisStore` (when Redis is
  reachable via the existing `get_redis()` client) and `_InProcessStore`
  (single-process fallback for dev/tests — never fails open on a missing Redis).
* State previously held in process-local dicts (`_GLOBAL_ACTIVE`, `_CONV_STATE`)
  in `broker.py` is now owned by the store. `broker.acquire_concurrency` /
  `release_concurrency` / `global_active_count` / `reset_concurrency` /
  `reset_conv_state` delegate to it.
* `get_store()` is a process-wide singleton; `reset_store()` is a test helper.
* `broker` now resolves `_conv_id = ctx.get("conversation_id") or
  f"_ephemeral_{id(ctx)}"` so ephemeral callers stay isolated (intentional).
* **Atomicity** (Redis path):
  * global slot acquire = one Lua script (`SCARD >= limit ? 0 : SADD + EXPIRE`)
    — no TOCTOU between the count check and the add across workers.
  * dedup = `SADD` returns the added-count (1 = new, 0 = duplicate); fail-open.
  * child-count = `HINCRBY` per requesting agent; fail-open.
  * tokens = `INCRBY` per conversation; fail-open.
* **Owner-scoped lease recovery**: every acquired global slot carries a unique
  `uuid4` token; `release_global_slot` only removes its own token. All keys carry
  a 3600s TTL so a crashed worker's reservation expires automatically instead of
  pinning the counter.
* **Fail-safe semantics**: acquire errors → **fail-closed** (refuse new
  top-level work); soft-guard (dedup/counter/token) errors → **fail-open** (do
  not block real work on a cache hiccup, logged as warning).

## Part 1.2 — Real child cancellation (not silent truncation)

`delegate_tool._run_sub_agent` now wraps the child coroutine in:

```python
_child = await asyncio.wait_for(
    _run_sub_agent_inner(...), timeout=_wall_s)
except asyncio.TimeoutError:
    _child = {"success": False, "status": "timeout", ...}
```

* `asyncio.wait_for` **cancels the in-flight child coroutine** (interrupting an
  awaiting LLM/HTTP call) — previously the parent only truncated its own view
  while the child kept running. The inner wall-clock check stays as
  defense-in-depth.
* `asyncio.CancelledError` is caught, recorded honestly as
  `status="cancelled"`, and **re-raised** so a parent/worker shutdown is never
  silently swallowed as a success.
* `record_invocation` status now maps to `timeout`/`cancelled`/`failed`/
  `completed` (no fabricated success on abnormal exit).

## Part 1.3 — Token metering + budget enforcement (measured AND enforced)

* `sub_agent_reliability.call_llm_with_reliability` now returns real
  `usage{prompt_tokens, completion_tokens, total_tokens}` + `model` (never
  invented).
* `_run_sub_agent_inner` accumulates per-LLM-call tokens into `_usage` and the
  model list; attaches `token_usage` to both the early final-text return and the
  final return.
* On completion it writes cumulative tokens to the per-conversation store
  (`get_store().conv_token_add(conv_id, total_tokens)`) — best-effort, only when
  a `conversation_id` is known (ephemeral stays isolated).
* `_compute_child_cost(token_usage)` derives an estimated `cost_amount` from
  `cost_service.TOKEN_COSTS`; returns `None` for an unknown model or absent
  model (measured where reliable, estimated otherwise — never guessed).
* `record_invocation` now persists `token_usage` + `cost_amount` on the
  sub-agent invocation row (observable in the invocations drawer).
* `broker.evaluate_delegation` enforces the budget: once the per-conversation
  cumulative (`_store.conv_token_total`) has **already reached** `token_budget`
  it blocks further delegation with `status="blocked"` (preserves completed
  findings, no fabrication, no silent over-spend). Zero budget = no cap.
* `AgentTaskContract.token_budget` threaded into the nested child context so a
  Pattern-C specialist-initiated delegation is itself budget-scoped.

## Tests

* `backend/tests/test_p2_production_hardening.py` (NEW, 10 tests, LLM mocked
  only): in-process store semantics; Redis store semantics (2 tests are
  Redis-specific and **skip** where Redis is not authenticated/usable, e.g. a
  stray local Redis — they run and pass against the authed container Redis);
  1s-TTL lease recovery; true cancellation timeout; parent-cancel propagation
  into child; token surfacing + recording; unknown-model cost = None;
  budget-exhaustion blocks delegation.
* `test_p1p2_hybrid_hardening.py` updated: `test_adversarial_child_timeout_
  parent_survives` now expects `status=="timeout"` (not `partial`); helpers use
  `wall_seconds=None`; `_GLOBAL_ACTIVE` → `broker.get_store().global_active_count()`.
* `test_p1p2_hybrid.py`: added `autouse` `_reset_governance_state` fixture
  (reset_store + broker resets) to fix Redis-backed state leaking across tests;
  fake-inner patches gained `wall_seconds=None`.
* Final regression after Phase 1: **340 passed / 4 skipped** (manufacturing
  golden, tenant/RLS/auth, ontology ×4, MAS, P1, scenarios, hybrid ×2,
  p2-hardening, nl2sql) — verified in the authed container. Locally (dev box
  with a stray unauthenticated Redis), the p2 module shows 7 passed / 2 skipped
  (the 2 Redis-specific tests skip by design) and the hybrid + hardening suites
  pass 22/22; `get_store()` now degrades to the in-process store when Redis is
  reachable-but-unusable, so the hard slot gate no longer fails-closed against a
  misconfigured Redis. Frontend `vite build` green; backend container
  restarted. Live sanity (container): `store type: _RedisStore`, redis
  reachable True, `cost gpt-4o: 0.00075`, unknown → None.

## Honest status — Phases 3–5 of the P2 mandate (NOT started)

Phase 1 (production hardening) and **Phase 2 (intelligent collaboration)** are
both implemented and covered by tests. The remaining P2 roadmap items are
**unstarted** and should be scoped as separate work:

* **Phase 3** — richer shared-evidence surface beyond the current
  `DelegateBatchCard` + `LiveActivityStream`.
* **Phase 4** — execution-tree / collaboration visualization.
* **Phase 5** — teams, cross-department collaboration UX; A2A external
  federation (separate flag-gated roadmap).

### Known deferred items still open
* Per-agent default token-budget config knob (today the budget is global
  `COLLAB_TOKEN_BUDGET` + contract-supplied; a per-agent default is not wired).
* Hard budget under a concurrent batch is soft (enforcement is pre-delegation on
  cumulative consumption; a burst of concurrent children can collectively
  overshoot before any one observes the limit — acceptable for the current
  single-supervisor fan-out, revisit if nested batching is enabled).
* Live LLM end-to-end orchestration still BLOCKED on provider quota (P0,
  external billing action).

---

# P2 Advanced Hybrid — Intelligent Agent Collaboration (Phase 2) (2026-10-31, TDD)

Builds directly on Phase 1's governance/timeout/token foundation. Implements
the four collaboration capabilities and the seven enterprise acceptance
scenarios from the mandate, **without** a new orchestration runtime or a
duplicated broker — all of it extends the existing Collaboration Broker, the
`delegate_task` runtime, and the Plan-DAG executor (`capability_router`).

Guiding principle (from the mandate): *General Assistant supervises,
specialists collaborate, reviewers challenge, evidence validates, and Zhanlu
delivers one trustworthy enterprise answer.*

## Part 1 — Critic / Reviewer workflow

New `backend/app/services/agent_collaboration/reviewer.py`.

* `review_findings(question, content, evidence_items, endpoint)` runs a
  **fact-safe** LLM judge via `chat_completion_json_sync` against a strict
  `REVIEW_SCHEMA`. The judge is instructed to **only** assess the material
  provided (question + answer + evidence references) and to **never invent
  facts, figures, or database results** — missing information is flagged as a
  GAP under `needs_evidence`, never fabricated.
* Seven evaluation dimensions, each `ok|warn|fail`:
  `numerical_accuracy`, `evidence_completeness`, `source_attribution`,
  `contradictory_findings`, `unsupported_conclusions`, `assumptions_uncertainty`,
  `question_answered`. Plus `overall` (`approved|needs_revision|rejected`),
  `issues[]`, and `needs_evidence[]` (targeted gaps).
* Robustness: if the judge call is unavailable/unparseable, the reviewer returns
  a **conservative** report (`needs_revision`, all dims `warn`) — it never
  approves fabricated content.
* **Selective invocation**: the reviewer is opt-in. The caller sets
  `delegate_task(review=True)` (or uses a DAG `reviewer` node). Simple questions
  never trigger it. `COLLAB_REVIEW_ENABLED` gates availability;
  `COLLAB_REVIEW_MAX_RETRIES` bounds the follow-up loop.
* **Targeted re-investigation**: when `needs_evidence` is non-empty, the
  orchestrator issues **bounded** (≤ `COLLAB_REVIEW_MAX_RETRIES`, ≤2 gaps)
  targeted delegations through the governed `delegate_task` path to fill the
  gaps, then re-reviews once. No review loops.

## Part 2 — Conditional peer collaboration

* The broker already governed nested delegation (cycle/depth/child-count/dedup).
  What was missing: children were barred from calling `delegate_task` by
  `_RESTRICTED_TOOLS`.
* `delegate_tool._run_sub_agent_inner` now **exposes `delegate_task` to
  children** when `COLLAB_NESTED_DELEGATION_ENABLED` (default True) **and**
  `COLLAB_POLICY_ENFORCED` are on. The broker still governs every nested call, so
  exposing the tool does **not** relax governance — it only enables
  specialist→specialist requests (Pattern C). Set the flag False to restore the
  legacy delegation-free children.
* A specialist requests help from a peer; the broker validates, enforces
  governance, and dispatches. The requesting specialist receives structured
  results and completes its investigation. The General Assistant remains
  responsible for the original user request and final answer.

## Part 3 — Shared evidence (reusable, by reference)

New `backend/app/services/agent_collaboration/shared_evidence.py` (mirrors
`governance_store`: Redis-backed when reachable+authenticated, in-process
fallback otherwise — keyed per `conversation_id`).

* `EvidenceItem` carries everything needed to trust a finding **without
  re-running the query**: `producer_agent`, `source_id`/`datasource_id`,
  `query_provenance`, `metric_definition_ids`, compact `findings`, `freshness`,
  `assumptions`, `limitations`, `validation_status`, `references` (upstream
  evidence ids).
* `provide_evidence(...)` creates one item and returns an `evidence_id`;
  `request_evidence_references(conv_id, topic)` returns **compact references**
  (producer, source, validation status, findings *summary*) — **never raw rows**.
* Two tools, `provide_evidence` / `request_evidence`, are registered and always
  available to sub-agents (added to the child toolset). They are the ONLY way a
  sub-agent contributes/consumes shared evidence.
* Honesty rule enforced by design: an unsupported textual statement does NOT
  become verified evidence merely because another agent repeats it — only the
  *producing* agent creates the item, and its `validation_status` reflects what
  that producer actually did. Each child's `evidence_ids` are surfaced on its
  result so the parent/orchestrator can reference them.

## Part 4 — Collaboration workflow (DAG + broker extensions)

All in the existing `capability_router.execute_plan_nodes` / `_execute_single_node`
and the broker — no new runtime:

* **Conditional delegation**: a node may carry `run_if` in its `inputs`
  (no schema migration). `_node_condition_met` supports
  `requires_evidence_topic` (skip if no peer evidence matches) and
  `requires_upstream_status` (skip if a named upstream node didn't reach the
  required status). An unsatisfied condition → node `skipped` (not failed), so
  dependents simply never receive its output.
* **Parallel / dependent specialist tasks**: already supported by the existing
  Plan-DAG (edges + `_parallel_ready_batch_prepass`); unchanged.
* **Reviewer stage**: new `node_type == "reviewer"`. It aggregates upstream
  findings (`node.inputs["content"]`) + the execution's shared evidence, runs
  `review_findings`, and if it reports missing evidence, dispatches **bounded**
  targeted follow-up delegations through the governed path before re-reviewing.
* **Targeted evidence requests**: realised by the reviewer node's follow-up
  delegations + the `request_evidence` tool.
* **Result aggregation**: Plan-DAG already merges node observations; the
  reviewer node aggregates the targeted findings into the final report.
* **Partial completion**: a failed node does not abort the run (unchanged);
  the run surfaces completed + failed honestly (e.g. `status == "partial"` in
  `_delegate_batch`).
* **Human approval when required**: if an agent node's result carries
  `requires_approval`, `_execute_single_node` **surfaces** it and **pauses** the
  run (returns early) so downstream nodes don't execute on unapproved output. The
  existing chat-loop approval/resume flow handles the rest.

## Part 5 — Enterprise acceptance scenarios

Covered by `tests/test_p2_phase2_collaboration.py` (LLM mocked; governance,
registry, reviewer, DAG run for real) + the locally-runnable
`tests/test_p2_phase2_core.py` (modules that don't require `app.models`):

* **A** Manufacturing root-cause (Quality + Maintenance + Production) via
  plan-DAG — partial completion when one specialist fails.
* **B** Supply Chain requests Inventory Agent assistance (Pattern C) + evidence
  shared by reference.
* **C** Reviewer detects an unsupported numerical claim (returns
  `needs_revision` + the failing dimension).
* **D** Reviewer requests additional evidence → bounded targeted
  re-investigation occurs.
* **E** Finance + Sales collaborate on vehicle profitability (evidence + review
  → `approved`).
* **F** One specialist fails while others return partial findings (batch stays
  honest: `status == "partial"`).
* **G** Duplicate and circular collaboration attempts are blocked by the broker
  (dedup + cycle detection).

## Preserved (engineering requirements)

Phase 1 governance (Redis coordination + in-process fallback), timeout/cancellation
(`asyncio.wait_for`), token accounting (`token_usage`/`cost_amount`), project &
tenant isolation (`conversation_id`/ephemeral scoping), user identity, and
invocation tracing (`record_invocation`) are all unchanged and still exercised.
No new orchestration runtime; the broker is extended, not duplicated. No external
A2A federation. The permanent department-agent catalog is not expanded (specialists
collaborate via `delegate_task` to existing published AgentApps).

## Tests

* `tests/test_p2_phase2_core.py` (NEW, 10 tests) — runs locally: evidence
  registry (store/get/find/isolation/reference-not-raw), evidence tools, reviewer
  fact-safety (structured verdict, unsupported-claim detection, conservative on
  parse failure), broker governance for peer collaboration (duplicate/circular
  blocked, nested peer allowed). **All 10 pass locally.**
* `tests/test_p2_phase2_collaboration.py` (NEW) — full integration covering
  A–G + reviewer-wired `delegate_task(review=True)` + DAG reviewer/conditional/
  approval nodes. Runs in the correct runtime (needs `app.models`, i.e. a
  SQLAlchemy build that exports `sqlalchemy.orm.inspect`).

## Phase 2 status — three engineering states (Zhanlu rule)

Per the Zhanlu engineering rule **implemented ≠ tested ≠ production-ready**,
every collaboration scenario is tracked as three separate states:

| Scenario | Implemented | Integration-tested | Production-ready |
|---|---|---|---|
| A — Manufacturing root-cause (parallel specialists + partial completion) | ✅ | ✅ (`test_scenario_a_root_cause_partial_completion`) | ❌ (live LLM quota-blocked) |
| B — Supply Chain → Inventory by reference (Pattern C + evidence) | ✅ | ✅ (`test_scenario_b_peer_evidence`) | ❌ (live LLM quota-blocked) |
| C — Reviewer flags unsupported claim | ✅ | ✅ (`test_scenario_c_reviewer_flags_unsupported`) | ❌ (live LLM quota-blocked) |
| D — Reviewer requests evidence → bounded re-investigation | ✅ | ✅ (`test_scenario_d_reviewer_requests_evidence_dag` + `test_review_triggers_targeted_reinvestigation` via real `delegate_task`) | ❌ (live LLM quota-blocked) |
| E — Finance + Sales collaborate → approved | ✅ | ✅ (`test_scenario_e_reviewed_collaboration`) | ❌ (live LLM quota-blocked) |
| F — One specialist fails, others partial (honest `partial`) | ✅ | ✅ (`test_scenario_f_batch_partial_honesty` + `test_p1_scenarios::test_scenario_d_failures_are_explicit`) | ❌ (live LLM quota-blocked) |
| G — Duplicate / circular collaboration blocked | ✅ | ✅ (`test_duplicate_delegation_blocked`, `test_circular_delegation_blocked`, `test_nested_peer_collaboration_allowed`) | ✅ (pure governance, no LLM) |

"Production-ready" is gated on **live** LLM execution in the deployment runtime
(reviewer + delegate LLM calls). G is the exception — it is pure broker
governance and needs no LLM.

## Known risks / open items

* **SQLAlchemy diagnosis corrected (2026-10-31)**: earlier note claimed a broken
  `sqlalchemy.orm.inspect` export. That was WRONG — `app/models/metric_definition.py`
  already imports `from sqlalchemy import inspect` (top-level, always present in
  SQLAlchemy 2.0.x), not `from sqlalchemy.orm import inspect`. The transient
  `app.models` import failure was caused by `pip` re-install churn during
  diagnosis, not by the code. `app.models` imports cleanly in the standard
  SQLAlchemy 2.0.36 install; the full backend suite runs locally. No code change
  was required for this.
* Per-agent default token-budget config knob remains unstarted (global
  `COLLAB_TOKEN_BUDGET` + contract-supplied today).
* Hard budget under a concurrent batch is still soft (pre-delegation on
  cumulative consumption).
* Live LLM end-to-end orchestration still BLOCKED on provider quota (P0).
* Reviewer is advisory/single-pass (bounded follow-ups); not a multi-round
  adversarial debate (that is a Phase 3+ concern).
* Pre-existing (NOT a Phase 2 regression): `test_capability_router_fixes.py::
  test_topological_sort_detects_cycle` / `test_topological_sort_detects_self_loop`
  assert `ValueError` on a cycle, but `_topological_sort` was intentionally
  changed (FIX 2026-09-22) to **break** cycles instead of raising. The test was
  never updated. Out of scope for Phase 2; flagged for follow-up.

---

# P2 Phase 3 — Enterprise Multi-Agent Execution Visualization (2026-10-31)

**Spec:** "ZHANLU P2 PHASE 3 — ENTERPRISE MULTI-AGENT EXECUTION VISUALIZATION".
**Goal:** make the backend's multi-agent intelligence **visible, understandable,
interactive, and trustworthy** to enterprise users — *without* rebuilding the
backend, creating new execution-tracking, or new event infrastructure.

**Engineering constraints (honored):** reuse existing `LiveActivityStream`
(`attachSubagentParent` / `groupIntoBranches`), existing `AgentInvocation`
records, the shared evidence registry (`provide_evidence` / `get_evidence` /
`get_registry`), the existing executions router/broker, and the existing
`DelegateBatchCard` evidence path. No duplicate infra, no new event types,
tenant/project isolation preserved, backward-compatible, no new feature flags.

## What shipped

### Backend — tree + evidence REST (no new event infrastructure)

| Item | Where | Notes |
|---|---|---|
| Reviewer verdict + evidence ids persisted on sub-agent invocation | `backend/app/services/tool_handlers/delegate_tool.py` (~L600) | `output_json` now carries `"review": result.get("review")` and `"evidence_ids": result.get("evidence_ids")` so historical runs are fully visualizable |
| Execution-tree builder (13 viz states) | `backend/app/services/agent_invocations.py` | `AGENT_TREE_VIZ_STATES`, `_resolve_agent_names`, `_node_phase`, `_row_to_node`, `_rows_to_tree`, `build_agent_tree(*, conversation_id, user_id, awaiting_approval=False)`, `_summarize(roots)`. Tree built from the **existing** `AgentInvocation.span_id` parent links (no `parent_invocation_id` column added); ordered by `started_at`; filters `is_deleted==False` |
| Summary roll-up | `backend/app/services/agent_invocations.py::_summarize` | total / completed / partial / failed / peer_collaborations / reviewer_status / total_tokens / estimated_cost / evidence_count / duration_ms |
| Tenant isolation helper | `backend/app/services/agent_invocations.py` + `routers/executions.py::_owns_conversation` | isolation via `conversation_id + user_id` (invocations always carry a real `user_id`; `org_id` defaults to `'default-org'` so it is **not** trusted for isolation) |
| Tree API | `backend/app/routers/executions.py` | `GET /executions/tree` (params: `execution_id` OR `conversation_id`), `GET /executions/{execution_id}/agent-tree` — both behind `_org_scoped_execution` + `_owns_conversation` guards |
| Evidence REST | `backend/app/routers/executions.py` | `GET /executions/evidence` (params: `conversation_id` required, optional `evidence_id`); `_serialize_evidence` returns the shared-registry item **without** raw backing rows (references only) |
| Evidence registry exposure | existing `get_registry` / `get_evidence` | already in-process/Redis; now reachable over REST for the UI evidence panel |

### Frontend — `AgentExecutionTree` (single component, PART 2–8 covered)

| Item | Where | Notes |
|---|---|---|
| Execution tree + 13 states | `frontend/src/components/chat/AgentExecutionTree.jsx` | `STATUS` map (12 + labels EN/ZH, icons, colors): planning, queued, running, delegating, waiting_for_child, reviewing, waiting_for_approval, completed, partial, failed, blocked, cancelled, timeout. `StatusBadge`, `fmtDuration`, `fmtTokens` |
| Supervisor / peer / reviewer / approval / blocked distinction | `NodeBody` (recursive, collapsible) | `delegation_mode=='peer'` → dashed indigo connector + "Peer" chip; reviewer node → violet "Review" chip + `ReviewerPanel`; `awaiting_approval` → amber "Approval" chip; `block_reason` → red "Blocked" chip |
| Reviewer / critic panel (PART 6) | `ReviewerPanel` / `ReviewerBadge` | renders `review.overall` (approved / needs_revision / rejected) + per-dimension verdicts (numerical_accuracy, evidence_completeness, source_attribution, contradictions, unsupported_conclusions, assumptions, question_answered) + issues + missing-evidence + follow-up count |
| Evidence panel (PART 5) | `EvidenceChip` | click → `GET /executions/evidence?conversation_id=&evidence_id=` → shows source / producer / validation status / findings / freshness; fail-open on error |
| Compact execution summary (PART 8) | `ExecutionSummary` | Agents / Completed / Partial / Failed / Peer collab / Evidence / Duration / Tokens / Cost / Review |
| Live-event synthesis (PART 3) | `buildTreeFromLiveEvents(liveEvents)` (exported) | reuses `attachSubagentParent` + `groupIntoBranches` from `LiveActivityStream` — **no fabricated facts**; merges `subagent_returned` into its matching invoked node; every node originates from real SSE payloads |
| Chat integration (PART 9) | `MessageBubble.jsx` | renders `<AgentExecutionTree executionId={message.phase?.execution_id} liveEvents={message.live_events} />` **after** the stream completes (`!isStreaming`) |
| Historical restore (PART 10) | data priority in `AgentExecutionTree` | `tree` prop → backend tree API (by `conversation_id`/`execution_id`) → synthesized from `liveEvents`. Re-fetch on `conversationId`/`executionId` change |

**Data priority:** `tree` prop (instant) → backend `/executions/tree` (authoritative,
historically restorable) → `buildTreeFromLiveEvents(liveEvents)` (fallback). Every
displayed fact originates from backend data — never a frontend guess.

## Tests

- `backend/tests/test_p3_execution_tree.py` — **6 passing**: tree structure
  (supervisor + peer + reviewer + partial), reviewer phase/partial,
  awaiting_approval, tenant isolation, evidence provide/get contract, and
  evidence reference excludes raw rows.
- `frontend/src/components/chat/AgentExecutionTree.test.jsx` — **5 passing**:
  supervisor/peer distinction, no-render on empty roots, reviewer panel,
  evidence REST fetch on click, and `buildTreeFromLiveEvents` synthesis
  (no fabricated facts).
- Backend focused regression (directly-affected modules):
  `test_agent_invocations.py`, `test_invocation_enrichment.py`,
  `test_p3_execution_tree.py`, `test_p2_phase2_collaboration.py`,
  `test_delegate_task_endpoint.py` — **42 passed**.
- Frontend production build: **green** (`npm run build`, 7.1s).
- Note: full backend suite bulk runs show pre-existing cross-test SQLite
  in-memory fixture leakage (errors disappear when those modules run in
  isolation); this is environmental, not a P2 Phase 3 regression.

## Engineering-rule compliance

- No new execution-tracking and no new event infrastructure: the tree is built
  entirely from the existing `AgentInvocation` records + the existing SSE event
  vocabulary (`subagent_invoked` / `subagent_returned` / `tool_call_started` /
  `tool_call_finished`).
- One evidence source (the shared registry) — surfaced over REST, not duplicated.
- Tenant isolation via `conversation_id + user_id`; every new endpoint is guarded
  by `_owns_conversation`.
- Feature flags: **none added** — all changes are additive and backward-compatible.

## Rollback

| Change | Rollback |
|---|---|
| Tree API + evidence API | Remove the three new `@router.get` routes in `executions.py` + the `build_agent_tree` import; delete `test_p3_execution_tree.py` |
| `agent_invocations.build_agent_tree` | Delete the appended builder block (existing `AgentInvocation` columns unchanged) |
| delegate_tool review/evidence persistence | Revert the two `output_json` lines at `delegate_tool.py` ~L600 (fields simply stop being recorded; existing rows keep their data) |
| `AgentExecutionTree.jsx` | Delete the component; remove the `<AgentExecutionTree>` render + import in `MessageBubble.jsx`; delete `AgentExecutionTree.test.jsx` |

## Known open items

- Live LLM end-to-end orchestration still BLOCKED on provider quota (P0, external) —
  the tree/evidence endpoints are verified against persisted `AgentInvocation`
  fixtures and the live-event synthesizer, not a live multi-agent run.
- SSE reconnection / duplicate-event de-duplication (PART 12 item) is covered by
  the authoritative backend tree API + live-event fallback; a hardened
  front-end reconnect wrapper is a follow-up, not required for integration.
- Evidence panel currently shows a single evidence reference per click; batch
  multi-evidence expansion is a UI nicety, not a correctness gap.

---

# P2 Phase 4 — Enterprise End-to-End Validation & Experience Hardening (2026-10-31)

**Scope honored:** integration + hardening only. No new orchestrator, broker,
evidence registry, execution tracker or event infrastructure was created. All
changes extend the existing P1/P2-Phase-1/2/3 implementations.

## 1. Actual implementation changes

| # | Change | Where | Why (root-caused, not guessed) |
|---|---|---|---|
| 1 | **SECURITY — evidence REST no longer leaks raw DB rows** | `backend/app/routers/executions.py::_serialize_evidence` | Inspection found `GET /executions/evidence` returned `EvidenceItem.to_dict()`, whose `query_provenance` carries `rows` — contradicting the module's own contract ("peers fetch the *reference* … never the raw rows"). The safe `reference()` shape was never used over REST. Now `rows` is stripped at the REST boundary (a non-sensitive `row_count` remains); a belt-and-braces top-level `rows` guard was added. Findings (compact, curated) still ship. |
| 2 | **Duplicate-event prevention + deterministic ordering** | `frontend/src/components/chat/LiveActivityStream.jsx` (new exported `dedupeAndSortEvents`, wired into `mergeEvents`); `AgentExecutionTree.buildTreeFromLiveEvents` | SSE frames had no dedup (`liveEvents.push` unconditional) and no ordering beyond arrival; a redelivered/out-of-order frame could mis-pair `tool_call_started/finished` and `subagent_invoked/returned`, or double-render. `dedupeAndSortEvents` dedupes by event `id` (last occurrence wins — later frames carry merged lifecycle data), dedupes id-less identical frames, and orders by `ts` with `id` tiebreak. |
| 3 | **Live-event cap raised 100 → 300** | `backend/app/services/agent_loop/sse_builders.py::_LIVE_EVENT_CAP` | A single multi-agent turn (GA → several parallel specialists, each with multiple tool calls + sub-delegations) legitimately exceeds 100 events; overflow was **silently dropped server-side**, leaving the tree/activity feed incomplete. Cap remains a runaway-loop safety valve. |
| 4 | **Evidence panel extended (Part 4)** | `frontend/src/components/chat/AgentExecutionTree.jsx::EvidenceChip` | Now renders (when the backend provides them): query provenance (SQL + natural-language intent — never rows), metric-definition ids, built-on evidence references (relationships), assumptions, limitations/missing evidence, freshness state — alongside the existing source/producer/validation/findings/freshness. References stay lightweight. |
| 5 | **Scenario-F batch contract test realigned + locked** | `backend/tests/test_p1_orchestration.py` | `test_batch_status_failed_and_partial` encoded the pre-P2 precedence ("any failure → failed") and failed even on pristine code. `_delegate_batch` deliberately implements the newer, spec-matching semantics (in-code contract, lines ~398-403): mixed success+failure → `partial` (survivors produced real findings); **zero** successes → `failed`. Test updated to the current contract and a new `test_batch_all_failed_is_failed` locks the zero-success half. |

## 2. Verified end-to-end user journey (code-traced, per component)

Department select (`PlusMenu` → `ChatInput` chip → conv metadata) → `ACTIVE DEPARTMENT`
prompt block on v2+v3 (`agents.py::_append_department_block`) → GA interprets intent →
discovery (`list_available_agents` + department/capability enrichment, default dept from
conv metadata) → governed delegation (`delegate_tool` → `evaluate_delegation` broker:
publication → dedup → cycle → depth → children → allowlist) → specialists run with
user identity preserved (never the requesting agent's) → peer collaboration (Pattern C,
`delegation_mode="peer"`, nested delegation itself governed) → evidence exchanged as
compact references (`provide_evidence`/`request_evidence_references`) → reviewer verdict
persisted to invocation `output_json.review` → plan-DAG agent-node dispatch through the
same governed path → approval via existing `Execution.needs_approval` + plan approve/reject
endpoints → invocations recorded (`AgentInvocation`, `span_id` parent links) → tree +
evidence read back through `/executions/tree`, `/executions/{id}/agent-tree`,
`/executions/evidence` → `AgentExecutionTree` renders in `MessageBubble` after stream
completion (authoritative tree API first, live-event synthesis as fallback).

**Department = discovery context, not an authorization bypass** — verified: the
department block only adds prompt context; KB bindings, RLS and permission policy are
untouched, and the user's identity is never replaced by the requesting agent's.

## 3. Seven scenario results

| Scenario | Result | Basis |
|---|---|---|
| A Production root-cause | **INTEGRATION-TESTED** | Cross-department plan-DAG delegation test (`test_p1_scenarios.py` A: Inventory + Procurement agents each dispatch their own specialist); same machinery covers Production/Quality/Maintenance |
| B Inventory shortage (peer) | **INTEGRATION-TESTED** | `test_p3_execution_tree.py` peer-collaboration node + shared-evidence contract tests; P2 Phase 2 collaboration suite |
| C Vehicle profitability | **INTEGRATION-TESTED** (machinery) | Metric-binding at the NL2SQL gate (query contract) grounds figures in governed metric definitions; this specific question was not exercised (no live LLM) |
| D Supplier performance | **INTEGRATION-TESTED** (machinery) | Same delegation + evidence path as A/B; no dedicated scenario test |
| E Reviewer challenge | **INTEGRATION-TESTED** | Reviewer verdict persisted + `phase="reviewing"` mapping + `needs_evidence` rendering (tests pass) |
| F Partial failure | **INTEGRATION-TESTED** (this session) | Batch roll-up now contract-locked both ways: mixed → `partial` + machine-readable per-item statuses + `[PARTIAL RESULT]` prefix; zero-success → `failed` |
| G Approval-required action | **INTEGRATION-TESTED** | `awaiting_approval` → `waiting_for_approval` node phase (test), `Execution.needs_approval` + plan approve/reject endpoints; pause/resume not exercised live |

**None of the seven is LIVE-VERIFIED or PRODUCTION-READY** — that requires a funded
provider + running deployment (below). Mocked-LLM tests are explicitly NOT counted as
production acceptance.

## 4. Regression results (this work vs pre-existing)

Backend (each suite run in isolation to avoid the known cross-test SQLite fixture leakage):
`test_p3_execution_tree` **7 passed** (incl. new serializer security test) ·
`test_agent_invocations` + `test_invocation_enrichment` + `test_p2_phase2_collaboration` +
`test_delegate_task_endpoint` combined **43 passed** · `test_p1_orchestration` **13 passed**
(realigned +1 new) · `test_p1_scenarios` **5 passed** · `test_p1p2_hybrid` **12 passed** ·
`test_p2_production_hardening` **7 passed, 2 skipped** · `test_fsm_tenant_isolation` **15 passed** ·
`test_live_events` 95-passed set with **1 pre-existing failure** (below).

Frontend: `AgentExecutionTree.test.jsx` **6 passed** · `LiveActivityStream.test.jsx` **4 passed**
(new) · production build **green** (`npm run build`, exit 0).

**Pre-existing failures (NOT introduced by this work — verified by reverting my edits and re-running):**
- `test_live_events.py::TestContentInvariant::test_erp_table_names_replaced` — the content-invariant
  regex covers `erp_[tv]_*` table patterns but never bare view names (`v_sale_order`); the test
  asserts behavior the code never implemented. **Genuine security limitation, recorded below.**
- Bulk full-suite runs show ~328 collection/setup ERRORs from cross-test SQLite in-memory fixture
  leakage — every affected module passes in isolation (environmental, pre-existing).

## 5. Real LLM acceptance status: **BLOCKED (external)**

Provider inspection (no secrets exposed): local `backend/.env` has `OPENAI_API_KEY` **present**;
no dedicated `DASHSCOPE_API_KEY`/`ALIYUN`/`QWEN` key; `LLM_BASE_URL`/`ACTIVE_PROVIDER` unset
(default = OpenAI-compatible "primary", `LLM_MODEL` default `gpt-4o-mini`; hierarchical catalog
default otherwise). A *funded, reachable* provider cannot be confirmed from the repo alone, and
this analysis environment has no running backend+frontend+LLM stack to execute real turns —
prior sessions additionally recorded live orchestration blocked on `429 insufficient_quota` (P0,
external billing action). **All non-LLM integration work is complete; live acceptance is
reported separately as blocked, and mocked-LLM tests are not classified as production acceptance.**

## 6. Performance

**Not measured — no live runtime available in this environment**, and per the execution rules no
optimization was performed without measurements. Static (code-level, NOT measurements) notes for
the future measurement pass: evidence retrieval is an O(1) in-process/Redis lookup; the tree
builder is one scoped query + in-memory assembly (O(n)); agent discovery is one `AgentApp` query
plus ≤5 KB-name lookups per agent; the dedupe/ordering pass is O(n log n) per feed render.
Suggested instruments: time-to-first-response, discovery latency, delegation overhead,
parallel-batch wall time, evidence retrieval, reviewer overhead, total duration, tokens.

## 7. Security findings

1. **FIXED (this session):** `/executions/evidence` leaked `query_provenance.rows` (raw result
   rows) despite its own no-raw-rows contract — now stripped at the REST boundary (+ regression test).
2. **Pre-existing, unfixed (recorded, out of scope):** the live-event content invariant does not
   scrub bare view names (`v_sale_order`) — only `erp_[tv]_*` patterns and ERP column-id shapes.
   Lower risk (server-generated labels), but the sanitizer's guarantee is narrower than documented.
3. **Verified sound:** tenant isolation via `conversation_id + user_id` with org-scoped execution
   resolution (foreign org = 404, indistinguishable); org always from the authenticated user,
   never the request body; evidence endpoints conversation-scoped; department never bypasses
   KB/RLS/permission policy.
4. **Note (functional, not a leak):** the evidence registry's in-process fallback is not shared
   across workers/replicas unless Redis is configured and authenticated.

## 8. Remaining limitations

- **SSE mid-stream resume:** a dropped connection cannot resume an in-flight generation (the
  stream is a one-shot POST response). Current behavior: watchdog (90s) aborts with an error;
  accumulated `live_events` are kept and the authoritative tree API restores state after
  completion/refresh. True resume needs backend turn-resumable SSE (architectural, not started).
- **Delayed invocation persistence:** `AgentInvocation` rows are written at sub-agent *completion*;
  a viewer querying `/executions/tree` during a long in-flight sub-agent sees nothing running
  (status `running` is supported by the model but unused). Fix = write a `running` row at
  delegation start and update on completion.
- **Per-node approval flag:** `AgentInvocation` has no approval column; paused state lives at
  execution/conversation level (tree marks the root via `awaiting_approval`). Only one resume
  state per turn.
- **Event cap** remains finite (now 300); `_LIVE_EVENT_CAP` truncation is logged, not surfaced in UI.
- Live multi-agent E2E + performance measurements blocked on funded provider/running deployment.

## 9. Rollback

| Change | Rollback |
|---|---|
| Evidence serializer | Revert `_serialize_evidence` in `backend/app/routers/executions.py` to plain `to_dict()` passthrough (restores the leak — do not do in production) |
| Dedup/ordering | Remove `dedupeAndSortEvents` + its use in `mergeEvents` (`LiveActivityStream.jsx`) and `buildTreeFromLiveEvents` (`AgentExecutionTree.jsx`) |
| Event cap | Restore `_LIVE_EVENT_CAP = 100` in `sse_builders.py` |
| Evidence panel fields | Revert the `EvidenceChip` expanded-panel block in `AgentExecutionTree.jsx` |
| Scenario-F test alignment | Revert `test_batch_status_failed_and_partial` assertion + delete `test_batch_all_failed_is_failed` (restores the stale contract — code behavior unchanged either way) |
| New tests | Delete `LiveActivityStream.test.jsx`; remove the added tests from `test_p3_execution_tree.py` / `AgentExecutionTree.test.jsx` |

## 10. Capability classification (honest three-states)

| Capability | State |
|---|---|
| GA + department selector + discovery + AgentApp-aware delegation | **INTEGRATION-TESTED** |
| Collaboration Broker + AgentTaskContract + nested peer collaboration | **INTEGRATION-TESTED** |
| Critic/Reviewer + shared evidence registry + plan-DAG execution | **INTEGRATION-TESTED** |
| Human approval (plan gate) + AgentInvocation records | **INTEGRATION-TESTED** |
| Execution-tree APIs + AgentExecutionTree + LiveActivityStream + SSE delivery | **INTEGRATION-TESTED** (hardened this session) |
| Evidence REST (post-fix) | **INTEGRATION-TESTED** |
| Scenarios A–G | **INTEGRATION-TESTED** (machinery); live execution **BLOCKED** |
| Live multi-agent LLM acceptance / performance measurements | **BLOCKED** (external: funded provider + running deployment) |
| LIVE-VERIFIED / PRODUCTION-READY | **none** — nothing in this milestone has been executed against a live funded provider |

---

# Security-Hardening Follow-Up — Content-Invariant Sanitizer (2026-10-31)

**Classification: FIXED** (the reported defect — bare view names reaching user-visible
payloads — is closed on every identified path and covered by adversarial tests).
Complete security coverage is **NOT** claimed on the basis of unit tests alone; the
residual boundaries are listed explicitly under Limitations.

## Before / after reproduction

```
# BEFORE (defect):
_sanitize_live_event_params({"label": "v_sale_order"})
→ {"label": "v_sale_order"}                      # identifier passed through
_sanitize_live_event_params({"label": "查询v_sale_order的十月销量数据"})
→ {"label": "查询v_sale_order的十月销量数据"}      # CJK-adjacent form passed through
agents tree node.error_message ("Unknown column 'v_sale_order.fqty' ...")
→ returned verbatim over /executions/tree        # no gate at all

# AFTER (fixed):
_sanitize_live_event_params({"label": "v_sale_order"})            → "[data]"
_sanitize_live_event_params({"label": "查询v_sale_order的十月销量数据"}) → "[data]"
"sales.v_sale_order", "V_PURCHASE_ORDER", "rows from v_sale_order for October"
                                                                  → "[data]"
tree node.error_message                                           → "[data]"
# preserved: "revenue", "over_value", "CV_sale", "v2 launch plan",
#            "gross_margin_rate", "查询十月的销量数据"
```

## Root cause (two layered defects)

1. **Pattern gap** — `_LIVE_EVENT_FORBIDDEN_RE` matched only `erp_[tv]_*`-prefixed
   identifiers; bare view names (`v_sale_order`) never matched. The pre-existing
   failing test (`test_erp_table_names_replaced`) asserted behavior the regex never
   implemented.
2. **CJK word-boundary trap** (found by adversarial testing, not by inspection) —
   Python `\b` treats Chinese characters as Unicode word chars, so in
   `"查询v_sale_order"` there is **no** boundary before `v` and a naive `\bv_…\b`
   fix would still leak. The view branch therefore uses ASCII-only lookarounds
   `(?<![A-Za-z0-9_])…(?![A-Za-z0-9_])`.
3. **Ungated surface** — the execution-tree REST returned `AgentInvocation.error_message`
   (and broker `block_reason`) verbatim; driver errors embed SQL fragments/column names.

## Where identifiers can and cannot reach users (investigated, not assumed)

| Surface | Verdict | Action |
|---|---|---|
| Live SSE events (`_build_live_event`) | user-visible; gated by the sanitizer — **the gap** | regex extended + dynamic supplement |
| Execution-tree REST (`error_message`, `block_reason`) | user-visible; **no gate existed** | now passed through the same sanitizer (`_scrub` in `agent_invocations.py`) |
| Evidence REST (`query_provenance.sql/nl`, compact `findings`) | user-visible **by design** — authorized provenance (rows already stripped in Phase 4; `EvidenceItem` has no credential field) | regression-tested, no change |
| `AgentInvocation.output_json` via tree API | curated fields only (tools/review/evidence ids) — no identifiers | none needed |
| Tool execution outputs → LLM answers / artifacts | authorized deliverables of the tenant's own data; answer-level content is the grounding layer's domain, not the sanitizer's | out of scope (documented) |
| Logging / audit output | internal, not user-visible | out of scope (documented) |
| User-authored `task` / `input_message` | the user's own conversation-scoped text; tenant isolation applies | deliberately NOT scrubbed |

## Implementation (reuses the existing sanitizer — no second framework)

1. `backend/app/services/agent_loop/sse_builders.py` — view branch added to the
   case-insensitive group with ASCII lookarounds; new **dynamic supplement**:
   `register_sensitive_identifiers()` (snake_case-gated, ≤2000 ids, compiled
   alternation, thread-safe) checked in `_sanitize_value` alongside the static regex.
2. `backend/app/services/tool_handlers/db_tools.py::_describe_schema` — single clean
   hook: after schema enumeration, the datasource's REAL table/view names are
   registered into the supplement (metadata-driven, not a hardcoded list). Failure to
   register can never break the tool (try/except + debug log).
3. `backend/app/services/agent_invocations.py` — `_scrub()` applies the same sanitizer
   to machine-generated `error_message` / `block_reason` on tree nodes.

## Tests added (TDD: written RED first, then GREEN)

`backend/tests/test_content_invariant_hardening.py` (14): bare view; single-word view;
uppercase view; schema-qualified; keyword-free SQL fragment; nested SSE payload; JSON
round-trip + structure/identity preservation; sanitizer purity (input never mutated);
Chinese/English mixed; plain-Chinese untouched; ordinary business language untouched;
`\b`-substring safety ("revenue"/"over_value"/"CV_sale"/"delivery_van"); approved metric
names untouched; dynamic registration (scrub-after-register, substring safety, prose
protection "3 users found", junk rejection).
`backend/tests/test_p3_execution_tree.py` (+2): invocation `error_message` and broker
`block_reason` sanitized through `build_agent_tree`.

## Regression results

`test_content_invariant_hardening` + `test_p3_execution_tree` + `test_live_events` +
`test_agent_loop_sse_builders` + `test_live_plan_persistence` combined: **95 passed**
— including the formerly-failing `test_erp_table_names_replaced`, now genuinely fixed ·
P1 orchestration + invocations + enrichment + Phase-2 collab + delegate endpoint:
**49 passed** · db_tools validation hints + query contract: **37 passed** ·
`test_nl2sql_gaps` **11**, `test_db_tools_validation_hints` **8** (standalone; bulk-run
errors remain the known environmental fixture leakage) · frontend P2-Phase-4 suites
**10 passed**, lints clean.

**Pre-existing, unrelated (verified not mine):** `test_nl2sql_oci_p2` 2 failures —
stale `persist_rewrite_outcome()` signature in the test vs `app/services/db/execution_feedback.py`
(file untouched by this session); `tests/test_erp_connector.py` 5 failures + a
bulk-collection ImportError — **untracked WIP** files (`??` in git), not part of this work;
bulk-suite fixture leakage (environmental).

## Security limitations (explicit)

- **Un-prefixed table names** (`sales_summary`) are scrubbed only once registered via
  describe_schema (snake_case-gated to protect prose). A flow that queries a source
  without describing it first has no dynamic registration for that turn — the static
  prefix patterns still apply.
- **Static SQL-keyword branches still use `\b`** — CJK-adjacent keywords could evade
  (minor: bare keywords are not identifiers; the view branch — the actual leak class —
  is lookaround-anchored).
- **Replacement remains whole-string** (`"[data]"` fallback), not surgical: surrounding
  business prose in a leaking string is lost. Chosen deliberately — conservative over
  lossy-but-leaky; surgical rewriting would risk corrupting text in new ways.
- **Evidence SQL/intent is shown by design** (authorized provenance); tenants see their
  own conversation's data only.
- **Unit tests ≠ complete coverage** — no live end-to-end execution was run (live LLM
  acceptance remains blocked, see Phase 4 section).

## Rollback

| Change | Rollback |
|---|---|
| View-name regex branch | Remove the `(?<![A-Za-z0-9_])v_…` alternative from `_LIVE_EVENT_FORBIDDEN_RE` |
| Dynamic supplement | Delete the `_DYNAMIC_*` block + `register_sensitive_identifiers` + the `_dynamic_identifier_hit` call in `_sanitize_value` |
| describe_schema hook | Remove the registration try/except block in `db_tools._describe_schema` |
| Tree error/block scrub | Revert `_scrub()` + its two call sites in `agent_invocations.py` |
| Tests | Delete `test_content_invariant_hardening.py`; remove the 2 added tests from `test_p3_execution_tree.py` (note: this also re-breaks `test_erp_table_names_replaced`, which this work fixed) |

## Closure audit (2026-10-31, later same day) — one more UNPROTECTED surface found and fixed

Re-verifying every `sanitize=False` site as the closure required **found a real gap the
first pass had classified too generously**: plan-step titles are **LLM-authored**
(`turn_planner.parse_dynamic_plan` parses step titles from raw planner LLM output —
only markdown-cleaned), not server-template metadata like tool labels. Six emission
points carried them past the sanitizer:

1–3. The three persisted `_push_live_event(..., sanitize=False)` sites in `agents.py`
     (`plan_step_added` ×1, `plan_step_completed` ×2).
4–6. The raw `plan_step_added_frame` / `plan_step_completed_frame` builders
     (`turn_planner.py`) — plain `json.dumps` yields that bypass `_build_live_event`
     entirely (agents.py:14425 / 17599 / 18765).

`tests/test_live_plan_persistence.py::test_plan_step_titles_bypass_content_scrubber`
had codified the wrong premise ("same class as tool labels") and was realigned with
the verified facts.

### Fixes (minimal, TDD — 5 RED tests first, incl. 2 that caught further defects)

- `turn_planner.py` — both frame builders now route titles through the SAME sanitizer
  (`_scrub_step_title`); ordinary titles render verbatim.
- `agents.py` — the three persisted plan-step pushes use the default `sanitize=True`;
  stale comment updated.
- `sse_builders.py` — adversarial tests caught **two more layered defects**:
  (a) the `erp_[tv]_` branch had the SAME CJK `\b` trap as the view branch
  (`查询erp_t_sal_outstock的数据` evaded) → ASCII lookarounds applied;
  (b) quote normalization was outer-only, so `` `sales`.`inv_snapshot` `` registered
  "`inv_snapshot" instead of the bare tail → quoting is now stripped PER DOT-SEGMENT
  and qualified names register both the dotted form and the bare last segment.
- Registration cap (2000) now logs a WARNING with the skipped count — never silent.
- `test_live_plan_persistence.py` realigned: no live-event site may bypass the scrubber;
  the `sanitize` capability itself remains supported and tested.

### Dynamic registration — verified answers (Part 1 of the closure spec)

| Question | Verdict |
|---|---|
| Datasource/tenant scoping | Deliberately **process-global** — registered names scrub across datasources/tenants. This is OVER-BLOCKING (fail-safe): there is no per-tenant allowance, so no cross-tenant LEAK path exists; the cost is occasional over-scrubbing of a name another datasource legitimately shares. Proven by `test_registration_is_global_over_blocking_not_per_tenant_leaky`. |
| Thread safety | `threading.Lock` around registration + regex rebuild; readers may see the previous alternation for a just-registered name (under-scrub only for that instant). Proven by an 8-thread concurrent register+sanitize test. |
| Cap enforcement + observability | Hard cap 2000; skipped names WARN-logged with count. NOT silent. Beyond-cap names keep static-prefix protection only (documented limitation). |
| Normalization | Case-folding, per-segment quote/backtick/bracket stripping, schema qualification → dotted form + bare last segment. Junk (non-str, <4 chars, no underscore) rejected. |
| Registration failure | `db_tools` wraps registration in try/except (debug log): failure degrades to static-prefix protection only; it can never break describe_schema nor widen exposure. |

### Sanitization coverage matrix (final, every surface classified)

| Surface | Classification | Evidence |
|---|---|---|
| `_build_live_event` (all typed events incl. delegation, tool, retry, data_offer cells) | **SANITIZED** | default `sanitize=True`; suite `test_agent_loop_sse_builders` |
| Plan-step persisted pushes (×3 sites) | **SANITIZED** (was UNPROTECTED) | default path restored; `test_plan_step_sites_do_not_bypass_scrubber` |
| Plan-step raw SSE frames (×3 yield sites) | **SANITIZED** (was UNPROTECTED) | `_scrub_step_title`; `test_plan_step_{added,completed}_frame_*` |
| Execution-tree REST `error_message` / `block_reason` | **SANITIZED** | `_scrub` in `agent_invocations.py`; tree tests |
| Evidence REST rows | **SANITIZED** | Phase 4 serializer; `test_evidence_rest_serializer_strips_raw_rows` |
| Evidence REST `query_provenance.sql/nl` + compact findings | **AUTHORIZED EXCEPTION** | see boundary below; `test_evidence_rest_rows_excluded_and_no_credentials` |
| Evidence REST access control | **SANITIZED (guard verified)** | `_owns_conversation` fail-closed; `test_unauthorized_conversation_gets_no_evidence` |
| User-authored `task` / `input_message` on tree nodes | **AUTHORIZED EXCEPTION** | user's own tenant-scoped prose; scrubbing would corrupt their own text |
| LLM final answers / artifacts | **AUTHORIZED EXCEPTION** (out of sanitizer scope) | authorized deliverables of the tenant's own data; grounding layer's domain |
| Logs / audit output | **INTERNAL ONLY** | not user-visible |
| Tool results → parent LLM | **INTERNAL ONLY** | model context, not rendered |

### Evidence SQL provenance — precise security boundary

What is intentionally visible to an authorized user (owner of the conversation, verified
per request by `_owns_conversation`; org always from the authenticated user):
`query_provenance.sql` (the NL2SQL-generated query), `query_provenance.nl` (the natural
language intent), `execution_id`, compact curated `findings`, freshness, validation
status, metric-definition ids, references. What can NOT appear: raw rows (stripped at
the serializer), platform credentials (the platform never injects datasource credentials
into SQL text — they live in datasource config; `EvidenceItem` has no credential field,
verified by the forbidden-key test), internal driver errors (`query_provenance` carries
`execution_id`, never error text), cross-tenant data (conversation-scoped store +
ownership guard). NOT hidden — and not claimed to be: the SQL itself remains visible by
design for enterprise auditability.

### Test-count reconciliation (95 vs 92 — no discrepancy, different file sets)

- 95 = content_invariant + p3_execution_tree + live_events + agent_loop_sse_builders +
  live_plan_persistence (5 files).
- 92 = content_invariant + p3_execution_tree + live_events + p1_orchestration (4 files,
  different mix). Both were full passes of their respective sets.
- **Fresh closure-run results (this session, per suite):** content_invariant **26** ·
  live_events **53** · agent_loop_sse_builders **13** · live_plan_persistence **3** ·
  turn_planner **43** · p3_execution_tree **10** · agent_invocations **5** ·
  invocation_enrichment **5** · p1_orchestration **13** · p1_scenarios **5** ·
  p1p2_hybrid **12** · p2_production_hardening **7+2 skipped** ·
  fsm_tenant_isolation **15** · db_tools_validation_hints **8** · query_contract **29** ·
  nl2sql_gaps **11** · nl2sql_db_error_repair **23** — **all passed** · frontend
  AgentExecutionTree + LiveActivityStream **10 passed** · production build **exit 0**.

### Git hygiene (Part 6)

No commits made (not authorized). Working tree carries extensive uncommitted WIP from
prior sessions (P0–P7, orchestration) — untouched. This closure touched exactly:
`sse_builders.py`, `turn_planner.py`, `agents.py` (3 one-line flag removals + comment),
plus tests (`test_content_invariant_hardening.py` new, `test_live_plan_persistence.py`
realigned). Temp files (`/tmp/exec_bak.py`, `/tmp/sse_bak.py`, build logs) removed. No
debug code, no accidental modifications (verified via `git status` / `git diff --stat`).

### Final acceptance classification (Part 8)

| Item | State |
|---|---|
| Bare/schema-qualified/quoted/CJK-adjacent view + erp_ identifiers in live events | **FIXED** |
| Plan-step titles (persisted + raw frames) | **FIXED** (was UNPROTECTED) |
| Execution-tree error/block strings | **FIXED** |
| Evidence raw-row exclusion + credential absence + access guard | **VERIFIED** |
| Evidence SQL provenance boundary | **VERIFIED** (documented authorized exception) |
| Dynamic registration (normalization, cap, concurrency, global-scope semantics) | **VERIFIED** (global over-blocking documented as deliberate) |
| CJK-adjacent SQL *keyword* evasion (`\b` on keyword branches) | **DEFERRED** (minor: bare keywords are not identifiers) |
| Surgical (token-level) replacement instead of whole-string fallback | **DEFERRED** (usability trade-off, not a security gap) |
| Un-prefixed table names before any describe_schema registration | **PARTIALLY FIXED** (dynamic supplement covers post-describe; static prefixes always) |
| Live deployment validation | **BLOCKED — separate acceptance gate** (funded provider + running stack; unit tests do not constitute production security acceptance) |

**Milestone frozen.** Remaining live-deployment acceptance blockers, reported separately:
(1) funded LLM provider + running backend/frontend stack for real multi-agent E2E
(prior sessions recorded 429 insufficient_quota); (2) live verification of the sanitizer
against production event streams; (3) performance measurements (Part 8 of Phase 4) on a
running deployment.

---

# Enterprise AI OS — Phase 0 complete + Lifecycle-consistency fix (2026-10-31)

## Phase 0 (Parts A–C of the full mission in `docs/architecture/MESSAGE.MD`) — COMPLETE

Deliverables produced (all grounded in a same-day read-only code audit + full fetches of all
11 mandated references):

1. `zhanlu-manus-chatgpt-capability-benchmark.md` — reference capabilities (IBM ×6, Manus
   product + API v2, ChatGPT agent launch/help/engineering docs), documented principles, the
   26-row capability matrix, gaps, R1–R9 recommendations, dependencies/risks, priorities.
   Honesty notes: ChatGPT agent mode marked legacy/retired per OpenAI's own help page;
   Manus internals never claimed beyond public docs.
2. `zhanlu-general-assistant-intelligence-architecture.md` — GA's 20 responsibilities mapped
   to verified mechanisms; gap designs (intent classification in the plan payload, plan
   enrichment fields, reviewer-driven replan criteria, delegation-decision observability).
3. `zhanlu-hybrid-multi-agent-execution-design.md` — Patterns A/B/C preserved with the
   hardening plan (H1–H5); A2A mapping boundaries (no compliance claims); enterprise
   boundary rules.
4. `zhanlu-agentic-platform-implementation-roadmap.md` — Phases 0–7 with dependencies,
   constraints, and the standing live-acceptance blocker.

## Mission §23 / Hybrid H1 — Lifecycle consistency: FIXED (TDD)

**Root cause (investigated, not assumed):** delegation wrote `AgentInvocation` rows ONLY at
terminal states — a long-running child had **no persisted existence**. During an in-flight
batch the authoritative `/executions/tree` showed nothing while the live feed synthesized
"running" nodes with nothing behind them; §23.7 ("reconnection restores actual persisted
state") was only satisfied post-hoc. (No code path ever *created* a `running` row, so a
literally stuck-RUNNING DB row was impossible — the gap was the inverse: invisible work.)

**Fix (two-phase lifecycle, single row):**
- `agent_invocations.py`: `record_invocation(..., open_invocation=True)` inserts a `running`
  row (no completion time, no reward); new `update_invocation_terminal(...)` finalizes the
  SAME row — first terminal write wins, terminal status never regresses, reward computed once
  on transition, `False` on refusal (unknown id / non-terminal target / already terminal).
- `delegate_tool._run_sub_agent`: start row created right after the concurrency gate; the
  finally block finalizes it (completed/partial/failed/timeout/cancelled/blocked) and falls
  back to the legacy terminal-only insert if the start row never materialized — behavior is
  never worse than before.
- §23 checklist now holds: truthful terminal state for every started child (finally covers
  exception/cancel/timeout paths); required children resolve before parent completion
  (`_delegate_batch` awaits all); partial stays partial; failed/cancelled visible; duplicate
  branches and terminal regression already prevented (dedupeAndSortEvents + first-terminal-
  wins); **reconnection mid-flight now restores real persisted state**; UI never fabricates.

**Tests:** 3 new (running-row visible in tree; terminal update updates the same row with no
duplicate; terminal never regresses) — RED→GREEN. Fresh regression: p3_execution_tree **13** ·
p1_orchestration **13** · p1p2_hybrid **12** · p2_phase2_collaboration **22** ·
delegate_task_endpoint **4** · agent_invocations **5** · invocation_enrichment **5** ·
content_invariant_hardening **26** · live_events **53** — all passed; lints clean.

**Next per roadmap:** Phase 1 GA-intelligence items (plan-payload intent classification,
plan enrichment fields) and the remaining P1 benchmark items (model selector, completion
webhooks, interactive code execution, durable queue) — each behind the documented designs.

## H1 end-to-end closure (same day) — lifecycle VERIFIED, one gap fixed

**Investigation result:** the two-phase persistence from the previous step was correct but
incomplete. Re-inspection of every return path (success / failure / batch / peer / nested /
timeout / cancellation / exception / replan / synthesis) established:

- **H1-A (synchronization):** structurally sound — `_delegate_batch` awaits ALL required
  children (parallel, not serialized), so the parent physically cannot answer while a
  required child runs. Verified with a deterministic concurrent test: 4 children with
  staggered delays (0.05–0.30s) → batch returns after the slowest (elapsed ≥ 0.30s, < 0.60s)
  with all 4 per-item results. Failed child → truthful `partial` parent outcome (scenario-F
  suite). Parent timeout never implies children completed (each child row finalizes in its
  own finally block, independent of the parent's fate).
- **H1-B (orphans) — REAL GAP FOUND AND FIXED:** a worker crash/restart between the
  start-row insert and the finally update left a `running` row forever. Fix:
  `recover_orphaned_invocations()` — `running` rows older than 1h (delegation wall-caps are
  minutes) become `failed` with `error_message="orphaned: …"` and
  `output_json.recovery={reason: worker_lost, recovered_at}` — **never completed**;
  idempotent; fresh in-flight rows untouched; scheduled every 10 min via the existing
  advisory-locked scheduler. A naive/aware datetime crash (SQLite) was caught by the
  adversarial test and fixed (tz normalization).
- **H1-C (DB/event consistency):** first-terminal-wins verified including the late-`failed`-
  after-`completed` case; duplicate updates are no-ops; concurrent child finalizations are
  per-row; reconnection during execution restores the actual running tree (running rows now
  exist); reordering/duplication handled by `dedupeAndSortEvents` + tree-API priority.
- **H1-D (frontend truthfulness):** verified the authoritative tree renders in-flight
  `running` nodes truthfully (new test); identity is always the real AgentApp label
  ("sub-agent" only as last-resort fallback); no completion fabrication (synthesis uses only
  real events; the fetched tree wins once available).
- **Required vs optional tasks:** the task contract has no `optional` flag — all delegated
  children are required by construction today. Policy documented in the hybrid design
  (§7.2); deliberately not implemented (no caller).

**State-transition contract** (statuses, ownership, A2A mapping, completion/partial criteria,
cancellation propagation, recovery, terminal precedence) is documented in
`zhanlu-hybrid-multi-agent-execution-design.md` §7.

**Test evidence (fresh):** test_p3_execution_tree **18 passed** (8 new adversarial: orphan
recovery ×2, batch-waits, mixed tree, out-of-order, plus prior lifecycle tests) ·
p1_orchestration 13 · p1p2_hybrid 12 · p2_phase2_collaboration 22 · delegate_task_endpoint 4 ·
agent_invocations 5 · invocation_enrichment 5 · fsm_tenant_isolation 15 ·
workflow_scheduler 6 · agent_loop_sse_builders 13 · live_plan_persistence 3 — all passed ·
frontend 11 passed · production build exit 0. Lints clean; no commits made.

**Classification: H1 = INTEGRATION-TESTED (complete at that level).** LIVE-VERIFIED remains
blocked on a funded provider + running deployment; the concurrent test uses deterministic
delayed mocks and is classified accordingly.

---

# Phase 1 — GA Intelligence: intent, planning, strategy, reviewer-driven replan (2026-10-31)

Mission §13 P1.1–P1.5 IMPLEMENTED (+ P1.6 scenario tests at planner level). Full record:
`zhanlu-phase1-intent-planning-implementation.md`. Summary:

- **P1.1 Intent:** semantic classification by the planner LLM inside the EXISTING one-shot
  planning call (10-value `INTENT_TYPES` taxonomy; no keyword router; template path leaves
  intent empty by design). Original user request preserved as authoritative — meta travels
  alongside it.
- **P1.2 Plan enrichment:** `TurnPlan` + optional `objective_statement / expected_outcome /
  constraints / acceptance_criteria / verification_requirements / risk_level /
  required_approvals`; `_norm_plan_meta` drops unknown values (never guesses); steps-only and
  numbered-list payloads parse byte-for-byte as before.
- **P1.3 Strategy:** explicit `execution_strategy` (direct_response / direct_tool /
  single_specialist / multi_agent), conservative defaults by mechanical kind, invalid → safe
  default; broker-governed escalation paths unchanged; direct execution remains the default.
- **P1.4 Reviewer-driven replan:** `evaluate_reviewer_findings()` in the existing replan
  policy (decision table incl. `gather_missing_evidence` with gaps preserved verbatim — never
  invented); attached as `review_guidance` on reviewed delegations (flag
  `REVIEWER_REPLAN_GUIDANCE_ENABLED`, default on, advisory); the pre-existing bounded targeted
  re-investigation remains the active mechanism (verified present at delegate_tool.py:182).
- **P1.5 Observability:** `plan_meta` typed live event + SSE frame (persisted, content-
  invariant scrubbed, never emitted empty) — intent/objective/strategy/criteria/risk visible
  in the execution experience; no private chain-of-thought exposed.

**Tests:** `test_phase1_intent_planning.py` **21 passed** (TDD RED→GREEN; caught a real
NameError in `plan_meta_frame` + fixed invalid-strategy semantics) · regression all green:
turn_planner 43 · live_plan_persistence 3 · p1_orchestration 13 · p1p2_hybrid 12 ·
p2_phase2_collaboration 22 · delegate_task_endpoint 4 · p3_execution_tree 18 · live_events 53 ·
content_invariant_hardening 26 · frontend build exit 0.

**Classification: INTEGRATION-TESTED.** Scenarios 3/5/6 verified at the planning-schema
level; live multi-agent E2E remains BLOCKED (funded provider). No commits made.



## Phase 1.5 — GA intelligence completion (2026-10-31)

Closed the four Phase-1 gaps (TDD RED→GREEN; `tests/test_phase15_completion.py` 18 tests +
`LiveActivityStream.test.jsx` 7):

1. **Intent consistency** — `unknown` is a first-class `INTENT_TYPES` value. Template path:
   greeting → `conversational`, all other kinds → `unknown`; LLM plans omitting intent →
   `unknown`. `plan_meta()` always carries intent. Explicit planner values still win.
2. **Reviewer-guidance execution** — verified bounded: 1 follow-up round, ≤2 gaps, no
   `_enable_review` on follow-ups (no loops, no duplicate delegation), one re-review whose
   verdict wins, `review_guidance` attached. `plan_to_system_block` now injects the plan
   contract (objective/constraints/acceptance criteria/verification/risk/strategy) so the
   loop can be held to it.
3. **plan_meta UI** — `LiveActivityStream` renders ONE compact `Plan · intent · strategy ·
   risk · N criteria` row (Target icon, language-aware, structured keys only — no
   chain-of-thought); later richer emissions replace, never stack.
4. **Strategy/actual consistency** — `plan_dag` in `EXECUTION_STRATEGIES` (+prompt);
   delegate results carry `actual_execution_strategy` (`single_specialist` / `multi_agent`).

Tests: phase15 18 + phase1 21 (expectations updated for explicit unknown) + 20 adjacent
suites green. Pre-existing failures in foreign uncommitted WIP (replan_policy `node_id` ×2,
reflexion flag ×1, frontend Sidebar/steer/ProjectDetail/translations/previewToken ×8) are
unrelated — verified via stash test. Frontend build exit 0.

**Classification: INTEGRATION-TESTED.** No commits made.

## Phase 2 — Autonomous Execution Intelligence (2026-10-31)

Root-cause audit + implementation (design: `zhanlu-phase2-autonomous-execution-design.md`,
plan: `zhanlu-phase2-implementation-plan.md`). NEW modules (all TDD RED→GREEN):
- `execution_recovery.py` — the SINGLE failure-classification authority
  (16 categories, 7 outcomes; empty≠failure; permission terminal; unknown+mutating
  non-retryable) + bounded healing policy (per-call attempts, per-turn HealingBudget=4,
  write idempotency required).
- `execution_decisions.py` — pure observation→decision table (11-value taxonomy;
  authorization > determinism > transient > evidence gaps > completion; no CoT;
  `decision_from_review` routes via the Phase-1 replan precedence).
- `capability_fallback.py` — published-only, keyword-matched alternative selection;
  destructive rerouting refused.
- `evidence_validation.py` — output/contract validation verdicts (validated|partial|invalid);
  NL2SQL gates untouched.

WIRED: `execute_tool_with_retry` (classify + bounded heal + structured failure feedback),
`delegate_tool` (execution_decision + alternative_capabilities on failure), reviewer
(unsupported_claims/contradictions/failed_acceptance_criteria producers + acceptance
criteria param), approval gate (destructive tier ALWAYS gated, flag
EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=True), v3 loop (per-turn HealingBudget +
`failure_classified`/`execution_decision` SSE), LiveActivityStream (recovery rows).

Tests: 9 new backend files (131 tests) + scenarios A–H; sweep of 34 adjacent suites
475 passed; the only 4 failures verified pre-existing via stash A/B (foreign WIP /
cross-test pollution). Frontend: LiveActivityStream 11 green; build exit 0.

**Classification: INTEGRATION-TESTED.** Live E2E still BLOCKED (provider quota).
No commits made.

## Phase 3 — Production-Grade Autonomous Platform (2026-10-31)

Audit-first (gap matrix: `zhanlu-phase3-production-readiness-audit.md`). Reused
(did NOT rebuild): PlanNode/ObservationRecord/AgentRunStep durable state,
scheduled orphan recovery, local embeddings, Phase-2 lexical matcher, org-scoped
telemetry tables, governance/cost services.

SHIPPED (TDD RED→GREEN, ~90 new tests):
- P3.1 `durable_execution.py` — resume decision layer (skip/leave_running/retry/
  recover/manual; approval+mutating NEVER auto-resumed; deterministic idempotency
  keys; stale TTL 1800s) + migration 115 `observation_records.evidence_ids`
  (persisted via capability_router `_extract_evidence_ids`).
- P3.2 `semantic_capability_match.py` — local-embedding cosine selection over
  published-only prefiltered pool; threshold→own-tools-sufficiency; department
  wall; lexical degrade; wired into delegate_tool fallback discovery.
- P3.3 zh markers merged into execution_recovery `_MARKERS` (regression caught
  dict-duplicate-key wipe; fixed by programmatic merge), `localized_recovery_hint`,
  CJK bigram tokenizer; zh/en classification EQUIVALENCE tested (9 pairs).
- P3.4 `reliability_metrics.py` (12-metric table) + LIVE provider probe:
  **HTTP 401 invalid_api_key** — live evaluation BLOCKED (key invalid, not just quota).
- P3.5 `usage_analytics.py` + admin `/api/usage/*` + AdminConsole Usage tab
  (org-scoped; cost honesty: unknown pricing never estimated).

Regression: 37 suites 493 passed; 2 failures pre-existing (stash A/B verified).
Frontend: build exit 0; same 8 pre-existing foreign-WIP failures. No commits.
**Classification: INTEGRATION-TESTED; live E2E BLOCKED (invalid provider key).**

## Phase 3.5 — Autonomous Recovery Completion (2026-10-31, zero-credit mission)

Closed the Phase-3 recovery gap: `recovery_resumer.recover_interrupted_plans`
scheduled pass (PLAN_NODE_RECOVERY, 1800s, settings-gated) now CONSUMES
`durable_execution.resume_decision` over persisted PlanNodes — stale read-only
→ re-eligible, approval-gated → paused at gate, destructive → honest failed
(never auto-repeated), dep-blocked → audited, completed → preserved; every
action audited as a recovery ObservationRecord; no AgentInvocation rows
created; idempotent. Also: provider diagnostics (offline CLI + service; found
`LLM_MODEL=deepseek-v4.1-flash` NOT in enabled llm_models registry — likely
401 root cause), zh/en paired-audit (4 genuine marker gaps fixed), semantic
scenario eval (10 families, deterministic embedder), 10-scenario credit-free
acceptance suite (golden-format compatible).

Tests: 63 new, all green; 43-suite sweep 571 passed (3 pre-existing,
stash-verified). Build exit 0. Zero LLM credits consumed.
**Classification: INTEGRATION-TESTED; live BLOCKED (invalid key + registry drift).**

## Chat UI & Agent Execution Experience Repair (2026-10-31, zero-credit)

Root causes (traced before any edit): (1) AgentExecutionTree synthesized a
permanent-Running generic "sub-agent" node from EVERY standalone SSE branch +
MessageBubble never passed conversationId so the authoritative REST tree was
unreachable + footer counted feed rows vs the tree's per-agent counters;
(2) assistant bubble shrink-wrapped at max-w-90% of the column; (3) TurnPlan
already classified "hi" as direct_response but nothing enforced it — the loop
kept delegate_task in the toolset and the local LLM fanned out; (4) backend:
no terminal subagent_returned guarantee on cancellation, no subagent_id on
returned events, delegate finally-fallback could insert terminal DUPLICATE
next to a stuck running row, `/executions/{id}/agent-tree` dead on a
NameError typo.

Fixes: direct_response ENFORCED (toolset cleared via in-place slice assignment
— closure-scoping gotcha caught by tests); real agent identities on
subagent_invoked; subagent_id + interrupted-outcome terminal guarantee
(pre-done yield + finally persistence); fallback insert only when the start
row is genuinely absent; tree rewrite (real delegations only, terminal
reconciliation, node-derived summary); end-of-stream interrupted
reconciliation; assistant messages fill the readable column; conversationId
(AgentConversation identity) wired through.

Tests: 11 backend + 12 frontend NEW, all green; focused 194 passed; broad
sweep 587 passed (3 pre-existing stash-verified); frontend 1029 passed
(27 pre-existing/foreign-WIP); build exit 0. Zero LLM credits.
**Classification: INTEGRATION-TESTED (deterministic); live UX pass pending valid provider key.**

## End-to-End Execution Consistency Investigation (2026-10-31, zero-credit)

Root-cause audit: `docs/architecture/zhanlu-chat-execution-consistency-root-cause-audit.md`.
CONFIRMED + FIXED (TDD, RED→GREEN): (D1) execution tree was conversation-scoped — every
assistant bubble rendered ALL prior turns' supervisor rows ("two general_assistant
Completed" = two real turns); `build_agent_tree(started_after=…)` turn-window scoping +
`/executions/tree?started_after=` + frontend anchor (first persisted live-event ts, server
clock) + orphan sub_agent rows now attach to their enclosing supervisor turn. (D2)
direct-execution turns had ZERO evidence (pipeline was delegation-only; live DB proof:
all 226 invocation rows output_json=NULL) — successful data-tool results now stored as
real EvidenceRegistry items (compact findings, unvalidated) referenced from the turn's
output_json.evidence_ids. (D3) verify gate was a pure OR over tool failures (one failed
read_file stamped "Verification failed" on a fully grounded answer) — new
`_verification_verdict`: fails only when failures exist AND no data grounding;
grounded-but-with-failures passes with failed_steps listed.
CONFIRMED, fix scheduled: (D4 P0) FSM route dual-persists a chat_messages row per turn
(_persist_fsm_turn INSERT with fresh uuid vs client optimistic row). Mitigated/watch:
(D5) empty content_replace resets. Pending live provenance: (D6 P4) report data quality
(Oct-29 vs Oct-30 coverage → likely warehouse cut-off or date-boundary SQL; needs the
turn's persisted SQL via NL2SQL lineage). Live DB verified: conv 830debd4 = 2 GA rows.
Tests: 12 new (tree scoping 6, evidence+verdict 6); focused regression 324 passed;
frontend 1029 passed (same 27 pre-existing/foreign-WIP); build exit 0. Discovered
pre-existing defect: test_react_stream_recovery_e2e's data cleanup wipes plans rows →
order-dependent FK failures in test_auto_resumption (test-isolation, not production).
Zero LLM credits. **Classification: INTEGRATION-TESTED (deterministic); D4/D6 pending.**

## D4 Authoritative Chat Message Lifecycle (2026-11-01, zero-credit)

Doc: `docs/architecture/zhanlu-authoritative-chat-message-lifecycle.md`. D4 P0 twin
FIXED (TDD, RED→GREEN 8→9): `_persist_fsm_turn` now consumes the Stage-2B transported
`assistant_message_id` (Commit 3) — client's optimistic row is finalized IN PLACE
(validated session+role+not-deleted), legacy INSERT only for old/missing/foreign rows
(cross-session row never adopted), regenerate writes nothing, commit failure rolls back
BOTH stores; return contract `{"messages", "assistant_message_id", "answer_persisted"}`;
done payload attests `answer_persisted` + `assistant_message_id` → existing frontend
`backendOwnsContent` handler relinquishes its write (single writer; NO frontend
identity changes needed — pipeline was 80% pre-built). D5 root cause CONFIRMED+
FIXED (deterministic vitest repro): server retries emit `content_reset`/`content_replace
""`; frontend rejected empty replace (invariant I1) but had no content_reset handler →
post-retry deltas appended onto stale content (transient doubling). Fix: pure
`applyDelta(previous, delta, pendingReset)` in answerLifecycle.js + `content_reset`
branch/pendingReset flag in Chat.jsx — no retry-semantics change. Out of scope:
v2 blocking route 7360 (own `_assistant_msg`, no twin), legacy no-identity clients.
Tests: 9 new backend FSM-identity tests + stage1 contract update (49/50, one stale
pre-existing stash-verified assertion in test_v3_fsm_stream); frontend answerLifecycle
55/55 (4 new D5); full sweep 1033 passed / 27 pre-existing (steer_wiring stash-verified
A/B identical; Sidebar/previewToken/translations/ProjectDetail untouched files); build
exit 0. Zero LLM credits. **Classification: INTEGRATION-TESTED (deterministic); live UX
pass pending. D6 (report data quality provenance) remains open.**

## Enterprise reliability milestone — Projects-not-Departments correction (2026-11-01)

Architecture decision applied: Organization = tenant isolation; PROJECT = business
context + resource boundary; agents = reusable capabilities (ProjectAgent join
table allows multi-project membership); no Department layer introduced. Project
infrastructure audited and confirmed real/load-bearing (project_access.py,
project_kb_membership.py, data_source_runtime project context, conversation
project identity); legacy `department` label frozen as discovery-only metadata
(pinned by foreign tests, never used for authorization — not removed per
"prove unused first"). Acceptance suite reframed to the five automobile PROJECTS
(+3 new boundary scenarios = 11 total, all green vs REAL enforcement services);
P0 fixed: `_list_available_agents` cross-tenant discovery leak (now org-scoped,
legacy no-context callers warn+legacy; TestDiscoveryOrgWall). Docs: audit §9,
scenarios header, implementation §6. Zero LLM credits.

### Consolidated regression sweep (2026-11-01, sequential 16-batch, 921 top-level files)

4466 passed / 17 skipped / 412 distinct FAILED / 3579 module-setup ERRORS.
Attribution: (a) the 3579 errors = pre-existing test-order pollution in the shared
T1 sqlite DB — a cleanup fixture deletes rows from tables other fixtures never
created in that batch ("no such table q_sql_examples"); VERIFIED pre-existing:
3 heavily-errored modules pass standalone (75/75, 53/53, 72/74). (b) Of the 412
failures, subsystems touched by this milestone: test_turn_evidence (1, C-1
contract evolution — honest "partial" stamp replaces the constant
"unvalidated"; test updated with intent preserved) and discovery (org wall —
new tests green). All other failures are foreign-WIP drift (prompt/report-copy
pins: DATA_AGENT_PROMPT text, report wording, schema-slice length 431>300) or
environment-broken (test_v3_stream_persists_assistant hardcodes
/root/zhanlu/backend/... path), or pre-existing (execution_gate db=None,
forecast flag FORECAST_OILCHEM_EXOG_ENABLED never landed, web-search token
heuristics, docx/pptx skill suites). Parallel sweeping (xargs -P4) is NOT valid
on this repo — legacy tests use CWD-relative sqlite files (./test_runtime.db)
and corrupt each other; sequential only.

### Milestone consolidation — Enterprise Multi-Agent Reliability (2026-11-01)

Full architecture + acceptance matrix: `docs/architecture/zhanlu-enterprise-multi-agent-reliability.md`.
Delta on top of the earlier same-day milestone pass:

- **Unified tool-access contract** (replaces the "exact operator contract" reading):
  AgentApp `enabled_tools` are PREFERENCES (priority head, always injected),
  `disabled_tools` a hard deny (wins even over always-on evidence tools), and
  `tool_config.strict_tools: true` is the explicit opt-in that restores the EXACT
  allowlist. Non-strict children get bounded task-relevant discovery
  (`_bound_child_toolset`: head protected, remainder top-K ranked by the delegated
  task, `search_tools` escape hatch appended) and the RBAC availability filter is
  now applied to child toolsets (previously bypassed — children could be advertised
  tools their role cannot invoke). `AGENT_UNIVERSAL_TOOL_ACCESS=false` still
  restores the full legacy contract. Files: `delegate_tool.py` (toolset builder +
  helper + child finding contract + delegate_task schema description),
  `models/agent_app.py` (tool_config doc), `config.py` (flag doc),
  `tests/test_delegate_agentapp_aware.py` (contract tests: default/strict/deny/
  bounding ×4 new, 12 green).
- **Verification stage in the live panel**: frontend consumes the EXISTING
  `verify_passed`/`verify_failed` + `phase_enter.verify` live events (no duplicate
  event system) and renders a supervisor Verification badge
  (`AgentExecutionTree.jsx`, 3 new tests; scoped to turns where specialists ran so
  plain messages stay panel-free).
- **Child-side finding contract** injected into every delegated child: direct
  answer first, per-figure source citation, explicit limitations, structured for
  supervisor synthesis; the "restricted toolset" wording removed everywhere.
- **Honest evaluation**: enterprise scenarios A–E demonstrated at the
  deterministic orchestration level (`test_p1_scenarios.py` green); live-LLM
  end-to-end NOT executed (no credits) — acceptance matrix marks this PARTIAL.
- **Focused verification**: 9 backend suites green (90 tests; 1 documented
  nondeterministic flake in the MAS prepass — shared-SQLite order pollution,
  capability_router untouched); frontend lint clean on all modified files,
  vitest 34/34, `vite build` OK. Pre-existing branch-failure baseline re-verified
  identical at HEAD (stash/patch/restore diff) before attribution.
- **Risk register**: `KEY_AUTO_BIND_ALL_DATASOURCES` not present in this codebase
  (stale audit reference) — retired. Residuals documented honestly in §10/§13 of
  the new doc (legacy span_id parent storage, per-tool audit rows, sandbox
  pids-limit, live scenarios).
- Foreign WIP untouched; no commits made.

### Final acceptance review (2026-11-01)

Two real bypasses found and fixed at the invocation boundary, no new features:
1. **Agent tool policy was assembly-only** — a strict AgentApp child could
   guess a non-allowlisted (or `disabled_tools`) tool name and `execute_tool`
   would run it (only generic permission + tier policy checked). Fix:
   `execute_tool` now loads the caller's effective policy from
   `context["agent_app_id"]` via the new `app/services/agent_tool_policy.py`
   helper and denies (hard deny for disabled_tools incl. always-on evidence
   tools; exact-allowlist for strict_tools; fail-open ONLY on lookup errors
   with warning). Universal access + all Project/org/RBAC/data-source
   controls unchanged.
2. **`search_tools` was invocable-dead**: its 1-arg handler crashed under the
   governed `(args, db, user_id)` dispatch — the escape hatch could never run
   via `execute_tool`. Fixed arity + results now filtered by the caller's
   policy (discovery never advertises forbidden tools).

Also: verification-event consistency fix — a stream dying after
`phase_enter.verify` but before a verdict no longer shows a permanent
"running" verification badge (`not_run` downgrade at finalize; same
`verify_passed/verify_failed` names on React + FSM routes, verdict wins).
`done` emitted post-persist confirmed, with honest `answer_persisted:false`
fallback frame.

Live orchestration: NOT RUN live (no LLM credits; local deployments don't run
this code) — deterministic path verification instead: 100 backend tests / 10
suites (incl. 10 new `tests/test_agent_tool_policy_gate.py`) + 36 frontend
component tests + lint clean + `vite build` OK. Acceptance matrix updated in
`zhanlu-enterprise-multi-agent-reliability.md` §15.

### Unified Agent Execution Timeline — UI consolidation (2026-11-01)

The chat's three duplicated execution displays (plan checklist / live
execution rows / Agent Execution panel) merged into ONE primary
`TaskExecutionTimeline` card (`frontend/src/components/chat/
TaskExecutionTimeline.jsx`): steps from the ACTUAL plan events (never
hardcoded), delegated specialists nested INSIDE the step they served
(deterministic ts-window attribution, "N tool calls" summaries, raw tool ids
only in advanced details, `[data]` placeholders → honest "Step N" labels),
and the full AgentExecutionTree preserved inside a collapsed "Technical
Execution Details" `<details>`. Single source of truth: everything derives
from the same `live_events` via the existing `buildTreeFromLiveEvents` /
`summarizeNodes` reducers; MessageBubble gates on `hasExecutionContent`
(plain turns unchanged); LiveActivityStream gains `suppressExecutionRows`
(keeps reasoning/search/data previews, drops duplicated execution rows).

Status-consistency root cause FIXED (traced, not hidden): (1) tool rows
paired by blind LIFO stack → mispairing + phantom duplicates from replays —
now paired by operation identity (`tool_name`); (2) last-wins dedupe could
overwrite terminal states with stale frames → terminal types now sticky;
(3) residual running rows said `interrupted` while the tree said `cancelled`
→ shared `finalizeResidualRows` uses the tree's `cancelled` vocabulary
(failures stay `failed`; cancellation ≠ failure ≠ timeout).

TDD: 24 new tests written FIRST (14 timeline incl. all 9 acceptance
scenarios + 10 terminal-guard), RED→GREEN. Regression: all 85
LiveActivityStream/ActivitySteps tests green, 61 tree/timeline/MessageBubble
green, Chat consumer family green (1 pre-existing baseline failure),
lint clean, `vite build` OK. Foreign WIP untouched; no commits.

