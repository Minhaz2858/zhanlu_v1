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

---

## Increment 4 — Enterprise Map & Process Visualization (2026-11-01)

Status: COMPLETE. Backend map aggregation + two new xyflow components +
landing-experience rewiring. No new dependencies; existing discovery
infrastructure reused end-to-end.

### Backend

- `enterprise_map(db, org_id)` in `enterprise_discovery.py`: one org-scoped
  aggregation — `org_units`, `capabilities`, `processes` (with family,
  discovery_status, evidence_status, activity counts, performer-derived
  responsible_units), `families` with counts, `systems` (authorized
  EnterpriseResource rows, capped 60), `process_links` (persisted only —
  links whose successor is missing/cross-org are DROPPED, never fabricated).
- `GET /api/ontology/enterprise/map` — any authed user, `_org_or_404`
  scoping (404 cross-org).
- Test: `test_enterprise_map_aggregates_persisted_structure` — aggregation
  contents, performer relationship, ghost-successor link dropped, and an
  honestly empty map for another org. Suite: 8 passed.

### Frontend

- `components/enterprise/EnterpriseMap.jsx` — the PRIMARY LANDING experience
  (Enterprise Profile now defaults to it, tab 'M'): Enterprise root →
  authorized systems → org units + capabilities → family headers → process
  nodes (border ring = verified/proposed/rejected; fill = documented/
  configured/observed — the two dimensions are never conflated). Edges are
  ONLY persisted process links (labeled handoff) and performer participation
  (unit performs process). Zoom/pan/fitView/Controls/MiniMap. Legend for all
  six states. Click process node → ProcessFlowCanvas overlay. Honest empty
  state pointing at discovery.
- `components/enterprise/ProcessFlowCanvas.jsx` — swimlane visualization of
  one process: lanes generated from ACTUAL activity performers
  ('Unassigned' lane for performers not yet discovered — no invented actors);
  nodes typed per activity_type (start/end/human/system/decision/approval/
  subprocess) with evidence-status border; edges = persisted seq order
  (solid) + persisted related_processes (dashed, labeled direction, clickable
  to navigate the canvas to the peer process); click activity → side panel
  (seq, type, performer, evidence status, business rules, exception paths,
  evidence-ref count); 'Process detail' opens the catalogue drawer; header
  badges carry discovery_status + evidence_status.
- `EnterpriseProfile.jsx` — map as default landing; Processes catalogue
  preserved with a 'flow view' button in the detail drawer; overlays for
  canvas + drawer; cross-navigation map→canvas→drawer→peer process.

### Verification

One focused backend run (8 passed, incl. incr-3 suites) + one frontend
production build (green). No live LLM discovery, no browser automation.

### Limitations (honest)

- Process nodes only link to org-unit lanes via performer NAME match — a
  performer that is not a discovered OrgUnit renders in the flow canvas
  'Unassigned'-equivalent (no edge), which is honest (no invented mapping).
- The map is a static manual layout (no force/dagre); large enterprises will
  need virtualization or clustering later.
- Capability→process edges are not drawn: no persisted capability-process
  link exists yet (would be fabrication).
- 'Systems' column lists authorized resources; resource→process edges do not
  exist in the model and are not drawn.

### Increment 5 handoff — Confirm/Reject Evidence Lifecycle + Audit

NOTE: the process-level lifecycle ALREADY SHIPPED in Increment 3
(confirm/reject endpoints, review_history, AuditEvent, rediscovery
preservation) and the UI review actions live in the detail drawer. What
remains for a "Increment 5" scope, if pursued:
- Per-ACTIVITY and per-capability/org-unit confirm/reject (the lifecycle
  helpers generalize: `_record_process_review` → `_record_review(target)`).
- An aggregate "review queue" endpoint (needs_review counts by kind) +
  batch review actions with org-admin gating and the same audit trail.
- Evidence-level verification (ProcessEvidence rows carry no status column —
  add migration if per-quote verification is required).
The frontend already renders verified/proposed/rejected everywhere via
`EVIDENCE_BADGE_STYLE` / `EVIDENCE_RING`; any new review surface should reuse
that vocabulary, and Increment 7's browser-validation pass should exercise
the full discover → review → map flow on the live stacks (backend :5003,
vite :5157, browser DB zhanlu_ebom_browser).

