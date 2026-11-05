# Zhanlu — Current Enterprise Architecture Audit

**Date:** 2026-11-01 · **Branch:** `fix/automation-chat-artifacts-ordering` · **HEAD:** `010f282f6` (EBPM increment 5) · **Mode:** READ-ONLY (no files modified, no commits)

---

## 1. Executive Summary

Zhanlu is a multi-tenant (org-scoped, RLS-backed) enterprise AI platform with a My Space workspace of projects binding knowledge bases, agents, automations and dashboards, and a chat runtime where a general assistant orchestrates tools (NL2SQL over catalogued schemas, ontology tools, delegation, RAG) to answer questions.

The **Enterprise Ontology** exists in two parallel layers:

1. **Semantic layer** (P0-2 + P1–P7): `OntologyDomain/Object/Link/Action`, `MetricDefinition`, `AgentDomainBinding`, identity resolution, revisions — **reachable by agents only through explicit tool calls** (`ontology_directory`, `compute_metric`, `read_object_data`, …), never through the system prompt or NL2SQL planning.
2. **EBPM business-process layer** (increments 1–5, this branch): deterministic inventory → verbatim evidence → LLM proposals → human verification → Enterprise Map/flow visualization. **Complete as an admin subsystem but consumed by zero runtime components.**

The core finding: **the ontology is an opt-in parallel subsystem, not a shared business-intelligence foundation.** A chat agent answers business questions entirely through `KBTableMeta/KBColumnMeta` + `Terminology` + `MetricDefinition` NL2SQL stack unless it independently chooses to call an ontology tool. Business processes, org units, capabilities, and evidence never reach the runtime.

---

## 2. Repository and Git State

- Branch `fix/automation-chat-artifacts-ordering`, HEAD `010f282f6`.
- Working tree: **~211 dirty/untracked entries — substantial foreign WIP** (config.py, agents.py, many services/tests modified by other sessions). Any future work must stage explicit files only.
- Enterprise Ontology commit lineage: `fbd568c1b` (EBPM 1: models+inventory) → `5aa98e44d` (EBPM 2: AI discovery) → `11a5b0e89` (EBPM 3: catalogue/detail/review APIs) → `05bb4565a` (EBPM 4: Enterprise Map + ProcessFlowCanvas) → `010f282f6` (EBPM 5: review center). Earlier semantic-layer lineage: `b22058bcd`, `faa566314` (Decision Center/outcomes), ontology P0-P7 series.
- Specs/logs: `docs/superpowers/specs/enterprise-process-operating-model.md` (7-increment plan), `docs/plans/enterprise-process-operating-model-progress-log.md` (increment reports + handoffs).

---

## 3. My Space and Project Architecture

**Frontend:** route `/my-space` → `MySpace.jsx` (tabbed: `project|agent|kb|dashboard|workflow|mcp`, `frontend/src/pages/MySpace.jsx:59`); detail routes `ProjectDetail`, `AgentConfig`, `ResourceDetail` (`frontend/src/App.jsx:157-161`).

**Backend:** most CRUD rides the generic entity router — `ENTITY_MODELS` registered in `backend/main.py:85-113` mounting `/apps/{app_id}/entities/{name}` (`app/routers/entities.py:314`); project-specific analytics in `app/routers/project_catalog.py` (knowledge-map, catalog/tables, catalog/metrics).

**Project model** (`app/models/project.py:15`): `name`, `status`, `is_system`, `resource_type` (`company|personal`), `llm_model_id`, `department` (label only), `project_memories` relation. **No "section" concept** — a project is the union of bound resources.

**Resource binding — three mechanisms coexist:**

| Mechanism | Evidence |
|---|---|
| Join tables | `ProjectKnowledgeBase` (`project_knowledge_base.py:22`, role `home\|member`), `ProjectAgent` (`project_agent.py:18`, role `primary\|member`) |
| Direct `project_id` FK | `KnowledgeBase`, `AgentApp`, `AutomationTask`, `UserFile`, `Report`, `ChatSession`, `AgentConversation`, `Dashboard` |
| Agent→datasource binding | `AgentDataBinding` (`agent_data_binding.py:15`): `agent_app_id`+`datasource_id`, `allowed_tables/columns`, `row_filters`, `nl2sql_enabled` |

