# Phase 7C — Implementation Plan (2026-11-02)

Companion to `2026-11-02-phase-7c-architecture-audit.md`. TDD RED→GREEN→REFACTOR
per task; every task ends in an isolated commit. Foreign-dirty (FD) files are
staged surgically (individual hunks only).

## Group 1 — Security & authorization (P0)

### T1 — Pinned-datasource org wall (B1)
- Files: `backend/app/services/data_source_runtime/data_source_runtime.py` (FD)
- Behavior: the client-supplied `pinned_data_source_id` may only pin a
  KnowledgeBase whose `org_id` equals the agent's `org_id`; when `agent_app`
  is None (no verifiable org) the pin is refused. Fail-closed; same-org pins
  behave exactly as before.
- Tests: `tests/test_datasource_pinning_org_wall.py` — same-org pin allowed;
  cross-org pin refused (id not in bound_ids); no agent_app → refused; deleted
  KB refused.
- Migration: none. Rollback: revert hunk.

### T2 — workspace-settings authorization (B2)
- Files: `backend/app/routers/workspace_settings.py` (clean)
- Behavior: GET/PUT resolve the org from the verified user (`user.org_id`);
  a client-supplied `org_id` that mismatches → 403. PUT additionally requires
  org-admin (`require_org_admin`). Reads keep working for org members.
- Tests: extend `tests/test_workspace_settings.py` — non-member org_id → 403;
  non-admin PUT → 403; admin same-org PUT → 200 and persisted.
- Rollback: revert file.

### T3 — auto-bind doc/code mismatch (B3)
- Files: data_source_runtime.py docstrings only (FD)
- Behavior: no behavior change; docs corrected to state the union includes
  all workspace KB kinds (deliberate per test contract) and the flag is
  deprecated (admin-gated; removal deferred pending user migration).
- Rollback: doc-only.

## Group 2 — Lifecycle correctness (P0/P1)

### T4 — Cleanup dead-wiring fix (D0)
- Files: `backend/app/services/enterprise/cleanup.py` (clean)
- Behavior: `_load_resource_row` loads by id only (org verification follows);
  `handle_resource_deleted` works on already-soft-deleted rows (production
  order). Idempotent; cross-org still fails closed.
- Tests: `tests/test_cleanup_post_delete_order.py` — soft-delete first, then
  cleanup performs catalog/binding/backing/EBPM work; second call idempotent;
  cross-org still PermissionError.
- Rollback: revert.

### T5 — Dedicated automation delete runs cleanup (D2)
- Files: `backend/app/routers/automation_api.py` (clean)
- Behavior: after tenant-checked soft-delete, call `handle_resource_deleted`
  (fail-open, `cleanup_error` surfaced — mirrors entities.py contract).
- Tests: `tests/test_automation_delete_cleanup.py`.

### T6 — Bulk delete runs cleanup (D3)
- Files: `backend/app/services/entity_service.py` (clean, add helper) +
  `backend/app/routers/entities.py` delete_many (FD)
- Behavior: new `soft_delete_many_records(...) -> list[ids]` returning deleted
  ids (same tenant scoping); delete_many endpoint runs the hook per id for
  hook-mapped entities with per-record fail-open errors collected honestly.
- Tests: `tests/test_bulk_delete_cleanup.py`.

### T7 — AgentApp deletion cleans its bindings (D4)
- Files: cleanup.py + entities.py hook map (FD)
- Behavior: `agent_app` added to SUPPORTED_RESOURCE_TYPES; cleanup soft-deletes
  org-scoped AgentDataBinding rows whose `agent_app_id` matches; generic
  AgentApp delete runs the hook.
- Tests: extend test_cleanup_post_delete_order.py.

### T8 — Watchdog stops spinning on deleted resources (D1)
- Files: `backend/app/services/enterprise/drift_watchdog.py` (clean)
- Behavior: before digest, `onboarding._resource_exists`; missing →
  summary["skipped_missing"] and prior EnterpriseOnboardingRun rows for the
  triple are soft-deleted (org-scoped) so the candidate disappears; no
  failed run rows accumulate.
- Tests: `tests/test_drift_watchdog_deleted_resources.py`.

### T9 — Onboarding digest org filter (D6)
- Files: onboarding.py — one-line org_id filter; covered in T8's suite.

### T10 — list_links honors soft-delete (D5)
- Files: `backend/app/services/ontology/queries.py`
- Tests: extend existing queries/ontology suite; verify callers (graph panel,
  7B queue) unaffected — they either filter already or expect live links.