---

## Increment 5 — Enterprise Knowledge Verification & Review Center (2026-11-01)

Status: COMPLETE. Verification extended to EVERY discovered kind + central
admin Review Center. No duplication of the process-level lifecycle — the
generic path routes `kind=process` to the existing Increment-3 endpoints.

### Backend

- `ProcessEvidence.evidence_status` (default/server_default 'proposed')
  added to the model AND to unapplied migration 114 (no new migration;
  114 is not applied in any shared environment). Nothing is auto-promoted.
- `_record_review(...)` — generalized review bookkeeping (review_history
  append + AuditEvent). Legacy process audit event types stay stable
  (`process_confirmed`/`process_rejected` via `event_prefix`); new kinds
  write `org_unit_*`, `business_capability_*`, `process_activity_*`,
  `process_evidence_*`.
- `review_entity(db, org_id, kind, id, decision, reviewer, reason)` —
  generic confirm/reject for org_unit | capability | activity | evidence;
  provenance preserved (evidence rows carry no provenance column — the
  audit trail is their record).
- `list_review_queue(db, org_id)` — central queue across ALL kinds with
  confidence, discovery_method, evidence_ids, source refs/quotes.
- Rediscovery preservation extended to ALL kinds: `_persist_proposals`
  now skips updates for `verified` AND `rejected` org units, capabilities,
  and activities (processes already handled in Increment 3).
- Router: `GET /ontology/enterprise/review-queue` (org-admin),
  `POST /ontology/enterprise/{kind}/{id}/confirm|reject` (org-admin,
  optional reason body; `process`/`processes` aliased to the existing
  lifecycle — route shadowing handled). `get_process` now returns
  per-evidence `evidence_status`.

### Tests (suite 9 → all pass; 20 across the three enterprise suites)

`test_review_queue_and_entity_review`: viewer 403 on queue; counts across
five kinds with provenance; generic confirm (org_unit, capability) + reject
with reason (activity, evidence); viewer 403 on review; per-kind audit
events; AI provenance preserved; rediscovery survival on every reviewed
kind with queue shrink; unknown kind → 404.

### Frontend

- `components/enterprise/ReviewCenter.jsx` — admin-only tab 'R' "Review":
  pending proposals grouped by kind (units/capabilities/processes/
  activities/evidence) with confidence, discovery method, evidence-ref
  count, source refs + verbatim quotes; text filter; confirm / reject with
  reason textarea; 'open flow' navigation for processes/activities; honest
  empty state.
- Process detail drawer now renders per-evidence review status chips.
- Map/flow/drawer read the same evidence_status fields — after review,
  switching back to the Enterprise Map refetches and reflects the new
  statuses immediately (tab remount refetch); historical decisions persist
  in review_history + audit_events and survive rediscovery.

### Verification

One focused backend run (20 passed) + one frontend build (green).

### Limitations (honest)

- Evidence rows carry no provenance_json column — their review decision
  lives in audit_events only (by design; quotes are immutable).
- The queue is unpaginated (evidence capped at 200 rows per fetch).
- Batch review actions not implemented (single-item decisions only).

### Increment 6 handoff — Observed-Execution Mapping

Increment 6 maps ACTUAL execution onto the discovered model, honestly:
- Source event logs already identified: AutomationExecution,
  AgentRun/AgentRunStep, WorkflowRun/WorkflowRunStep, Nl2sqlQueryLog,
  AuditEvent (audit_events, NOT legacy audit_logs).
- Scope: per discovered process/activity, query these logs for matching
  execution records (org-scoped); produce an `observed` signal ONLY when
  timestamped records exist — the discovery prompt stays forbidden from
  claiming `observed` on its own (this remains enforced).
- Suggested shape: `observed_execution` service + `GET /ontology/enterprise/
  processes/{id}/observations` (honest-empty default), surfacing
  last-executed timestamps + counts on process/flow nodes (Increment 4
  canvas is ready to render an 'observed' badge from
  `DISCOVERY_FILL.observed`, which already exists).
- No new discovery engine; no fabricated execution events; org isolation
  via the same `_org_or_404` pattern.
