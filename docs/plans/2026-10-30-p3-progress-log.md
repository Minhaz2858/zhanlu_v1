# P3 — Cross-source Identity Resolution: Progress Log

**Date:** 2026-10-30
**Status:** IMPLEMENTED, CLOSURE PASS COMPLETE — presented for acceptance review
**Plan:** `docs/plans/2026-10-30-p3-identity-resolution-plan.md`
**Commits:** `c50fa034b` (P3 impl), `2839e1c30` (closure C1+C2+C3 fix)

## What shipped

## CLOSURE PASS (2026-10-30, review-driven) — commit `2839e1c30`

### C1 — confirmation semantics corrected

The first implementation auto-confirmed tiers 1-2 (`reviewed_by='system:exact'`
/ `'system:normalization'`). That conflated system-generated MATCHING EVIDENCE
with a human-confirmed IDENTITY DECISION. Corrected contract (all four tiers):

| Tier | Status after resolve | Confidence | Human gate |
|------|---------------------|------------|------------|
| 1 exact | `inferred` | 1.0 | confirm to activate |
| 2 normalization | `inferred` | containment | confirm to activate |
| 3 semantic | `inferred` | cosine | **requires review** |
| 4 probabilistic | `inferred` | cosine | **requires review** |

- `reviewed_by` is now reserved for HUMANS (system runs leave it NULL).
- Only `review_match` (org-admin) sets `confirmed`, which is the ONLY status
  the runtime exposes: `crosswalk_summary` / `read_object_data.cross_sources`
  surface confirmed pairs exclusively — verified by a dedicated
  activation-gate test (`test_tier1_and_2_not_active_until_human_confirm`).
- No-op re-review (same decision) is rejected with a validation error.
- Documented in resolver/review/endpoint docstrings + tested.

### C2 — append-only identity decision history

New `identity_decision_events` table (added to migration `105` in place —
not yet applied via alembic anywhere; see Environment note):

- One append-only row per decision: `created` (system), `confirm`, `reject`,
  and REVERSALS (`confirmed→rejected`, `rejected→confirmed`).
- Each event preserves: previous decision, new decision, reviewer identity,
  matching method (tier), confidence, evidence JSON, source record
  identifiers (`source_a_key/source_b_key/pk_a/pk_b`), timestamp; sequence
  number enforced org-scoped (`uq_identity_decision_event_seq`).
- Reconstructable via `decision_history` +
  `GET /api/ontology/identity/matches/{id}/history` (org-scoped; cross-org
  raises → 404).
- The unaudited `mark_review` shortcut was REMOVED — every mutation of
  `IdentityMatch.status` flows through the audited `review_match`
  (regression-tested: `test_no_unaudited_decision_path_survives`).
- Rationale for a dedicated table vs `ontology_revisions`: identity matches
  are crosswalks, not ontology elements (P2 revisions are element-scoped);
  forcing them into `ontology_revisions` would break the P2 entity model.
  The platform `identity_review` AuditEvent is ALSO still written per
  decision — both trails coexist (domain history + platform audit).

### C3 — DB-to-file live integration fixture (found a real bug)

Live fixture: `p3_live_crm.customers` in the zhanlu-mysql container
(:3308, C-prefixed ids) as a DB-source KB vs the uploaded `p3_crm.xlsx`
(bare ids) as a file-source KB, resolved for the bound ontology object.

1. Staging/access: file rows from staging; DB rows through `QueryService`
   (org-checked, row-capped) against live MySQL. ✓
2. Candidate generation + matching: tier 2 fired `tier2: 4` with the
   recorded `strip_alpha_prefix, b_to_a, containment 1.0, shared_pairs 4`
   rule. ✓
3. Human confirmation (new semantics): confirm → `confirmed,
   admin@zhanlu.dev`; decision history shows `created (None→inferred)` then
   `confirm (inferred→confirmed)` with reviewer/tier/confidence/evidence. ✓
4. Runtime provenance: `read_object_data` on the bound object returned 4
   live rows from MySQL PLUS the `cross_sources` block listing both active
   crosswalks (file↔file 3 confirmed pairs, file↔db 1 confirmed pair). ✓
5. Org isolation: moving the DB KB to `other-org` → resolve returns all
   zeros (no candidates cross-org); reverted after the probe. ✓

**Bug found and fixed (commit `2839e1c30`):** on drifted-schema deployments,
`execution_gate.enforce` swallows its own internal DB errors — but those
failures abort the SHARED request transaction, and the next resolver
statement died with `InFailedSqlTransaction` (500). Fix: after each
DB-source read, the resolver probes the transaction and rolls back if
poisoned (reads are read-only; the resolver commits only after all
fetches). Regression-tested with a real SQLite DB source and a poisoned
gate (`test_db_source_gate_poisoning_does_not_abort_resolution`).

### C4 — defensible regression comparison

Methodology: identical command
(`pytest tests/ -q --timeout=90 -n 4`) in clean git worktrees — baseline at
the pre-P3 commit `bda7c79a0` (P2 accepted state), comparison at closure
commit `2839e1c30`:

