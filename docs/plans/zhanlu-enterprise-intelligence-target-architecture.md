# Zhanlu Enterprise Intelligence — Target-State Architecture

**Date:** 2026-11-01 · **Branch:** `fix/automation-chat-artifacts-ordering` · **HEAD:** `010f282f6`
**Evidence basis:** `docs/plans/zhanlu-current-enterprise-architecture-audit.md` + `docs/plans/zhanlu-enterprise-intelligence-target-architecture-investigation.md` (same HEAD, verified against code). Every claim below is tagged ✅ VERIFIED (code evidence), 🔶 RECOMMENDATION (design decision), or ❓ UNRESOLVED (product decision needed). Implementation roadmap: `docs/plans/zhanlu-enterprise-intelligence-roadmap.md`.

---

## PART I — TARGET ARCHITECTURE

### 1. Design Principles

1. **One knowledge model, one authoritative owner per concept.** No parallel semantic stores (P0 of the redesign: bridge, don't duplicate).
2. **Retrieval, not enumeration.** Business context is dynamically retrieved per turn under token budgets — the full ontology is never injected (budget precedent: `_bounded_prompt_evidence`, 40 items/24k chars, `enterprise_discovery.py`).
3. **Everything is evidence-annotated.** Verified / proposed / rejected / observed travel with every concept from store to prompt to answer citation.
4. **Reuse the rails.** All new work plugs into verified choke points (`prepare_data_source_runtime`, `get_system_prompt`, entity hooks, lifespan loops, Review Center) — no new discovery engines, no new enforcement planes.
5. **Security prerequisites first.** The two verified holes (unvalidated `project_id`, delete-path orphaning) are fixed before context is shared more widely.

### 2. A. Enterprise Workspace and My Space — Conceptual Model

```
Organization (org_id, RLS)                      ← the ONLY hard security boundary ✅
  └─ Enterprise Workspace (= org-level configuration surface) ✅
       ├─ Departments  = EBPM OrgUnit (authoritative, evidence-backed) 🔶
       ├─ Enterprise Resources (KB, Datasource, Document, MCP, App, Automation) ✅
       ├─ Shared Knowledge (ontology, terminology, metrics, processes) ✅ org-scoped today
       └─ Projects = WORK CONTEXT (not security boundary, not department) 🔶
            ├─ bound resources (KB home∪member, agents, automations, dashboards) ✅
            ├─ project general assistant + subagents ✅
            └─ project memory, LLM config ✅
```

| Concept | Current carrier (✅) | Target semantics 🔶 |
|---|---|---|
| **Organization** | `org_id` on every model + Postgres RLS (`app/models/base.py:43-48`) | Unchanged — the tenant and the hard boundary |
| **Enterprise Workspace** | Org-level settings (workspace flags, marketplace, LLM catalog) | A *configuration surface of the org*, not a new entity |
| **Department / OrgUnit** | `Project.department` label (routing only, `project.py:44-50`) + EBPM `OrgUnit` (discovered, verifiable, `enterprise_process.py`) | `OrgUnit` authoritative; `department` becomes a resolvable FK/link to `OrgUnit` (soft migration); departments group agents for discovery AND scope shared-context visibility (labeled, never hidden — Decision 6) |
| **Project** | `Project` + `ProjectKnowledgeBase`/`ProjectAgent` joins + `ProjectMemory` | A **work context**: a named container of resources, agents and memory. It determines *convenience scoping* (what the assistant sees by default), never authorization on its own; ownership of the referenced project is validated on every message (fix P0) |
| **Enterprise resource** | KB, Datasource, MCP server, AutomationTask, `EnterpriseResource` (EBPM inventory) | Registration = onboarding pipeline entry (§6); deletion = hooked cleanup (fix P0) |
| **General assistant** | Per-conversation `AgentApp` resolved org-scoped (`agents.py:5723`); project association via `conversation.project_id` | Default per project; consumes BusinessContextService with project filter |
| **Department agent** | Published `AgentApp` with home-project department; bound via `AgentDomainBinding` | Specialist carrying department/domain scope; participates in discovery routing (`semantic_capability_match`) AND domain-gated tools |
| **Subagent** | `delegate_task` children under own bindings (`delegate_tool.py:1356-1360`) | Same, with **default permission ceiling = caller's effective authorization** (Decision 4) |
| **User & permissions** | roles (`admin`/`user`), `ResourceShare`, `ResourceAccessPolicy` (KB/table/column/row) | Unchanged; add project-ownership check + evidence-aware ring (§7) |

**Resource sharing model 🔶:** a resource is *registered once at org level*; projects *reference* it (home/member); agents *bind* to it; policies *constrain* it. Sharing across departments is explicit (member rows / agent bindings), never ambient — except the transition flag `auto_bind_all_datasources` (Decision 5).

### 3. B. Enterprise Knowledge Model — one shared architecture

**Authoritative registry 🔶** (each concept has exactly one owner; consumers link):

| Concept | Authoritative model (✅ exists) | Role in target | Consumers |
|---|---|---|---|
| Business objects/classes | `OntologyObject` (+properties) | Canonical "things of the business" | all agents via BCS, NL2SQL bridge |
| Object relationships | `OntologyLink.link_type` | Typed relations; application-layer cross-DB joins | fan-out today; queries later |
| Vocabulary & synonyms | `Terminology` (`db/semantic_store.py:583`) | **Single vocabulary home**; gains explicit `term→object` link 🔶 | NL2SQL expansion, BCS |
| Business processes/activities | `BusinessProcess`/`ProcessActivity` (EBPM) | How work flows | BCS, UX "How We Work", flow canvas |
| Org structure/responsibilities | `OrgUnit`, activity `performer` | Who does what | BCS, UX, routing |
| Business rules/policies | **NEW `BusinessRule` concept** 🔶 (see below) | Constraints on decisions & data | NL2SQL guidelines, BCS, action gating |
| Metrics/KPIs | `MetricDefinition` (governed, versioned) + `ProjectMetric` | **Computation authority**; ontology metric-objects are façades (`ontology/data.py:51-57`) | `compute_metric`, dashboards, outcomes |
| Evidence/provenance | `ProcessEvidence` + doc grounding + figure grounding | **NEW canonical provenance envelope** 🔶 unifies all three | citations, review center, BCS |
| Resource↔concept mapping | `backing_ref kb:<kb_id>:<table>` + `AgentDomainBinding` | Live data wiring | `read_object_data`, NL2SQL bridge |
| Process↔object relations | `ProcessEvidence.source_ref = ontology:{id}` convention ✅ | Promoted to first-class links 🔶 | process intelligence, UX |
| Entity identity | `app/models/identity.py` (human-confirm) | Cross-DB entity resolution | future query federation |
| Versioning/evolution | `OntologyRevision`/`OntologyEvolutionState` + EBPM `review_history` | Change history | governance UX |

**New concept evaluation 🔶:**

- **`BusinessRule` (new table, one migration):** today rules exist as staged-disabled NL2SQL guidelines mined from column comments (`business_rule_extractor.py:1-23`) and risk tiers on actions. Target: a first-class `{scope: org|domain|object|process, statement, source_evidence_ids[], status: proposed|verified|rejected|enabled, applies_to: sql|action|process}` — sourced by EBPM evidence, enabled by humans (never auto), consumed by (a) NL2SQL prompt guidelines, (b) BCS rule retrieval, (c) action pre-checks. This **absorbs** SemanticGuidelines as one `applies_to=sql` subset rather than duplicating them.
- **Canonical provenance envelope (new shared schema, no table):** `{envelope_id, source_type: kb|doc|ontology|execution|metric, source_id, locator, quote?, evidence_status: verified|proposed|rejected|observed?, confidence?, discovered_at, reviewed_by?, review_history_ref?}` — emitted by EBPM evidence, doc grounding and the metric lineage; consumed by BCS, `result_contract.provenance`, and answer citations. Implementation = a shared serializer module, not a new store.

**Anti-duplication rule 🔶:** `Terminology`/`MetricDefinition`/`ProjectMetric` keep their tables; ontology gains *links*, never copies. The NL2SQL-side stores remain authoritative for their retrieval behavior; the ontology remains authoritative for business meaning.

### 4. C. Business Context Service (BCS)

**One new service, two injection points** — verified choke points that already serve every execution path (ReAct `agents.py:14852`, FSM `agents.py:12758-13098`, workflow `agents.py:12401`):

```
app/services/enterprise/business_context.py          (NEW)
build_business_context(db, *, org_id, project_id, agent_app, user,
                       question, turn_history_digest, budget) -> BusinessContext
```

**Inputs:** org/user/agent/project ids, the user question (and optional intent digest), token budget, per-domain visibility grants.
**Pipeline (each stage bounded, fail-open-degrade, cache-aware):**
1. **Intent sketch** — cheap lexical+embedding pass over the question (reuse `semantic_capability_match` embedding infra); no LLM call required for v1.
2. **Object retrieval** — top-k ontology objects by terminology hits + embedding + domain binding (`AgentDomainBinding`); weights mirror `retrieval_evidence.py:61`.
3. **Process retrieval** — top-k processes by purpose/name/family match + performer overlap; activities included only for the top process(es).
4. **Terminology & metrics** — verified terms and published `MetricDefinition`s matching the intent (reuse `semantic_store.py:583-665` queries).
5. **Rule retrieval** — verified+enabled `BusinessRule`s scoped to matched objects/processes.
6. **Resource resolution** — objects → `backing_ref` → org-checked KBs → intersect with the agent's `bound_kb_ids` + `ResourceAccessPolicy` (per-object, not just per-KB).
7. **Evidence ranking** — sort by `verified > observed > proposed > rejected(excluded)` then confidence; every item carries the provenance envelope.
8. **Assembly under budget** — sections with per-section caps (objects ≤8, processes ≤3, terms ≤15, rules ≤6, metrics ≤5), total ≤ context-token budget (default 2k tokens; config).

**Output `BusinessContext` (dataclass):** `{objects[], processes[], terms[], rules[], metrics[], resources[], warnings[], budget_used, context_version}` — rendered by two small renderers: prompt block (for `get_system_prompt`) and tool-context dict (for `data_ctx_extras`).

**Caching/invalidation:** LRU keyed by `(org, project, agent, question-intent-hash, context_version)`; `context_version = max(discovery_run_at, latest review event, catalog reindex at)` — bumped automatically by the same hooks that run discovery (§6). TTL fallback 15 min.
**Failure behavior:** any stage error → omit that section + `warnings[]` (honest degradation, matching platform convention — e.g. `ontology/data.py:62-75` refusals, `semantic_store` degrade). **Insufficient evidence → explicit `"business_context": "none_available"` block, never silence, never invention.** The service NEVER promotes proposals; it labels them.

### 5. D. Agent Architecture — one knowledge model, many runtimes

| Runtime | Integration (all verified entry points) |
|---|---|
| Project general assistant (ReAct, `agents.py:14852`) | BCS prompt block in `get_system_prompt` + BCS tool-context in `data_ctx_extras` |
| Department agents | Same, plus department-filtered process/org-unit retrieval; `AgentDomainBinding` gates domain tools (existing, `permissions.py:107-115`) |
| Subagents | Prompt digest (existing `DELEGATE_CONTEXT_INHERITANCE`) **plus** a context *reference* (domain ids + version); child re-resolves via BCS under its own ceiling (Decision 4) |
| Synexia FSM | Already consumes `data_ctx_extras` (`agents.py:13094-13098`) — BCS dict drops in unchanged |
| Workflow execution | Per-node prompts pull the same BCS payload via `prepare_data_source_runtime` preflight |
| NL2SQL | Bridge (new, highest leverage): BCS objects/terms/rules feed `table_retrieval` weighting (`retrieval_evidence.py:61`), the query contract, and verified `BusinessRule` guidelines (absorbing staged SemanticGuidelines) |
| Document RAG | Unchanged stores; BCS may cite doc-derived evidence through the provenance envelope |
| MCP tools | Unchanged registration/grants; MCP servers appear as enterprise resources in the inventory (§6) |

**Context propagation during delegation 🔶:** child receives `{context_version, matched_domain_ids, question_digest}`; it rebuilds context itself. Nothing unfiltered crosses the boundary — the digest stays prose-only, permissions stay per-child.

**Permission inheritance (Decision 4) 🔶:** default = **ceiling model**: child's effective authorization = `own bindings ∩ caller's effective authorization`. The current intentional widening (child may exceed parent, `delegate_tool.py:1356-1360`) becomes an explicit, audited **elevation**: `delegate_task(..., elevated=true)` allowed only for published specialists and recorded in the delegation broker audit. This is a behavior change gated behind a flag (`DELEGATE_PERMISSION_CEILING`, default off at rollout, then on).

### 6. E. Resource Discovery and Enterprise Evolution — one lifecycle, existing engines

```
registration ─→ authorization ─→ technical discovery ─→ semantic discovery
     │                                                        │
 deletion/drift ◀── continuous updates ◀── human review ◀── proposals/evidence
```

**Reuse map (✅ everything exists; 🔶 = new orchestration only):**

| Stage | Existing engine (reuse, ✅) | New work 🔶 |
|---|---|---|
| registration | entity CRUD + `_maybe_fire_catalog_index` (`entities.py:58,96`), MCP probe (`knowledge_bases.py:216`) | extend hooks: after successful index → fire stage 3-5 (async thread, platform convention) |
| authorization | org scoping + `ResourceAccessPolicy` resolution | none |
| technical discovery | `catalog_indexer` (KBTableMeta/Column/Relation, idempotent, drift-repairing `catalog_indexer.py:1734-2096`) | periodic drift signature check as a lifespan watchdog (pattern exists: `run_catalog_watchdog` `main.py:819-832`) |
| semantic discovery | EBPM `refresh_inventory` + `extract_evidence` (verbatim, bounded) | invocation from hooks |
| ontology mapping | `discover_and_recompute_coverage` (`ontology/discovery.py:331-531`, never overwrites human decisions `:386-387`) | same hook chain |
| process discovery | `propose_operating_model` (bounded LLM, traceable ids) | same hook chain |
| human review | Review Center + review endpoints + AuditEvent (EBPM incr 3/5) | add `BusinessRule` and resource-removal reviews |
| runtime publication | BCS `context_version` bump on review/discovery events | version bump hook |
| continuous updates | lifespan loops (`main.py:819-876`), ontology sensors (`:861-868`) | drift + reingest watchdogs in the same style |
| deletion | — | **new delete-path hook** in `entities.py` (currently create/update only): soft-delete catalog rows, flag ontology bindings `orphaned`, drop agent-binding references, mark EBPM inventory rows |

**Explicitly NOT built:** no second discovery engine, no event bus, no Celery — the platform has none by design (`workload_miner.py:20`) and the hook-loader + lifespan-thread pattern suffices.

### 7. F. Business Process Intelligence

**Process model connections 🔶** (all anchored in existing models):

| Link | Mechanism |
|---|---|
| Departments & performers | `ProcessActivity.performer` → matched to `OrgUnit.name` (existing convention in `list_processes.responsible_units`) |
| Business objects | `ProcessEvidence.source_ref = ontology:{id}` → resolved to `OntologyObject` (existing in `get_process`) |
| Systems/resources | EBPM `EnterpriseResource` inventory; process evidence carries `kb:`/`automation:`/`action:` refs |
| Rules & decisions | `ProcessActivity.business_rules` + activity_type ∈ decision/approval; future `BusinessRule.scope=process` |
| Metrics & outcomes | `OutcomeMeasurement` (`outcome.py:20-63`, governed baseline/observed/realized) linked to process KPIs 🔶 |
| Execution records | **NEW linkage columns** 🔶: `process_id` (nullable) on `AutomationExecution`, `AgentRun`, `WorkflowRun`, `Nl2sqlQueryLog` + an `ObservedActivity` matcher service |

**Three-state discipline (✅ enforced today, preserved):** `documented` (SOP chunks) / `configured` (automation+action evidence) / `observed` (timestamped execution records ONLY — the discovery prompt is forbidden from claiming it). 🔶 Observed mapping is heuristic (tool/task name ↔ activity name) with `observed_confidence`; an execution log proves an activity ran, **never** a complete process; `discovery_status` is never auto-promoted; observed signals surface as badges (canvas `DISCOVERY_FILL.observed` already exists) and review-queue candidates, not as verified truth. Failure/bottleneck detection = duration/error aggregation over linked records per activity (future).

### 8. G. Security and Governance

**Prerequisites (P0, before shared context) 🔶:**
1. **Project-ownership validation** — client-supplied `project_id` must pass `created_by_id`/`ResourceShare`/admin check at `llm_router.py:116-141` and `agents.py:6925-6938` (verified hole: existence-only check).
2. **Delete-path cleanup hook** — prevent orphaned bindings resolving against re-registered resources.

**Layered authorization (existing planes, no new enforcement engine):**

| Ring | Mechanism (✅) | Shared-context application 🔶 |
|---|---|---|
| Org | RLS + `org_id` filters | BCS queries are org-scoped by construction |
| Project/agent scope | `bound_kb_ids` bound-set gate (`db_tools.py:174-246`) | BCS resource resolution intersects bound sets (§4 step 6) |
| Resource policy | `ResourceAccessPolicy` KB/table/column/row (`access_policy_service.py:100-165`) | BCS applies per-object, and **fails closed** for the context service (unlike the current default-allow/fail-open data path — narrower surface, deliberate) |
| Domain | `AgentDomainBinding.permission_scope` | domain-gated BCS sections + delegation filter |
| Evidence visibility | per-viewer doc evidence (`doc_access.py`), review center admin gate | proposals are **labeled, never hidden** from agents (Decision 6); verified-only for anything that steers SQL |
| Delegation | broker caps + new ceiling model (Decision 4) | elevated delegation audited |
| Writes | approval gates by risk tier (`governance.py`), `OutcomeMeasurement` verification | unchanged; `BusinessRule.applies_to=action` adds pre-checks |
| Audit | `audit_events` (event-type registry tightened 🔶) + provenance envelope | every BCS payload version is reconstructible |

**Cross-database resolution safety:** per-object org-checked reads through `QueryService` (existing, `ontology/data.py:293-303`), joined in the result layer — never federated SQL, never cross-org.

---

## PART II — FIVE ENGINEERING SPECIFICATIONS

### Spec 1 — Enterprise Knowledge Model Specification

- **Purpose & responsibilities:** single authoritative home per business concept; explicit links where consumers need cross-concept navigation; canonical provenance envelope.
- **Existing components reused:** `OntologyDomain/Object/Link/Action`, `MetricDefinition`, `Terminology`, EBPM 7 models, `identity.py`, `OntologyRevision` (all ✅, paths in investigation §6).
- **Proposed interfaces/contracts:**
  - `BusinessRule` table: `id, org_id, scope(enum), scope_ref_id, statement(Text), source_evidence_ids(JSON), status(proposed|verified|rejected|enabled), applies_to(sql|action|process), created/updated/review_history` — additive migration; SemanticGuidelines imported as `applies_to=sql` rows on first run (one-time backfill script, bridge-only per platform rule).
  - `Terminology.ontology_object_id` (nullable FK) + index.
  - `project_evidence_envelope(evidence_row) -> dict` serializer in `app/services/enterprise/provenance.py` (shared by EBPM, doc grounding, BCS, `result_contract`).
  - `Project.department_org_unit_id` nullable FK (label kept for back-compat).
- **Dependencies:** migration runner; EBPM review endpoints (for rule review reuse).
- **Security:** rules/evidence inherit org scoping + review gating; no auto-enable.
- **Failure handling:** missing link targets render as warnings, never break retrieval; envelope tolerates absent fields.
- **Acceptance criteria:** (1) zero remaining "second semantic store" writes — guideline writes route through `BusinessRule`; (2) every EBPM/doc/metric evidence item serializes through the envelope in tests; (3) terminology↔object link resolvable in one query.
- **Boundaries:** no changes to existing table columns beyond additive nullable/FK fields; no data model for "workspace" (org config surface only); no ontology object writes from BCS.

### Spec 2 — Business Context Service Specification

- **Purpose:** dynamic, authorized, budgeted business context for every runtime (§4 above is the contract).
- **Existing components reused:** `semantic_capability_match` embeddings, `Terminology`/`MetricDefinition` queries (`semantic_store.py:583-665`), `retrieval_evidence` weighting, `backing_ref` resolution (`ontology/data.py`), bound-set + policy stack, `_bounded_prompt_evidence` budget discipline, `ContextBus` (digest precedent).
- **Proposed interfaces:**
  - `build_business_context(...) -> BusinessContext` (dataclass; §4).
  - `render_prompt_block(ctx) -> str` (verified-first sections, `[status]` tags, warnings footer).
  - `render_tool_context(ctx) -> dict` (drop-in for `data_ctx_extras["business_context"]`).
  - Config: `BCS_ENABLED` (flag, default off), `BCS_TOKEN_BUDGET=2000`, `BCS_TOP_OBJECTS=8`, `BCS_TOP_PROCESSES=3`.
- **Dependencies:** Spec 1 (envelope, rules); review/discovery hooks for `context_version` (Spec 4).
- **Security:** per-object policy intersection; fail-closed on policy resolution errors; proposals labeled; org-scoped queries only; no write access.
- **Failure handling:** per-stage isolation with `warnings[]`; empty-context honest block; cache misses rebuild synchronously with a hard wall-clock cap (2s, then serve degraded inventory-only context).
- **Acceptance criteria:** (1) integration test — ReAct turn with BCS on shows verified object/process in prompt and NONE of the forbidden patterns (no full-ontology dump: payload ≤ budget); (2) FSM turn consumes tool-context unchanged; (3) missing ontology → `"none_available"` block, agent still answers via NL2SQL; (4) policy-restricted object absent from context for restricted agent; (5) context_version bump invalidates cache.
- **Boundaries:** BCS never calls LLMs in v1; never writes; never bypasses `bound_kb_ids`.

### Spec 3 — Agent Runtime Integration Specification

- **Purpose:** wire BCS into all runtimes + delegation ceiling.
- **Existing components reused:** `get_system_prompt` block registry (`agent_prompts.py:2393-2612`), `prepare_data_source_runtime` preflight (`data_source_runtime.py:1366-1431`), `delegate_tool` broker/audit, `agents.py` FSM/workflow branches.
- **Proposed interfaces:**
  - Prompt block `BUSINESS_CONTEXT` registered like existing blocks (verified insertion point: after Bound-Data-Sources block, `data_source_runtime.py:836-863`).
  - `data_ctx_extras["business_context"]` populated when `BCS_ENABLED`.
  - `delegate_task(..., elevated: bool = false)` + `DELEGATE_PERMISSION_CEILING` flag: when on, child bound-set = `child_bindings ∩ caller_effective` (implemented at `_child_ctx` assembly, `delegate_tool.py:940-949`), elevations audited via broker.
  - NL2SQL bridge: BCS objects/terms/rules → `retrieval_evidence` extra-evidence fields + verified rules into the prompt-guideline stage (replacing disabled-stage SemanticGuidelines path).
- **Dependencies:** Spec 2; delegation broker (exists).
- **Security:** ceiling model default after rollout; elevation audited; NL2SQL bridge consumes only `verified`/`enabled` rules; prompts never receive rejected evidence.
- **Failure handling:** BCS failure ⇒ runtimes behave exactly as today (flag-off equivalence is an acceptance test).
- **Acceptance criteria:** (1) flag-off A/B: byte-identical prompts (regression guarantee); (2) delegation ceiling test — child denied parent-only KB under flag-on; elevation audited; (3) NL2SQL: ontology-bound table outranks unbound synonym-only table in retrieval for the same question; verified rule appears in SQL prompt, proposed rule does not.
- **Boundaries:** no changes to tool schemas; no FSM/workflow prompt-format redesign; no v2-path work.

### Spec 4 — Resource Discovery and Evolution Specification

- **Purpose:** one governed lifecycle from registration to runtime availability (§6).
- **Existing components reused:** entity hooks, `catalog_indexer`, `catalog_triggers`, EBPM inventory/evidence/proposals, `discover_and_recompute_coverage`, Review Center, lifespan watchdog pattern, lifecycle-hook loader.
- **Proposed interfaces:**
  - `app/services/enterprise/onboarding.py`: `run_post_index_pipeline(kb_id)` (technical→semantic→proposal stages, per-stage error isolation writing to a new `onboarding_runs` status table — one small migration) fired from the two verified entity-hook sites.
  - Delete-path hook: `handle_resource_deleted(record)` in `entities.py` delete branch → cleanup service (catalog rows soft-delete, bindings `orphaned`, EBPM inventory mark, evidence retention).
  - `run_drift_watchdog()` lifespan loop (signature hash over `information_schema` per connected KB; reindex on drift — reuses existing reindex path).
  - `context_version` bump on: index completion, review decision, deletion.
- **Dependencies:** Specs 1-2 (version consumption); none blocking.
- **Security:** pipeline runs under system actor with org scope; proposals only — no runtime data access granted by onboarding; deletions audited.
- **Failure handling:** per-stage isolation (EBPM precedent: `skipped[]`), `onboarding_runs` records stage failures; watchdog never blocks request paths.
- **Acceptance criteria:** (1) new DB KB → within pipeline run: catalog rows + inventory row + evidence + (if LLM enabled) proposals + review-queue entries; (2) KB delete → no orphaned live bindings (test asserts zero live `backing_ref` refs to deleted KB); (3) schema change detected by watchdog within one cycle; (4) all stages idempotent (double-run = no-op deltas).
- **Boundaries:** no new discovery algorithms; no event bus; no changes to manual admin triggers (they stay).

### Spec 5 — Enterprise User Experience Specification

- **Purpose:** business-user-facing Enterprise experience; ontology administration demoted to a separate advanced area.
- **Existing components reused:** `EnterpriseProfile.jsx` tab framework (M/A-G/R), `EnterpriseMap.jsx`, `ProcessFlowCanvas.jsx`, `ReviewCenter.jsx`, `BusinessProcesses` catalogue + drawer, `OntologyGraph.jsx`, EvidenceBadge/DISCOVERY_STATUS_STYLE vocabulary.
- **Proposed IA (navigation redesign of the same page):**
  - **Business Overview** ← today's `EnterpriseMap` (landing, ✅ exists) + Executive KPI strip.
  - **How We Work** ← EBPM catalogue + flow canvas (exists as tabs F; becomes the primary process surface).
  - **Business Knowledge** ← ontology objects/links/metrics/terminology (today's Object Explorer + OntologyGraph) with plain-language labels ("business terms & data").
  - **Resources** ← inventory + onboarding status (`onboarding_runs` surfaced: indexed/annotated/proposed/needs-review badges).
  - **AI Workforce** ← agent directory with department grouping (reuse `list_available_agents` enrichment) + delegation/observation health.
  - **Administration & Governance** ← admin-gated: Review Center (R), Health (G), ontology studio, decisions/outcomes (D/E), access policies.
- **Drill path:** Enterprise → Department (OrgUnit) → Capability → Process → Activity → Business Object → Resource → Evidence — every hop already has a data source in EBPM APIs; missing hops are links, not new pages.
- **Dependencies:** Specs 1-4 data contracts; `isAdmin` gating pattern (exists).
- **Security:** admin tabs server-gated already; business user sees proposals as "awaiting review" chips; evidence quotes visible per doc-access rules.
- **Failure handling:** every tab keeps its existing honest-empty states.
- **Acceptance criteria:** (1) non-admin user completes the drill path from landing to a verbatim evidence quote in ≤5 clicks; (2) no admin-only route reachable without `require_org_admin`; (3) existing EBPM tests + build stay green.
- **Boundaries:** no rewrite of OntologyGraph/flow canvas; reorganization + copy changes + routing only.

---

## PART III — PRODUCT EXPERIENCE (summary)

See Spec 5. Core principle: **users navigate their business, not the ontology admin UI.** Landing = the Enterprise Map (already the default tab 'M'); "How We Work" makes processes first-class; knowledge and resources are plain-language; governance is one admin destination. The natural language of the UI is verified/proposed/observed + department/process/object — never "backing_ref" or "embedding".

## PART IV — CRITICAL ARCHITECTURAL DECISIONS

| # | Decision | Recommendation 🔶 | Reasoning | Alternatives rejected |
|---|---|---|---|---|
| 1 | Project semantics | Work context; security stays org+bindings+policies; ownership-validated references | Matches verified model; projects already container resources without being principals | Dept-as-project (conflates work and org), workspace entity (redundant with org) |
| 2 | Department authority | EBPM `OrgUnit` authoritative; `department` → FK link, soft migration | OrgUnit is evidence-backed and reviewable; label is not | Hard cutover (breaks existing data), status quo (two truths) |
| 3 | Shared vs per-agent context | **Shared org-level ontology + project/agent-filtered BCS views** | Copying per agent recreates the duplication problem; filters reuse policy stack | Per-agent ontology copies; full shared dump (token/security) |
| 4 | Delegation permission inheritance | **Ceiling model** (child ⊆ caller effective), explicit audited elevation for specialists | Removes silent widening; preserves specialist power via explicit path | Status quo widening (leak vector); strict inherit-all (breaks specialists) |
| 5 | `auto_bind_all_datasources` | Deprecate: keep flag (default OFF) through transition; BCS + explicit bindings replace it; remove in cleanup phase | Deliberate widening is incompatible with evidence-scoped context | Immediate removal (breaks current users), keep forever (ambient access) |
| 6 | AI proposal visibility | **Labeled, not hidden** for agents and business users; verified-only steers SQL/actions | Transparency builds trust; hiding creates parallel truth; SQL steering from proposals = fabrication risk | Hide proposals (loss of review signal), full trust (fabrication) |
| 7 | Cross-DB resolution | Application-layer joins via ontology links over per-object org-checked reads | Safe, policy-preserving, no new query engine | Federated SQL (policy/RLS complexity, risk) |
| 8 | Terminology/ontology consolidation | `Terminology` = vocabulary home; add term→object link; MetricDefinition keeps computation | Each store already wins at its job; links are cheap | Merging tables (migration risk, breaks NL2SQL) |
| 9 | Evidence/provenance | Canonical envelope serializer (shared module), three producers one contract | No new store; unblocks unified citations | Single evidence mega-table (lossy, disruptive) |
| 10 | Process execution observation | Linkage columns + heuristic matcher + `observed_confidence`; never auto-promote; surfaced as badges + review candidates | Honest; reuses review center | Auto-verification from logs (fabrication), full process mining engine (out of scope) |

**Unresolved (❓):** exact token budget per deployment tier; whether department-filtered context should hide cross-department verified processes from prompts (visibility UX); migration 114 application strategy for shared stacks; whether `BusinessRule` authoring UI ships with Spec 1 or follows adoption.

---

## Verification appendix

All ✅ claims trace to: `app/models/{base,project,project_knowledge_base,project_agent,agent_data_binding,project_memory,enterprise_process,ontology,identity,governance,outcome,agent_run,agent_run_step,workflow_run,automation_execution,nl2sql_query_log,audit_event,llm_model,mcp_server,agent_app}.py`; `app/services/{agent_prompts,data_source_runtime/data_source_runtime,agent_tools,tool_handlers/{delegate_tool,delegation_tools,db_tools,ontology_tools,erp_submit_tool},ontology/{data,discovery,enterprise_discovery,permissions,fanout,escalation},db/{semantic_store,table_retrieval,retrieval_evidence,result_contract,answer_grounding,business_rule_extractor},knowledge_graph/{catalog_triggers,catalog_indexer},document_ingestion/*,harness/run_service,workflow_runtime,llm_router,access_policy_service}.py`; `app/routers/{entities,agents,ontology,knowledge_bases,mcp,llm,access_policies,project_catalog}.py`; `main.py`; `frontend/src/pages/{MySpace,EnterpriseProfile}.jsx`, `frontend/src/components/enterprise/*`, `frontend/src/components/ontology/OntologyGraph.jsx`.

---

## Implementation Log

### Phase 0A — Secure Project Ownership Validation (2026-11-01, DONE)

New shared helper `app/services/project_access.py`:
- `project_use_authorized(db, user, project)` — fail-closed check: same-org required; authorized = org admin (own org) | creator | `ResourceShare` grant with `access_level='use'` (verified semantics: 'use' = view + run agents; the reserved 'view' level does NOT authorize runtime execution) | platform-shipped `is_system` projects in-org. Share-lookup errors DENY (fail closed).
- `authorize_project_reference(db, user, project_id)` — live-project load; missing/soft-deleted → `None` (callers degrade gracefully, stale sessionStorage UX preserved); existing-but-unauthorized → **HTTP 404** (no existence leak).
- `resolve_authorized_message_project(db, user, conv, body_project_id, body_project_name)` — wraps the existing `resolve_message_project_id` precedence, then authorizes the RESOLVED reference (covers spoofed id AND spoofed name).

Wired at all three runtime entry points in `app/routers/agents.py`:
1. Conversation creation (`create_conversation`) — replaces the existence-only check; unauthorized → 404, stale → dropped exactly as before.
2. v2 message path (`_llm_pid_v2`).
3. v3 stream (`_llm_eff_pid`) — the path that feeds `prepare_data_source_runtime` / `resolve_effective_llm`.

Note: an in-flight conversation whose share is revoked now fails closed on the next message (documented behavior; the conversation creator retains access as creator).

Tests: `tests/test_project_access_authorization.py` (9) — creator/admin/'use'-share authorized, unauthorized same-org 404, cross-org 404 even for a foreign admin, missing/deleted degrade, 'view' share insufficient, system projects in-org runnable, message resolver 404 on id AND name spoofing with stale-id fallback, and the creation endpoint enforcing the same wall. Focused regression: 49 passed incl. the pre-existing `test_global_chat_project_resolution.py` (legacy resolution behavior preserved; `llm_router.py` itself untouched).

### Phase 0B — Resource Deletion & Orphan Cleanup (2026-11-01, DONE)

New `app/services/enterprise/cleanup.py` — `handle_resource_deleted(db, resource_type, resource_id, org_id, actor_id)`:
- Supported types: `knowledge_base`, `mcp_server`, `automation_task`, `datasource` (the last has NO deletion endpoint today — documented limitation; supported for programmatic callers).
- **Catalog** (database KBs): soft-deletes `KBTableMeta`/`KBColumnMeta`/`KBTableRelation` — history preserved, resource never queryable as live.
- **Agent bindings:** soft-deletes `AgentDataBinding` rows referencing the resource; surgically removes the id from `AgentApp.knowledge_bases` JSON arrays (unrelated ids survive; other-org apps untouched).
- **Ontology:** objects with `backing_kind='table'` and `backing_ref='kb:<id>:…'` → `binding_status='orphaned'` + backing_ref CLEARED (cannot resolve to live data) with the prior ref preserved in `provenance_json.orphaned_backings` + audit. Objects, links, review decisions untouched; nothing deleted.
- **EBPM:** matching `EnterpriseResource` inventory rows soft-deleted with `status_json.deleted` markers; `ProcessEvidence` never modified.
- **Audit:** one `resource_deleted` AuditEvent with per-component counts (no resource contents).
- Org isolation fail-closed: resource row must exist in `org_id` (`LookupError`), cross-org → `PermissionError`; both leave zero writes. Idempotent (second run = zero-effect counts). Caller-commit convention; failure → rollback + re-raise, callers surface `cleanup_error` instead of claiming clean success.

Wired into BOTH real deletion paths (post-authorization): generic entity DELETE (`entities.py`, hook map `_ENTITY_RESOURCE_TYPES`) and the dedicated MCP disconnect (`mcp.py`); response contracts extended additively (`cleanup` key). Double-cleanup across the two MCP paths is safe by idempotency.

Unsupported/limitations: `Datasource` has no deletion endpoint (cleanup supported programmatically only); `EnterpriseResource` for `agent`/`ontology`/`action_interface` kinds cleans only when their ref_id matches the deleted resource id; agent-side MCP grants (`granted_agents`) are handled by the pre-existing MCP unregister flow, not the cleanup service.

Tests: `tests/test_enterprise_cleanup.py` (8) — catalog/binding/ontology/EBPM behavior, unrelated-binding survival, other-org immunity, object+link survival with verified status preserved, evidence survival, cross-org rejection with zero writes, idempotency, missing-records safety, type coverage. Regression: 55 passed across entity tenant-scoping/router/master-filter/backing suites.

### Phase 1A — BusinessRule Unified Model (2026-11-01, DONE)

New `app/models/business_rule.py` — `BusinessRule` (table `business_rules`), the ONE authoritative, evidence-backed home for enterprise business rules:

- Fields (TimestampedBase conventions — `org_id`/`app_id`/`created_by_id`/`is_deleted`/`created_date`/`updated_date`): `scope` (`org|domain|object|process`, default `org`), `scope_ref_id` (nullable; required by constraint when scope ≠ `org`), `statement` (Text, NOT NULL), `source_evidence_ids` (JSON list of EBPM `ProcessEvidence` ids — traceability only, evidence rows never modified), `status` (`proposed|verified|rejected|enabled`, default `proposed`), `applies_to` (`sql|action|process`, default `sql`), `review_history` (JSON, append-only via `record_review()`).
- Governance: every rule is born `proposed`; NO auto-enabling; only `enabled` rules may influence SQL/actions (consumed by callers, enforced procedurally — the DB stores the status vocabulary; `verified → enabled` is the review transition). Review decisions append to `review_history` with ISO timestamps; callers persist the AuditEvent.
- Constraints: 4 CheckConstraints (scope/status/applies_to vocabularies + `ck_business_rule_scoped_needs_ref` — scoped rules must name a target) and indexes `ix_business_rules_org_status` / `ix_business_rules_org_scope` (plus `status` and `scope_ref_id` single-column indexes).
- Relationship to existing machinery (no duplication): `SemanticGuidelines` remain the NL2SQL prompt representation; a Phase 1B+ bridge maps enabled `applies_to='sql'` rules onto them. `ForecastBusinessRule` is an unrelated forecasting concept (different table).

Migration `alembic/versions/119_business_rule.py` — additive `create_table` chained on the worktree head `118_kb_multi_project`, explicit-column style with `server_default`s, tenant RLS policy (`tenant_isolation`, same as 118), no backfill (empty table), reversible downgrade.

Tests: `tests/test_business_rule_model.py` (18 passed) — proposed-by-default creation, org isolation, all valid enum values accepted, parametrized rejection of invalid scope/status/applies_to, scoped-rule-without-target rejection, enabled/verified/rejected storability, evidence-id preservation across expiry, append-not-replace review history, constraint/index definitions match the migration. Regression: `tests/test_enterprise_cleanup.py` 8 passed; `app.models` package imports clean.

Boundaries respected (NOT implemented in 1A): no Business Context Service, no runtime/NL2SQL consumption, no SemanticGuidelines backfill, no Terminology/Project changes, no frontend, no new discovery engine, no review API.

Phase 1B handoff: (1) bridge enabled `applies_to='sql'` BusinessRules → `SemanticGuidelines` (backfill is a bridge only, keep BusinessRule authoritative); (2) review-center API (list/proposed-queue + confirm/reject/enable with `record_review` + AuditEvent); (3) Business Context Service reads rules by `(org, scope, scope_ref_id)` for prompt/process/action consumption; (4) rule extraction from existing evidence stays out of scope until the review loop exists.

### Phase 1B.1 — BusinessRule Governance & Review Lifecycle (2026-11-01, DONE)

New `app/services/ontology/business_rules.py` — the procedural transition contract (the DB stores the status vocabulary; the lifecycle is enforced here): `confirm` proposed→verified, `reject` proposed→rejected, `enable` verified→enabled, `disable` enabled→verified, `resubmit` rejected→proposed. **`proposed → enabled` is never allowed** (409). Per decision: appends to the rule's `review_history` via `record_review()` (append-only; invalid attempts never append) + one `AuditEvent` (`business_rule_{confirmed|rejected|enabled|disabled|resubmitted}`, `event_source="ontology"`, from/to status + reason in `detail_json`). Org-scoped fail-closed loads (missing/cross-org → None → 404, zero writes). Defensive shape validation (422): scope/applies_to vocabulary + scoped-rule-must-name-target + non-empty statement. Evidence ids are read-only inputs — never touched by review.

Endpoints on the existing enterprise ontology router (mounted `/api`), reusing `require_org_admin` (403 for non-admin) / `_org_or_404` / the `page`/`page_size` pagination convention:
- `GET /ontology/enterprise/business-rules` — status / scope / applies_to filters, pagination, evidence ids + review history per item (any org member).
- `GET /ontology/enterprise/business-rules/{id}` — detail; 404 cross-org.
- `POST .../confirm | reject | enable | disable | resubmit` — org-admin only; reject/enable/disable/resubmit take optional `{reason}`.

Route-ordering note: the block sits BEFORE the generic `/ontology/enterprise/{kind}/{entity_id}/confirm|reject` routes — otherwise the generic route captures `kind="business-rules"` and shadows the specific lifecycle endpoints (caught by the HTTP-level tests).

"Do not let rediscovery overwrite human decisions": review only ever moves status forward through the allowed map; a rule with any human decision can never silently return to `proposed` except via explicit resubmit, and future discovery integration (1B.2+) must treat `review_history`-bearing rows as decided (to be enforced in the discovery bridge).

Tests: `tests/test_business_rule_lifecycle.py` (13, HTTP-level with ephemeral SQLite + dependency overrides) — cross-org 404 reads+writes with zero side-effects, viewer 403 on all five write ops (reads open), all five valid transitions, proposed→enable blocked (409), rejected cannot be enabled (409; only path back is resubmit), append-only history across repeated reviews with invalid attempts non-appending, AuditEvent per decision (type/org/actor/from→to/reason), list filters + pagination + evidence/history visibility, missing rule 404. Plus `test_business_rule_model.py` 18. Regression: 51 passed across ontology core/governance/P3/escalations suites.

Phase 1B.2 handoff: (1) discovery-side rule proposal must create rows ONLY as `proposed` and must skip rows already carrying a `review_history` decision (no overwrite); (2) `review-queue` aggregation can union proposed BusinessRules alongside EBPM proposals; (3) BCS (1B.3) consumes ONLY `status='enabled'` rules by `(org, scope, scope_ref_id)`; (4) SemanticGuidelines bridge reads enabled `applies_to='sql'` rules.

### Phase 1B.2 — Semantic Integration (2026-11-01, DONE)

**BusinessRule → SemanticGuidelines bridge** (`app/services/ontology/business_rule_bridge.py`) — BusinessRule stays AUTHORITATIVE; SemanticGuidelines remains the compatibility layer consumed by `SemanticStore.guidelines()` (no second SQL rule engine; the existing BUSINESS RULES prompt block is untouched):

- Eligibility: ONLY `status='enabled'` AND `applies_to='sql'`. Proposed / rejected / merely verified rules are never materialized, and any state change retracts previously derived rows (lifecycle hook `maybe_sync_on_review` runs inside the review transaction, fail-open).
- Derived rows are marked `[business-rule:{rule_id}]` in the title; sync manages ONLY marker rows — manual SemanticGuidelines are untouched, and the full sync sweeps stale markers whose rule no longer exists.
- Idempotent: re-sync produces zero created/updated/removed when state already matches (rows updated in place, never duplicated).
- Provenance preserved: derived content = statement + footer `[source: business_rule:{id} | scope: {scope}:{ref} | applies_to | evidence: {ids}]`; org/scope carried on the row.
- Targeting: new nullable `business_rules.datasource_ids` (JSON) — listed KBs, or org-wide (every non-deleted KB enumerated at sync time; new KBs picked up by the next sync — documented limitation).
- `POST /api/ontology/enterprise/business-rules/sync-guidelines` (org-admin, audited `business_rule_guidelines_synced`) for backfills/drift repair; lifecycle hooks keep everyday flow in sync automatically.

**Terminology → Ontology link** — new nullable `terminologies.ontology_object_id` (indexed; NO hard FK — org consistency validated at link time, unlinked rows fully functional, existing rows preserved). Router endpoints on the existing terminologies API: `POST/GET/DELETE /api/datasources/{ds}/terminologies/{id}/ontology-link` — link validates org consistency fail-closed (missing AND cross-org → 404), never auto-invents links; GET inspects the linked object (id/name/domain/backing_kind); DELETE unlinks; serialization exposes `ontology_object_id`.

Migration `120_semantic_integration.py` (on `119_business_rule`): two nullable columns + one index — purely additive, no backfill, no behavior change for existing rows.

Tests: `tests/test_semantic_integration.py` (10) — enabled SQL materialization (incl. per-datasource targeting + provenance footer), proposed/rejected/verified/non-SQL exclusion, idempotency (zero-churn second sync), disable retracting retrieval influence (and re-enable re-materializing), reject-hook stale-row cleanup, manual-guideline preservation, terminology link/unlink/inspect, cross-org link 404, existing terminology upsert/list/search behavior preserved. Regression: 41 across the three BusinessRule suites + 53 across semantic-store/prompt-injection/terminology/OCI suites (2 `test_nl2sql_oci_p2.py` failures verified pre-existing on stashed HEAD — unrelated).

Phase 1B.3 handoff: BCS consumes ONLY enabled rules via `(org, scope, scope_ref_id)` (+ optional `datasource_ids` pre-filter); the bridge can be reused as the write-path template for other compatibility layers (action pre-checks, process reasoning); org-wide targeting enumerates KBs at sync time — if live KB churn becomes a problem, move targeting into the store read path.

### Phase 1B.3 — Project ↔ OrgUnit Integration (2026-11-01, DONE)

Projects (My Space work contexts) can now reference the authoritative enterprise organizational model without changing any security semantics:

- **Model:** nullable `projects.department_org_unit_id` (indexed, NO hard FK — same convention as the terminology link). Migration `121_project_org_unit.py` on `120_semantic_integration`: one column + one index, no backfill, existing projects untouched.
- **Linking (generic entity API only — no duplicate project APIs):** `entity_service.create_record` / `update_record` / `bulk_create_records` / `bulk_update_records` / `update_many_records` validate an explicit `department_org_unit_id` payload field when the model carries the column: fail-closed load (missing AND cross-org → 404), only `unit_type ∈ {department, team}` accepted (`role` units are performers, not containers → 400). Raised as `OrgUnitLinkError(HTTPException)` so the generic router needs zero special-casing. Never auto-assigned; `None` unlinks.
- **Legacy `department` label stays authoritative for discovery-routing** and is backward compatible: an explicit human link back-fills the label from the OrgUnit name ONLY when the payload doesn't carry its own `department` value; unlinking never erases the label.
- **Reads:** `get_record` / `list_records` / filter annotate Project dicts having a link with `department_org_unit = {id, name, unit_type, hierarchy}` — hierarchy is the name-based `parent_name` chain walked org-locally with a cycle guard and 10-level bound (OrgUnit hierarchy is name-based by design).
- **Invariants held:** Project remains a work context, NOT a security principal — the link is metadata only; ownership / ResourceShare / `project_use_authorized` untouched (verified by test: a department-linked project grants nothing to same-org non-owners or foreign admins; explicit 'use' share still grants). No datasource/KB/agent permission expands. No automatic department discovery.

Tests: `tests/test_project_org_unit.py` (6) — create-time link + label back-fill (payload label wins), get/list hierarchy annotation, cross-org/missing 404 + role-unit 400 with zero writes, unlinked-project functionality + explicit unlink preserving the label, unrelated-field preservation, authorization unchanged (creator ok / colleague denied / foreign admin denied / 'use' share grants). Regression: `test_project_access_authorization.py` 9 + entity tenant-scoping / master-filter / list-fields / backing / router-sync suites 51.

Phase 2 handoff (roadmap Phase 2 — Business Context Service): BCS can now join Project → OrgUnit → (department-scoped context); the link is display/metadata only until BCS defines explicit consumption rules; Terminology.ontology_object_id (1B.2) follows the same validated-link pattern.

### Phase 2A — Business Context Service Foundation (2026-11-01, DONE)

New `app/services/enterprise/business_context.py` — the reusable runtime bridge between enterprise knowledge and AI agents. **Foundation only: NOT yet wired into `get_system_prompt` / `prepare_data_source_runtime` (Phase 2B).**

- **`build_business_context(db, user, org_id, *, project_id=None, agent_id=None, query=None, budget_tokens=2000)`** assembles: organization; authorized project context (via the EXISTING `project_access.authorize_project_reference` wall — raises-404 caught → honest `unavailable_reason`, never guessed); department context from the 1B.3 OrgUnit link (explicitly labeled context-only, never an access grant); verified-first relevant processes + activities + verbatim evidence refs; ontology objects + verified relationships (backing_ref deliberately NOT exposed — runtime resolvers own that mapping); enabled terminology; BusinessRules in three explicit tiers (enabled = executable, verified = informational-only, proposed = capped at 2 and labeled hypotheses; rejected NEVER included); governed metrics (names/semantics only — no SQL); authorized resource references (the project's own KBs, ids/names only, no credentials).
- **Ranking + budget:** token-overlap relevance vs. the query with project/department-name boost, then authority order (enabled rules → verified processes → ontology → terminology/metrics → informational rules → proposed), then a hard token budget using the EXISTING `compaction.token_estimator.estimate_text_tokens` (tiktoken when available). Payload sections carry ONLY what survived the budget; exposes `estimated_tokens`, `budget_tokens`, `included_items`, `omitted_items` (+ per-section), `evidence_coverage`, `status` (complete | partial | unavailable), `warnings`. Every section fails open individually (`section_unavailable:<name>` warning + honest empty section).
- **`render_prompt_block(context)`** renders explicitly separated tiers: Verified facts / Discovered-but-unverified processes (labeled proposals) / Enabled rules (executable) / Verified-but-NOT-enabled rules (do not act) / Proposed (UNVERIFIED — hypotheses, never facts) / Unavailable-truncated gaps. Hypotheses are never converted into facts; cross-org and unauthorized content never appear anywhere in the output.
- **Caching: intentionally none** — no safe existing cross-request pattern to reuse and the build is a bounded set of org-scoped queries; revisiting with the semantic-cache pattern is a 2B decision.

Invariants held: org isolation on every query; project authorization only via `project_access.py`; `ResourceAccessPolicy` remains authoritative; department is contextual metadata; proposed/rejected rules cannot influence execution; verified-but-disabled rules are informational; missing knowledge is explicit.

Tests: `tests/test_business_context.py` (9) — honest empty context, relevance-ranked verified processes with activities+evidence, enabled/proposed/rejected rule tiers in both payload and renderer, authorized project + department + resource refs, cross-org isolation (processes/rules/terms/project), unauthorized project exclusion, budget truncation (estimated ≤ budget, omitted counted, partial status), provenance preservation (process evidence refs + rule evidence ids), section-failure → warning + honest empty section. Regression: 65 across the enterprise ring (cleanup / rule lifecycle / rule model / project-access / project-org-unit / semantic-integration).

Phase 2B handoff: wire `build_business_context` into the two verified choke points (`get_system_prompt` block injection, `prepare_data_source_runtime` extras) behind a flag (BCS_ENABLED), budget from prompt-size config, and decide on caching; renderer output is the prompt block contract.

### Phase 2B — Business Context Runtime Integration (2026-11-01, DONE)

BCS is now available to the existing agent runtime behind **`BCS_ENABLED` (default False)** + `BCS_BUDGET_TOKENS` (default 2000) in `app/config.py`. When disabled: zero BCS queries, zero added latency, byte-identical prompts.

- **Build-once-per-request helper** `agent_prompts.build_bcs_block(db, user, org_id, project_id=None, query=None, budget_tokens=None)` → `(rendered_block, structured_context)`; flag-gated + fail-open (any failure → `(None, None)`, ordinary chat continues). Authorization enforced INSIDE the BCS via the existing `project_access` wall.
- **Choke point A — system prompt:** `get_system_prompt(..., bcs_block=None)` (additive kwarg) appends a `<enterprise_business_context>…</enterprise_business_context>` delimited block when one is passed. Wired at the v2 and v3 chat call sites in `agents.py` (built once with the conv's org + effective project + `user_content`, following the `_append_department_block` precedent). Subagents / red-team / every other existing caller pass no block → prompt byte-identical, authorization surfaces untouched.
- **Choke point B — data-source runtime:** `prepare_data_source_runtime(..., user=None, bcs_context=None)` thin wrapper (impl renamed `_prepare_data_source_runtime_impl`) attaches a COMPACT structured payload to `ctx_extras["business_context"]` (enabled rule statements, department, project_authorized, warnings) — metadata only, never an authorization grant; KB/RLS/policy scoping untouched. v2 passes the prebuilt context; v3 attaches it to `data_ctx_extras` after the pre-flight (same single build). Duplicate construction avoided: one `build_bcs_block` per request feeds both choke points.
- **Graceful degradation:** missing/cross-org project → `unavailable_reason` in the block, empty resources; BCS failure → no block, prompt unchanged; per-section failures → `section_unavailable:*` warnings. Proposed rules render ONLY under the UNVERIFIED/hypotheses header; verified-but-not-enabled under "do not act on them"; enabled under "executable guidance".

All four edited runtime files (`config.py`, `agent_prompts.py`, `data_source_runtime.py`, `agents.py`) were foreign-dirty — staged surgically (HEAD content + only the listed edits, py_compile-verified); foreign WIP untouched in the worktree.

Tests: `tests/test_bcs_runtime_integration.py` (9) — flag off → zero BCS invocation + unchanged prompt; flag on → delimited injection at the system prompt and structured extras via the runtime wrapper; missing project graceful; cross-org project denied (no name/KB leak); rule tiers in the rendered block; BCS failure fail-open; subagent path receives nothing + payload carries no grants; budget bounded (`estimated ≤ budget`, wrapper extras + flag-off extras unchanged). Regression: 17 across BCS foundation + 64 across data-source-runtime/prompt-builder/multiagent/global-project-resolution suites (2 `test_data_source_runtime_driver_error.py` failures verified pre-existing on stashed HEAD).

Phase 3 handoff: BCS runtime path is live behind the flag — next phases (per roadmap) are the evidence/provenance unification envelope, BCS consumption by governed actions (enabled `applies_to='action'` rules as pre-checks), and observed-execution linkage; NL2SQL/planning/delegation architecture remain untouched by design.

### Phase 3A — Ontology-Aware NL2SQL Planning (2026-11-01, DONE)

The NL2SQL table-candidate ranking now consumes enterprise ontology semantics, behind **`NL2SQL_ONTOLOGY_ENABLED` (default False)** in `app/config.py` (rollback: set False).

- **Planning-context builder** `business_context.build_ontology_planning_context(db, org_id, query)` — org-scoped internal data (NOT a prompt block): VERIFIED ontology objects first with their backing TABLE parsed from `backing_ref` (`kb:<id>:<table>`, `backing_kind='table'` only), then proposed objects explicitly labeled `hypothesis` (rejected/orphaned rows never included); links between the selected objects (verified = facts, proposed/inferred = labeled hypotheses); enabled terminology ranked by query relevance; and ONLY `status='enabled'` + `applies_to='sql'` BusinessRules (with optional `datasource_ids` targeting preserved).
- **New ranking component in `table_retrieval.retrieve_for_question`** (additive `ontology_context` param + `ontology_provenance` field on `RetrievalResult`): verified object → backing-table boost (1.25, breakdown `ontology`); proposed object → 0.25 labeled `ontology-hypothesis`; verified relationship → 0.75 partner hint (`ontology-link`) for the not-yet-scored side of a relationship when the other side is already a candidate — explicitly evidenced as "candidate hint, not a join"; proposed links are provenance-only, NEVER scored; terminology alias → 1.0 (`terminology`) for catalog tables whose description shares ≥2 tokens with the matched term. All boosts flow through the existing `_add` gate — a table outside the authorized catalog can NEVER be introduced by ontology evidence. Provenance (selected objects/links/terms/hypotheses/enabled rules) rides on `RetrievalResult.ontology_provenance` for downstream audit.
- **Caller wiring** (`nl_answer_service` retrieval call site): flag-gated, fail-open, org derived from the authorized KB row — no default datasource, no first-database selection; multi-KB requests keep every authorized source dynamically selectable (ontology only boosts, never excludes).

Invariants held: no invented joins (ontology links never produce join conditions — only the authorized schema catalog does); no second SQL rule engine (enabled rules ride the SAME planning-context path; the 1B.2 SemanticGuidelines bridge remains the prompt layer); proposed/rejected content cannot guide SQL; cross-org ontology data cannot influence ranking; flag-off behavior is byte-identical (`ontology_provenance is None`, no new breakdown components).

All three edited NL2SQL-side files (`config.py`, `table_retrieval.py`, `nl_answer_service.py`) were foreign-dirty — staged surgically (HEAD + only the listed edits, compile-verified).

Tests: `tests/test_nl2sql_ontology_planning.py` (9) — verified object boosts its backing table with provenance; terminology alias improves retrieval; verified relationship hints the partner (labeled "not a join"); proposed object/link stay hypotheses with zero fact-score; enabled-SQL-rules-only (verified-not-enabled/proposed/non-SQL excluded; datasource targeting preserved); cross-org isolation; flag-off identical behavior + default-off flag; multiple datasources remain selectable. Regression: 256 across `test_semantic_store` / `test_query_contract` / `tests/nl2sql` + 25 across the BCS suites.

Phase 3B recommendation: persist `ontology_provenance` into `Nl2sqlQueryLog.lineage_json` (the audit trail already carries the structure), extend ranking with rule-scoped datasource filtering (rules currently ride as provenance, not filters), and consider a metric-definition component using the same builder — all small, flag-gated follow-ups on the same seam.

### Phase 3B — Complete Ontology-Aware NL2SQL Integration (2026-11-01, DONE)

Completes the three Phase 3A gaps, all on the same flag (`NL2SQL_ONTOLOGY_ENABLED`, default False) — no new flags, no migrations.

- **Task A — provenance persistence:** `nl_answer_service._shadow_validate_and_log` gains an additive `ontology_provenance` kwarg; when present it is attached as `lineage_json["ontology"]` on the SAME telemetry record (`Nl2sqlQueryLog.lineage_json`). Legacy keys (`tables/columns/join_tables/filter_columns/dialect`) are untouched and historical rows stay readable (no-provenance calls write byte-identical rows; NULL lineage + provenance → `{"ontology": …}`, never an empty dict faking "no lineage"). Both log call sites (success + failure paths) pass `_ont_provenance`, captured right after candidate retrieval — the static-route path (no retrieval) honestly logs no ontology section. Every provenance entry now carries an honest `influenced` flag (objects/links/terms/metrics: true ONLY when a boost actually passed the authorized-catalog `_add` gate; rules: always false — planning guidance, never a scoring component), so the log can never claim an ontology item influenced a query that it did not.
- **Task B — datasource-scoped rules:** `build_ontology_planning_context(..., datasource_id=)` — enabled SQL rules with `datasource_ids` apply ONLY to their listed datasources; NULL/empty = org-wide (existing scope semantics unchanged). Scoping only NARROWS (org filter is applied first) so it can never expose an unauthorized datasource; no-datasource calls keep exact Phase 3A union behavior (backward compatible with the 1B.2 SemanticGuidelines sync, which remains the prompt layer).
- **Task C — MetricDefinition ranking:** the builder now emits a `metrics` section — org-scoped, enabled, `lifecycle='published'` MetricDefinitions with name/display_name/description/synonyms + `base_table` + `evidence_status` ONLY (`base_sql`/`filters_json` deliberately stay out — deterministic metric computation is never bypassed and the LLM can never execute a metric). `table_retrieval` adds an `ontology-metric` component (weight 1.5, matching the existing verified-metric precedent): a question-relevant VERIFIED metric boosts its authorized base_table; unmapped metrics (no `base_table`, or mapped table outside the authorized catalog) and unverified metrics are recorded in provenance with `influenced: false` (+ `unmapped: true` where applicable) — honest degradation, zero fabricated boosts. A Phase 3A-shaped context without a `metrics` key keeps working unchanged.

Edited files: `business_context.py` + `table_retrieval.py` + `nl_answer_service.py`. `table_retrieval.py` and `nl_answer_service.py` remain foreign-dirty — staged surgically (HEAD + only the listed edits, compile-verified; HEAD lacks the foreign `classification_facts` plumbing, so the staged retrieval call site anchors on HEAD structure).

Tests: `tests/test_nl2sql_ontology_phase3b.py` (8) — provenance persisted with legacy lineage keys preserved; historical compatibility (no-prov rows byte-identical, ontology-only record on NULL lineage, NULL stays NULL); datasource-scoped rule filtering (org-wide union preserved without a datasource); cross-org rules never leak through scoping; disabled/proposed/soft-deleted rules excluded even when scoped; metric-aware ranking (draft lifecycle excluded, no base_sql leakage, `ontology-metric` boost + influenced provenance); missing/unauthorized/unverified metric mappings degrade honestly; flag-off + Phase 3A-context backward compat. Regression: 264 across `test_semantic_store` / `test_query_contract` / `tests/nl2sql` / `test_bcs_runtime_integration` + the 8 Phase 3A tests.

Limitations: ontology provenance is logged only on the `nl_answer_service` live path (the legacy `services/nl2sql` writer is untouched by design); rule applicability is provenance-only (rules still ride the SemanticGuidelines prompt layer, not scoring); metric ranking uses base_table only — column-level metric mappings remain a Phase 3C candidate.

Phase 3C recommendation: unify the three provenance vocabularies (ontology provenance, data lineage, evidence refs) into the canonical provenance envelope from the target architecture — the `lineage_json["ontology"]` section is its first consumer; then governed-action pre-checks (enabled `applies_to='action'` rules) and observed-execution linkage.

### Phase 3C — Canonical Enterprise Provenance (2026-11-01, DONE)

One reusable provenance contract — `app/services/enterprise/provenance.py` (`prov-v1`) — unifying ontology provenance, SQL execution lineage, document evidence and business-process evidence. No migrations, no new flags.

- **Contract**: `canonical_entry(...)` builds a validated entry (closed `SOURCE_TYPES` set); `validate_canonical(...)` enforces honesty (unknown type / confidence outside [0,1] / non-boolean flags rejected; `supports_answer=True` REQUIRES `retrieved=True` AND an identifiable source — retrieval never implies support); `serialize_provenance(...)` wraps entries in `{"version": "prov-v1", "entries": [...]}`. Absent optional data is OMITTED — no invented confidence, ids or statuses. States stay DISTINCT: `retrieved` ≠ `influenced` ≠ `supports_answer` (tri-state; undetermined is absent from the dict).
- **Converters (pure functions — never touch the DB, so cross-org leakage is impossible; `org_id` stamped verbatim from the caller's already-authorized org)**: `from_ontology_provenance` (3B prov → entries; status from the BUCKET — objects=verified, hypotheses=proposed, never promoted — influence from the honest 3B flags); `from_sql_lineage` (P-SV2 lineage tables → `retrieved=True, influenced=True` — the one case where influence is a fact; no status invented); `from_document_proposal` (P4 DocumentProposal → status passthrough, proposed stays proposed); `from_process_evidence` (EBPM ProcessEvidence → confidence passthrough-or-omitted).
- **Adoption**: the NL2SQL telemetry row (`nl2sql_query_logs.lineage_json`) now carries `provenance` (canonical, ontology + SQL lineage unified) beside the raw 3B `ontology` section and legacy keys — additive, fail-open, and the section is omitted entirely when zero entries exist (never an empty envelope). Historical rows stay byte-identical.

Edited files: `provenance.py` (new) + `nl_answer_service.py` (foreign-dirty — staged surgically, HEAD + only the 3C edit, +17/−0; 3B pairs were already in HEAD) + the new test file.

Tests: `tests/test_canonical_provenance.py` (13) — canonical shape; missing-fields-stay-missing; legacy lineage conversion (original dict never mutated); lineage None/missing-keys → []; ontology conversion (bucket statuses, influence preserved, rules never influenced); partial/missing prov; document + process conversion (no promotion, no invented confidence); retrieved-vs-used-vs-supporting distinctness; supporting-evidence validation (4 rejection cases); cross-org stamping isolation; log row carries BOTH raw and canonical sections; historical rows without provenance unchanged. Regression: 57 (3C+3B+3A+BusinessRule+semantic-integration) + 264 (semantic_store/query_contract/nl2sql/BCS runtime) + 39 (answer/document grounding + enterprise process API) — all green.

Limitations: document/process converters are adopted only as contract functions (their emitting systems — document_grounding, EBPM process detail — are untouched this phase; wiring them is a small mechanical follow-up); the canonical section is written only on the `nl_answer_service` live path; `result_contract.py` / `answer_grounding.py` (both foreign-dirty) were NOT modified — figure-grounding verdicts remain a separate mechanism.

Phase 3D recommendation: mechanical adoption of `from_document_proposal` / `from_process_evidence` at their emitting systems (document review queue serialization, EBPM process detail API) so every evidence surface emits `prov-v1`; then governed-action pre-checks (enabled `applies_to='action'` rules as pre-checks) and observed-execution linkage (AgentRun/Nl2sqlQueryLog → process_id).

### Phase 3D.1 — prov-v1 Adoption at Document + Process Emitters (2026-11-01, DONE)

- **Document evidence**: `document_review._to_dict` (serving the review queue AND approve/reject/revise responses) now emits `canonical_provenance` (a validated prov-v1 entry) additively beside ALL 19 legacy fields — ids/chunk_key/quote preserved verbatim, `status` lifecycle stays authoritative in the legacy field, retrieved=True/influenced=False (review queue ≠ planning influence), no `supports_answer`.
- **Process evidence**: `enterprise_discovery.get_process` adds `canonical_provenance` per evidence item (built from the ORM rows, org-stamped from the already-org-scoped query); the detail-level `provenance` (AI-discovery provenance_json) and `review_history` are untouched — no key collision, review lifecycle never changed through serialization.
- **Contract fix found by adoption (test-first)**: `from_document_proposal` previously defaulted every proposal to "proposed" because `DocumentProposal` stores its lifecycle in `status` (not `evidence_status`) — the converter now honors both fields verbatim (approved stays approved, rejected stays rejected, superseded stays superseded). No schema change, no new format.

Edited files (all clean — plain staging): `provenance.py` (+9/−2), `document_review.py` (+7), `enterprise_discovery.py` (+29/−14 restructure of the evidence loop), new `tests/test_prov_v1_adoption.py`.

Tests: `test_prov_v1_adoption.py` (7) — document conversion/serialization; status passthrough across all four lifecycle states; process detail emission (refs/quote/confidence preserved, missing confidence omitted, retrieval never implies support); legacy key sets unchanged (19 doc fields + all detail keys); pre-seeded review_history intact + evidence_status untouched; cross-org fail-closed (`get_process` returns None); converter org stamping. Regression: 53 across `test_canonical_provenance` / `test_document_grounding` / `test_enterprise_process_api` / `test_enterprise_discovery` / `test_enterprise_acceptance` / `test_enterprise_cleanup` — green. No frontend changes → no build.

Limitations: process detail emits per-item canonical entries (not one envelope) because the detail-level `provenance` key is already occupied by AI-discovery provenance — `serialize_provenance` remains available for list surfaces; the NL2SQL log path (3C) remains the only envelope-level writer; OntologyObject/OntologyLink evidence (ontology connections in process detail) are not yet converted (they are references, not evidence rows).

Phase 3D.2 handoff (NOT implemented): governed-action pre-checks — enabled `applies_to='action'` BusinessRules as pre-checks on governed ontology actions (the same builder/scoping seam as Phase 3B Task B); then observed-execution linkage (AgentRun / Nl2sqlQueryLog → nullable process_id columns + heuristic matcher, never auto-promote).

### Phase 3D.2 — Governed Business-Action Pre-checks (2026-11-01, DONE)

Enabled `applies_to='action'` BusinessRules now enforce DETERMINISTICALLY at the ontology action execution boundary — the verified choke point `execute_action` / `execute_approved_action` in `services/ontology/data.py` (both share `check_action_permission`; the rule check runs BEFORE approval creation and tool execution, so a blocked action creates no approval request and fires no tool).

- **Retrieval** (`business_rules.applicable_action_rules`): org wall first, then scope match — `org` always; `object` via `action.object_id`; `domain` via one org-scoped object lookup; `process` scope never matches an action (no linkage — documented limitation). `datasource_ids`-scoped rules match ONLY when the caller's datasource (context/params) is listed — scoping only narrows. Only `status='enabled'` + `applies_to='action'` + not-deleted participate; proposed/verified/rejected NEVER enforce.
- **Deterministic decision** (`evaluate_action_rules`): statement MARKER protocol, case-insensitive, colon required — `PROHIBIT:`/`FORBIDDEN:`/`DENY:` → BLOCK; `REQUIRE_APPROVAL:`/`APPROVAL_REQUIRED:` → REQUIRE_APPROVAL; enabled rules WITHOUT a marker are recorded in `applicable_rule_ids` (traceable) but never gate — free text alone is not a demonstrable violation, and missing evidence is never converted into enforcement. The LLM never decides.
- **Outcomes**: BLOCK → `denied + blocked="business_rule"` (works on read AND write paths; a PROHIBIT rule VETOES even a valid governance approval — the approval flow the rule governs cannot override the rule). REQUIRE_APPROVAL → write actions proceed into the EXISTING approval creation (decision attached); read actions are gated through the SAME `ApprovalService` workflow (medium tier) and never auto-fire. ALLOW → executes with the decision attached (`rule_decision` key, additive on every return shape).
- **Audit**: `audit_action_rule_decision` (mirrors `audit_permission_denial` — independent fail-safe session) writes `business_rule_check` AuditEvents with decision + applicable/violated rule ids + approval_required + evaluation_status whenever any rule was applicable. Decision contract: `{decision, applicable_rule_ids, violated_rule_ids, approval_required, explanation, evidence[], evaluation_status: "deterministic"}`.
- **Preserved**: permission checks (write predicate, domain bindings, fail-closed), approval validity/matching/expiry/params-drift checks, ResourceAccessPolicy, SemanticGuidelines SQL behavior (this seam is `applies_to='action'` only), no new rule storage.

Edited files (all clean — plain staging): `business_rules.py` (+194), `data.py` (+83), new `tests/test_action_rule_precheck.py`.

Tests: `test_action_rule_precheck.py` (14) — prohibit blocks with zero tool calls; informational-rule allow with traceable ids; proposed/verified/rejected never block; cross-org never retrieved; datasource scoping both directions; REQUIRE_APPROVAL gates a read through the existing workflow; prohibit vetoes an APPROVED write; valid approval satisfies an approval rule; blocked writes create no ApprovalRequest; audit preserves decision+rule ids; no-rules compatibility; marker/scope protocol unit test (all violated ids recorded). Regression: 51 (BusinessRule lifecycle/model/escalations) + 20 (canonical provenance + prov adoption) + 38 (ontology core, P0 governance, governance gaps) — green.

Limitations: `process`-scoped action rules never match (no action→process linkage); marker protocol requires the explicit colon convention (plain-text prohibitions stay informational — by design, since free text cannot be deterministically enforced); decision quality is binary — no partial/conditional enforcement.

Phase 3E handoff (NOT implemented): observed-execution linkage — nullable `process_id`/`activity_seq` columns on AgentRun/Nl2sqlQueryLog + a heuristic matcher (agent binding / object / tool-name evidence), never auto-promoting to verified process steps; feeds EBPM observed-execution and the canonical provenance envelope's `execution_ref`.

### Phase 3E — Observed Execution Intelligence (2026-11-01, DONE)

Evidence-based linkage between ACTUAL execution records and persisted enterprise processes — deterministic, NO LLM.

- **Schema (migration `122_observation_linkage`, additive only)**: `nl2sql_query_logs` + `agent_runs` each gain nullable `process_id` (+index) / `activity_seq` / `match_confidence` / `match_method`. No hard FK (processes soft-delete); NULL = never matched — historical rows byte-identical and untouched. AgentRun has NO org_id column — its org lives in `caller_context` JSON and is verified per-record (cross-org runs never count).
- **Matcher** (`app/services/enterprise/observations.py`, deterministic confidence ladder; max signal wins, ties across DIFFERENT processes stay unmatched as "ambiguous" — never forced): explicit process/activity reference (1.0, org-verified) → VERIFIED ProcessEvidence referencing the executed ontology object (0.8) → tool bound to an ontology action whose object has verified process evidence (0.6) → executed SQL lineage tables in the process's verified table-catalog evidence (0.4). Insufficient evidence → honest `unmatched_reason`; proposed object evidence NEVER matches (verified-only); datasource alone never maps (no reliable link — never invented).
- **Lifecycle honesty (enforced by not writing)**: `record_observation` stamps ONLY linkage columns; `evidence_status`/`discovery_status` are NEVER touched (proposed stays proposed, verified stays verified); existing linkages are never overwritten (explicit wins forever, idempotent); no sequence edges fabricated from timestamps; no LLM-generated guesses.
- **API**: `GET /api/ontology/enterprise/processes/{id}/observations` (org-scoped, fail-closed 404) — execution count, activity-level counts, confidence distribution (high ≥0.8 / medium ≥0.5 / low), time range, source references with NO sensitive payloads (no question/sql/result); honest empty response; optional `record_type`+`record_id` inspect-one-execution decision (unmatched executions otherwise not enumerated — bounded query, stated in the response note).
- **Enterprise map integration**: `get_process` detail embeds an additive `observed_executions` summary (fail-open) shown SEPARATELY from documented/configured statuses.

Edited files: `observations.py` (new), migration 122 (new), `nl2sql_query_log.py`/`agent_run.py` (+4 columns each), `routers/ontology.py` (+25 endpoint), `enterprise_discovery.py` (+23 summary), two new test files. All clean — plain staging.

Tests: `test_observation_linkage.py` (16) + `test_observation_api.py` (4) — explicit process/activity linkage; verified-object matching (proposed evidence rejected); table-lineage matching; tool-binding chain; insufficient/ambiguous unmatched; cross-org isolation (matcher + summary + HTTP 404); historical rows preserved + idempotent never-overwrite; no status promotion either direction; empty history; confidence/evidence preservation; no payload leaks; no fabricated sequence edges; detail summary separate from statuses. Regression: 51 (3D.2 prechecks + BusinessRule suites + provenance suites) + 35 (enterprise process API/discovery/acceptance + prov adoption) — green.

Limitations: no automatic stamping at execution-ingestion time yet (writers in agents.py/nl_answer_service are foreign-dirty hot paths — `record_observation` is the integration point for Phase 4 onboarding hooks); AgentRun org attribution depends on `caller_context` JSON; `process`-scope signal and activity-sequence inference from execution order are deliberately absent (no fabrication).

Phase 4 handoff (NOT implemented): resource onboarding and continuous ontology evolution — connector registry, delete-path cleanup hooks feeding catalog freshness, execution-observation writers at ingestion time (calling `record_observation` from the agent-run/nl2sql log writers), drift-driven re-observation, and provenance-envelope `execution_ref` unification.

### Phase 4 — Enterprise Resource Onboarding & Continuous Ontology Evolution (2026-11-01, DONE)

Evidence-driven, idempotent onboarding of enterprise resources into the ontology — reusing the EXISTING discovery engine (`enterprise_discovery`), no second pipeline, no LLM in the lifecycle path.

- **Lifecycle** (`app/services/enterprise/onboarding.py`): `onboard_resource(db, org_id, resource_type, resource_id, *, actor_id, retry, llm_fn)` — (1) existence check (deleted resources are never "skipped_unchanged"); (2) digest short-circuit (sha256 of the sorted catalog table names → `inventory_digest`; unchanged digest ⇒ `skipped_unchanged`, no duplicate discovery); (3) per-step pipeline over the existing seams: `refresh_inventory` → catalog snapshot + `stale_references` (drift diff vs the previous run's stored catalog, `_MAX_CATALOG_NAMES=200`) → `extract_evidence` → `propose_operating_model` (llm_fn seam injectable, tests monkeypatch). Each step independently try/except'd — `partial_failed` records which steps failed and why; nothing is half-silent. Proposals stay PROPOSED; no auto-verification ever.
- **Capability honesty**: `datasource`/`knowledge_base` run inventory+evidence+discovery; `mcp_server`/`automation` run inventory only with an explicit "unsupported" skip step — never fake success. Unsupported types are rejected outright.
- **Runs model** (`enterprise_onboarding_runs`, migration `123_enterprise_onboarding_runs`, additive): org_id/resource_type/resource_id indexed, status ∈ `completed|partial_failed|failed|skipped_unchanged|resource_missing`, `steps_json` (per-step status/detail + catalog snapshot), `inventory_digest`, `attempt` (retry increments), `error`, `actor_id`. Every run audited (`resource_onboarded` AuditEvent, fail-safe).
- **Evolution/drift**: `on_resource_changed` re-runs onboarding on digest change; existing evidence is NEVER deleted — removed catalog tables are recorded as stale references in the run's `stale_references` step (honest marking, not destruction); verified knowledge untouched. `handle_resource_deleted` marks cleanup. `get_onboarding_status` returns the latest run + count, org-isolated (cross-org → None).
- **Observed-execution integration**: `ONTOLOGY_OBSERVATIONS_ENABLED` (default OFF, rollback=set False) gates a fail-open hook in `_shadow_validate_and_log` (nl_answer_service) that calls `record_observation(org, "nl2sql_query", log_row.id)` AFTER the log row commits (real persisted ID — the earlier pre-add/`deferred_id` wiring was wrong and was corrected before commit). Byte-identical log rows when disabled. Flag stays OFF until a standalone enablement pass.
- **BCS refresh**: onboarding's discovery reuses `propose_operating_model`, so `build_business_context`/`build_ontology_planning_context` see new evidence/proposals with no separate refresh path (verified by `test_bcs_visibility_after_onboarding`).

Files: new `enterprise_onboarding.py` (model), migration 123, `onboarding.py` (service), `test_onboarding.py` (18 tests); surgical hunks in foreign-dirty `config.py` (flag), `models/__init__.py` (registration), `nl_answer_service.py` (writer hook only).

Tests: `test_onboarding.py` (18) — datasource/document/MCP onboarding; idempotent skip; cross-org isolation; partial failure + retry (attempt=2); missing resource; drift detect + reprocess; unchanged no-op; deletion cleanup; verified knowledge preserved; drift never deletes evidence (stale refs recorded); observation writer (on + off); BCS visibility; audit trail; status query. Regression: 108 (observation×3, business rules×2, provenance×2, process API, discovery, action precheck) — green.

Limitations: drift detection is catalog-table-granular only (column-level drift not yet hashed); MCP/automation resources honestly skip evidence/discovery (no integration exists yet); `mcp_server`/`automation` inventory listing is pass-through (no credential probing); observation integration covers only the nl2sql log writer — agent-run writers remain foreign-dirty hot paths (flag OFF by default).

Phase 5 handoff (NOT implemented): provenance-envelope `execution_ref` unification, agent-run-side observation writers, drift-driven scheduled re-onboarding (Celery beat), connector registry expansion beyond the four resource types.
### Phase 5 — Execution Provenance, Observation Writers, Drift Watchdog, Connector Registry (2026-11-01, DONE)

Completes the enterprise-intelligence evidence lifecycle: every milestone TDD (RED→GREEN), separately committed, end-to-end verified.

**Phase 5A — canonical execution provenance (commit `a0a9a9c0e`).** prov-v1 gains ONE execution-reference representation: `execution_ref(record_type, record_id)` / `parse_execution_ref` (closed set `nl2sql_query | agent_run`; missing stays missing, unknown rejected), new `source_type="execution"` + `from_observed_execution` (state honesty: retrieved=False, influenced=False, supports_answer NEVER implied, observed linkage passthrough verbatim, statuses never promoted), `resolve_execution_ref(db, org, ref)` (org-verified via the observation loader — AgentRun org from caller_context; cross-org → None, malformed → ValueError), `entries_with_verified_execution_refs` (unverifiable/cross-org entries OMITTED, never laundered). NL2SQL writer: log-row identity fixed BEFORE the canonical build so the persisted envelope's `execution_ref` names the row itself (`nl2sql_query:<id>`) and entries carry the datasource's verified org stamp; no-provenance rows byte-identical.

**Phase 5B — AgentRun observation writer (commit `3c80738a7`).** `record_observation` fires in `AgentRunService._update_run_finished` AFTER the terminal commit — the single choke point covering inline AND queued runs, and the only writer of `tool_calls` (the matcher's signal source). Same `ONTOLOGY_OBSERVATIONS_ENABLED` flag (one rollback switch, default OFF); org attribution from persisted `caller_context` only (unattributable → fails closed); idempotent (explicit linkages never overwritten; proposed evidence never matches); FAIL-OPEN (observation failure logged, run + response contract untouched).

**Phase 5C — scheduled drift watchdog (commit `71ad905d7`).** `app/services/enterprise/drift_watchdog.py` — lifespan watchdog (catalog_watchdog pattern: asyncio task from `main.py`, Event-stop; NO new scheduling platform). Candidates = distinct (org, type, id) with onboarding runs (never invents scope); digest-driven drift → `on_resource_changed` (retry=drifted; stale references recorded; verified evidence preserved); no-digest types skipped HONESTLY; per-resource failure isolation + configurable backoff; in-process in-flight guard (no double-onboarding); bounded batch per cycle. Flags `ONTOLOGY_DRIFT_WATCHDOG_*` — default OFF (no task, no scans); clean shutdown; `get_drift_status()` (last/next run + counters).

**Phase 5D — connector capability registry (commit `ca774f9cd`).** `app/services/enterprise/connectors.py`: closed capability vocabulary (inventory, catalog, metadata, evidence, semantic_discovery, drift, deletion_cleanup) + `UnsupportedCapabilityError` (explicit unsupported result naming type+capability). Honest limits declared once: datasource/knowledge_base = full pipeline; mcp_server = inventory + deletion_cleanup ONLY; automation = inventory ONLY. Onboarding consults the registry (types/evidence/drift gates) and now existence-verifies mcp/automation against org-scoped rows (cross-org/missing fail closed). Credentials never enter steps/logs (verified by test).

**Phase 5E — end-to-end verification (commit `87dff0582`).** `tests/test_enterprise_e2e_phase5.py`, 4 scenarios: NL2SQL lifecycle (onboard → verified object → authorized query → log → observation → execution_ref → process API), AgentRun lifecycle (persist → observe → tool_binding match → provenance ref → process summary), resource drift (watchdog → stale refs → evidence + BCS survive), security & failure (cross-org refusals, unsupported capabilities, ambiguous matches, failed observation writes, in-flight guard, watchdog restart, legacy-row compatibility).

Files: `provenance.py` (+102), `nl_answer_service.py` (writer hoist + stamping — surgical in foreign-dirty file), `run_service.py` (+47), `drift_watchdog.py` (new), `connectors.py` (new), `onboarding.py` (registry consult), `main.py` (watchdog registration — surgical), `config.py` (5C flags — surgical). Tests: 19+11+13+14+4 new + 209-suite regression green.

Limitations (honest): drift is catalog-table-granular only (no column hashing); mcp/automation provide no evidence/semantic discovery (integrations do not exist — registry says so explicitly); AgentRun-side canonical provenance is built by consumers but not persisted into AgentRun rows; FSM/workflow paths have no provenance writer; watchdog org scan is bounded per cycle (large fleets process incrementally by design).

Phase 6 handoff (NOT implemented): frontend observed-badges (ProcessFlowCanvas), execution_ref persistence at write time on AgentRun, column-level drift, connector adapters for genuinely new integrations, provenance writers on FSM/workflow paths.
### Phase 6A — Observed Execution Visualization (2026-11-01, DONE)

Frontend integration of observed-execution intelligence into the EXISTING ProcessFlowCanvas — no new API, no matching engine, no backend change (the process detail already embeds the payload-free `observed_executions` summary from Phase 3E).

- **Performance**: observations load ONLY when the detail summary reports executions (`execution_count > 0`); ONE request per canvas (`GET /observations`), never per activity; zero-observation processes pay nothing; failures degrade fail-open to the detail summary.
- **Node indicator**: compact `⚡ n×` badge on activities with matched executions + confidence-band tooltip. Absent badge = no execution evidence — documentation never implies execution.
- **Activity detail panel** (separate OBSERVED section below the documented rows): execution count, max match confidence + band (missing confidence = "unknown", never fabricated), latest observed time, linkage methods, record-type references labeled "payload-free" (no SQL, results, prompts, credentials — backend contract).
- **Trust separation preserved**: documented/configured/observed remain independent dimensions; evidence ring (verified/proposed/rejected) and discovery labels render as-is; observations never promote proposed activities.

Files: `ProcessFlowCanvas.jsx` (integration), `observedExecution.js` (pure aggregation), `ProcessFlowCanvas.observed.test.jsx` (9 tests). Regression: enterprise slice green; production build green.

Limitations: per-activity aggregates come from the bounded references list (limit=50 on the observation endpoint — counts beyond that show the summary's count, references show the latest 50); `activity_seq` linkage exists in the data model but recorded executions rarely carry it yet (matching is process-level), so per-activity observed counts are currently sparse by nature.

**Phase 6B recommendations (NOT implemented):** observed-execution trend sparkline per process (time_range already available); process-level confidence distribution widget in EnterpriseMap; "inspect one execution" affordance using the observation API's `inspect_record` decision; activity-level matching enrichment (stamp activity_seq via ontology-action → activity mapping); observed-badge in EBPM ProcessFlowCanvas `DISCOVERY_FILL.observed` fill already defined but unused.
### Phase 6B — Enterprise Map Business Intelligence Overview (2026-11-01, DONE)

The Enterprise Map landing experience gains a truthful executive overview — incremental UX, no rewrite, backed ONLY by already-authoritative data.

- **Backend (ONE additive section)**: `enterprise_map` response gains `overview` — process status counts (verified/proposed/awaiting_review, never conflated), observed-execution evidence (total matched executions, processes covered, confidence bands, latest time — org-verified via `caller_context`; bounded 500-row AgentRun scan), knowledge coverage (business objects / connections / terms / governed metrics / business rules with verified/proposed/enabled splits), resources onboarded, and attention signals (review backlogs, resources whose LATEST onboarding run failed/partial). Sections that fail to compute surface `available: false` — UNAVAILABLE, never zero. No new store, no second matcher, no new endpoint.
- **Frontend (`EnterpriseMap.jsx`)**: compact 5-KPI strip (Business processes / Verified / Awaiting review / With execution evidence / Systems & resources), execution-evidence panel (confidence bands, latest time, honest "N processes without execution evidence — documented only, not (yet) observed"), business-knowledge panel in business language (zero ontology jargon — verified by test), "Needs attention" banner. KPI navigation reuses EXISTING tabs via new optional `onNavigate` prop (`EnterpriseProfile` passes `setExp`; review navigation hidden for non-admins via `canReview`). Observed never promotes evidence/discovery statuses.

Files: `enterprise_discovery.py` (+147), `EnterpriseMap.jsx` (+148), new `test_enterprise_map_overview.py` (8) + `EnterpriseMap.overview.test.jsx` (9), one surgical hunk in foreign-dirty `EnterpriseProfile.jsx`. Tests: 8+9 new, 62 backend + 18 frontend enterprise regression green; production build green.

Limitations: AgentRun observation scan bounded at 500 most-recent runs (very high-volume orgs may undercount totals; per-resource views remain exact); attention signals are count-only (no per-item drilldown yet); KPI navigation targets existing tabs, not filtered views.
### Phase 6C — Enterprise Business Drilldown & Execution Intelligence (2026-11-01, DONE)

The complete navigation chain Enterprise → Department → Capability → Process → Activity → Business Object → Resource → Evidence, reusing existing APIs, models and panels — no new store, no second matcher.

- **Backend (ontology.py, clean file — no main.py changes)**: two org-scoped read-only endpoints — `GET /ontology/enterprise/business-objects/{id}` (authoritative object summary: domain, relationships w/ peer names, associated processes via ProcessEvidence, backing resource parsed from `kb:<id>:<table>`), `GET /ontology/enterprise/resources/{ref_id}` (identity, LATEST onboarding run, related concepts via backing_ref prefix; KB-row fallback; 404 cross-org/missing). Credentials/SQL/execution payloads never returned (test-verified).
- **Department drilldown**: unit nodes in the Enterprise Map open a panel computed ONLY from the persisted map payload (performer processes → clickable, capabilities, connected systems, org-wide execution evidence). Department association stays contextual metadata — no authorization change.
- **Activity → Business Object**: activity panel lists evidence-backed objects (honestly labeled "referenced by this process's evidence" — activity execution linkage exists ONLY when activity_seq was genuinely stamped, never inferred from process-level evidence).
- **Business Object / Resource panels** (`Drilldown.jsx`): breadcrumbs, back/close, relationship peer objects clickable, resource card → onboarding status (latest run, attempt, honest "never onboarded"), related concepts. Missing/cross-org entities → honest "not available" (never fabricated).
- **Execution trend** (`ExecutionTrend` + pure `dailyTrend`): daily bars aggregated from REAL dated observation references (already fetched once per canvas in 6A — zero extra requests); fewer than 2 dated points → "unavailable", never invented counts.

Files: `ontology.py` (+152), `Drilldown.jsx` (new), `EnterpriseMap.jsx`/`ProcessFlowCanvas.jsx` (integration), `EnterpriseProfile.jsx` (3 surgical hunks in foreign-dirty file), tests. Results: 8 backend + 9 frontend new tests; 48 backend + 27 frontend enterprise regression green; production build green.

Limitations: department panel is map-payload-scoped (no separate org-unit API; unit hierarchy not shown — OrgUnit parent names are not in the map payload); capability navigation remains the map's family grouping (no separate capability→process API); resource→evidence navigation surfaces evidence via the object's process links, not a standalone evidence browser; AgentRun-scan bounds apply to map-level observed totals.

Phase 6D recommendation (NOT implemented): unit hierarchy + capability→process API enrichment, standalone evidence browser with review actions, execution-trend on the catalogue cards, saved drilldown deep-links.
