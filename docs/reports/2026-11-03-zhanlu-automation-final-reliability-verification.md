# Zhanlu Automation — Final Reliability & Contract Verification (F-22 … F-25) + Two-Defect Closure (F-26 / F-27)

**Date:** 2026-11-03
**Role frame:** principal distributed-systems architect / backend reliability engineer / agent-runtime engineer.
**Repo state:** branch `fix/automation-chat-artifacts-ordering`; HEAD moved twice during this pass due to concurrent user work (`12030dd5` → `6c5fcc8d9` → `7865c966`) — all user changes preserved; documentation-only pass; no production code, migrations, records, config, or services touched.

**Source evidence inspected:** `backend/sandbox_worker/main.py` (worker→PostgreSQL event persistence precedent; Redis-optional polling fallback); `backend/app/services/notification_gateway/provider.py` (`SmtpProvider.send`, `_build_message`, `_send_smtp` — no idempotency parameter, no `Message-ID`, 30s socket timeout, error classification); `backend/app/services/tool_handlers/erp_submit_tool.py` (ledger-layer idempotency + `idempotent_replay` + audit events); `backend/app/services/agent_loop/tool_executor.py` (no mid-tool-call cancellation checkpoints); `automation_dispatcher.py` / `automation_executor.py` (CAS patterns, `parent_execution_id` conflation, `_ensure_schema`).

---

## F-22 — Subprocess event durability

**The critical question, answered honestly:** JSONL framing + sequence numbers + parent ACKs do **not** make events durable — a child crash destroys its un-ACKed in-memory buffer. The protocol must therefore make an explicit, provable durability claim, and only for the events that need it.

**Options evaluated.**

| Option | Telemetry durability | Critical-event durability | Complexity | Verdict |
|---|---|---|---|---|
| A — durable child-side spool (flush-before-send, delete-after-commit-ACK, replay) | Full | Full | High: spool lifecycle, partial-write handling, cleanup inside the child | Rejected: durability for UI-progress events is not worth a filesystem protocol in the child |
| B — parent-owned durable broker (all events ACKed after commit) | Full | Full | Medium-high: every telemetry frame costs a synchronous commit round-trip | Rejected: same correctness as C for critical events, much higher overhead |
| C — **bounded-loss telemetry + durable critical events** | Bounded, counted loss (permitted) | **Exactly-durable after parent commit** | Lowest: one sync round-trip per critical event; matches `sandbox_worker` precedent; zero new infrastructure | **Selected** |

**Selected protocol (normative — tech spec §6.5).** Frames `{event_id, execution_id, attempt, seq, critical, type, payload}`; `seq` monotonic per `(execution_id, attempt)`. Critical events (`lifecycle`, `approval_*`, `artifact_*`, `error`, `verification`, state-relevant `tool_call_*`, `run_finished`) block the child until the parent ACKs **after DB commit** (`UNIQUE(execution_id, attempt, seq)` absorbs replays). Telemetry ACKs are opportunistic; gaps are permitted and counted (`dropped_telemetry`) — never silently ignored. Output and terminal state are persisted by the parent independently of telemetry. Backpressure: critical events block (cannot overflow); telemetry drops at capacity (10k ingest queue + pipe capacity). Retention: 90-day event purge — the ledger is observability, not a durable log.

**Crash matrix (9 scenarios):** all specified (tech spec §6.5) — the load-bearing outcomes: #4 (parent commits, crashes before ACK) → event durable, child replay absorbed by the unique index; #8 (parent DB down) → critical events stall in the pipe, **no false ACKs are ever possible** because ACK follows commit; #1/#3 → the un-sent critical event is lost *with the attempt*, and correctness survives because terminal state/output are parent-derived (§6.2b), not telemetry-derived.

**Guarantee statement (weakened to what the mechanism proves):** *zero loss for critical events after parent commit; bounded, counted loss for telemetry; no claim of zero total event loss.*

---

## F-23 — Cancellation and external side-effect semantics

**Verified mechanics.** Cooperative cancellation checkpoints exist **between SSE chunks / between tool calls**; the tool executor has no mid-call checkpoint — so under cooperative cancellation a running tool **always completes**. Only subprocess `terminate()` (hard kill, stage 2) can interrupt a tool mid-call, after which an in-flight external request is in an **unknown** state.

