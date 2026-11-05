# Zhanlu Automation — Final Architecture Consistency Review

**Date:** 2026-11-03
**Scope:** consistency review and correction of the four specification documents (target architecture, technical specification, roadmap, ADR — all dated 2026-11-03). Documentation-only; no production code, frontend, migrations, records, or configuration touched; working tree's uncommitted changes preserved.
**Method:** every contract was re-derived against actual repository code (spot-verified: `parent_execution_id` dual-use at `automation_dispatcher.py:785` vs retry rows; confirmation path; delivery paths; advisory-lock handling), then each mandatory finding (A–E), each strengthening area (1–12), and each cross-document contract was checked and corrected in place.

---

## 1. Findings and Corrections

### Blocking (would have produced incorrect implementations)

| ID | Finding | Where | Correction |
|---|---|---|---|
| F-1 | **Retry identity contradiction.** The spec put `UNIQUE(automation_task_id, occurrence_key)` on `automation_executions` *and* created retries as new execution rows — the retry insert would violate its own arbiter. Also `parent_execution_id` carried two meanings (retry chain AND recursion chain — verified conflated in current code, `automation_dispatcher.py:785` vs `schedule_retry`). | Tech spec §1.3, §3.2, §6.1; architecture §4.2; roadmap R5; ADR-001 | **Adopted the occurrence/attempt identity model:** new `automation_occurrences` table (§1.3a) owns logical occurrence identity and the duplicate-claim arbiter (`UNIQUE(automation_task_id, occurrence_key)`); executions are attempts (`occurrence_id`, `UNIQUE(occurrence_id, attempt)`); retry lineage = `retry_of_execution_id`; `parent_execution_id` reserved exclusively for automation-recursion. Claim = atomic occurrence `UPDATE … WHERE status='pending'`. Updated in all four documents. |
| F-2 | **`delivery_events` was referenced in five places but never specified** — no table, no constraints, no recovery contract. | Tech spec §1.5b/§6.4b (new), §8 case 18; architecture §9; roadmap R9 | **Specified `automation_delivery_events`**: UNIQUE `(execution_id, channel)` as the idempotency arbiter, claim-before-side-effect protocol, `pending → delivered | failed_permanent` lifecycle, max 1 retry, janitor recovery scan for crash-between-terminal-and-delivery, parent-owned writes only. Execution success ≠ delivery success boundary made explicit. |
| F-3 | **Confirmation protocol lacked exact locking/transaction semantics** — "INSERT … SELECT" prose did not specify serialization, the arbiter index, stable-ID replay, or crash recovery. | Tech spec §4.2 (new normative transaction); architecture §3.3; ADR-002 | **Normative 6-step transaction:** `SELECT … FOR UPDATE` on the draft (serializes concurrent confirms) → `If-Match` validation under the lock → `INSERT … ON CONFLICT (org_id, confirmation_key) DO NOTHING RETURNING id` (database-enforced arbiter; new partial unique index added to §1.2) → loser reads the winner's row (stable definition ID on retries) → draft marked `confirmed` in the same tx → single COMMIT. Crash before/after commit both resolve deterministically. |

### High

| ID | Finding | Correction |
|---|---|---|
| F-4 | **Misfire semantics under-specified:** multiple missed occurrences could enumerate unbounded rows (a 5-min cron × 3-day outage = 2,160 rows); one-time-trigger expiry undefined; restart behavior implicit. | Tech spec **§6.1a (new)**: within grace → one late fire; beyond grace → one `skipped(misfire)` occurrence; multiple missed slots collapse into **exactly one occurrence row per definition per episode** (`misfire_collapsed_count = N`) — bounded by construction; once-trigger past grace → skipped + auto-archive, never fires late; restart uses the identical first-tick path. Architecture §4.2 aligned. |
| F-5 | **Cancellation semantics conflated:** no typed reasons, no `superseded`, org-deactivation and parent/child cancellation undefined; dead `skipped` vocabulary. | Tech spec §3.2 rewritten: occurrence-level and execution-level state machines; typed `cancel_reason` (`user | admin | definition_deleted | org_deactivated | parent_cancelled | superseded`); six behavior definitions (user, admin, deletion, org deactivation, parent→child propagation, child→parent non-propagation). `cancel_reason` column added to executions; `superseded` given a precise meaning (queued occurrence invalidated by schedule edit before dispatch). |

