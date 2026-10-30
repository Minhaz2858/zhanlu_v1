# P4 — Document-Grounded Ontology: Gap Audit + Implementation Plan (v3, FINAL)

**Date:** 2026-10-31 (v3 — final corrections after second review)
**Status:** PLAN v3 — presented for final approval. NO implementation before approval.
**Scope guard:** P4 only. No P5 (computed metrics), P6, P7 work.
**Companion decision record:** `docs/plans/2026-10-31-p4-decision-record.md`
(D1-D4: file-level identity, dedicated approval writer, inheritance rule,
atomic approval — D5-D7: immutable versions, FAIL-CLOSED authorization
function, per-provenance-entry authorization).

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
      └─ document_versions row APPENDED — rows are IMMUTABLE, never updated/deleted      [D5, migration 107]
      └─ current pointer: kb.content_hash + kb.content_version_id (on knowledge_bases)   [D5]
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
  │   Authorization gate: assert_document_kb_access (D6, FAIL-CLOSED) on the
  │   source KB for the reviewer; audience-unresolvable proposals are NOT
  │   promotable (D3)
  ├─ reject   → remembered, never re-proposed
  └─ revise   → admin edits payload, then approve
Re-ingest with changed hash → new immutable version row; old proposals
flagged `superseded` — history + decision events preserved (doc_version_id
anchors them)                                                                [D1/D5]
Runtime disclosure (discovery / read_object_data enrichment / external API):
  PER-PROVENANCE-ENTRY filtering via the D6 fail-closed gate — each
  document-derived provenance entry carries {source_kb_id, doc_version_id,
  content_hash, location, quote, reviewer, decision, audience_snapshot};
  restricted entries are hidden even when the object's other entries are
  visible; a global `verified` status is NEVER a disclosure license.         [D7]
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

- Migration `107_document_versions`: **immutable** `document_versions`
  (`id, org_id, kb_id, content_hash, content_bytes, file_url_at_ingest,
  first_indexed_at` + RLS `tenant_isolation`, 105 pattern — NO mutable
  status column, D5) + current pointer columns on `knowledge_bases`
  (`content_hash`, `content_version_id`). SHA-256 from actual file bytes.
- Migration `108_ontology_document_proposals`: proposal table (+ RLS) —
  idempotency `(org_id, kb_id, doc_version_id, chunk_key, proposal_kind,
  quote_hash)`; `source_kb_id` + `audience_json` (D3); `status` ∈
  `proposed|approved|rejected|superseded`.
- `extractors.extract_segments(file)` + `chunker.chunk_segments()` —
  additive; existing `extract_text`/`chunk_text` behavior unchanged.
- `evidence.stamp_human_document_approval` (D2) — evidence.py stays the
  sole evidence writer.
- `services/ontology/doc_access.py::assert_document_kb_access` (D6) — NEW
  FAIL-CLOSED gate. The existing `resolve_user_policies`
  (`access_policy_service.py:100-165`) FAILS OPEN (no rows / missing
  principal / resolution error → `has_policies=False` = full access), so it
  CANNOT be reused as-is for disclosure decisions; the new gate inverts the
  default at this boundary while using it for evaluation. **Two distinct
  permissions**: administrative (org-admin — review/manage proposals) vs
  source-document access (D6 gate) — approval requires BOTH; runtime
  disclosure enforces source-document access for everyone, admins included.
  Five-state semantics: `EXPLICITLY_AUTHORIZED` (owner / policy allow /
  org-unrestricted for an authenticated member — basis recorded) → ALLOW;
  `EXPLICITLY_DENIED`, `NO_APPLICABLE_POLICY`, `MISSING_PRINCIPAL`
  (API-key principals get no document-derived content),
  `RESOLUTION_FAILURE` → DENY. `has_policies=False` is NEVER treated as
  proof of access.
- `services/ontology/document_grounding.py` — extraction + verification +
  proposal persistence (+ per-run counts: proposed / verified_quote /
  dropped_untraceable / duplicates_skipped).
- `services/ontology/document_review.py` — atomic approve/reject/revise +
  decision events + revision integration (D4). Provenance ENTRIES carry
  `{source_kb_id, doc_version_id, content_hash, location, quote, reviewer,
  decision, audience_snapshot}` (D7) — per-entry authorization.
