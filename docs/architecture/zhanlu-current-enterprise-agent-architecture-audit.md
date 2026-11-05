# Zhanlu Current Enterprise Agent Architecture — Audit Report

**Date:** 2026-10-31 · **Scope:** read-only investigation of the working tree + live DB (`zhanlu-backend` container serving localhost:8089)
**Method:** 3 parallel code-explorer passes (platform / data / multi-agent), direct live-DB verification, cross-checked against prior audits (`docs/audits/2026-10-20-zhanlu-vs-deepseek-harness-full-system-audit.md`, `docs/audits/2026-10-30-ontology-gap-audit.md`).
**Evidence convention:** every claim carries file:line. Where the live DB was probed, it says **[live-verified]**. Claims that could not be verified are marked **[assumption]**.

---

## 0. Executive summary

Zhanlu is a **real, working multi-tenant enterprise agent platform** with unusually deep data grounding (a ~200-test NL2SQL pipeline, a 6-table business ontology with fail-closed governance, org-level RLS). What it is *not* yet is a cross-department agent ecosystem: the 7 recently added manufacturing agents are **catalog entries + functional shells** — correct prompts and real tools, but **zero data bindings, `status="active"` (not `published`), and no working delegate-resolution to AgentApp identities**. Four specific breakpoints (§10) prevent the cross-department scenarios; all four are small, surgical fixes — no framework rebuild is needed.

**Counts [live-verified]:** 200 registered tools · 14 MarketAgent catalog rows · 7 manufacturing AgentApps (active) · 127 RLS policies on 144 public tables (12 exempt) · 201 NL2SQL tests · 6 ontology test suites.

---

## 1. Agent Marketplace

**What it does.** A catalog of agent *cards* (`MarketAgent`) that users clone into a live, editable `AgentApp`.

| Aspect | Evidence | Status |
|---|---|---|
| Catalog model | `backend/app/models/market_agent.py:9-17` — only `name, category, description, capabilities, rating, subscribers`. No prompts/tools/org scoping. | Fully implemented (display only) |
| Listing UI | `frontend/src/pages/Market.jsx:38` fetches via generic entity API; category→kind map `CATEGORY_TO_KIND` (:11-16): `industry→Industry`, `office→Role`, `ops_governance/data_processing→Function`. Filtering is client-side only. | Fully implemented |
| Clone-on-use | `frontend/src/pages/MarketAgentDetail.jsx:36-92` `clone()`: find-or-create `AgentApp` by name, generate 5-layer prompts, restore CAD `tool_config`. | Fully implemented |
| Backend router | `backend/app/routers/marketplace.py` is a **skill** marketplace (`/marketplace/skills/...`), NOT agent marketplace. `MarketAgent` is served only via generic CRUD and is a global (RLS-exempt) catalog. | Gap |
| `launchMarketAgent.js ensureAgentApp` (:83-136) | No importer anywhere; live flow is MarketAgentDetail's inline clone. | **Dead code** |
| Subscription flow | `subscribers` counter is never incremented by any code path. | Decorative |

## 2. Agent Creation & Configuration (My Space / Agent Builder)

`AgentApp` (`backend/app/models/agent_app.py:14-151`, table `agent_apps`) is the central entity. Full field inventory with **runtime-consumed vs stored-only** verdict [verified by global grep]:

| Field(s) | Lines | Runtime verdict |
|---|---|---|
| `name, description, project, project_id` | :17-22 | Consumed (scoping, prompts) |
| 5 prompt layers `prompt_identity…prompt_output` | :30-34 | **Consumed** (§3) |
| `tool_config` `{enabled_tools, disabled_tools}` | :89 | **Consumed** — the enforced toolset (`composable_toolsets.py:151-177`: posture → enabled_tools → skills → fallback) |
| `knowledge_bases` (JSON list of KB ids) | :38 | **Consumed** (`data_source_runtime.py:129-137,1360-1385`) |
| `skills` (JSON name list) | :37 | Consumed at creation (translated to `tool_config`, `agent_tools.py:1165-1170,1296-1301`) + prompt block |
| `max_call_count` | :50 | **Consumed** — per-conversation `IterationBudget` (`agents.py:7444-7445`) |
| `model, temperature, top_p, max_tokens` | :26,64-66 | Consumed |
| `status, version, rollout_percent` | :69-78 | `published` gates `list_available_agents` only; `rollout_percent` stored-only |
| `flow_mode, flow` | :45-47 | **Consumed** — workflow canvas runtime (`workflow_runtime.py:873`, `WorkflowRun`/`WorkflowRunStep`) |
| `sub_agents`, `topology`, `agent_type` | :27,41-42 | **Stored-only.** Team-tree UI (`AgentConfig.jsx:54-93,309`) edits `sub_agents`; nothing in the chat runtime reads it. `topology` read only by disabled-FSM `context_assembler.py:468`. `agent_type` never branched on. |
| `max_retries`, `max_iterations` | :51-52 | Stored-only (router uses global `MAX_TOOL_ITERATIONS=40`) |
| `trace_enabled`, `log_level` | :60-61 | **Decorative** — reasoning trace is documented always-on (`agents.py:5706-5709`) |
| `data_read`, `data_write`, `human_fallback` | :55-57 | Advisory — only seed `policy_profile` defaults (`agent_tools.py:1074-1076,1119-1129`); no runtime gate reads `data_write` |
| `policy_profile` | :144 | **Enforced** (§5) |
| `data_bindings` | :135 | Stored-only metadata (mirrors `knowledge_bases` at creation; shown in preflight `agent_studio/preflight.py:72-76`) |
| `manifest_json, skill_bindings, memory_scope, output_contract, evaluation_profile` | :132-150 | Partial: `memory_scope` routes memory; manifest boundaries are descriptive (`system_agents.py:566-568` admits the enforced thing is `tool_config`) |

**APIs:** generic entity CRUD `backend/app/routers/entities.py` (`register_entity_router` :301; `AgentApp` in `USER_SCOPED_ENTITIES` :198-214; tenant-stamped `_tenant_ids()` :244; shareable write-guard :266). Conversational creation via the `agent_builder` meta-agent's `create_agent`/`update_agent` tools (`agent_tools.py:1207-1301`), with `_autofill_harness_profile` (:1061-1129) deriving manifest/policy/output contracts.

**Frontend:** `MySpace.jsx` (tabs project/agent/kb/dashboard/workflow/mcp, embedded `StepsAgentBuilder`), `AgentConfig.jsx` (sections Role/Prompt/Skills/DataSources/Capabilities/FusionBridge/Harness; team tree in simple mode, FlowTree in flow mode), plus a second builder `AgentBuilder.jsx`.

## 3. Five-Layer Prompt Engineering

**Fully implemented and consumed at runtime.** `backend/app/services/agent_prompts.py`:
- `assemble_user_agent_prompt()` (~:2178-2209) concatenates the five layers as `## IDENTITY / BOUNDARY / REASONING PROCESS / TOOLS & SKILLS / OUTPUT FORMAT` sections.
- `get_system_prompt` gate (:2441-2454): if any layer non-empty → assembled prompt + skills block; else generic fallback.
- Authoring discipline: agent_builder instructions demand "FIVE-LAYER CONSTITUTIONAL PROMPT / never leave a layer empty" (:1126-1134); create_agent JSON schema marks all five required (:2770-2785).
- Applies to both the v2 loop and delegate sub-agents (`delegate_tool.py:321` — but see §10, breakpoint ②).

## 4. Agent Teams & Orchestration

