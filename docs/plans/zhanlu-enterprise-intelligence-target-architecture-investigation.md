# Zhanlu — Enterprise Intelligence Target Architecture: Phase 0 Investigation

**Date:** 2026-11-01 · **HEAD:** `010f282f6` (unchanged since the current-state audit `docs/plans/zhanlu-current-enterprise-architecture-audit.md` — no subsequent commits; that audit remains accurate and is reused throughout as baseline). **Mode:** READ-ONLY. Foreign WIP (~212 dirty entries) untouched.

**Convention:** ✅ VERIFIED (code evidence) · 🔶 RECOMMENDATION (design judgment, not yet implemented) · ❓ UNRESOLVED (needs product-owner decision or deeper trace).

---

## 1. Executive Summary

Zhanlu's current "project" concept is an **arbitrary resource container**, not a department or workspace: any number of projects can exist per org, resources attach via three inconsistent mechanisms (join tables, direct FKs, agent-only bindings), and a single workspace opt-in (`auto_bind_all_datasources`) can deliberately widen agent access to everything. Business context reaches the runtime at exactly **one choke point** — `prepare_data_source_runtime` → `bound_kb_ids` — plus a prose-only department hint. The ontology (semantic layer + EBPM) remains admin-side.

The architecture is **one integration point away** from business awareness: everything an agent will ever touch flows through `prepare_data_source_runtime` (`data_source_runtime.py:1366-1431`) and `get_system_prompt` (`agent_prompts.py:2393-2612`). A shared, evidence-annotated, bounded Business Context Service plugged into those two functions — plus a domain filter in delegation — would light up the ontology for every execution path (ReAct, FSM, workflow) without new engines. The main hazards are an unvalidated per-message `project_id` (same-org cross-owner KB inheritance), no cleanup on resource removal, and evidence/provenance fragmentation across three vocabularies.

---

## 2. Verified Current Architecture

Baseline = current-state audit (this HEAD). Load-bearing facts re-confirmed:

- Multi-tenant wall: `TimestampedBase.org_id/app_id` (`app/models/base.py:43-48`), Postgres RLS via `app.current_org` session var.
- Runtime: v3 stream `agents.py:12112` → ReAct loop `agents.py:14852` (primary), Synexia FSM (flag-ON default, `config.py:799`), workflow chat branch `agents.py:12401`.
- Ontology reaches agents only via voluntary tools (`ontology_tools.py`); NL2SQL (`app/services/db/`) has zero ontology references.
- EBPM increments 1–5 complete admin-side; increment 6 (observed execution) not started.

## 3. My Space and Project Architecture

### 3.1 Lifecycle facts ✅