| Run | passed | failed | errors |
|-----|--------|--------|--------|
| baseline `bda7c79a0` | 10,307 | 771 | 148 |
| P3 closure `2839e1c30` | 10,356 | 790 | 114 |

- **+49 passed, −34 errors.**
- 27 tests failed at HEAD but passed at baseline — **every one passes on a
  serial re-run at HEAD** (26 individually + the 27th's whole file 4/4):
  xdist worker-distribution flakes, not P3 regressions. Zero P3-attributable
  failures.
- 130 tests that failed at baseline pass at HEAD (incl. the P2 regression
  fix: `test_ontology_core` now imports `ontology_revision` for
  `create_all`).
- A 46-test "regression" list produced by comparing against the MAIN TREE
  (which carries another session's uncommitted in-flight changes to
  config.py, synexia/*, nl2sql/*, db_tools, delegation_tools,
  access_policy_service, …) was run at the clean HEAD worktree: **46/46
  pass** — those failures belong to the unrelated in-flight work, not P3.

### C5 — migration 105 verification

- **The repo's alembic chain does not build from scratch** — `alembic
  upgrade head` on an empty database fails at an early migration
  (`intelligence_events RENAME relevance_to_sample_market`, ~051: the
  rename assumes a column no prior migration creates). Pre-existing, NOT
  concealed, NOT fixed here (out of P3 scope).
- 105 itself verified on a CLEAN database stamped at the accepted P2 head
  (`104_ontology_revisions`): `alembic upgrade 105_identity_resolution` →
  `alembic_version = 105_identity_resolution`; all 4 tables exist;
  RLS **enabled AND forced** on all 4 with `tenant_isolation` policy (ALL);
  all expected indexes present (incl. `ix_file_table_rows_kb_sheet`
  composite, `ix_identity_matches_status`, decision-event indexes); both
  unique constraints present (`uq_identity_match_pair`,
  `uq_identity_decision_event_seq`).
- Chain topology: the pre-existing `106_schema_classification` declares
  `down_revision = "105_identity_resolution"` — the chain links cleanly
  through this migration.
- Local dev `zhanlu` DB: its `alembic_version` is `095_ontology_core`,
  which matches NO revision in this repo (stamped by a foreign lineage) —
  the P3 tables + RLS were applied there via idempotent SQL mirroring
  migration 105 (documented workaround, consistent with prior practice on
  this deployment).

## Original implementation notes

### P3a — structured spreadsheet ingestion (prerequisite)

- `backend/app/services/identity/file_rows.py` — `ingest_structured_rows`,
  `ingest_tabular_kb`, `is_tabular`. Header = first non-empty row; every later
  row becomes a `values_json` dict; type inference int→float→string (any float
  presence → float, int+float mix included); all values coerced to str for
  uniform cross-source comparison; bounded (`max_rows` default 10 000) with an
  honest `truncated` flag; idempotent per (kb, sheet) via delete+rewrite;
  file paths resolved through the upload-root-confined traversal-proof helper.
- Staging tables: `file_table_schemas` / `file_table_rows` (migration `105`,
  `org_id`-scoped, RLS `tenant_isolation` policies).
- Wiring: `ingest_kb` (document ingestion service) now ALSO stages rows for
  tabular KBs (non-fatal); explicit re-stage endpoint
  `POST /apps/{app_id}/knowledge_bases/{kb_id}/ingest-rows`.

### P3 — resolver (four strictly separated tiers)

- `backend/app/services/identity/resolver.py` — `resolve_object_identity`
  (bounded, read-only, per-object, two sources):
  - **Tier 1 exact** — lossless only (trim/case-fold/int↔str). `1001` never
    matches `C1001`. Auto-`confirmed`, `reviewed_by='system:exact'`.
  - **Tier 2 normalization** — closed transform set (`strip_alpha_prefix`,
    `strip_leading_zeros`, `numeric_suffix`), EITHER direction (rule records
    `direction`), valid ONLY when transformed value-set containment ≥ 0.9
    AND ≥ 3 shared pairs, re-validated each run. No rule → no match.
  - **Tier 3 semantic** — embedding similarity over EXPLICIT caller-supplied
    `attribute_pairs` (never silently embeds arbitrary columns); requires
    ≥ 2 attribute agreements AND cosine ≥ 0.85 → `inferred`.
  - **Tier 4 probabilistic** — cosine ≥ 0.60 (below semantic gate) → `inferred`.
  - Rejection memory: `_record_match` never overwrites an existing decision;
    dedup query AND unique constraint are org-scoped
    (`uq_identity_match_pair (org_id, object_id, source_a_key, pk_a,
    source_b_key, pk_b)`).
  - Org wall: file staging reads and DB reads are org-checked; cross-org is
    an empty result, never a leak.

### Governance / API

- `backend/app/services/identity/review.py` — `list_matches` (org-scoped,
  status filter), `review_match` (confirm/reject, audited as
  `identity_review`), `crosswalk_summary` (CONFIRMED pairs only — provenance
  block, no per-row joins).
- `backend/app/routers/ontology.py` (no router prefix — literal paths):
  - `POST /api/ontology/identity/resolve` (org-admin)
  - `GET  /api/ontology/identity/matches?object_id=&status=`
  - `POST /api/ontology/identity/matches/{id}/confirm` (org-admin, audited)
  - `POST /api/ontology/identity/matches/{id}/reject` (org-admin, audited)
- P2 revisions are intentionally NOT written for match decisions — matches
  are crosswalks, not ontology elements (documented decision).

### Runtime integration

- `read_object_data` (both platform-table and kb-backed reads) now appends an
  honest `cross_sources` block when CONFIRMED crosswalks exist for the
  object: which sources, how many pairs, a small sample. Enrichment failure
  degrades silently — never blocks a read. Inferred/rejected never appear.

### Frontend

- `frontend/src/pages/AdminConsole.jsx` — new **Identity** tab: review queue
  with tier chip, confidence, `pk_a ≡ pk_b`, source keys, expandable evidence
  JSON, Confirm/Reject (inferred rows only). Count badge = pending queue.

## Deviations from the plan (and why)

1. `attribute_columns` → **`attribute_pairs`** `[(col_in_a, col_in_b), ...]`:
   source column names differ across systems (`account_name` vs
   `customer_name`); a single side-list silently compared against missing
   keys. Explicit pairing is the honest contract.
2. Tier 3/4 run ONLY with explicit `attribute_pairs` (no auto-derivation):
   silently embedding arbitrary columns (free-text notes, timestamps) is not
   defensible in an identity decision.
3. Migration `105` edited in place (not yet applied via alembic anywhere —
   see "Environment note" below).

## Testing (TDD)

- `tests/test_identity_file_rows.py` — 7 (P3a: xlsx/csv, idempotent re-ingest,
  bounds, traversal rejection, cross-org rejection).
- `tests/test_identity_resolver.py` — 9 (tier1 prefix exclusion, tier2
  evidence+direction, tier2 no-evidence, tier3 review queue, tier4 weak
  signal, rejection memory, bounded sampling, human-only activation gate,
  gate-poisoning recovery with a real DB source).
- `tests/test_identity_review.py` — 5 (org-scoped list, confirm/reject audit,
  unknown/bad-decision errors, cross-org guard).
- `tests/test_identity_decision_history.py` — 6 (created/confirm/reject
  events, reversal, no-op re-review rejection, org-scoped history, no
  unaudited path).
- `tests/test_identity_crosswalk_runtime.py` — 4 (summary confirmed+org-scope,
  empty case, read enrichment, no-block-when-absent).
- `tests/test_identity_row_staging_hook.py` — 4 (ingest_kb stages rows,
  re-stage endpoint, non-tabular 400, unknown 404).
- `tests/test_ontology_core.py` — P2 regression fix (import
  `app.models.ontology_revision` for `create_all`).
- **48/48 green at the clean closure commit** (35 identity + 13 ontology
  core/review-adjacent).

## Live verification (local backend :8123, local Postgres `zhanlu`)

1. Two xlsx KBs (`p3_crm.xlsx` bare ids / `p3_master.xlsx` `C`-prefixed ids,
   identical attributes) staged through the live `ingest-rows` endpoint
   (4 rows each).
2. `POST /api/ontology/identity/resolve` → `tier2: 4` with recorded rule
   `{transform: strip_alpha_prefix, direction: b_to_a, containment: 1.0,
   shared_pairs: 4}`; tier1=0 (nothing exact — correct); skipped_existing=0.
3. `GET matches` shows `normalization/confirmed`, per-match evidence with
   original→transformed values.
4. `POST .../confirm` → `confirmed, admin@zhanlu.dev` + `identity_review`
   AuditEvent (decision, match_id, tier, pks, source keys).
5. `POST .../reject` on another match → `rejected, admin@zhanlu.dev`.
6. Re-run resolve → `skipped_existing: 4`; rejected row stays `rejected`
   with reviewer preserved (decision memory, no re-proposals).

## Environment note

The local Postgres `zhanlu` alembic lineage is stuck at `095_ontology_core`
(chain diverged; `alembic upgrade head` fails with "Can't locate revision").
The three P3 tables were created idempotently with SQL mirroring migration
`105` (incl. RLS policies); pre-existing schema drift
(`knowledge_bases.nl2sql_state` et al.) was bridged with idempotent
`ALTER TABLE ADD COLUMN` — the established workaround for this deployment.
This does not affect deployments with a clean alembic chain.

## Known follow-ups (not P3 scope)

- `knowledge_bases` uploads of tabular files should prefer `ingest-rows`
  status in the KB chips UI (backend ready).
- DB-source identity resolution exists (`QueryService` route, org-checked)
  but has no live-verified fixture yet (needs a bound tenant DB — P4-adjacent).
- Crosswalk-aware filtering (per-row join across sources) — explicitly
  deferred to a future phase (v1 is provenance-only).
