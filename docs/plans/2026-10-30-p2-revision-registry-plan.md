# P2 Versioned Ontology Registry — Implementation Plan

> **STATUS: IMPLEMENTED & VERIFIED 2026-10-30** — see `docs/plans/2026-10-30-p2-progress-log.md`. Commits `70cd60dc9`, `0ca4388a9`, docs commit.

**Goal:** Every ontology mutation produces exactly one meaningful revision, transactionally atomic with the mutation; idempotent re-runs produce zero revisions; history is append-only; rollback creates a new revision.

## Inspected mutation inventory (2026-10-30)

| Path | Mutates | Hook point |
|---|---|---|
| `queries.ensure_domains` (seeding + domain-pack install) | creates domains/objects/links/actions | record create-revisions before `queries.py:38` commit |
| `discovery._apply_binding` (hint + LLM call sites) | object bind | record `bind` revision inside the hook (batch commit follows) |
| `routers.bind_object` | bind/unbind | record `bind`/`unbind` before commit |
| `routers.bind_action` | tool wiring | record `update` before commit |
| `template_export.import_org_template` | creates objects/links | record create-revisions before `template_export.py:232` commit |
| retirement | none existed | NEW org-admin DELETE endpoints (objects/links/actions) → `retire` revision |
| out of scope | CoverageState, sensors, agent-domain bindings (governance config, already audited), approvals | — |

## Revision data model

`ontology_revisions` (migration `104_ontology_revisions`, RLS inline like 094):
- `entity_type` String(20) `object|link|action|domain`; `entity_id` String(36)
- `revision_no` Integer — per-entity sequence, `UniqueConstraint(entity_type, entity_id, revision_no)`
- `change_kind` String(20) `create|update|bind|unbind|retire|rollback`
- `before_json`/`after_json` JSON — full mutable-state snapshots
- `changed_by` String(255) NULL; `change_summary` Text NULL
- Index `(org_id, entity_type, entity_id)`

## Transactional strategy

`revisions.record_revision(db, entity_type, entity, change_kind=None, changed_by=None, summary=None)`:
1. **before = the LATEST revision's `after_json` for that entity** (or `{}` on first) — callers never hand-maintain before-state.
2. **after = full snapshot** of the entity's mutable state (incl. evidence fields).
3. **No-change detection**: `after == before` → return None → zero revisions (idempotency for seeding, discovery re-runs, repeated binds).
4. **Atomicity**: revision row added to the CALLER'S session — commits (or rolls back) together with the mutation. Forced-failure test proves both die together.
5. **Concurrency**: entity row locked `with_for_update()` (no-op on SQLite) before `max(revision_no)+1`; insert flushed inside a SAVEPOINT — IntegrityError → savepoint rolled back, max re-read, retry (max 3) → no duplicate/gapped numbers, caller's mutation never lost.
6. **History immutable**: no update/delete paths for revisions; rollback = restore `before_json` of revision N + a NEW `rollback` revision.
7. Default `change_kind`: `create` when no prior revision, else `update`; callers force `bind`/`unbind`/`retire`/`rollback`.

## Tasks

1. **T1** `tests/test_ontology_p2_revisions.py` RED: atomicity (forced failure), idempotency (no-change → zero), concurrency (pre-collided revision_no → retry succeeds; unique constraint), create/update defaults, snapshots carry evidence fields.
2. **T2** Migration `104_ontology_revisions` (+RLS inline) + `models/ontology_revision.py`.
3. **T3** `services/ontology/revisions.py` GREEN.
4. **T4** Wire: seeding, bind/unbind, action bind, discovery, template import, retire endpoints (org-admin `DELETE /ontology/{objects|links|actions}/{id}`).
5. **T5** Endpoints `GET /ontology/{entity_type}/{id}/revisions`, `GET /ontology/revisions?since=`, `POST /ontology/{entity_type}/{id}/rollback` (org-admin) + minimal frontend history list.
6. **T6** Full regression + live verification (idempotent re-seed/discovery → zero revisions; bind → 1 revision; rollback → new revision) + review + progress log.

## Invariants preserved
- P0: enforcement untouched; revisions add no permission surface (GETs authenticated, mutations org-admin).
- P1: evidence fields are part of snapshots (immutable history of evidence states); stamps remain the only evidence writers.
- Provenance/evidence states in history are immutable (append-only table).
