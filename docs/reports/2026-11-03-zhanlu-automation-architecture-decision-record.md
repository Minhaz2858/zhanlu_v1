# Zhanlu Automation — Architecture Decision Records

**Date:** 2026-11-03
**Status:** Proposed — awaiting architectural approval
**Context docs:** audit (`2026-11-03-zhanlu-automation-system-architecture-audit.md`), target architecture, technical specification, roadmap (same date).
**Format:** each ADR states Context → Decision → Alternatives considered → Trade-offs → Consequences. Rejected alternatives are recorded to prevent re-litigation without new evidence.

---

## ADR-001: Scheduler — PostgreSQL-backed durable scheduling with claims + leases, executed by an advisory-lock-elected in-process leader

**Context.** The audit (B3, B4, B5, S1, S2) showed the scheduler cluster is single-process by assumption: no leader election on the automation dispatcher, in-memory workflow dedup, destructive startup reap, in-memory retries, a CAS→INSERT lost-fire window, and an unrenewed advisory lock. Options: (1) harden in-process with an advisory lock; (2) Postgres claims + leases under an elected leader; (3) a dedicated scheduler/queue service (Temporal, Celery Beat, custom).

**Decision.** Option 2. Logical occurrences are persisted in `automation_occurrences` with `UNIQUE(automation_task_id, occurrence_key)` as the duplicate-claim arbiter; execution **attempts** reference the occurrence (`occurrence_id`, `UNIQUE(occurrence_id, attempt)` — retry identity is separate from occurrence identity); running executions hold heartbeat-renewed leases with fencing tokens; one dispatcher leader per cluster is elected per-tick via `pg_try_advisory_lock` on a dedicated connection released at tick end; startup recovery reclaims only expired leases.

**Alternatives.**
- *Option 1 (advisory lock only)*: cheapest, but leaves four core gaps (no fencing → B4/S1 remain, no occurrence ledger → B5 remains, retries still process-bound). Rejected.
- *Option 3 (Temporal/Celery/custom)*: strongest guarantees, but adds a second orchestration stack, a new failure domain, and operational burden the team does not carry today; current load (minute-granularity, tens–hundreds of definitions) gives Postgres ~3 orders of magnitude headroom. Rejected for now; the occurrence-ledger schema is deliberately queue-agnostic should volume ever justify migration.

**Trade-offs.** Postgres becomes the scheduling transactional substrate (tick load is trivial; index maintenance negligible). Lease tuning (30s heartbeat) trades recovery latency (≤2× heartbeat) against DB write volume. Exactly-once is scoped honestly: at-most-once dispatch per occurrence, at-least-once recovery; business side effects key on `execution_id`+`attempt`.

**Consequences.** B3/B4/B5/S1/S2 fixed mechanically; `reap_on_startup` semantics change (breaking for anyone relying on force-reap — escape hatch flag added); retries become crash-survivable.

---

## ADR-002: Creation lifecycle — server-side draft + confirmation-key confirm; LLM proposes, never creates

**Context.** Audit B2/lifecycle: two creation paths, no idempotency, no user confirmation; the chat draft card is client-only; tasks can be committed mid-turn without review.

**Decision.** One lifecycle: `propose_automation` (drafts only) → server-validated review card → `confirm(confirmation_key, If-Match)` (exactly-once, unique partial index backstop) → definition. The `create_automation` tool and the entity-API write surface are retired behind `AUTOMATION_CREATION_V2`. The normative confirmation transaction (draft row lock + `ON CONFLICT (org_id, confirmation_key) DO NOTHING` arbiter + stable-ID replay + crash-safe single transaction) is specified in the technical spec §4.2.

**Alternatives.**
- *Keep agent-tool creation + add idempotency keys to it*: preserves "one-step creation" but keeps LLM-side duplication pressure and the no-review hazard; confirmation remains unverifiable. Rejected.
- *Keep both paths, converge validation*: reduces UX divergence but preserves dual write surfaces (the root of B9/B13). Rejected.

**Trade-offs.** Creation becomes two-phase (slightly more friction for power users); the chat fastpath (`AUTOMATION_FASTPATH_ENABLED`, already default-off, `config.py:1444`) is formally retired. Benefit: the LLM is structurally incapable of bypassing confirmation; drafts are durable across browser loss.

**Consequences.** Chat prompt/tooling changes; legacy rows keep working via compat mapping (roadmap R3/R8).

---

## ADR-003: Agent Invocation Contract — extracted service, phase-1 adapter over the v3 chat handler

