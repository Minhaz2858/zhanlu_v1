# Zhanlu Enterprise Reliability Audit

**Date:** 2026-11-01 · **Branch:** `fix/automation-chat-artifacts-ordering` (working tree incl. uncommitted multi-session work)
**Method:** read-only. Baseline = `docs/audits/2026-10-20-zhanlu-vs-deepseek-harness-full-system-audit.md` (34 findings) + `docs/architecture/zhanlu-current-enterprise-agent-architecture-audit.md` (2026-10-31) + `docs/architecture/zhanlu-chat-execution-consistency-root-cause-audit.md` (D1–D5). Every baseline claim was re-verified against the CURRENT working tree by three parallel code-explorer passes plus direct security greps; statuses below reflect today, not the baseline date. Every claim carries file:line.
**No production code was modified in this phase.**

**Status vocabulary:** FIXED (was defective, now correct) · PARTIAL (fixed under a default-OFF flag, or partially wired) · PRESENT-DEFECT (confirmed live) · POTENTIAL (needs more evidence) · GAP (missing integration contract) · DEAD (service exists, zero production callers).

---

## 0. Executive summary

The platform's honesty/integrity core is in good shape: the P0 class from the 2026-10-20 audit (fake-success fallbacks, plaintext credentials, schema-only success, unbounded delegation, done-after-failed-commit, entity-API tenant scoping, empty-bubble persistence) is **fixed and verified** in this tree. What remains is concentrated in exactly two patterns:

1. **Fixed code left flag-gated OFF** (`DELEGATE_AGENTAPP_AWARE`, `DELEGATE_INHERIT_BINDINGS`, `DATA_AGENT_FASTPATH_ENABLED`, `KG_COVERAGE_PROBE_ENABLED`) — the fixes exist but default deployments do not run them, so cross-department scenarios A–E cannot work out of the box.
2. **Built-but-unwired reliability services** (`evidence_validation.py`, `reliability_metrics.py`, the `evaluate_observation` decision table) — Phase-2/3 work that tests exercise but no production path calls.

Plus a short tail of small confirmed defects (silent 200-row slice in `ask_data_agent`, approval-paused activity step never closed, one legacy error-text-as-assistant-row path, mobile chat missing the D5 safety layer).

**Counts (current tree):** ~200 registered tools · 14 MarketAgent rows · 7 manufacturing AgentApps · 127 RLS policies on 144 public tables · 201 NL2SQL tests · 12-state execution-tree status map.

---

## 1. Subsystem A — Agent orchestration

### Existing capabilities & locations

| Capability | Implementation | Status |
|---|---|---|
| General Assistant supervisor | `app/routers/agents.py` v3 stream (`add_message_stream` :11863); ReAct loop `_stream_llm_with_tools` (:11015) | IMPLEMENTED |
| Department-based discovery | `delegate_tool.py:1346-1519` `list_available_agents` (dept filter from conversation metadata :1372-1384, capability filter, project/KB enrichment) | IMPLEMENTED |
| Published-agent registry | `delegate_tool.py:46-59, 270-303` — `AgentApp.status == "published"` gate | IMPLEMENTED (see D-A2) |
| Specialist delegation | `delegate_tool.py` — batch mode, heterogeneous `{task, agent_name}` items (:328-455), `DELEGATE_MAX_CONCURRENT=4` semaphore | IMPLEMENTED |
| AgentApp-aware delegation (prompt + toolset propagation) | `delegate_tool.py:597-603, 838, 875-884` — 5-layer prompt via `get_system_prompt(agent_name, agent_app=…)`, child toolset = target's `tool_config` | **PARTIAL — `DELEGATE_AGENTAPP_AWARE` default False** (`config.py:1767`) |
| Context / KB inheritance to children | `delegate_tool.py:850-855` (1200-char digest, ON) ; real `bound_kb_ids`+`conversation_id` lineage (:930-952) | **PARTIAL — `DELEGATE_INHERIT_BINDINGS` default False** (`config.py:1774`) |
| Delegation wall-clock timeout | `delegate_tool.py:663-677` `asyncio.wait_for`, shared deadline :958-974, `DELEGATE_WALL_SECONDS=600` | FIXED (was A3) |
| Multi-agent collaboration (DAG) | `synexia/capability_router.py` plan-DAG, agent nodes via delegate (:620-632); parallel ready-batch prepass `PLAN_DAG_PARALLEL_ENABLED=True` | IMPLEMENTED |
| Semantic capability matching + fallback | `services/semantic_capability_match.py` ← `capability_fallback.select_alternatives`, consumed on delegation failure (`delegate_tool.py:371-383`) | IMPLEMENTED |
| Cross-agent trace tree | `AgentInvocation.conversation_id/trace_id` (`models/agent_invocation.py:26,66`); start row + same-row finalize, first-terminal-wins (`delegate_tool.py:636-652, 739-768`) | FIXED (was breakpoint ④). Caveat: parent linkage lives in `span_id`, no `parent_invocation_id` column |
| Bounded replanning | ACT replan ≤2 + VERIFY ≤1, both spend from ONE per-turn refinement budget (`capability_router.py:271-281`); turn replan cap 2 | FIXED (was A4) |
| Budgets & loop prevention | `MAX_TOOL_ITERATIONS=40`, per-agent `max_call_count` budget, wall-clock caps + ≤2 extensions (`agents.py:14820-14846`), `ToolLoopGuardController` | IMPLEMENTED |

