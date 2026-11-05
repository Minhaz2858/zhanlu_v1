# Zhanlu Automation — Target Architecture

**Date:** 2026-11-03
**Status:** Proposed — awaiting architectural approval
**Basis:** `docs/reports/2026-11-03-zhanlu-automation-system-architecture-audit.md` (same date), re-verified against the working tree at commit `12030dd5` (branch `fix/automation-chat-artifacts-ordering`). All line references in this document were verified 2026-11-03.
**Decision frame:** Option 2 — architectural refactoring of the existing subsystem. The production Agent Runtime (v3 chat loop, FSM planning, `agent_loop/` mechanics, tool registry) is retained and reused; fragmented automation components are consolidated.

---

## 0. Audit Re-Verification Summary (findings that remain valid / corrections)

The audit's B1–B18 and S1–S6 findings were re-verified against current source. **All remain valid.** Material additions discovered during this design pass:

| # | New fact | Evidence | Architectural consequence |
|---|---|---|---|
| N1 | The executor couples to **two** route handlers (`add_message_stream` at `agents.py:12148`, `resume_conversation` at `agents.py:9168`), faking FastAPI DI (`db`, `user` supplied manually) and parsing SSE text frames back with regex | `automation_executor.py:2241, 2281-2302, 2303-2319, 1917` | An Agent Invocation Contract is genuinely new — there is no HTTP-free agent-turn entry point today |
| N2 | `app/services/agent_loop/` already exists as an extracted, router-independent mechanics package (tool_executor, guardrails incl. `pause_for_approval`, sse_builders, fallbacks) | `agent_loop/__init__.py:1-18` | Natural seam for the Agent Invocation API |
| N3 | Approval infrastructure already exists and is reusable: `ApprovalService` (`services/governance/approval_service.py:19`) over table `approval_requests` (`models/governance.py:46-53`) incl. `expire_old_requests()` | Verified | Approval policy needs wiring, not new infrastructure |
| N4 | `AuditEvent` model exists with registered vocabularies (`models/audit_event.py:29-63`); writes are ad-hoc `db.add(AuditEvent(...))` with **no central writer** | `erp_submit_tool.py:108-114`, `nl2sql/__init__.py:665-671` | Governance needs a writer function + automation call sites, not new tables |
| N5 | The workflow engine is a **disjoint runtime** (`run_workflow_agent`, node-graph over flow_mode AgentApps, `workflow_runtime.py:857`); its scheduler polls published flow_mode AgentApps' `trigger_schedule` nodes every 30s, in-memory only | `workflow_scheduler.py:24-64` | Consolidation must define the boundary between "automation task" and "scheduled workflow trigger" rather than merging runtimes |
| N6 | MinIO is already part of the deployment stack (`zhanlu-minio` container) but artifacts are written to local FS | `document_generator.py:306-335` | Durable artifact storage is a configuration/integration task, not new infra |
| N7 | Alembic head is `125_agent_run_provenance` (~194 files); automation-table migrations: 028, 051, 055, 059, 061 | `alembic/versions/125_agent_run_provenance.py:22-23` | Alembic-only evolution is feasible; the two runtime-DDL shims can be retired |
| N8 | `AUTOMATION_FASTPATH_ENABLED = False` by default — the chat fastpath is already flag-gated off | `config.py:1444` | The legacy creation path can be retired with low blast radius |
| N9 | Approval pause/resume state lives in `conv.metadata_._resume_state`; the executor's auto-approve reads it and calls `ApprovalService.approve(reviewed_by="automation:skip_confirmation")` | `automation_executor.py:1894-1915` | "Do not silently auto-approve" requires replacing this exact block |
| N10 | Cancellation is a process-local `threading.Event` registry (`_CANCEL_EVENTS`, `automation_executor.py:120-138`); timeout uses `ThreadPoolExecutor.shutdown(cancel_futures=True)` which cannot kill running threads (code's own comment, `:1263-1266`) | Verified | Hard termination requires a process-boundary design (§6) |

Discrepancies with the audit: none. The 246 uncommitted working-tree changes were present during both investigation passes and are preserved.

---

## 1. Architectural Vision and Responsibility Separation

Four planes, one contract each:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  A. INTERACTION LAYER                                                   │
│     Chat (creation/review/confirm)   Automation Center   Execution UI   │
│     All surfaces speak to ONE canonical resource + ONE API.             │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ Automation API (Control Plane contract)
┌───────────────────────────────▼─────────────────────────────────────────┐
│  B. AUTOMATION CONTROL PLANE                                            │
│     Definition lifecycle (draft→confirmed→active) · ownership · RBAC    │
│     schedule config · approval policy · audit · versioning · deletion   │
│     Canonical source of truth: automation_definitions (+ _drafts).      │
└──────────┬─────────────────────────────────────────────┬────────────────┘
           │ due-occurrence claims                       │ definition reads
┌──────────▼──────────────────────┐   ┌──────────────────▼───────────────┐
│  C. SCHEDULING & TRIGGER PLANE  │   │  (event triggers in later phase) │
│  Single leader (pg advisory     │   │                                  │
│  lock) · DB-backed claims ·     │   │                                  │
│  leases · misfire policy ·      │   │                                  │
│  retry policy · absorbs         │   │                                  │
│  workflow_scheduler triggering  │   │                                  │
└──────────┬──────────────────────┘   └──────────────────────────────────┘
           │ ExecutionRequest (Agent Invocation Contract)
┌──────────▼──────────────────────────────────────────────────────────────┐
│  D. EXECUTION RUNTIME                                                   │
│  Worker isolation · heartbeats/leases · cooperative+hard cancel ·       │
│  observability events · verification · provenance · delivery            │
│         │ invokes (Agent Invocation Contract)                           │
│  ┌──────▼──────────────────────────────────────────────┐                │
│  │  PRODUCTION AGENT RUNTIME (retained, not duplicated)│                │
│  │  v3 chat loop / FSM planning / ReAct / agent_loop/  │                │
│  │  tool registry · RBAC · TraceContext                │                │
│  └─────────────────────────────────────────────────────┘                │
│  The Harness (tool registry RBAC, TraceContext, ApprovalService,        │
│  access policies) governs what the agent may do — never the prompt.     │
└─────────────────────────────────────────────────────────────────────────┘
```

**One-sentence responsibility statements:**

- **Interaction Layer** — presents and edits; never persists tasks directly and never writes execution state.
- **Control Plane** — owns the definition lifecycle, authorization decisions, and the audit record; the only writer of `automation_definitions`.
- **Scheduling Plane** — owns *when* an approved definition produces an execution occurrence; the only creator of `automation_executions` rows (except manual run-now, which it also brokers).
- **Execution Runtime** — owns *how* an occurrence is executed, observed, verified, and delivered; the only writer of execution status and events.
- **Agent Harness / Runtime** — owns *what* the agent may do; unchanged by this refactor except for the Invocation Contract extraction (§5).

Anti-goals (explicitly preserved from the audit): no hardcoded keyword→tool routing, no business-specific execution paths, no fixed/default datasource selection, no governance in prompts, no duplicated agent runtime.

---

## 2. Layer A — Automation Interaction Layer

### 2.1 Canonical resource rule

Every surface (chat review card, Automation Center, detail page, execution drawer) reads and writes **only** through the Automation Control Plane API (`/api/automations/*`). The generic entity-API surface for `AutomationTask` (`/api/apps/{app}/entities/AutomationTask`) is retired for writes (§9 migration); this is the structural fix for audit B9/B16 (three access policies over one row).

### 2.2 Chat creation flow (target)

1. User types an objective. Intent recognition stays where it works today (server-side rebind `agents.py:3039-3056` + agent prompt), with the frontend regex kept only as a routing hint — **recognition may fail back to normal chat; it must never auto-create**.
2. The agent produces a structured `AutomationSpec` via a **new `propose_automation` tool** (schema in the tech spec). `propose_automation` **does not write** `automation_definitions` — it creates a server-side draft and returns a draft id + review payload.
3. The chat renders the review card **from the server draft** (not from parsed message text — fixes the client-only draft problem, audit B2/lifecycle #1). Edits in the card call `PATCH /api/automations/drafts/{id}`; the server re-validates (schedule parse, project resolution, datasource constraint checks) and returns field-level errors.
4. The user presses **Confirm** → `POST /api/automations/drafts/{id}/confirm` with a client-generated `confirmation_key` (idempotency key). The server validates spec version (`If-Match: draft.version`), converts draft → definition **exactly once**, and returns the definition.
5. The page/listens surfaces refresh via a `automation.definition.changed` event (SSE bus already exists in pattern via the executions stream; reuse).

Browser closed mid-flow: draft survives (server-side, TTL 7 days, cleanup task); confirmation pending in another tab is safe (idempotency). Stream dies before proposal: nothing persisted beyond the draft — visible as "Draft (not scheduled)" in the Center, never as a phantom task.

### 2.3 Automation Center (management)

Views and actions (full UX spec in the tech-spec document §7): month/calendar view (from schedule next-run), card view, table view, project + status + search filters (server-side query params), next-run display (same `next_run_at` the scheduler uses — preserved from current code), last execution summary, success/failure stats (aggregated from `automation_executions`), pause/resume, run-now, edit (versioned update), execution history, delete (soft).

### 2.4 Execution detail

Event-driven timeline rendered from a persisted, append-only `automation_execution_events` table (§5) — not from `activity_steps` JSON blobs mutated in place. Shows: plan steps (FSM), tool invocations, datasource usage, agent/delegation activity, verification results, errors, retries, approvals, artifacts, and provenance links (AgentRun/WorkflowRun ids).

---

## 3. Layer B — Automation Control Plane

### 3.1 Canonical Automation Specification

A versioned JSON document stored on the definition (`spec_version`, `spec` JSONB). Schema (normative in tech-spec §2):

```
identity            definition_id, org_id, app_id, created_by_id, project_id
name/description    human strings (unique per (org_id, created_by_id, name) — dedupe guard, audit B2)
objective           natural-language objective + execution_instructions (prompt to agent)
agent_binding       agent_id | runtime (hidden automation_runtime_agent) | null→runtime
trigger             type: schedule | once | manual | event
                    schedule: cron + timezone (IANA) + misfire_policy + grace_window
                    once: run_at datetime; event (phase 2): event_type + filter
datasources         constraints ONLY: allowed_source_ids[] (optional), allow_workspace_auto_bind, policy: "agent_selects"
tools               constraints ONLY: allowed_tool_ids[] (optional), denied_tool_ids[] (optional)
output              format (html|docx|pptx|md|none), delivery channels
notifications       notify_on (always|on_success|on_failure|never), emails, chat_delivery bool
approvals           policy: block | require_review, pre_approved_scope (declared tool classes), max_auto_scope_approvals=0
retry               max_attempts (default 2), backoff (exponential, cap 30m), retry_on (transient|all)
budgets             wall_clock_seconds (default 600), max_tool_calls, max_llm_calls (new), artifact_byte_cap
version             spec_version (int, incremented on edit), status
```

**Design rules enforced by this schema:**
- Datasources and tools are **constraint sets, never selections** — the agent decides at run time which authorized sources/tools to use (Invariant 2.1/2.4). The harness (TraceContext + tool registry + datasource runtime) enforces the constraints; the spec only narrows authority.
- `approvals.pre_approved_scope` replaces today's boolean `skip_confirmation`: unattended runs may proceed **only** for tool classes explicitly declared in the confirmed spec and below the org's risk-tier wall; every other pause → execution state `awaiting_approval` and, for unattended mode, terminal `failed(approval_required)` after `approval_ttl` — **never a silent auto-approve** (fixes audit finding, N9).

### 3.2 Definition lifecycle (distinguishing the five states)

```
DRAFT ──confirm──> CONFIRMED(active) ──pause──> PAUSED ──resume──> ACTIVE
   │                     │                                        │
   │ (ttl expiry)        └──edit(v+1, stays active; applies next occurrence)──┐
   ▼                     └──archive──> ARCHIVED (terminal; is_deleted)        │
 DISCARDED                                                                    │
                       SCHEDULED is NOT a definition state — it is the        │
                       presence of a persisted next_run_at + active status    │
```

- Draft, validated, user-confirmed, persisted, scheduled are **five stages of one lifecycle**, not five statuses: draft rows live in `automation_drafts`; "validated" is a server-computed property returned by PATCH; "confirmed/persisted" is the single `confirm` transition; "scheduled" is derived (`status=active AND next_run_at IS NOT NULL`).
- Definition status enum (target): `active | paused | archived` (+ `is_deleted` soft flag). Fixes audit B1 by never writing `"deleted"` or `"failed"/"completed"` into definition status (run outcomes live on executions, not on the definition — audit B17 resolved by *removing* the misfeature, not automating it; the Center shows last-execution outcome via join).
- **Edit during execution** (audit B7): edits create spec version v+1; the running execution holds `spec_version` frozen at claim time and is unaffected; mid-run edits apply from the next occurrence. No version checks inside the executor hot loop.

### 3.3 Confirmation protocol (exactly-once creation)

- `automation_drafts.confirmation_key` — client-generated UUID, `UNIQUE(org_id, confirmation_key)` on definitions (partial unique index where `is_deleted=false`).
- `POST .../confirm` is transactional: a single transaction holds `SELECT … FOR UPDATE` on the draft, validates the `If-Match` version under the lock, inserts the definition with `ON CONFLICT (org_id, confirmation_key) DO NOTHING` (the database is the idempotency arbiter), and marks the draft `confirmed`. A duplicate confirm with the same `confirmation_key` returns the **original definition** (200, `already_confirmed: true` — stable ID on retries); a stale `If-Match` version returns 409 with the current draft. Crash before COMMIT ⇒ nothing happened (retryable); crash after COMMIT ⇒ replay resolves via the unique index. Normative transaction: tech spec §4.2.
- Concurrent confirms on different drafts with the same name: allowed (name uniqueness is per-owner advisory — the API returns a warning, not an error, since legitimate renames/recreations exist). Duplicate **tasks** from LLM retries become structurally impossible: the LLM can only call `propose_automation` (drafts are cheap and dedupable); only the human confirm creates a definition.
- Version validation: every draft mutation increments `version`; confirm requires `If-Match` = latest version, preventing "confirm what I reviewed" drift.

### 3.4 Ownership, authorization, RBAC

One policy function in the Control Plane:

```
authorize(user, definition, action):
  org mismatch            → 404 (unchanged, current _assert_tenant)
  action ∈ read           → org member
  action ∈ run/toggle/edit/delete  → owner OR org role ≥ editor (configurable; default owner-only, matching the entity API's current posture)
  action ∈ administer     → org admin
```

Every mutation writes an `AuditEvent` (`event_source="automation"`): `automation_created, automation_updated, automation_confirmed, automation_paused, automation_resumed, automation_run_triggered, automation_cancelled, automation_deleted, automation_approval_policy_changed`. Actor is the authenticated user; for unattended actions, actor_type=`system`, actor_id=`automation:{definition_id}:{execution_id}` (closes audit B11).

---

## 4. Layer C — Scheduling and Trigger Architecture

### 4.1 Alternatives evaluated

| Criterion | 1. Improved in-process + advisory lock | 2. PostgreSQL durable scheduling (claims+leases) | 3. Dedicated scheduler/queue service (Temporal/Celery Beat/custom) |
|---|---|---|---|
| Operational complexity | Lowest (no new components) | Low (Postgres only — already the datastore) | High (new service, new failure domain, new deploy artifact) |
| Multi-instance | Only via leader lock; workers still in each web process | Yes — any instance can run dispatcher; claims make it safe | Yes, native |
| Execution guarantee | CAS only, no lease fencing (audit B4/S1) | Atomic claim + lease + heartbeat + fencing token | Exactly-once-ish via durable workflow engines; still at-least-once callbacks |
| Crash recovery | Startup reap only (destructive multi-instance) | Lease expiry reclaims orphans automatically; no destructive startup reap | Engine-managed |
| Missed-run recovery | Fire-once-late only | Misfire policy evaluated from persisted occurrence ledger | Engine-dependent |
| Duplicate prevention | Fire CAS (untested) | `UNIQUE(definition_id, occurrence_key)` + claim CAS — testable in SQL | Engine-managed |
| Retry handling | In-memory delayed runs (dies with process) | Persisted retry rows w/ backoff | Engine-managed |
| Scalability | Vertical | Thousands of defs at 15–60s ticks | Horizontal by design |
| Infra requirements | None | None beyond existing Postgres | New runtime, likely Redis/Temporal cluster |
| Maintenance burden | Current (already showing drift) | One well-understood pattern (outbox/claim) | A second orchestration stack to operate |

**Recommendation: Option 2 — PostgreSQL-backed durable scheduling, executed by an in-process dispatcher loop guarded by a Postgres advisory lock (leader election).** Rationale:

- Zhanlu's actual enterprise requirement is *correct unattended execution with auditability*, not high-frequency throughput; volume is tens–low hundreds of definitions, minute-granularity. Postgres claim/leases handle this with orders of magnitude of headroom.
- It is the smallest architecture that fixes the audit's multi-instance P0s (B3, B4, S1, S2) without adding an operations surface the team does not run today.
- Option 1 is rejected because it leaves the four core gaps (no lease fencing, destructive reap, in-memory retries, no occurrence ledger) unfixed.
- Option 3 is rejected *for now* on YAGNI + operational grounds; the claim/lease schema is deliberately shaped so a future queue backend could adopt the same occurrence ledger without schema redesign. This is **not** an endorsement of Redis/Celery/Temporal by popularity — none is warranted by current load.
- The advisory lock (pattern already proven in `scheduled_tasks.py:875-905`) elects one dispatcher leader per cluster; leadership loss (lock reclaimed elsewhere) is detected by re-acquiring per tick. The `start_scheduled_tasks` lock-renewal defect (audit S2) is fixed by re-checking the lock each tick rather than holding a pooled connection.

### 4.2 Occurrence model (at-least-once dispatch, honest about side effects)

- On confirmation/schedule change, the Control Plane persists `next_run_at` (existing column, kept).
- Each tick, the leader selects due definitions and creates the **logical occurrence** in `automation_occurrences` (`UNIQUE(automation_task_id, occurrence_key)` is the duplicate-claim arbiter), then **claims** it atomically (`UPDATE … SET status='claimed' WHERE status='pending'`; rowcount 0 ⇒ lost race). The attempt-0 `automation_executions` row and its lease are inserted in the same transaction. Retry attempts add execution rows under the **same occurrence** (`occurrence_id`, `attempt`, `UNIQUE(occurrence_id, attempt)`) — logical identity and attempts are separate (normative model: tech spec §1.3a).
- Claim → a **lease** row (`execution_leases`: execution_id, owner, lease_expires_at, fencing_token) is written in the same transaction. The runner renews the lease every `lease_renewal_s` (30s) while it holds the execution; a runner whose lease expired **stops work at the next checkpoint** (and, in the subprocess design §6, is hard-killed).
- **Misfire policy** (per definition): `fire_once_late` (default; grace window 1h — dispatch late if within grace) or `skip`. Multiple missed slots (a downtime episode) collapse into **one** occurrence row per definition with `misfire_collapsed_count = N` — never an enumerated backlog (deterministic semantics: tech spec §6.1a). The audit's dead `skipped` status becomes the real carrier of misfire outcomes.
- **Guarantee statement (honest):** dispatch is *at-most-once per occurrence* (unique occurrence_key + leader) with *at-least-once recovery* (lease expiry → re-claim as attempt+1). **End-to-end exactly-once business side effects are NOT promised**; instead, side-effecting tools receive `execution_id`+`attempt` in their invocation context and are expected to key idempotency on it (documented contract for tool authors; enforced for automation-CRUD tools which are already forbidden in unattended runs).

### 4.3 Trigger types

- **Recurring (cron + IANA tz):** computed with the existing `schedule_parser.next_run_at` (zoneinfo + croniter; recomputed per fire — DST-correct today, retained). The silent `+1h` fallback (audit B12) is replaced by: parse failure → definition flagged `schedule_degraded=true` + `schedule_error` set + audit event + Center warning; the occurrence is **not** silently rescheduled.
- **One-time (`once`):** stored `run_at`; after claim, definition auto-`archived` with `completed_at` (audit B17's legitimate core, implemented correctly at the trigger layer).
- **Manual (run-now):** `POST /automations/{id}/run` goes **through the scheduling plane** (`trigger_now` becomes an occurrence insert with `occurrence_key = definition_id + ':manual:' + ULID`, unique per call) and **refuses when a non-terminal execution for the same definition exists** unless `?force=true` (fixes audit B10 while preserving operator override). Runs also require a fresh confirmation when the spec changed since the last confirmed version.
- **Event-based (phase 2, designed not built):** `event_triggers` table (event_type, filter JSONB); the existing ontology sensing/escalation fan-out (P3-2 pipeline) publishes to it. Out of scope for the first roadmap pass; schema reserved.

### 4.4 Workflow-scheduler consolidation

The `WorkflowScheduler` daemon keeps its **workflow semantics** (firing flow_mode AgentApps is a different product surface — verified subject: published AgentApps' `trigger_schedule` nodes, `workflow_scheduler.py:24-64`) but consolidates **scheduling mechanics**: workflow due-slots are claimed in a dedicated `workflow_occurrences` table (FK to the workflow subject — never a `subject_type` overload of automation occurrences, which would break FK integrity and blur retention; tech spec §1.3b), with claim/lease/fencing/recovery executed by the **shared `scheduler_core` service** parameterized per subject table. The in-memory `_in_flight` dedup is deleted (fixes audit B3 at the mechanism level). Its 30s thread loop runs under the same per-tick advisory-lock leadership. Full unification of workflow triggers into automation definitions is **rejected** (different resources — N5); consolidation is at the mechanism layer only. The dead `cronjob_tool` is deleted (audit B14).

---

## 5. Layer D / Agent Execution Architecture — the Agent Invocation Contract

### 5.1 Problem (verified, N1)

Today the executor fakes FastAPI DI into two router handlers, mutates contextvars on both sides, and regex-parses SSE text. The contract is stringly typed (`force_planning`, `phase`, `automation_task_id` body keys).

### 5.2 Design: `app/services/agent_invocation.py`

A service module defining the typed contract; **phase 1 implemented as an adapter over `add_message_stream`** (zero risk to the chat path), with internal mechanics progressively sourced from the existing `agent_loop/` package. The router remains the owner of chat orchestration; automation stops importing from `app.routers.agents` entirely.

```python
@dataclass class AgentInvocationContext:
    db: Session                  # runner-owned session
    user: User                   # principal (creator for unattended runs)
    org_id / app_id / project_id
    conversation_id              # per-task conversation (one-chat-per-task retained)
    automation: dict             # {definition_id, execution_id, attempt, spec_version, trigger}
    cancellation: CancellationSignal   # asyncio.Event + deadline; replaces _CANCEL_EVENTS registry
    budgets: ExecutionBudgets    # wall_clock, max_tool_calls, max_llm_calls
    trace_id: str

@dataclass class AgentInvocationRequest:
    message: str                 # composed objective + instructions + grounding rules
    force_planning: bool
    resume: bool = False         # approval-resume path (replaces direct resume_conversation call)
    approval_id: str | None

# Events: typed dataclasses, not SSE strings
AgentEvent = PlanStepCreated | PlanStepUpdated | ToolCallStarted | ToolCallFinished
           | ApprovalRequested | Delta | ArtifactCreated | RunFinished(status, content_hash, usage)
           | RunFailed(classification, transient: bool) | StreamEnded

async def invoke_agent(ctx, req) -> AsyncIterator[AgentEvent]
```

- **Provenance:** the invocation service stamps `agent_runs` via the existing `AgentRunService.finalize_run` choke point (migration 125's prov-v1 envelope) and returns `agent_run_id` in `RunFinished` — closing the provenance gap (audit B11/S5) at the seam where it is cheapest.
- **Adapters:** `invoke_agent` internally builds the v3 body, sets TraceContext, iterates the stream, translates SSE → typed events, and translates `CancellationSignal` into the existing cooperative checkpoint mechanism. `resume` mode wraps `resume_conversation` behind the same interface. The router never learns about automation; the contract is the only shared surface.
- **Chat parity:** `add_message_stream` keeps its exact external behavior; chat continues calling it directly. No duplicated runtime (Invariant 2.3 satisfied — this is an *entry-point extraction*, not a fork).
- **Consumer invariant (verified, enforceable):** automation adds **no** tool-routing tables or handlers, **no** planner, **no** datasource-selection logic, and **no** prompt-level governance. FSM planning remains available (passed through the contract as `force_planning`); dynamic datasource selection is preserved (spec constraints only *narrow* the authorized descriptor set — §3.1); heterogeneous sources, tool RBAC, and multi-agent delegation are the harness's existing mechanisms, untouched. Enforcement: the runtime agent's toolset must equal the parity list plus documented exclusions (static test, tech spec §10 item 27), and no `automation_*` module may import from `app.routers.agents` (§10 item 20). Automation is a *consumer* of the Agent Harness; the Control Plane owns only *when* and *under what lifecycle* the harness is invoked.

### 5.3 Execution flow (target happy path)

```
1 Load confirmed definition (spec_version frozen)          Control Plane
2 Verify current authorization (creator exists+active, org active, spec constraints valid)
3 Build execution context (occurrence, attempt, budgets, trace_id)
4 Expose authorized source descriptors (agent bindings + workspace auto-bind + project KBs,
   filtered by spec constraints) — agent selects; preflight connectivity probes retained
5 invoke_agent(ctx, req)  →  Agent Runtime plans dynamically (FSM/ReAct retained)
6 Execute tools / delegate (existing registry, RBAC, delegation caps)
7 Observe events → append automation_execution_events (plan/tool/approval/artifact)
8 Verify: truthfulness gate (retained) + grounding checks (retained) + optional output schema check
9 Persist outputs: execution record, artifacts (object storage), provenance ids, chat delivery
10 Finalize: CAS running→terminal under lease fencing token; release lease; audit event
```

---

## 6. Execution Runtime and Isolation

### 6.1 Worker boundaries — staged design

**Stage 1 (roadmap increments 1–6, current process model hardened):** retain asyncio dispatcher + thread-pool runner (the code that exists), but add:
- **Heartbeats:** runner updates `execution_leases.lease_expires_at` every 30s (new), independent from the 600s `timeout_at` wall clock (retained).
- **Fencing:** every terminal/status write carries `WHERE lease_token = :mine` (extends the existing CAS discipline, audit-verified as sound).
- **Startup recovery becomes non-destructive:** `reap_on_startup` is replaced by lease-expiry reclaim — a starting instance does **not** touch executions whose lease is unexpired and owned by another instance (fixes audit B4/S1 mechanically). A config escape hatch (`AUTOMATION_FORCE_REAP_ON_STARTUP=false` default) remains for single-instance dev.
- **Orphan containment:** on lease-expiry reclaim or timeout, the runner's `CancellationSignal` is set *before* marking terminal; late SSE events from an orphaned stream are dropped at the event-append layer (`execution already terminal` check that exists today at `automation_executor.py:1408-1415`, extended to all side-effect writes).

**Stage 2 (roadmap increment 8): subprocess runners for hard termination.**
- The runner executes the agent invocation in a **child process** (`multiprocessing` spawn, following the existing `backend/sandbox_worker/` pattern), parent holds the lease and a control pipe. Event durability follows **Option C (bounded-loss telemetry + durable critical events)** — critical events are synchronously parent-ACKed after DB commit, telemetry loss is bounded and counted; zero-loss claims are limited to what the mechanism proves (normative: tech spec §6.5). Critical events are uniquely identified per attempt — `UNIQUE(execution_id, attempt, seq)` (F-26): sequences restart at 0 for each attempt, so cross-attempt reuse cannot collide, and parent ACK frames carry `{execution_id, attempt, committed_upto}` so recovery always attributes events to the attempt that emitted them.
- Hard kill = `process.terminate()` at wall-clock deadline or lease loss. The child's DB session dies with it; because terminal writes are parent-owned (child streams events to parent, parent persists), an orphan child cannot corrupt state — this structurally fixes audit B6 ("timeout does not kill the work"). Python threads are never force-terminated (per the brief's constraint) — the boundary moves to the process level, which is the only safe termination primitive available.
- Side-effect protection during the kill window: delivery/chat writes are parent-side (post-stream), so a killed child cannot emit them.

### 6.2 Budgets and concurrency

Retained: semaphore 3/process (per-leader), 600s wall clock, 20 due/tick, recursion depth 3, 200KB output cap. Added: `max_llm_calls`/`max_tool_calls` from spec budgets (checked at the invocation contract layer), artifact byte cap enforced before persistence. Concurrency limits become cluster-aware: the semaphore is sized as `AUTOMATION_MAX_CONCURRENCY / expected_instances` with lease-based admission as the true limiter (a runner cannot exceed leases held).

### 6.3 Retry isolation

Retries remain new execution rows (`attempt+1`, `parent_execution_id`, backoff 60s→30m — retained, now **persisted-scheduled** in the occurrences/claims tables instead of in-memory delayed calls, fixing the process-death retry loss). Each retry gets a fresh lease, fresh cancellation signal, and its own conversation turn in the same per-task conversation (one-chat-per-task retained). The janitor-reap-vs-orphan overlap (audit S1) is eliminated by fencing: the orphan's terminal write loses the fencing check and its chat delivery is suppressed.

**Pre-retry side-effect barrier (F-27):** no new attempt opens before every row of the `automation_side_effect_ops` ledger (tech spec §1.5c) for the concluded attempt is resolved — `confirmed_complete` ops are preserved and never repeated, `confirmed_not_started` ops are safely retried, `unknown` ops retry only when the tool is idempotent (Gate B), and `unknown` non-idempotent ops **block automatic retry** until an audited human reconciliation unblocks a manual retry. A missing persisted output is never treated as proof that no side effect occurred; the ledger is the sole source of truth (normative: tech spec §6.6).

---

## 7. State Machines (summary — normative in tech spec §3)

**Definition:** `active | paused | archived` (+`is_deleted`); draft lives in its own table/status.
**Schedule:** derived (`next_run_at` + active), plus `schedule_degraded` flag for parse failures.
**Execution:** `claimed → running → finalizing → succeeded | failed | cancelled | timed_out | skipped` (+ `awaiting_approval` non-terminal state replacing "paused"), one row per **attempt** under a stable `automation_occurrences` identity (`pending → claimed → completed | failed_terminal | cancelled | skipped` at the occurrence layer). Terminal states immutable (existing CAS discipline retained and extended with fencing). Cancellations carry typed `cancel_reason` values (`user | admin | definition_deleted | org_deactivated | parent_cancelled | superseded` — tech spec §3.2). `timed_out` becomes a real backend status (fixing the frontend-only dead vocab, audit B14).
**Agent run:** existing `agent_runs` statuses (C-08), linked via `agent_run_id` on execution events.
**Approval:** existing `approval_requests` statuses (`pending|approved|rejected|expired`), `expire_old_requests()` scheduled.
**Artifact:** `rendering | ready | failed` on `automation_files` (artifact failure no longer fails the run — audit B7: text output ships with `artifacts_failed=true`).

Ownership of writes is exclusive: definitions ← Control Plane; executions ← Scheduling plane (creation) + Execution Runtime (transitions); events ← Execution Runtime only; approvals ← ApprovalService only.

---

## 8. Governance and Enterprise Security

| Control | Design |
|---|---|
| Org/project isolation | Unchanged mechanisms (org_id/app_id walls, `_assert_tenant` semantics), unified through the single Control Plane policy (§3.4) |
| Ownership | Per-action policy (§3.4); default owner-only for destructive actions |
| Datasource authorization | Spec constraints narrow the harness's authorized-descriptor set; harness (not prompt) enforces; no default/first-source assumptions (Invariant 2.4) |
| Tool permissions | Existing registry RBAC + spec allow/deny lists; admin + automation-CRUD tools stay forbidden in unattended runs (retained from `automation_runtime.py:74-78`) |
| Credentials | Org-shared KB credentials retained for now (documented limitation); secret handling inherits the platform-level P0 already tracked in the 2026-10-20 audit (out of scope here); executor never logs credentials (existing `to_dict()` stripping retained) |
| Unattended policy | Runs as creator identity (retained); authority = creator's harness permissions ∩ spec constraints; **no silent auto-approval** — `skip_confirmation` replaced by `pre_approved_scope`; all pauses either resolve within `approval_ttl` or fail the run with `failed(approval_required)` + audit event + Center notification |
| Human approvals | `ApprovalService` reused; unattended runs create approval requests visible in the existing governance queue; approvals expire via `expire_old_requests()`; approval audit events added |
| Audit | Central `record_audit_event(db, **kw)` writer (new, ~40 lines) + automation call sites at every mutation and every execution terminal (§3.4); reuses `AuditEvent` vocabularies |
| Provenance | `agent_run_id`, `conversation_id`, `parent_execution_id`, artifact ids, and approval ids persisted on execution/events — every output traceable to the run that produced it |

---

## 9. Delivery and Storage

- **Artifacts:** write to MinIO (already deployed, N6) via the existing storage abstraction used elsewhere in the platform; `automation_files` gains `storage_key` (object path) while `file_path` retained for local-fallback mode. `ARTIFACT_STORAGE_BACKEND=local|minio` config, default `minio` when the client is configured, `local` otherwise (no hard dependency). Renders are isolated from run success (§7).
- **Delivery channels** (all failure-isolated, per-channel rows in `automation_delivery_events` — tech spec §1.5b/§6.4b): chat bubble (`_persist_run_to_chat` retained as the mechanism, moved behind the Delivery module), email (existing gateway, unchanged API), Center/panel (event-driven). `_notify_chat` dead code deleted (audit B14).
- **Execution success ≠ delivery success:** the run terminalizes on verified output; a chat-write or email failure is recorded, retried, and surfaced in the UI — it never re-runs the agent. **Delivery guarantees are honest per channel:** chat/panel are effectively-once visible state (idempotent upserts); **email is at-least-once delivery-*attempt* semantics with possible duplicate messages** — the deterministic `Message-ID` identifies and traces the message and may enable downstream dedup where relays/clients honor it, which is never claimed as a guarantee; `delivery_unknown` + reconciliation covers ambiguous outcomes. Exactly-once external delivery is never claimed (normative: tech spec §1.5b, §6.4b).

---

## 10. What Is Retained / Consolidated / Replaced (explicit)

**Retained (proven, audit-verified):** v3 chat runtime + FSM planning + ReAct; `agent_loop/` mechanics; tool registry RBAC + TraceContext; `schedule_parser` (cron/NL/tz); truthfulness gate; grounding guardrails; DS preflight probes; one-chat-per-task sessions (`automation_sessions`); CAS terminal transitions; transient-error classification; ApprovalService + approval_requests; AuditEvent model; notification gateway; AutomationFile/execution/task models (evolved, not dropped); the Center's next-run computation.

**Consolidated:** two creation paths → one Control Plane lifecycle; three access policies → one policy function; `workflow_scheduler` dedup → shared claim/lease tables; three DDL mechanisms → Alembic-only; `_CANCEL_EVENTS` registry + regex SSE parsing → Agent Invocation Contract; in-memory retries → persisted occurrences; dead vocabulary (`skipped`, `timed_out`) → real semantics.

**Replaced/retired:** `skip_confirmation` silent auto-approve; `status="deleted"` writes; `_ensure_schema()` and `main.py` runtime ALTERs (become a drift *checker* only); entity-API write surface for AutomationTask; `_notify_chat`; `cronjob_tool`; destructive `reap_on_startup`.

---

## 11. Architecture Diagram (target end-to-end)

```
 Chat ──propose_automation──▶ automation_drafts ──review/edit──▶ confirm(confirmation_key, If-Match)
                                                                    │ (exactly-once)
 Automation Center ────────────┐                                    ▼
   views/actions ──────────────┤─────────────▶ CONTROL PLANE ◀── automation_definitions
                                │                (policy, audit, versioning)
                                │                     │ next_run_at
                                ▼                     ▼
                     SCHEDULING PLANE (leader via pg advisory lock)
                       tick → due defs → claim occurrence (UNIQUE occurrence_key)
                            → lease(fencing_token) → dispatch
                                │ ExecutionRequest
                                ▼
                     EXECUTION RUNTIME
                       runner(heartbeat) → agent_invocation.invoke_agent
                            │                                    │
                            │ typed events                       ▼
                            ▼                        PRODUCTION AGENT RUNTIME
                       execution_events ─────────▶ (v3/FSM/ReAct/agent_loop,
                       verification                    registry RBAC, TraceContext)
                            │                                    │
                            ▼                                    ▼
                  finalize (fenced CAS) ◀──────────── ApprovalService (pauses;
                            │                          no silent auto-approve)
                  delivery: chat bubble · email · Center · artifacts (MinIO)
                            │
                            ▼
                  audit_events (every mutation & terminal)
```

Every arrow is a named contract defined in the technical specification.