**Context.** Verified coupling (N1): the executor fakes DI into `add_message_stream` (agents.py:12148, ~8k lines) and `resume_conversation`, regex-parses SSE, and shares state via contextvars. `agent_loop/` is an extracted mechanics package but not an entry point; no HTTP-free invocation API exists.

**Decision.** New `services/agent_invocation.py` with a typed contract (context, request, typed events, cancellation, budgets, provenance); phase 1 is a behavior-preserving adapter over the existing handlers; phase 2 progressively re-sources internals from `agent_loop/`. The chat router keeps its own path initially.

**Alternatives.**
- *Extract a full new runtime for automation*: duplicates the agent runtime — violates Invariant 2.3 and the audit's core finding that the existing runtime works. Rejected.
- *Call the HTTP endpoint via test-client-style ASGI invocation*: keeps the coupling, adds transport overhead and auth-token gymnastics. Rejected.
- *Refactor `add_message_stream` first, then wrap*: riskiest possible order (8k-line handler before any automation benefit). Rejected; adapter-first inverts the risk.

**Trade-offs.** Temporary double maintenance of event translation (adapter) until phase 2; SSE parser pinned by golden-frame tests. Benefit: automation no longer breaks when chat internals change; provenance (`agent_run_id`) is captured at the seam.

**Consequences.** Static test forbids `automation_* → app.routers.agents` imports (acceptance §10.20).

---

## ADR-004: Execution isolation — fencing + leases now, subprocess runners for hard kill

**Context.** Timeout/cancel are cooperative; the agent thread is deliberately left to finish (audit B6; code comment `automation_executor.py:1263-1266`). Python threads cannot be safely force-terminated.

**Decision.** Stage 1: leases + heartbeats + fencing tokens on all terminal/side-effect writes; orphan event suppression. Stage 2 (flagged, default off): runner executes the agent invocation in a spawned child process; parent owns persistence and terminal writes; hard kill = `process.terminate()`.

**Alternatives.**
- *Threads + stronger checkpoints only*: never guarantees termination; orphans persist under pathological LLM hangs. Rejected as end-state (kept as stage 1 because it is 80% of the value).
- *Container-per-run (sandbox worker model everywhere)*: strongest isolation but heavy per-run latency/cost for report-generation workloads; the existing `sandbox_worker` remains for code execution, not agent turns. Rejected for now.
- *kill -9 the whole worker process per run (process-per-run in the main service)*: interferes with the web process model. Rejected.

**Trade-offs.** Subprocess adds spawn overhead (~hundreds of ms) and IPC complexity; gated behind a flag until the chaos matrix (R11) passes. Parent-owned persistence means a killed child cannot write state — the structural guarantee that closes B6. **Corrected by the final correction pass (F-21):** the child cannot be DB-free (the production agent runtime requires DB access), so ownership is enforced as a **table-scope contract** — the child runs the runtime with its own session for agent-runtime tables and receives no handle to the automation persistence layer; events cross the process boundary as seq-numbered JSONL frames persisted by the parent; cancellation/deadlines propagate over the control pipe plus parent-side `terminate()` at deadline.

**Consequences.** Budgets enforced at the contract layer; orphan side effects bounded to already-flushed stream content, which fencing suppresses from persistence.

---

## ADR-005: Approvals — pre-approved scope replaces silent skip_confirmation

**Context.** Audit N9/B11: the executor auto-approves governance pauses (`reviewed_by="automation:skip_confirmation"`, unaudited); enterprise rule: unattended automation must not gain unapproved authority.

**Decision.** `skip_confirmation` retired. The confirmed spec declares `pre_approved_scope` (tool classes below the org risk wall). Pauses inside scope proceed **with an audit event naming the scope class**; all other pauses create an `approval_requests` row and, unattended, fail the run `failed(approval_required)` after `approval_ttl`. Existing boolean tasks are migrated to explicit scope (failure-strict: unmapped tasks get empty scope).

**Alternatives.** Keep-and-audit skip_confirmation: preserves convenience but ratifies silent authority expansion — contradicts the brief. Human-approve-everything-unattended: safest but breaks the product's core value (unattended execution). Rejected.

**Trade-offs.** Some formerly-completing runs will now fail pending approval — intentional, surfaced in the Center with a one-click approve-and-resume (attended) path.

---

## ADR-006: Access policy — one policy function; entity-API write surface retired

**Context.** Audit B9/B16: three inconsistent policies (owner-only entity API, org-wide `/api/automations/*`, owner-only chat tool) over the same rows; the Center's owner-scoped, string-project-filtered, capped list hides valid tasks.

**Decision.** Single `authorize()` (org + owner + role, per-action matrix in spec §3.4); Center/chat/executor resolve through it; `/definitions` gains server-side filters and pagination; entity-API writes for AutomationTask return 410 under the flag.

