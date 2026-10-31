# Enterprise Business Process & Operating Model — Progress Log

## Increment 2 — Evidence-Based AI Discovery Engine (2026-10-31)

**Status:** COMPLETE. Commit: (this commit). Spec:
`docs/superpowers/specs/enterprise-process-operating-model.md`.

### Implemented

**Phase A/B — evidence extraction** (`enterprise_discovery.py:
extract_evidence`): deterministic, per-resource error-isolated extraction from
authorized rows only — per-datasource table catalog (names, roles,
descriptions, verbatim source comments, columns), table relations (resolved
endpoint names), ontology objects + links, document catalog rows + best-effort
verbatim Chroma chunk samples (`document_chunk`, skipped honestly when the
chunk store is empty/unavailable), automation configs, governed actions.
Every item: `{id, kind, source_ref, quote}` — quotes are verbatim, never
synthesized.

**Phase C — AI-assisted proposal** (`propose_operating_model`):
- One bounded structured-LLM call (`chat_completion_json_sync`) with a strict
  JSON schema (org_units / capabilities / processes with typed activities).
- LLM endpoint resolved via the EXISTING hierarchical resolver
  (`llm_router.resolve_effective_llm`, org-scoped) — no hardcoded providers.
- Bounded input: `_bounded_prompt_evidence` (≤40 items, ≤24k chars, kind
  quotas) + bounded retries (schema call → one no-schema retry for
  compatible-mode providers that reject `response_json_schema`).
- Validation: unknown/absent evidence_ids are DROPPED (fabrication cannot
  persist); activity types coerced to the allowed vocabulary;
  discovery_status restricted to documented|configured|observed; everything
  persists as `evidence_status='proposed'` — never auto-verified.

**Persistence** (`_persist_proposals`): upsert by (org, name) — repeated runs
UPDATE, never duplicate; evidence upsert by (org, process, source_ref);
**verified rows are never weakened** by later proposals; activities upsert by
(seq, name); removed activities retained as evidence history.

**API:** `POST /ontology/enterprise/discover` now returns
`{inventory_summary, discovery}` where discovery carries the full §6 metadata:
resources/documents/catalog inspected, evidence extracted, proposals
created/updated, evidence created, skipped resources with reasons, and honest
partial states (`insufficient_evidence` / `llm_malformed` / `llm_error`).

**Migration:** none beyond 114 — one additive `provenance_json` column was
added to `ProcessActivity` directly in the unapplied migration 114.

### Verified live (browser DB, real LLM)

Real discovery run against the automotive sandbox org (85 evidence items:
1 catalogued datasource, 2 documents, 35 ontology objects, 32 actions) with a
DB-resolved qwen endpoint produced **8 evidence-grounded business
capabilities** (Sales Transaction Management, Financial Analysis, Inventory
Management, Demand Forecasting, Shipment Coordination, Price Adjustment
Proposal, Stockout Alerting, Shipment Rerouting Proposal) and **honestly
proposed zero processes** — the sandbox's document KBs have no ingested chunk
text, and the model declined to fabricate end-to-end workflows from catalog
evidence alone. This is the required honest behavior. Once SOP documents are
ingested (`document_chunk` evidence exists), the same pipeline proposes
processes (proven by unit tests with seeded evidence).

### Tests (`tests/test_enterprise_ai_discovery.py`, 8 — all mocked LLM)

Document/catalog/ontology evidence → persisted proposals with traceable
evidence; honest empty on no evidence; malformed LLM → explicit partial state
(inventory still refreshed); idempotent re-discovery; verified-record
preservation; hallucinated evidence-id filtering; org isolation; partial
failure isolation (one resource's catalog read fails → run completes, skip
recorded, other evidence still persisted). Combined with increment-1 suite:
**83 regression tests pass**; frontend production build green.

### Limitations (honest)

- Observed processes: no case-id/activity/timestamp event-log model yet —
  `discovery_status='observed'` can never be produced by this pipeline (the
  prompt forbids it); increment 6 (observed-execution mapping) is the path.
- Document metadata is thin (no title/type/author model) — evidence quotes
  carry the signal instead.
- Process proposal quality depends on ingested SOP documents; with catalog
  evidence alone the engine stops at capabilities — by design.

### Increment 3 handoff — Process Catalogue & Detail APIs