- Runtime authorization gate (D6/D7) in discovery / `read_object_data`
  enrichment / external read API: PER-ENTRY filtering of document-derived
  provenance; fail-closed (unknown → hidden); API-key principals get no
  document-derived content by default.
- `routers/ontology.py` — `/ontology/document-evidence/*` endpoints
  (org-admin mutations, literal paths).
- `AdminConsole.jsx` — "Doc Evidence" tab; `EnterpriseProfile.jsx` —
  document provenance on objects (EvidenceBadge reuse; per-entry
  visibility).
- Flag: `ONTOLOGY_DOC_GROUNDING_ENABLED` (default False).

## Part C — Bite-sized TDD tasks (revised sequence)

| # | Task | Acceptance condition |
|---|------|----------------------|
| T0 | Resolve document identity + authorization contracts | DONE — decision record D1-D7: cardinality settled (one KB = one file, evidence cited), inheritance + fail-closed gate defined, atomic approval defined, per-entry authorization defined |
| T1 | Document content hashing/versioning (migration 107: immutable `document_versions` + current pointer on `knowledge_bases`; `ingest_kb` computes SHA-256 from file bytes, appends version rows) | Stable file-level identity; **unchanged re-ingest → NO new version** (scenario 1); **changed content → NEW immutable version** (scenario 2); version rows never updated/deleted; legacy rows NULL |
| T2 | Structured extraction `extract_segments` (pdf pages, docx paragraphs+headings, xlsx sheets, csv rows) | Page/sheet/section provenance correct per type; existing `extract_text` behavior byte-identical (regression) |
| T3 | Location-aware chunking `chunk_segments` | Location propagated; existing RAG behavior preserved |
| T4 | Proposal model + migration 108 (+ RLS + idempotency + `source_kb_id`/`audience_json`/`doc_version_id`) | Org isolation + document identity; **old proposals + decision history remain accessible after superseding** (scenario 3) |
| T5 | Deterministic quote verification | Every accepted quote traceable to source segment; ORIGINAL span stored; untraceable dropped + counted |
| T6 | Extraction + dedup pipeline (flag-gated; untrusted-content prompt hardening; LLM step stubbed in tests) | No fabricated evidence; counts reported separately; cross-org KB rejected; dedup no duplicates; conflicts flagged |
| T7 | Human review service (atomic approval; `stamp_human_document_approval`; per-entry provenance authorization (D7); decision events; revisions; never overwrite verified) | Atomic approval — **an approval failure rolls back EVERY mutation** (scenario 6) and **an audit failure cannot leave a partially approved proposal** (scenario 7); **machine-generated evidence can never become `verified`** (scenario 8); restricted entries independently protected (scenario 5 groundwork) |
| T8 | API + authorization (`doc_access.assert_document_kb_access` FAIL-CLOSED, five-state; admin ≠ document-reader — approval requires BOTH permissions; extract, list, approve/reject/revise, history; runtime per-entry visibility gate) | **A restricted document cannot leak through ontology discovery** (scenario 4); different-audience documents on one object independently protected (scenario 5); an org-admin WITHOUT source-document permission can neither approve nor see the restricted entries; non-promotable proposals refuse approval with explicit reason; API-key principals get no document-derived content by default |
| T9 | Frontend: AdminConsole "Doc Evidence" tab + EnterpriseProfile provenance (navigable citation: document + page/sheet; per-entry visibility) | Build green; review shows quotation, location, conflicts, evidence status; approve/reject/revise work |
| T10 | Independent review + E2E | Real documents (PDF/DOCX/XLSX): quote verification, missing evidence, conflicting policies, document update → supersede w/ history preserved, cross-source grounding, authorization, revision history, all eight binding scenarios above; clean-worktree regression comparison (P3 methodology); progress log + commits |

### Binding regression scenarios (from final review §3)

1. Re-ingesting unchanged content creates no new document version. (T1)
2. Changed content creates a new immutable version. (T1)
3. Old proposals and decision history remain accessible after superseding. (T4/T7)
4. A restricted document cannot leak through ontology discovery. (T8)
5. Two documents with different audiences attached to one ontology object
   remain independently protected. (T7/T8)
6. An approval failure rolls back every associated mutation. (T7)
7. An audit failure cannot leave a partially approved proposal. (T7)
8. Machine-generated evidence cannot become `verified`. (T7)

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
