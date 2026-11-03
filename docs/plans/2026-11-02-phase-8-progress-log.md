# Phase 8 — Progress Log

**Mission:** Phase 7C architectural verification + Phase 8 specification (spec-only; no Phase 8 implementation).
**Date:** 2026-11-02 · Branch `fix/automation-chat-artifacts-ordering` · HEAD at start/end of mission: `19e4aab7f`.

## Investigation completed

1. **Git reconciliation:** 10 Phase 7C commits verified (`b5407ebb7`→`19e4aab7f`); completion-report discrepancy confirmed (stated "nine", actual ten). All reported changes present at HEAD. 241 foreign-WIP entries untouched.
2. **Six-workstream verification:** every 7C fix independently re-verified at file:line level (see `docs/audits/2026-11-02-phase-7c-architectural-completeness-review.md` §3).
3. **Second-order audit:** 8 findings (S1-S8): 3 NO DEFECT, 3 MEDIUM deferred (sync-race index, v2/v3 pin inconsistency, MCP context bypass), 2 LOW (missing duration_ms, finalize snapshot gap). None block Phase 8; S4/S5/S6 are absorbed into Phase 8 scope.
4. **Test evidence:** 29-suite verification slice executed fresh → **251 passed / 0 failed** (26.13s).
5. **Phase 8 inventory:** AgentDataBinding (unconsumed rich model), AgentApp.knowledge_bases (actual runtime authority), per-turn resolution (no cache), auto-bind single consumer, AgentRun/steps/invocation models, ChatTurnRecorder choke point, delegation kb-id inheritance, MCP bypass, frontend AgentConfig/DataSourcesSection + ProjectDetail resources sheet.

## Verified existing capabilities (Phase 8 can build on)

- Per-turn binding resolution (revocation needs no cache invalidation — ADR-6).
- prov-v1 converters + validate_canonical + deterministic serialization (provenance.py).
- `parent_run_id` on AgentRun; step rows carry tool name/args/status/duration.
- Org-admin gating pattern (require_org_admin) and fail-closed TestClient test patterns from 7C.

## Decisions made

ADR-1..ADR-6 recorded (`docs/plans/2026-11-02-phase-8-architecture-decisions.md`): AgentDataBinding authoritative + JSON projection; descriptor serializer; narrow-only delegation; prov-v1 envelope on `agent_runs.provenance_json` at finalize; three-state auto-bind migration; no new caches.

## Outstanding questions (for Phase 8 implementation kickoff)

1. Do any legacy AgentApp rows carry `org_id = "default-org"` from pre-backfill data? (affects binding migration scoping; static analysis says fail-closed regardless)
2. Preferred UI placement for the provenance panel inside the existing chat message layout (TrustPanel adjacency proposed).
3. Compatibility-period length for the auto-bind flag before removal (proposal: one release cycle post-migration-availability).
4. Whether `AgentInvocation` should also carry the envelope reference for queued/async paths (spec says AgentRun is the choke point; invocation linkage optional).

## Implementation dependencies

Phase 8A (T8-1/T8-2) gates everything binding-related; Phase 8F (T8-8/T8-9) is independent and can start in parallel; T8-11 requires T8-5; T8-13 last.

## Acceptance status

- Phase 7C: **ACCEPTED WITH DEFERRED DEBT** (verdict in the audit report).
- Phase 8: **SPECIFICATION COMPLETE** — 5 deliverables produced; implementation explicitly NOT started, per mission.

---

# Phase 8 implementation log (2026-11-03)

All 13 roadmap tasks implemented TDD RED→GREEN; commits on
`fix/automation-chat-artifacts-ordering` (base `26d426cd2`):

| Task | Commit | Tests (RED first) |
|---|---|---|
| T8-1 binding model + migration 124 | `92132973c` | 4 (2 failed RED) + 22 regression |
| T8-2 binding service + projection + runtime read | `aecc44d64` | 12 + 4 RED + 30 regression |
| T8-3 binding CRUD + source-descriptors API | `a42a78881` | 7 RED + skills e2e 39 |
| T8-4 DataSourcesSection binding editor | `12030dd55` | 4 RED → 6/6 + build |
| T8-5 auto-bind migration gate | `1d11029f0` | 1 RED + 31 regression |
| T8-6 delegation provenance refs | `687b1176c` | 3 RED + MAS 10 |
| T8-7 MCP verified-context pass-through | `d2b748842` | 4 RED + MCP regression |
| T8-8 AgentRun prov-v1 envelope + migration 125 | `284ec3566` | 8 RED + 176 harness regression |
| T8-9 provenance read API | `d452bef91` | 1 RED (endpoint missing) + router 7 |
| T8-10 RunProvenancePanel | `6614c9bd1` | 4 + build |
| T8-11 auto-bind migration endpoint | `acd4d7ee6` | 3 RED + settings 10 |
| T8-12 flag deprecation + record | `2fb7ee45b` | settings/runtime 17 green |

**Bonus defect fixes found by the TDD suites:**
- `append_step` kwargs never matched AgentRunStep columns → EVERY call failed (latent);
- studio `add_data_binding` inserted rows with no org validation and no projection update;
- `get_bindings` called a nonexistent `to_dict`;
- `agent_data_bindings` and `agent_runs` predated the RLS rollout — tenant policies now applied (migrations 124/125).

## Integrated acceptance (T8-13, 2026-11-03)

- Backend matrix (9 new Phase 8 suites + the full 7C slice, 36 files):
  **286 passed / 0 failed** (32.4s).
- Frontend (agent + chat provenance + enterprise): **80 passed / 0 failed** (13 files).
- Production build: **green** (vite, exit 0).
- Migrations: alembic history verifies single linear head
  `123 → 124_agent_binding_enable → 125_agent_run_provenance (head)`;
  both migrations carry idempotent RLS blocks and full downgrades.
- Pre-existing unrelated failures (untouched, foreign scope): MCP
  `test_list_tools_includes_multimodal` (multimodal handlers not on this
  branch), harness `test_agent_class_normalisation` (policy env-parse
  drift), `test_every_observed_rule_...` (vision-bridge shadow primitive).

## Outstanding (for Phase 8.1)

1. Wire RunProvenancePanel into the Chat.jsx message footer (Chat.jsx is
   heavily foreign-dirty — one-line insert when that WIP lands).
2. Auto-bind flag removal after migration criteria are met (T8-12).
3. MCP session-level auth mapping (beyond the allow-listed pass-through).