| Aspect | Verified behavior | Evidence |
|---|---|---|
| Project create | Generic entity CRUD; `resource_type` (`company\|personal`) stamped server-side from creator role, immutable (`_IMMUTABLE_FIELDS`) | `entities.py:366-377`, `entity_service.py:149` |
| Project delete | Soft-delete only (`is_deleted=True`); `is_system` refused; **no cascade** — bound KBs, `ProjectKnowledgeBase`, `ProjectAgent`, `AgentConversation` survive orphaned; only `ProjectMemory` has ORM cascade (and it doesn't fire on soft delete) | `entities.py:543-559`, `entity_service.py:526-554`, `project.py:53-57` |
| Membership | **No membership model** — access = `role=="admin"` OR `created_by_id==user` OR `ResourceShare` grant (view-only for grantees on write) | `entities.py:266-278` |
| KB attach/remove | Dedicated attach router; removal = **unlink, not delete**; KBs remain "project-less" | `register_project_kb_router` (`main.py:1044`) |
| Conversation→project | `project_id` accepted from request body; stale ids stripped | `agents.py:6913-6948`, `llm_router.py:92-142` |
| Department | `Project.department` + conversation metadata → `_append_department_block` (`agents.py:3206-3218`) — **discovery-routing hint only, never authorization** | `project.py:44-50`, `semantic_capability_match.py:77,100-101` |

### 3.2 Answers to the conceptual questions

1. **Project = ?** A user-scoped *container of bound resources*, explicitly neither department nor workspace (`department` is a routing label; `resource_type` only toggles admin-lock of LLM config). ❓ Product decision required (see §12).
2. **Multiple departments/projects per enterprise?** ✅ Yes — unbounded projects per org; `department` labels group them for agent discovery only.
3. **Shared enterprise resources?** ✅ Yes — org-level ontology/metrics are implicitly shared by all projects; KBs are shareable via multi-project membership; `auto_bind_all_datasources` (workspace flag, default OFF) unions every DB KB into every agent (`data_source_runtime.py:1013-1064`).
4. **Resource in multiple projects?** ✅ KBs: yes (home FK + `ProjectKnowledgeBase` member rows, `project_knowledge_base.py:22-43`). Datasources: no project concept at all (agent-bound only). Agents: denormalized `AgentApp.project_id` + `ProjectAgent` member rows.
5. **How resources reach the general assistant:** `prepare_data_source_runtime` (`data_source_runtime.py:1366-1431`) computes `bound_kb_ids` = agent's own bindings ∪ conversation-project KBs (`_extend_with_project_kbs`, `:1416-1436`) ∪ workspace auto-bind → threaded into every tool's context (`db_tools.py:174-246` enforces the bound set).
6. **Can subagents exceed parent access?** ✅ **Yes, by design.** Children run under their OWN bindings ("discovery never grants data access; delegated children still run under their own bindings and the fail-closed permission checker" — `delegate_tool.py:1356-1360`). `DELEGATE_INHERIT_BINDINGS=False` (`config.py:1768-1774`) means the parent's KB set is NOT inherited (opt-in copy at `delegate_tool.py:930-934`); only governance/lineage keys are copied (`:940-949`). A published specialist with broader `AgentDataBinding`/project KBs therefore sees more than its caller.
7. **Leakage prevention:** bound-set gate (`db_tools.py:232-241`) + org/app filters on every KB query + RLS + delegation broker cycle/depth caps (`:583-598`). **Verified gap:** client-supplied body `project_id` is validated for existence only, not ownership (`llm_router.py:116-141`, `agents.py:6925-6938`) — any authenticated user in the same org can point a conversation at any live project and inherit its KBs (cross-org is blocked by the agent's org/app filters, `data_source_runtime.py:1150-1151`).

### 3.3 Conceptual model recommendation 🔶

Do **not** create new tables blindly — the existing columns already encode most distinctions. Recommended reading of the current model:

| Concept | Current carrier | Recommendation |
|---|---|---|
| **Organization** | `org_id` (+RLS) — already the tenant | Keep as the only hard boundary |
| **Enterprise Workspace** | The org itself + workspace settings (e.g. `auto_bind_all_datasources`) | Treat "workspace" == org-level config surface; no new entity |
| **Department** | `Project.department` + `AgentApp.department`-via-home-project | Promote to a first-class reference when EBPM `OrgUnit` stabilizes (link, don't duplicate) |
| **Project** | `Project` | Keep as a *work context* (container of work, not a security principal) |
| **Resource** | KB/Datasource/Automation/MCP/App models | Keep; add consistent binding discipline (see §11) |
| **Agent** | `AgentApp` | Keep; domain permissions via `AgentDomainBinding` |
| **User** | `User` + `ResourceShare` + roles | Keep; add project-ownership validation on `project_id` |

## 4. Enterprise Resource Lifecycle

### 4.1 What happens today per onboarding action ✅

| Action | Automatic | Manual / missing |
|---|---|---|
| Add database KB | Catalog reindex fires on create + connection-field change (`entities.py:58,96`; `catalog_triggers.py:291-344`): readiness `CONNECTED→discovering→indexed→enriched→validated→nl2sql_ready`; upserts `KBTableMeta/KBColumnMeta/KBTableRelation` idempotently (`catalog_indexer.py:1734-2096`); schema-cache invalidation (`entities.py:47-48`) | Ontology binding/EBPM discovery: **manual admin call** (`routers/ontology.py:706`). Drift: re-index repairs it (`catalog_indexer.py:1870`) but **no periodic drift detection** — only connection-edit, manual refresh, startup backfill |
| Upload document/SOP | Nothing automatic — embedding runs on `POST /{kb_id}/reindex` or inline in chat turn (`document_ingestion/service.py:250`, `agents.py:13665-13775`) | No background ingestion pipeline with retry |
| MCP server | Auto tool probe on connect (`knowledge_bases.py:216` → `_probe_server_background`) | Rehydration on restart is an on-demand endpoint, not startup (`mcp.py:579`) |
| ERP/CRM connector | **None — no connector registry**; `ontology/erp.py:24-61` has a sandbox adapter only; CRM: none | Real ERP/CRM integration unimplemented |
| New app (`app_id`) | None — `app_id` is a path param; `AgentApp.knowledge_bases/skills` are unvalidated JSON arrays (`agent_app.py:37-38`) | No post-create hook |
| Remove/disable resource | Soft-delete only; **no cleanup** of catalog rows, ontology bindings, `AgentDataBinding`, `AgentApp.knowledge_bases` JSON, EBPM `EnterpriseResource`/evidence → orphans | Only vector-index delete for file KBs (`service.py:394`) |

### 4.2 Existing hooks usable for the desired lifecycle 🔶

Desired: `registration → authorization → metadata discovery → evidence extraction → ontology mapping → process/capability proposals → human verification → runtime availability`.

Existing hook inventory (no Celery, no SQLAlchemy after_commit hooks, no event bus — verified `workload_miner.py:20`):
1. **Request-path:** `entities.py` KB create/update hooks (extend here: fire EBPM `refresh_inventory` + ontology `discover_and_recompute_coverage` after indexing).
2. **Startup/loop threads in `main.py` lifespan:** catalog watchdog 60s (`main.py:819-832`), coverage refresh (`:837-849`), workload miner 6h (`:855-876`), ontology sensors 60s (`:861-868`), automation reaper (`:784-792`), DataExecution cleanup 1h (`:806-815`), generic lifecycle-hook loader (`main.py:604-608` — plugin mechanism, today the natural place for an onboarding pipeline).
3. **Drift detection:** add a watchdog-style periodic signature check reusing the existing reindex path (audit precedent: `SchemaDriftSkill` pattern from prior work).
4. **Verification stage:** EBPM Review Center + `audit_events` already implement the human-verification stage end-to-end.

🔶 Recommendation: an `OnboardingPipeline` orchestrating the existing services in sequence per hook site, driven by the lifecycle-hook loader — **no new discovery engine** (explicitly reuse `catalog_indexer`, `extract_evidence`, `discover_and_recompute_coverage`).

## 5. Agent Execution Architecture

### 5.1 Execution paths and their context entry ✅

| Path | Entry | Context mechanism |
|---|---|---|
| ReAct (primary) | `agents.py:14852` | `get_system_prompt` + `data_ctx_extras` (bound_kb_ids) |
| Synexia FSM | `agents.py:12758-13098` | Same preflight (`prepare_data_source_runtime`) via `ExecutionRequest` |
| Workflow chat | `agents.py:12401` | `run_workflow_for_chat`; per-node context |
| NL2SQL | `nl2sql.ask` (`services/nl2sql/__init__.py:725`) | binding + Terminology + MetricDefinition + table retrieval weights |
| Document RAG | `document_ingestion/` + `upload_rag` | per-KB vector store; hybrid/agentic retrieval |
| Ontology tools | `ontology_tools.py` | on-demand, org-checked, binding-gated |
| Memory | `ProjectMemory` (shared), `AgentMemory` (private), scratchpad fallback | multi-turn continuity |

### 5.2 Answers ✅/🔶

1. **Where should enterprise context enter?** Two verified choke points cover every path: `get_system_prompt` (prompt-time) and `prepare_data_source_runtime` (tool-context time). 🔶 A **Business Context Service** with a bounded, evidence-annotated payload injected at both points would serve ReAct, FSM and workflow uniformly (FSM already consumes `data_ctx_extras`; workflows build prompts per node).
2. **Shared context service feasible?** ✅ Yes — `ContextBus.format_for_react` already exists as a precedent (used for delegation digests); `prepare_data_source_runtime` is already a single preflight for all paths.
3. **Parent→subagent business context:** today prose-only (`DELEGATE_CONTEXT_INHERITANCE` digest, `delegate_tool.py:845-855`); permission inheritance opt-in and default-OFF. 🔶 Pass a *reference* (domain ids + evidence status), never the raw payload; child re-resolves under its own bindings.
4. **Capability/domain enforcement:** `AgentDomainBinding.permission_scope` (read/write) at `ontology/permissions.py:107-115` + `compute_metric` gate (`ontology_tools.py:334-357`) — exists for ontology tools, absent from delegation and general KB tools.
5. **How should agents pick relevant processes/datasources?** 🔶 Retrieval, not enumeration: reuse the `retrieval_evidence` weighted-scoring pattern (`retrieval_evidence.py:61`) over ontology objects/processes (name/purpose embedding + terminology hits), returning top-k with evidence status.
6. **Avoid injecting the whole ontology:** bounded prompt precedent already in-code: `_bounded_prompt_evidence` (≤40 items/24k chars, `enterprise_discovery.py`), `_MAX_OBJECT_EVIDENCE=30`, delegation digest cap 1200 chars. 🔶 Same discipline: top-k + token budget + cache by (org, project, hash).
7. **Multi-turn context:** ✅ conversation rows + `ProjectMemory` (content-hash dedup) + scratchpad fallback already persist; add "verified business facts" as a `ProjectMemory.entry_type` rather than a new store.

## 6. Ontology and Semantic Architecture

### 6.1 Concept inventory and overlap analysis ✅

| Pair | Overlap? | Verdict |
|---|---|---|
| `OntologyObject` vs `Terminology` | Terms name things; objects ARE things | **Keep separate**; 🔶 add explicit term→object link (today Terminology is NL2SQL-only, `semantic_store.py:583`) |
| `MetricDefinition`/`ProjectMetric` vs ontology metric-backing | MetricDefinition is governed & versioned; ontology metric-objects reference it via `backing_kind=metric` (`ontology/data.py:51-57`) | **MetricDefinition authoritative for computation**; ontology object = business-facing façade. No merge |
| EBPM `OrgUnit` vs `Project.department` | Department label vs discovered org structure | **OrgUnit authoritative** (evidence-backed, verifiable); department becomes a projection |
| EBPM `BusinessProcess` vs ontology `OntologyLink`/Actions | Different axes (workflows vs entity relations/verbs) | **Keep separate**; add explicit process↔object relationship (via `ProcessEvidence.source_ref = ontology:{id}` — already exists as convention, `enterprise_discovery.py` get_process) |
| `BusinessRuleExtractor` guidelines vs ontology Actions | Comment-mined SQL hints vs governed write actions | **Keep separate**; rules are prompt-level guidance (staged disabled), actions are executed verbs |
| Identity resolution vs EBPM evidence | Identity dedups entities across DBs; EBPM proves processes | Complementary; both human-confirm gated — 🔶 share the same review-center UX |
| `OntologyEvolutionState`/revisions vs EBPM review_history | Versioning of ontology vs decision log of proposals | **Both needed**; 🔶 unify event vocabulary in `audit_events` (currently free-form — verified: written types not all declared, `audit_event.py:16-24` vs actual writers) |

### 6.2 Multi-database mapping and cross-database relationships ✅

- Mapping: `backing_ref = kb:<kb_id>:<table>` per object — different objects may resolve to different org-owned KBs at runtime (`ontology/data.py:77-81, 293-303`). **This already is multi-database mapping.**
- Cross-database relationships: `OntologyLink` connects objects whose backings may live in different KBs, but **no join machinery exists** — links drive fan-out notifications only (`ontology/fanout.py:65-72`). 🔶 Safe future resolution: object A read + object B read (each via its own KB, policy-checked) joined in the result contract at the application layer — never federated SQL.
- 🔶 Vocabulary home: `Terminology` (org/agent/datasource-scoped, verified-tier) should be the single vocabulary store, linked to ontology objects; EBPM quotes stay evidence, not vocabulary.
- 🔶 Evidence unification: one provenance envelope — `{source_type: kb|doc|ontology|execution, source_id, quote/ref, evidence_status, verified_by, at}` — consumed by `result_contract` citations, EBPM `ProcessEvidence`, and doc grounding, instead of three vocabularies.

## 7. Business Process Intelligence

### 7.1 Execution record inventory ✅ (from code inspection)

| Model | Analytic fields | Writer |
|---|---|---|
| `AutomationExecution` (`automation_execution.py:15-74`) | status, started/completed/duration, output_data JSON, activity_steps, parent_execution_id | `automation_dispatcher.py:447-632` |
| `AgentRun`/`AgentRunStep` (`agent_run.py:13-39`, `agent_run_step.py:13-52`) | tool_calls array, iterations, parent_run_id, caller_context; per-step tool_name/args/result_preview/duration/tokens/status | `harness/run_service.py:37-301` |
| `WorkflowRun`/`WorkflowRunStep` (`workflow_run.py:14-44`) | trigger, per-node node_id/type/status/latency/output_json | `workflow_runtime.py:472-521` |
| `Nl2sqlQueryLog` (`nl2sql_query_log.py:13-35`) | question, generated_sql, **lineage_json.tables**, policy_decision, row_count, duration_ms, outcome, feedback | `nl2sql/__init__.py:725` |
| `AuditEvent` | event_type/actor/resource/outcome/detail_json (declared types partly unwritten; written types partly undeclared — free-form) | 8 writer files |
| `ApprovalRequest`/`OutcomeMeasurement` (`governance.py:46-76`, `outcome.py:20-63`) | risk_tier, reviewer chain; governed baseline/observed/realized impact + evidence_refs | ontology outcomes/lifecycle |

### 7.2 Distinguishing documented / configured / observed ✅

- **Documented:** EBPM `discovery_status='documented'` from SOP chunks — live.
- **Configured:** `discovery_status='configured'` from automations/actions evidence — live; also directly enumerable from `AutomationTask` + ontology Actions.
- **Observed:** ❌ **nothing today** — no execution record carries `process_id`/`ontology_id` (grep across `app/models/`: hits only inside `enterprise_process.py` itself). The nearest links are `AuditEvent.resource_type/resource_id` strings, `OutcomeMeasurement` lineage, and `ProcessEvidence.source_ref` naming `execution_log` sources without FK.
- ⚠️ Honesty constraint: an execution log proves an activity ran, **not** that a discovered process is complete or correct — process-matching must be heuristic (tool/task name ↔ activity name) with an explicit `observed_confidence`, and must never auto-promote `discovery_status` (the discovery prompt already forbids claiming observed; `enterprise_discovery.py`).

## 8. Security and Governance

### 8.1 Authorization layers today ✅

1. Org isolation: RLS + `org_id` filters everywhere (verified: `_tenant_filters`, entity/entity-service filters).
2. Agent resource scope: `bound_kb_ids` bound-set gate (`db_tools.py:174-246`), `AgentDataBinding` tables/columns/row_filters.
3. KB/table/column/row policy: `ResourceAccessPolicy` specificity resolution, default-allow when absent, fail-open on resolution errors (`access_policy_service.py:100-165` — ⚠️ fail-open is a deliberate but notable posture).
4. Ontology: `AgentDomainBinding.permission_scope` on actions/metrics; per-viewer document evidence fail-closed (`doc_access.py`); human confirm for identity/evidence.
5. Governance: approval gates by risk tier (`governance.py`), delegation broker (cycle/depth/count), audit events (free-form types), `OutcomeMeasurement` verification.

### 8.2 Gaps that materialize when context becomes shared 🔶 (each grounded in verified code)

1. **`project_id` spoofing** (§3.2.7) — shared context amplifies it: a forged project id would pull another owner's KB set into the *context payload* too. Fix: ownership check at `resolve_message_project_id` + `agents.py:6925`.
2. **Shared Business Context Service becomes a new data surface** — it must apply the same policy stack (`resolve_policy_from_context`) per object/process, not just per KB; evidence rows referencing restricted tables must be filtered per viewer (precedent: per-viewer doc evidence, `doc_access.py`).
3. **Fail-open policy resolution + default-allow** could leak through the broader context surface — consider fail-closed for the context service only.
4. **Subagent > parent access** is intentional; shared context must preserve that invariant (context references resolve per-child).
5. **Prompt-injection via evidence quotes** (verbatim document text enters prompts) — quotes must be escaped/marked and never executed as instructions (today `_DISCOVERY_PROMPT` already treats them as data).
6. **Orphaned bindings after resource removal** (§4.1) — dead `backing_ref`s could silently resolve to nothing (honest refusal today) or, worse, to a re-registered different resource with the same id pattern; cleanup hook recommended.

### 8.3 Consistent authorization model 🔶

Keep the three-ring model that already exists — org (RLS) → project/agent scope (bound sets) → resource policy (tables/columns/rows) — and extend it with: **evidence-aware ring** (an ontology element is visible to an agent only if at least one backing resource passes rings 2-3 for that agent; proposals without live backing are prompt-labeled, not hidden). No new enforcement plane; reuse `resolve_policy_from_context`.

## 9. Performance and Scalability

| Concern | Current bounds (verified) | 🔶 Strategy |
|---|---|---|
| Large ontologies | Prompt budget 40 items/24k chars (`_bounded_prompt_evidence`); object evidence cap 30; coverage computed on demand | Top-k retrieval with embedding + terminology scoring (reuse `retrieval_evidence` pattern); cache per (org, project, context-hash); invalidate on discovery/review events |
| Thousands of tables | Catalog already scales: weighted retrieval over `kb_table_meta` (plan 3.0/text 3.0/metric 1.5/column 2.25/embedding 0.75); schema cache with invalidation | Add pg vector/existing embedding index on table descriptions; per-agent view pruning before scoring |
| Many processes/activities | EBPM catalogue paginated (≤100); map layout manual | Process retrieval by family/performer filters + purpose search; lazy detail loads |
| Long conversations | Memory cap + scratchpad fallback; delegation digest 1200 chars; reflexion bounded | Sliding-window + `ProjectMemory` summaries (entry_type exists) |
| Large doc repos | Chroma per-org collections; hybrid RRF search; semantic cache exists (agentic RAG) | Version collections (versioning.py exists); index lifecycle on reingest |
| Frequent resource changes | Reindex idempotent; drift repair on reindex; watchdog loops exist | Event-driven refresh via entity hooks + periodic drift signature check (cheap `information_schema` hash) |
| Context service cost | — | Never full-ontology: per-turn payload budget (audit precedent), LRU cache keyed by (org, project, agent, version) where version = max(discovery run, review event) |

## 10. Existing Components to Reuse

1. `prepare_data_source_runtime` — the single preflight choke point (attach Business Context here).
2. `get_system_prompt` block architecture + `_bounded_prompt_evidence` budget discipline.
3. `retrieval_evidence` weighted scoring — template for business-context retrieval.
4. `Terminology` store + `MetricDefinition` computation + `compute_metric` gating.
5. `ontology/data.py` backing-ref resolution + org checks.
6. EBPM discovery/evidence/review/audit pipeline (increments 1–5) — verification stage done.
7. `ContextBus.format_for_react` — delegation digest precedent.
8. `catalog_triggers` + entity hooks + `main.py` lifespan loops + lifecycle-hook loader — onboarding pipeline rails.
9. `ResourceAccessPolicy` resolution stack — per-object authorization in the context service.
10. `AgentRun/AgentRunStep` + `Nl2sqlQueryLog.lineage_json` — future observed-execution join targets.

## 11. Architectural Duplications and Conflicts

1. Terminology/ProjectMetric vs ontology objects/metric-objects — two "meaning" stores (§6.1).
2. Three provenance vocabularies: `ProcessEvidence`, doc grounding, figure grounding (§6.2).
3. Resource binding asymmetry: KBs project+agent, datasources agent-only, legacy `project` name-string alongside `project_id`.
4. Department as routing label vs EBPM `OrgUnit` — same concept, two models.
5. Audit event types: declared list vs actual free-form writes (verified mismatch).
6. Soft-delete-only resources with no cascade → orphaned catalog/ontology/binding/EBPM rows.
7. FSM vs ReAct dual paths — context integration must be duplicated or the paths consolidated.

## 12. Unresolved Questions (product-owner decisions)

1. ❓ What IS a Project in target terms — work context, department proxy, or workspace? (drives whether `department` promotes to `OrgUnit` link or a new entity)
2. ❓ Should subagent access exceed parent (current intentional design) or inherit-and-narrow? (`DELEGATE_INHERIT_BINDINGS` default)
3. ❓ Is workspace-wide `auto_bind_all_datasources` an acceptable permanent feature or should shared context make it obsolete?
4. ❓ Per-viewer evidence filtering: should AI proposals be hidden from non-admin agents or labeled? (visibility vs prompt-noise tradeoff)
5. ❓ Cross-database joins at application layer (recommended) vs read-only federation — appetite/latency budget?
6. ❓ Observed-execution ambition: per-activity confidence scoring acceptable, or require exact process confirmation before surfacing?

## 13. Evidence-Based Architectural Recommendations (priority order)

1. **Project-ownership validation** on client-supplied `project_id` (`llm_router.py:116-141`, `agents.py:6925`) — security fix, prerequisite for any shared context.
2. **Business Context Service** at the two verified choke points (`get_system_prompt`, `prepare_data_source_runtime`): bounded top-k retrieval over ontology objects + verified processes + terminology, evidence-status annotated, policy-filtered per agent, cached by (org, project, agent, version). Serves ReAct/FSM/workflow uniformly.
3. **Domain filter in delegation** (`delegate_tool.py:1346-1443`) using `AgentDomainBinding`.
4. **Ontology provenance in `result_contract`** + answer-citation block extension (`agent_prompts.py:2576-2585`).
5. **Onboarding pipeline** on existing entity hooks + lifecycle-hook loader: index → EBPM inventory → ontology discovery → review queue (no new engines).
6. **Resource-removal cleanup** (delete-path hook in `entities.py` — currently create/update only) for catalog rows, bindings, EBPM inventory.
7. **Evidence-provenance envelope** unifying the three vocabularies (§6.2).
8. **Observed-execution mapping** (increment 6): add `process_id` linkage to execution records, heuristic activity matching with explicit confidence — never auto-promote.
9. **Terminology↔ontology-object link**; EBPM `OrgUnit` becomes authoritative for departments.
