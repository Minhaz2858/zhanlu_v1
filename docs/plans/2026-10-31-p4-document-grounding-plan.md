# P4 — Document-Grounded Ontology: Gap Audit + Implementation Plan (v2, REVISED)

**Date:** 2026-10-31 (v2 — revised after review REQUEST CHANGES)
**Status:** PLAN v2 — presented for approval. NO implementation before approval.
**Scope guard:** P4 only. No P5 (computed metrics), P6, P7 work.
**Companion decision record:** `docs/plans/2026-10-31-p4-decision-record.md`
(resolves the four review blockers: file-level document identity, dedicated
human document-approval writer, authorization inheritance rule, atomic
approval).

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

1. **No structured provenance** — page numbers/sheet names exist only as
   in-text markers that the chunker destroys; DOCX has no section provenance.
2. **No document identity/versioning** — `KnowledgeBase` has NO content hash
   or version column; re-ingest is destructive wipe-and-replace with no
   change detection and no invalidation hook for derived artifacts.
3. **No document→ontology path** — nothing reads a document and proposes
   ontology elements; `provenance_json` has never been written from a
   document; no proposal store; `stamp_link` unwired.
4. **No deterministic quote verification** — nothing confirms a quoted span
   exists in the source text.
5. **No document-evidence review surface** — no endpoints, no queue UI.
6. **No per-document authorization beyond org scoping** — access policy is
   table/SQL-oriented; document retrieval doesn't consult it.

## Part B — Architecture (final, per decision record)

```
file KB ingest (PDF/DOCX/XLSX/CSV)                    [one KB = one file — D1, evidence in decision record]
  └─ ingest_kb computes SHA-256 from actual file bytes
      └─ document_versions row appended (append-only history; kb.content_hash = current)   [NEW, migration 107]
  └─ extract_segments()            # NEW: structured {text, location(page|sheet|section)}   [T2]
      └─ chunk_segments()          # NEW: chunks carry location metadata (RAG path untouched) [T3]
          └─ document_grounding.extract_document_evidence(db, kb)   # NEW, flag-gated [T6]
              ├─ candidates: quotes + proposed elements   # LLM step; document text = UNTRUSTED DATA,
              │                                            #   injection-hardened prompt; model-version stamped
              ├─ deterministic verifier                   # normalized containment; ORIGINAL verbatim span stored;
              │     └─ untraceable quote → DROPPED, counted                                    [T5]
              └─ dedup (existing proposals, existing objects, normalized-name match)
                    └─ ontology_document_proposals (status='proposed', audience_json captured)  [T4]
Human review (org-admin, AdminConsole "Doc Evidence" tab)                                   [T7-T9]
  ├─ approve  → ATOMIC unit (single transaction, no internal commits):
  │     create-or-attach element + append doc provenance
  │     + stamp_human_document_approval (NEW evidence.py writer — D2)
  │     + record_revision (P2 savepoint/lock/retry unchanged)
  │     + document decision event (IdentityDecisionEvent pattern)
  │     + AuditEvent + proposal status update
  │   Authorization gate: reviewer must pass the source KB's access check;
  │   audience-unresolvable proposals are NOT promotable (D3)
  ├─ reject   → remembered, never re-proposed
  └─ revise   → admin edits payload, then approve
Re-ingest with changed hash → old proposals flagged `superseded`
(history + decision events preserved; doc_version_id anchors them)          [D1]
```

**Evidence contract (P1-aligned):**
- Proposals are created `proposed` — a verified quote proves TRACEABILITY,
  never semantic correctness; only human approval makes business truth.
- Approve = human authorization via the NEW `stamp_human_document_approval`
  (D2) → element `verified` per P1 semantics, `provenance_json` carrying the
  full document reference; `record_revision` written (P2).
- Machine steps can never set `verified` (P1 guard reused; the new stamp is
  the only document-approval entry point and refuses machine callers).
- Attach-to-existing appends provenance + revision; `verified` definitions
  are NEVER overwritten — conflicts stay in review requiring explicit human
  conflict resolution.

**Every proposal retains (reviewer requirement):** kb_id, doc_version_id
(+ content hash), page/sheet/section location, chunk id, exact verbatim
evidence span (original, un-normalized), extraction method + model version,
extraction timestamp — plus `audience_json` (D3).

### New/changed artifacts

- Migration `107_document_versions`: append-only `document_versions`
  (`org_id, kb_id, content_hash, content_bytes, file_url_at_ingest,
  first_indexed_at, is_current` + RLS `tenant_isolation`, 105 pattern) AND
  `kb.content_hash` current-hash column. SHA-256 from actual file bytes.
- Migration `108_ontology_document_proposals`: proposal table (+ RLS) —
  idempotency `(org_id, kb_id, doc_version_id, chunk_key, proposal_kind,
  quote_hash)`; `source_kb_id` + `audience_json` (D3); `status` ∈
  `proposed|approved|rejected|superseded`.