**Alternatives.** Keep the entity API and fix its filters: preserves a second write surface and the two-models-one-resource confusion. Rejected. Org-wide access for all actions: simplest, but fails the enterprise ownership requirement. Rejected.

**Trade-offs.** Clients using `base44.entities.AutomationTask` writes must migrate (frontend is the only known consumer — verified in the audit's component map).

---

## ADR-007: Storage — MinIO for automation artifacts, local fallback; delivery failure-isolated

**Context.** Artifacts write to local FS (lost on container recreation); MinIO is already deployed (N6); artifact render failure currently fails whole runs (B7).

**Decision.** `ARTIFACT_STORAGE_BACKEND=minio|local` (default minio when configured); `automation_files.storage_key`; render isolated (`artifacts_failed=true` never fails the run); per-channel delivery events with one retry, keyed uniquely per execution.

**Alternatives.** DB BLOB storage: simple but bloats Postgres and fights the existing file-serving code. S3 direct: adds an external dependency MinIO already abstracts. Rejected.

**Trade-offs.** Two backends to test; existing local artifacts not backfilled (documented limitation; historical runs keep their local files until container loss — accepted, flagged in runbook).

---

## ADR-008: Schema governance — Alembic-only; runtime DDL shims become a drift checker

**Context.** Audit B15: three DDL mechanisms with demonstrated divergence (`parent_execution_id`, `email_notified_at`); migration 059's own text admits executions/files were historically created only by `create_all`.

**Decision.** All future automation schema via Alembic (126+); `_ensure_schema()` reduced to log-only drift checking; `main.py` automation ALTER entries deleted after 126–129 ship.

**Alternatives.** Keep shims for "deployments that skip Alembic": preserves the drift that caused B15 and the audit's type-mapping divergence (`DATETIME` vs `TIMESTAMP`). Rejected; deployment procedure (prestart alembic) is the fix.

**Trade-offs.** Environments that silently skipped Alembic will now fail loudly — intentional.

---

## ADR-009: Workflow-scheduler consolidation — share the claim mechanism, keep separate product semantics

**Context.** N5: `WorkflowScheduler` fires flow_mode AgentApps (node-graph runtime, `run_workflow_agent`) — a genuinely different resource from chat-agent automations, but with weaker guarantees (in-memory dedup → guaranteed duplicate fires across processes, B3).

**Decision.** *(Corrected by the final correction pass, F-15.)* Give workflows their **own occurrence table** (`workflow_occurrences`, FK to the workflow subject: `agent_app_id` + `node_id`) with claim/lease/fencing/recovery executed by the **shared `scheduler_core` service** parameterized per subject table, under the same per-tick leader; do **not** merge workflow triggers into automation definitions and do **not** overload `automation_occurrences` with a `subject_type` column — that would break the FK to `automation_tasks` and blur retention. Mechanics are shared; identity, constraints, and lifecycle are per-subject.

**Alternatives.** Full unification into one trigger model: conflates two products with different execution runtimes (node graph vs chat agent) and different definitions of "run". Rejected. Leave workflow scheduler untouched: leaves a guaranteed duplicate-fire defect. Rejected.

**Trade-offs.** Two consumers of one mechanism — slightly more coupling in `scheduler_core.py`; acceptable versus maintaining two claim systems.

---

## ADR-010: Status vocabulary — real semantics for every state; no status inflation

**Context.** Audit B14/B17/B1: `skipped`/`timed_out`/`succeeded` dead or legacy; `deleted`/`failed`/`completed` misused on the definition; partial results handled by warnings.

**Decision.** Definition: `active|paused|archived` only (outcomes live on executions; once-triggers auto-archive). Occurrence: `pending|claimed|completed|failed_terminal|cancelled|skipped`. Execution: `claimed|running|awaiting_approval|finalizing|succeeded|failed|cancelled|timed_out|skipped`, terminal immutable + fenced, with typed `cancel_reason` values (`user|admin|definition_deleted|org_deactivated|parent_cancelled|superseded`). Partial success is a verification/delivery annotation, not a status.

**Alternatives.** Add `partial` execution status: another state to keep consistent across 6 consumers for information already expressible in events/tallies. Rejected.

**Consequences.** Frontend vocab aligned (R10); legacy rows reconciled in migration 129.

---

## ADR-011: Delivery guarantees are honest and per-channel (F-13)

**Context.** A worker can crash between an SMTP send and its success persistence; retrying would duplicate the email. The prior draft promised one retry for all channels, which silently claims exactly-once external delivery.

**Decision.** Delivery state machine `pending → claimed → delivered | failed_permanent | unknown` with durable claims (`claim_token`/`claim_owner`/`claim_expires_at`, 5-min expiry, token-compared writes). Per-channel honesty: chat/panel = effectively-once visible state (idempotent upserts); **email = at-least-once delivery-*attempt* semantics with possible duplicate messages** (verified: `SmtpProvider.send()` has no idempotency parameter, `_build_message` sets no `Message-ID`, and the 30s socket timeout maps to `EmailTransportError` — the ambiguous-acceptance case). The deterministic `Message-ID = f(idempotency_key)` added by R9 provides identification/tracing and *optional* downstream dedup where relays/clients honor it — **a deterministic Message-ID alone is not a dedup guarantee and is never claimed as one** (final correction, F-24). `unknown` outcomes (crash between send and persist, ambiguous timeout) are reconciled, never blindly retried; retrying an `unknown` requires explicit opt-in (`delivery_retry_unknown=false` default). Independent channel failures never re-run the agent.

**Alternatives.** Transactional outbox with provider two-phase commit: no such thing for SMTP. Provider-acked queues (SES/SendGrid idempotency): a provider migration, not an architecture property — noted as an option if the email provider changes. Rejected for now.

**Consequences.** Duplicate emails remain possible in a narrow crash window but are detectable (same Message-ID) and reconcilable; the reconciliation window is a documented operational knob.

---

## ADR-012: Authorization and side-effect safety are independent gates (F-20)

**Context.** Human approval (Gate A) and retry safety (Gate B) answer different questions; conflating them means an approved non-idempotent write could be silently retried into a duplicate side effect.

**Decision.** Two registry-enforced gates, evaluated independently at tool dispatch: Gate A = RBAC + spec constraints + org unattended permission + `pre_approved_scope`/ApprovalService (existing mechanisms, unchanged); Gate B = new registry metadata `idempotent`/`idempotency_key_scope`/`reconcilable` + attempt-scoped idempotency keys from the invocation context + `unknown` handling for ambiguous timeouts. Approved-but-non-idempotent tools run at most once per attempt; execution-level retries give new attempt-scoped keys and rely on the tool's own re-application guard. No prompt-level governance.

**Consequences.** New registry metadata is a small, per-tool authoring obligation; the two-gate matrix (approved×idempotent) is testable (acceptance §10.36).

---

## ADR-013: Misfire representative slot = the LATEST missed slot; grace measured against it (F-16)

**Context.** The prior draft keyed the collapsed occurrence on the first missed slot but evaluated grace ambiguously ("evaluated against now"). Deterministic behavior requires an explicit rule.

**Decision.** Missed interval = `[next_run_at, now)`; representative slot = **latest** missed slot; staleness = `now − latest_slot`; within grace → one late fire under the latest slot's `occurrence_key` with `misfire_collapsed_count = |slots|`; beyond grace → one `skipped(misfire)` row; `next_run_at` advances past `now` via CAS in the same transaction. Chosen product behavior (not an established prior decision): "fire late" means "the schedule freshly came due during the outage". Edits-during-downtime and pause discard the un-evaluated episode with an audit event. Deterministic worked examples are normative (tech spec §6.1a) and unit-tested.

**Alternatives.** Earliest-slot representative (always skips long outages even when freshly due); fire-all-missed-slots (unbounded backlog — forbidden by the brief); per-slot enumeration with a cap (arbitrary cap, still enumerates). Rejected.

---

## ADR-014: Subprocess event durability — Option C (bounded-loss telemetry + durable critical events) (F-22)

**Context.** JSONL framing, sequence numbers, and parent ACKs do not by themselves make events durable: a child crash destroys its in-memory un-ACKed buffer. Investigated against the actual `sandbox_worker` precedent (worker→PostgreSQL event persistence).

**Decision.** Option C. Events are typed `critical | telemetry`. Critical events (lifecycle, approval, artifact, error, verification, state-relevant tool calls, run_finished) are **synchronously ACKed by the parent only after the event row is committed** — the child blocks on that ACK (natural backpressure; ≤1 in flight), duplicates are absorbed by `UNIQUE(execution_id, attempt, seq)`, and reconnect replays un-ACKed critical events. Telemetry (progress, plan steps, deltas) is best-effort with bounded, **counted** loss and permitted gaps. Final output and terminal state are persisted by the parent independently of telemetry delivery.

**Alternatives.** (A) durable child-side spool: strongest telemetry durability but adds spool lifecycle/partial-write handling inside the child for a property the UI does not require — rejected as unnecessary infrastructure. (B) parent-owned durable broker for everything: equals C plus telemetry durability at the same cost — rejected; C is strictly smaller with the same correctness envelope.

**Limitation stated honestly:** zero event loss is guaranteed **only for critical events after parent commit**; telemetry may be lost (bounded, counted); a critical event the child never sent is lost with its attempt (terminal state is parent-derived, so recovery correctness does not depend on it).

---

## ADR-015: Cancellation ≠ termination ≠ side-effect reconciliation (F-23)

**Context.** Killing a subprocess cannot undo an external request another service already accepted; the prior documents did not state the final execution status when cancellation follows an irreversible side effect.

**Decision.** Three separate concepts: cancellation requested (signal), process terminated (worker stop), side effects confirmed stopped/reconciled (per-tool outcome). Verified mechanics: cooperative cancellation checkpoints sit **between** tool calls, so a running tool completes; only subprocess `terminate()` can interrupt mid-call, leaving Gate B `unknown` states resolved per §11.3. Rules: side-effecting tools accept attempt-scoped idempotency keys (`erp_submit_tool.py` ledger idempotency is the house precedent, verified); after cancellation/kill a `side_effect_reconciliation` event records `confirmed_complete | confirmed_not_started | unknown` per outstanding call; **the execution stays `cancelled`** — a completed irreversible side effect is surfaced in the reconciliation event/audit/UI, never rewriting the status to succeeded/failed; every cancellation/reconciliation is audited. Gate A mechanisms untouched.

**Amended (F-27): side-effect ledger + mandatory pre-retry reconciliation barrier.** A dedicated `automation_side_effect_ops` ledger (tech spec §1.5c) records intent (`dispatched`/`started`) and outcome (`confirmed_complete`/`failed`) for every external side-effecting op, keyed `{execution_id, attempt, op_key}` with the Gate B idempotency key carried per op. The ledger — not `output_text` presence — is the **sole source of truth** for whether an external operation occurred. Before any new attempt opens (retry scheduling, lease-recovery re-claim, forced `Run now`), recovery must resolve every ledger row of the concluded attempt via the §6.6 barrier: `confirmed_complete` ⇒ op preserved and never repeated; `confirmed_not_started` ⇒ safe automatic retry; `unknown` + idempotent tool ⇒ retry allowed under the fresh attempt-scoped key (provider dedup bounds duplicates); `unknown` + non-idempotent tool ⇒ **automatic retry blocked** (`blocked_on_side_effect_reconciliation`), requiring audited human reconciliation in the Center before a manual retry. The system never invents a successful reconciliation when the external system cannot establish the outcome. This supersedes the earlier F-20-rule-4 wording that allowed `unknown` non-idempotent tools to re-issue with only pre-approved scope + audit.

---

## Unresolved Questions (for the architecture review)

1. **Org-wide vs owner-only default for run/toggle/delete** (ADR-006): the design defaults to owner-only for destructive actions with a role override — needs an explicit product/owner decision on whether small-team orgs expect shared control (today's `/api/automations/*` behavior is org-wide).
2. **`pre_approved_scope` taxonomy** (ADR-005): the tool-class vocabulary (`db.read`, …) must be ratified against the current tool registry's risk tiers; the migration mapping for existing `skip_confirmation=true` tasks (empty scope vs. inferred scope) is failure-strict by default and may change behavior for existing schedules.
3. **Misfire grace window default** — *partially resolved by the consistency review*: the collapse semantics are now deterministic and bounded (one outcome per definition per downtime episode, tech spec §6.1a). Remaining sign-off: whether the global default grace (1h) is right for business-critical schedules, or whether critical definitions should carry a longer per-definition grace.
4. **Event-ledger retention** (90 days proposed): compliance may require longer; storage cost is trivial but the purge task interacts with audit requirements — confirm with governance owner.
5. **Subprocess-runner adoption timeline** (ADR-004 stage 2): valuable for hard-kill guarantees but not required for correctness under fencing; decide after R11 chaos results whether to promote the flag.
6. **Subprocess runner on the existing single-container deployment** (`zhanlu-backend` on :5002, no `--reload`): memory ceiling for concurrent subprocess runs needs measurement before enabling.
7. **Historic local-FS artifacts** (ADR-007): accept loss on container recreation (current proposal) or fund a one-time MinIO backfill.
8. **`max_llm_calls` budget accounting**: needs a definitive hook point in `agent_loop` (candidate: the existing tool-call cap machinery); exact counting semantics (per turn vs per occurrence) to be pinned during R6 design detail.

---

*Per the assignment: this is a specification-only deliverable. No production code, frontend, migrations, records, or configuration were modified; the working tree's uncommitted changes are untouched. Implementation awaits architectural approval.*
