# P3/P3a Cross-source Identity Resolution — Implementation Plan

> **STATUS: IMPLEMENTED & VERIFIED 2026-10-30** — see `docs/plans/2026-10-30-p3-progress-log.md`.

**Inspected foundations (2026-10-30):**
- `knowledge_graph/join_edge_detector.py` — type-bucketed inverted-index VALUE_OVERLAP with containment scoring + `min_shared` threshold (the blocking analog; pure functions).
- Read-only DB access: `QueryService.execute(kb_id, sql, max_rows, params)` (org-checked, row-capped) — used by `_read_via_kb` (`ontology/data.py:204`).
- Spreadsheet reality: `document_ingestion/extractors.py` converts xlsx/csv to TEXT chunks only; `KnowledgeBase` (`source_kind='file'`, `file_url`, `file_type`) has NO structured row store → **P3a is a genuine prerequisite**.
- Safe file-path resolution exists: `document_ingestion/service.py::_resolve_local_path` (upload-root confinement, traversal-proof).
- `ontology/data.py::read_object_data` — the runtime integration point (currently no cross-source awareness).

## P3a — structured row staging (prerequisite)

Migration `105` adds `file_table_schemas` (org, kb_id, sheet_name, columns_json [{name, inferred_type}], row_count, parsed_at) and `file_table_rows` (org, kb_id, sheet_name, row_idx, values_json).
`services/identity/file_rows.py::ingest_structured_rows(db, kb, max_rows=10_000)`:
- resolves the stored file via `_resolve_local_path`; xlsx via openpyxl (per worksheet), csv via csv reader;
- header = first non-empty row; rows stored as `values_json` dicts (str keys = header names); bounded by max_rows (honest truncation flag in schema row);
- column type inference: int → float → string (sampled);
- idempotent per (kb, sheet): re-ingest replaces that sheet's rows;
- hook: invoked after successful RAG indexing of file KBs with tabular `file_type` (xlsx/xls/excel/csv) + `POST /api/knowledge-bases/{id}/ingest-rows` (org-admin).
- Row-level accessor `fetch_source_values(db, org, source, column, limit)` — DB source: `SELECT DISTINCT` via QueryService; file source: staged rows. NEVER mutates sources.

## P3 — identity matching (`services/identity/resolver.py`)

`identity_matches` (migration 105, RLS): org, object_id, `source_a`/`source_b` JSON ({kind: 'db'|'file', kb_id, table|sheet, pk_column}), `pk_a`, `pk_b` (row identifiers), `match_tier` (1-4), `normalization_rule_json` NULL, `confidence` NULL, `status` `confirmed|inferred|rejected`, `evidence_json`, `reviewed_by/at`. UniqueConstraint (object_id, source_a, pk_a, source_b, pk_b).

**Four strictly separated tiers** (`resolve_object_identity(db, org, obj, source_a, source_b, max_rows=500, max_matches=500)`):
1. **Exact** — lossless normalization only (trim, case-fold, int↔str) → `confirmed`, `reviewed_by='system:exact'`, evidence records the compared column pair + counts. `1001` vs `C1001` NEVER matches here.
2. **Evidence-backed normalization** — for UNMATCHED residuals only, a SMALL closed set of transforms (`strip_alpha_prefix`, `strip_leading_zeros`, `numeric_suffix`) is applied to one side; the transform is valid ONLY if the transformed value-set containment vs the other side ≥ 0.9 with ≥ `min_evidence_pairs` (3) shared pairs. Rule + containment stats recorded in `normalization_rule_json`; re-validated against live samples on each run; auto-links `confirmed` with `reviewed_by='system:normalization'`. **No rule, no match** — never transform on assumption.
3. **Semantic** — non-ID attribute agreement: for residual rows, name-like attributes embedded (existing `get_embeddings_batch`) compared per row; requires ≥2 attribute agreements AND cosine ≥ 0.85 → `inferred` (human review).
4. **Probabilistic** — single-attribute similarity 0.6–0.85 → `inferred`, low confidence, review queue.
- Rejection memory: pairs with status `rejected` (or `confirmed`) are never re-proposed by the same method — the resolver skips any existing match row for the pair unless force-recheck.
- Bounded: sampled values capped (500/side), matches capped per run, read-only sources, one LLM/embedding budget guard.

**Governance/runtime integration:**
- `POST /ontology/identity/resolve` (org-admin), `GET /ontology/identity/matches?object_id=&status=`, `POST /ontology/identity/matches/{id}/confirm|reject` (org-admin; AuditEvent `policy_violation`-style entry with `event_type='identity_review'` — added to the audit vocabulary; P2 revisions are NOT written for match decisions because matches are crosswalks, not ontology elements — documented).
- `read_object_data` enrichment: objects with confirmed crosswalks get an honest `cross_sources` summary block (which sources, how many confirmed pairs, sample) — no per-row joins in v1, no fabrication.

## Tasks
1. **T1 (RED)** P3a tests: xlsx + csv ingestion, schema inference, bounds, idempotent re-ingest, traversal-safe path.
2. **T2** Migration `105` + models (`FileTableSchema`, `FileTableRow`, `IdentityMatch`) → GREEN P3a.
3. **T3 (RED)** Resolver tests: tier separation (exact ≠ normalization), evidence-backed rules, rejection memory, bounds, org scoping.
4. **T4** Resolver GREEN (+ embeddings tier).
5. **T5 (RED)** Endpoints + audit + runtime enrichment tests → GREEN.
6. **T6** Frontend identity review section.
7. **T7** Full regression + review + live verification (two-source fixture through the real API) + progress log.

## Non-negotiables honored
No merging/copying of source records (crosswalk rows only); no automatic transformation without recorded evidence; tiers 3–4 human-gated; rejections remembered; confirm/reject org-admin + audited; bounded read-only candidate generation; P0/P1/P2 untouched.