### Confirmed defects (A)

- **A-1 (P1):** No central tool-execution timeout. `agent_tools.py` `execute_tool`/`_execute_with_reliability` contain no `wait_for`; the only guarded paths are delegate children and synthesis. A hung non-LLM tool handler stalls the turn. (Baseline D4, still open.)
- **A-2 (P1):** `DELEGATE_AGENTAPP_AWARE=False` + `DELEGATE_INHERIT_BINDINGS=False` by default — children get generic prompt + default toolset and cannot see parent's bound KBs. Discovery advertises capability routing that default config cannot honor.
- **A-3 (P2):** Delegation directory is `published`-only; manufacturing agents seeded `active` are invisible until published (data-side fix, not code).
- **A-4 (P2):** Plan-DAG prepass: `max_conc = max(2, …)` silently floors config to 2 (`capability_router.py:662`); no plan_version re-validation inside the prepass.
- **A-5 (P2):** `IterationBudget` docstring claims conversation-level; constructed fresh per turn/delegation (`agents.py:7660, 9438, 14338`; `delegate_tool.py:956`) — effective bound is per-turn.
- **A-6 (P1):** Approval-paused tool step is never closed — `running` step emitted before execution (:17406, :17486-17492), approval-pause branch returns without marking it terminal (:17842-17860, :18964-18998) → permanent "Running" indicator. Directly violates mission Phase 4 ("No permanent Running indicators").

## 2. Subsystem B — Enterprise data intelligence

### Existing capabilities & locations

| Capability | Implementation | Status |
|---|---|---|
| Governed NL2SQL | `db/nl_answer_service.py` (plan → retrieve → P-SV gate → SQL → execute gate → self-heal → narrate) | IMPLEMENTED |
| Execution gate (unconditional) | `db/execution_gate.py:59-71` — 12 checks incl. sqlglot parse, table/column existence, join reachability/fanout, row_filter, scope, metric contract+binding; "unprovable ≠ pass" | IMPLEMENTED |
| P-SV source verification gate | `nl_answer_service.py:1119-1179`, blocking, default ON | IMPLEMENTED |
| Figure-grounding guardrail | `answer_grounding.py` via `agents.py:6420-6492, 6537-6545`; every number vs tool results, 0.5% tolerance | IMPLEMENTED, all 3 loops |
| Schema-only fake success (B1) | `capability_router.py:1178-1214` honest failure, never executed | FIXED |
| Query logs | `_shadow_validate_and_log` unconditional write + row_count/duration_ms + real outcome (`nl_answer_service.py:3416-3536, 1656-1660, 3525`) | FIXED (was B7) |
| Row truncation honesty | `query_service.py:150-154` +1-row proof, cap clamp :100-106 | FIXED — one residual (B-3) |
| Evidence registry | `turn_evidence.py:27-109` records successful data-tool results as `EvidenceItem`s; ids → `AgentInvocation.output_json.evidence_ids`; consumed by `_verification_verdict` (`agents.py:4931-4960, 19137-19145`) | IMPLEMENTED |
| Evidence validation | `evidence_validation.py` (4 validators) | **DEAD — zero production callers** (tests only) |
| Hybrid RAG | `document_ingestion/retrieval.py:30-80` dense+RRF, grading+rewrite (:141-166), decomposition (:104-137); all flags ON | IMPLEMENTED |
| Catalog freshness | `knowledge_graph/catalog_coverage_refresh.py` re-probe refreshes coverage_json + indexed_at | **PARTIAL — `KG_COVERAGE_PROBE_ENABLED` default False neutralizes it** (`config.py:1607`) |
| Date-boundary misrouting | `reroute_uncovered` swaps stale `covers_window=False` tables (`nl_answer_service.py:848-857`, `table_retrieval.py:730`) | IMPLEMENTED |
| Zero vs missing | `catalog_indexer.py:1882-1889` stats stay None; `schema_linker.py:415-419` renders nothing for absent stats | IMPLEMENTED |
| Multi-source | `QueryService.execute(kb_id,…)` strictly per-KB; cross-source via ontology `read_object_data` (`ontology/data.py:24-43`) | BY DESIGN |

