# Zhanlu Enterprise Multi-Agent Reliability — Final Architecture & Milestone Consolidation

**Date:** 2026-11-01 · **Branch:** workspace working tree (uncommitted, mixed with foreign WIP — see §13) · **Supersedes:** the "exact operator contract" reading of `AGENT_UNIVERSAL_TOOL_ACCESS` from the same-day earlier milestone pass

---

## 1. Final architecture (one paragraph)

The General Assistant (GA) is the supervisor. It routes every turn DIRECT (own tools, including `ask_data_agent`), SINGLE-SPECIALIST (one `delegate_task`), or MULTI-SPECIALIST (one bounded `delegate_task` batch or a dependency-aware plan DAG). Specialists are published AgentApps (org-scoped, Project-anchored) or builtin managed agents; each child runs in its own governed tool loop with its own AgentInvocation row, live SSE event stream, and evidence contract. Every tool invocation — supervisor or child — flows through the same `execute_tool` layer (`check_permission`, agent tier policy, data-source access policies, credential isolation). The Agent Execution panel renders the whole thing live from SSE events and dedupes onto the persisted tree via `agent_execution_id`.

## 2. Universal tool-access policy (unified contract, 2026-11-01)

Three separated layers — **discovery ≠ availability ≠ authorization**:

| Layer | Mechanism | Where |
|---|---|---|
| Discovery | Every eligible child may discover the shared registry. Non-strict children get `search_tools` in their toolset; the child tool loop executes any named tool through `execute_tool` (no schema-membership gate) | `delegate_tool.py::_bound_child_toolset`, `_run_sub_agent_inner` |
| Availability | Injected schemas = priority head + task-relevant top-K (`TOOL_SEARCH_TOP_K`, ranked by the delegated task) + `search_tools` escape hatch. RBAC availability filter (`authz.filter_resources`) strips tools the current role cannot invoke — same B1 filter `registry.get_schemas` applies | `delegate_tool.py` toolset builder |
| Authorization | Never relaxed: `execute_tool` re-checks `check_permission`, agent tier policy, org/project boundaries, data-source policies per invocation. A denied tool fails honestly | `agent_tools.py::execute_tool` |

**AgentApp `tool_config` shape** (`models/agent_app.py`):

```json
{
  "enabled_tools":  ["query_database"],   // preferences / priority head — ALWAYS injected
  "disabled_tools": ["memory"],           // hard operator deny — wins over everything
  "strict_tools":   false                 // true => enabled_tools is an EXACT allowlist
}
```

Decision matrix (`delegate_tool.py::_run_sub_agent_inner`):

| Child | `AGENT_UNIVERSAL_TOOL_ACCESS=true` (default) | `=false` (rollback) |
|---|---|---|
| AgentApp, `strict_tools=true` | exact `enabled_tools` allowlist (minus `disabled_tools`, restricted) | same |
| AgentApp, `strict_tools=false` (default) | priority head + bounded task-relevant discovery + `search_tools` | exact allowlist (legacy) |
| Builtin child | bounded task-relevant discovery + `search_tools` | `enabled_by_default` only (legacy) |

Protected from bounding: operator head, `provide_evidence`/`request_evidence` (always-on evidence contract, unless explicitly `disabled_tools`-denied), and the builtin child's dynamic entity-CRUD schemas. `delegate_task` stays restricted unless `COLLAB_NESTED_DELEGATION_ENABLED && COLLAB_POLICY_ENFORCED` (broker governs every nested call).

## 3. Tool-discovery and authorization model

- `search_tools` meta-tool (`tool_search.py`) — keyword/semantic search over the full RBAC-filtered catalog, returns name/description/params cards; the agent then invokes the returned tool name directly.
- Bounding reuses the same ranking as the supervisor loop (`select_relevant_tools`), so prompt token cost is bounded (~`TOOL_SEARCH_TOP_K`) regardless of registry size (190+ tools).
- Dynamic entity-CRUD schemas are generated per child context, not copied from the registry.
- Authorization is checked at: discovery (`list_available_agents` — published + org-scoped only), availability (RBAC filter), invocation (`execute_tool`), and data access (per-source policy + SQL validation).

