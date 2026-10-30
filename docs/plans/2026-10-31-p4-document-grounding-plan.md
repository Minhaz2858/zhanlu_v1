# P4 — Document-Grounded Ontology: Gap Audit + Implementation Plan

**Date:** 2026-10-31
**Status:** PLAN — presented for review. NO implementation before approval.
**Scope guard:** P4 only. No P5 (computed metrics), P6, P7 work.

---

## Part A — Evidence-backed gap audit (what exists vs what's missing)

### A1. What already exists (verified in code, reuse — never duplicate)

| Capability | Location | Evidence |
|---|---|---|
| PDF/DOCX/XLSX/CSV/PPTX extractors | `services/document_ingestion/extractors.py` | `extract_text` dispatch (L25); PDF emits `--- page {i+1} ---` markers (L102-110), XLSX emits `## Sheet:` headers (L87-99) — **in-text only** |
| RAG chunk store | `services/document_ingestion/store.py` | Chroma; chunk meta `kb_id, chunk_index, file_name, file_type, title, summary` (L476-502); hybrid dense+lexical+RRF `hybrid_query` (L383-468) |
| Retrieval w/ grading | `services/document_ingestion/retrieval.py` | `search` L30, grading/self-correction `answer` L95-219; citations = `file_name, chunk_index, score` (L211-218) |
| Agentic chunking (title/summary per chunk) | `services/document_ingestion/agentic_rag.py` | wired at `service.py:287-293` |
| BusinessRuleExtractor | `services/db/business_rule_extractor.py` | deterministic, **catalog-only** (`kb_table_meta`/`kb_column_meta`); writes `SemanticGuideline` rows titled `[auto] …`, `is_enabled=False` (L175-228); documents NOT a source |
| Evidence taxonomy (P1) | `services/ontology/evidence.py` | `EVIDENCE_STATUSES = ("verified","computed","inferred","proposed","unavailable")` (L27-35); `stamp_*` writers ONLY; automation can NEVER set `verified` (L116-120 raises); `stamp_link` defined but never called (L142-151) |
| Evidence columns | `models/ontology.py` | `evidence_status/confidence/provenance_json` on Object (L85-90), Link (L106-111), Action (L139-144) |
| Revision registry (P2) | `services/ontology/revisions.py` | `record_revision` (L90-148): savepoint `begin_nested` (L133-140), `with_for_update` row lock (L111-113), retry on revision_no races; `rollback_to_revision` L188 |
| Identity (P3) | `services/identity/` | `resolve_object_identity`, `review_match` (allowed transitions incl. reversals, AuditEvent + `IdentityDecisionEvent`), `crosswalk_summary` (confirmed-only), surfaced at `ontology/data.py::_cross_sources_block` (L225-239) |
| Human review queue pattern | `pages/AdminConsole.jsx` | Identity tab (L273-348): queue + confirm/reject + count badge — the template for a document-evidence tab |
| Governance/audit | `routers/governance.py`, `AuditEvent` | admin-gated approvals, audit trail |
| Org isolation | `models/base.py` org_id/app_id + app-level filters | consistent across ontology/identity tables |

### A2. What is genuinely missing (the P4 gap list)

1. **No structured provenance** — page numbers/sheet names exist only as in-text
   markers that the chunker destroys; DOCX has no section provenance. Chunks
   carry no page/sheet/section metadata.
2. **No document identity/versioning** — `KnowledgeBase` has NO content hash or
   version column; re-ingest is destructive wipe-and-replace with no change
   detection and no invalidation hook for derived artifacts.
3. **No document→ontology path** — nothing reads a document and proposes
   ontology elements; `provenance_json` has never been written from a document;
   no proposal store; `stamp_link` unwired.
4. **No deterministic quote verification** — nothing confirms a quoted span
   exists in the source text.
5. **No document-evidence review surface** — no endpoints, no queue UI.
6. **No per-document authorization beyond org scoping** — access policy is
   table/SQL-oriented; document retrieval doesn't consult it (observation; P4
   keeps org-scoping + admin-gated review, notes the gap, does not fix RAG).

## Part B — Architecture (proposed)

```
file KB ingest (PDF/DOCX/XLSX/CSV)
  └─ extract_segments()            # NEW: structured {text, location(page|sheet)} per segment
      └─ chunk_segments()          # NEW: chunks carry location metadata (RAG path untouched)
          └─ document_grounding.extract_document_evidence(db, kb)   # NEW, flag-gated, post-ingest
              ├─ candidate quotes + proposed elements   # LLM step (monkeypatchable, model-version stamped)
              ├─ deterministic verifier                  # NEW: exact normalized containment in source segment
              │     └─ untraceable quote → DROPPED, counted honestly
              └─ dedup (existing proposals, existing objects, normalized-name match)
                    └─ ontology_document_proposals rows  # status='proposed' (+ conflict note when applicable)
Human review (org-admin, AdminConsole tab like Identity)
  ├─ approve → create/attach ontology element via record_revision + human stamp
  │             (existing verified definitions are NEVER overwritten — evidence is APPENDED)
  ├─ reject  → remembered, never re-proposed
  └─ revise  → admin edits payload, then approve
Re-ingest with changed content hash → proposals from the old hash flagged superseded
```