### Confirmed defects (B)

- **B-1 (P1):** `ask_data_agent` sub-loop has no pre-flight probe/breaker — the fix covers `query_database` only (`query_database_tool.py:41-182, 297-304`); dead warehouse still burns the full LLM pipeline via `db_tools.py` tools.
- **B-2 (P1):** Single-bound-KB auto-resolve is gated behind `DATA_AGENT_FASTPATH_ENABLED=False` / `DATA_AGENT_SWARM_ENABLED=False`; legacy sub-loop requires explicit `data_source_id` (`delegation_tools.py:564-566`, `db_tools.py:229-230`).
- **B-3 (P1):** `ask_data_agent` slices returned rows to 200 **without any truncation flag** (`delegation_tools.py:1105-1111`) — a silent-truncation surface distinct from the proven QueryService cap. Violates "no silent SQL result truncation".
- **B-4 (P2):** Honest-empty: `success=True` with `rows=None` when the sub-loop captured zero rows (`delegation_tools.py:1130`); distinguishable only via `data_quality.row_count`, not the success flag.
- **B-5 (P2):** `KG_COVERAGE_PROBE_ENABLED=False` default → catalog freshness loop inert; stale-catalog misrouting protection rests on rerouting alone.

## 3. Subsystem C — Execution reliability

### Existing capabilities & locations

| Capability | Implementation | Status |
|---|---|---|
| Failure classification + healing budget | `execution_recovery.py` taxonomy + `HealingBudget`; wired `agent_tools.py:424-530`, per-turn budget `agents.py:14842-14846` (`EXEC_RECOVERY_MAX_ATTEMPTS_PER_CALL=2`, `MAX_PER_TASK=4`) | IMPLEMENTED |
| Execution decision model | `execution_decisions.py:112-283` pure 11-value observation→decision table, authorization-first | **PARTIAL — only reviewer reports feed it** (`delegate_tool.py:258-266`); main-loop tool outcomes reach the LLM as prose |
| Durable execution / recovery | `durable_execution.py` resume-decision (stale TTL 1800s, approval always manual) ← `recovery_resumer.py`, scheduled every 30 min (`scheduled_tasks.py:289-305`), `PLAN_NODE_RECOVERY_ENABLED=True`, never executes tools | IMPLEMENTED |
| Answer anti-collapse | `answer_recovery.py` — `register_best` ×5 + `final_answer` ×4 sites in agents.py (:15927…:20752), always on | IMPLEMENTED |
| Turn diagnostics | `turn_diagnostics.py` `turn_scope` wraps the stream (`agents.py:21317-21322`) | IMPLEMENTED |
| Tool-schema sanitize | `tool_schema_sanitize.py` (`agents.py:10573, 11365`; `sub_agent_reliability.py:126`) | IMPLEMENTED |
| Approval / permissions | `PermissionChecker.check_tool` fail-closed (`permissions/__init__.py:194-207`); risk-tier confirmation (`agent_tools.py:620-655`); destructive always confirms even for meta-agents (`EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=True`, `agent_tools.py:687-715`) | FIXED (was E5) |
| Final-commit failure honesty | `_final_commit` retried 3× + background reconciliation + additive `answer_persisted` flag on done (`agents.py:20891-20909, 21181-21194`) — done still emitted, but with honest attestation | FIXED (was A6; semantics changed from lie to attestation) |
| Reliability metrics | `reliability_metrics.py` | **DEAD — zero production callers** |

### Confirmed defects (C)

- **C-1 (P2):** `evidence_validation.py` never runs in production (see B section) — the verify gate treats any successful data call as grounded without validation.
- **C-2 (P2):** `reliability_metrics.py` unwired — no router/scheduler produces the report.
- **C-3 (P2):** `evaluate_observation` advisory-only for main-loop tool outcomes.
- **C-4 (P2):** Activity-step closure gaps beyond approval-pause are handled via the generator `finally` ("interrupted" live events, :21241-21253) — acceptable, but approval-pause remains open (A-6).

