# Zhanlu Automation — Final Acceptance Matrix

**Date:** 2026-11-03
**Companions:** correction pass (`…final-correction-pass.md`), consistency review (`…final-architecture-review.md`), tech spec (`…technical-specification.md`), architecture, roadmap, ADR.
**Status vocabulary (honest, three-states rule — implemented ≠ tested ≠ production-validated):**

- **Designed** — specified with a named implementation location and test case; not yet implemented.
- **Blocked** — design complete but an external product decision gates the increment.
- *(No row is marked Implemented/Tested/Production-validated: implementation has not begun — nothing may claim those states.)*

"Impl. location" = target file/module for the increment that owns the requirement (current code locations noted where behavior is retained/reused).

## Correction-pass findings F-13 … F-27

| ID | Requirement | Design section | Impl. location | Test case | Expected result | Status |
|---|---|---|---|---|---|---|
| F-13 | Durable delivery claims + honest guarantees | TS §1.5b, §6.4b; ADR-011 | `services/automation/delivery.py` (new); `automation_delivery_events` (migration 128); `notification_gateway/` reuse | TS §10.29–31 (7-scenario crash matrix; Message-ID determinism; channel independence) | Crash in any window → no unbounded duplicates; email at-least-once w/ deterministic Message-ID; channels fail independently; agent never re-run | Designed |
| F-14 | Replay-first confirmation transaction | TS §4.2 | `services/automation/control_plane.py` (new); `automation_api.py` confirm route | TS §10.1–3, 23 (PG concurrent-confirm profile) | 7 behaviors of §4.2 table; one definition under concurrency; stable ID on replay | Designed |
| F-15 | Workflow occurrence identity (separate tables, shared mechanics) | TS §1.3b; Arch §4.4; ADR-009 | `scheduler_core.py` (new); `workflow_occurrences` (migration 130); `workflow_scheduler.py` rewire | TS §10.32 (multi-process race) | One occurrence row per workflow due-slot across pollers; workflow semantics preserved | Designed |
| F-16 | Deterministic misfire collapse | TS §6.1a; ADR-013 | `automation_dispatcher._tick` / `scheduler_core` | TS §10.33 (worked-example unit tests, fixed clocks/tz) | All 6 worked examples reproduce exactly; ≤1 occurrence row per definition per episode | Designed |
| F-17 | Unambiguous migration ownership 126–131(+reserved) | TS §1.7 | `alembic/versions/126…131` | §10.16, 19 + `alembic history` linearity check | Linear chain off head `125_agent_run_provenance`; no duplicate numbers; backfills idempotent | Designed |
| F-18 | Occurrence-level cancellation metadata | TS §1.3a, §3.2 | migration 127; `control_plane.py` cancel paths | TS §10.26, 34 | Pending occurrence cancellable w/o execution row; typed reason/at/by persisted; `superseded` exercised | Designed |
| F-19 | Finalization state machine | TS §6.2, §6.2b | `automation_executor.py` (runner rewrite, R6) | TS §10.4, 35 + finalize-replay test | Output persisted before `finalizing`; crash → finalize-replay or new attempt; agent success independent of artifact/delivery | Designed |
| F-20 | Independent authorization (A) vs side-effect-safety (B) gates | TS §11.3; ADR-012 | tool registry metadata + `agent_invocation.py` dispatch boundary | TS §10.36 (two-gate matrix) | Approved ≠ retry-safe; unknown outcomes never auto-retried; no prompt-level governance | Designed |
| F-21 | Subprocess DB ownership (table-scope contract) | TS §11.4; ADR-004 | child entry (R6, `AUTOMATION_SUBPROCESS_RUNNERS`) | TS §10.35 (child-write rejection; kill -9; seq integrity) | Child cannot write automation tables; parent owns persistence; no duplicate/lost events | Designed |
| F-22 | Subprocess event durability (Option C: critical = ACK-after-commit; telemetry bounded/counted) | TS §6.5; ADR-014; ReliabilityVerification §F-22 | `agent_invocation.py` sinks + parent ingest (R6) | TS §10.37 (9-scenario crash matrix) | Critical events exactly-durable after parent commit; telemetry loss bounded+counted; no false ACKs | Designed |
| F-23 | Cancellation ≠ termination ≠ side-effect reconciliation | TS §6.6; ADR-015 | runner + `side_effect_reconciliation` events (R6/R7) | TS §10.38 (cancellation-after-irreversible-side-effect) | Status stays `cancelled`; side effect surfaced + audited; unknown non-idempotent never auto-retried | Designed |
| F-24 | Honest email guarantees (no receiver-dedup claim) | TS §1.5b, §6.4b; ADR-011 | `notification_gateway/provider.py` message_id param (R9) | TS §10.39 (timeout→unknown; permanent→failed_permanent) | At-least-once attempts, duplicates possible; Message-ID = identification only | Designed |
| F-25 | Finalization recovery discrimination | TS §6.2b; ReliabilityVerification §F-25 | runner finalization (R6) | TS §10.40 (PG fault-injection of the discrimination table) | Row-state-decidable recovery; duplicate finalization impossible; DB-failure leaves `finalizing` | Designed |
| F-26 | Attempt-scoped event identity (`UNIQUE(execution_id, attempt, seq)`) | TS §1.4, §6.5; ADR-014 | events schema (R5) + parent ingest ACK framing (R6) | TS §10.37, §10.41 (same-attempt replay absorbed), §10.42 (cross-attempt seq reuse, ACK carries `{execution_id, attempt, committed_upto}`) | Same-attempt replay idempotent; cross-attempt sequence reuse legal, no collision; recovery attributes events per attempt | Designed |
| F-27 | Side-effect ledger + mandatory pre-retry reconciliation barrier | TS §1.5c, §6.2b, §6.6; ADR-015 | `services/automation/side_effect_ledger.py` (R5); barrier in recovery (R6); blocked-retry + Center item (R7) | TS §10.43 (crash after external acceptance before output persistence → op confirmed, never repeated), §10.44 (unknown non-idempotent blocks retry; `confirmed_not_started` safely retried; operator `mark reconciled` unblocks) | Barrier precedes every new attempt; ledger is sole source of truth; no invented reconciliation; confirmed-complete ops never repeated | Designed |