| Mechanism | Evidence | Status |
|---|---|---|
| Team tree (`sub_agents`) | UI editing only (`AgentConfig.jsx`); accepted by create/update whitelists; **zero runtime readers** | **UI-only** |
| `topology`, `agent_type` | No runtime branch | UI-only |
| Workflow canvas (`flow_mode`/`flow`) | `routers/workflows.py:52-110` CRUD; `workflow_runtime.py` validate+execute; `WorkflowRun`/`WorkflowRunStep`; scheduler `workflow_scheduler.py:3-46`; chat branches at `agents.py:7030-7032` (v2) / :12160+ (v3) | **Fully implemented** |
| Plan DAG | `synexia/capability_router.py` (70 KB): node types tool/nl2sql/synthesize/sandbox/agent (:6-8); parallel ready-batch prepass `PLAN_DAG_PARALLEL_ENABLED=True` (config.py:1727, 4 concurrent, independent sessions); adaptive replan with `SYNEXIA_ACT_REPLAN_MAX=2`; recoverable-failure → corrective sub-plan (:237-290) | Fully implemented |
| Automation executor | `automation_executor.py` — scheduled agent runs, unattended approval semantics (`_AutomationPaused` :83-90), memory ledger `_previous_run_context` (:848-874) | Fully implemented |
| FSM (Synexia) | `SYNEXIA_FSM_ENABLED` — **config default True (config.py:793) contradicts the module docstring / agents.py comment claiming default False** (`fsm.py:10`, `agents.py:192`) | Flag conflict (doc bug) |

Note: two divergent "flow" representations exist — AgentConfig's step-array (`agentFlow.js`) vs workflow `{version,nodes,edges}`; `AgentConfig.jsx:126-136` redirects workflow rows to the canvas editor.

## 5. Capabilities & Permissions

**Enforced (real):**
- `PermissionChecker.check_tool()` (`services/permissions/__init__.py:161-293`): tool allow/deny, plan-mode write blocking (`WRITE_TOOLS` :89-105 incl. `delegate_task`), dangerous-command blacklist, sensitive-path globs, risk registry with destructive → `requires_confirmation` (:268-276). **Fail-closed** on lookup failure (:199-207).
- `policy_profile.risk_tier` enforced in the approval loop (`agent_tools.py:536-577`): high/critical tiers force confirmation; system meta-agents bypass (:616-621). Approval requests persist via governance API (`routers/governance.py:39-113`); chat loop pauses (`paused` SSE) and resumes via `/resume` (`agents.py:8506-8553, 8925-8931`).

**Advisory/decorative:** `data_write`/`human_fallback` (derive defaults only), `manifest_json` boundaries, `trace_enabled`.

## 6. Data Source Configuration on Agents