**Design (normative — tech spec §6.6).** Three concepts separated: cancellation requested ≠ process terminated ≠ side effects confirmed stopped/reconciled. Rules: side-effecting tools carry Gate B metadata + attempt-scoped idempotency keys (`erp_submit_tool.py` ledger idempotency + `idempotent_replay` + audit is the verified house precedent); after cancellation/kill a `side_effect_reconciliation` event records `confirmed_complete | confirmed_not_started | unknown` per outstanding call; **a completed irreversible side effect followed by cancellation leaves the execution `cancelled`** — status is never rewritten to succeeded/failed; the side effect is surfaced in the reconciliation event, audit trail, and execution UI ("Cancelled — 1 external action had already completed"); `unknown` non-idempotent tools are never auto-retried (originally F-20 rule 4; as amended by F-27, the pre-retry reconciliation barrier §F-27 below governs unknown-outcome retries — unknown non-idempotent blocks automatic retry until audited human reconciliation); every cancellation and reconciliation is audited. A process kill never "undoes" an accepted external request — the docs now say so explicitly.

---

## F-24 — SMTP and email delivery guarantees

**Verified facts (`provider.py`):** `send()` has no idempotency parameter; `_build_message` sets only Subject/From/To (no `Message-ID`); 30s socket timeout; `SMTPAuthenticationError`/`SMTPRecipientsRefused` → `EmailPermanentError`; `SMTPException/socket/TimeoutError` → `EmailTransportError` — i.e., **a timeout (the ambiguous case) is currently classified as retryable transport error, which is exactly the duplicate-production path**.

**Corrections applied (normative — tech spec §1.5b, §6.4b; ADR-011 amended).**

