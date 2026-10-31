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