**Scoping verdict:**
- **KBs: both** — nullable "home project" FK (`knowledge_base.py:16-19`) **plus** project membership; runtime KB set = home ∪ member.
- **Datasources:** org-scoped only, **no project binding** — exclusively agent-bound via `AgentDataBinding`.
- Everything carries `org_id`/`app_id` from `TimestampedBase` (`app/models/base.py:43-48`) — the Layer-1 isolation wall.
- ⚠️ Legacy dual convention: `project` (name string, `"global"` default) coexists with `project_id` FK on several models.

**Department navigation:** `Project.department` is a backend label used only for agent-discovery scoping, **never authorization** (`project.py:44-50`); zero frontend usage. There is no department model or UI in My Space.

**Project LLM config:** `LlmModel` catalog (Fernet-encrypted keys, org-scoped, `llm_model.py:13-69`) + `Project.llm_model_id`; resolution precedence in `llm_router.py:168-198` (system-agent > project > agent > catalog default > legacy env).

**Project shared memory:** `ProjectMemory` (`project_memory.py:19-53`) — entry types fact/decision/artifact_ref/conversation_summary/data_insight, content-hash dedup, shared by all agents in the project; API at `/api/projects/{id}/memories`.

**Access:** `get_current_user_required`/`require_admin`/`require_org_admin` (`app/deps.py:76-118` — the latter two currently identical checks); Postgres RLS via `scripts/apply_rls.sql` + `app.current_org` session var; `ResourceAccessPolicy` KB/table/column/row policies resolved fail-open (`access_policy_service.py:100-165`).

---

## 4. Agent Orchestration and Execution Flow

**Main path:** `POST /apps/{a}/agents/conversations/v3/{c}/messages/stream` → `add_message_stream` (`app/routers/agents.py:12112`). Branches: workflow chat (`:12401`), Synexia FSM (flag-gated, data-bound turns, `:12758-13098`), and the default **ReAct loop** `event_stream()` (`agents.py:14129`, iteration loop `:14852`) with iteration budget, wall-clock cap, batch-replay guard, tool retry, and reflexion critique. FSM path now defaults ON (`app/config.py:799`) but ReAct remains the primary chat path.

**System prompt** (`agent_prompts.py:2393-2612`): injects tone, role, tools, "Bound Data Sources" block (from `data_source_runtime.py:836-863`), memory/scratchpad blocks, escalation nudges. **Zero ontology content** — no domains, objects, links, business processes, org units, or EBPM evidence (grep across prompt builder: 0 ontology hits).

