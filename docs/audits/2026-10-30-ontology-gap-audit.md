# Zhanlu Enterprise Ontology — Evidence-Backed Gap Audit

**Date:** 2026-10-30 · **Branch:** fix/automation-chat-artifacts-ordering · **Method:** two parallel code audits of the full ontology surface (models, migrations, services, routers, agent tools, tests, frontend), every claim verified to file:line. Motivated by a Palantir-Foundry-style "Enterprise Ontology Engine" proposal; this report separates what already exists from the proposal's true gaps.

---

## Part 1 — What already exists (do NOT rebuild)

| Proposal item | Evidence it exists |
|---|---|
| Multi-source discovery, no single-DB assumption | `services/ontology/discovery.py:1-518` — 3-layer matching (connector hints L40-106, org glossary L130-154, LLM w/ confidence L210-283), round-robin candidate shortlisting across ALL connected sources (L215-250), auto-bind ≥0.8 / "proposed" below (L458-485), LLM budget cap (L369). Live-proven on a foreign-schema CRM DB. |
| Object/property/link/action model + registry | `models/ontology.py` — 6 tables, org-scoped, RLS inline (`migrations/094`), `BACKING_KINDS`/`RISK_LEVELS` vocabularies |
| Grounded reads + business-language contract | `services/ontology/data.py:24` `read_object_data` — kb-route org-ownership check (L164), identifier guard (L16), verified-column-only reads, media wrapping |
| Governed actions (their P9) | `data.py:223-330` — write actions NEVER auto-fire; create `ApprovalRequest` (L256-289); `execute_approved_action` validates org/status/action/expiry (L371-392); `create_po_draft` direct-call guard (`po_draft_tool.py:113-128`) |
| Sensing (continuous sense→decide→act) | `services/ontology/sensing.py` — 60s loop, state-change transitions, read-only gate, fan-out; `routers/ontology.py:422-526` |
| Fan-out to agents | `services/ontology/fanout.py` — bidirectional link traversal → agent bindings → audit + swarm mailbox |
| Template export/import (portability) | `services/ontology/template_export.py` + `routers/ontology.py:382,587` — idempotent, never copies backing refs |
| External read-only API | `routers/ontology_external.py` — API-key gated, disabled when key empty |
| Escalations, PO/quote worklists, xlsx export | `services/ontology/escalation.py`, `routers/ontology.py:239-286` |
| Agent consumption of the same ontology | 9+ registered tools (`query_ontology`, `read_object_data` via `execute_action`, `notify_ontology_change`, `ontology_directory`, `score_leads`, `segment_records`, `plan_campaign`, `escalate_to_human`, PO/quote/invoice) — binding gates refuse unbound objects |
| Test coverage | 59 direct ontology tests across 6 files (`test_ontology_core.py` 13, `test_ontology_p1_batch.py` 13, `test_ontology_sensing.py` 10, `test_p3_batch2.py` 9, `test_p3_batch3.py` 8, `test_escalations.py` 6) |

**Verdict on the proposal's phase plan:** its P1 (source inventory/discovery), P3 (object/link model), P5 (provenance, partial), P6 (registry/runtime), P9 (governed actions) are **already built**. Re-executing them would create a parallel system beside `services/ontology/` — the exact fragmentation the proposal warns against.

---

## Part 2 — True gaps (verified negatives, ranked)

### G1 · Cross-source record-level identity resolution — ABSENT (highest value)
Nothing decides `customer_id=1001` (MySQL) ≡ `Customer No.=C1001` (Excel) ≡ `account_id=1001` (CRM). Discovery matches object *types* to *tables*; nothing matches *records*.
**Reusable foundations:** `knowledge_graph/join_edge_detector.py:91-193` — sampled-value containment (`VALUE_OVERLAP`) + name-match edges with confidence, column granularity (a blocking-key analog); `knowledge_graph/resolver.py` (exact+substring alias resolution, single-domain); `entity_extractor.py:220-288` (embedding-linked entities ≥0.7 confidence, within-project only).
**Missing:** cross-source candidate generation, probabilistic match scoring, survivorship/golden-record rules, human review queue for ambiguous merges. Zero hits for record linkage / golden record / MDM anywhere in `backend/app`.

