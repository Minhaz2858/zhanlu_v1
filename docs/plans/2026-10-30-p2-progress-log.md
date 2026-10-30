# P2 Versioned Ontology Registry — Progress Log

**Status: COMPLETE** (2026-10-30). Implemented, independently reviewed (3 blockers + recommendations all fixed), regression-tested, live-verified.

## Actual results

| Task | Result |
|---|---|
| T1 · RED tests (atomicity, idempotency, concurrency, rollback) | 9 tests written first; forced-failure test proves mutation+revision roll back together |
| T2 · Migration `104_ontology_revisions` | Applied live: head `104_ontology_revisions`, table + RLS `tenant_isolation` policy (1) present; model↔migration columns/constraints match |
| T3 · `revisions.py` | before = latest revision's after_json; no-change detection; FOR UPDATE row lock + SAVEPOINT retry (max 3); snapshots include all P1 evidence fields |
| T4 · Mutation-path wiring | seeding (create), bind/unbind (bind/unbind), action bind (update), discovery autobind (bind), template import (create), NEW org-admin retire endpoints (retire) — full inventory audited by reviewer: no unwired ontology writers remain |
| T5 · Endpoints + frontend | GET /ontology/revisions?since=, GET /ontology/{type}/{id}/revisions, POST /ontology/{type}/{id}/rollback (org-admin), DELETE retire routes; RevisionsFeed in EnterpriseProfile |
| T6 · Regression + live | **99 passed, 0 failed** (88 prior + 11 new); live scenarios below |

## Review outcome

Independent reviewer verdict: REQUEST_CHANGES → **all blockers + recommendations fixed** in commits `0ca4388a9` (+working-tree route fixes folded):

1. **(BLOCKER)** `import_org_template` had a duplicate `created_entities` init that clobbered domain/object entries — only links got revisions. Fixed + regression test (`test_template_import_revisions_and_idempotency`) that fails without the fix; live-verified (import#1 → revisions recorded; import#2 → ZERO new).
2. **(BLOCKER)** SAVEPOINT retry never rolled back the aborted savepoint → the Postgres retry path was dead code. Fixed (explicit `nested.rollback()`); the concurrency test now forces a REAL collision (concurrent winner commits no=1 first; poisoned insert collides; retry lands on no=2) — previously tautological per reviewer.
3. **(BLOCKER, test)** concurrency test rewritten as above.
4. since-cursor → composite `(created_date, id)` (same-second batches no longer skipped).
5. Rollback docstring corrected ("state AT revision N"); rollback provenance records `rollback_by/at/to`; unique-name conflicts surface as honest ValueError → 422 (regression test).
6. JSON round-trip normalization before change detection — serialization drift can never spam revisions on idempotent re-runs.
7. Accepted limitation (documented, reviewer-noted): pre-P2 legacy entities have no baseline revision — their first tracked change appears as `create` and pre-P2 state is not reconstructible from history.

## Verification evidence

- **Regression:** 99 passed, 0 failed across the 9 ontology suites.
- **Live DB:** head `104_ontology_revisions`; `ontology_revisions` + RLS policy present.
- **Live idempotency:** `ensure_domains` re-seed → revisions 0→0; `POST /ontology/discover` re-run → 0 new; template import re-run → 0 new.
- **Live lifecycle** (HTTP API, scratch object): `#1 create (by=seed) → #2 bind (verified) → #3 rollback (state restored to proposed, by=admin) → #4 retire`; per-entity history has no gaps/duplicates.
- **Live no-op rollback:** honest `{"ok": true, "revision": null, "note": "state already matches…"}` (was a 500 — found and fixed during smoke).
- **Frontend:** build clean; RevisionsFeed live on :8089.
- **Commits:** `70cd60dc9` (implementation), `0ca4388a9` (review fixes), docs commit.

## P0/P1 guarantees re-verified

- Enforcement code untouched; revision recording only READS state for snapshots (evidence stamps remain the sole evidence writers).
- Evidence states are immutable in history (append-only table; rollback appends).
- RLS inline on `ontology_revisions`; all endpoints org-scoped; mutations org-admin only.

**P3 (cross-source identity resolution) not started** — awaiting review acceptance of P2.
