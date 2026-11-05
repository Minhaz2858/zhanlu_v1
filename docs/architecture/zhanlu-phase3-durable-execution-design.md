# Phase 3.1 — Durable Execution & Task Recovery Design (2026-10-31)

## Root causes

Durable state already existed (PlanNode result/status, ObservationRecord
result_data, AgentRunStep checkpoints, Execution tree, scheduled orphan
recovery). What was missing was the DECISION layer for post-restart
continuation: nothing decided what may safely resume, no idempotency keys
prevented duplicate execution, no stale-TTL distinguished "live worker owns
it" from "worker died", and ObservationRecord did not persist evidence
references.

## Design

`app/services/durable_execution.py` — pure decision layer (never executes):

- `resume_decision(status, started_at, error, mutating, requires_confirmation, ...)`
  → `ResumeDecision{action, reason, idempotency_key, requires_authorization}`.
  Safety precedence: **terminal-state consistency > approval/authorization >
  duplicate prevention > transient recovery**.
  - completed/skipped → `skip` (no duplicate execution)
  - running within TTL (1800s) → `leave_running` (live worker owns it)
  - running STALE + read-only → `recover` (safe re-own under idempotency key)
  - running STALE + mutating → `manual` (write may have landed — outcome
    unknown; read-only verification first; never auto-rerun)
  - approval-gated → `manual` ALWAYS (recovery never bypasses approval)
  - failed + terminal markers (permission/审批) → `manual` + authorization flag
  - failed + transient → `retry` within the Phase-2 healing budget
- `task_idempotency_key(plan_id, seq, name)` — deterministic sha256[:64].
- `is_stale(started_at, ttl_s=STALE_RUNNING_TTL_S)` — tz-safe.

Persistence additions (migration `115_observation_evidence`, information_schema
-guarded like V050):

- `observation_records.evidence_ids JSON` — durable evidence-registry refs;
  `_extract_evidence_ids` (capability_router) best-effort extracts
  `evidence_ids|evidence_refs|citations` from tool result payloads; NULL when
  absent (never fabricated).

## Composition (no second scheduler)

`recover_orphaned_invocations` (invocation level, >1h) and `resume_decision`
(node level, 1800s TTL) are layers of the same recovery story: the scheduler
reowns work; the decision layer says WHAT is safe per node; the Phase-2
healing budget bounds retries. Recovery-attempt provenance stays in
`AgentInvocation.output_json.recovery` (existing contract).

## Acceptance criteria mapping (mission §B)

- Restart during multi-agent run: completed nodes `skip`; stale read-only
  `recover`; stale mutating `manual` — `test_scenario_6_restart_during_executive_report`.
- No duplicate completed-task execution: skip branch + deterministic keys.
- Evidence remains available: `evidence_ids` persisted.
- Final status truthful: never `completed` from recovery (orphan contract kept).
- Recovery auditable: reason codes on every decision; output_json.recovery.
