# Zhanlu Automation — Technical Specification

**Date:** 2026-11-03
**Status:** Proposed — awaiting architectural approval
**Companion:** `2026-11-03-zhanlu-automation-target-architecture.md` (architecture rationale), `docs/plans/2026-11-03-zhanlu-automation-implementation-roadmap.md` (build order), `docs/reports/2026-11-03-zhanlu-automation-architecture-decision-record.md` (decisions).
**Ground truth:** all "current" statements verified against the working tree at commit `12030dd5`, 2026-11-03. Audit reference: `docs/reports/2026-11-03-zhanlu-automation-system-architecture-audit.md`.

---

## 1. Data Models and Schema

Principles: evolve existing tables (no drops until migration proven); Alembic-only DDL; every table carries `TimestampedBase` columns (`id` uuid pk, `created_date`, `updated_date`, `created_by_id`, `is_deleted`, `org_id` idx, `app_id` idx — `models/base.py:27-48`).

### 1.1 `automation_drafts` (NEW)

Server-side draft behind the chat review card (replaces client-only `pendingDraft`).

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | |
| `spec` | JSONB (PG) / JSON (SQLite) | Canonical AutomationSpec (§2) |
| `spec_version` | Integer NOT NULL default 1 | Incremented on every PATCH |
| `status` | String(20) NOT NULL default `draft` | `draft` \| `confirmed` \| `discarded` |
| `confirmation_key` | String(64) | Client UUID; copied to definition on confirm |
| `origin_session_id` | String(36) FK→chat_sessions.id, idx | Chat provenance |
| `origin_message_id` | String(36) | Message that produced the proposal |
| `validation_errors` | JSON | Last server validation result |
| `expires_at` | DateTime, idx | TTL (default 7d; cleanup task reuses scheduled-tasks pattern) |
| `confirmed_definition_id` | String(36) | Set on confirm (for idempotent re-confirm) |

Unique: none beyond PK (multiple drafts per conversation are legitimate). Index: `(org_id, status)`, `expires_at`.

### 1.2 `automation_definitions` (EVOLUTION of `automation_tasks`)

Existing table/columns retained (full current column list in audit §2.1). Changes:

| Change | Detail |
|---|---|
| ADD `spec` JSONB | Canonical spec; legacy scalar columns (`schedule`, `cron_expression`, `timezone`, `output_format`, `skills`, `notify_*`, `max_retries`, `skip_confirmation`) become **derived/compat** during migration, authoritative from `spec` after cutover flag |
| ADD `spec_version` Integer NOT NULL default 1 | |
| ADD `schedule_degraded` Boolean default false + `schedule_error` Text | Parse-failure surfacing (fixes audit B12 silent +1h) |
| ADD `trigger_type` String(20) default `schedule` | `schedule` \| `once` \| `manual` \| `event` |
| ADD `run_at` DateTime | For `once` triggers |
| ADD `confirmation_key` String(64) | From draft |
| ADD `current_execution_id` String(36) | Denormalized pointer to the non-terminal execution (run-now guard) |
| CHANGE semantics of `status` | Allowed values become `active` \| `paused` \| `archived` only; `deleted`/`failed`/`completed` are never written (fixes audit B1/B17). `"deleted"` rows from the legacy era are reconciled to `is_deleted=true, status='archived'` |
| ADD UNIQUE partial index | `(org_id, created_by_id, name)` WHERE `is_deleted = false` — soft dedupe guard (audit B2) |
| ADD UNIQUE partial index | `(org_id, confirmation_key)` WHERE `is_deleted = false` — the confirm idempotency arbiter (§4.2) |
| RETAIN | `next_run_at` (idx), `last_run_at`, `agent_id`, `project_id`, `session_id`, `data_source_id` (becomes pinned-source compat), `execution_history` (legacy, frozen read-only) |
| DROP (later, not in initial migration) | `skip_confirmation`, `max_retries`, `notify_chat` string-typed columns after spec cutover (audit B16) — only in a post-cutover migration |

### 1.3 `automation_executions` (EVOLUTION)

Retained columns per audit §2.1. Changes:

| Change | Detail |
|---|---|
| ADD `occurrence_id` String(36) FK→automation_occurrences.id, idx | Logical occurrence identity (§1.3a); the dedup `occurrence_key` lives on the occurrence row, not here |
| ADD `retry_of_execution_id` String(36) | Same-occurrence retry lineage (points at attempt n−1) |
| ADD UNIQUE `(occurrence_id, attempt)` | Exactly one execution row per attempt — retry idempotency |
| ADD `cancel_reason` String(40) | Typed reason when status=`cancelled` (§3.2) |
| ADD `status` value `claimed` | New pre-`running` state (claim → running) |
| ADD `status` value `timed_out` | Real backend status (frontend `timed_out` vocab becomes true, audit B14) |
| ADD `awaiting_approval` | Non-terminal state replacing "paused" semantics |
| ADD `skipped_reason` String(40) | For `skipped` terminal rows (`misfire`, `superseded`, `definition_deleted`) — gives `skipped` real semantics |
| ADD `lease_token` String(64) | Fencing token copied from lease at claim |
| ADD `spec_version` Integer | Frozen at claim (edit-during-run isolation) |
| ADD `agent_run_id` String(36), idx | Provenance (from AgentRunService) |
| ADD `artifacts_failed` Boolean default false | Render-failure isolation (audit B7) |
| CHANGE `parent_execution_id` | Gains real FK in migration (currently plain VARCHAR — audit §2.1); now used **exclusively** for automation-recursion lineage (nested `execute_automation`, depth cap 3 — the current code conflates this with retries at `automation_dispatcher.py:785`; the two meanings are split by this model) |

Indexes: existing + `(status, lease_expires_at)` via lease table, `(automation_task_id, started_at desc)`.

### 1.3a `automation_occurrences` (NEW)

The logical identity of one "time the automation should run". Executions are *attempts against an occurrence*; the occurrence survives retries and is the duplicate-claim arbiter.

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | Stable occurrence ID |
| `automation_task_id` | String(36) FK, idx | |
| `occurrence_key` | String(140) NOT NULL | `scheduled:{scheduled_time_utc}` or `manual:{ULID}` — dedup identity |
| `scheduled_at` | DateTime | Intended fire time (UTC) |
| `status` | String(20) NOT NULL default `pending` | `pending → claimed → completed \| failed_terminal \| cancelled \| skipped` |
| `attempt_count` | Integer NOT NULL default 0 | Denormalized from executions |
| `misfire_collapsed_count` | Integer default 0 | Missed slots collapsed into this row (§6.1a) |
| `claimed_at` / `completed_at` | DateTime | |
| `cancel_reason` | String(40) | Typed reason when status=`cancelled` (§3.2) — exists at occurrence level so a **pending** occurrence can be cancelled without any execution row |
| `cancelled_at` | DateTime | |
| `cancelled_by` | String(80) | `{user:{id}}` or `{admin:{id}}` or `system:{cause}` |

Constraints: **UNIQUE `(automation_task_id, occurrence_key)`** — the duplicate-claim arbiter (two leaders, crash-replay, or double Run-Now cannot create two occurrences). Index `(status, scheduled_at)`.

**Identity model (normative):** `occurrence_id` (stable across retries) ⊃ `execution_id` (one per attempt) ⊃ `attempt` (0-based, `UNIQUE(occurrence_id, attempt)`); `retry_of_execution_id` chains attempts within one occurrence; `parent_execution_id` chains *recursion* across occurrences. Retry rows therefore never violate the occurrence uniqueness, and "duplicate claim" is impossible by construction: the claim is `UPDATE automation_occurrences SET status='claimed' … WHERE id=? AND status='pending'` (rowcount 0 ⇒ someone else claimed).

### 1.3b `workflow_occurrences` (NEW, migration 130)