## 4. Subsystem D — Enterprise reporting

| Capability | Implementation | Status |
|---|---|---|
| HTML report core | `synexia/finalize.py:221-247` `finalize_into_artifact` → Artifact + ArtifactVersion + Blob | IMPLEMENTED |
| PPTX/DOCX/XLSX/PDF/HTML export | `sandbox_tool.py` (`_IMAGE_BY_FORMAT` :92-97, aliases :266-315, rich-HTML sibling enrichment :647-698); `document_generator.py` (automation, PPTX/DOCX/HTML/PDF/JSON/CSV/MD); legacy `functions.py:616,638`; skills `document-skills/*` | IMPLEMENTED |
| Deterministic business report | `db/business_reports.py:633` ← `agents.py:1563-1568` | IMPLEMENTED |
| Report snapshots | none (`ReportSnapshot` not found; every finalize stamps ArtifactVersion v1 — regeneration mints no new version) | GAP |
| Date ranges | `task_spec_parser.py:238-239, 347-350` (follow-ups rewritten with explicit concrete dates — strong); `schedule_parser.py:208-218` (weekly Monday, monthly 1st, DOM clamped 28) | PARTIAL — no central chat-report date normalization module |
| Lineage/provenance | `turn_evidence.py:71-109` + `verifier.py:370-390` source_citation check + frontend evidence chips | IMPLEMENTED in chat/tree; **not embedded in exported DOCX/PPTX files** |
| Dead code | `report_ai_service.py` + `schemas/reports.py` — zero imports, no router | DEAD |

## 5. Subsystem E — Chat & frontend experience

