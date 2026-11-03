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

---

# Phase 8.1 — Final integration and production readiness (2026-11-03)

Base: `f868574fd` · Commits: `94fcc9167` (Stage 1), `b0a197dea` (Stage 2), `fc052c7c6` (Stage 3), + this log.

**Stage 1 — chat provenance end-to-end (`94fcc9167`):** `record_chat_turn_run`
gives every completed v3 chat turn a real AgentRun (org/user from the
VERIFIED session; executed tools as steps — blocked denials never recorded
as executions; honest status; fail-open; flag-gated). run_id is attached to
the final assistant message (v3 + v2) so it survives reload, and
MessageBubble renders RunProvenancePanel for messages carrying run_id
(historical messages without one render nothing — honest absence).

**Stage 2 — MCP authorization hardening (`b0a197dea`):**
`verify_mcp_context` validates every `_zhanlu_context` identity claim
against the database (org exists; agent live + same-org; user same-org).
Any unverifiable claim drops the WHOLE context to the no-context fallback —
never partially trusted; claim-free contexts pass UNVERIFIED; verified
contexts are stamped `verified: True`. The mount itself remains unauthenticated
by design (external MCP clients) — session-token auth for /api/ext-mcp is
the documented next step, and even a forged claim now cannot reference a
cross-org agent/user.

**Stage 3 — auto-bind compatibility (`fc052c7c6`):** effective bound set
proven IDENTICAL pre/post migration; flag honored until migrated; stamp
removal restores legacy behavior (rollback); codebase scan pins exactly
four readers of the deprecated flag.

**Stage 4 — integrated acceptance (2026-11-03):**
- Backend: 39-test matrix → **302 passed / 0 failed** (46s).
- Frontend (agent + chat + enterprise): **392 passed / 0 failed** (60 files).
- Production build: **exit 0**.
- Migrations: alembic history — single linear head
  `123 → 124 → 125_agent_run_provenance`; up/down verified by revision
  linkage + RLS idempotency blocks.

**Remaining known-unrelated failures (foreign scope, untouched):** MCP
multimodal-tools listing (handlers not on this branch), harness policy
env-parse drift, vision-bridge shadow-primitive scan, 7 legacy Chat/Sidebar
frontend failures.

**Next:** session-token auth for /api/ext-mcp; auto-bind flag removal once
all workspaces migrate (T8-12 criteria).

---

# Phase 8.2 — External MCP authentication, authorization & security hardening (2026-11-03)

Base: `4c370c34d` · Commits: `6c5fcc8d9` (Stages 0-1), `c25155023` (Stages 2-6).

**Stage 0 — Security audit** (`docs/audits/2026-11-03-phase-8-2-external-mcp-security-audit.md`):
F1 CONFIRMED/CRITICAL — /api/ext-mcp unauthenticated; F2 CONFIRMED/HIGH —
context claims unanchored; F3 no audit; F4 exception leakage; F5/F6 not a
defect; F7 suspected SSE session; F8 deferred.

**Stages 2-4 — Auth + authorization boundary** (`6c5fcc8d9`...`c25155023`):
`mcp_auth.py` reuses the existing JWT machinery (signature + expiry + JTI
revocation + DB User row); `is_privileged_jsonrpc` separates public protocol
negotiation (initialize/tools/list/notifications/ping) from privileged
execution (tools/call, incl. batched); `McpAuthMiddleware` is pure-ASGI and
wraps the transport, so every transport inherits protection; per-request
principal in a ContextVar; `resolve_execution_context` is the single choke
point — principal identity authoritative, mismatching claims drop the whole
context, anonymous claims carry NO identity authority, cross-org agent claims
never enter the context, scope from enabled AgentDataBinding only (no default,
no query-all). `POST /api/mcp/tokens` issues 90-day (max 365) MCP tokens;
revoke is self-only (cross-user → 403 + audited).

**Stage 5 — Audit:** `mcp` event source + `mcp_auth_success` /
`mcp_auth_failed` / `mcp_tool_invoked` / `mcp_token_issued` / `mcp_token_revoked`
/ `mcp_token_revoke_denied`; ids and outcomes only — no secrets, no token
material, no arguments in any audit row.

**Stages 6-7 — TDD matrix + regression:** 41 new tests green
(authentication 9, middleware 3, token issuance 3, authorization boundary 9 —
plus context/audit suites, 62 together). Regression matrix: 102 passed, sole
failure is the documented pre-existing multimodal listing case. Stray dev-DB
rows from an early debug run (audit_events/revoked_tokens for `u1`) cleaned.

**Resolved from 8.1's "Next":** session-token auth for /api/ext-mcp — DONE
(this phase). Remaining: auto-bind flag removal once T8-12 criteria are met;
deferred items in `docs/plans/2026-11-03-phase-8-2-completion-report.md`
(per-session SSE binding residual, scoped tokens, rate limiting).

---

# Phase 9 — Enterprise reliability & E2E acceptance (2026-11-03)

Base: `7865c9662` · Commits: `9e01e1a66` (reliability fixes), `a344cbb78`
(acceptance + test-infra), `60c6fdcf0` (audit doc), + this report.

**P0 re-verification:** 6 of 8 P0s from the 2026-10-20 audit were already
fixed on HEAD (B1 honesty guard, A2 preflight probe, A3 delegate wall-clock,
D4 FSM id-adoption, C1 frontend watchdog, E1 credential encryption). The two
live ones are now fixed TDD:

- **A1** `9e01e1a66` — sync tool handlers ran on the caller's SQLAlchemy
  Session inside `asyncio.to_thread` workers while parallel batches
  dispatched concurrently. Per-call `SessionLocal` (closed in `finally`);
  4 isolation tests.
- **D3** `9e01e1a66` — data-expected automations with zero successful
  datasource calls could ship fabricated reports as `completed`. Data-call
  tally (executed-query evidence only) + `_should_fail_for_ungrounded_report`
  wired at both status points; honest failure explanations pass; 13 tests.

**Reproducible baseline restored:** the full backend sweep produced ~12.8k
errors from three stacked test-infra defects (DATABASE_URL pollution at
collection time; lazy-registered models truncated before creation — 12,224
setup errors; missing `engine` fixture) plus a broken `--no-llm` flag that
let live-LLM tests hang on a dead proxy. All fixed; conftest fixes ride on
the uncommitted T1 WIP by design (documented).

**Acceptance:** `tests/test_e2e_enterprise_acceptance.py` — 5 deterministic
enterprise scenarios (evidence survival, fabrication rejection, batch
isolation, cross-org isolation, privileged split). Reliability matrix (30
suites, all 10 audit areas): 299 passed / 8 failed — all 8 re-verified
pre-existing at HEAD (stash-tested). Frontend: 1154/1183, all failures in
active foreign-WIP files. Production build: exit 0.

**Deferred (foreign-WIP collision):** model/migration-020 drift (legacy
`project` column still on models while live foreign-WIP services query it) —
P1, owner coordination required; documented precisely by
`tests/migrations/test_020_*`.

Full details: `docs/audits/2026-11-03-phase-9-reliability-audit.md` and
`docs/plans/2026-11-03-phase-9-completion-report.md`.
