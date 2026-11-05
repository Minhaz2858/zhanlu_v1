# Zhanlu Automation — Final Architectural Correction Pass (F-13 … F-21; rounds 3–4 extend to F-27)

**Date:** 2026-11-03
**Role frame:** principal architect / senior backend / distributed systems / QA architect.
**Repo state:** branch `fix/automation-chat-artifacts-ordering`, HEAD `6c5fcc8d9` (2026-11-03 12:51 +0800), 257 uncommitted user changes — all preserved; nothing reset, cleaned, or overwritten. Documentation-only pass.
**Source-of-truth hierarchy applied:** repository code (existing behavior) → technical specification (normative target) → architecture (boundaries) → roadmap (sequencing) → ADR (decisions).

**Documents read:** target architecture, technical specification, implementation roadmap, final consistency review (`…final-architecture-review.md` — this is the brief's "final-consistency-review"; no file of the other name existed), ADR, and the original audit; plus code inspection of `automation_dispatcher.py`, `automation_executor.py`, `notification_gateway/` (incl. `provider.py`), `workflow_scheduler.py`, `sandbox_worker/main.py`, `governance/approval_service.py`, `alembic/versions/` (head `125_agent_run_provenance`; **no 126–131 revisions exist** — allocation below is safe).

---

## F-13 — Delivery idempotency and crash recovery

> *Supersession note (final reliability verification, F-24): the phrase "dedupable at the receiver" in this section's original resolution was **retracted** — a deterministic `Message-ID` identifies and traces the message; receiver-side dedup is not a guaranteed property. The normative guarantee is at-least-once delivery-*attempt* semantics with possible duplicates (TS §1.5b/§6.4b; ADR-011 as amended).*

**Problem (verified).** The prior contract retried delivery with a unique `(execution_id, channel)` row but had no claim ownership, no expiry, and no state for "the provider may have delivered but the worker died before persisting" — a retry could duplicate the email.

**After (normative — tech spec §1.5b, §6.4b; ADR-011).**

- **Durable claim:** the UNIQUE `(execution_id, channel)` row is claimed with `claim_token` (ULID), `claim_owner`, `claim_expires_at` (5 min). State writes are token-compared (delivery-side fencing).
- **States:** `pending → claimed → delivered | failed_permanent | unknown`; `attempts ≤ 3` then `failed_permanent`.
- **Provider-side idempotency:** used where it exists — SMTP has **none** (verified: `SmtpProvider`), so email carries a **deterministic `Message-ID` derived from the idempotency key**, enabling receiver-side dedup. Future provider migration (SES/SendGrid keys) is noted as an option, not assumed.
- **`delivery_unknown`:** set on crash-between-send-and-persist and on ambiguous timeouts. Reconciled, never blindly retried: optional provider-side check; age-out to `failed_permanent(ambiguous_outcome_unresolved)` + `delivery` execution event; retry requires `delivery_retry_unknown=true`.
- **Channel separation:** chat/panel = effectively-once **visible state** (idempotent upserts keyed by execution id); email = **at-least-once, dedupable at the receiver**. The system nowhere claims exactly-once external delivery.
- **Recovery:** janitor scan handles missing rows (claim+send), expired claims (re-claim, attempts+1), and `unknown` reconciliation.
- **Boundary:** channels fail independently; execution status is untouched by delivery outcomes; the agent is never re-run for a delivery failure.

**Acceptance tests (§10.29–31):** crash before external delivery; crash after claim; provider-accepted-but-persist-crashed → `unknown`; expired-claim recovery; duplicate prevention with provider idempotency; ambiguous reconciliation without it; independent channel failures.

---

## F-14 — Confirmation idempotency and transaction correctness

**Problem (verified).** The prior transaction validated `draft.status='draft'` at step 2 **before** the replay check — a legitimate replay of an already-confirmed draft would receive 409 instead of the original definition ID.

**After (normative — tech spec §4.2).** Replay-first ordering:

1. `SELECT … FOR UPDATE` on the draft (serializes all paths).
2. **Replay check first:** `confirmed` + same key → 200 original definition ID; `confirmed` + different key → 409 `already_confirmed_different_key`.
3. Key ownership: key bound to the draft at first attempt; mismatch → 409.
4. `If-Match` version validation under the lock → 409 `stale_version`.
5. `INSERT … ON CONFLICT (org_id, confirmation_key) DO NOTHING RETURNING id` (DB arbiter); index-loser reads the winner → 200 stable ID.
6. Draft → `confirmed` + audit event; single COMMIT → 201.

All seven required behaviors (same-key replay, different-key reject, stale 409, exactly-one create, concurrent duplicates, crash-before-commit retry, crash-after-commit replay) are enumerated in the behavior table with their step references, and tested (behaviors 2/5's concurrency semantics are Postgres-profile; SQLite covers the rest — documented, not faked).

---

## F-15 — Shared workflow and automation occurrence identity

**Inspected.** Workflow subject = published `flow_mode` AgentApp × `trigger_schedule` node with cron config (`workflow_scheduler.py:24-64`); executions live in `workflow_runs`. Automation subjects are `automation_tasks`. The prior draft's `subject_type` overload of `automation_occurrences` would have broken the FK and mixed retention.

**Decision (ADR-009 corrected; tech spec §1.3b; architecture §4.4).** **Separate subject-specific occurrence tables + one shared claim/lease service:**

- `automation_occurrences` (FK `automation_task_id`) and `workflow_occurrences` (FK `agent_app_id`, `node_id`; `UNIQUE(agent_app_id, node_id, occurrence_key)`).
- Primary identity per subject table; claim = `UPDATE … WHERE status='pending'`; identical lease/fencing/recovery via `scheduler_core` table adapters.
- Workflow-specific recovery: same `recover_leases()` run over both tables; no workflow backfill needed (current scheduler is stateless — occurrences synthesize on first adoption claim).
- Migration **130** owns `workflow_occurrences` + adoption. Workflow semantics untouched; mechanics consolidated only.

---

## F-16 — Deterministic misfire-collapse algorithm

**Chosen product behavior (explicit decision, ADR-013):** representative slot = **latest** missed slot; grace measured `now − latest_slot`. Prior draft was ambiguous (first slot vs "now").

**Algorithm (normative — tech spec §6.1a):** interval `[next_run_at, now)` → slots via croniter+zoneinfo (fold=0 for DST-ambiguous/nonexistent times) → representative = max(S) → within grace & `fire_once_late` → one late fire under `occurrence_key="scheduled:{R}"` with `misfire_collapsed_count=|S|`; else one `skipped(misfire)` row → `next_run_at` advances past `now` via CAS in the same transaction → repeated ticks cannot duplicate (not due anymore + unique index).

Edge cases fixed as deterministic rules: schedule edit during downtime discards the un-evaluated episode (`misfire_discarded_on_edit` audit); pause discards on resume (`misfire_discarded_on_pause`); degraded schedules never selected; once-triggers past grace skip + auto-archive; restart uses the identical first-tick path. Six worked examples with timestamps/timezones are normative and unit-tested (§10.33). Backlog bound: **one occurrence row per definition per episode, by construction.**

---

## F-17 — Alembic migration ownership and ordering

**Verified:** head = `125_agent_run_provenance` (`down_revision="124_agent_binding_enable"`); no 126–131 exist.

**Allocation (one owner each; tech spec §1.7):**

| Migration | Responsibility | Owner increment |
|---|---|---|
| 126 | Drafts + canonical spec + confirmation columns + confirm arbiter index | R2 |
| 127 | `automation_occurrences` + execution identity (`occurrence_id`, `retry_of_execution_id`, `UNIQUE(occurrence_id, attempt)`) + **cancellation metadata** (executions & occurrences) + status values + in-flight backfill | R5 |
| 128 | `automation_execution_events` + `execution_leases` + `automation_delivery_events` *(round-1 snapshot — superseded by round 4 (F-26/F-27): 128 also owns the `automation_execution_events.attempt` column and `automation_side_effect_ops`; see the round-4 section below)* | R5 |
| 129 | Definition status reconciliation + partial unique name index + spec cutover | R8 |
| 130 | `workflow_occurrences` + scheduler-core adoption | R5 |
| 131 | Artifact storage metadata (`storage_key`, `storage_backend`, `status`, `render_error`) | R9 |
| ≥132 (reserved) | Legacy column retirement (`skip_confirmation`, `max_retries`, `notify_chat`) — post-cutover soak, number assigned at implementation time | R8 follow-up |

Status/enum changes live in 127+129; backfills are idempotent and inside 127/129; delivery-event schema in 128. No duplicate numbers, no conflicting revision IDs, no shared ownership.

---

## F-18 — Occurrence-level cancellation metadata

**After (tech spec §1.3a, §3.2).** `automation_occurrences` gains `cancel_reason`, `cancelled_at`, `cancelled_by` (`{user:{id}}`/`{admin:{id}}`/`system:{cause}`) — migration 127. Because the metadata is occurrence-level, a **pending occurrence is cancellable without any execution row** (deletion and org-deactivation sweep pending occurrences directly). Coverage: user (`user`), admin (`admin`), definition deletion (`definition_deleted`), org deactivation (`org_deactivated`), parent cancellation (`parent_cancelled`), superseded queued occurrences (`superseded` — schedule edit invalidates before dispatch). Execution-level `cancel_reason/cancelled_at/cancelled_by` retained; the two layers record their own fences (occurrence cancellation needs no fence pre-claim; claimed occurrences are fenced via the execution lease).

---

## F-19 — Finalization state machine

**Problem.** The execution state machine showed `running → terminal` in some places and `finalizing` in others without defining who owns the transition or what happens on crash.

**After (normative — tech spec §6.2/§6.2b).**

- Full protocol: `claimed → running → finalizing → succeeded | failed | cancelled | timed_out`.
- **Ownership:** only the runner (parent process in subprocess mode) writes all three transitions; heartbeats continue through `finalizing`.
- **Protocol:** `output_text/output_data` + provenance persisted **before** the `running→finalizing` CAS; finalization steps are idempotent and never re-invoke the agent; terminal CAS is fenced.
- **Artifact failures:** `artifacts_failed=true`, finalization continues to `succeeded` — agent success independent of artifact success.
- **Delivery failures:** outside the state machine entirely (own lifecycle, §6.4b).
- **Crash during finalizing:** lease expiry → recovery checks `output_text IS NOT NULL` ⇒ finalize-replay on the same execution; `NULL` ⇒ attempt failure → new attempt row.
- **Lease expiry during finalizing (runner alive):** renewal failure aborts at next checkpoint; takeover per the rule above; fencing rejects the original runner's late write.
- **Terminal immutability:** unchanged (fenced CAS on non-terminal states).

---

## F-20 — Approval authorization versus side-effect idempotency

**After (normative — tech spec §11.3; ADR-012).** Two independent gates at tool dispatch, both registry/harness-enforced, never prompt-enforced:

- **Gate A (may it run?):** registry RBAC + TraceContext principal → spec allow/deny → org unattended permission → approval requirement (`pre_approved_scope` / ApprovalService pause / TTL failure). Existing ApprovalService, RBAC, registry preserved unchanged.
- **Gate B (may it be retried?):** new registry metadata `idempotent`, `idempotency_key_scope`, `reconcilable`; attempt-scoped idempotency keys `{execution_id, attempt}` in the invocation context; ambiguous timeout ⇒ `outcome="unknown"`, no auto-retry.

The 2×2 outcome matrix (approved × idempotent) is normative: approval never implies retry safety; approved+non-idempotent runs at most once per attempt; execution retries carry fresh keys and rely on the tool's own re-application guard. Automation-CRUD tools stay hard-forbidden unattended. Tested by the two-gate independence matrix (§10.36).

---

## F-21 — Subprocess execution and database ownership

**Inspected.** `backend/sandbox_worker/main.py` confirms the subprocess worker pattern. The production agent runtime requires DB access (conversations, messages, KB reads, tool handlers) — a DB-free child would break Invariant 2.3, so ownership is **table-scope**, enforced by injection and import boundary (normative — tech spec §11.4):

1. Child may perform **agent-runtime-owned operations only** (chat/conversation/KB reads-writes, tool-handler DB usage, artifact staging); must never write `automation_executions/occurrences/events/leases/delivery/definitions/audit_events`.
2. Child receives its own `SessionLocal`-derived session for runtime tables and **no handle** to the automation persistence layer (never imported by the child entry point).
3. Automation execution events are persisted by the **parent**, fed by the child's event sink.
4. Terminal status writes: parent-only (§6.2b).
5. Typed events cross as **seq-numbered JSONL frames** with parent ACKs; gaps re-requested by `seq` — no loss, no duplicate persistence.
6. Cancellation/deadlines propagate over the control pipe (`cancel`/`deadline` messages) plus parent-side `process.terminate()` at the wall clock; the child checks the pipe at the same checkpoints as the in-process signal.
7. Child crash (pipe EOF / exit-before-terminal): parent marks `failed(transient)` → retry per policy; the child's partial chat writes remain as honest traces.
8. Fencing: children never write fenced tables, so stale children cannot corrupt state; late child events are dropped at the parent's terminal check.
9. Credentials/identity: DB URL via environment (unchanged posture); identity passes as principal id + org/app/project in the invocation context; `TraceContext` set inside the child so RBAC is live.

The runtime is reused unchanged — no second planner, no tool-routing engine (invariant §3 preserved; tests §10.20/27/35).

---

## Additional contradictions found and fixed during the pass

- §6.1 tick pseudocode referenced "§6.2a" for misfire (stale after renumbering) → §6.1a.
- Runner sketch showed delivery inside the runner loop, contradicting the post-terminal claim model → rewritten (§6.2).
- Redundant pre-F-14 bullets with stale step references removed from §4.2.
- Architecture doc's workflow-consolidation wording ("same claim/lease tables with `subject_type`") contradicted the F-15 decision → rewritten.
- ADR-009 still described the `subject_type` model → corrected.

## Remaining decisions (unchanged from the consistency review, plus one new)

1. Org-wide vs owner-only destructive-action default (blocks R3).
2. `pre_approved_scope` taxonomy + legacy `skip_confirmation` mapping (blocks R3).
3. Misfire grace default (1h proposed; collapse semantics now decided — ADR-013) (blocks R5 tuning).
4. Event-retention period; historic artifact backfill (blocks R9).
5. **New:** whether to adopt an idempotency-key-capable email provider (SES/SendGrid) — optional hardening, not required by this design.

**Verdict:** all nine findings resolved with enforceable contracts; no blocking architectural gaps remain. Package is ready for human architectural approval (see `…final-acceptance-matrix.md` for per-requirement status).

---

## Independent review round (Stage 5) — results and fixes

A fresh-eyes review pass (separate investigator, code-grounded) challenged the corrected package and returned **2 blocking + 6 minor issues**. All were verified against source (it independently confirmed: alembic head `125_agent_run_provenance`, no 126–131 exist; `SmtpProvider.send()` has no idempotency parameter and never sets `Message-ID` (`provider.py:57-129`); the `parent_execution_id` conflation; the workflow subject model). Disposition:

| # | Review finding | Disposition |
|---|---|---|
| B-1 (blocking) | Roadmap "Files" lists gave migrations 127→R2, 129→R5, 130→R8 — contradicting normative ownership (TS §1.7: 127/128/130→R5, 129→R8, 131→R9) | **Fixed**: roadmap R2 (126 only), R5 (127, 128, 130), R6 (no migration), R8 (129), R9 (131) |
| B-2 (blocking) | Roadmap R9 said "`notification_gateway` untouched", incompatible with F-13's deterministic `Message-ID` (only settable in `provider.py` send path) | **Fixed**: R9 now specifies the minimal backward-compatible provider change (`send()` gains optional `message_id`/headers) |
| M-3 | Retry-count inconsistency (§8 said "one retry", §1.5b says attempts ≤ 3) | **Fixed**: §8 cases 13/18 aligned |
| M-4 | §6.1 `recover_leases()` omitted the §6.2b finalize-replay branch | **Fixed**: pseudocode now branches on `output_text IS NOT NULL` |
| M-5 | §4.2 unhandled edges: (a) two drafts confirming with the same key left a half-bound draft; (b) replay after winner soft-delete returned nothing | **Fixed**: loser draft → `discarded` + 409 `confirmation_key_already_used`; replay lookup ignores `is_deleted`, returns `definition_archived: true` |
| M-6 | `workflow_occurrences` had no provenance link to `WorkflowRun` (multi-node flows unattributable) | **Fixed**: `workflow_run_id` column added (migration 130) |
| M-7 | DST worked example imprecise (nonexistent-time behavior unpinned) | **Fixed**: rule pinned — nonexistent local fire times excluded from slots, ambiguous times `fold=0`; pinned by fixed-clock test §10.33 |
| M-8 | §11.4 allowed-operations omitted `agent_runs` (child needs it for provenance stamping) | **Fixed**: `agent_runs` named as runtime-owned (allowed in-child) |

Post-fix verification: package-wide grep found no remaining `subject_type='workflow'` model references, no stale §6.2a refs (outside the fix log), no stale occurrence_key-on-executions contracts, and migration ownership is stated identically in TS §1.7, roadmap, correction pass, and acceptance matrix.

**Final verdict: READY FOR ARCHITECTURAL APPROVAL** (human sign-off pending; the three product decisions in the consistency review §7 still gate R3/R5/R9 tuning but are not architectural blockers).

---

## Independent review round 3 — reliability verification (F-22 … F-25)

A subsequent reliability-focused review raised four findings; all resolved with source-verified mechanisms — full detail in `docs/reports/2026-11-03-zhanlu-automation-final-reliability-verification.md`:

| Finding | Resolution |
|---|---|
| F-22 subprocess event durability | **Option C selected** (ADR-014): critical events synchronously parent-ACKed after DB commit (`UNIQUE(execution_id, attempt, seq)`), telemetry bounded/counted loss, output+terminal parent-derived — zero-loss claimed only where proven. 9-scenario crash matrix specified (TS §6.5) |
| F-23 cancellation vs side effects | Three-concept separation; tools complete under cooperative cancel (verified: no mid-call checkpoints); hard-kill `unknown` → Gate B reconciliation; **cancelled-after-irreversible-side-effect keeps status `cancelled`** with reconciliation event + audit (TS §6.6; ADR-015) |
| F-24 SMTP honesty | "Dedupable at the receiver" retracted — Message-ID = identification/tracing only; timeout→`unknown` pinned (verified `EmailTransportError` classification); guarantee = at-least-once attempts with possible duplicates (TS §1.5b/§6.4b; ADR-011 amended) |
| F-25 finalization recovery | Recovery discrimination table made row-state-decidable incl. DB-failure and unknown-terminal cases; PG fault-injection scenarios added (TS §6.2b) |

New ADRs: **014** (event durability Option C), **015** (cancellation vs side effects). Acceptance tests §10.37–40 added; roadmap R6/R7/R9 updated; guarantee→enforcement matrix completed (weakened where the mechanism cannot support the strong claim). Verdict: **READY FOR FINAL ARCHITECTURAL APPROVAL** (see the reliability-verification report for the full evidence chain).

---

## Correction round 4 — two-defect closure (F-26 / F-27)

A final verification pass identified two residual defects in the F-22/F-23 designs and closed both; full analysis in `docs/reports/2026-11-03-zhanlu-automation-final-reliability-verification.md` (§F-26, §F-27):

| Defect | Resolution |
|---|---|
| **F-26** event-sequence uniqueness conflict | `UNIQUE(execution_id, seq)` on `automation_execution_events` collided with per-attempt sequences under retries/leases (attempt 2 reuses seq 0+ → spurious unique violations or silent event drops). **Fixed:** attempt-scoped identity `UNIQUE(execution_id, attempt, seq)` (TS §1.4 — the `attempt` column rides with migration 128); parent ACK frames carry `{execution_id, attempt, committed_upto}`; §6.5 duplicate/gap detection reworked per-attempt; crash-matrix scenario 4 rewritten; same-attempt replay absorbed by the unique index, cross-attempt seq reuse legal by construction. |
| **F-27** unsafe pre-retry assumption | The recovery rule "`running/claimed + lease expired + no output → new attempt`" implicitly assumed no output ⇒ no side effect — false for any tool that dispatched an external operation before crashing. **Fixed:** new `automation_side_effect_ops` intent-and-outcome ledger (TS §1.5c; migration 128) as the **sole source of truth** for side-effect state; a **mandatory pre-retry reconciliation barrier** (TS §6.6 rule 6) must resolve every ledger row of a concluded attempt before a new attempt opens — `confirmed_complete` ⇒ preserved, never repeated; `confirmed_not_started` ⇒ safe retry; `unknown`+idempotent ⇒ retry with the fresh attempt-scoped key; `unknown`+non-idempotent ⇒ **automatic retry blocked** (`blocked_on_side_effect_reconciliation`), requiring audited human reconciliation in the Center. The system never invents a successful reconciliation. |

Supersession: ADR-015 amended (ledger + barrier; supersedes the F-20-rule-4 wording that allowed `unknown` non-idempotent tools to re-issue with only pre-approved scope + audit). Migration 128 now owns `automation_side_effect_ops` (TS §1.7 + roadmap R5). Acceptance tests §10.41–44 added; roadmap R5/R6/R7, architecture doc §6.1/§6.3, and the acceptance matrix updated in sync. Status remains **Designed** — nothing is claimed implemented.
