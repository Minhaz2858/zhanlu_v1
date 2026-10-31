# Zhanlu Enterprise Business Process & Operating Model — Implementation Spec

Status: EXECUTION GUIDE. Extends (does not replace) the EBOM program
(`enterprise-business-operating-model-redesign.md`, commits `864efd48b` →
`a3abd5c68`). Branch `fix/automation-chat-artifacts-ordering`.

## 1. Product objective shift

The ontology-administration view (domains, objects, metric cards, sensors) is
the semantic foundation — not the product. The product objective is:

> "Show me how this entire enterprise works, how its departments and systems
> interact, how business processes flow, and how value is created."

Zhanlu must **discover** the enterprise from its authorized resources — not ask
administrators to model it by hand. Every discovered element carries evidence
status (verified / inferred / proposed / unknown), source, confidence, and
discovery method.

## 2. Stage-1 audit findings (evidence)

### Existing substrate (reuse — never rebuild)

| Capability | Where |
|---|---|
| Multi-source KBs (database/file/document), org-scoped, catalogued schemas | `app/models/knowledge_base.py`, `app/models/knowledge_catalog.py` (KBTableMeta/KBColumnMeta/KBTableRelation with descriptions + source comments) |
| Agent harness source discovery (bound_kb_ids, auto-bind, demo/real split) | `app/services/tool_handlers/delegation_tools.py`, `data_source_runtime/data_source_runtime.py` |
| Document ingestion (pdf/docx/xlsx/…→Chroma per-org collections) | `app/services/document_ingestion/` |
| Ontology + governance (objects, links, actions, metrics, approvals, audit, revisions, sensors) | `app/models/ontology.py`, `app/services/ontology/*` |
| Governed metrics (deterministic compute) | `app/services/ontology/metrics.py` |
| Timestamped execution surfaces (candidate event logs) | `AutomationExecution`, `AgentRun/AgentRunStep`, `WorkflowRun/WorkflowRunStep`, `Nl2sqlQueryLog`, `AuditEvent` |
| Frontend graph canvas | `@xyflow/react` v12 (OntologyGraph custom nodes + domain-cluster layout; WorkflowCanvas) |

### Genuine gaps (this program)

1. **No process model**: nothing represents a business process, its activities,
   triggers, handoffs, decisions, or evidence.
2. **No enterprise resource inventory** endpoint: the pieces exist but are not
   assembled into "what does this org actually have".
3. **No organizational/capability architecture**: departments, roles,
   capabilities are not modeled.
4. **No event-log/case model**: process mining needs case-id + activity +
   timestamp; none of the run tables expose that abstraction.
5. **Document metadata is thin** (no title/type/author/dates) — process
   discovery from documents must therefore quote chunk text as evidence.
6. **No swimlane/BPMN visualization** (xyflow supports it; nothing builds it).

## 3. Architecture (additive; ontology stays the semantic foundation)

### 3.1 Domain models (new tables, org-scoped, evidence-carrying)

`app/models/enterprise_process.py`:

- **EnterpriseResource** — inventory row: `resource_kind` (datasource|document|
  action_interface|ontology|automation|agent), `ref_id`, `name`, `status_json`
  (health/catalog/lifecycle), `evidence_status`. Refreshable (discovery run
  stamps `discovered_at`).
- **OrgUnit** — discovered department/team/role: `unit_type`
  (department|team|role), `name`, `parent_ref`, `responsibilities`, evidence.
- **BusinessCapability** — `name`, `description`, evidence; linked to
  org units / processes / ontology objects via `CapabilityLink`.
- **BusinessProcess** — `name`, `purpose`, `trigger_event`, `objective`,
  `start_condition`, `completion_condition`, `process_family` (discovered
  label, e.g. "order-to-cash" — never a hardcoded catalogue),
  `discovery_status` (documented|configured|observed), `evidence_status`,
  `confidence`, `provenance_json` (source refs + method).
- **ProcessActivity** — belongs to a process: `seq`, `name`, `activity_type`
  (human_task|system_task|decision|approval|subprocess|start_event|end_event),
  `performer_ref` (org unit / role / system / agent), `input_refs`,
  `output_refs`, `business_rules`, `exception_paths`, evidence + confidence.
- **ProcessLink** — dependency between processes (`predecessor_id`,
  `successor_id`, `handoff_ref`, evidence).
- **ProcessEvidence** — per process/activity: `evidence_kind`
  (document_chunk|table_catalog|relation|ontology_link|metric|execution_log),
  `source_ref` (kb id + chunk/table), `quote` (verbatim), `confidence`.

Discovery method: deterministic inventory first; LLM **proposes** process
candidates from document chunks + catalog semantics (clearly marked
`inferred/proposed`); nothing is auto-promoted to `verified` without human
confirmation (reuse the ontology evidence lifecycle, P4).

### 3.2 APIs (all org-scoped under `/api/ontology/enterprise/*`)

- `GET  /enterprise/inventory` — deterministic resource inventory (all kinds,
  health/catalog status, counts; no LLM).
- `POST /enterprise/discover` — runs a discovery pass: refresh inventory,
  propose org units / capabilities / process candidates from authorized
  resources (LLM-assisted where document/catalog evidence exists, honest
  "no evidence" otherwise). Idempotent-ish: upserts by (org, name, kind),
  records evidence rows.
- `GET  /enterprise/processes` / `GET /enterprise/processes/{id}` — catalogue
  + full detail (activities, links, evidence, discovered KPIs).
- `GET  /enterprise/map` — the Enterprise Map aggregate (value streams =
  process families, org units, capabilities, systems) for the Level-1 view.
- `POST /enterprise/processes/{id}/confirm` / `/reject` — org-admin evidence
  lifecycle (inferred → verified), audited.
- `GET  /enterprise/processes/{id}/execution` — observed execution view from
  the timestamped run tables where a mapping exists; honest "no observed
  execution evidence" otherwise.

### 3.3 Frontend — new primary navigation in EnterpriseProfile

New first-class experience tabs (Level 1–5 drilldown), keeping A–F intact:

1. **Enterprise Map** (new default landing): value streams → process families
   → org units → systems; xyflow with swimlane columns; every node badge =
   evidence status.
2. **Business Processes**: catalogue + BPMN-inspired swimlane detail
   (`ProcessFlowCanvas` — xyflow, swimlanes = departments/roles/systems,
   node types = start/end/human/system/decision/approval, evidence badges).
3. **Organization & Capabilities**.
4. **Ontology** (existing C studio).
5. **Process Intelligence** (observed execution; only where evidence exists).
6. **Business Intelligence** (existing A experience, process-linked).
7. **Governance** (existing F).

### 3.4 Invariants (from the master directive)

- Discovery is dynamic: no industry templates, no hardcoded automotive.
- The harness exposes only authorized source descriptors; discovery reads
  catalog metadata + document chunks of authorized KBs only.
- LLM proposes; deterministic services inventory; humans verify.
- Never present documented as observed; never fabricate execution events.
- Reuse P0–P7 ontology, evidence lifecycle, approvals, audit.

## 4. Increment plan (each: RED→GREEN→review→commit)

1. Models + migration + deterministic `/enterprise/inventory` (+tests).
2. LLM-assisted discovery pass (org units/capabilities/processes) with
   evidence quotes + confidence; upsert idempotency (+tests, mocked LLM).
3. Process catalogue + detail APIs (+tests).
4. Enterprise Map + Process swimlane visualization (frontend) (+build).
5. Confirm/reject evidence lifecycle + audit (+tests).
6. Observed-execution mapping (+tests, honest-empty default).
7. Browser validation + screenshots + spec/progress updates.