## Original audit findings B1–B18

| ID | Requirement | Design section | Impl. location | Test case | Expected result | Status |
|---|---|---|---|---|---|---|
| B1 | No `status="deleted"` writes | TS §3.1; Arch §7 | `automation_api.py` delete (R1) + migration 129 | R1 repro test (delete under CHECK flag) | Delete never violates enum/CHECK; legacy rows reconciled | Designed |
| B2 | Idempotent creation (no duplicate tasks) | Arch §3.3; TS §4.2 | Control Plane confirm (R3) | TS §10.1, 2, 8, 23 | LLM cannot create definitions; confirm exactly-once | Designed |
| B3 | No duplicate workflow fires | TS §1.3b, §11.10 | `scheduler_core` + workflow adoption (R5) | TS §10.6, 32 | Zero duplicate fires across processes | Designed |
| B4 | Non-destructive startup recovery | TS §6.3 | `recover_leases()` (R5) | TS §10.7 (rolling restart) | Sibling's live run untouched on restart | Designed |
| B5 | No lost-fire window | TS §6.1 | dispatcher claim tx (R5) | TS §10.22 | Claim+advance+execution+lease atomic | Designed |
| B6 | Timeout stops the work | TS §6.2b, §11.4; ADR-004 | fencing (R5) + subprocess (R6) | TS §10.4, 5, 35 | Late writes fenced out; hard kill at process boundary (stage 2) | Designed |
| B7 | Artifact failure ≠ run failure | TS §6.2b, §3.4 | `automation_executor` render isolation (R1/R6) | TS §10.10 | `succeeded` + `artifacts_failed=true` | Designed |
| B8 | Delete cancels in-flight runs | TS §3.2 | delete path (R1/R4) | TS §10.11 | Cancelled before completion; no post-delete deliveries | Designed |
| B9 | Center shows all visible tasks | TS §4, §7.2 | `/definitions` server filters (R3) | TS §10.14, 18 | Chat-created tasks visible without remount; server-side filters/pagination | Designed |
| B10 | Run-Now concurrency guard | TS §11.8 | `trigger_now` (R1) | R1 API test | 409 in-progress; force audited | Designed |
| B11 | Audit trail for all mutations | TS §9.5, §11.12 | `record_audit_event()` (R7) | TS §10.15 | Exactly one audit event per mutation | Designed |
| B12 | No silent schedule corruption | TS §6.1a; Arch §3.1 | `schedule_degraded` (R4) | TS §10.13 + degraded-flag test | Parse failure loud; no +1h silent fallback | Designed |
| B13 | Single schedule-validation UX | TS §2, §4.2 | Control Plane validation (R3) | validation table tests | One validation path for chat + Center | Designed |
| B14 | Dead vocab/code removed | TS §3.2, §7.3; Arch §9 | R4/R6/R9 deletions | TS §10 status-vocab test | `timed_out`/`skipped` real; `_notify_chat`/cronjob deleted | Designed |
| B15 | Alembic-only DDL | TS §1.7, §11.11; ADR-008 | drift checker (R8) | TS §10.16 | Zero runtime ALTERs; drift checker silent | Designed |
| B16 | No string-typed booleans/ints | TS §1.2, §1.7 | spec cutover (R8) | §10.19 legacy-compat test | Spec authoritative; retirement migration reserved | Designed |
| B17 | One-shot completion handled | TS §3.1, §6.1a | once-trigger auto-archive (R4) | TS §10.12 | Auto-archive after single run/skip; no definition-level outcome statuses | Designed |
| B18 | Authenticated parse-schedule; tz-aware descriptions | TS §4 | `automation_api.py` (R3) | R3 API test | No unauthenticated endpoints; tz shown | Designed |

