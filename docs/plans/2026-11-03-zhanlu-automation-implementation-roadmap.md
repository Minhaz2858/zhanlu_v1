# Zhanlu Automation — Implementation Roadmap (TDD)

**Date:** 2026-11-03
**Status:** Proposed — awaiting architectural approval
**Companions:** target architecture + technical specification (`docs/superpowers/specs/2026-11-03-zhanlu-automation-*`), ADR (`docs/reports/2026-11-03-zhanlu-automation-architecture-decision-record.md`).
**Ground rules:** Option 2 refactor; TDD (RED→GREEN per increment); no implementation starts until the architecture is approved; the working tree's uncommitted user changes are preserved at all times (no resets/cleans; new work proceeds on top).

Increment ordering is by dependency and correctness, not frontend convenience. Each increment is independently shippable behind a flag and independently revertible.

---

## Testing Strategy (applies to all increments)

- **Unit tests** (pure logic): schedule edge cases incl. DST/invalid cron, misfire decisions, spec validation, backoff math, event translation.
- **API contract tests** (TestClient + real SQLite per `conftest.py` T1): every §4 endpoint, incl. idempotency replays and 409 paths.
- **Database integration tests:** migrations up/down, constraint behavior, backfills.
- **PostgreSQL concurrency tests** (new, tagged, run against the :5400 Postgres in CI-manual profile): duplicate confirm race, duplicate occurrence claim, fenced terminal writes. SQLite cannot prove these — explicitly out of SQLite scope (documented, not faked).
- **Multi-process scheduler tests** (new): 2 dispatcher processes × N ticks, assert zero duplicate fires; kill -9 worker; rolling restart with live run.
- **Worker recovery tests:** lease expiry, heartbeat loss, reaper fallback for legacy rows.
- **Agent integration tests:** invocation contract against the stubbed stream (pattern of `test_automation_one_chat_per_task.py`) + one live-LLM staging test (marked, non-CI).
- **Frontend tests** (vitest): draft card server-round-trip, Center filters/pagination/subscription, execution timeline from events, status vocab alignment.
- **Security tests:** cross-org/cross-owner denies on every endpoint, unattended scope enforcement, audit-event emission.
- **Fault injection:** fake clocks for lease/misfire/TTL; render and notification failure injection.

Acceptance = real behavior (DB rows, processes, HTTP), not mocks. The three-states rule applies to every deliverable: *implemented ≠ tested ≠ production-ready* — each increment records all three.

### Audit findings disposition (B1–B18, S1–S6)

| Finding | Disposition | Increment |
|---|---|---|
| B1 status="deleted" vs enum/CHECK | **Fix** (reconcile + never write) | R1 |
| B2 duplicate creation | **Redesign** (draft+confirm lifecycle) | R2/R3 |
| B3 workflow scheduler dup fires | **Fix** (shared claim tables) | R5 |
| B4 startup reap kills live runs | **Fix** (lease-based recovery) | R5 |
| B5 CAS→INSERT lost-fire window | **Fix** (transactional claim+insert) | R5 |
| B6 timeout doesn't kill work | **Redesign** (fencing + subprocess stage) | R6/R9 |
| B7 artifact render fails run | **Fix** (isolation) | R6 |
| B8 delete doesn't cancel runs | **Fix** | R4 |
| B9 page hides tasks | **Fix** (unified API + server filters) | R3 |
| B10 Run-Now concurrency | **Fix** (in-progress guard) | R3 |
| B11 zero audit | **Fix** (writer + call sites) | R7 |
| B12 silent +1h schedule corruption | **Fix** (degraded flag) | R4 |
| B13 divergent schedule UX | **Fix** (single validation path) | R3 |
| B14 dead vocab/code | **Fix** (real semantics + deletions) | R4/R6 |
| B15 triple DDL | **Fix** (Alembic-only + drift checker) | R8 |
| B16 string-typed columns | **Fix** (spec cutover; drop post-cutover) | R8 |
| B17 task failed/completed never set | **Redesign** (outcomes on executions; once→auto-archive) | R4 |
| B18 unauthenticated parse-schedule / no tz in description | **Fix** | R3 |
| S1 janitor retry vs orphan overlap | **Fix** (fencing) | R5 |
| S2 advisory lock not renewed | **Fix** (per-tick acquisition) | R5 |
| S3 tick starvation | **Accepted** (documented; limit 20→config; revisit if >500 defs) | R5 |
| S4 frontend polls forever | **Fix** (client max-age + `skipped` terminal) | R10 |
| S5 conversation_id missing on failure | **Fix** (contract returns always) | R6 |
| S6 legacy project string divergence | **Accepted** short-term (project_id authoritative; string compat retained) | — |