**Evidence contract (P1-aligned):**
- Proposals are created `proposed` (deterministic verified-quote but no human).
- Approve = human authorization → the resulting ontology element is stamped via
  the human path (`verified` per P1: "directly supported AND human-authorized"),
  with `provenance_json` carrying the full document reference; a
  `record_revision` is written (P2).
- Machine steps can never set `verified` (P1 guard `evidence.py:116-120` reused).
- Attach-to-existing (dedup) appends a provenance entry + `record_revision`;
  `verified` definitions are never silently overwritten — conflicts keep the
  proposal in review with a conflict note.

**Every proposal retains (reviewer requirement):** kb_id, document (kb) id +
content hash, page/sheet location, chunk id, exact verbatim evidence span,
extraction method + model version, extraction timestamp.

### New/changed artifacts

- Migration `107_knowledge_base_content_hash`: `content_hash` + `content_bytes`
  on `knowledge_bases` (computed at ingest; idempotent skip when unchanged).
- Migration `108_ontology_document_proposals`: `ontology_document_proposals`
  (+ RLS `tenant_isolation`, mirrors 105 pattern) — idempotency constraint
  `(org_id, kb_id, chunk_key, proposal_kind, quote_hash)`.
- `extractors.extract_segments(file)` + `chunker.chunk_segments()` — additive;
  existing `extract_text`/`chunk_text` behavior unchanged.
- `services/ontology/document_grounding.py` — extraction + verification +
  proposal persistence.
- `services/ontology/document_review.py` — approve/reject/revise + decision
  events (mirrors P3 `IdentityDecisionEvent` pattern) + revision integration.
- `routers/ontology.py` — `/ontology/document-evidence/*` endpoints (org-admin
  for mutations, literal paths per router convention).
- `AdminConsole.jsx` — "Doc Evidence" tab (mirror of Identity tab);
  `EnterpriseProfile.jsx` — document provenance visible on objects
  (EvidenceBadge reuse).
- Flag: `ONTOLOGY_DOC_GROUNDING_ENABLED` (default False; pipeline opt-in per
  org, consistent with P1/P2 rollout style).

## Part C — Bite-sized TDD tasks

| # | Task | Tests (RED→GREEN) |
|---|------|-------------------|
| T1 | `content_hash`/`content_bytes` on KB (migration 107 + model + ingest computes sha256 at index time; unchanged file → same hash) | hash stable across re-ingest; differs on content change; NULL for legacy rows |
| T2 | `extractors.extract_segments` (pdf pages, docx paragraphs+headings, xlsx sheets, csv rows) — structured `{text, location}`; existing `extract_text` byte-identical behavior | per-type location correctness; csv row locations; docx heading capture; text-path regression |
| T3 | `chunker.chunk_segments` — chunks carry `location` (first segment's location; boundary-safe) | location propagation; cross-segment chunk takes first location |
| T4 | `DocumentProposal` model + migration 108 + RLS + idempotency constraint | create/dedup; org-scoped queries; cross-org invisible |
| T5 | Deterministic quote verifier (`verify_quote(segment_text, quote)` — whitespace-normalized exact containment; untraceable → reject + counter) | exact match; whitespace variants; untraceable; empty quote; huge-span guard |
| T6 | Extraction pipeline `extract_document_evidence` (flag-gated; LLM candidate step stubbed in tests; verifier applied; dedup vs proposals + existing objects; conflict detection) | happy path; untraceable dropped; dedup no duplicates; conflict flagged; flag-off no-op; cross-org KB rejected |
| T7 | `document_review.py` — approve (create-or-attach via `record_revision`, human stamp, never overwrite verified), reject (remembered), revise | approve creates object with revision + verified stamp + doc provenance; attach-to-existing appends provenance + revision; verified-definition overwrite refused; reject remembered; revise→approve flow; decision events appended |
| T8 | Router endpoints (extract, list w/ status filter, approve/reject/revise, history; org-admin mutations; literal paths) | authz (non-admin 403), 404s, org scoping, history |
| T9 | Frontend: AdminConsole "Doc Evidence" tab + EnterpriseProfile provenance surfacing | build green; manual check |
| T10 | Full regression (clean-worktree baseline vs closure, same methodology as P3), live verification with REAL documents (PDF + DOCX + XLSX: quote verification, missing-evidence, conflicting policy, document update → supersede), progress log + commit | 0 P3-attributable regressions; live evidence recorded |

## Part D — Non-goals / guarantees

- No separate document ontology; no RAG duplication (segments feed the SAME
  Chroma store via additive metadata; the ontology reads through proposals).
- P0 authorization preserved (org-admin mutations, org scoping, audit).
- P1 states reused verbatim; automation never writes `verified`.
- P2 revisions written for every approved mutation; rollback works unchanged.
- P3 identity untouched; confirmed crosswalks remain the only cross-source
  runtime surface.
- `knowledge_bases` RLS gap and document-level access policy are NOT fixed in
  P4 (platform-wide concerns; noted for a future governance task).
- No P5/P6/P7 work.