Build on `BusinessProcess`/`ProcessActivity`/`ProcessEvidence`:
`GET /api/ontology/enterprise/processes` (catalog: family grouping, evidence
status filter, needs_review counts), `GET /enterprise/processes/{id}` (full
detail: ordered activities, performers, inputs/outputs, evidence quotes,
linked ontology objects via provenance evidence source_refs), and the
confirm/reject lifecycle (`POST .../confirm` org-admin, audited, proposed →
verified — the counterpart of `_persist_proposals`' verified-preservation).
Frontend: catalogue view + detail drawer feeding the swimlane canvas
(increment 4).

---

## Increment 3 — Enterprise Process Management (2026-10-31)

Status: COMPLETE. Backend + frontend + tests. Human review lifecycle with
preserved AI provenance; verified AND rejected decisions survive rediscovery.

### Backend

Service (`app/services/ontology/enterprise_discovery.py`, appended):
- `list_processes(db, org_id, q, family, status, page, page_size)` — catalogue
  with search (name/purpose), family + evidence-status filters, pagination,
  family grouping with counts, per-process `evidence_count`/`activities_count`,
  `responsible_units` derived from ACTIVITY performers (no hardcoded
  department mapping), `needs_review` flag (evidence_status == 'proposed').
- `get_process(db, org_id, process_id)` — full detail: ordered activities,
  verbatim evidence rows, related processes (both ProcessLink directions with
  peer names), ontology connections resolved from evidence `source_ref`
  (`ontology:{id}` → OntologyObject/OntologyLink), review history from
  `provenance_json["review_history"]`. Cross-org/missing → None (fail closed).
- `confirm_process` / `reject_process(db, org_id, id, reviewer, reason)` —
  org-admin decisions; append to `provenance_json["review_history"]`
  (decision, reviewer id/name, reason, timestamp) + `AuditEvent`
  (`process_confirmed` / `process_rejected`). Rejection reason recorded.
  `_persist_proposals` update branch is now
  `evidence_status not in ("verified", "rejected")` — BOTH human decisions
  survive rediscovery unchanged.

Router (`app/routers/ontology.py`):
- `GET  /api/ontology/enterprise/processes` — any authed user, org-scoped
  via `_org_or_404` (platform admin may pass org_id; others pinned).
- `GET  /api/ontology/enterprise/processes/{id}` — 404 on missing/cross-org.
- `POST /api/ontology/enterprise/processes/{id}/confirm` — require_org_admin.
- `POST /api/ontology/enterprise/processes/{id}/reject` — require_org_admin,
  optional `{reason}` body.

### Tests (`tests/test_enterprise_process_api.py`, 7 — mocked LLM)

Honest empty catalogue; search/family/status filters + family counts +
pagination + performer-derived responsible units; detail ordering + verbatim
evidence + ontology connections + honest 404; cross-org 404; confirm
admin-gated (viewer 403) with review history + AI provenance preserved +
AuditEvent; reject records reason and survives rediscovery without re-proposal;
review history appends (reject then confirm → both entries, final verified).
Regression slice: 55 tests pass across the enterprise/EBPM/ontology-core
suites; frontend production build green.

### Frontend (`frontend/src/pages/EnterpriseProfile.jsx`)

New experience **F "Processes"** (`BusinessProcesses`):
- Catalogue: search box, family + status filters (incl. "Needs review"),
  family-grouped card grid, discovery_status + evidence_status badges,
  needs_review highlight, evidence/activity counts, responsible units,
  pagination, honest empty state ("Run AI discovery…").
- Detail drawer (`ProcessDetailDrawer`): overview fields, ordered activity
  sequence (type/performer/rules/exceptions), evidence with verbatim quotes +
  source refs, related processes, ontology connections, review history.
- Human review (admin-gated via `useAuth().isAdmin`, server still enforces
  403): Confirm / Reject with reason textarea; result refreshed in place.

### Limitations (honest)

- Per-activity evidence review (activity-level confirm) not in this
  increment — review operates at process level.
- `needs_review` is a flag, not a queued-work aggregate endpoint (the
  catalogue `status=proposed` filter covers triage).
- Process inputs/outputs (`input_refs`/`output_refs`) exist on the model but
  the proposal schema does not populate them yet — surfaced in the detail
  drawer only when present.

### Increment 4 handoff — Enterprise Map + Swimlanes (@xyflow/react)

Ready for consumption:
- `GET /api/ontology/enterprise/processes` → catalogue rows carry
  `process_family`, `activities_count`, `responsible_units` — group into
  swimlanes by family; lanes from `responsible_units` (performers), NOT a
  hardcoded department list.
- `GET /api/ontology/enterprise/processes/{id}` → `activities` (ordered,
  `activity_type` ∈ start_event/end_event/human_task/system_task/decision/
  approval/subprocess — map to xyflow node kinds), `related_processes`
  (direction + peer — cross-process edges), `ontology_connections`
  (ontology object/link ids — overlay anchors for the existing
  `OntologyGraph.jsx` which already uses @xyflow/react).
- Patterns to reuse: `OntologyGraph.jsx` / `WorkflowCanvas.jsx` (xyflow
  setup), `EVIDENCE_BADGE_STYLE` + this file's `DISCOVERY_STATUS_STYLE` for
  node badges, `ProcessDetailDrawer` for click-through.
- Rules to keep: never fabricate edges — only `related_processes` and
  ontology connections from evidence; documented ≠ configured ≠ observed
  coloring on nodes; proposal (unverified) nodes visually distinct.
