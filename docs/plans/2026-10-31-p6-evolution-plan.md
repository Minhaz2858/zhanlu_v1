# P6 — Continuous Ontology Evolution: Implementation Plan v1 (2026-10-31)

Anchor audit: `docs/audits/2026-10-31-p5-p7-stage-a-audit.md` (G-P6-1..5). Non-negotiables: never silently overwrite verified definitions; never auto-confirm identity; never turn inferred into verified; preserve rejected decisions; every meaningful change through P2 revisions; reuse the sensing/lifespan scheduling (no new framework); idempotent reconciliation.

## Invariants
- E1: Reconciliation is IDEMPOTENT — an unchanged source produces zero proposals/revisions/notifications (signature comparison).
- E2: The ONLY automatic ontology mutations are: marking a binding stale (`binding_status`), and creating `proposed` (never verified) objects/links via the existing discovery dry-run pipeline. Everything else → human review queue or notification.
- E3: Verified objects/definitions are never mutated by reconciliation; conflicting business definitions become document-style PROPOSALS (P4 review queue discipline).
- E4: Failed reconciliation is retryable and partial-failure-safe: per-source state rows, bounded per-run work, clear failure states; no corruption on crash (per-source transactions).
- E5: Cross-organization isolation: every scan and every state row is org-scoped.

## Tasks

### P6-T1 — Evolution state + migration 112
- `ontology_evolution_state` (org_id, source_key unique w/ org, source_kind, signature, last_status ok|changed|failed, last_error, last_run_at, attempts).
- `ontology_objects.binding_status` (String 20 NULL: ok|stale|broken) + `last_validated_at` (DateTime NULL) — additive.
- Idempotent DDL applied to dev postgres (lineage mismatch workaround).

### P6-T2 — Detection (`services/ontology/evolution.py`)
`reconcile(db, org, apply=False) -> ChangeReport`
- DB drift: for each org database KB → `SchemaService.drift()` vs catalog (KBTableMeta/KBColumnMeta) → added/removed tables, added/removed/type-changed columns.
- Stale bindings: org objects with `backing_kind=table` whose bound table/columns no longer exist in the live schema/catalog → `binding_status=stale|broken` (E2 allowed mutation).
- Broken metric dependencies: MetricDefinitions whose `base_table` vanished → report + typed unavailable already at compute; flagged in report.
- Document changes: file KBs re-hashed via `record_document_version` pass → new version → note for grounded re-extraction (proposal pipeline already handles supersede).
- Spreadsheet changes: tabular KB row-count/header diff vs catalog (bounded sample).
- Self-checks: unbound objects, invalid backing refs, identity conflicts (inferred crosswalks count).
Signature per source = hash of the detected facts; state row unchanged → skip (E1).

### P6-T3 — Impact analysis
For each change: affected objects (backing_ref match), properties (column match), relationships (links touching affected objects), metrics (MetricDefinition.base_table/datasource match, source_object_refs), agents (fanout traversal over linked domains), consumer capabilities (sensors on affected objects). Included in ChangeReport per change.

### P6-T4 — Controlled evolution actions (apply=True)
- stale marking (E2) + `notify_object_change` fan-out for material changes (P6 notification reuse).
- New-concept proposals: run `discover_bindings(db, org, apply=False)` on changed sources; NEW candidate matches become `proposed` objects only via the existing discovery apply path flagged `ONTOLOGY_EVOLUTION_PROPOSE_ENABLED` (default OFF) — v1: report-only + notification.
- Conflicting definitions: never auto-mutated (E3); surfaced in report + notification.
- Post-actions: `compute_coverage` recompute.

### P6-T5 — Scheduling + API
- `run_due_evolution` tick in the sensing loop (flag `ONTOLOGY_EVOLUTION_ENABLED`, default OFF) — same lifespan task, no new framework.
- `POST /ontology/evolution/reconcile` (org admin; body {apply: bool}) → ChangeReport; `GET /ontology/evolution/state` (org admin) → per-source state.
- Retry: failed state rows re-attempted next tick with attempts cap; per-source try/except so partial failure never corrupts (E4).

### P6-T6 — Tests (`tests/test_ontology_evolution.py`)
Schema addition → changed signature → stale none + proposal note; column removal → stale marking + revision via bind-status mutation? (binding_status change is a field update — record_revision change_kind="drift" per E2? YES: record P2 revision on stale marking); type change; doc version change; idempotency (second run = zero changes); duplicate-event prevention; verified preservation (verified object with vanished column → marked stale NOT deleted/rebound); cross-org isolation; retry after failure; apply=False report-only; admin gate on endpoints.
