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