### Medium

| ID | Finding | Correction |
|---|---|---|
| F-6 | Advisory-lock release undefined (the audit's S2 pattern — lock held on a pooled connection — could recur in the new design). | Tech spec §6.1 + Enforcement Contract 11.1: per-tick acquisition on a **dedicated connection**, released by closing it at tick end; never held across ticks; non-Postgres dev skips with a warning. |
| F-7 | Migration numbering/contents drifted between documents (roadmap R2 claimed 126+127; occurrences were nowhere). | Tech spec §1.7: one linear chain 126 (drafts+spec+confirm arbiter) → 127 (occurrences + execution identity changes + backfill) → 128 (events/leases/delivery) → 129 (status reconciliation + name index) → 130 (workflow claim adoption). Roadmap R2/R5/R8 aligned. |
| F-8 | Parent-owned subprocess persistence was an aspiration, not a contract. | Enforcement Contract 11.4: child owns no terminal/event/delivery/lease writes; parent persists from the control pipe; enforced by code ownership + a subprocess test. |
| F-9 | Run-now `?force=true` semantics undefined (who, what audit, what interleaving). | Enforcement Contract 11.8: same `authorize(...,'run')` grant, audit event `automation_run_forced`, own manual occurrence, per-execution chat writes documented. |

### Low

| ID | Finding | Correction |
|---|---|---|
| F-10 | Execution-event vs audit-event separation was implicit. | Enforcement Contract 11.12: telemetry (90-day, runtime-only writer) vs governance (permanent, `record_audit_event()` at authorization-relevant moments); audit may reference `execution_id`, never the reverse. |
| F-11 | Acceptance list lacked tests for the new guarantees (occurrence identity, confirm crash, delivery recovery, misfire collapse, cancellation reasons, harness parity, provenance-on-failure). | Tech spec §10 items 22–28 added; roadmap R3/R5/R6/R9 test references updated to match. |
| F-12 | Naming drift (`delivery_events` vs `automation_delivery_events`; "occurrence_key" location). | Unified across all four documents. |

---

## 2. Strengthening Contracts (area 2 of the brief) — where each now lives

| # | Area | Enforceable contract |
|---|---|---|
| 1 | Advisory-lock ownership & release | Tech spec §6.1 + §11.1 (dedicated connection, per-tick, released at tick end) |
| 2 | Lease fencing vs stale workers | Tech spec §6.2 runner + §11.2 (token compare on terminal/event/delivery writes; renewal failure aborts) |
| 3 | External side-effect idempotency | Tech spec §11.3 (registry `idempotent` metadata; unattended runs refuse non-idempotent write tools outside `pre_approved_scope`) |
| 4 | Parent-owned persistence (subprocess) | Tech spec §11.4 |
| 5 | Creator permission revocation | Tech spec §11.5 (fail-fast at claim; mid-run = live per-tool RBAC via TraceContext, run not killed) |
| 6 | Org/project isolation | Tech spec §11.6 (+ §9) |
| 7 | Artifact-storage durability | Tech spec §11.7 (MinIO read-back verification before `ready`; `storage_backend` recorded) |
| 8 | Run-now force semantics | Tech spec §11.8 |
| 9 | Agent invocation cancellation/timeout | Tech spec §5 (deadline + cancellation enforced at the contract layer, per event yield and per tool dispatch) |
| 10 | Workflow-scheduler integration | Tech spec §11.10 (single `scheduler_core.claim(subject_type)`; in-memory dedup deleted) |
| 11 | Alembic migration ordering | Tech spec §1.7 + §11.11 (linear 126→130, idempotent in-migration backfills) |
| 12 | Execution-event vs audit-event separation | Tech spec §11.12 |

---

## 3. Agent Harness Integration Validation (area 3)

Verified against source and now contractually enforced in the documents:

- **Production v3 runtime reused** — yes; the invocation contract is an entry-point extraction (adapter over `add_message_stream`, `agents.py:12148`), phase 2 re-sources internals from the existing `agent_loop/` package. No second runtime exists in the design; static test §10.20 forbids `automation_* → app.routers.agents` imports.
- **FSM planning remains available** — yes; `force_planning` passes through the contract (test §10.27).
- **Dynamic datasource selection preserved; heterogeneous authorized sources supported** — yes; spec carries *constraint sets only* (`allowed_source_ids` narrows, never selects); the harness's existing resolution chain (`_resolve_bound_data_source_ids` mechanics retained) and per-tool RBAC via `TraceContext` are untouched.
- **Tool RBAC enforced** — yes; registry RBAC + runtime-agent deny-list (admin/automation-CRUD) retained; spec can only narrow authority (architecture §3.1).
- **Multi-agent delegation remains available** — yes; existing delegation machinery (depth-capped `execute_automation`, delegate tools) unchanged; cancellation propagates via the contract.
- **No hardcoded automation-specific routing** — enforced by test §10.27 (parity toolset + documented exclusions; no routing tables/handlers).
- **Harness owns governance/budgets/provenance/authorization; Control Plane owns scheduling/execution lifecycle** — stated as the Consumer Invariant (architecture §5.2) with enforcement points (§10 items 20, 27).

Conclusion: automation remains a **consumer** of the Agent Harness. The Control Plane decides *when/why*, the Harness decides *what is permitted*, the agent decides *how*.

---

## 4. Cross-Document Consistency Results

| Check | Result |
|---|---|
| DB columns ↔ API contracts | Aligned: `confirmation_key`/`spec_version` (definitions §1.2) ↔ confirm headers/body (§4); `occurrence_id`/`attempt`/`cancel_reason` (§1.3) ↔ execution detail + status chips (§7.3) |
| DB indexes ↔ stated guarantees | `UNIQUE(org_id, confirmation_key)` → idempotent confirm (§4.2); `UNIQUE(automation_task_id, occurrence_key)` → duplicate-claim protection (§6.1); `UNIQUE(occurrence_id, attempt)` → retry idempotency; `UNIQUE(execution_id, channel)` → duplicate-delivery protection (§6.4b); partial unique name index → dedupe guard |
| State machines ↔ lifecycle behavior | Occurrence + execution machines rewritten together (§3.2); retries stay within one occurrence; terminal immutability + fencing stated once, consistently |
| Retry rules ↔ occurrence identity | Fixed (F-1) — retry = new attempt row, same occurrence, `UNIQUE(occurrence_id, attempt)` |
| Event contracts ↔ frontend behavior | Event payload list (§6.4) matches drawer sections (§7.3); status vocab list matches backend enum exactly; `skipped`/`timed_out` real semantics |
| Migration numbers unique & ordered | 126→130 linear chain, contents mapped to roadmap increments (R2:126; R5:127+128; R8:129/130 + checker retirement) |
| Roadmap increments ↔ acceptance tests | Every R1–R11 cites §10 items; items 22–28 added and referenced (R3: 1,2,8,14,18,23 · R5: 3,4,5,6,7,12,22,25,26 · R6: 17,20,27,28 · R9: 10,21,24) |
| Acceptance tests ↔ defined requirements | Each §10 item maps to a normative section (matrix below) |
| ADRs ↔ normative spec | ADR-001/002/010 updated to the occurrence model, confirm transaction, and cancellation taxonomy; ADR-003/004/005/006/007/008/009 already consistent |
| No contradictory examples remain | Architecture §4.2 pseudocode, tech spec §6.1 pseudocode, §3.2 diagrams, and roadmap R5 scope all describe the same claim/attempt model |

---

## 5. Final Architecture Acceptance Matrix

Status legend: **Designed** = specified + test-mapped, not implemented (three-states rule).

| Requirement | Architecture | Technical Spec | Roadmap | Test | Status |
|---|---|---|---|---|---|
| Confirmation idempotency (exactly-once, stable ID) | §3.3 | §1.1, §1.2, §4.2 | R3 | §10.1, 2, 23 | Designed |
| Duplicate-claim protection | §4.2 | §1.3a, §6.1 | R5 | §10.3, 22 | Designed |
| Occurrence/attempt/retry identity | §4.2, §7 | §1.3a, §3.2 | R5 | §10.22 | Designed |
| Missed-run recovery (bounded misfire) | §4.2 | §6.1a | R5 | §10.12, 25 | Designed |
| Worker-crash recovery (leases) | §6.1 | §6.1, §6.3, §11.2 | R5 | §10.4, 5 | Designed |
| Multi-instance execution | §4 | §6.1, §11.1, §11.10 | R5, R11 | §10.6, 7 | Designed |
| Agent invocation contract | §5 | §5 | R6 | §10.20, 28 | Designed |
| Harness parity (no second runtime) | §1, §5.2 | §10.27 ref, §11.3 | R6 | §10.20, 27 | Designed |
| Authorization (org/owner/role) | §3.4 | §3.4, §9, §11.6 | R3 | §10.14 | Designed |
| Approvals (no silent bypass) | §8 | §9.4 | R7 | §10.9 | Designed |
| Cancellation semantics (typed reasons) | §7 | §3.2 | R5 | §10.11, 26 | Designed |
| Provenance (agent_run/conversation on all terminals) | §8 | §1.3, §5 | R6 | §10.28 | Designed |
| Audit trail | §8 | §9.5, §11.12 | R7 | §10.15 | Designed |
| Delivery durability | §9 | §1.5b, §6.4b | R9 | §10.21, 24 | Designed |
| Artifact durability & isolation | §9 | §1.6, §11.7 | R9 | §10.10 | Designed |
| Schema migration & backward compatibility | §10 | §1.7, §11.11 | R2, R5, R8 | §10.16, 19 | Designed |
| Frontend consistency (one canonical resource) | §2 | §7 | R3, R10 | §10.14, 18 | Designed |
| Execution-event observability | §2.4 | §1.4, §6.4 | R6, R10 | §10.17 | Designed |

Every requirement traces to an increment and a test. No requirement is Architecture-only or Test-orphaned.

---

## 6. Remaining Risks (accepted, documented)

1. **Postgres-only guarantees.** The concurrency guarantees (unique arbiters, advisory lock, fencing) are provable only on Postgres; the SQLite test contract cannot exercise them. Mitigated by the tagged Postgres/multi-process test profiles (roadmap Testing Strategy) — but CI coverage depends on that profile being run.
2. **Fencing is optimistic.** Token comparison prevents *persistence* corruption; a stale worker can still burn LLM budget until its next checkpoint (inherent to cooperative cancellation). The subprocess stage (R6/R11) is the hard-kill answer; until promoted, orphan cost is bounded by the wall-clock budget and non-persistence.
3. **Subprocess runners unproven on the current single-container deployment** (memory ceiling for concurrent child processes) — flag-gated, decided after R11 chaos results.
4. **Behavior change for existing `skip_confirmation=true` tasks** — migration maps them to empty `pre_approved_scope` (failure-strict); some schedules will newly fail pending approval. Intentional; surfaced in the Center.
5. **Org-shared KB credentials** unchanged (platform-level secret-at-rest issue tracked separately since the 2026-10-20 audit).
6. **Tick starvation** (S3) remains accepted: 20 due/tick with collapse-on-misfire now bounds the worst case to delayed, not duplicated or multiplied, executions.

## 7. Explicit Unresolved Decisions (non-blocking for R1–R2; blocking where noted)

1. **[Blocking for R3]** Org-wide vs owner-only default for run/toggle/delete (ADR-006): design defaults owner-only for destructive actions; needs product sign-off.
2. **[Blocking for R3]** `pre_approved_scope` tool-class taxonomy and the migration mapping for legacy `skip_confirmation` tasks (failure-strict default chosen; confirm or amend).
3. **[Blocking for R5]** Global misfire grace default (1h) vs longer per-definition grace for critical schedules (collapse semantics already decided).
4. **[Blocking for R9]** Event-ledger retention (90 days proposed) and historic local-FS artifact backfill (accepted-loss proposed).
5. **[Non-blocking]** Subprocess-runner promotion timeline; `max_llm_calls` counting semantics (pinned during R6); event-ledger compliance retention.

## 8. Readiness Recommendation

**The design is ready for implementation approval.** All blocking contradictions (F-1, F-2, F-3) are resolved with database-enforceable mechanisms; the execution lifecycle is deterministic (occurrence/attempt identity + bounded misfire + typed cancellations); recovery and delivery guarantees are technically defensible (unique-index arbiters, fencing, claim-before-side-effect); all four documents agree after correction; the acceptance matrix is complete with per-requirement tests.

Recommended approval path: approve the architecture + ADR-001..010; answer the three blocking decisions above (§7 items 1–3) before R3/R5 begin; R1 (P0 hotfixes) may start immediately after approval as it touches none of the unresolved decisions.

---

---

## 9. Correction Pass 2 (F-13 … F-21) — same day, second review round

A subsequent principal-architect review raised nine additional findings (F-13–F-21). All are resolved in the second correction pass; the full resolutions, before/after contracts, and rationale live in `docs/reports/2026-11-03-zhanlu-automation-final-correction-pass.md`; the normative contracts were written into the technical specification and the decisions into ADR-011/012/013 (plus corrections to ADR-001/002/004/009/010). Summary:

| Finding | Resolution | Where |
|---|---|---|
| F-13 delivery idempotency & crash recovery | Durable claims (`claim_token/owner/expires_at`), `unknown` state + reconciliation, per-channel honesty (email = at-least-once via deterministic Message-ID), 7-scenario test matrix | Tech spec §1.5b, §6.4b; ADR-011; tests §10.29–31 |
| F-14 confirmation transaction correctness | Replay-first ordering (already-confirmed resolved before any rejecting validation), key ownership bound to draft, behaviors 1–7 table, Postgres concurrent tests | Tech spec §4.2; tests §10.1–3, 23 |
| F-15 workflow occurrence identity | Separate `workflow_occurrences` table (FK agent_app_id+node_id) + shared `scheduler_core`; `subject_type` overload rejected | Tech spec §1.3b; architecture §4.4; ADR-009; test §10.32 |
| F-16 deterministic misfire collapse | Explicit algorithm; representative = latest missed slot; grace measured against it (ADR-013); DST/edit/pause/degraded edge cases; worked examples normative | Tech spec §6.1a; ADR-013; test §10.33 |
| F-17 migration ownership | Linear chain 126–131 + reserved ≥132; one owner per migration verified against actual head `125_agent_run_provenance` | Tech spec §1.7; roadmap R2/R5/R8/R9 |
| F-18 occurrence-level cancellation metadata | `cancel_reason/cancelled_at/cancelled_by` on occurrences (pending occurrences cancellable without execution rows) | Tech spec §1.3a, §3.2; test §10.34 |
| F-19 finalization state machine | §6.2b: output persisted before `running→finalizing`; idempotent finalize-replay on lease recovery; agent success independent of artifact/delivery | Tech spec §6.2, §6.2b; roadmap R6 |
| F-20 authorization vs side-effect safety | Independent Gate A / Gate B at tool dispatch; registry idempotency metadata; approved ≠ retry-safe | Tech spec §11.3; ADR-012; test §10.36 |
| F-21 subprocess DB ownership | Table-scope contract: child runs the runtime with runtime-tables-only access; all automation persistence parent-owned; seq-numbered JSONL event pipe; cancel/deadline via control pipe + parent `terminate()` | Tech spec §11.4; ADR-004; test §10.35 |

**Note on filenames:** this document is the "final consistency review" referenced in the correction-pass brief (file name: `…final-architecture-review.md`).

The acceptance matrix covering F-13–F-21 plus B1–B18/S1–S6 with honest statuses is maintained in `docs/reports/2026-11-03-zhanlu-automation-final-acceptance-matrix.md`.

A third review round (reliability verification, F-22–F-25) is recorded in `docs/reports/2026-11-03-zhanlu-automation-final-reliability-verification.md` and the correction pass; its decisions (ADR-014 event durability, ADR-015 cancellation vs side effects, ADR-011 email-honesty amendment) supersede any conflicting wording in earlier sections of this document.

*Review complete (three passes). No implementation started; production code, frontend, migrations, records, and configuration untouched; working tree preserved.*