## Original audit suspected issues S1–S6

| ID | Requirement | Design section | Impl. location | Test case | Expected result | Status |
|---|---|---|---|---|---|---|
| S1 | No retry/orphan overlap | TS §6.2b, §11.2 | fencing (R5) | TS §10.4 | Orphan's late writes rejected; retry is a clean attempt | Designed |
| S2 | Advisory lock held correctly | TS §6.1, §11.1 | dispatcher tick (R5) | S2 regression test | Per-tick dedicated connection; recycle-proof | Designed |
| S3 | Tick starvation bounded | TS §6.1a | dispatcher (R5) | misfire-collapse tests | Overdue backlog collapses; delays, never duplicates | Designed (accepted limit documented) |
| S4 | No infinite frontend polling | TS §7.3 | `useAutomationExecutionStatus.js` (R10) | frontend test | Max-age + terminal `skipped` stop polling | Designed |
| S5 | Provenance on failure paths | TS §5 | `agent_invocation.py` (R6) | TS §10.28 | `agent_run_id`/`conversation_id` on failed executions too | Designed |
| S6 | project/project_id divergence | TS §1.2 | compat retained (—) | §10.19 | FK authoritative; string compat documented | Designed (accepted) |

## Harness invariants (brief §3) — preservation checks

| Invariant | Verified by |
|---|---|
| v3 runtime retained; FSM/ReAct/registry/TraceContext/delegation intact | TS §5, §10.27; Arch §5 consumer invariant |
| Dynamic datasource selection; constraints not selections; no default/query-all | TS §2 schema; Arch §3.1 |
| No automation → router imports; no duplicated runtime | TS §10.20 (static test) |
| Traceability (execution/AgentRun/conversation/artifact/audit ids) | TS §1.3, §5, §10.17, 28 |
| No silent auto-approval | TS §9.4; ADR-005; §10.9 |
| Governance in runtime/authz, never prompts | TS §11.3, §9 |

**Blocking-decision flags:** R3 rows (B2, B9, B13, B18) blocked on the ownership-default and scope-taxonomy decisions; R5 tuning (F-16 grace default) blocked on the misfire-grace sign-off; R9 rows (F-13 retention, artifact backfill) blocked on the retention/backfill decision. All other rows are unblocked pending architectural approval.