## Group 3 — Provenance integrity (P1)

### T11 — Canonical provenance for lineage-only rows (E1)
- Files: nl_answer_service.py `_shadow_validate_and_log` (FD)
- Behavior: prov-v1 envelope built whenever entries exist (ontology, SQL
  lineage, or both); raw `lineage["ontology"]` section stays gated on
  `ontology_provenance`; no-provenance rows remain byte-identical; NULL
  lineage stays NULL.
- Tests: `tests/test_lineage_only_provenance.py` — sql-lineage-only row gets
  envelope; no-data row unchanged; both-present row unchanged.

### T12 — Honest evidence_status in converters (E2)
- Files: `backend/app/services/enterprise/provenance.py`
- Behavior: `from_document_proposal` / `from_process_evidence` omit
  `evidence_status` when the source row/dict carries none (status first,
  evidence_status second, then omit — never invent "proposed").
- Tests: update `tests/test_canonical_provenance.py` expectations.

## Group 4 — Audit governance (P1/P2)

### T13 — nl2sql audit org stamp (C1)
- Files: nl2sql/__init__.py `_write_audit` (FD)
- Behavior: `_write_audit` gains optional `org_id`; resolves from the
  AgentDataBinding row when available (org-scoped, fail-open); explicit
  org_id param preferred at call sites where the org is already known.
- Tests: `tests/test_nl2sql_audit_org.py`.

### T14 — Audit registry reconciliation (C2)
- Files: `backend/app/models/audit_event.py` (clean)
- Behavior: registry lists extended to the full written vocabulary (types,
  sources incl. `enterprise`, outcomes incl. `error|allowed|completed|partial`);
  dead registered types kept (legacy readability); contract test asserts every
  statically-known writer type/source/outcome is registered.
- Tests: `tests/test_audit_event_registry.py`.

## Group 5 — SemanticGuidelines consistency (P2)

### T15 — Marker-prefix guard (A1)
- Files: `backend/app/routers/knowledge_bases.py` (clean)
- Behavior: create/PATCH reject titles beginning `[business-rule:` (400) —
  manual rows can never masquerade as derived markers, derived rows can never
  be renamed into stale manual lines.
- Tests: `tests/test_guideline_marker_guard.py`.

### T16 — Ownership/deprecation documentation
- Files: target-architecture doc — BusinessRule authoritative / SemanticGuidelines
  compatibility-layer statement + known limitations (A2 race, A3 new-KB gap).

## Group 6 — BCS consistency (P1/P2)

### T17 — Planning datasource narrowing live (F1)
- Files: nl_answer_service.py :739 (FD)
- Behavior: pass `datasource_id=kb_id`; scoped rules now narrow in production.
- Tests: `tests/test_ontology_planning_datasource_wiring.py` (capture-kwargs
  style around the answer path, or direct builder param test + wiring assert
  via monkeypatched builder).

### T18 — BCS rule narrowing via agent bindings (F2)
- Files: `backend/app/services/enterprise/business_context.py`
- Behavior: `build_business_context` resolves `agent_id` → AgentApp bound KB
  ids; `_rule_candidates` narrows `datasource_ids`-scoped rules to that set;
  org-wide rules always pass; unresolvable agent → current behavior (never
  widens).
- Tests: extend `tests/test_business_context.py`.

### T19 — Rejected processes excluded (F3) + metric gates (F4)
- Files: business_context.py
- Behavior: `_process_candidates` filters `evidence_status == 'rejected'`;
  `_metric_candidates` adds `is_enabled` + `lifecycle == 'published'` (parity
  with planning).
- Tests: extend `tests/test_business_context.py`.

## Stage 4 — Integration verification
- BusinessRule lifecycle → bridge sync → SemanticStore (existing suites).
- Resource deletion → cleanup → onboarding → watchdog (new T4/T5/T6/T7/T8 suites
  + test_enterprise_cleanup + test_onboarding regression).
- prov-v1: test_canonical_provenance + test_prov_v1_adoption + phase3b suites.
- BCS: test_business_context + test_bcs_runtime_integration + 7B suite.
- Full backend enterprise slice re-run; frontend untouched (no FE changes →
  no FE build needed unless docs/tests only).

## Stage 5 — Documentation
- Append Phase 7C to `zhanlu-enterprise-intelligence-target-architecture.md`;
  update roadmap; memory files; final report.
