# P4 — Document-Grounded Ontology: progress log

Branch: `fix/automation-chat-artifacts-ordering` · Plan: `2026-10-31-p4-document-grounding-plan.md` (v3 FINAL) · Decisions: `2026-10-31-p4-decision-record.md` (D1–D7)

## Status: T0–T10 COMPLETE (2026-10-31). P5/P6/P7 untouched (non-goals honored).

| Task | Commit | What |
|---|---|---|
| T0 | 5bb1b715c / aa9fba20b / 5de97ff4c | Plan v3 + decision record D1–D7 (3 review rounds) |
| T1 | c8a579aad | Migration 107: immutable `document_versions` + current pointer on `knowledge_bases` (`content_hash`, `content_version_id`, FK `use_alter=True` for the cycle); SHA-256 from file bytes; unchanged re-ingest → no new version |
| T2/T3 | 111ab0a97 | `extract_segments` (pdf pages / docx sections / xlsx sheets / csv rows) + `chunk_segments` (location-aware); `chunk_text`/`extract_text` untouched |
| T4 | 64714c3fe | Migration 108 + `DocumentProposal` (idempotency `(org, kb, doc_version, chunk, kind, quote_hash)`; `source_kb_id` + `audience_json` D3) |
| T5 | 575c1b2a1 | Deterministic quote verifier: whitespace-normalized containment → ORIGINAL verbatim span + offset mapping; no LLM, no thresholds |
| T6 | e944226d4 | `document_grounding.py`: extraction → kind whitelist → verification → dedup → conflict detection → `DocumentProposal(proposed)`; injection-hardened prompt; flag `ONTOLOGY_DOC_GROUNDING_ENABLED` (default OFF) |
| T7 | b0d97c313 | Atomic review: `document_review.review_proposal` (create-or-attach + D7 entry + D2 stamp + revision + decision event + audit + status — ONE transaction, rollback on any failure) + migration 109 `ontology_document_decision_events`; 23 tests |
| T8 | 5b3860280 | Fail-closed `doc_access` (five states; has_policies=False is NEVER access; admin ≠ document-reader; explicit deny wins even on org-unrestricted KBs) + `/api/ontology/document-evidence/*` + D7 per-entry runtime filtering (serialize default-strip; revision-feed scrub after review BLOCKER) |
| T9 | 4db9daf6b | AdminConsole "Doc Evidence" tab + EnterpriseProfile per-entry document citations (backend pre-filters per viewer) |
| T10 | (this commit) | Real-document E2E (TXT/DOCX/XLSX via real extractors; only the LLM step stubbed with verbatim-from-real-chunk quotes); rejection memory across versions (`rejected_skipped`); `GET /ontology/domains` (review caught its absence — the tab 404 would have blanked the console); clean-worktree comparison 88→88 zero regressions |

## Independent review rounds (all findings dispositioned)
- **T7 review**: no blockers → hardened (proposal row FOR UPDATE, org-scoped version read, audience-org equality, reviewer required on every decision, explicit name-clash refusal, revision-row rollback test, end-to-end machine-caller test).
- **T8 review**: 1 BLOCKER (revision history leaked document provenance → `_scrub_revision`) + majors fixed (explicit-deny on org-unrestricted path, object-level reviewer keys removed into entries).
- **T10 review**: 1 BLOCKER (`/ontology/domains` missing → fixed with a thin endpoint), dead `handleDocApprove` removed, honest `rejected_skipped` counter, PDF E2E noted as covered-by-extractor-parity (same segment shape; txt/docx/xlsx verified; pdf path exercised by existing extractor tests).

## Design interpretation on record (flagged for sign-off)
Once an org-admin approves a proposal, the resulting DEFINITION (name/description) is org-wide business truth and stays readable by org members who cannot read the source document. D7's per-entry model hides the provenance entry (quote/location/source/audience) — not the human-authorized claim. Approval IS the explicit human conflict resolution (extraction never auto-merges; verified definitions are never overwritten, corroborating documents append entries).

## Known-opens / notes
- Live LLM E2E (real model in the loop) not run — the pipeline's LLM seam (`_llm_propose` → `chat_completion_json_sync`) is the only stubbed step; wire it behind the flag for a live pass when a staging key is available.
- Conflict detection is O(objects) per candidate in Python — fine at current scale; move normalized-name matching into SQL if orgs grow past ~10⁴ objects.
- Full-suite xdist runs are currently broken by an UNCOMMITTED foreign conftest rework ("T1 test-database isolation") — session-scoped DATABASE_URL assertion races under xdist (11k setup errors; serial runs fine). Not P4's; do not "fix" here.
- Pre-existing foreign failures (not P4): forecast flag test (`FORECAST_OILCHEM_EXOG_ENABLED`), `test_live_events` erp-table-names, `harness_shadow` intent_router, `weekly_digest`, frontend Chat-steer + Sidebar tests.

## Verification evidence
- P4 ring: 139/139 (`document_versions/segments/chunking/proposals/quote_verifier/grounding/review/access` + ontology core/governance/evidence/revisions/batch/sensing/policies-router).
- Real-document E2E: 6/6 (`test_document_e2e.py`).
- Clean-worktree comparison: pre-P4 commit `5de97ff4c` vs HEAD on the 7 shared suites → 88 passed = 88 passed (zero P4-caused regressions).
- Frontend: `vite build` green; 1047 tests pass; 27 failures pre-existing foreign WIP (zero touch P4 files).
