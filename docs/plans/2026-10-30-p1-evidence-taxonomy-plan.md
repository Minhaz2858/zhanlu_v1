# P1 Unified Evidence Taxonomy — Implementation Plan

> **STATUS: IMPLEMENTED & VERIFIED 2026-10-30** — see `docs/plans/2026-10-30-p1-progress-log.md`. Commits `7c15cdbed`, `713f8d279`.

**Goal:** One canonical five-status evidence taxonomy (`verified | computed | inferred | proposed | unavailable`) persisted on ontology objects/links/actions with confidence + provenance, centralized in `services/ontology/evidence.py`, surfaced through serialization and a frontend `EvidenceBadge` that uses the same canonical statuses.

**Inspected starting state (2026-10-30, live DB `zhanlu` @ postgres:5432):** alembic head `102_turn_identity`; `ontology_objects` (21 `manual` + 10 `table`-bound), `ontology_actions` (1 with tool), `ontology_links` (17) — **no evidence/confidence/provenance columns**. `serialize.py` emits none. Discovery autobind happens in `_apply_binding` (2 call sites: hint path L406, LLM path L459); human binding is `PATCH /ontology/objects/{id}/bind` (router L137-157). TrustPanel (`services/trust_panel.py`) is answer-level and NOT touched.

## Migration & backfill rules (defined BEFORE schema change)

Migration `103_ontology_evidence` (down_revision `102_turn_identity`):

| Table | New columns |
|---|---|
| `ontology_objects`, `ontology_links`, `ontology_actions` | `evidence_status` String(20) NOT NULL server_default `'proposed'`; `confidence` Float NULL; `provenance_json` JSON NULL |

**Backfill (never auto-verify uncertain legacy rows):**
- Objects with `backing_kind <> 'manual'` AND non-empty `backing_ref` → `inferred` (binding evidence exists, but no recorded human authorization) + provenance `{"backfill": "2026-10-30", "note": "binding predates evidence taxonomy"}`.
- All other objects (manual/unbound) → `proposed` (from server_default).
- Actions with non-empty `tool_name` → `inferred`; others → `proposed`.
- All links → `proposed` (no evidence was ever recorded for links).
- `confidence` stays NULL for every backfilled row (never fabricated). `provenance_json` records the backfill marker so the row is auditable.
- `verified` is ONLY ever set by an explicit human bind after this migration (`stamp_human_bind`). Nothing in the backfill or discovery path can set it.

## Tasks

### Task 1: `services/ontology/evidence.py` (TDD)
- `EVIDENCE_STATUSES = ("verified","computed","inferred","proposed","unavailable")`
- `is_valid_status(s)`, `normalize_status(s)`
- `legacy_backfill_status(kind: str, backing_ref: str|None) -> str` (pure, unit-tested)
- `legacy_backfill_status_action(tool_name) -> str`
- `stamp_human_bind(obj, *, bound_by)` → sets `verified`, provenance `{bound_by, bound_at, backing_kind, backing_ref}` (REASSIGN JSON)
- `stamp_unbind(obj)` → back to `proposed` (manual bindings are proposals again)
- `stamp_discovery(obj, *, method, confidence, kb_id, table)` → sets `inferred` + confidence + provenance (REASSIGN JSON)
- `stamp_unavailable(obj_or_prop_context)` reserved (P6 uses it; only constant now)
- Test: `tests/test_ontology_p1_evidence.py`

### Task 2: Migration `103_ontology_evidence` + model columns
- Columns per table above (alembic; models get matching `mapped_column`s with python-side defaults).
- Test: pure backfill rules; live migration applied in container.

### Task 3: Serialization emits the fields
- `serialize.py`: `object_to_dict`/`link_to_dict`/`action_to_dict` include `evidence_status`, `confidence`, `provenance`.

### Task 4: Wire human bind (PATCH endpoint)
- `bind_object`: `stamp_human_bind` when binding to real backing; `stamp_unbind` when set back to `manual`/empty.

### Task 5: Wire discovery autobind
- `_apply_binding(...)` gains evidence kwargs; hint path stamps `method="hint_match", confidence=None`; LLM path stamps `method="llm_discovery", confidence=conf`; both `evidence_status="inferred"`.

### Task 6: Frontend canonical statuses + EvidenceBadge
- New `frontend/src/lib/evidenceStatuses.js` — the five statuses, comment pointing at `services/ontology/evidence.py` as the source of truth.
- `EvidenceBadge` in `EnterpriseProfile.jsx` objects panel (next to `BackingBadge`): verified=emerald, computed=sky, inferred=amber, proposed=stone, unavailable=red.

### Task 7: Regression + live verification
- Full ontology suite; TrustPanel untouched (no diffs); live migration + API smoke; rebuild frontend dist.

## Non-goals (this phase)
- No `computed` writer yet (P5 metric backing activates it) — the status exists canonically from day one.
- TrustPanel answer grades untouched (boundary mapping is a later concern, and currently identity-free).
- No P2 revision-registry work.