---

## R1 — P0 correctness hotfixes (no schema change)

**Objective:** remove the four sharpest correctness defects before any structural work.
**Scope:** (a) delete path stops writing `status="deleted"` (uses `is_deleted` + reconcile legacy rows via data fix in the same migration that follows); (b) Run-Now in-flight guard (`trigger_now` refuses when a non-terminal execution exists for the task, `?force=true` override); (c) artifact-render call wrapped (render failure → run succeeds, `artifacts_failed`, event); (d) delete cancels non-terminal executions.
**Files:** `backend/app/routers/automation_api.py`, `backend/app/services/automation_dispatcher.py` (`trigger_now`), `backend/app/services/automation_executor.py` (`_render_and_save_files`, delete hook), `frontend/src/pages/AutomationTaskDetail.jsx` (force toggle, minor).
**DB changes:** none (row reconciliation lands with R5's migration).
**Dependencies:** none. **Tests:** unit+API for each of (a)–(d) incl. cancel-on-delete and render-failure isolation (audit acceptance 10, 11).
**Acceptance:** B1-repro test passes (delete under CHECK-flag does not 500); two rapid run-nows → one 409; render exception → `succeeded`+`artifacts_failed`.
**Rollback:** flag `AUTOMATION_P0_GUARDS` (default on after green); revert = flag off.
**Risks:** run-now 409 may surprise UI (mitigate: frontend shows "already running" state).

## R2 — AutomationSpec + draft persistence (schema foundation)

**Objective:** canonical spec and server-side drafts exist; nothing consumes them yet.
**Scope:** models `automation_drafts`, spec column + `spec_version` + `confirmation_key` columns on `automation_tasks`; validation module `services/automation/spec.py`; Alembic 126 (additive; the confirm unique arbiter included); spec is optional (legacy rows valid without it).
**Files:** new `backend/app/models/automation_draft.py`, `backend/app/services/automation/spec.py`, `alembic/versions/126…` (single migration; 127+ belong to R5); model edits.
**Dependencies:** none. **Tests:** migration up/down, validation table-driven (valid/invalid cron/tz/budgets/scope), draft CRUD at repo level.
**Acceptance:** `alembic upgrade head` on a copy of prod-like data; legacy task loads unchanged; validation acceptance §10 items 1, 19.
**Rollback:** additive migrations (down safe). **Risks:** JSONB-vs-JSON parity (handled per §1.7).

## R3 — Canonical creation lifecycle (chat + API)

**Objective:** exactly-once creation through drafts; single access policy; legacy write path retired behind flag.
**Scope:** Control Plane API (`/drafts`, `/confirm`, `/definitions` list/detail/patch with server filters + pagination); `propose_automation` tool; `authorize()` policy module; authenticated `/parse-schedule` with tz-aware description; flag `AUTOMATION_CREATION_V2`. Agent prompt updated to propose-then-confirm protocol; `create_automation` registry entry removed under the flag; entity-API write routes for AutomationTask return 410 under the flag.
**Files:** new `backend/app/routers/automation_api.py` (extend), `backend/app/services/automation/control_plane.py`, `backend/app/services/agent_tools.py` (tool swap), `backend/app/services/agent_prompts.py`, `frontend/src/components/chat/AutomationDraftCard.jsx` (server-backed), `frontend/src/pages/AutomationTasks.jsx` (new list API), `frontend/src/pages/NewAutomationTask.jsx`.
**Dependencies:** R2. **Tests:** acceptance §10 items 1, 2, 8, 14, 18, 23; draft-card frontend tests; cross-owner/org denies for every endpoint.
**Acceptance:** the audit's chat-scenario E2E: propose → edit → confirm → task visible in Center without remount; replayed confirm returns same id.
**Rollback:** flag off restores legacy paths (kept one release).
**Risks:** LLM compliance with propose protocol (mitigate: registry schema forces it; prompt has fallback to clarify); migration of user habits (Center "New" still routes to chat).

## R4 — Trigger fidelity (schedule semantics, once-triggers, dead vocab)

**Objective:** schedule behavior is loud, correct, and complete.
**Scope:** `schedule_degraded`/`schedule_error` surfacing (no silent +1h — B12); `trigger_type`/`run_at` once-triggers with auto-archive (B17); real backend `timed_out` and `skipped(misfire)` semantics where the current code already writes those states; tz-aware `describe_schedule`; `max_retries` int migration path begins (read spec, fallback string).
**Files:** `backend/app/services/schedule_parser.py`, `automation_dispatcher.py`, `automation_executor.py`, `automation_api.py`; Center banner (frontend).
**Dependencies:** R2. **Tests:** acceptance §10 items 12, 13; DST unit tests (the untested core).
**Acceptance:** degraded flag fires on unparseable cron; once-task archives after first run.
**Rollback:** flags `AUTOMATION_STRICT_SCHEDULE` (degraded-loudness), revert-safe.
**Risks:** strictness may flag existing bad rows → one-time reconciliation report in migration 129 output.

## R5 — Durable scheduling plane (claims, leases, leader, recovery)

**Objective:** the scheduling core of the target architecture.
**Scope:** `automation_occurrences` + execution identity changes (Alembic 127: `occurrence_id` FK, `retry_of_execution_id`, `UNIQUE(occurrence_id, attempt)`, typed cancellation metadata on executions **and occurrences** — `cancel_reason/cancelled_at/cancelled_by` (F-18) — new status values, legacy in-flight backfill) and `automation_execution_events` + `execution_leases` (Alembic 128); occurrence claim with `UNIQUE(automation_task_id, occurrence_key)` + transactional next_run advance (B5); per-tick advisory-lock leadership with dedicated-connection release (S2); heartbeat + fencing on terminal/event/delivery writes; lease-based `recover_leases()` replacing destructive `reap_on_startup` (B4) with `AUTOMATION_FORCE_REAP_ON_STARTUP` escape hatch; persisted retries keyed `UNIQUE(occurrence_id, attempt)` (in-memory `_delayed_run` retired); deterministic misfire-collapse algorithm (§6.1a, incl. the worked-example tests); typed cancellation reasons incl. pending-occurrence cancellation; **`workflow_occurrences` (Alembic 130) + shared `scheduler_core` claim adoption** (F-15) + `cronjob_tool` deletion.
**Files:** `automation_dispatcher.py` (major rework), `automation_executor.py` (claim/lease/fencing), new `services/automation/scheduler_core.py` (shared claim/lease logic), `workflow_scheduler.py`, delete `tool_handlers/cronjob_tool.py` + registry entry, `main.py` (startup calls), `alembic/versions/127…, 128…, 130…` (per TS §1.7 ownership: 127+128+130 → R5; **128 also creates the F-27 side-effect ledger `automation_side_effect_ops`**), new `services/automation/side_effect_ledger.py` (§1.5c write points + barrier scan used by R6's recovery).
**Dependencies:** R1, R2. **Tests:** acceptance §10 items 3, 4, 5, 6, 7, 12, 22, 25, 26, 32, 33, 34; the previously-untested fire CAS now tested directly (dispatcher `_fire` race simulation); multi-process suite.
**Acceptance:** 2-instance × 200-tick simulation zero duplicates; kill -9 recovery < 2× heartbeat; rolling restart leaves sibling's live run untouched.
**Rollback:** flag `AUTOMATION_SCHEDULER_V2` (old loop retained behind it for one release); migrations additive.
**Risks:** highest-risk increment (touches the hot loop) — mitigated by parallel-run mode: V2 claims and legacy loop disabled together, never both; long soak on dev stack before prod.

## R6 — Agent Invocation Contract + execution isolation

**Objective:** decouple automation from router internals; typed events; provenance; isolation hardening.
**Scope:** `services/agent_invocation.py` (phase-1 adapter per spec §5); executor refactored to consume it (regex SSE parsing deleted from executor); `agent_run_id`/`conversation_id` persisted on all terminal paths (S5); event-ledger writes (plan/tool/approval/artifact/lifecycle) with `activity_steps` kept as a derived cache for one release; **finalization protocol implemented (§6.2b: output persisted before `running→finalizing`; idempotent finalize-replay on lease recovery; recovery-discrimination table + PG fault-injection scenarios)**; subprocess runner prototype (stage 2) behind `AUTOMATION_SUBPROCESS_RUNNERS` (default off) with the **table-scope DB-ownership contract (§11.4)** and the **Option C event-durability protocol (§6.5: critical events synchronously ACKed after commit, telemetry bounded-loss, 9-scenario crash matrix)**.
**Files:** new `backend/app/services/agent_invocation.py`, `automation_executor.py` (rewrite of `_run_agent_in_conversation` into runner protocol; no new migration — events/leases tables already exist from R5's 128), frontend execution drawer (events source).
**Dependencies:** R5 (leases). **Tests:** acceptance §10 items 17, 20, 27, 28, 35, 37, 38, 40, 41, 42, 43; contract translation tests (SSE→typed), cancellation propagation, orphan event suppression, finalize-replay after crash, event crash matrix, side-effect reconciliation, **F-27 pre-retry reconciliation barrier (four-outcome matrix; same-attempt replay + cross-attempt seq reuse via the F-26 unique index)**; subprocess kill test (marked multi-process).
**Acceptance:** static test proves no `automation_*` → `app.routers.agents` import; execution drawer renders from events for a real run.
**Rollback:** adapter is behavior-preserving; flags per stage.
**Risks:** SSE frame parser drift vs router changes (mitigate: golden-frame tests pinned to `agent_loop/sse_builders.py` fixtures).

## R7 — Governance and audit

**Objective:** unattended runs fully governed and auditable.
**Scope:** `record_audit_event()` writer + automation call sites (all mutations/terminals, B11); `skip_confirmation` → `pre_approved_scope` (approval-ttl enforcement, `failed(approval_required)` path, N9 fix); **two-gate governance (F-20): Gate A authorization vs Gate B side-effect safety — registry metadata `idempotent`/`idempotency_key_scope`/`reconcilable`, attempt-scoped idempotency keys in the invocation context, ambiguous-timeout `unknown` outcomes, two-gate matrix tests**; approval queue integration (existing governance UI shows automation approvals); audit endpoint `/definitions/{id}/audit`; unattended policy test matrix.
**Files:** new `services/automation/audit.py`, `automation_api.py`, `automation_executor.py` (approval block rewrite at `:1905-1915`), `models/audit_event.py` (vocabulary entries), Center governance tab (frontend).
**Dependencies:** R3 (spec), R6 (events). **Tests:** acceptance §10 items 9, 15, 36, 38, 44; scope-enforcement matrix; TTL expiry fault-injection; two-gate independence matrix (Gate A vs Gate B); side-effect reconciliation on cancellation/kill; **F-27 blocked-retry path (unknown non-idempotent → `blocked_on_side_effect_reconciliation`, Center reconciliation item, operator `mark reconciled` unblocks)**.
**Acceptance:** zero `ApprovalService.approve` calls from automation without explicit scope; every endpoint mutation produces exactly one audit event.
**Rollback:** flag `AUTOMATION_APPROVAL_POLICY_V2`; legacy `skip_confirmation` honored read-only during transition.
**Risks:** behavior change for existing tasks relying on skip_confirmation → migration maps boolean to explicit `pre_approved_scope` (documented in ADR).

## R8 — Schema consolidation (Alembic-only)

**Objective:** one DDL mechanism; string columns resolved.
**Scope:** `_ensure_schema()` → drift checker (log-only); `main.py:289-314` automation entries deleted; spec-cutover backfill (scalar columns → spec JSON for all rows, verified by report); reconciliation of legacy statuses; post-cutover migration reserving (not executing) the eventual drop of `skip_confirmation`/`max_retries`/`notify_chat`.
**Files:** `automation_dispatcher.py`, `main.py`, `alembic/versions/129…` (reconciliation + cutover; 130 belongs to R5), drift-check report script.
**Dependencies:** R2–R5 stable. **Tests:** acceptance §10 item 16; drift checker zero-divergence on dev+staging DBs; backfill round-trip (spec→scalars equality).
**Acceptance:** fresh env boots with zero runtime ALTERs; drift checker silent on migrated DBs.
**Rollback:** checker is passive; backfill idempotent. **Risks:** low.

## R9 — Delivery reliability + durable artifacts

**Objective:** delivery and storage are failure-isolated and durable.
**Scope:** MinIO artifact backend (`ARTIFACT_STORAGE_BACKEND`, Alembic 131: `storage_key`/`storage_backend`/`status`/`render_error`, read-back verification) with local fallback; delivery module on `automation_delivery_events` (tech spec §1.5b/§6.4b: durable claims with `claim_token`/`claim_owner`/`claim_expires_at`, `pending→claimed→delivered|failed_permanent|unknown` lifecycle, deterministic email `Message-ID`, `unknown` reconciliation, janitor recovery scan); `_notify_chat` deletion; upload of existing local artifacts is NOT backfilled (documented; new artifacts only).
**Files:** `document_generator.py` call site, new `services/automation/delivery.py`, Alembic 131 (`automation_files` storage metadata), **minimal backward-compatible change to `notification_gateway/provider.py`: `send()` gains an optional `message_id`/headers parameter so the delivery module can set the deterministic `Message-ID = f(idempotency_key)` (required by F-13/TS §1.5b; smtplib otherwise auto-generates one — verified `provider.py:57-129`)**.
**Dependencies:** R6. **Tests:** acceptance §10 items 10, 21, 24, 29, 30, 31, 39; MinIO integration test (tagged), local fallback test, render/notification failure isolation, delivery claim crash matrix, SMTP timeout→`unknown` classification.
**Acceptance:** kill between render and chat-write → delivery retried once on recovery, no duplicate bubble.
**Rollback:** backend flag reverts to local FS. **Risks:** MinIO creds/config (already deployed in stack).

## R10 — Frontend completion (Center + execution UX)

**Objective:** full UX per spec §7 on the new contracts.
**Scope:** Center month/card/table views, server-side filters/search/pagination, stats; execution timeline from event ledger (poll `after_seq`, SSE optional); status vocab alignment (`claimed/timed_out/skipped/awaiting_approval`); S4 polling max-age; provenance footer; degraded-schedule banners; draft-card final polish.
**Files:** `frontend/src/pages/AutomationTasks.jsx`, `AutomationTaskDetail.jsx`, `components/automation/AutomationExecutionStatusDrawer.jsx`, `lib/useAutomationExecutionStatus.js`, `lib/automationStatusBus.js`, chat card.
**Dependencies:** R3–R7 APIs. **Tests:** vitest suites per surface; component tests grounded in recorded event fixtures.
**Acceptance:** §10 items 14, 18; visual parity checklist.
**Rollback:** frontend deploy is independently revertible. **Risks:** low.

## R11 — Multi-instance acceptance + hardening

**Objective:** declare the platform multi-instance-safe.
**Scope:** full multi-process test suite on Postgres (:5400 profile); load soak (200 defs, 60s tick); chaos pass (kill leader mid-tick, kill runner, DB blip); runbooks (leader loss, lease manual-recovery, drift report); S3 starvation re-evaluation with real counts.
**Files:** tests + `docs/runbooks/automation-operations.md` (new).
**Dependencies:** R5–R9. **Acceptance:** zero duplicate fires, zero lost occurrences, zero duplicate deliveries across the chaos matrix; dashboard shows all execution states reachable.
**Rollback:** n/a (validation increment).

### Sequencing summary

```
R1 ──┬──▶ R2 ──▶ R3 ──▶ R4 ──▶ R5 ──▶ R6 ──▶ R7 ──▶ R8 ──▶ R9 ──▶ R10 ──▶ R11
P0   schema  lifecycle triggers scheduler isolation govern  DDL    delivery  UX     acceptance
```
R1–R3 are user-visible early wins; R5 is the critical-path risk center (parallel-run + soak mandated); R10 deliberately late (APIs stable first).

### Deployment sequencing & feature flags

`AUTOMATION_P0_GUARDS` (R1) → `AUTOMATION_CREATION_V2` (R3) → `AUTOMATION_STRICT_SCHEDULE` (R4) → `AUTOMATION_SCHEDULER_V2` (R5) → `AUTOMATION_INVOCATION_V2` (R6) → `AUTOMATION_APPROVAL_POLICY_V2` (R7) → `ARTIFACT_STORAGE_BACKEND` (R9). Flags default off in prod, soaked on the dev stack (per repo convention: backend restarts without `--reload` require explicit relaunch). Rollback at every step = flag off + additive migrations stand. Data reconciliation scripts are idempotent and re-runnable.
