# P1 Unified Evidence Taxonomy — Progress Log

**Status: COMPLETE** (2026-10-30). Implemented, independently reviewed (all findings fixed), regression-tested, live-verified.

## Actual results

| Task | Result |
|---|---|
| 1 · `services/ontology/evidence.py` | Canonical five statuses; pure backfill rules; stamp helpers. RED→GREEN (12 tests) |
| 2 · Migration `103_ontology_evidence` | Applied live: head `103_ontology_evidence`; backfill = 10 objects inferred / 21 proposed / 1 action inferred / 17 links proposed / **0 verified / 0 fabricated confidence**; backfill provenance marker present. All pre-existing rows preserved (31 objects before = 31 after) |
| 3 · Serializers | objects/links/actions emit `evidence_status`/`confidence`/`provenance` |
| 4 · Human bind | `PATCH /bind` → `stamp_human_bind` (`verified` + `verified_by`/`verified_at` provenance); unbind → `stamp_unbind` (proposed, confidence cleared, stale keys superseded, dangling `backing_ref` nulled) |
| 5 · Discovery | `_apply_binding` stamps `inferred` + method (`hint_match` w/o score, `llm_discovery` w/ confidence) at both call sites; `stamp_discovery` refuses any drift toward `verified` (raises) |
| 6 · Frontend | `src/lib/evidenceStatuses.js` mirrors `evidence.py` exactly (reviewer-verified); `EvidenceBadge` renders canonical status + confidence in the EnterpriseProfile objects panel; dist rebuilt |
| 7 · Regression | **88 passed, 0 failed** (74 prior + 14 new) |

## Review outcome

Independent reviewer verdict: REQUEST_CHANGES → **all findings fixed** in commit `713f8d279`:

1. **(must-fix)** `stamp_unbind` left stale confidence + provenance → a proposal could display "proposed · 90%" and a re-discovery cycle could inherit stale `verified_by`. Fixed: confidence cleared, stale keys superseded (`unbound_at` recorded); regression test added.
2. Backfill SQL aligned exactly with `legacy_backfill_status` (empty-string `backing_kind` → proposed).
3. Action backfill rows now carry the backfill provenance marker.
4. `evidence_status` index parity model ↔ migration on all three tables.
5. `stamp_link` documented as reserved for the P3 identity-review queue.
6. Unbind clears dangling `backing_ref` (stale-data cleanup).

Reviewer-verified passes: verified-writer invariant (only `stamp_human_bind` reaches `verified`; grep of all writers); TrustPanel untouched (zero coupling); P0 guarantees untouched (permissions/approval/param-drift logic unchanged); coverage honesty unaffected (coverage still reads `backing_kind`, not evidence); template import produces `proposed` objects; frontend handles pre-migration payloads.

## Verification evidence

- **Regression:** 88 passed, 0 failed across the 8 ontology suites.
- **Live DB** (postgres, container): distribution `objects {inferred:10, proposed:21}`, `actions {inferred:1, proposed:30}`, `links {proposed:17}`; `verified=0`; `confidence IS NOT NULL = 0`.
- **Live API:** `GET /api/ontology/objects` emits canonical statuses only (asserted in smoke).
- **Live lifecycle smoke** (scratch object, then soft-deleted): discovery → `inferred 0.9` → human bind → `verified, confidence=None, verified_by=admin@zhanlu.dev` → unbind → `proposed, confidence=None, verified_by=None`.
- **Frontend:** `npm run build` clean; dist bind-mounted into `zhanlu-frontend` (:8089).
- **Commits:** `7c15cdbed` (implementation), `713f8d279` (review fixes).

## Constraints honored

- Existing data + provenance preserved (row counts identical; backfill only adds columns + conservative statuses).
- No uncertain legacy row auto-classified as verified (verified count = 0).
- Taxonomy centralized in `services/ontology/evidence.py` — the only writer of evidence fields (reviewer-audited).
- TrustPanel answer-level semantics untouched.
- Backend serialization and frontend EvidenceBadge use the same canonical five statuses.
- P2 not started; no unrelated ontology capability modified; P0 security guarantees intact.

**P2 (versioned ontology registry) not started** — awaiting review acceptance of P1.