### G2 · Document-grounded ontology proposals — ABSENT
Documents (PDF/DOCX/XLSX) enter only as RAG chunks (`document_ingestion/upload_rag.py:1-19`). `discovery.py:327-335` queries **only** `KnowledgeBase.source_kind == "database"` — text sources are never discovery candidates. `services/db/business_rule_extractor.py` mines SQL catalog metadata (decodes/enums/grains/views), not prose. No path turns a policy PDF into a proposed ontology rule with document provenance.

### G3 · Evidence/status vocabularies — 21 overlapping vocabularies, no first-class provenance
The six ontology tables have **no `status`, `evidence`, `confidence`, `provenance`, or `version` columns** (`models/ontology.py:57-122`); provenance lives in free JSON and transient discovery reports. Enumerated collisions: 3 confidence vocabularies (discovery LLM, `kb_domain.confidence` migration-088, TrustPanel), 4 readiness/status vocabularies (coverage `live/wired/drafted/empty`, `backing_kind`, discovery `bound/would-bind/proposed/…`, PO draft statuses). Plus one **bug**: ontology audit events use `event_source="ontology"` and types `ontology_fanout`/`escalation_created`/`escalation_resolved` that are **not whitelisted** in `models/audit_event.py:16-22`.
Full inventory in the audit working notes (21 rows).

### G4 · Versioned ontology registry — ABSENT
No version/diff/change-history on any ontology table. Only history: row timestamps, AuditEvents, approval rows. The only "version" string is the hard-coded `"template_version": "1.0.0"` (`template_export.py:110`). There is no eval/approval gate over ontology changes (eval gates cover *agent publish* only — `agent_lifecycle.publish_agent`).

### G5 · Metrics registry ↔ ontology — declared, not implemented (stub)
`BACKING_KINDS` includes `metric` and the model docstring says it references a MetricDefinition id (`models/ontology.py:60-63`) — but `data.py:40,48-56` returns honest *"not yet supported"* for `entity`/`metric` backings, and discovery never assigns them. Meanwhile a real, **enforced** metric registry exists in the NL2SQL layer: `models/metric_definition.py` (binding constraints injected at `nl_answer_service.py:1085-1101`, enforced pre-execution via `query_contract.py:61-87` + `execution_gate.py:342-383`), plus LLM-bootstrapped `ProjectMetric` proposals (human-approved). They are datasource-scoped, lack owner/lineage, and are detached from the ontology. Domain-value computations are hard-coded Python (`domain_value.py:118-130`), registered nowhere.

### G6 · Evolution triggers — rich sensing, thin actuation
Trigger inventory (10 triggers audited): the **only** automatic ontology mutation is the manually-invoked `POST /ontology/discover`. Everything else dead-ends:
- `services/datasources/drift.py:15-69` (schema drift detect) is **dead code** — no runner calls it; even if run, report-only.
- Sensor breaches notify agents; never propose/update ontology elements.
- New KB create/update hooks reindex the *semantic catalog* (FKs, join edges, domain + metric proposals) but never trigger ontology discovery.
- No binding-validation loop: if a bound table's mapped column disappears, nothing marks the binding stale.
- Nightly drift task is forecast-model drift, unrelated.

### G7 · `permission_scope` never enforced at runtime — governance hole (P0 bug)
`AgentDomainBinding.permission_scope` (read|write) is stored (`routers/ontology.py:629-667`), exported, counted — but **no execution path checks it**. `execute_action`/`execute_approved_action` check only the action's `risk_level` and the approval record. An agent bound read-only to a domain is not actually restricted at execution time. Currently mitigated only by the approval gate on write actions, but it is decorative as designed.

### G8 · Relationship graph / visualization — ABSENT (frontend + API)
No component renders `ontology_links` (zero Mermaid/D3/SchemaExplorer matches in `frontend/src`). `EnterpriseProfile.jsx` renders totals, coverage cards, objects panel, PO worklist, sensing card — no graph, no cross-source data-source mapping view. No schema-map/drift/visualize endpoints exist in any router.

---

## Part 3 — Recommendation

Extend the existing `services/ontology/` layer; build only G1–G8 above. Sequence and acceptance criteria in the companion PRD: `docs/plans/2026-10-30-ontology-engine-prd.md`.

**Correction record:** earlier project memory claimed `SchemaRelationshipAgent`, `SchemaDriftSkill`, `SchemaVisualizer`, `OntologyFeedback`, `SchemaExplorer`, and `/api/project_config.py` drift/visualize endpoints existed in this workspace. This audit verified they do **not** (no `backend/agents/` dir; drift has no runtime caller; no visualize endpoints; no graph UI). Those artifacts belong to a different project.