## 4. General Assistant supervisor responsibilities

Doctrine lives in the GA system prompt (`agent_prompts.py` "ENTERPRISE ORCHESTRATION" block): decide the mode first; match by capability + data domains from `list_available_agents` (never guess names); write self-contained briefs that demand grounding + source tables; aggregate with per-finding attribution; never present a partial/failed child as complete; never delegate trivial questions. Hard backstop: `_enforce_turn_execution_strategy` clears the toolset when the TurnPlan says `direct_response`.

## 5. Specialist delegation

- `delegate_task` (single), `tasks` batch (heterogeneous `{task, agent_name, max_iterations}`, bounded by `DELEGATE_MAX_CONCURRENT=4`, per-item machine-readable `status`), plan-DAG agent nodes (routed through the same governed `delegate_task` path).
- Child-side finding contract (injected into every child's system prompt): direct answer first; cite the concrete source per figure (table/query/file); report missing data / failed tools / uncovered scope explicitly; structured and concise for supervisor synthesis.
- Governance: Collaboration Broker (`cycle/depth/child-count/dedup`), wall-clock budget (`DELEGATE_WALL_SECONDS=600`, real `asyncio.wait_for` cancellation), iteration budgets, parent-context digest (flag), Agent Task Contract lineage.

## 6. Parallel and sequential orchestration

- Parallel: batch delegation (same-input or per-task fan-out, semaphore-bounded) and plan-DAG ready-batch prepass (`PLAN_DAG_PARALLEL_ENABLED`, `PLAN_DAG_MAX_CONCURRENT=4`, isolated DB session per node). Adaptive replanning forces serial.
- Sequential: the prompt doctrine instructs dependent steps as separate `delegate_task` calls feeding outputs forward; the DAG planner encodes dependencies and only dispatches dependency-satisfied nodes.
- Deduplication: batch fingerprints + loop guards prevent repeated identical subtasks/tool calls.

## 7. Agent invocation lifecycle

`record_invocation(open_invocation=True)` at delegation start (row = the stable `agent_execution_id`) → `update_invocation_terminal` on the SAME row in a `finally` block — first-terminal-wins, never-regress, statuses `{completed, partial, failed, timeout, cancelled, blocked}`. Timeout → `timeout`; cancellation → `cancelled` (re-raised). The Data Agent has the same pattern (`_begin/_finish_data_agent_execution`) with `output_json.agent_name="data_agent"` giving it distinct identity while the AgentApp FK anchors to `general_assistant` (FK is non-nullable by schema). Orphaned `running` rows recover to truthful terminal states (turn-scoped tree adoption).

## 8. Real-time SSE event architecture

Single event system: `live_event` frames. Mapping of the required moments to EXISTING event names (no duplicate event system was introduced):

| Moment | Event |
|---|---|
| Supervisor started / planning | `phase_enter.init` / `phase_enter.plan`, `plan_step_added`, `plan_step_completed`, `plan_summary` |
| Delegation requested / specialist started | `agent_started` (carries `agent_execution_id`, `parent_agent_execution_id`, `agent_id/name/label`, `delegation_mode` peer\|supervisor, `task`) |
| Specialist progress | `agent_progress` |
| Tool started / completed / failed | `agent_tool_started` / `agent_tool_completed` (`status=failed` carries `error`) — tools render under the specialist that invoked them, never counted as agents |
| Agent completed / partial / failed | `agent_completed` (`status=partial` supported) / `agent_failed` |
| Evidence registered | `agent_completed.evidence_ids` / `evidence_count` + the evidence REST contract |
| Verification started | `phase_enter.verify` (FSM route) |
| Verification completed | `verify_passed` / `verify_failed` (honest per-tool last-attempt verdict; `failed_steps` listed, grounded-but-with-failures passes) |
| Final synthesis | `finalize_started` / `phase_enter.finalize` |
| Final answer | `done` |

Reserved (defined, intentionally not emitted): `agent_result_received` (redundant with `agent_completed`), `peer_collaboration_started` (peer status is carried on `agent_started.delegation_mode` — set ONLY on real peer exchange, never inferred).

Frontend (`AgentExecutionTree.jsx`): `buildTreeFromLiveEvents` synthesizes the live tree keyed by `agent_execution_id` during streaming; the panel mounts as soon as live events exist; terminal-without-start tool rows still render; unmatched running nodes become `cancelled` at stream end (never a permanent Running); turn-scoped — no cross-turn bleed. The supervisor verification stage renders as a badge from the existing `verify_*` events.

## 9. Evidence and provenance

`provide_evidence`/`request_evidence` exchange findings by reference (no dataset copies); evidence rows carry `query_provenance` (SQL + NL), `validation_status`, `freshness`, `metric_definition_ids`, `references`, `assumptions`, `limitations`. The verification verdict (`_verification_verdict`) uses per-tool LAST-attempt status — a transient failure that was retried successfully no longer fails verification; grounding comes only from successful DATA-evidence tools.

## 10. Project isolation and security

**Final acceptance review additions (2026-11-01, post-implementation review):**

1. **Invocation-boundary enforcement (was a real bypass, now fixed).** Toolset assembly alone was the only place `strict_tools`/`disabled_tools` were enforced — a strict AgentApp child could invoke a non-allowlisted tool (or a `disabled_tools` entry) by GUESSING its name, because `execute_tool` checked only generic permission + tier policy. Fix: `execute_tool` now loads the caller AgentApp's effective policy (via `context["agent_app_id"]`, new helper `app/services/agent_tool_policy.py`) and denies at the invocation boundary — hard deny for `disabled_tools` (wins over always-on evidence tools), exact-allowlist denial for `strict_tools` (evidence tools admitted, matching the assembly contract). Universal access for non-strict agents unchanged; all Project/org/RBAC/data-source controls unchanged. Lookup errors fail open with a warning (same advisory-on-error contract as the tier policy); a loaded policy is fail-closed. Tests: `tests/test_agent_tool_policy_gate.py` (10).
2. **`search_tools` was invocable-dead and policy-blind (both fixed).** The handler's 1-arg signature crashed under the governed invocation contract `(args, db, user_id)` — the escape hatch could never actually run via `execute_tool`. Fixed to the full signature, and results are now filtered by the caller's effective policy so discovery never advertises a forbidden tool. For strict agents `search_tools` itself is outside the allowlist and is denied at invocation.
3. **Verification event consistency (fixed).** A stream that saw `phase_enter.verify` (FSM VERIFY entered) but died before a verdict would have shown a permanent "running" verification badge. `buildTreeFromLiveEvents(finalize=true)` now downgrades that to an honest `not_run` ("not completed"); real verdicts (`verify_passed`/`verify_failed` — the SAME event names on both the React tool-loop and FSM routes) always win. Tests codify both routes.



- Agent discovery is organization-scoped (published + same org only — org-wall tests).
- Data access is enforced by the data-source access-policy layer + SQL validation per invocation, inside Project/K bindings; the child is permission-checked AS ITSELF (parent identity deliberately NOT inherited).
- Projects remain the business-context boundary. No Department authorization layer exists (the `department` label is discovery-only metadata).
- `KEY_AUTO_BIND_ALL_DATASOURCES`: NOT present in this codebase (stale reference from an older audit/branch) — retired from the risk register; the current binding path is `data_source_runtime.prepare_data_source_runtime` (bound KB ids, workspace bindings).
- Residual (documented, not masked): execution/artifact Project identity through session indirection (audit item — indirect but guarded by org walls), no per-tool `audit_events` rows for child tool calls, sandbox `--pids-limit` flag missing, `done`-after-failed-commit edge (full-system audit P0 list, different branch lineage).

## 11. Retry, timeout, cancellation, failure handling

- Retries: reliability wrapper with retryable classification; per-tool verification reconciliation (last attempt wins); delegation retry consumes remaining wall-clock budget only.
- Timeouts: per-child wall clock (`asyncio.wait_for`) → `timeout` status; LLM calls carry their own timeouts.
- Cancellation: mid-turn cancel → child ctx cancel checks → `cancelled` terminal, no orphaned running rows (orphan recovery as backstop).
- Failures: structured `status=failed` with `error`; batch summary `N completed, M partial, K failed`; the supervisor must mark affected conclusions incomplete (prompt doctrine).

## 12. Acceptance scenarios (evaluation, not fabrication)

Deterministic orchestration-level acceptance exists in `tests/test_p1_scenarios.py` (A cross-domain DAG, B partial surfacing, C scoped discovery, D explicit failures, E cross-domain batch — all green) and `tests/test_p1_orchestration.py`. These run against REAL enforcement services (org walls, broker, batch contracts) with no LLM calls.

**Honest status: PARTIAL.** Live-LLM end-to-end runs of Scenarios A–E (with real sales/manufacturing/supply-chain/quality data) were NOT executed in this milestone — no live model credits are available in this workspace and fabricating a "PASS" would violate the mission rules. The orchestration mechanics each scenario needs (discovery, delegation, parallel/sequential, evidence, honest status) are individually demonstrated by deterministic tests; the composed live path is demonstrated only by prior manual live sessions (e.g. Supplier 2-link traversal, pack chains — see progress log).

## 13. Remaining technical limitations

1. `parent_invocation_id` is stored in `span_id` (no dedicated parent column on the legacy table) — the tree model's dedicated column covers new paths; legacy rows rely on adoption logic.
2. MAS prepass test `test_parallel_prepass_runs_independent_nodes_concurrently` is nondeterministically flaky (shared-SQLite order pollution — passes standalone; documented same-split comparison rule). Not caused by this milestone (`capability_router.py` untouched here).
3. Pre-existing failure baseline on this branch (NOT this milestone): 14 data-agent suite failures (schema-slice length pins, iteration drift, empty-answer synthesis, fastpath linker) + `test_delegate_phase2_wiring::test_approved_review_decision_is_complete` + 7 frontend files (source-regex tests vs foreign-edited Chat.jsx, Sidebar, previewToken, translations, ProjectDetail ×2). All verified identical at HEAD via stash/patch/restore diff.
4. `strict_tools` is enforced at toolset assembly + `execute_tool` permission layer, not by a separate DB-level policy object.
5. Live-LLM enterprise scenarios not executed (see §12).
6. The working tree mixes foreign WIP (models, routers, services — including `grounding.py`, `nl_answer_service.py`); this milestone touched ONLY: `delegate_tool.py`, `delegation_tools.py` (prior pass), `agent_app.py` (docstring), `config.py` (docstring), `sse_builders.py` (prior pass), `agents.py` (prior pass), `AgentExecutionTree.jsx`, `MessageBubble.jsx` (mount + import cleanup), and the two test files. No commits made.

## 14. Milestone acceptance matrix

| Capability | Required outcome | Status | Evidence |
|---|---|---|---|
| Universal tool access | All authorized agents can access the shared tool ecosystem | **COMPLETE** | Unified contract (preferences + discovery + escape hatch); builtin + AgentApp default paths; RBAC availability filter; `tests/test_delegate_agentapp_aware.py` (12 tests) |
| Strict restrictions | Explicit operator restrictions remain enforceable | **COMPLETE** (enforced at ASSEMBLY **and** INVOCATION — post-review) | `strict_tools` exact-allowlist test; `disabled_tools` hard-deny test (incl. over always-on evidence tools); guessed-name invocation denied at `execute_tool` (`test_agent_tool_policy_gate.py`, 10 tests); global rollback flag |
| Agent discovery | Dynamic and authorization-aware | **COMPLETE** | `search_tools` meta-tool + bounded top-K ranking; org-scoped `list_available_agents` (org-wall tests) |
| Supervisor orchestration | Intelligent task decomposition | **COMPLETE** | Prompt doctrine + `_enforce_turn_execution_strategy` + plan DAG; `test_p1_orchestration.py` |
| Specialist delegation | Correct specialist selection | **COMPLETE** | Capability+domain matching tests; batch per-task dispatch; agent-node DAG dispatch |
| Multi-agent collaboration | Parallel and sequential coordination | **COMPLETE** | Bounded batch + DAG prepass + nested-delegation broker; `test_multiagent_mas.py` (1 nondeterministic flake documented) |
| Tool execution | Actual tool invocation and structured results | **COMPLETE** | Governed `execute_tool` for children; structured status contracts; finding contract in child prompt |
| Real-time visibility | Execution appears before final answer | **COMPLETE** (deterministic path; live-LLM run NOT executed) | Live panel during streaming; heartbeat-drained agent frames; verification badge with cross-route consistency (`not_run` guard); 24 frontend component tests. Full path User→GA→discovery→delegation→child discovery→tools→evidence→result→synthesis→answer traced through the actual v3 handler wiring (agents.py:17777 heartbeat drain; `done` emitted post-persist with honest `answer_persisted:false` fallback); live-LLM run not executed (no credits; serving deployment does not run this workspace's code) |
| Evidence | Resolvable evidence with provenance | **COMPLETE** | Evidence tools + REST resolution + provenance fields (JSX tests incl. raw-row secrecy) |
| Project security | Project and organization isolation | **COMPLETE** | Org-wall tests; child-identity permission checks; Project boundary unchanged |
| Message consistency | No duplicate or overwritten final answers | **COMPLETE** | Duplicate-answer guards (prior milestone); first-terminal-wins invocation rows |
| Enterprise scenarios | End-to-end acceptance demonstrated | **PARTIAL** | Deterministic orchestration scenarios green; live-LLM runs not executed (no credits) — §12 |

---

*Verification (2026-11-01): 90 backend tests across 9 focused suites green (1 documented flake); frontend lint clean on all modified files; vitest component suites 34/34; `vite build` OK. Baseline failures attributed before acceptance (§13.3).*

## 15. Final acceptance review (2026-11-01, post-implementation)

### 15.1 Unified Agent Execution Timeline (UI consolidation, 2026-11-01)

The chat previously rendered the same operations in THREE places (plan checklist in the activity feed, live execution rows, Agent Execution panel) — visual duplication and the "Running (live) vs Cancelled (panel)" inconsistency.

**New architecture** (`frontend/src/components/chat/TaskExecutionTimeline.jsx`):
- **A. Task Progress Card** — the ONE primary execution card: steps from the ACTUAL execution plan (`plan_step_added`/`plan_step_completed` live events, or live `streaming_plan_steps`; never hardcoded), compact progress bar, vertical stepper with honest pending/running/done statuses.
- **B. Nested Agent Activity** — delegated specialists render INSIDE the step they served (deterministic timestamp-window attribution: an agent belongs to the step in progress when it started). Business-friendly rows: agent identity + status + "N tool calls"; raw tool identifiers stay in the advanced section. Raw redaction placeholders (`"[data]"`) fall back to honest positional labels ("Step N") — never fabricated names.
- **C. Technical Execution Details** — the full AgentExecutionTree (names/roles, tool invocations, delegation relationships, durations, peer collaboration, evidence, verification, errors) moves into a collapsed `<details>` section inside the card. All existing tree functionality preserved.

**Single source of truth:** the timeline derives everything from the SAME `live_events` stream via the EXISTING pure reducers (`buildTreeFromLiveEvents`, `summarizeNodes`). No new state-management system; `MessageBubble` gates on `hasExecutionContent` (plan or agent events) — plain conversational turns keep the existing UI. `LiveActivityStream` gains `suppressExecutionRows` (keeps reasoning/search/data previews; drops the duplicated execution rows + checklist + progress bar).

**Status-consistency root cause (traced, fixed — not hidden):**
1. Tool rows paired by a blind LIFO stack — concurrent same-name tools mispaired and replayed starts became phantom duplicate rows → now paired by OPERATION IDENTITY (`tool_name`), terminal-without-start still renders honestly.
2. Dedupe was last-wins — a stale frame could overwrite a terminal state → terminal types are now STICKY in `dedupeAndSortEvents`.
3. Residual running rows were relabeled `interrupted` at stream end while the agent tree reported `cancelled` — the two displays used different words for the same state → shared `finalizeResidualRows` now uses the tree's `cancelled` vocabulary; real failures keep `failed`; cancellation is distinguished from failure everywhere.

**Tests:** 24 new (14 `TaskExecutionTimeline.test.jsx` covering all 9 mission scenarios + 10 `LiveActivityStream.terminalGuard.test.jsx`); all 85 existing LiveActivityStream/ActivitySteps tests green; 61 tree/timeline/MessageBubble tests green; lint clean; `vite build` OK. (`Chat.completionNotify` failure = documented pre-existing baseline; Chat.jsx carries foreign WIP untouched.)

### 15.2 Security guarantees (same-day review)

**Security guarantees confirmed:**
- A strict AgentApp child **cannot** invoke a tool outside its allowlist by guessing its name — denied at `execute_tool` before any handler runs (`test_strict_agent_cannot_guess_tool`).
- A `disabled_tools` entry is denied on every invocation, even when also listed in `enabled_tools`, even for always-on evidence tools (`test_disabled_tool_denied_even_when_enabled`, `test_disabled_always_on_tool_denied`).
- `search_tools` cannot expose forbidden tools: results are filtered by the caller's effective policy; for strict agents `search_tools` itself is denied at invocation (`test_search_tools_never_advertises_disabled_tools`).
- Universal access for non-strict agents preserved — non-strict children can still invoke discovered tools (`test_non_strict_agent_can_invoke_discovered_tool`); all Project/org/RBAC/data-source authorization controls unchanged (`test_no_agent_app_no_gate`, org-wall suites green).

**Bypasses found and fixed (2):** (1) strict/disabled policy was assembly-only → now enforced at the invocation boundary (`agent_tool_policy.py` + `execute_tool` gate). (2) `search_tools` handler crashed under the governed invocation contract (1-arg vs 3-arg dispatch) → arity fixed + policy filtering added.

**Live orchestration verification status: NOT RUN live — deterministic path verification complete.** No live-LLM credits in this workspace and the reachable local deployments (:8089 frontend / :5002 foreign backend) do not run this workspace's code. The full path (User → GA → specialist discovery → delegation → child tool discovery → tool execution → evidence → result → synthesis → answer) is verified by: 100 backend tests across 10 focused suites incl. the real orchestration machinery (`test_p1_orchestration`, `test_p1_scenarios`, `test_enterprise_reliability_p1`), the invocation-boundary gate tests, and 36 frontend component tests; the SSE wiring is anchored at agents.py:17777 (heartbeat drain of live agent frames) and the `done` frame is emitted post-persist with an honest `answer_persisted:false` fallback. Both single-specialist and multi-specialist (batch + DAG) paths are covered by the deterministic suites.

**Verification event consistency:** same `verify_passed`/`verify_failed` event names on React tool-loop and FSM routes; frontend badge handles both param shapes; `not_run` guard prevents a phantom verification stage (tests: `verify-started without a verdict at stream end becomes NOT-run`, `a real verdict always wins over the started state`).

**Test totals this review:** 100 backend (10 suites, including 10 new policy-gate tests) + 36 frontend component tests + lint clean + `vite build` OK. No full-repository campaign repeated; baseline failures unchanged (§13.3).