- `AgentApp.knowledge_bases` → `prepare_data_source_runtime()` (`data_source_runtime.py:129-137, 1360-1385`): merges bound KBs ∪ project KBs; if any database KB is bound, **injects `ask_data_agent` idempotently** + a "Bound Data Sources" prompt block.
- `data_bindings` JSON: stored-only (no runtime reader).
- Per-user data matrix: `routers/access_policies.py` `ResourceAccessPolicy` (table allow/deny/**allow_columns**, column allowlist, **row filters**), resolved with specificity kb+table > kb > table > global (`access_policy_service.py:6-16,31-54`); enforced at execution-gate time.
- Workspace `auto_bind_all_datasources`: default **OFF** (`workspace_settings_service.py:34`).

## 7. Agent Execution Runtime

- Endpoints: v2 `POST .../v2/{c}/messages` (`add_message`, `agents.py:6859`); v3 SSE `.../v3/{c}/messages/stream` (`add_message_stream`, :11863); shared core `_stream_llm_with_tools` (:11015); resume loop :9206+.
- Budgets: `MAX_TOOL_ITERATIONS = 40` (`harness/policy.py:40`); per-conversation budget from `agent.max_call_count` (`agents.py:7444-7445`); `ToolLoopGuardController` loop detection; wrap-up nudge at `MAX-3` (:7536-7541).
- Tool batch: `agent_loop/tool_executor.py` `execute_tool_batch()` (:96-155) — guard fail-closed, parallel `asyncio.gather` with exception isolation, serial execution for `fusion360_*` (documented index-scramble incident, `_SERIAL_TOOL_PREFIXES` :86-93). Each parallel call gets an **isolated DB session** via `database.tool_session()` (P1 fix 2026-10-22; `tests/test_tool_session_isolation.py`).
- Wall-clock caps: `DELEGATE_WALL_SECONDS=600`, `DATA_AGENT_BUDGET_SECONDS=60` / `DATA_AGENT_MAX_WALL_SECONDS=600` (`delegation_tools.py:66-87`) — per-statement SQL timeouts are separately disabled (`QUERY_TIMEOUT_DISABLED=True`, config.py:539, disclosed as `timeout_policy:"unbounded"`).
- Streaming: v3 SSE with heartbeats; frontend 90s no-data watchdog (`agentEnhanced.js`, P0 fix).

## 8. Agent-to-Agent Communication

| Mechanism | Flag | Status |
|---|---|---|
| `delegate_task` (`delegate_tool.py`, registered :626-633 `enabled_by_default=True`) | on | **Real.** Batch fan-out bounded `DELEGATE_MAX_CONCURRENT=4`; child loop = all default-enabled tools minus `_RESTRICTED_TOOLS={delegate_task, memory, send_message}` (:35); 600 s wall clock with partial results `truncated=True` (no fake failure); parent-context digest ≤1200 chars (`DELEGATE_CONTEXT_INHERITANCE=True`); records `AgentInvocation` rows. |
| `ask_data_agent` (`delegation_tools.py:350`) | on | **Real.** Sub-loop whitelist `_DATA_AGENT_TOOLS` (:90-98: list_data_sources, describe_schema, execute_query, answer_from_database, search_documents, answer_from_documents); bound-KB enforcement (:361, :406-413); planner with `needs_clarification` (flag on); honesty checks (:107-219). Swarm path `DATA_AGENT_SWARM_ENABLED=False`. |
| `list_available_agents` (:641-684) | on | Real — builtin definitions + **published** AgentApps (⚠ our agents are `active`, not `published` → invisible). |
| A2A outbound (`a2a_tool.py:72-262`) | on | Real, requires registered `ExternalAgent`s. |
| Swarm suite (`services/swarm/`, 9 tools) | `AGENT_HARNESS_ENABLED=False` (config.py:1984) | Implemented but **off**; scratchpad is in-process only; spawn resolves **builtin definitions only** — cannot spawn AgentApps. |
| MCP | — | Complete (client + oauth + manager tools). |

## 9. Observability & Tracing

- **Production:** `AgentInvocation` (`models/agent_invocation.py`) per turn AND per delegation (duration, tokens, cost, confidence, model, tool_call_count, route/outcome, trace_id/span_id :60-67); frontend `AdminObservability.jsx` (stat cards, filters, detail drawer with sub-agent children :221-260). Router `admin_invocations.py`. **Real.**
- **Built but disabled:** harness `AgentRun`/`AgentRunStep` trail + replay/resume routes (`agent_runs.py` — docstring notes gate), `AGENT_TRACING_ENABLED=False` (OTEL spans; `tracing/otel.py` exists, provider `noop`), `AGENT_CHECKPOINTING_ENABLED=False`.
- **Gap:** each delegation writes an invocation row but with `conversation_id=None` (`delegate_tool.py:289`) — **no cross-agent trace tree** today.

## 10. Multi-Agent Collaboration — the four breakpoints

1. **Publish gate:** delegation directory lists only `status=="published"` AgentApps (`delegate_tool.py:662-663`); the 7 manufacturing agents are `active` → **invisible to delegation**. [live-verified]
2. **AgentApp-unaware delegation:** `delegate_task` resolves children via `get_system_prompt(agent_name)` **without** the AgentApp object — a published AgentApp's name falls through to the **generic fallback prompt**; the child also gets the default toolset, **not** the target agent's `tool_config`. Prompt/capability catalog are disconnected.
3. **No binding inheritance:** the child loop calls `execute_tool(...)` without `context` (`delegate_tool.py:467-482`) → child's `ask_data_agent` cannot see the parent's `bound_kb_ids`.
4. **No cross-agent trace tree:** invocation rows aren't linked to the parent conversation; harness run-tree/OTEL are off.

Failure recovery that already works: batch `gather(return_exceptions=True)` → per-task `success:false` without killing siblings; wall-clock → partial results with `truncated=True`; DAG-level corrective replans; data layer's no-fake-success guarantees.

## 11. Project, Organization & Tenancy

- `Organization` (slug, plan) / `Project` (is_system, resource_type, llm_model_id); every table carries `org_id` (`TimestampedBase` docstring).
- RLS: `database.py:52-80` executes `SET LOCAL app.current_org` at **every** transaction begin from verified JWT identity (`tenant_context.py` — client header is never trusted, fixed 2026-10-22). Postgres FORCE RLS + `tenant_isolation` policy with deliberate NULL-bypass for background jobs; batches 1-6 in `scripts/apply_rls*.sql`. [live-verified 2026-10-25: app connects as NOSUPERUSER `zhanlu_app`; `cad_revisions` was the last genuine miss, now enrolled + pinned by a live guard test `tests/test_tenant_rls.py`]
- Entity API tenant scoping fixed server-side (`_stamp_tenant`, client org_id stripped — `entities.py` audit fix 2026-10-22; `tests/test_entity_tenant_scoping.py`).
- RLS is Postgres-only; SQLite dev relies on app-layer filters. RLS-exempt tables (12) are documented shared catalogs/ACL tables.

## 12. Skills System

**Fully implemented end-to-end.** `skills` (names) → translated to `tool_config` at creation; loader `skills_loader/__init__.py` (`SkillsRegistry.load()` :510-526: bundled dir + `~/.zhanlu/skills`; manifest.yaml or SKILL.md); injected as a prompt block; `progressive_disclosure` → `load_skill_body` on demand; marketplace install writes Tool rows (`marketplace.py:323-373`); execution recorder feeds `SkillExecutions.jsx`.
**Known gap:** the skill registry is global (cross-tenant) — per-org namespacing NOT done (only `skill_manager` removed from default user toolsets, audit D1).

## 13. Enterprise Data Architecture

### 13.1 Connections
`KnowledgeBase` is the datasource record (`source_kind='database'`, db_type/host/port/database/username, **fail-closed encrypted password** — property refuses unencrypted writes, `knowledge_base.py:40-58`). A second `Datasource` model exists (`datasource.py`, engines sqlite/postgres/mysql/mssql/bigquery/snowflake) with its own adapter sub-layer — two parallel datasource catalogs is debt (§14). Connection factory `db/connector_factory.py:68-77`: **sqlite, mysql, mariadb, postgres, mssql, oracle** (5 connector classes, lazy driver install). Lifecycle on KB: `connected→discovering→indexed→enriched→validated→nl2sql_ready` (:88-101). *(No `nl2sql_datasources` table exists in this tree.)*

### 13.2 Schema discovery (catalog)
`knowledge_graph/catalog_indexer.py` (~1900 lines): introspects tables/columns via connector, batched-LLM zh/en descriptions, persists `KBTableMeta` (row_count + source, verbatim `source_comment`, grain, authority_override, schema_class, coverage_json), `KBColumnMeta` (PK/FK key_role, measured `stats_json` — "absent key = not measured, never 0", semantic_type, enum_values, unit), `KBTableRelation` (FK + confidence). Embeds into ChromaDB `catalog_{kb_id}`. Caps: MAX_TABLES=300 with honest truncation stats, 8 introspection threads. **No PII classification field.**

### 13.3 Business ontology
`models/ontology.py`: `OntologyDomain/Object/Action`, `OntologyLink`, `AgentDomainBinding` (permission_scope read|write), `CoverageState`, `OntologyRevision`, `OntologySensor` — all org-scoped + RLS. Modules under `services/ontology/`: discovery (3-layer matching, auto-bind ≥0.8), KB-backed reads (`data.py read_object_data :24` — `kb:<id>:<table>` routed through read-only QueryService after org-ownership check), risk-tiered actions with the two chokepoints `execute_action`/`execute_approved_action`, **P0 fail-closed permissions** (`permissions.py:77-133`, deny by default, denial audit failure-safe), **P1 evidence taxonomy** (`verified/computed/inferred/proposed/unavailable`; only human bind grants `verified`), **P2 append-only revisions** (row-locked revision allocation, rollback = new revision, history never erased), escalation, sensing (60 s loop, state-change transitions, fan-out), template export/import, domain value, coverage. All live-verified 2026-10-30 (99 tests green).
**Open ontology gaps** (2026-10-30 audit): G1 cross-source record-level identity resolution ABSENT; G2 document→ontology proposals ABSENT; G5 `metric` backing kind is a stub; G6 evolution triggers thin (`datasources/drift.py` is dead code); G8 no graph UI.

### 13.4 Semantic mappings & naming conventions
Three authoring layers: `MetricDefinition` (base_table/column/aggregation + synonyms), `SemanticMapping` ((table,column)→display_name+synonyms+join hints), `Terminology` glossary (hierarchical). `db/semantic_store.py` is a read-only facade over catalog + samples + workload + authored knowledge with 4 provenance tiers (VERIFIED/DISCOVERED/LEARNED_FROM_WORKLOAD/RUNTIME_INFERRED — runtime inference never promoted). Match normalization handles `_`/case/dialect variants. **Different naming conventions for the same metric** are handled by synonyms + the metric-binding contract (`query_contract.py` RC-1: deterministic measure-alias constraints, per-(org, datasource) scoped so datasource A can't influence B); unresolved metrics surface as `unsupported_slot` and are **never silently coerced**.

### 13.5 NL2SQL pipeline
`db/nl_answer_service.py` (169 KB): plan (`data_agent_planner.py`, intent/decomposition/clarify, fail-soft) → table retrieval (`table_retrieval.py` + stale-coverage rerouting) → grounding (`grounding.py` 82 KB: unsupported slot is NEVER converted to invented SQL) → **P-SV source-verification gate** (blocking, between retrieval and SQL generation) → LLM SQL → **`query_contract.py` execution gate, unconditional**: source_authorization, read_only, sqlglot dialect parse, table/column existence, join reachability + fan-out, policy, row_filter, scope, metric contract + binding → execute via `QueryService` (row cap 1000 default, truncation proof via extra-row fetch) → self-heal loop (syntax_heal ER-1064, collation_heal ER-1267, unknown-column repair keyed to gate error phrasing) → narrate with figure-grounding guardrail (`answer_grounding.py`: every number must exist in tool results, 0.5% tolerance, 万/亿 normalization, one prescriptive retry) → per-turn SQL budget (`turn_sql_budget.py`) → **unconditional `nl2sql_query_logs` write** (fixed 2026-10-22; workload mining no longer starved). Result contract: `CanonicalResultSet` with declared columns; `answer_contract.py` deterministic post-checks (period_not_applied, grain_mismatch, result_truncated).

### 13.6 Cross-database
**Per-KB scope only** — `QueryService.execute(kb_id, …)` takes exactly one KB; no cross-source SQL joins; semantic store strictly `kb_ids`-filtered. Multi-source questions must go through the ontology layer (which can bind objects across KBs) or multiple agent turns. Record-level identity resolution (G1) absent.

### 13.7 RAG & documents
Extractors: txt/md/csv/excel/pdf/docx/pptx/html/json/images(OCR) — **xlsx/csv are serialised header+rows text chunks, not structured tables**. Chunker ~800 tokens CJK-aware; agentic chunking off. Embeddings bge-m3; **hybrid dense+RRF on**; LLM grading on; decomposition on; semantic answer cache. Vector store: ChromaDB (Milvus/pgvector present in code but not wired into the data path). Tools `search_documents`/`answer_from_documents`.

### 13.8 Freshness & sync
Catalog coverage refresh loop (6 h poll, stale-after 24 h) — **inert unless `KG_COVERAGE_PROBE_ENABLED=True` (default False)**. `datasources/drift.py` = dead code. Ontology sensors run but never trigger discovery re-runs; no stale-binding validation loop (G6).

### 13.9 Tests
`tests/nl2sql/` 201 tests across 20 files; ontology 6 suites (governance/evidence/revisions/sensing/core/...); `test_multiagent_mas.py` (10), `test_swarm_enablement.py` (9), `test_tool_timeouts_p1.py` (4), `test_tool_session_isolation.py` (3), `test_tenant_rls.py` (live-only guard). No evaluation harness for agent answer quality beyond quality-eval remnants.

## 14. Architecture & Technical Debt Register

**Duplicated/overlapping:**
- Two datasource catalogs (`KnowledgeBase` vs `Datasource` + adapter layer) with different engines lists.
- Two flow representations (step-array vs workflow graph); two "flow" concepts in AgentConfig.
- Duplicated orchestration: ReAct loop + Synexia FSM (flag-conflicted) + capability DAG + swarm harness — three ways to run an agent turn.
- Overlapping data tools: `query_database` / `answer_from_database` / `ask_data_agent` / `universal_query|kpi|trend|forecast` (the universal family overlaps the data-agent path).
- `ensureAgentApp` dead code; `datasources/drift.py` dead code.

**Decorative config (misleading to builders):** `sub_agents`, `topology`, `agent_type`, `max_retries`, `max_iterations`, `trace_enabled`, `log_level`, `rollout_percent`, `data_bindings`, `data_write`/`human_fallback` (directly), MarketAgent `subscribers`.

**Config conflicts:** `SYNEXIA_FSM_ENABLED` docstring says default False, config ships True. `KG_COVERAGE_REFRESH_ENABLED=True` but requires `KG_COVERAGE_PROBE_ENABLED=False` → freshness loop inert by default. `QUERY_TIMEOUT_DISABLED=True` removes per-statement timeouts (mitigated by 600 s loop walls, not for raw `execute_query`).

**Hardcoded/platform-specific:** hardcoded `/root/zhanlu` paths in some tests (fixed in fsm subset); crypto.py still logs-and-stores plaintext on crypto error (:62-64, KB property guards only KB); generic report-card synthesizer bypasses the Final Answer Delivery Contract (open item from 2026-10-22).

**Permission boundaries:** skill registry cross-tenant; `ontology_tool._org_and_agent` masquerade default removed (P0), but `list_available_agents`→delegate mismatch means an agent can be delegated to with the wrong prompt (privilege-shape mismatch, not escalation); RLS NULL-bypass is by design but means background jobs are unscoped.

**Missing shared capabilities:** cross-source joins/identity (G1); document→ontology (G2); metric backing (G5); no PII classification in catalog; no agent evaluation harness; no org-scoped skill namespaces.

## 15. Verification of the Manufacturing-Agents Implementation Report

Claims from the latest implementation report, checked against code + live DB:

| Claim | Verdict | Evidence |
|---|---|---|
| `scripts/create_manufacturing_agents.py` exists | ✅ | Workspace `scripts/` (bind-mounted to `/app/scripts` in container) |
| AgentApp + MarketAgent records for 7 agents | ✅ | 7 AgentApps + 7 of the 14 MarketAgent rows [live-verified] |
| Five-layer prompt configurations | ✅ | All 7 have non-empty `prompt_identity…prompt_output` (identity 234–348 chars + boundary with grounding/human-gate rules) |
| `enabled_tools`, `max_call_count=80`, `max_iterations=30` | ✅ | Seed script values; `max_call_count` is the one that actually drives runtime budget |
| Tool counts 20–27 per agent | ✅ | Range 20 (Inventory) – 27 (Demand Forecasting) [live-verified] |
| Industry Agents category | ✅ | All 7 `category="industry"` (→ "Industry Agents" filter) |
| "DB verification of seven active agents" | ✅ with caveat | All 7 exist, `status="active"` — but **active ≠ published**: they are invisible to the delegation directory, and **all have `knowledge_bases=None`** — no agent can query a single table yet |

### Per-agent dossier [live-verified 2026-10-31]

| Agent | ID | Tools | delegate_task | ask_data_agent | forecast_* | po/quote draft | escalate | KBs | Skills |
|---|---|---|---|---|---|---|---|---|---|
| Predictive Maintenance | 5cfa1572-…85da1d | 21 | ✗ | ✓ | ✗ | ✗ | ✓ | None | None |
| Quality Control | f0bae0c7-…fa2552 | 22 | ✗ | ✓ | ✗ | ✗ | ✓ | None | None |
| Supply Chain Optimization | 12625119-…8b1dd | 24 | ✗ | ✓ | ✗ | ✓ (po+quote) | ✓ | None | None |
| Production Scheduling | 4ab9b9d2-…25a2b | 21 | **✓** | ✓ | ✗ | ✗ | ✓ | None | None |
| Inventory Management | 6e223397-…3a5d | 20 | ✗ | ✓ | ✗ | ✓ (po) | ✓ | None | None |
| Autonomous Vehicle Systems | 6935f6c5-…541a | 24 | ✗ | ✓ | ✗ | ✗ | ✓ | None | None |
| Demand Forecasting | f0261dee-…528f4 | 27 | ✗ | ✓ | ✓ (10) | ✗ | ✓ | None | None |

For every agent: **all enabled tools are real registry entries (200 registered; zero unregistered names)** — no simulated tools. Specifically:
- **SQL execution:** yes, but **indirectly** — `ask_data_agent` (planner→NL2SQL→gates) and the direct `execute_query`/`answer_from_database` tools. With `knowledge_bases=None`, every data question today ends in the designed honest refusal ("no datasource bound").
- **Ontology access:** read-side only (`query_ontology`, `ontology_directory` in BASE_TOOLS). `execute_ontology_action` deliberately excluded — matches the human-gate design.
- **Delegation:** only Production Scheduling carries `delegate_task`/`list_available_agents`; and due to breakpoint ②, even that cannot currently run a *named* sibling with its real prompt.
- **Real results:** all handlers execute for real (registry handlers, not stubs): forecast tools run the ForecastRun pipeline; `create_po_draft`/`create_quote_draft` are approval-gated DB writes; `execute_code`/`sandbox_code` run real sandboxes; `vision` requires an actual image.
- **Missing:** datasource/ontology bindings per deployment; `published` status; org-appropriate `policy_profile` (all seeded `risk_tier=low` — Supply Chain/Inventory draft POs and should arguably be `medium`); per-agent eval tests; no agent produces artifacts by default configuration beyond tool availability.

---

## 16. Verified-vs-assumption ledger

**Verified:** everything carrying file:line above; agent dossier (live DB); tool-registry membership (200 names); RLS posture (live probes 2026-10-25); ontology P0/P1/P2 (live smokes 2026-10-30).
**Assumptions / not probed:** runtime behavior of every one of the 200 tools individually (membership verified, not each handler's happy path); frontend build parity with `dist/`; per-agent LLM answer quality (no eval harness — that absence is itself a finding).