Workflow schedule triggers are a **separate resource** (verified subject: published `flow_mode` AgentApps' `trigger_schedule` nodes — `workflow_scheduler.py:24-64`), so they get their own occurrence table with identical claim/lease mechanics via the shared `scheduler_core` service — never a `subject_type` overload of automation occurrences (which would break the FK and blur retention).

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | |
| `agent_app_id` | String(36) FK→agent_apps.id, idx | Workflow subject |
| `node_id` | String(80) | `trigger_schedule` node id inside the flow |
| `workflow_run_id` | String(36) | Set by the workflow executor when a run starts — occurrence→run provenance (a flow may hold multiple `trigger_schedule` nodes; without this, runs could not be attributed to a specific occurrence). Backfills are unnecessary: the current scheduler is stateless, so only post-adoption runs carry it. |
| `occurrence_key` | String(140) NOT NULL | `scheduled:{fire_time_utc}` |
| `status` / `attempt_count` / `claimed_at` / `completed_at` / cancel metadata | | Same shapes and state machine as §1.3a |

Constraints: **UNIQUE `(agent_app_id, node_id, occurrence_key)`**. Claim/lease/fencing/recovery run through the same `scheduler_core` functions parameterized by table adapter — mechanics are shared, identity and retention are per-subject. Backfill: none required (the current scheduler is stateless; occurrences are synthesized on first adoption claim).

### 1.4 `automation_execution_events` (NEW)

Append-only observability ledger (replaces in-place `activity_steps` JSON mutation; old column retained read-only for one release).

| Column | Type |
|---|---|
| `id` | String(36) PK |
| `execution_id` | String(36) FK→automation_executions.id, idx |
| `attempt` | Integer NOT NULL | The attempt this event belongs to (F-26: sequences are per-attempt) |
| `seq` | Integer NOT NULL | Monotonic per `(execution_id, attempt)`; **UNIQUE(execution_id, attempt, seq)** — different attempts may reuse sequence numbers |
| `event_type` | String(40) | `phase_started, plan_step, tool_call_started, tool_call_finished, datasource_used, delegation, approval_requested, approval_resolved, artifact_created, artifact_failed, verification, error, retry_scheduled, delivery, side_effect_reconciliation, lifecycle` |
| `payload` | JSON | Typed per event_type (contracts in §6.4) |
| `agent_run_id` | String(36) | Provenance |
| `occurred_at` | DateTime NOT NULL |

Retention: 90 days hot (config), then purge by scheduled task. Index: `(execution_id, attempt, seq)`.

### 1.5 `execution_leases` (NEW)

| Column | Type |
|---|---|
| `execution_id` | String(36) PK, FK→automation_executions.id |
| `owner` | String(120) | `{hostname}:{pid}:{boot_id}` |
| `lease_expires_at` | DateTime NOT NULL, idx | Heartbeat-renewed every 30s |
| `fencing_token` | String(64) NOT NULL | Monotonic per execution (ULID/timestamp-based) |
| `acquired_at` | DateTime |

No separate `execution_leases` claim table needed for scheduling: the occurrence claim is the unique insert in §1.3; leases govern *running* executions only.

### 1.5b `automation_delivery_events` (NEW — F-13 durable delivery claims)

Durable per-channel delivery ledger with **claim-based, crash-safe** semantics. Execution terminality and delivery success are **independent lifecycles**: a run terminalizes on verified output; delivery outcomes never change execution status.

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | |
| `execution_id` | String(36) FK→automation_executions.id, idx | |
| `channel` | String(20) NOT NULL | `chat` \| `email` \| `panel` |
| `idempotency_key` | String(140) NOT NULL | `{execution_id}:{channel}` — provider-facing where supported (email: deterministic `Message-ID`) |
| `status` | String(20) NOT NULL default `pending` | `pending → claimed → delivered \| failed_permanent \| unknown` |
| `claim_token` | String(64) | ULID minted at claim; compared on state writes (delivery-side fencing) |
| `claim_owner` | String(120) | Runner/recoverer identity |
| `claim_expires_at` | DateTime, idx | Claim lease (default 5 min); expired claims are re-claimable |
| `attempts` | Integer NOT NULL default 0 | Max 3 (initial + 2 retries), then `failed_permanent` |
| `last_error` | Text | |
| `delivered_at` | DateTime | |

Constraints: **UNIQUE `(execution_id, channel)`** (one row per channel — the claim arbiter); index `(status, claim_expires_at)` for the recovery scan.

**Per-channel guarantees (honest):**

| Channel | Guarantee | Mechanism |
|---|---|---|
| `chat` | Effectively-once **visible state** (in-place bubble update keyed by execution id) | Upsert; a repeated write is idempotent |
| `panel` | Stateless read; no duplicate concept | Read model only |
| `email` | **At-least-once delivery *attempt* semantics; duplicate messages are possible** — exactly-once is NOT claimed and receiver-side deduplication is NOT a guaranteed property (verified: `SmtpProvider.send()` has no idempotency parameter and `_build_message` sets no `Message-ID` — `provider.py:57-129`; relays/clients may or may not honor one). The deterministic `Message-ID = f(idempotency_key)` (added by R9) provides **identification/tracing** and enables *optional* downstream dedup where the receiving infrastructure honors it | `Message-ID` header set via the provider's new optional `message_id` param; 30s socket timeout (`provider.py` `_send_smtp`) — a `TimeoutError` is classified *transport* (ambiguous: the server may have accepted) → `unknown`; `SMTPAuthenticationError`/`RecipientsRefused` are permanent → `failed_permanent` |

**State transitions:** `pending` →(claim: win the unique row, mint `claim_token`, set `claim_expires_at`)→ `claimed` →(provider ack)→ `delivered`; →(provider permanent rejection)→ `failed_permanent`; →(crash between send and persist, or ambiguous timeout)→ `unknown`. `unknown` rows are **reconciled, not blindly retried**: the recovery scan attempts a provider-side check where available (optional Message-ID/mailbox lookup); otherwise an `unknown` row older than the reconciliation window becomes `failed_permanent` with `last_error="ambiguous_outcome_unresolved"` + a `delivery` execution event recording the ambiguity. Retrying an `unknown` row requires explicit opt-in (`delivery_retry_unknown=false` default) because it can duplicate.

### 1.5c `automation_side_effect_ops` (NEW — F-27 side-effect ledger; migration 128)

Intent-and-outcome ledger for every external side-effecting tool operation. It is the **sole** source of truth for side-effect state: missing `output_text` says nothing about side effects — only this ledger can prove (or disprove) that an external operation occurred.

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | |
| `execution_id` | String(36) FK→automation_executions.id, idx | |
| `attempt` | Integer NOT NULL | Attempt that (was to) perform the op |
| `op_key` | String(160) NOT NULL | `{attempt}:{tool_call_seq}:{tool_name}` — operation identity within the attempt |
| `tool_name` | String(120) | |
| `idempotency_key` | String(160) | Gate B key (`{execution_id}:{attempt}:{tool_call_seq}`) — supplied to the provider where supported |
| `status` | String(24) NOT NULL default `dispatched` | `dispatched → started → confirmed_complete \| failed`; reconciliation may set `confirmed_not_started` or `unknown` |
| `reconciled_at` | DateTime | |
| `reconciled_via` | String(40) | `provider_check` \| `ledger_replay` \| `operator` \| `timeout_ageout` |
| `last_error` | Text | |

Constraints: **UNIQUE `(execution_id, attempt, op_key)`**; index `(status, updated_date)` for the barrier scan. Write points (parent-owned, like all automation persistence): `dispatched` before tool dispatch; `started` when the tool reports the external call in flight; outcome on completion/failure. Lifecycle: rows are immutable except status/reconciliation fields.

### 1.6 Reused tables (no schema change)

- `approval_requests` (`models/governance.py:46-53`) — unchanged; automation adds `event_source`-tagged usage and audit events.
- `audit_events` (`models/audit_event.py`) — unchanged; new event types appended to `AUDIT_EVENT_TYPES` vocabulary (`automation_*`).
- `automation_files` — ADD `storage_key` String(500), `status` String(20) default `ready` (`rendering|ready|failed`), `render_error` Text.
- `chat_sessions`, `agent_conversations`, `agent_runs`, `workflow_runs` — unchanged; linked by id from executions/events.

### 1.7 Schema evolution strategy

- All changes via Alembic migrations numbered ≥ 126, in one linear chain (each `down_revision` chains to the previous; numbering is unique and applied in order):
  - **126** — `automation_drafts` + `spec`/`spec_version`/trigger/confirmation columns on definitions (additive).
  - **127** — `automation_occurrences` + execution changes (`occurrence_id` FK, `retry_of_execution_id`, `UNIQUE(occurrence_id, attempt)`, `cancel_reason`, new status values) + data backfill: any legacy in-flight execution rows get a synthesized occurrence; historic duplicate in-flight rows (none expected — the fire CAS blocked them) are collapsed idempotently.
  - **128** — `automation_execution_events`, `execution_leases`, `automation_delivery_events`, `automation_side_effect_ops` (additive; F-27 ledger rides with 128).
  - **129** — definition status reconciliation (`deleted`→`archived`+`is_deleted`, legacy `failed`/`completed` remapped) + partial unique name index + **cutover**: spec becomes authoritative, scalar columns derived.
  - **130** — **`workflow_occurrences`** (§1.3b) + workflow-scheduler claim adoption via `scheduler_core`.
  - **131** — artifact storage metadata (`automation_files.storage_key`, `storage_backend`, `status`, `render_error`).
  - **Reserved ≥132** — legacy column retirement (`skip_confirmation`, `max_retries`, `notify_chat` string columns) only after the spec cutover soak; takes the next free number at implementation time. Never pre-assigned.

**Ownership (one owner per migration, verified against the actual chain: head is `125_agent_run_provenance`, no 126–131 exist on this branch):** 126→roadmap R2; 127→R5; 128→R5; 129→R8; 130→R5; 131→R9. Cancellation metadata rides with 127 (executions + occurrences); delivery-event schema and the F-27 side-effect ledger with 128; status/enum changes with 127+129; backfills live inside 127 and 129 (idempotent).
- **Retirement of runtime DDL:** `automation_dispatcher._ensure_schema()` (`automation_dispatcher.py:137-297`) is reduced to a *drift checker* (logs divergence, alters nothing); the automation entries in `main.py:289-314` `specific_migrations` are deleted once migration 126–129 ship. Rule: no parallel runtime DDL mechanisms (audit B15).
- SQLite dev/test parity: JSONB→JSON, partial unique index → enforced in service layer with a SQLite-compatible filtered index where supported; test suite already runs on real SQLite (`conftest.py` T1 contract).

---

## 2. Canonical AutomationSpec (normative schema)

```jsonc
{
  "name": "Daily ERP Sales Sync Report",            // required, ≤255
  "description": "...",                              // optional
  "project_id": "prj-...",                           // required; resolved+validated server-side
  "objective": "Synchronize ERP sales data into the business DB and detect anomalies",
  "execution_instructions": "Prefer incremental sync; validate row counts; ...",  // optional prompt addendum
  "agent": { "agent_id": "agt-..." | null },         // null → hidden automation_runtime_agent
  "trigger": {
    "type": "schedule",                              // schedule | once | manual
    "cron": "0 8 * * *",                             // schedule only
    "timezone": "Asia/Shanghai",                     // IANA; required for schedule/once
    "misfire_policy": "fire_once_late",              // fire_once_late | skip
    "grace_window_seconds": 3600,
    "run_at": null                                   // once only (ISO 8601)
  },
  "datasource_constraints": {
    "allowed_source_ids": [],                        // empty = all authorized (agent selects)
    "allow_workspace_auto_bind": true
  },
  "tool_constraints": {
    "allowed_tool_ids": [],                          // empty = all authorized
    "denied_tool_ids": []
  },
  "output": { "format": "html", "artifact_byte_cap": 10485760 },
  "delivery": { "chat": true, "email": false },
  "notifications": { "notify_on": "on_failure", "emails": [] },
  "approvals": {
    "policy": "block",                               // block = pauses fail the run after ttl
    "pre_approved_scope": ["db.read"],               // tool classes allowed to proceed unattended
    "approval_ttl_seconds": 900
  },
  "retry": { "max_attempts": 2, "backoff_base_seconds": 60, "backoff_cap_seconds": 1800 },
  "budgets": { "wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40 }
}
```

Validation rules (server, on PATCH and confirm): name/project/type checks; cron parseable via `schedule_parser.parse_schedule` **and** `next_run_at` resolvable (no silent fallback); timezone is a valid IANA name; budgets within org ceilings; `pre_approved_scope` ⊆ org-allowed unattended classes; `agent_id` exists+active in org or null. Validation failures return field-level errors; the draft stays editable. "Validated specification" is a computed state returned by the API, not a stored status.

---

## 3. State Machines

### 3.1 Definition (automation_definitions.status)

```
            confirm(draft)                pause                archive
 DRAFT ────────────────▶ ACTIVE ──────────────▶ PAUSED ───────────────▶ ARCHIVED
                            ▲    └────resume────────┘   └──delete(soft)──┘│
                            └────────────edit (spec_version+1)───────────┘ (applies next occurrence)
 once-trigger: ACTIVE ──occurrence claimed──▶ ARCHIVED (auto, completed_at set)
```
Legal writers: Control Plane only. Terminal: `archived` (immutable; `is_deleted` is the soft flag). Unknown/legacy values are reconciled by migration 129; the dispatcher's NULL/invalid self-heal is deleted (its job is done by the migration + CHECK-consistent writes).

### 3.2 Occurrence and Execution state machines

**Occurrence (`automation_occurrences.status`)** — logical schedule identity:

```
        claim (atomic UPDATE … WHERE status='pending')        last attempt reaches terminal
 PENDING ────────────────────────────────▶ CLAIMED ─────────────────────────────▶ COMPLETED
   │                                        │                                    (an attempt succeeded)
   │ misfire beyond grace / collapse        ├─ last attempt failed permanently ─▶ FAILED_TERMINAL
   ▼                                        ├─ definition deleted / org deactivated ─▶ CANCELLED
 SKIPPED                                    └─ schedule invalidated before dispatch ─▶ CANCELLED(superseded)
```
Occurrence terminals `{completed, failed_terminal, cancelled, skipped}` are immutable. A retry never creates a new occurrence.

**Execution (`automation_executions.status`)** — one attempt against an occurrence:

```
 start        verify/finalize (fenced CAS)
 CLAIMED ──▶ RUNNING ──▶ FINALIZING ──▶ SUCCEEDED
    │           │  │              └────────▶ FAILED (transient → NEW attempt row; permanent → terminal)
    │           │  └─approval pause──▶ AWAITING_APPROVAL ──resolve──▶ RUNNING
    │           │                                     └─expire/ttl──▶ FAILED(approval_required)
    │           └─wall-clock/lease-loss──▶ TIMED_OUT
 Any non-terminal ──cancel(fenced CAS + cancel_reason)──▶ CANCELLED
 FAILED(transient) ──retry policy──▶ NEW row: same occurrence_id, attempt+1, retry_of_execution_id
```
Execution terminal set: `{succeeded, failed, cancelled, timed_out, skipped}` (execution-level `skipped` retained only for legacy rows; new skips are occurrence-level). Terminal writes are immutable and fenced (`WHERE status NOT IN terminal AND lease_token = :token`). `claimed→running` is the existing CAS (`automation_executor.py:1082-1101` pattern) plus lease insert in the same transaction.

**Typed cancellation/skip reasons (normative):**

| Status | Reason value | Trigger |
|---|---|---|
| `cancelled` | `user` | Authorized user cancels the execution (policy §3.4) |
| `cancelled` | `admin` | Org admin cancels (bypasses ownership, audited) |
| `cancelled` | `definition_deleted` | Task deleted → non-terminal executions cancelled first |
| `cancelled` | `org_deactivated` | Org deactivated → no new claims; in-flight runs cancelled at next checkpoint |
| `cancelled` | `parent_cancelled` | Parent automation execution cancelled → nested (recursion) children cancelled via the invocation contract's cancellation signal |
| `cancelled` | `superseded` | Queued occurrence invalidated by a schedule edit before dispatch |
| `skipped` | `misfire` | Occurrence beyond grace per policy (occurrence-level) |
| `skipped` | `definition_deleted` / `org_deactivated` | Pending occurrence discarded for the same causes |

Cancellation behavior: cancelling a **child** execution directly does **not** cancel the parent (the parent observes a delegation failure event); cancelling a **parent** cancels its recursion children (`parent_cancelled`) but not sibling occurrences. All cancellations are fenced CAS writes + audit events.

### 3.3 Approval — reused `approval_requests.status` (`pending|approved|rejected|expired`), `expires_at` enforced by `expire_old_requests()` (already implemented, `approval_service.py:124`). New: automation `AWAITING_APPROVAL` transitions to `FAILED(approval_required)` when the request expires or `approvals.approval_ttl_seconds` elapses.

### 3.4 Artifact — `rendering → ready | failed` on `automation_files.status`; `failed` never affects execution status; execution carries `artifacts_failed=true` + delivery event.

### 3.5 Agent run — existing `agent_runs` statuses (C-08 runtime); linked one-to-many execution→agent turns via `automation_execution_events.agent_run_id` and `automation_executions.agent_run_id` (final turn).

---

## 4. API Contracts (Control Plane, prefix `/api/automations`)

All authenticated via `get_current_user_required` (removing today's unauthenticated `/parse-schedule`, audit B18). Errors: standard `{detail}`; 409 for version/idempotency conflicts; field errors `{detail: {fields: {name: "..."}}}`.

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /drafts` | Create draft (from `propose_automation` tool or Center "New") | Body: `{spec_partial, origin_session_id?, origin_message_id?}` → `{draft_id, version, validation}` |
| `GET /drafts/{id}` | Read draft + validation result | Owner/org policy |
| `PATCH /drafts/{id}` | Edit spec | `If-Match: version`; re-validates; increments version |
| `POST /drafts/{id}/confirm` | **Exactly-once** confirm | Headers: `If-Match: version`; Body: `{confirmation_key}`. 200 `{definition, already_confirmed}` on replay; 409 on stale version |
| `POST /drafts/{id}/discard` | Discard draft | |
| `GET /definitions` | List | Query: `project_id, status, search, trigger_type, sort, limit, cursor` (server-side filters — fixes audit B9); includes `next_run_at`, `last_execution_summary` |
| `GET /definitions/{id}` | Read (spec + derived schedule info) | |
| `PATCH /definitions/{id}` | Edit (creates spec v+1) | `If-Match: spec_version`; schedule re-parse with loud failure (`schedule_degraded`) |
| `POST /definitions/{id}/pause` \| `/resume` | Toggle | Resume re-validates schedule (loud 400 on unparsable — retained behavior) |
| `POST /definitions/{id}/run` | Run now | 409 `{detail: "execution_in_progress", current_execution_id}` if non-terminal exists unless `?force=true`; brokered through scheduling plane |
| `POST /definitions/{id}/cancel` | Cancel current execution | Fenced CAS + cooperative signal (retained two-phase) |
| `DELETE /definitions/{id}` | Soft delete → `archived`+`is_deleted` | Cancels non-terminal executions first (fixes audit B8); audit event |
| `GET /definitions/{id}/executions` | History (paged) | Replaces `execution_history` JSON reads |
| `GET /executions/{id}` | Execution detail | Includes provenance ids |
| `GET /executions/{id}/events?after_seq=` | Event stream (poll/SSE) | Append-only ledger; replaces `activity_steps` reads |
| `POST /executions/{id}/cancel` | Cancel execution | Retained semantics |
| `GET /executions/{id}/status` | Lightweight snapshot | Retained (2s panel polling) |
| `GET /files/{file_id}/download` \| `/preview` | Artifacts | Retained auth (signed token / session) |
| `POST /parse-schedule` | Utility | **Now authenticated** (audit B18) |
| `GET /definitions/{id}/audit` | Audit trail for a definition | From audit_events |

Retired endpoints (kept one release with 410/deprecation headers, then removed): entity-API write routes for `AutomationTask` (`POST/PUT/DELETE /api/apps/{app}/entities/AutomationTask`); `POST /{id}/ensure-session` logic moves inside `/run` (single call).

### 4.2 Confirmation transaction (normative)

Concurrency-safe exactly-once confirm — database-enforced, **replay-first** (an already-confirmed draft is resolved as a replay *before* any validation that could reject it):

```
BEGIN (single transaction on the Control Plane session)
1. SELECT * FROM automation_drafts WHERE id = :draft_id FOR UPDATE     -- serializes ALL confirm paths
2. REPLAY CHECK (before any rejecting validation):
   IF draft.status = 'confirmed':
     a. request.confirmation_key == draft.confirmation_key
        → COMMIT; 200 {definition_id: draft.confirmed_definition_id, already_confirmed: true}
     b. request.confirmation_key != draft.confirmation_key
        → COMMIT; 409 {detail: "already_confirmed_different_key", definition_id}
3. KEY OWNERSHIP:
   draft.confirmation_key IS NULL  → SET draft.confirmation_key = :key   -- key bound to draft at first attempt
   draft.confirmation_key != :key  → 409 {detail: "confirmation_key_mismatch"}
4. VERSION VALIDATION: draft.spec_version == :if_match → else 409 {detail: "stale_version", current}
5. INSERT INTO automation_definitions (…, confirmation_key, …)
   VALUES (…) ON CONFLICT (org_id, confirmation_key) WHERE is_deleted = false
   DO NOTHING RETURNING id                                               -- DB-enforced arbiter
6. If step 5 returned no row (concurrent winner committed first):
   SELECT id, is_deleted FROM automation_definitions
   WHERE org_id = :org AND confirmation_key = :key        -- replay ignores is_deleted (immutable history)
   → COMMIT; 200 {definition: existing, already_confirmed: true, definition_archived: is_deleted}
7. UPDATE automation_drafts SET status='confirmed', confirmed_definition_id=:id
8. write audit event automation_confirmed; COMMIT → 201 {definition}
```

**Behavior contract:**

| # | Request | Result |
|---|---|---|
| 1 | Same draft + same key + already confirmed | 200, original definition ID (step 2a) |
| 2 | Same draft + different key + already confirmed | 409 `already_confirmed_different_key` (step 2b) |
| 3 | Draft + stale `If-Match` | 409 `stale_version` with current version (step 4) |
| 4 | Draft + valid version + valid key | 201, exactly one definition (step 5) |
| 5 | Concurrent duplicate requests | One definition — the row lock serializes; the loser observes `confirmed` post-commit and takes the step-2 replay path |
| 6 | Crash before COMMIT | Draft unchanged, no definition — safe retry |
| 7 | Crash after COMMIT, before HTTP response | Stable replay via step 2a (draft row + definition committed together) |

- **Key ownership & scope:** the `confirmation_key` is a client-generated UUID, bound to the draft row at the first confirm attempt (step 3) and scoped by the partial unique index `UNIQUE (org_id, confirmation_key) WHERE is_deleted = false` on `automation_definitions` — the database, not application logic, decides idempotency. The step-5/6 pattern is race-free under READ COMMITTED: the index loser reads the winner's row inside its own transaction.
- **Row-lock behavior:** every path begins with `SELECT … FOR UPDATE` on the draft; concurrent confirms block on that lock and re-read the post-commit row, so no path needs an application-level mutex.
- **Tests:** real-DB integration tests for behaviors 1–7, including PostgreSQL concurrent-confirmation tests (two sessions, same key → one definition, both get 2xx with identical ID; two keys → one 201 + one 409). SQLite covers behaviors 1–4, 6–7 single-threaded; behaviors 2/5's concurrent semantics are Postgres-profile only (documented, not faked).
- **Edge cases (independent review round 2):**
  - **Two different drafts, same key:** the step-5/6 index loser cannot claim the key; its draft stays `draft`. The loser path therefore marks its own draft `discarded` with `validation_errors.confirmation_key = "already_used_by:<definition_id>"` and responds 409 `confirmation_key_already_used` — the second draft never silently sits in a half-bound state.
  - **Winner later soft-deleted:** the step-6 replay lookup deliberately does **not** filter `is_deleted` (idempotency keys are immutable history); a replay of a key whose definition was archived returns 200 with `definition_archived: true`. The partial unique index freeing on soft-delete cannot resurrect creation because the replay path never inserts.

### 4.1 Chat-side tool contract

`propose_automation(spec_partial, origin_message_id)` — registered in the tool registry, callable only by agents bound to `automation_agent` (or when the conversation is automation-rebound). **Writes only drafts.** `update_automation(definition_id, spec_patch)` → applies PATCH to the definition via Control Plane rules (owner check enforced by harness identity, not prompt). The old `create_automation` tool is removed from the registry after the cutover flag (`AUTOMATION_CREATION_V2=true`) — the LLM can no longer bypass confirmation (brief §B invariant).

---

## 5. Agent Invocation Contract (normative)

Module: `backend/app/services/agent_invocation.py`. Types (pydantic):

```python
class AgentInvocationContext(BaseModel):
    db: Session; user: User
    org_id: str; app_id: str; project_id: str | None
    conversation_id: str
    automation: AutomationRef          # definition_id, execution_id, attempt, spec_version, trigger
    cancellation: CancellationSignal   # asyncio.Event + wall-clock deadline, settable from runner
    budgets: ExecutionBudgets
    trace_id: str

class AgentInvocationRequest(BaseModel):
    message: str
    force_planning: bool = True
    resume: bool = False               # True → wraps resume path; approval_id optional
    approval_id: str | None = None

class AgentEvent(BaseModel):
    type: Literal["phase","plan_step","tool_call_started","tool_call_finished",
                  "approval_requested","delta","artifact_created","run_finished","run_failed","stream_ended"]
    seq: int; occurred_at: datetime
    payload: dict                      # typed per §6.4; run_finished carries
                                       # {status, content_hash, usage, agent_run_id, conversation_id}
```

`async def invoke_agent(ctx, req) -> AsyncIterator[AgentEvent]`

**Phase 1 adapter implementation (no chat-path changes):** builds the v3 body (`content`, `force_planning`, `phase="automation"`, ids), sets `TraceContext`, calls `add_message_stream` (the executor's current mechanics, `automation_executor.py:2281-2302`), parses SSE frames once (regex parser centralized here, deleted from the executor), translates to `AgentEvent`, honors `ctx.cancellation` at chunk boundaries (existing cooperative checkpoints, `:2303-2312`), and wraps `resume_conversation` for `resume=True`. **Phase 2:** internals progressively re-sourced from `agent_loop/`; the router delegates its own v3 path to the same core, making the contract the single entry point for both chat and automation (eliminating the coupling by construction, not by duplication).

Provenance: the contract's final event always carries `agent_run_id` (stamped via `AgentRunService.finalize_run`, migration-125 envelope) and `conversation_id`; the runner persists both — closing audit S5 (conversation id recorded on failure paths too, because the contract returns them regardless of terminal status).

**Cancellation and timeout inside the contract (normative):** `ctx.cancellation` is checked at every event yield and before every tool dispatch; the wall-clock deadline is enforced at the contract layer (deadline exceeded ⇒ `RunFailed(classification="timeout")` after a final `stream_ended`), not left to the runner. `resume=True` shares the same cancellation signal and deadline budget. Delegation inherits the parent's remaining deadline; a cancelled child surfaces to the parent as a delegation failure event, never as a silent hang.

---

## 6. Scheduling and Execution Protocols

### 6.1 Dispatcher tick (leader)

```
every 60s (config TICK_INTERVAL):
  open DEDICATED connection; if not pg_try_advisory_lock(380631): close conn; skip tick
    # Leadership is PER-TICK: the lock is session-scoped and released by closing the tick
    # connection at tick end — never held across ticks, so pooled-connection recycle cannot
    # silently drop it (fixes S2). Non-Postgres (SQLite dev): leadership check is skipped
    # with a startup warning; single-process is assumed.
  recover_leases()          # expired lease → if execution.output_text IS NOT NULL:
                            #     FINALIZE-REPLAY the same execution (§6.2b — finalization is idempotent)
                            #   else: reclaim the SAME occurrence as attempt+1, or
                            #     occurrence → failed_terminal/skipped if retry budget exhausted
  process_retries()         # due retries: INSERT execution (occurrence_id, attempt = max+1)
                            #   [UNIQUE(occurrence_id, attempt) guards double-retry]
  claim_due()               # SELECT defs active ∧ next_run_at ≤ now ∧ org active (limit 20)
                            #   → INSERT automation_occurrences(occurrence_key)
                            #     [UNIQUE(automation_task_id, occurrence_key) = dup-claim arbiter]
                            #   → CAS advance next_run_at (same tx; protects schedule-edit races)
                            #   → misfire evaluation (§6.1a): beyond grace → occurrence skipped
                            #     (collapsed); within grace → fire late
                            #   → UPDATE occurrence SET status='claimed' WHERE status='pending'
                            #     (rowcount 0 ⇒ lost race → skip)
                            #   → INSERT execution(attempt=0) + lease(fencing_token)   [same tx]
                            #   → dispatch runner (in-proc stage 1 / subprocess stage 2)
  janitor()                 # every 5 ticks: legacy-row fallback reaper (rows without leases),
                            #   delete-cancels, approval-ttl enforcement,
                            #   delivery recovery scan (§6.4b), draft-TTL cleanup
  release advisory lock (close the tick connection)
```

The lost-fire window (audit B5) is closed because the occurrence insert, the `next_run_at` CAS, the claim, and the execution/lease insert are **one transaction**: a crash either leaves the occurrence `pending` (retried next tick) or fully claimed with its attempt-0 execution row.

Multi-instance: N processes, one leader per tick; non-leaders still run their in-flight runners (leases protect). Leader crash: lock expires on session end; another instance acquires next tick; in-flight runs continue under valid leases.

### 6.1a Misfire-collapse algorithm (deterministic, bounded)

**Product decision (chosen here, recorded in ADR-013 — not a pre-established behavior):** grace is evaluated against the **latest missed slot**, and the representative occurrence is the **latest missed slot**. Rationale: "fire late" should mean "the schedule just came due while we were down", not "fire something that was due days ago"; evaluating against the earliest slot would make long outages always skip even when the schedule freshly came due, and evaluating against `now` (previous draft) was ambiguous.

Deterministic algorithm, executed at the first tick where the definition is observed due (`next_run_at ≤ now`):

```
1. Missed interval I = [next_run_at, now)               # next_run_at is the persisted, per-definition marker
2. Slots S = croniter slots of the task's cron/tz inside I (earliest→latest)
   # computed with zoneinfo localization; ambiguous/nonexistent local times (DST) use fold=0 defaults
3. Representative slot R = max(S)                        # the LATEST missed slot
4. Staleness = now − R
5. If staleness ≤ grace_window_seconds and policy = fire_once_late:
     create occurrence(occurrence_key="scheduled:{R}", scheduled_at=R,
                       misfire_collapsed_count=|S|)  → fire once (late)
   else:
     create occurrence(occurrence_key="scheduled:{R}", status='skipped',
                       skipped_reason='misfire', misfire_collapsed_count=|S|)
6. Advance next_run_at = croniter.next_after(now) via CAS (same transaction as step 5)
7. Repeated ticks cannot duplicate: next_run_at has already advanced past `now`,
   so the definition is no longer due; the UNIQUE(automation_task_id, occurrence_key)
   index additionally arbitrates any racing insert.
```

Worked examples (cron `0 8 * * *`, tz `Asia/Shanghai`, grace 3600s, policy `fire_once_late`):

| Scenario | Timeline | Result |
|---|---|---|
| Single missed slot, within grace | Down 08:00–08:40, recovery tick 08:40 | S={Nov-04 08:00}, R=08:00, staleness 40min ≤ 1h → **fires late**, collapsed_count=1 |
| Single missed slot, beyond grace | Down 08:00–09:30 | R=08:00, staleness 90min > 1h → **skipped(misfire)**, collapsed_count=1 |
| Multi-slot episode | 5-min cron down 02:00–02:50 | S={02:00…02:45}, R=02:45, staleness 5min → **fires once**, collapsed_count=10 |
| Downtime longer than grace, fresh due | Daily cron down Nov-01 08:00 → Nov-04 08:30 | S={Nov-01…Nov-04 08:00}, R=Nov-04 08:00, staleness 30min → **fires once** for the whole episode, collapsed_count=4 |
| Downtime longer than grace, stale due | Daily cron down Nov-01 08:00 → Nov-04 10:00 | R=Nov-04 08:00, staleness 2h → **skipped(misfire)**, collapsed_count=4 — never 4 executions, never 4 rows |
| DST transition | `30 2 * * *` (02:30 local) on a 02:00→03:00 spring-forward day | The 02:30 local time **does not exist**; the slot is **excluded from S** (pinned rule: nonexistent local fire times are skipped, ambiguous times resolve with `fold=0`); R is the latest *existing* slot and `misfire_collapsed_count` counts only existing slots. The exact croniter/zoneinfo behavior is pinned by a fixed-clock unit test (§10.33) before implementation |

Edge cases (all deterministic):

| Case | Behavior |
|---|---|
| Schedule edited during downtime | PATCH recomputes `next_run_at` from the new cron at edit time; the un-evaluated pre-edit missed interval is **discarded** (no occurrence row) and recorded via an audit event `misfire_discarded_on_edit` |
| Paused definition | Not selected while paused; on resume, `next_run_at` is recomputed from `now` (existing behavior) — missed slots during pause produce no rows; audit event `misfire_discarded_on_pause` |
| Degraded/invalid schedule (`schedule_degraded=true`) | Never selected; no occurrences are created; the Center warning and `schedule_error` persist until fixed (fix → recompute) |
| One-time trigger (`run_at`) past grace | Occurrence → `skipped(misfire)`; definition auto-archives with last outcome `skipped`; never fires late, never re-arms |
| Restart after prolonged downtime | Identical path — the first post-recovery tick performs steps 1–7; startup recovery does no backfill |

Config: `misfire_policy ∈ {fire_once_late (default), skip}`; `grace_window_seconds` (default 3600). Every decision writes an audit event + a lifecycle occurrence event, so "what happened during the outage" is answerable from persisted state. The backlog is bounded at **one occurrence row per definition per episode, by construction**.

### 6.2 Runner and finalization protocol (F-19)

```
acquire runner slot (cluster-aware semaphore)
claim execution: fenced CAS claimed→running (lease insert in same tx; §6.1)
loop: renew lease every 30s (heartbeats continue through finalization)
  invoke_agent(ctx, req) → append events (seq++) → verify (truthfulness/grounding retained)
  PERSIST output_text + output_data + provenance           # BEFORE the finalizing CAS
  render artifacts (isolated try; failures → artifacts_failed=true + artifact event — never run failure)
  fenced CAS running→finalizing (token check)
  finalize (idempotent, re-runnable — §6.2b)
  fenced CAS finalizing→succeeded|failed|timed_out (token check)
  → schedule_retry if failed-transient (NEW attempt row, same occurrence)
  release lease; write audit terminal event
delivery is NOT in the runner loop: it is claimed post-terminal per §6.4b
```

### 6.2b Finalization protocol (normative)

**Ownership:** only the runner (the parent process in subprocess mode) writes `claimed→running`, `running→finalizing`, and `finalizing→terminal`. Heartbeats continue during `finalizing`.

Steps inside `finalizing` (each idempotent, re-runnable):

1. Re-read the persisted `output_text`/`output_data` (written before the `running→finalizing` CAS) — **finalization never re-invokes the agent**.
2. Finalize artifact records (`rendering→ready|failed`); write provenance ids (`agent_run_id`, `conversation_id`, artifact ids) onto the execution row.
3. Task-level bookkeeping (`last_run_at`, `execution_history` entry).
4. Terminal CAS `finalizing→succeeded|failed` fenced by `lease_token`.
5. Release lease; write the audit terminal event. Delivery is claimed post-terminal (§6.4b) and cannot alter terminal status.

| Situation | Behavior |
|---|---|
| Artifact rendering failure | `artifacts_failed=true` + `artifact_failed` event; finalization continues to `succeeded` — **agent success is independent of artifact success** |
| Delivery failure | Outside the finalization state machine entirely; delivery rows carry their own lifecycle (§6.4b) |
| Worker crash during `finalizing` | Lease expires → recovery (§6.3) inspects the row: `output_text IS NOT NULL` ⇒ **finalize-replay** on the same execution (steps 1–5 are idempotent); `output_text IS NULL` ⇒ attempt failure → new attempt row |
| Lease expiry during `finalizing` (runner alive) | Renewal fails ⇒ runner aborts at the next checkpoint; a recovering worker takes over per the rule above; fencing rejects the original runner's late terminal write after takeover |
| Terminal immutability | All terminal writes fenced + CAS-guarded on non-terminal states; no path re-opens a terminal execution |
| Cancellation during `finalizing` | Cancellation flips only non-terminal states → it applies (fenced CAS) while `finalizing`; the runner observes it at the next finalization step boundary; artifacts/delivery already produced are recorded, status lands `cancelled` (F-23 reconciliation rules apply to any side effect already issued) |
| DB failure during terminal-state persistence | The terminal CAS is a single statement; on failure the row remains `finalizing` (output already committed in earlier statements) → lease recovery finalize-replays → no partial terminal state can exist |
| Duplicate finalization (two recovering workers) | Both attempt the fenced terminal CAS; exactly one wins the `lease_token` compare; the loser's writes (including delivery claims) are rejected — duplicate finalization is structurally impossible |

**Recovery discrimination contract (how recovery classifies a `finalizing`/orphaned row — PostgreSQL fault-injection scenarios):**

| Observed row state | Recovery action | Scenario |
|---|---|---|
| `finalizing`, `output_text` present, lease expired | Finalize-replay the SAME execution (steps are idempotent) | Worker died mid-finalize |
| `finalizing`, `output_text` NULL (cannot occur by protocol — output precedes the CAS; if seen: protocol violation) | `failed(transient)` + new attempt row + `error` event | Defensive |
| `running`, lease expired, `output_text` present | Treat as finalize-replay (stream ended, persist raced) | Crash between output persist and finalizing CAS |
| `running`/`claimed`, lease expired, no output | **Reconciliation barrier first (F-27):** replay-check the side-effect ledger §1.5c for this attempt — outcome classes per the four-outcome table (§6.6). `confirmed_complete` rows ⇒ the attempt is *not* silently re-run past them; `confirmed_not_started` ⇒ safe to retry; `unknown + idempotent` ⇒ retry with the Gate B key; `unknown + non-idempotent` ⇒ **block automatic retry**, open a reconciliation item (human decides, audited). Only after every ledger row for the attempt is resolved does recovery open the new attempt row (same occurrence, `UNIQUE(occurrence_id, attempt)`) | Crash mid-stream |

| Terminal already set, late fenced write arrives | Rejected by fencing; no-op | Duplicate finalization / stale runner |
| Terminal state unknown after a failed terminal write (connection lost after commit) | Recovery **reads** the row — the DB is the truth; committed terminal is honored, uncommitted → finalize-replay | Crash between commit and runner learning the result |

**Barrier rationale (F-27):** `output_text IS NULL` proves only that the final output was not persisted — it says *nothing* about side effects already dispatched by the attempt. Recovery therefore never treats "no output" as "no side effect"; the ledger (§1.5c) is the sole authority on whether an external operation occurred.

Retries never silently repeat completed side effects: before any new attempt opens, the **pre-retry reconciliation barrier (§6.6, F-27)** must have resolved every §1.5c ledger row of the prior attempt; a new attempt then executes tools under a **fresh attempt-scoped idempotency key** (Gate B); tools with provider-side idempotency dedup; non-idempotent tools re-issue only per the F-23 rule 4 + F-27 barrier restrictions, and every reissue is audited.

### 6.3 Startup recovery (replaces `reap_on_startup` force-reap)

New instances run `recover_leases()` only: executions with **expired** leases are reclaimed (attempt+1) or skipped per policy; executions with **unexpired** leases owned elsewhere are left untouched (fixes audit B4). Escape hatch `AUTOMATION_FORCE_REAP_ON_STARTUP` (default false) for single-instance emergencies.

### 6.4 Event payloads (append-only, UI-grounded)

`plan_step {step_id, title, status}`, `tool_call_started/finished {tool_name, args_digest, outcome, duration_ms, datasource_id?}`, `datasource_used {source_id, op}`, `approval_requested {approval_id, risk_tier, reason}`, `artifact_created {file_id, name, mime}`, `error {classification, transient, message}`, `delivery {channel, status}`, `lifecycle {from, to, reason}`.

### 6.4b Delivery persistence protocol (normative — F-13)

1. After the fenced terminal CAS succeeds, the runner (or delivery recoverer) inserts `automation_delivery_events (execution_id, channel, idempotency_key)` per enabled channel **before** performing the channel side effect — the UNIQUE `(execution_id, channel)` row is the durable claim; the claimant mints `claim_token` + `claim_expires_at` (5 min).
2. The channel side effect (chat bubble upsert / email send with deterministic `Message-ID` / panel state) executes only while the claim is held; on provider ack → `delivered` (token-compared write). On permanent provider rejection (SMTP auth failure / recipients refused → `EmailPermanentError`) → `failed_permanent`. On send-without-confirmable-outcome — crash window, or SMTP timeout (`TimeoutError` → `EmailTransportError`: the server may have accepted) → `unknown`. **Manual retry** of `unknown`/`failed_permanent` email is an audited operator action in the Center.
3. **Boundary:** execution `succeeded` + delivery `failed_permanent`/`unknown` is a legal, visible combination — the run is **never re-executed** because a channel failed; channels fail independently (test §10.31).
4. **Recovery after worker crash:** the janitor's recovery scan (§6.1) processes: missing delivery rows on terminal executions (claim + send); rows with expired `claim_expires_at` still `claimed`/`pending` (re-claim, `attempts+=1`, ≤3 then `failed_permanent`); `unknown` rows → reconciliation per §1.5b (provider check where available, else age-out to `failed_permanent` with `ambiguous_outcome_unresolved`; retry only via explicit `delivery_retry_unknown` opt-in).
5. **Honesty statement (F-24 corrected):** chat/panel delivery is effectively-once visible state (idempotent upserts). **Email is at-least-once delivery-*attempt* semantics with possible duplicate messages**; the deterministic `Message-ID` identifies the message and *may* enable downstream dedup where relays/clients honor it — **that is not a guaranteed property and is never claimed**. The system never claims exactly-once external delivery.
6. Delivery is parent-owned (subprocess children never write delivery rows — §11.4).

### 6.5 Subprocess event-durability protocol (normative — F-22, Option C)

**Evaluated options.** (A) durable child-side spool file + replay — strongest telemetry durability, but adds filesystem lifecycle, spool cleanup, and partial-write handling inside the child; (B) parent-owned durable broker — essentially (C) with telemetry durability added; (C) **bounded-loss telemetry + durable critical events** — smallest architecture, matches the existing `sandbox_worker` precedent (worker→PostgreSQL event persistence) without new infrastructure. **Selected: Option C.** Rationale: telemetry loss is observable, bounded, and harmless (the UI stream is best-effort by nature); only state-changing events need durability, and those are few — synchronous parent-ACK-after-commit costs one round-trip each and is naturally backpressured. Never claim zero event loss: telemetry loss is an explicit, bounded property of this protocol.

**Frame schema (JSONL over the control pipe):**

```json
{"event_id": "uuid", "execution_id": "…", "attempt": 0, "seq": 42,
 "critical": true, "type": "tool_call_finished", "payload": {…}}
```

- `seq` is monotonic per `(execution_id, attempt)`; `critical` events are exactly: `lifecycle`, `approval_requested/resolved`, `artifact_created`, `artifact_failed`, `error`, `verification`, `tool_call_started/finished` (state-relevant subset), `run_finished`. Telemetry: `phase` progress, `plan_step`, deltas, streaming progress.
- **Parent ACK:** `{execution_id, attempt, ack_seq, committed_upto}` sent **only after the event row is committed** to `automation_execution_events` (parent DB tx) — the frame names the attempt it ACKs (F-26), so a stale ACK from a previous attempt can never be applied to the current one. For critical events the child **blocks** on the ACK before proceeding past the boundary (synchronous checkpoint — natural backpressure). For telemetry the parent ACKs opportunistically; gaps are permitted and recorded (`dropped_telemetry` counter), never silently ignored.
- **Duplicate detection:** `UNIQUE(execution_id, attempt, seq)` on critical rows (F-26 — sequences are per-attempt); duplicate replay **within one attempt** is absorbed by the unique index (idempotent insert); **different attempts start their own sequence at 0** — cross-attempt reuse is legal and cannot collide. Parent ACK frames carry `{execution_id, attempt, committed_upto}` so an ACK can only ever apply to the attempt that sent the events; recovery attributes every event to its attempt, making cross-attempt confusion structurally impossible.
- **Gap detection:** parent tracks `last_critical_seq` per `(execution_id, attempt)`; a critical event with `seq > last_critical_seq + 1` is accepted (telemetry gap) but a *missing critical seq* at a boundary is a protocol violation → `error` event + execution failed(transient).
- **Replay/reconnect:** on reconnect the child re-sends all un-ACKed critical events (≤1 in flight under the blocking rule); parent absorbs duplicates via the unique index.
- **Child crash:** pipe EOF ⇒ parent marks `failed(transient)` (retry per policy). A critical event the child never sent is lost **with the attempt** — terminal state and output are parent-computed/persisted independently of telemetry (§6.2b), so recovery correctness never depends on telemetry delivery. Rendered-but-unreported artifacts are re-rendered by finalize-replay (idempotent).
- **Parent crash:** the lease expires; the child (now orphaned) is killed at next deadline/checkpoint; recovery re-claims per §6.3. The child's un-ACKed in-flight critical event is re-sent by the replacement attempt? No — a new attempt emits its own events; the old attempt's last critical event is lost **and must not be recovered** (its execution is failed by the reaper; the new attempt re-derives all state).
- **Backpressure/buffers:** OS pipe capacity + parent ingest queue (10,000 frames); telemetry dropped when full (counted); critical events block the child (cannot overflow).
- **Retention:** `automation_execution_events` 90-day purge (§1.4); replay from the ledger is impossible after purge — by design (telemetry, not a durable log).

**Crash-injection matrix (all seven-plus scenarios):**

| # | Scenario | Outcome |
|---|---|---|
| 1 | Child crashes before sending a critical event | Event lost with attempt; parent EOF → failed(transient); new attempt re-derives state; telemetry loss bounded & counted |
| 2 | Child crashes during transmission (partial frame) | Parent discards partial frame; EOF handling as #1 |
| 3 | Parent crashes before committing an ACKed-pending event | Event not persisted; lease expiry → recovery; new attempt re-derives; no half-committed rows |
| 4 | Parent commits but crashes before ACK | Event **persisted**; child died unaware — a reconnect replay of the SAME attempt is absorbed by UNIQUE(execution_id, attempt, seq); a replacement attempt starts its own sequence at 0 — no loss, no duplicate, no cross-attempt collision |
| 5 | Child receives ACK then crashes | Event durable; state consistent; parent proceeds |
| 6 | Duplicate replay (reconnect) | Absorbed by UNIQUE(execution_id, attempt, seq) |
| 7 | Sequence gap (telemetry dropped) | Permitted + counted for telemetry; violation if a *critical* seq is missing at a boundary |
| 8 | Parent DB unavailable | Critical events queue in the pipe (blocking); parent cannot commit → execution stalls → lease expiry → recovery; no false ACKs ever |
| 9 | Buffer capacity reached | Telemetry dropped (counted); critical events never dropped (blocking) |

### 6.6 Cancellation versus external side effects (normative — F-23)

Three distinct concepts, never conflated: **(1) cancellation requested** (signal set), **(2) execution process terminated** (worker stopped), **(3) external side effects confirmed stopped or reconciled** (per-tool outcome known). (1) ⇒ (2) at the next checkpoint or at wall-clock `terminate()`; (2) **never** implies (3).

**Mechanics (verified against current code):** the cooperative mechanism checks cancellation **between SSE chunks / between tool calls** — the tool executor has no mid-call checkpoint (`agent_loop/tool_executor.py`: no cancel checks), so a running tool call **always completes** under cooperative cancellation. A tool call therefore finishes atomically from the harness's perspective: "cancellation requested" during a tool call ⇒ the tool's outcome is observed, then the run cancels. **Subprocess `terminate()` (hard kill) is the only path that can interrupt a tool mid-call** — after it, any in-flight external request is in an **unknown** state that only Gate B reconciliation (§11.3) can resolve. A process kill never "undoes" a request already accepted by another service.

**Rules:**

1. Every side-effecting tool must declare Gate B metadata (§11.3) and accept the attempt-scoped idempotency key; `erp_submit_tool.py` is the house precedent (ledger-layer idempotency + `idempotent_replay` + audit events — verified).
2. After cancellation/termination, the runner (or recovery) records a `side_effect_reconciliation` execution event per in-flight/outstanding side-effecting call: `confirmed_complete | confirmed_not_started | unknown`.
3. **Final status when cancellation follows an irreversible side effect:** the execution remains **`cancelled`** — cancellation status is not rewritten to `succeeded`/`failed` by a side effect; the completed side effect is recorded in the reconciliation event and audit trail, and its output (e.g., a submitted ERP document) is surfaced in the execution detail UI. The user sees "Cancelled — 1 external action had already completed".
4. `unknown` side effects after a hard kill block automatic retry of the *same* attempt's tool; the retry decision itself is governed by the F-27 barrier (rule 6 below) — for unknown **idempotent** tools a retry may re-issue under a fresh attempt-scoped key (provider dedup applies per Gate B), and for unknown **non-idempotent** tools automatic retry is blocked until an operator reconciles (rule 6), with the resulting reissue audited.
5. Audit: every cancellation writes `automation_cancelled`; every reconciliation writes `side_effect_reconciled` with outcome. Existing tool authorization/approval mechanisms (Gate A) are untouched.

**6. Pre-retry reconciliation barrier (normative — F-27).** Before any new attempt of the same execution is opened (retry scheduling, lease-recovery re-claim, or `Run now` force), the runner/recovery **must** resolve every `automation_side_effect_ops` row (§1.5c) belonging to the concluded attempt(s). The ledger is the sole source of truth for side-effect state — `output_text IS NULL` is never evidence that a side effect did not happen. For each unresolved ledger row, reconciliation attempts, in order: (a) `provider_check` — query the external system using the operation's `idempotency_key`/business identity (e.g., ERP document lookup); (b) `ledger_replay` — re-derive the outcome from authoritative evidence already in the system (tool result committed before the crash, provider-side receipt); (c) if neither can establish the outcome and the row is older than the reconciliation window → `unknown` via `timeout_ageout`. **The system never invents a successful reconciliation when the external system cannot establish the outcome.**

Four outcome classes and their retry consequences:

| Ledger outcome after reconciliation | Evidence | Consequence for the next attempt |
|---|---|---|
| `confirmed_complete` | Provider confirms the op occurred (provider_check/ledger_replay) | The op is **preserved and never repeated** — the new attempt skips it (its result is carried forward as verified context); a `side_effect_reconciled` audit event records it |
| `confirmed_not_started` | Ledger shows `dispatched` but provider confirms no effect (provider_check) — or the op never reached `started` and the provider confirms nothing was created | **Safe to retry** — the new attempt re-executes the op under the fresh attempt-scoped key |
| `unknown` + tool is **idempotent** (Gate B `idempotent=true`) | Outcome could not be established | Retry **allowed** — the fresh attempt-scoped idempotency key plus provider-side dedup bounds duplicates; reissue audited |
| `unknown` + tool is **non-idempotent** (Gate B `reconcilable=false` or no provider check) | Outcome could not be established | **Automatic retry is blocked** for this execution — the run is failed with `blocked_on_side_effect_reconciliation`, a reconciliation item surfaces in the Center (execution detail) for a human to verify with the external system; an operator's explicit "mark reconciled" (`reconciled_via=operator`, audited) unblocks a subsequent manual retry. The system never guesses |

Barrier sequencing rule: the barrier applies **before** the new attempt's first tool dispatch, not merely before terminalization; a partially-reconciled attempt never runs concurrently with a new one (the new attempt row is only created after resolution). The barrier scan keys off `(status, updated_date)` index on §1.5c and is bounded (single scan per recovery decision, no polling loop).

---

## 7. Frontend Behavior Specification

### 7.1 Chat creation
- Review card renders **from `GET /drafts/{id}`** (not message text). Editable fields: name, description, schedule (+ timezone), project, output format, notifications, execution options (retry/budgets advanced section). Every edit → `PATCH` → server re-validation → inline field errors or success.
- **Confirm button** disabled until `validation.ok`. On confirm: `POST .../confirm` → success card "Scheduled. Next run {next_run_at} ({tz})" + link to Center; the `zhanlu:automation-created` event (existing) now fires on confirm, and the Center subscribes.
- Clarification: agent asks questions before calling `propose_automation` (prompt-level, retained); the card also surfaces `validation` gaps.
- Duplicate proposals: a second `propose_automation` in the same conversation for a similar objective returns a draft link, not a new definition — confirm is the only creation gate.

### 7.2 Automation Center (`/automation`)
- Data: `GET /definitions` with server-side `project_id/status/search/trigger_type` params + cursor pagination (removes 200 cap, client-side filtering, staleness).
- Month view: calendar of `next_run_at` occurrences (definitions + recent executions). Card view: status chip (active/paused/archived), schedule description **with timezone**, next run, last execution outcome chip (from last execution join), success/failure sparkline (30-day aggregates from executions). Table view: sortable columns.
- Actions: Run now (with in-progress guard + force option), Pause/Resume, Edit (detail page), Delete (confirm dialog; warns about in-flight execution which will be cancelled), Execution history drill-in.
- Subscribes to `zhanlu:automation-created`/`definition.changed` (same-tab refresh) — and on window focus re-fetches (belt-and-braces for multi-tab).

### 7.3 Execution detail (drawer + detail page)
- Timeline from `GET /executions/{id}/events` (poll `after_seq`; switch to SSE `?stream=true` reusing the existing signed-token stream endpoint pattern). Sections: Plan (FSM steps), Activity (tool invocations w/ outcomes + durations), Datasources used, Delegation (child agent runs), Approvals (with approve/reject inline for attended users), Verification, Artifacts (preview/download), Errors + retry chain (parent/attempt links), Provenance footer (agent_run_id, conversation_id, spec_version, trigger, occurrence_key).
- Status chips use the backend enum exactly: `claimed | running | awaiting_approval | finalizing | succeeded | failed | cancelled | timed_out | skipped` — `timed_out`/`skipped` now real (audit B14 resolved by backend, frontend vocab aligned).

### 7.4 States & empty states
- No automations: unchanged empty state + "New Automation Task" (routes to chat prefill — retained).
- Draft-not-confirmed items are **not** in the Center (drafts surface only in chat) — intentional, documented.
- Degraded schedule: warning banner "Schedule could not be parsed ({schedule_error}) — task will not fire until fixed" with Edit shortcut.

---

## 8. Failure and Recovery Design (20 cases)

Legend: **Detect** → **Transition** → **Recovery** → **Retry** → **Idempotency** → **User-visible** → **Audit**.

| # | Case | Design |
|---|---|---|
| 1 | Duplicate confirmation | Same `confirmation_key` replay → 200 original definition (`already_confirmed`); stale `If-Match` → 409 with current draft. Unique partial index backstop. |
| 2 | Scheduler process crash | Leader lock lost; next tick re-acquired by any instance. Occurrence already claimed (row exists) is not re-fired; executions in flight are governed by leases. |
| 3 | Worker crash | Lease expires (30s heartbeat) → `recover_leases()` reclaims as attempt+1 (retry budget) or skip-row. Parent-owned terminal writes mean no partial state. |
| 4 | Multiple scheduler instances | Only leader claims; claim insert is UNIQUE-guarded; runners on any instance protected by fencing tokens. |
| 5 | Missed scheduled execution | §6.1a: within grace → one late fire; beyond grace → occurrence `skipped(misfire)`; multiple missed slots collapse into **one** occurrence row per episode (`misfire_collapsed_count=N`). Backlog bounded at one row per definition per episode, by construction. |
| 6 | Database unavailable | Dispatcher tick fails → log + retry next tick (no state change); runner DB loss → lease expires → recovery path. No silent occurrence loss (claim is transactional with next_run advance). |
| 7 | Redis unavailable | N/A — Redis is not part of the automation design (audit confirmed no Redis dependency; none added). |
| 8 | Datasource unavailable | Retained preflight probes fail fast pre-LLM → `failed(transient)` + retry. Runtime tool failures → tool_outcome tally; all-failed → truthfulness gate fails run. |
| 9 | Agent timeout | Wall clock (600s default) → Stage 1: cancellation signal + terminal `timed_out` (fenced) + orphan events dropped; Stage 2: subprocess `terminate()` (hard). Retry per policy. |
| 10 | Agent cancellation | Two-phase retained (DB CAS + cooperative signal); Stage 2 adds hard kill. Cancelled runs never retry (retained). Late side-effect writes dropped by fencing + terminal check. |
| 11 | Child-agent timeout | Delegation is a tool call inside the parent budget; child runs are separate agent_runs; parent timeout cancels the whole invocation contract (signal propagates to delegation loop's existing deadline checks). Orphaned children: their events carry `parent_execution_id`; orphan reaper flags `delegation_orphaned` event. |
| 12 | Artifact rendering failure | Isolated try/catch → `automation_files.status=failed` + `artifacts_failed=true` + event; run still `succeeded` with text output + delivery note. |
| 13 | Notification failure | Per-channel `automation_delivery_events` lifecycle; up to 2 retries (attempts ≤ 3), then `failed_permanent`; never affects run status (§1.5b/§6.4b). |
| 14 | Task modified during execution | Execution froze `spec_version` at claim; edits apply next occurrence; no guards in hot loop (audit B7 resolved by isolation, not locking). |
| 15 | Task deleted during execution | Delete cancels non-terminal executions first (fenced) → `cancelled(skipped_reason=definition_deleted)`; runner sees cancellation at next checkpoint; delivery suppressed for cancelled-by-delete. |
| 16 | Deployment restart | Non-destructive startup: only expired leases reclaimed. Unexpired leases from rolling-deploy siblings untouched (audit B4 fixed). |
| 17 | Lease expiration | Heartbeat miss (GC pause, network) → renewal failure → runner aborts at checkpoint; lease reclaimed by recover_leases → attempt+1. Fencing prevents the aborted runner's late terminal write. |
| 18 | Duplicate delivery | Delivery is parent-side, post-terminal, claimed by the UNIQUE `(execution_id, channel)` row in `automation_delivery_events` before the side effect (§6.4b); up to 2 retries (attempts ≤ 3) then `failed_permanent`; `unknown` outcomes reconciled per §1.5b. Chat bubble is updated-in-place (retained mechanism). |
| 19 | Partial execution success | Truthfulness gate retained: partial tool failures ship with warning + tally (honest); verification event records gate outcome; `partial` is a delivery note, not a new status (no status inflation). |
| 20 | Approval timeout | `approval_ttl_seconds` + `expire_old_requests()` → execution `FAILED(approval_required)` (unattended) with `approval_required` event + Center notification + audit. Never silently approved (audit N9 fixed). |

---

## 9. Security Requirements

1. Every automation endpoint authenticated; `/parse-schedule` included (audit B18).
2. Single policy function `authorize(user, definition|draft, action)` — the only access check (org + owner + role); Center, chat tool, and executor all resolve through it (audit B9's three-policy problem ends).
3. Unattended authority = creator's harness permissions ∩ spec constraints (tools/datasources) ∩ runtime-agent deny-list (admin/automation-CRUD, retained `automation_runtime.py:74-78`). The spec can only *narrow* authority; no path widens it.
4. No silent approvals: `skip_confirmation` retired; `pre_approved_scope` is per-spec, human-confirmed, audited, and capped by org policy; every auto-proceeded pause writes `approval_auto_scope` audit event with the scope class.
5. Audit: `record_audit_event()` at every definition mutation, every execution terminal, every delivery failure, every approval resolution. Actor identity: user for interactive, `automation:{definition_id}:{execution_id}` for unattended.
6. Provenance: execution ↔ agent_run ↔ conversation ↔ artifacts ↔ approvals all id-linked (§1.3–1.6); the execution detail UI and `/definitions/{id}/audit` expose it.
7. Credentials: unchanged org-KB model (documented limitation; platform-level secret-at-rest fix tracked separately per the 2026-10-20 audit); no credential values in events, logs, or audit details (existing `to_dict()` stripping retained).
8. Signed surfaces (email download 7-day HMAC, SSE stream token 15-min) retained with tenancy checks (tested today).

## 10. Acceptance Criteria (verifiable, per component)

1. Confirm replay with identical `confirmation_key` returns the same definition id (API test, real DB).
2. Two concurrent confirms (threads) create exactly one definition (Postgres concurrency test).
3. Duplicate occurrence claim (two claimants, same occurrence_key) creates exactly one execution (SQL-level test).
4. Lease expiry with live "orphan" runner → reclaim + fenced late-write rejection (fault-injection test with fake clock).
5. Kill -9 on the runner process → recovery within `2×heartbeat`, execution retried with attempt+1 (multi-process test).
6. Two dispatcher instances → zero duplicate fires over a 200-tick simulation (multi-process test).
7. Rolling restart with a live run on instance A → instance B startup does not touch it (lease unexpired).
8. `propose_automation` never creates a definition (registry test: no write path exists).
9. Approval pause in unattended run without scope → `failed(approval_required)` + audit event, zero `ApprovalService.approve` calls (integration test).
10. Artifact render raising → run `succeeded`, `artifacts_failed=true`, delivery notes render failure.
11. Delete during run → in-flight execution cancelled, zero post-delete deliveries.
12. Misfire beyond grace → `skipped(misfire)` row + audit; within grace → one late fire.
13. Cron DST boundary (e.g., `0 2 * * *` on a DST-change date, tz `Asia/Shanghai` / `Europe/Berlin`) → next_run_at correct via zoneinfo (unit test on `next_run_at` — the currently untested core).
14. `/definitions` returns owner-scoped, project-filtered, paginated results including tasks created via chat (API test closing audit B9).
15. Every definition mutation writes exactly one audit event with correct actor (API test).
16. `automation_executions` FK/unique constraints exist after `alembic upgrade head`; `_ensure_schema` drift-checker reports zero drift.
17. Event ledger is append-only: no API/code path mutates or deletes `automation_execution_events` (code review + test).
18. Chat-created automation visible in Center within one refresh without remount (frontend test: event subscription).
19. Legacy row compatibility: a pre-migration `automation_tasks` row (string `max_retries`, no `spec`) loads, lists, fires once, and appears identically pre/post cutover (migration test).
20. No code path under `app/services/automation_*` imports from `app.routers.agents` (static test) — coupling eliminated.
21. Duplicate delivery impossible: killing the runner between artifact render and chat-write, then lease recovery, results in the delivery retried exactly once with no duplicate bubble/email (`automation_delivery_events` UNIQUE `(execution_id, channel)` upsert test).
22. Occurrence/attempt identity: two claimants race the same occurrence → exactly one `automation_occurrences` row and one attempt-0 execution; a double retry insert violates `UNIQUE(occurrence_id, attempt)` and is rejected (Postgres concurrency test).
23. Crash during confirmation: process killed after the draft `FOR UPDATE` but before COMMIT → draft still `draft`, no definition; retry confirm succeeds and returns the same definition id as a concurrent winner (fault-injection test).
24. Delivery recovery: kill the runner after the terminal CAS but before the delivery claim → janitor delivers within one sweep, exactly one bubble, delivery row `delivered` (§6.4b).
25. Misfire collapse: a 3-day outage on a 5-min cron yields **exactly one** occurrence row per definition (fired late or `skipped(misfire)`) with `misfire_collapsed_count = N` — never N rows (§6.1a).
26. Cancellation reasons: user/admin/delete/org-deactivation/parent-cancel produce the typed `cancel_reason` values of §3.2; org deactivation stops new claims and cancels in-flight runs at the next checkpoint.
27. Harness parity: static test proves automation adds no tool-routing tables/handlers and no planner; the runtime agent toolset equals the parity list plus documented exclusions; FSM planning reaches the agent (force_planning passes through the contract).
28. Provenance completeness: `agent_run_id` and `conversation_id` are present on **succeeded and failed** executions alike (contract test on the terminal event).
29. Delivery claim crash matrix (F-13): crash before external send → expired claim re-claimed and delivered once; crash after provider acceptance before success persistence → row `unknown`, reconciled per policy, never blindly retried (all seven F-13 scenarios).
30. Email honesty: two send attempts with the same idempotency key produce identical deterministic `Message-ID` values (receiver-side dedup possible); the system nowhere claims exactly-once email delivery (unit + provider contract test).
31. Channel independence: chat delivery fails permanently while email succeeds (and vice versa) — the execution remains terminal, the agent is never re-run (§6.4b).
32. Workflow occurrence identity (F-15): two pollers race the same workflow due-slot → exactly one `workflow_occurrences` row via the shared `scheduler_core` claim; workflow semantics (flow_mode AgentApp + trigger node) preserved (multi-process test).
33. Misfire determinism (F-16): every worked example of §6.1a reproduces exactly under fixed clocks and timezones, including the DST fold case and the edit/pause discarded-episode paths.
34. Occurrence-level cancellation (F-18): a **pending** occurrence with no execution row is cancellable; `cancel_reason/cancelled_at/cancelled_by` are persisted; the `superseded` path is exercised.
35. Subprocess ownership (F-21): a child process attempting any automation-persistence write fails; kill -9 mid-stream → parent marks `failed(transient)` and retries; event persistence is seq-keyed with no duplicates or gaps.
36. Two-gate independence (F-20): an approved-but-non-idempotent tool never auto-retries within an attempt; an idempotent-but-unapproved tool pauses for approval; the two decisions are made by different mechanisms (registry RBAC/scope vs registry idempotency metadata) and neither implies the other.
37. Event-durability crash matrix (F-22): all nine scenarios of §6.5 reproduce — critical events are exactly-durable (ACK after commit, UNIQUE(execution_id, attempt, seq) absorbs replays); telemetry loss is bounded and counted; no scenario loses a critical event after its parent commit (fault-injection suite).
38. Cancellation vs side effects (F-23): cancellation during a tool call → the tool completes, then the run cancels; hard kill mid-call → `side_effect_reconciliation` event with `confirmed_complete | confirmed_not_started | unknown`; an irreversible side effect followed by cancellation leaves the execution `cancelled` with the side effect surfaced — status is never rewritten (integration + fault-injection tests).
39. Email honesty (F-24): SMTP timeout maps to `unknown` (transport error), auth/recipients-refused to `failed_permanent`; no code path or doc claims receiver-side dedup as guaranteed; duplicate emails in the crash window are detectable via Message-ID (provider contract test).
40. Finalization fault injection (F-25): the recovery-discrimination table of §6.2b is exercised on PostgreSQL — DB failure during terminal CAS leaves `finalizing`; duplicate finalization impossible (fencing); committed-but-unacknowledged terminal is honored on read (PG fault-injection profile).
41. Same-attempt event replay (F-26): re-sending an un-ACKed critical event within the SAME attempt is absorbed by `UNIQUE(execution_id, attempt, seq)` — no duplicate row, no error surfaced; the ACK is re-issued (integration test on the §6.5 pipe protocol).
42. Cross-attempt sequence reuse (F-26): attempt 1 emits seq 0–5 and fails; attempt 2 re-emits seq 0–… — both stored without collision; recovery attributes every event to its attempt (ACK frames carry `{execution_id, attempt, committed_upto}`); no cross-attempt confusion in the timeline.
43. Crash after external acceptance before output persistence (F-27): the ledger row reaches `started` but output is never persisted; lease recovery applies the §6.6 barrier — provider_check confirms completion → `confirmed_complete`, the op is never repeated and the verified result is carried into the new attempt; no duplicate ERP submission occurs (integration test with a stub provider that counts invocations).
44. Unknown non-idempotent side effect blocks retry (F-27): a non-idempotent tool's ledger row stuck `unknown` (no provider check available) causes recovery to fail the execution with `blocked_on_side_effect_reconciliation` instead of opening a new attempt; the Center surfaces the reconciliation item; an operator `mark reconciled` (`reconciled_via=operator`, audited) unblocks a subsequent manual retry. Companion case: `confirmed_not_started` (provider confirms nothing was created) permits the automatic retry without operator action.

The finding-by-finding disposition table (B1–B18, S1–S6 → fix / redesign / accepted) is in the roadmap document §Testing Strategy.

---

## 11. Enforcement Contracts (consistency-review strengthening)

Each contract names its enforcement point and verification test — not an aspiration:

1. **Advisory-lock ownership.** Acquired per tick on a *dedicated* connection (`pg_try_advisory_lock(380631)`), released by closing that connection at tick end; never held across ticks, never on a pooled request connection. Non-Postgres: check skipped with a startup warning. *Test:* tick loop asserts connection lifecycle; S2 regression test with a forced connection recycle.
2. **Lease fencing.** `fencing_token` (monotonic ULID per execution) is compared on every terminal, event-append, and delivery write (`WHERE lease_token = :token`); a failed lease renewal (heartbeat 409/rowcount 0) aborts the runner at the next checkpoint. Fencing is optimistic (token compare), documented as such. *Test:* §10 item 4.
3. **Two independent gates: authorization (A) vs side-effect safety (B)** — F-20. Human approval never implies retry safety; the gates are evaluated independently at the tool-dispatch boundary (registry/harness-enforced, never prompt-enforced):

   **Gate A — Authorization (may it run at all?):** registry RBAC + `TraceContext` principal → tool allow/deny (spec constraints) → org unattended-execution permission → if the tool class requires approval and is outside `pre_approved_scope` → ApprovalService pause (Gate A resolves via human approve/reject or TTL failure). Existing ApprovalService, RBAC, and registry are preserved unchanged.

   **Gate B — Side-effect safety (may it be retried?):** registry metadata `idempotent: bool` + `idempotency_key_scope` + `reconcilable: bool`. The invocation context carries `{execution_id, attempt}`; tools performing writes must accept the attempt-scoped idempotency key and use it with their provider where supported.

   | Approved? | Idempotent? | Outcome contract |
   |---|---|---|
   | yes | yes | May auto-retry on transient failure (key dedups provider side) |
   | yes | no | Runs **at most once per attempt**; execution-level retry creates a NEW attempt with a NEW key — the tool must guard against re-application (documented per-tool responsibility) |
   | no | yes | Human approval required (Gate A); after approval, retry-safe |
   | no | no | Human approval + explicit operator awareness that retries can duplicate; ambiguous timeout ⇒ `unknown` outcome, no auto-retry |

   **Ambiguous timeout (Gate B):** a write-tool that times out without a provider-verifiable outcome returns `outcome="unknown"`; the executor records it, does not auto-retry the tool, and surfaces it in the verification event. Automation-CRUD tools remain hard-forbidden unattended. *Test:* registry policy unit tests + two-gate matrix tests (R7).
4. **Subprocess execution and database ownership (F-21).** Verified against the existing pattern (`backend/sandbox_worker/main.py` uses `subprocess` workers). Because the production agent runtime requires DB access (conversations, messages, KB reads, tool handlers), the child **cannot** be DB-free without breaking Invariant 2.3 — so the ownership contract is **table-scope**, enforced by injection:

   1. **Child may perform:** agent-runtime-owned operations only — `chat_messages`, `agent_conversations`, `agent_runs` (the invocation contract stamps provenance via `AgentRunService.finalize_run`, which may execute in-child under subprocess mode), KB/schema reads, tool-handler DB usage, artifact *staging*. **Child must NOT perform:** any write to `automation_executions`, `automation_occurrences`, `automation_execution_events`, `execution_leases`, `automation_delivery_events`, `automation_definitions`, or `audit_events`.
   2. **Handle:** the child receives its own `SessionLocal`-derived session (same DB, config-injected) for runtime tables; it receives **no handle to the automation persistence layer** — those modules are never imported by the child entry point.
   3. **Event persistence:** the child emits typed events over the seq-numbered JSONL control pipe under the **Option C durability protocol (§6.5)**: critical events are synchronously ACKed by the parent *after DB commit* (blocking, backpressured, exactly-durable via `UNIQUE(execution_id, attempt, seq)`); telemetry is best-effort with bounded, counted loss; **the parent persists** all accepted events (and forwards to the UI stream). The event sink is *injected* into the invocation contract (parent = DB writer; child = pipe writer).
   4. **Terminal status writes:** parent-only (§6.2b ownership).
   5. **Event transport:** JSONL frames `{seq, event}`; parent ACKs batches; critical events are replayed/absorbed per §6.5 (UNIQUE(execution_id, attempt, seq)); telemetry gaps are permitted and counted (`dropped_telemetry`) — not re-requested (bounded-loss protocol, §6.5).
   6. **Cancellation/deadline propagation:** parent→child control messages (`cancel`, `deadline_extended`); the child's contract checks the control pipe at the same checkpoints as the in-process `CancellationSignal`. Wall-clock deadline enforced by the parent (`process.terminate()` at deadline) *and* cooperatively in-child.
   7. **Child crash:** pipe EOF / exit-before-terminal ⇒ parent marks the execution `failed(transient)` (retry per policy); the child's own partial runtime writes (chat messages) remain as honest traces of the interrupted turn.
   8. **Fencing vs stale children:** children never write fenced tables, so a stale child cannot corrupt state; late child events arriving after the parent terminalized are dropped (parent-side `execution terminal` check that exists today at `automation_executor.py:1408-1415`).
   9. **Credentials/identity:** DB URL via environment (same as parent, unchanged secret posture); user identity passes as principal id + org/app/project in the invocation context (no session cookies); `TraceContext` is set inside the child so registry RBAC evaluates live.

   *Test:* multi-process suite (R6/R11) — child-writes-automation-tables must fail; pipe-loss recovery; cancel propagation; kill -9 child mid-stream.
5. **Creator permission revocation.** At claim time the creator must exist, be active, and belong to an active org (fail-fast, retained). Mid-run revocation does **not** kill the run (spec frozen at claim), but per-tool RBAC is evaluated live via `TraceContext`, so revoked tool/datasource permissions apply immediately within the run — the boundary is the tool call, not the run. *Test:* RBAC revocation mid-stream integration test.
6. **Org/project isolation.** Every scheduler query filters `org_id` and org-active; every Control Plane query goes through `authorize()`; the execution context carries the task's org/app/project and the harness re-derives per-tool scope from them. *Tests:* §10 items 14, 26; cross-org matrix in R3.
7. **Artifact-storage durability.** MinIO writes are verified by read-back (ETag/checksum) before `automation_files.status='ready'`; the row records `storage_backend` so local-fallback mode is explicit. *Test:* R9 tagged MinIO integration test.
8. **Run-now force semantics.** `?force=true` requires the same `authorize(user, definition, 'run')` grant, writes audit event `automation_run_forced`, and creates its own manual occurrence; concurrent runs share the per-task conversation (documented: chat writes are keyed per execution id and never overwrite another execution's bubble). Without `force`, a non-terminal execution ⇒ 409 `execution_in_progress`. *Test:* R3 API test.
9. **Agent invocation cancellation/timeout.** See §5 — deadline and cancellation enforced at the contract layer, checked per event yield and per tool dispatch. *Test:* §10 item 20 chain + R6 cancellation tests.
10. **Workflow-scheduler integration.** Claim/lease/fencing/recovery run through `scheduler_core`, parameterized by a per-subject table adapter (`automation_occurrences` / `workflow_occurrences` — §1.3a/§1.3b); no `subject_type` overload exists; the workflow scheduler's in-memory `_in_flight` set is deleted; its loop runs under the same per-tick leadership. *Test:* R5 multi-process suite extended to workflow subjects (§10.32).
11. **Alembic ordering.** One linear chain 126→131 (+ reserved ≥132, never pre-assigned) (§1.7); each migration owns exactly the tables listed there; backfills are idempotent and inside the migration. *Test:* §10 items 16, 19 + `alembic history` linearity check in CI.
12. **Execution-event vs audit-event separation.** `automation_execution_events` = high-volume operational telemetry (90-day retention, no security meaning, written by the runtime only); `audit_events` = permanent governance record (written by `record_audit_event()` at authorization-relevant moments only: definition mutations, claim/skip decisions, approvals, terminals, cancellations, delivery failures). An audit event may reference `execution_id`; the reverse is forbidden (no audit content inside telemetry). *Test:* R7 — every mutation emits exactly one audit event; retention task test.