- `extractors.extract_segments(file)` + `chunker.chunk_segments()` —
  additive; existing `extract_text`/`chunk_text` behavior unchanged.
- `evidence.stamp_human_document_approval` (D2) — evidence.py stays the
  sole evidence writer.
- `services/ontology/document_grounding.py` — extraction + verification +
  proposal persistence (+ per-run counts: proposed / verified_quote /
  dropped_untraceable / duplicates_skipped).
- `services/ontology/document_review.py` — atomic approve/reject/revise +
  decision events + revision integration (D4).
- Runtime authorization gate (D3) in discovery / `read_object_data`
  enrichment / external read API: document-derived elements filtered or
  annotated when the principal lacks source-KB access.
- `routers/ontology.py` — `/ontology/document-evidence/*` endpoints
  (org-admin mutations, literal paths).
- `AdminConsole.jsx` — "Doc Evidence" tab; `EnterpriseProfile.jsx` —
  document provenance on objects (EvidenceBadge reuse).
- Flag: `ONTOLOGY_DOC_GROUNDING_ENABLED` (default False).

## Part C — Bite-sized TDD tasks (revised sequence)

| # | Task | Acceptance condition |
|---|------|----------------------|
| T0 | Resolve document identity + authorization contracts | DONE — decision record: cardinality settled (one KB = one file, evidence cited), inheritance rule defined, atomic approval defined |
| T1 | Document content hashing/versioning (migration 107 + `ingest_kb` computes SHA-256 from file bytes, appends `document_versions`) | Stable file-level identity; hash stable across unchanged re-ingest; differs on content change; old versions preserved; legacy rows NULL |
| T2 | Structured extraction `extract_segments` (pdf pages, docx paragraphs+headings, xlsx sheets, csv rows) | Page/sheet/section provenance correct per type; existing `extract_text` behavior byte-identical (regression) |
| T3 | Location-aware chunking `chunk_segments` | Location propagated; existing RAG behavior preserved |
| T4 | Proposal model + migration 108 (+ RLS + idempotency + `source_kb_id`/`audience_json`) | Org isolation + document identity (doc_version_id) |
| T5 | Deterministic quote verification | Every accepted quote traceable to source segment; ORIGINAL span stored; untraceable dropped + counted |
| T6 | Extraction + dedup pipeline (flag-gated; untrusted-content prompt hardening; LLM step stubbed in tests) | No fabricated evidence; counts reported separately; cross-org KB rejected; dedup no duplicates; conflicts flagged |
| T7 | Human review service (atomic approval; `stamp_human_document_approval`; decision events; revisions; never overwrite verified) | Atomic approval (fail = nothing applied); P1 evidence semantics; P2 revision written; inheritance gate enforced |
| T8 | API + authorization (extract, list, approve/reject/revise, history; org-admin mutations; source-KB access check at approval; runtime visibility gate) | Org-admin mutations; appropriate read access; non-promotable proposals refuse approval with explicit reason |
| T9 | Frontend: AdminConsole "Doc Evidence" tab + EnterpriseProfile provenance (navigable citation: document + page/sheet) | Build green; review shows quotation, location, conflicts, evidence status; approve/reject/revise work |
| T10 | Independent review + E2E | Real documents (PDF/DOCX/XLSX): quote verification, missing evidence, conflicting policies, document update → supersede w/ history preserved, cross-source grounding, authorization, revision history; clean-worktree regression comparison (P3 methodology); progress log + commits |

## Part D — Non-negotiable architectural invariants

1. Zhanlu remains an Agent Harness, not a collection of document-specific agents.
2. No new ontology engine or graph database.
3. Existing RAG remains the shared document retrieval system.
4. Document extraction proposes evidence; it never authorizes business truth.
5. P1 evidence taxonomy remains authoritative (`evidence.py` sole writer).
6. P2 revisions remain append-only and transactional.
7. P3 identity resolution and confirmed crosswalks remain untouched.
8. No hardcoded department-specific schemas or document vocabularies.
9. Every document-derived claim retains source-level provenance.
10. No P5/P6/P7 implementation.

## Part E — Non-goals / guarantees

- No separate document ontology; no RAG duplication (segments feed the SAME
  Chroma store via additive metadata; the ontology reads through proposals).
- P0 authorization preserved (org-admin mutations, org scoping, audit).
- P1 states reused verbatim; automation never writes `verified`.
- P2 revisions written for every approved mutation; rollback works unchanged.
- P3 identity untouched; confirmed crosswalks remain the only cross-source
  runtime surface.
- `knowledge_bases` RLS gap and document-level RAG ACLs are NOT rebuilt in
  P4 (platform-wide concerns; the D3 inheritance rule covers P4-produced
  evidence only — noted for a future governance task).
- No P5/P6/P7 work.