| Capability | Implementation | Status |
|---|---|---|
| Streaming watchdog | `api/agentEnhanced.js:136-165` — 90s no-data watchdog, aborts + error frame | FIXED (was C1) |
| D4 authoritative identity | `answer_persisted`/`assistant_message_id` on done; `backendOwnsContent` (`answerLifecycle.js:169-179`, Chat.jsx :2655, :2860-2872) | IMPLEMENTED (this milestone's D4) |
| D5 reset/retry safety | `content_reset` → `pendingReset` → `applyDelta` (`answerLifecycle.js:213-219`; Chat.jsx :2517-2528); empty `content_replace` rejected (invariant I1, :2553-2577) | IMPLEMENTED — **desktop only** |
| `paused` terminal event | Chat.jsx :2601-2630 (bubble from authoritative content, unblock input) | FIXED (was C2) — desktop only |
| Per-turn execution tree | `AgentExecutionTree.jsx` — `started_after` anchor (MessageBubble.jsx:565-569), 12-state map incl. partial/failed/blocked/cancelled/timeout, evidence chips + counts, specialist identity badges, stale-tree synthesis (unmatched → cancelled) | IMPLEMENTED (D1 fixed) |
| Cancel | `POST …/cancel` + AbortController (Chat.jsx:3005-3017; backend :11674-11677, cooperative cancel registered :14679-14682) | IMPLEMENTED |
| Reconnect | `PersistentStreamContext.jsx` keeps stream alive across SPA navigation only — no network resume; reload loses stream (mitigated by persisted row + `answer_persisted`) | PARTIAL (documented limitation) |
| Conversation history restoration | server rows authoritative; regenerate is server-owned (no new client row, `regenerate: true` Chat.jsx:2899-2954) | IMPLEMENTED |

### Confirmed defects (E)

- **E-1 (P1):** `MobileChatPage.jsx` — `content_replace` assigned **unconditionally** (no empty-rejection, violates invariant I1; :194-201); naive delta append; no `content_reset`, no `paused`, no `answer_persisted`/`backendOwnsContent`. The whole D5/D4 safety layer is missing on mobile.
- **E-2 (P2):** `SkillAgent.jsx` (:287-330) and `useAgentBuilder.js` (:275-277) — naive append, no reconciliation; `WorkflowBuilderChat.jsx` still uses "longer wins" done-merge (:564-566).
- **E-3 (P1):** `Chat.jsx:2114` — a legacy path persists **error text directly as an assistant row**, contradicting the rule at :2781-2785 that error text must not be persisted (empty/incorrect authoritative answer class).
- **E-4 (P2):** Two evidence vocabularies: chat tree `validation_status` (validated/contradicted) vs ontology canonical 5 statuses (`evidenceStatuses.js:14-28`) — not reconciled.

## 6. Security & tenant-isolation

| Finding | Baseline | Current | Evidence |
|---|---|---|---|
| KB credentials plaintext | P0 live-confirmed | **FIXED** — encrypted at rest, fail-closed on write, decrypt on read; plaintext never persisted | `models/knowledge_base.py:34-58` |
| Entity API hardcoded `default-org` | P0 | **FIXED** — JWT-derived org stamping, server-side | `entity_service.py:20-24, 468`, `routers/entities.py:247-251` |
| RLS + NULL-bypass | P1 | Unchanged by design; tenant context server-derived from JWT (`database.py:52-80`); coverage 127 policies / 144 tables | live-verified 2026-10-25 |
| Skills global cross-tenant registry | P0 (D1) | **PRESENT (risk)** — no org scoping found in `skills_loader`; mitigated: `skill_manager` removed from default user toolsets; single-tenant deployment posture. Org-namespaced skills remain the structural fix | grep: no scope/namespace hits |
| Meta-agent approval bypass | P1 (E5) | **FIXED** for destructive risk (`EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=True`) | `agent_tools.py:687-715` |
| project_id rebind w/o app-layer ownership check (C3) | P1 | **MITIGATED by RLS** (project query runs in org-scoped transaction; cross-org id cannot resolve), app-layer check still absent — defense-in-depth gap, single-tenant today | `agents.py:6916-6938, 12690-12719` |
| `.env.production` committed, Postgres published | P2 | Unchanged (deployment hygiene, out of code scope) | — |

## 7. Missing integration contracts (drives Phase 3)

1. **Tool result → validation:** `evidence_validation` validators never run on live turns (C-1/B section) — contract "tool results → evidence registry → provenance → reviewer" lacks the validation link.
2. **Delegation → effective specialization:** discovery advertises routing; AgentApp prompt/toolset/KB propagation is flag-off (A-2).
3. **Approval pause → UI terminal state:** backend emits `paused` but leaves the tool step "running" (A-6) — the frontend cannot close what the backend never closes.
4. **Data-agent result → truncation honesty:** 200-row slice unflagged (B-3).
5. **Reliability metrics → observability surface:** `reliability_metrics` has no consumer (C-2).
6. **Main loop → decision table:** `evaluate_observation` not fed by main-loop tool outcomes (C-3).
7. **Mobile chat → answer lifecycle contract:** E-1.
8. **Report regeneration → version chain:** finalize always v1 (D section).

## 8. Prioritized remediation plan (feeds Phases 3/5/6)

**P0 — none open.** The P0 integrity class (duplicate persistence D4/D5, plaintext creds, tenant scoping, fake-success fallbacks, done-after-failed-commit) is fixed and verified in this tree; D1–D5 invariants are pinned by tests and must not regress.

**P1 (in scope this milestone, deterministic fixes):**
1. B-3 — flag the 200-row slice in `ask_data_agent`'s `data_quality` payload (root fix, tiny).
2. A-6 — close the approval-paused activity step (mark `awaiting_approval` on the step before pausing).
3. E-1 — MobileChatPage D5/I1/answer_persisted parity via existing `answerLifecycle` helpers (no new mechanisms).
4. E-3 — stop persisting error text as an assistant row on the Chat.jsx:2114 path.
5. B-1/B-2 — single-KB auto-resolve + preflight probe for the ask_data_agent legacy path (flag-conservative; reuse existing probe module).
6. C-1 — wire `evidence_validation.validate_query_result` into the evidence-recording path (flag-gated, fail-open→honest-limitations).
7. A-2 — flip `DELEGATE_AGENTAPP_AWARE`/`DELEGATE_INHERIT_BINDINGS` defaults to True (code already tested; this is the enabling change for scenarios A–E).

**P2 (fix only if cheap; otherwise document):** C-2 reliability metrics consumer, E-2 remaining SSE consumers, A-4 prepass floor, B-4 success-flag semantics, A-5 IterationBudget docstring, E-4 vocabulary reconciliation, report version chain, C3 defense-in-depth org check.

**P3 / DEFERRED:** skills org-namespacing (structural, deploy-risk), SSE network reconnection, evidence-in-exports, dead-code removal (`report_ai_service`, `ensureAgentApp`, `datasources/drift.py`), report snapshots.

**Ownership boundary (constraint §1):** 211 dirty working-tree files are foreign multi-session WIP and are preserved untouched except: `backend/app/routers/agents.py`, `frontend/src/pages/Chat.jsx`, `frontend/src/lib/answerLifecycle.*` (already this session's D4 files), `backend/app/config.py` (additive flag-default change only, documented), plus NEW files (docs, tests, acceptance suite). Any further shared-file edit will be additive and recorded in the implementation doc.

---

## 9. Architecture correction (2026-11-01): Projects, not Departments

**Decision:** Zhanlu's business-context and agent-organization boundary is the
**Project**. No Department layer is introduced — no department entities, registries,
routing, hierarchies, execution permissions, or management UI. Organization provides
tenant isolation; Projects provide business context and resource boundaries; agents
provide reusable capabilities; the General Assistant coordinates them.

### 9.1 What the audit found (project infrastructure is real and load-bearing)

| Capability | Location | Status |
|---|---|---|
| Project model (org-walled via base `org_id`/`app_id`) | `app/models/project.py:15-57` | ✅ real |
| Project↔agent association (multi-membership join table + legacy home FK) | `app/models/project_agent.py:18-38`, `agent_app.py:20-22` | ✅ real (runtime uses home FK; join table has no management surface yet) |
| Project context assembly (name + description + `ProjectMemory`, project LLM config) | `data_source_runtime.py:1239-1314`, `agents.py:7120-7144` | ✅ real |
| KB↔project membership (home ∪ legacy ∪ linked, org/app-scoped) | `app/services/project_kb_membership.py:32-51` | ✅ real, "all read paths MUST use" |
| Project authorization (org wall / admin / creator / `ResourceShare 'use'`; 404 no-leak; fail-closed) | `app/services/project_access.py:41-110` | ✅ real |
| Conversation/session project identity | `agent_conversation.py:22-24`, `chat_session.py:14-16` | ✅ real |
| Cross-project leak guards (project-reference authorization; strict project-memory scoping) | `project_access.py:1-8`, `project_memories.py:7-13` | ✅ present |
| Project stamping on messages/executions/artifacts | — | ❌ only via session/conversation indirection (documented limitation; no schema change without proof) |

**Department remnants — classified, NOT removed (per "prove unused before deprecating"):**
- Load-bearing, discovery-metadata only: `Project.department` label, delegate-tool
  discovery filter + prompt block (`delegate_tool.py:1369-1502`,
  `agents.py:3209-3234 _append_department_block`), semantic-match fallback filter
  (`semantic_capability_match.py:77,100-141`), usage analytics `by_department`.
  Pinned by `tests/test_p1_orchestration.py`, `tests/test_semantic_capability_match.py`.
  **Never used for data authorization.** Frozen as legacy metadata; nothing new may
  depend on it.
- Cosmetic: docstring/comment mentions only (`agent_data_binding.py`,
  `resource_access_policy.py`, `enterprise_process.py OrgUnit` is a discovered
  business unit, unrelated; `scripts/create_manufacturing_agents.py` uses none).

### 9.2 Defects found and fixed under the Project architecture

- **P0 — discovery directory spanned organizations** (fixed this milestone):
  `_list_available_agents` queried ALL published AgentApps with no org filter
  (`delegate_tool.py:1396-1400`). Fixed: published query scoped to the caller's org
  (user → org, else conversation → org); legacy no-context callers keep the
  historical unscoped behavior with a loud warning (real tool calls always carry a
  user). Regression tests: `tests/test_enterprise_reliability_p1.py::TestDiscoveryOrgWall`.
- **Residual, documented (not fixed — needs owner decision):**
  - `KEY_AUTO_BIND_ALL_DATASOURCES` workspace setting binds every connected KB in
    the ORG to an agent (`data_source_runtime.py:1026-1056`) — org-scoped but not
    project-scoped; a flag-gated escape hatch that widens project boundaries.
  - `ChatMessage`/`Execution`/`Artifact` are not project-stamped (identity derives
    via session/conversation); `Execution`/`Artifact` traceability gap is P2.
  - `ProjectAgent` join table has no management surface (only `AgentApp.project_id`
    is used at runtime).

### 9.3 Acceptance coverage added (deterministic, real enforcement)

`tests/test_enterprise_acceptance.py` now proves with sqlite fixtures against the
REAL services: cross-org denial (even for admins), `use`-share authorizes vs
`view`-only denied, fail-closed authorization, KB membership isolation (project A
sees home KBs + explicitly linked KBs only; foreign-org link rows never widen
membership), shared specialists across projects (`ProjectAgent` multi-membership),
conversation/session project identity. Scenarios A–E are reframed as the five
automobile PROJECTS; capability selection is unchanged (relevance-ranked) with
project availability asserted.