**Trace of a real question** ("What were October sales?"):
1. `prepare_data_source_runtime` computes `bound_kb_ids` (agent bindings ∪ project KBs ∪ optional workspace auto-bind, default OFF — `data_source_runtime.py:1366-1431`).
2. Prompt carries tool schemas; LLM either answers directly or calls tools: `ask_data_agent` (delegates under the caller's bound KBs, `delegation_tools.py:350`), `execute_query` (read-only NL2SQL, policy-checked `db_tools.py:1060-1066`), `compute_metric`/`read_object_data` (ontology tools), `delegate_task`/`list_available_agents` (subagents), document RAG tools.
3. Delegation routes by published status + KB names — **`AgentDomainBinding` is NOT consulted** (`delegate_tool.py:1396-1443`).
4. NL2SQL plans against `kb_table_meta`/`kb_column_meta` + `Terminology` glossary + `ProjectMetric/MetricDefinition` (`semantic_store.py:583-665`; weighted retrieval `table_retrieval.py:399-405`, `retrieval_evidence.py:61`).
5. Final answer passes the figure-grounding check (`answer_grounding.py` — every number must exist in tool results) and carries **datasource/execution-level provenance** (`result_contract.py:94-101`).

**How agents retrieve enterprise data:** via bound KBs (org-checked, policy-checked) → NL2SQL over catalogued schemas; ontology objects resolve through `backing_ref` `kb:<kb_id>:<table>` parsed in `ontology/data.py:77-81` and org-verified before read-only `QueryService` execution (`:293-303`). Different objects may live in different databases (per-object KB id), but **ontology links do not join data across KBs** — links only drive change fan-out notifications (`ontology/fanout.py:65-72`).

---

## 5. Existing Enterprise Ontology Implementation

### 5a. Semantic layer (pre-EBPM) — IMPLEMENTED, LIVE
- Models `app/models/ontology.py`: `OntologyDomain/Object/Link/Action`, `AgentDomainBinding` (permission_scope read/write), `MetricDefinition` (governed, versioned, lifecycle), plus `identity.py` (entity resolution, human-only confirm), `ontology_revision.py`/`OntologyEvolutionState` (versioning, `evolution.py`/`revisions.py`), `sensing.py` (scheduled sensing pipeline), `template_export.py` (org template export/import).
- Binding: `services/ontology/discovery.py:_apply_binding` (`discovery.py:286-289`) binds objects to `kb:<kb_id>:<table>`; auto-discovery (`discover_and_recompute_coverage`, `:331-507`) exists but is **admin-triggered only** (`routers/ontology.py:706`).
- Actions: risk-tiered, approval-gated (`permissions.py`, `outcomes.py`); escalation (`escalation.py`); document grounding + quote verification (`document_grounding.py`, `quote_verifier.py`).

### 5b. EBPM layer (increments 1–5) — IMPLEMENTED as admin subsystem
| Component | Status | Evidence |
|---|---|---|
| 7 models (EnterpriseResource, OrgUnit, BusinessCapability, BusinessProcess, ProcessActivity, ProcessLink, ProcessEvidence) | ✅ implemented | `app/models/enterprise_process.py`; migration `alembic/versions/114_enterprise_process.py` (+`evidence_status` in incr 5; **unapplied in shared envs** — apply via migration) |
| Deterministic inventory | ✅ | `enterprise_discovery.py:inventory_resources/refresh_inventory` |
| Verbatim evidence extraction | ✅ | `extract_evidence` — table catalogs, relations, ontology objects/links, doc chunks, automations, actions; per-resource error isolation |
| Bounded LLM proposals | ✅ | `propose_operating_model` + `_bounded_prompt_evidence` (≤40 items/24k chars), schema-validated, unknown evidence-ids dropped, endpoint via `llm_router.resolve_effective_llm` (org-scoped) |
| Idempotent persistence + verified/rejected preservation (all kinds) | ✅ | `_persist_proposals` — `not in ("verified","rejected")` guards on process/unit/capability/activity |
| Catalogue/detail APIs + confirm/reject (+review center, audit) | ✅ | `routers/ontology.py` — `/enterprise/processes[/{id}[/confirm\|/reject]]`, `/enterprise/{kind}/{id}/confirm\|reject`, `/enterprise/review-queue`, `/enterprise/map` |
| Enterprise Map (primary landing) + ProcessFlowCanvas (swimlanes) | ✅ | `frontend/src/components/enterprise/EnterpriseMap.jsx`, `ProcessFlowCanvas.jsx`; wired as tab 'M' in `EnterpriseProfile.jsx` |
| Review Center UI | ✅ | `components/enterprise/ReviewCenter.jsx` (admin-only tab 'R') |
| **Observed-execution mapping** | ❌ **not started** (increment 6) | no runtime event-log consumption anywhere |

Tests: `tests/test_enterprise_process_api.py` (9), `test_enterprise_ai_discovery.py` (8), `test_enterprise_discovery.py` (3) — all passing at `010f282f6`.

---

## 6. Data and Business Knowledge Architecture — capability truth table

| Capability | Exists? | Model / Service / API | Limitation |
|---|---|---|---|
| Business vocabulary & synonyms | ✅ | `Terminology` (`db/semantic_store.py:583`), terminology authoring API (NL2SQL OCI parity) | scoped to NL2SQL; no ontology-object linkage |
| Canonical business objects | ✅ | `OntologyObject` (+properties, backing) | bindings require admin discovery run |
| Typed relationships | ✅ | `OntologyLink.link_type` | notification-only; no cross-KB query joins |
| Entity resolution across databases | ✅ | `app/models/identity.py`, P3 services (human-only confirm) | org-scoped; no agent-facing resolution tool |
| Business rules & constraints | ⚠️ partial | `BusinessRuleExtractor` mines `kb_column_meta` comments → **staged disabled** SemanticGuidelines; ontology Actions carry risk tiers | rules reach SQL prompts only after human enablement; **not sourced from ontology** |
| Business metrics, semantic definitions | ✅ | `MetricDefinition` (governed, versioned) + `ProjectMetric`; `compute_metric` tool (domain-binding gated) | metric→ontology-object linkage loose |
| Business processes & decision logic | ✅ (EBPM 1-5) | `BusinessProcess/ProcessActivity`, discovery+review lifecycle | **zero runtime consumers**; decision logic = ontology Actions, separate |
| Organizational ownership | ⚠️ partial | `OrgUnit` + activity performers (EBPM); project `department` label | no runtime use; `department` not authorization |
| System→business-object mappings | ✅ | `backing_ref kb:<kb_id>:<table>`, `AgentDomainBinding` | per-object only; no cross-database relationship queries |
| Document-derived knowledge | ✅ | `document_ingestion` (chunks, hybrid/agentic RAG `agentic_rag.py`), `document_grounding.py`, `quote_verifier.py` | chunk metadata thin (no title/author model) |
| Event logs / observed execution | ❌ missing | candidate sources exist (AutomationExecution, AgentRun/Step, WorkflowRun(+Step), Nl2sqlQueryLog, audit_events) | increment 6 pending; `discovery_status='observed'` unreachable |
| Evidence provenance & verification | ✅ | EBPM `ProcessEvidence` + review center; doc provenance (per-viewer, fail-closed); figure grounding at answer time | **ontology provenance not carried into final answers** (datasource-level only) |
| Cross-project ontology reuse | ⚠️ partial | ontology is **org-level** (shared by all projects implicitly); `template_export.py` exports/imports bundles | no project-scoped ontology views or selective reuse; nothing project-aware in EBPM |

---

## 7. Ontology-to-Agent Integration Status (the critical question)

**Actual integration points (the ONLY places ontology reaches a running agent):**
1. `ontology_directory` tool — voluntary read-only directory (`ontology_tools.py:71-148`).
2. `compute_metric` — governed metrics, gated by `AgentDomainBinding` (`ontology_tools.py:310-360`).
3. `read_object_data` / `score_leads` / `segment_records` / `plan_campaign` — object→`kb:<kb_id>:<table>` reads, org-checked (`ontology/data.py:24-81, 293-303`).
4. `notify_ontology_change` — link fan-out notifications (`ontology/fanout.py:65-72`).
5. `AgentDomainBinding.permission_scope` on ontology actions/metrics (`permissions.py:107-115`).

**Answers to the ten questions:**
1. Prompt retrieval of ontology before answering: **NO** (0 hits in `get_system_prompt`, `agent_prompts.py:2393-2612`).
2. Subagents + shared ontology: subagents inherit context digests (`DELEGATE_CONTEXT_INHERITANCE`) but ontology access is via the same voluntary tools under their own bindings; EBPM layer: **NO**.
3. Object→authorized table resolution: **YES** (`backing_ref` parse + org check, `data.py:77-81, 293-303`).
4. Cross-database relationships: **per-object YES; relationship-driven cross-KB queries NO** (links notify, don't join).
5. Retrieve processes/rules: processes **NO** (EBPM unreachable from runtime); rules only via staged SemanticGuidelines from catalog comments — not ontology.
6. Verified vs proposal distinction at runtime: **NO** — the agent has no notion of evidence_status unless a tool payload happens to include it.
7. Ontology influencing SQL planning / tool selection: **NO** — `backend/app/services/db/` has 0 ontology references; table retrieval is catalog+terminology+metric weighted (`retrieval_evidence.py:61`).
8. Provenance into final answers: **datasource/execution/figure-level YES (`result_contract.py:94-101`, `answer_grounding.py`); ontology-level NO** (object names appear ad-hoc in tool payloads only).
9. Missing ontology: **honest degradation** — typed refusals (`data.py:62-75`, `ontology_tools.py:240-243, 353-357`), silent empty directory, then fallthrough to KB NL2SQL. No hallucination, no failure.
10. New resource connected: catalog metadata auto-indexed (`routers/knowledge_bases.py:297-378`); **ontology binding NOT auto** (admin must run discovery, `routers/ontology.py:706`); EBPM inventory refreshed only on explicit discovery.

---

## 8. Architecture & Data-Flow Maps

### A. Current architecture (ontology position in bold-outline)

```
My Space (frontend MySpace.jsx)
  └─ Projects (Project + ProjectKnowledgeBase/ProjectAgent joins, ProjectMemory)
       ├─ Resources: KBs (home∪member), Automations, Dashboards, Files
       ├─ Datasources (org-scoped, NO project binding) ←— AgentDataBinding
       └─ Agents (AgentApp, llm_model_id, department label)
            │
            ▼
General Assistant chat (agents.py v3 stream → ReAct loop)
  ├─ System prompt: role/tools/Bound-KBs/memory  ┈┈┈✗ NO ONTOLOGY INJECTED
  ├─ Tools:
  │    ├─ NL2SQL stack: kb_table_meta/kb_column_meta + Terminology + MetricDefinition
  │    │    (policy-checked via access_policy_service)          ← primary data path
  │    ├─ ⬛ ONTOLOGY TOOLS (voluntary): ontology_directory, compute_metric,
  │    │    read_object_data, score_leads, segment_records, notify_ontology_change
  │    ├─ ⬛ EBPM LAYER (admin-only): /enterprise/inventory|discover|processes|
  │    │    map|review-queue  →  ZERO runtime consumers
  │    └─ Delegation: delegate_task / list_available_agents (published+KBs,
  │         ⬛ ignores AgentDomainBinding)
  └─ Answer: figure-grounded, datasource-level provenance  ┈┈┈✗ no ontology provenance
```

### B. Current data flow (✗ = missing/incomplete connection)

```
Resource connect ──✅auto──▶ Catalog index (KBTableMeta/KBColumnMeta, catalog_indexer.py)
        │
        └──✗NOT auto──▶ Ontology discovery/binding (admin: routers/ontology.py:706)
                              │
   EBPM: authorized resources ──✅──▶ inventory ──✅──▶ evidence extraction (verbatim)
                              ──✅──▶ LLM proposals (bounded, traceable ids)
                              ──✅──▶ persistence (idempotent, human decisions survive)
                              ──✅──▶ review center (confirm/reject, audited)
                              ──✅──▶ Enterprise Map / FlowCanvas (frontend)
                              └──✗──▶ ✗ NOTHING flows to agent reasoning
        │
Retrieval: agent → bound KBs ──✅──▶ NL2SQL (catalog+terminology+metrics) ──✅──▶ tool results
        │                                                                        │
Final answer ◀──✅ figure grounding ◀────────────────────────────────────────────┘
        └──✗ ontology provenance dropped here
```

---

## 9. Architectural Duplication and Technical Debt

1. **Two semantic stacks:** NL2SQL's `Terminology`+`ProjectMetric`+`kb_table_meta` vs the ontology's objects+links+MetricDefinition — overlapping purpose, zero shared code paths.
2. **Two evidence models:** EBPM `ProcessEvidence` (verified/proposed/rejected lifecycle) vs doc-grounding/quote-verifier provenance vs figure grounding — three provenance vocabularies, no unification.
3. **Legacy dual binding convention:** `project` name-string vs `project_id` FK on KB/AgentApp/AutomationTask/Dashboard.
4. **`Datasource` has no project binding** — only agent-level `AgentDataBinding`; project resource model is asymmetric (KBs: project+agent; datasources: agent only).
5. `require_admin` == `require_org_admin` today (semantic distinction deferred).
6. `department` label exists on Project + AgentApp with no UI and no authorization effect.
7. Migration 114 carries the EBPM schema but is unapplied in shared environments (documented; apply idempotently there).
8. FSM (Synexia) and ReAct are both production-reachable paths — ontology would need integration in both or consolidation first.

## 10. Critical Gaps Preventing Business-Aware AI Agents

1. **No ontology in the system prompt or retrieval** — the agent cannot "think with" the business model; NL2SQL table selection is blind to objects/links/processes.
2. **EBPM processes/org units/capabilities/evidence have zero runtime consumers** — the freshest, human-verified business knowledge is admin-visual only.
3. **No evidence-status awareness at runtime** — agents cannot distinguish verified knowledge from AI proposals; verified/proposed/rejected never reaches answers.
4. **Delegation ignores business-domain bindings** — subagent routing uses published status + KB names, not `AgentDomainBinding`, so business-domain routing doesn't exist.
5. **No ontology provenance in final answers** — citation channels are datasource/execution-level; business-object citations and evidence quotes are dropped.
6. (Supporting) **No observed-execution layer** — `observed` status unreachable; processes remain documented/configured only. **No auto-registration** of ontology when resources connect.

## 11. Recommended Integration Points (for future work — not implemented here)

1. **System-prompt injection:** a bounded "Business Context" block in `get_system_prompt`/`data_source_runtime` Bound-KBs block — org's domains, bound objects, verified processes; evidence-status annotated.
2. **NL2SQL bridge:** feed `OntologyObject.backing_ref` + `Terminology` + `MetricDefinition` + EBPM business rules into `table_retrieval`/`retrieval_evidence` weighting and the query contract (the single highest-leverage change).
3. **Evidence-status channel:** extend `result_contract.NormalizedQueryResult.provenance` with object/evidence fields; extend the answer-citation block (`agent_prompts.py:2576-2585`) to carry them.
4. **Domain-aware delegation:** consult `AgentDomainBinding` in `list_available_agents`/`delegate_task`.
5. **Runtime observations (increment 6):** event-log mapping to make `observed` real, surfaced on flow nodes.
6. **Auto-registration hook:** run ontology discovery + EBPM inventory refresh on KB connect (alongside `maybe_reindex_catalog`).
7. **Project-aware ontology views:** per-project filtering of the org-level ontology to enable selective reuse.

## 12. Key References

| Area | Paths |
|---|---|
| Multi-tenancy | `app/models/base.py:43`, `app/deps.py:76-118`, `scripts/apply_rls.sql` |
| My Space / projects | `frontend/src/pages/MySpace.jsx`, `app/models/project.py:15`, `app/models/project_knowledge_base.py:22`, `app/models/project_agent.py:18`, `app/models/agent_data_binding.py:15`, `app/models/project_memory.py:19`, `app/routers/project_catalog.py` |
| LLM resolution | `app/models/llm_model.py:13`, `app/services/llm_router.py:168-198` |
| Chat runtime | `app/routers/agents.py:12112, 14129, 14852`, `app/services/agent_prompts.py:2393-2612`, `app/services/data_source_runtime/data_source_runtime.py:836-863, 1366-1431` |
| NL2SQL semantic stack | `app/services/db/semantic_store.py:583-665`, `db/table_retrieval.py:399-405`, `db/retrieval_evidence.py:61`, `db/business_rule_extractor.py:1-23`, `db/result_contract.py:94-101`, `db/answer_grounding.py` |
| Ontology tools | `app/services/tool_handlers/ontology_tools.py:71-148, 310-360, 425-619`, `app/services/ontology/data.py:24-81, 293-303`, `ontology/discovery.py:286-289, 331-531`, `ontology/fanout.py:65-72`, `ontology/permissions.py:107-115` |
| Delegation | `app/services/tool_handlers/delegate_tool.py:306-435, 1346-1443`, `delegation_tools.py:350` |
| EBPM | `app/models/enterprise_process.py`, `app/services/ontology/enterprise_discovery.py` (inventory/extract/propose/persist/list_processes/get_process/review/queue/map), `alembic/versions/114_enterprise_process.py`, `app/routers/ontology.py` (enterprise endpoints), `frontend/src/pages/EnterpriseProfile.jsx`, `frontend/src/components/enterprise/{EnterpriseMap,ProcessFlowCanvas,ReviewCenter}.jsx` |
| Access policies | `app/models/resource_access_policy.py:44-94`, `app/services/access_policy_service.py:100-165` |

*Uncertainties flagged for follow-up: (a) exact RLS coverage per table in shared deployments; (b) whether Synexia FSM path requires separate ontology integration or will be consolidated; (c) `audit_events` vs legacy `audit_logs` writer coverage; (d) applied-state of migration 114 in the running shared stacks.*