- Guarantee downgraded to fully honest: **at-least-once delivery-*attempt* semantics; duplicate messages possible.** The prior "dedupable at the receiver" phrasing is removed everywhere — a deterministic `Message-ID` identifies and traces the message and *may* enable downstream dedup where relays/clients honor it; **that is not a guaranteed property** (SMTP relays and recipient servers are under no obligation to dedup).
- Classification rule pinned: SMTP timeout → `unknown` (ambiguous acceptance), not plain retry; permanent errors → `failed_permanent`; manual retry of `unknown`/`failed_permanent` is an audited operator action.
- `Message-ID = f(idempotency_key)` remains specified (R9's minimal provider change: optional `message_id`/headers param) — as identification/tracing, not as a dedup mechanism.
- Chat/panel: idempotent visible-state upserts — the one channel where effectively-once is defensible.
- No provider change, no new infrastructure (per scope).

---

## F-25 — Finalization and recovery verification

**Verified.** The `claimed → running → finalizing → terminal` protocol's ordering (output+provenance persisted **before** the `running→finalizing` CAS; terminal CAS fenced) makes the recovery discrimination decidable from row state alone:

| Observed state | Recovery action |
|---|---|
| `finalizing`, output present, lease expired | Finalize-replay, SAME execution (idempotent steps) |
| `running`, lease expired, output present | Finalize-replay (persist raced the CAS) |
| `running`/`claimed`, lease expired, no output | **F-27 barrier first** (reconcile the side-effect ledger §1.5c per the §6.6 four-outcome table), then new attempt row (same occurrence; `UNIQUE(occurrence_id, attempt)`) — see §F-27 |
| Terminal set, late fenced write | Rejected by fencing — duplicate finalization impossible |
| Terminal write outcome unknown (connection lost) | **Read the row — the DB is the truth**: committed terminal honored; uncommitted → finalize-replay |
| DB failure during terminal persistence | Row remains `finalizing`; no partial terminal state can exist |

All four required recovery distinctions (reuse same execution / finalize existing output / create new attempt / mark ambiguous) map to observable row state — no heuristic guessing. Lease renewal continues through `finalizing`; worker death, stale-runner writes, artifact-render failures, delivery failures, and cancellation-during-finalization are each specified (tech spec §6.2b). **Retries never silently repeat completed side effects:** new attempts carry fresh attempt-scoped idempotency keys (Gate B); non-idempotent re-issues are restricted and audited (F-23 rule 4, as amended by the F-27 barrier — §F-27 below).

---

## F-26 — Event-sequence uniqueness under retries (two-defect closure, part 1)

**Defect.** The F-22 design pinned `UNIQUE(execution_id, seq)` on critical events. Sequences are per-attempt (each attempt starts at 0), so under retries/lease-takeovers attempt 2 would collide with attempt 1's rows — either spurious unique violations (events silently lost) or forcing per-execution sequences that make recovery attribution ambiguous.

**Resolution (verification against the mechanism).** Attempt-scoped identity: **`UNIQUE(execution_id, attempt, seq)`** (tech spec §1.4; the `attempt` column is added by migration 128, R5). Consequences verified:
- Same-attempt replay (reconnect re-send of an un-ACKed critical event) is absorbed idempotently by the unique index — no duplicate row, no error surfaced (§10.41).
- Different attempts start their own sequence at 0 — cross-attempt reuse is legal and cannot collide (§10.42).
- Parent ACK frames carry `{execution_id, attempt, committed_upto}`, so an ACK can only apply to the attempt that sent the events; recovery attributes every event to its attempt — cross-attempt confusion is structurally impossible.
- §6.5 gap detection keys `last_critical_seq` per `(execution_id, attempt)`; crash-matrix scenario 4 rewritten accordingly.

No remaining path writes `UNIQUE(execution_id, seq)` — package-wide sweep confirmed (TS, ADR-014, both review reports).

## F-27 — Mandatory pre-retry side-effect reconciliation barrier (two-defect closure, part 2)

**Defect.** The F-25 recovery rule "`running/claimed + lease expired + no output → new attempt`" implicitly equated "no persisted output" with "no side effect" — false whenever the attempt dispatched an external operation (ERP submission, email, write-back) before crashing. A new attempt could silently re-issue an irreversible operation, or recovery could proceed while an external system held an effect nobody recorded.

**Resolution.** A dedicated intent-and-outcome ledger, `automation_side_effect_ops` (tech spec §1.5c; migration 128), records every external side-effecting op: `op_key = {attempt}:{tool_call_seq}:{tool_name}`, the Gate B `idempotency_key`, status `dispatched → started → confirmed_complete | failed` (reconciliation may set `confirmed_not_started` / `unknown`), `reconciled_at/via`. **The ledger is the sole source of truth for side-effect state; `output_text IS NULL` is never evidence that nothing happened.**

**The barrier (tech spec §6.6 rule 6, normative):** before any new attempt opens — retry scheduling, lease-recovery re-claim, or forced `Run now` — every ledger row of the concluded attempt must be resolved via `provider_check` → `ledger_replay` → (window age-out) `unknown`. Four outcomes, four consequences:

| Outcome | Consequence |
|---|---|
| `confirmed_complete` | Op preserved, **never repeated**; verified result carried into the new attempt |
| `confirmed_not_started` | Safe automatic retry |
| `unknown` + idempotent tool | Retry allowed under the fresh attempt-scoped key (provider dedup bounds duplicates); audited |
| `unknown` + non-idempotent tool | **Automatic retry blocked** (`blocked_on_side_effect_reconciliation`); audited human reconciliation in the Center (`reconciled_via=operator`) unblocks a manual retry |

Honesty rule: **the system never invents a successful reconciliation when the external system cannot establish the outcome.** ADR-015 amended accordingly (superseding the F-20-rule-4 wording that permitted `unknown` non-idempotent re-issue with pre-approved scope + audit only). Barrier sequencing: applied before the new attempt's first tool dispatch; a partially-reconciled attempt never runs concurrently with a new one.

---

## Guarantee → enforcement matrix (brief §6)

| Guarantee | Enforcement mechanism | Honest? |
|---|---|---|
| No duplicate confirmation | PG transaction + `UNIQUE(org_id, confirmation_key)` partial index + replay-first row lock | Yes |
| No duplicate occurrence | Durable occurrence identity + `UNIQUE(automation_task_id, occurrence_key)` + atomic claim | Yes |
| No stale worker writes | `lease_token` fencing on terminal/event/delivery writes | Yes (optimistic fencing; orphan resource burn documented) |
| No lost *critical* events | Parent ACK-after-commit + `UNIQUE(execution_id, attempt, seq)` + blocking send | Yes — telemetry loss explicitly bounded/counted |
| No duplicate delivery *claims* | `UNIQUE(execution_id, channel)` + atomic claim + `claim_token` | Yes |
| No duplicate *external* side effects | Tool/provider idempotency (Gate B) + attempt-scoped keys + reconciliation; **impossible to guarantee for non-idempotent providers — guarantee weakened accordingly** | Yes |
| No repeated side effects *across attempts* (F-27) | `automation_side_effect_ops` ledger + pre-retry reconciliation barrier; `confirmed_complete` never repeated; `unknown` non-idempotent blocks automatic retry (human reconciles) | Yes — barrier never invents outcomes; unknown non-idempotent ops stay blocked, not guessed |
| Safe cancellation | Cooperative signal (between tool calls) + `terminate()` at deadline + side-effect reconciliation + audit | Yes |
| Safe finalization | Persisted output before `finalizing` + fenced terminal CAS + read-the-DB recovery | Yes |

Where a guarantee could not be achieved (external exactly-once delivery, receiver-side dedup, zero telemetry loss), the documents now state the weaker, true guarantee instead.

---

## Acceptance tests added (tech spec §10.37–44)

37 — F-22 nine-scenario crash matrix; 38 — F-23 cancellation/side-effect reconciliation (incl. "cancelled after irreversible side effect"); 39 — F-24 SMTP timeout→`unknown` classification + no-dedup-claim; 40 — F-25 PostgreSQL fault-injection of the recovery-discrimination table; 41 — F-26 same-attempt event replay absorbed by `UNIQUE(execution_id, attempt, seq)`; 42 — F-26 cross-attempt sequence reuse with per-attempt ACK attribution; 43 — F-27 crash after external acceptance before output persistence → barrier confirms completion, op never repeated (stub provider counts invocations); 44 — F-27 unknown non-idempotent blocks retry + operator reconciliation unblocks; `confirmed_not_started` safely retried. Roadmap R6/R7 test references updated (§10.41–43 → R6; §10.44 → R7); R5 owns the migration/ledger itself, no new test item.

## Remaining risks

1. Telemetry loss is user-visible only as UI gaps (counted server-side) — accepted by design.
2. Email duplicates remain possible in the crash/timeout window; detection via Message-ID, mitigation via reconciliation policy and operator-controlled retry.
3. Optimistic fencing cannot reclaim already-spent LLM budget from an orphan — bounded by the wall clock; subprocess hard-kill (stage 2) is the structural fix.
4. `side_effect_reconciliation` for `unknown` states depends on tools honestly reporting Gate B metadata — enforced at the registry, audited, but ultimately per-tool authoring quality.
5. F-27 barrier adds a recovery-latency dependency on the external provider check (`provider_check` round-trip) before a retried attempt can start; bounded by the reconciliation window, but a slow/unreachable provider delays retry. Accepted: correctness over latency for side-effecting runs; read-only runs have empty ledgers and skip the barrier scan entirely.

## Final readiness verdict

**`READY FOR FINAL ARCHITECTURAL APPROVAL`**

Evidence: all four findings resolved with mechanisms verified against source (provider.py, erp_submit_tool.py, tool_executor.py, sandbox_worker/, dispatcher/executor CAS patterns); every guarantee in §6 names its enforcement point and, where weakened, the documents state the weaker true guarantee; all failure scenarios have specified acceptance tests; affected documents (tech spec, architecture, roadmap, ADR-004/011 + new ADR-014/015, consistency review, correction pass, acceptance matrix) are synchronized; an independent consistency review follows this report (see correction-pass §"Independent review round 3"). No production code or user changes touched.

**Addendum (2026-11-03, two-defect closure F-26/F-27):** the residual event-uniqueness conflict (`UNIQUE(execution_id, seq)` vs per-attempt sequences) and the unsafe "no output ⇒ no side effect" recovery assumption were closed with the attempt-scoped event identity (`UNIQUE(execution_id, attempt, seq)`, ADR-014 as specified) and the `automation_side_effect_ops` ledger + mandatory pre-retry reconciliation barrier (ADR-015 amended; four-outcome table; no invented reconciliations; unknown non-idempotent blocks automatic retry). Both remain **Designed** — documented with named enforcement points and acceptance tests §10.41–44; nothing is claimed implemented or tested. All seven package documents updated in sync; a fresh independent consistency review follows (correction-pass round 4). The verdict is unchanged.
