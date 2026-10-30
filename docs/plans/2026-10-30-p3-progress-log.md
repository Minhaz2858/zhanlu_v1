# P3 — Cross-source Identity Resolution: Progress Log

**Date:** 2026-10-30
**Status:** IMPLEMENTED & LIVE-VERIFIED — presented for review (not yet accepted)
**Plan:** `docs/plans/2026-10-30-p3-identity-resolution-plan.md`
**Branch base:** P2 accepted (`70cd60dc9` impl, `0ca4388a9` review fixes, `bda7c79a0` docs)

## What shipped

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
- `tests/test_identity_resolver.py` — 8 (tier1 prefix exclusion, tier2
  evidence+direction, tier2 no-evidence, tier3 review queue, tier4 weak
  signal, rejection memory, bounded sampling).
- `tests/test_identity_review.py` — 5 (org-scoped list, confirm/reject audit,
  unknown/bad-decision errors, cross-org guard).
- `tests/test_identity_crosswalk_runtime.py` — 4 (summary confirmed+org-scope,
  empty case, read enrichment, no-block-when-absent).
- `tests/test_identity_row_staging_hook.py` — 4 (ingest_kb stages rows,
  re-stage endpoint, non-tabular 400, unknown 404).
- Fixed a P2 regression found during regression: `test_ontology_core` needed
  `app.models.ontology_revision` imported for `create_all`.

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
