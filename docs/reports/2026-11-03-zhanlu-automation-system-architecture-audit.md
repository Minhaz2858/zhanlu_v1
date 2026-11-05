# Zhanlu AI Automation System — Full Architectural Audit

**Date:** 2026-11-03
**Scope:** Chat-based automation creation → persistence → scheduling → execution → delivery
**Method:** Static source-code investigation only. No code modified, no migrations, no DB writes. All claims anchored to `file:line` in this working tree.
**Repo state at audit:** branch `fix/automation-chat-artifacts-ordering`, commit `12030dd5` ("Phase 8 T8-4: DataSourcesSection binding editor"), 246 uncommitted changes present (untouched by this audit).

> Evidence convention: **[CONFIRMED]** = directly visible in source. **[SUSPECTED]** = code-supported but requires runtime conditions to trigger. Line numbers refer to the working tree at commit `12030dd5` and will drift after edits.

---

## 1. Executive Summary

The automation subsystem is a **real, substantial, and mostly coherent in-process engine** (≈8,600 lines across dispatcher/executor/API + supporting services) with genuinely mature mechanics in places: compare-and-swap (CAS) claim guards on schedule fire, a janitor + startup reaper for zombies, transient/permanent error classification with bounded retries, a truthfulness gate against canned output, datasource preflight probes, org-level tenancy, and ~250 backend tests (largely on failure paths).

However, the audit confirms the user-reported symptoms and identifies the structural causes:

1. **The "pending automation" shown in chat is not the task.** It is pure React client state (`Chat.jsx:382`, `AutomationDraftCard.jsx`). The real task is committed server-side during the chat turn — **before any user confirmation** — via two competing creation paths (agent tool-call vs. legacy frontend entity-create), neither of which is idempotent. Duplicate tasks and "created but invisible" tasks both follow directly from this design.
2. **The `/automation` management page reads a different access surface than chat writes.** Chat writes are owner-stamped; the page lists via the generic entity API filtered to `created_by_id == caller` with a client-side project filter and a hard 200-row cap, no event refresh, no status filter. Any drift in owner identity, project name, or page cap silently hides valid tasks.
3. **The scheduler is single-process by assumption.** The automation dispatcher has no distributed lock (only `start_scheduled_tasks` has one); the workflow scheduler's dedup is a process-local in-memory set — multi-worker deployment **guarantees duplicate workflow fires**. A crash window exists between the fire CAS and the execution INSERT that silently loses an occurrence. Startup reaping is destructive in multi-instance deployments (kills other workers' live runs; `lease_owner` is write-only diagnostics, never checked).
4. **Timeout and cancel are cooperative, not enforced.** The agent sub-thread is "left to finish on its own" after timeout (code's own comment), continuing to burn LLM tokens and produce side effects (chat writes, files, nested automations) after its row is marked failed.
5. **Governance has real holes:** zero `AuditEvent` writes anywhere in the automation subsystem; `skip_confirmation` auto-approves governance pauses unattended (unaudited); datasource access uses org-shared KB credentials with no per-user layer; delete does not cancel in-flight executions, which keep writing results after deletion.
6. **Schema governance is triplicated and drifting:** three parallel DDL mechanisms (Alembic, `main.py` hand-maintained ALTERs, `automation_dispatcher._ensure_schema()` introspection), with demonstrated divergence (`parent_execution_id` missing from one list; `email_notified_at` missing from another).

**Bottom-line architectural judgment:** the automation subsystem is *functionally deep but structurally fragmented at its edges* (creation paths, access surfaces, scheduler multi-instance posture, provenance links). It is not a thin scheduled-task wrapper — it genuinely invokes the production agent runtime — but its lifecycle is not one coherent, governable unit. Recommendation: **Option 2 (architectural refactoring) after a small set of P0 bug fixes**, detailed in §19.

---

## 2. Current Architecture Diagram

```
FRONTEND (React, :8089 nginx or :5173 vite)
  Chat.jsx ──────────────┐  intent: regex isCreateAutomationRequest (Chat.jsx:3758)
   │ pendingDraft state  │  server rebind: _AUTOMATION_SETUP_HEADER_RE (agents.py:3039)
   ▼                     │
  AutomationDraftCard.jsx  (client-only draft; "Send to Agent" = text into input box, NO API)
   │
   ├─ AGENT PATH (primary): POST /api/apps/{id}/agents/.../v3/.../messages/stream
   │      └─ LLM tool-call create_automation → agent_tools._create_automation (agent_tools.py:1691)
   │             └─ INSERT automation_tasks  [committed mid-turn, no confirmation, no dedupe]
   └─ LEGACY PATH: POST .../Core/InvokeLLMStream (CLARIFY_SCHEMA forces create_resource)
          └─ Chat.jsx:1887 base44.entities.AutomationTask.create
                 └─ POST /api/apps/{app}/entities/AutomationTask → entities.py:377 (+ schedule normalize :112-171)

  AutomationTasks.jsx ──── GET /api/apps/{app}/entities/AutomationTask?sort=-updated_date&limit=200
   │                        (owner-scoped via USER_SCOPED_ENTITIES; client-side project filter; no refresh listener)
  AutomationTaskDetail.jsx ─ GET/PUT entities + POST /api/automations/parse-schedule | {id}/toggle
  runAutomationTask.js ──── POST /api/automations/{id}/ensure-session → POST /api/automations/{id}/run
  useAutomationExecutionStatus.js ── 2s polling GET /api/automations/executions/{id}/status
  AutomationExecutionStatusDrawer.jsx (AppLayout) + automationStatusBus.js (in-memory store)

BACKEND (FastAPI, :5002) — all schedulers IN-PROCESS (no Celery for automation; Redis unused here)
  main.py startup (:766-802):
    start_scheduled_tasks()      10 asyncio maintenance loops, ONLY component with pg advisory lock (scheduled_tasks.py:875-905)
    start_dispatcher()           automation_dispatcher._dispatcher_loop — asyncio, 60s tick, NO lock
    reap_on_startup()            _ensure_schema() + force-reap ALL queued/running executions
    workflow_scheduler.start()   daemon THREAD + ThreadPool(4), 30s poll, in-memory dedup set only

  automation_dispatcher._tick (60s)
    ├─ janitor (every 5 ticks): reap by timeout_at / stale fallbacks
    ├─ self-heal: invalid/NULL task status → active
    └─ SELECT due tasks (active, next_run_at ≤ now, limit 20)
         └─ _fire: rules-only cron parse → CAS UPDATE tasks SET next_run_at=real_next WHERE next_run_at=orig
              └─ INSERT AutomationExecution(status=queued) → asyncio.create_task(_run_executor)
                   └─ Semaphore(3) → asyncio.to_thread(execute_automation)

  automation_executor.execute_automation
    ├─ CAS queued→running, stamp timeout_at=now+600s
    ├─ preflight: bound datasource resolution + connectivity probes; data_sync w/o sources → fail fast
    ├─ _run_agent_in_conversation: per-task chat session + AgentConversation (one-chat-per-task)
    │    └─ NEW event loop → calls routers/agents.add_message_stream directly (the SAME v3 chat path), force_planning=True
    │         tools = task.agent_id or hidden automation_runtime_agent (parity toolset, admin/automation-CRUD forbidden)
    │         identity = task creator; org/app/project from task
    ├─ truthfulness gate → _render_and_save_files (local FS + AutomationFile row)
    ├─ _persist_run_to_chat (chat bubble + artifact cards + unread)  [three delivery channels: chat, email, panel]
    ├─ CAS running→completed|failed → task.execution_history, last_run_at
    └─ failure/timeout/reap → schedule_retry (queued row + IN-MEMORY delayed run, backoff 60s→30m)

DATA
  Postgres (:5400 / zhanlu-mysql in other stacks): automation_tasks, automation_executions, automation_files
  Artifacts: local filesystem settings.generated_path/automation/<task>/<exec>/  (NOT MinIO/S3)
  Schema DDL: Alembic migrations + main.py specific_migrations raw ALTERs + dispatcher _ensure_schema()  (3 parallel mechanisms)
```

---

## 3. Complete Frontend-to-Backend Component Map

| # | Component | Source file | Entry points / API | DB tables | Status |
|---|---|---|---|---|---|
| 1 | Chat automation intent (frontend) | `frontend/src/pages/Chat.jsx:1463-1471, 3758-3763, 1682-1683` | — | — | Implemented (regex + agent-name rebind; brittle to paraphrase) |
| 2 | Chat automation intent (backend) | `backend/app/routers/agents.py:3039-3056, 3151-3156, 13624-13639` | v3 stream | — | Implemented |
| 3 | Automation agent prompt | `backend/app/services/agent_prompts.py:1561-1659` | — | — | Implemented |
| 4 | Task creation — agent path | `backend/app/services/agent_tools.py:1691-2025` (`_create_automation`) | LLM tool-call | `automation_tasks` | Implemented; **no idempotency, no user confirmation** |
| 5 | Task creation — legacy path | `frontend/src/pages/Chat.jsx:1850-1887, 1964-2066`; `backend/app/routers/entities.py:112-171, 377-402`; `backend/app/services/entity_service.py:557-567` | `POST /api/apps/{app}/entities/AutomationTask` | `automation_tasks` | Implemented (legacy); parallel write surface |
| 6 | Pending draft card | `frontend/src/components/chat/AutomationDraftCard.jsx` (parse :398-440, send :79-83) | none | **none — client state only** | Implemented; **not persisted server-side** |
| 7 | Execution tool (chat) | `backend/app/services/automation_chat_tool.py:128-215` (`execute_automation`), in-flight guard :59-78 | agent tool | reads tasks, calls dispatcher | Implemented (owner-checked, recursion-capped) |
| 8 | Run-output chat binding | `backend/app/services/automation_sessions.py:114-135` | `/automations/{id}/ensure-session` | `chat_sessions` | Implemented |
| 9 | Automation REST API | `backend/app/routers/automation_api.py` (1,501 lines) | see §8 endpoints | all three tables | Implemented; gaps: parse-schedule unauth, org-only tenancy |
| 10 | Dispatcher / scheduler | `backend/app/services/automation_dispatcher.py:683-732` | startup hook | tasks + executions | Implemented; **no distributed lock** |
| 11 | Fire/claim | `automation_dispatcher.py:353-459` | — | CAS on `next_run_at` | Implemented; crash window CAS→INSERT |
| 12 | Janitor + startup reaper | `automation_dispatcher.py:578-676` | startup | executions | Implemented; destructive in multi-instance |
| 13 | Retry machinery | `automation_dispatcher.py:494-571` | — | executions | Implemented; in-memory delayed run, dies with process |
| 14 | Executor | `backend/app/services/automation_executor.py` (3,216 lines; entry :1019-1617) | thread from dispatcher | executions, chat_messages, automation_files | Implemented; cooperative timeout/cancel |
| 15 | Agent invocation | `automation_executor.py:2045-2524` (`_run_agent_in_conversation`) | direct call to `routers/agents.add_message_stream` | chat_sessions, agent_conversations | Implemented (real agent runtime, FSM planning forced) |
| 16 | Runtime agent provisioning | `backend/app/services/automation_runtime.py:41-125, 239-308` | — | agent_apps | Implemented (hidden per-(org,app) runtime agent) |
| 17 | Schedule parsing | `backend/app/services/schedule_parser.py` (cron :40-51, NL :138-292, LLM fallback :299-340, next_run_at :464-497) | `/automations/parse-schedule` | — | Implemented; silent +1h fallback on parse failure |
| 18 | Workflow schedule triggers | `backend/app/services/workflow_scheduler.py:67-148` | startup thread | workflow agent schedules | Implemented; **in-memory dedup only — duplicate fires across processes** |
| 19 | cronjob tool | `backend/app/services/tool_handlers/cronjob_tool.py` | agent tool (admin-only) | `tool_artifacts/cronjobs.json` | **Dead feature — jobs never dispatched** (no consumer of the JSON) |
| 20 | Management page | `frontend/src/pages/AutomationTasks.jsx:50-54, 149-173` | generic entity API | `automation_tasks` | Implemented; stale-state + cap + client-side filter issues |
| 21 | Detail page | `frontend/src/pages/AutomationTaskDetail.jsx:72-175` | entity API + toggle + parse-schedule | — | Implemented |
| 22 | New-task wizard | `frontend/src/pages/NewAutomationTask.jsx:46-75` | routes to chat prefill | — | Implemented (chat-anchored, not a form API) |
| 23 | Status panel / bus | `frontend/src/lib/automationStatusBus.js`, `useAutomationExecutionStatus.js`, `AutomationExecutionStatusDrawer.jsx`, `cancelAutomationExecution.js` | 2s polling `/executions/{id}/status`; SSE variant `/executions/{id}/events/stream` | executions | Implemented (polling, not push; dead `timed_out` vocab) |
| 24 | Email delivery | `backend/app/services/notification_gateway/gateway.py:64-122` | — | — | Implemented (flag-gated, never raises) |
| 25 | Artifact generation | `document_generator.generate_document` (called at `automation_executor.py:2616-2645`) | download URL `/automations/files/{id}/download` | `automation_files` | Implemented; **unguarded call — render failure fails the whole run** |
| 26 | Audit | — | — | `audit_events` | **MISSING: zero audit writes in the entire subsystem** |

---

## 4. Chat-to-Automation Lifecycle (Phase 2 trace)

Scenario: *"Create a daily automation that synchronizes ERP sales data into the business database at 08:00, performs incremental updates, detects anomalies, and generates an HTML report."*

| Step | Actual code | Findings |
|---|---|---|
| User message → intent | `Chat.jsx:1463-1471` regex + activeAgent name; `agents.py:3039-3056` server rebind to `automation_agent` | Two overlapping mechanisms (frontend regex AND backend regex); a paraphrase that matches neither falls into generic chat |
| Spec generation | Agent path: LLM emits `create_automation` tool-call (structured args) — `agent_tools.py:1691`. Legacy path: JSON-schema-constrained LLM emits `{text, create_resource:{type,name,fields}}` — `Chat.jsx:3812-3869` | Structured in both paths, but two different schemas owned by two different layers |
| Parameter extraction / validation | Placeholder-UUID defense `agent_tools.py:35-42, 1751-1757`; project guard :1853-1895; schedule→cron at :1732-1739 | Unparseable schedule: agent path silently coerces task to `paused`; entity path raises 400 — **divergent UX for the same input** |
| Clarification | AUTOMATION_AGENT_PROMPT clarify protocol (`agent_prompts.py:1561-1659`) | Prompt-level only; no structured clarification state machine |
| Pending preview | `AutomationDraftCard.jsx` parses prefill **text** bullets; edits in local state; "Send to Agent" appends `- field: value` lines to chat input (`:79-83`) — **no API call** | Draft ≠ task. It is an input aid, not a confirmation UI |
| Confirmation | **None exists.** The task is committed server-side mid-turn via tool-call, before the user ever edits the card | `skip_confirmation` (model :67-69) concerns run-time approvals, not creation |
| Persistence | `agent_tools.py:1996-2024` INSERT (org/app/owner/session stamped) OR `entities.py:377-402` → `entity_service.create_record` | Exactly one INSERT per tool call — but see duplicates below |
| Duplicate prevention | **Absent.** LLM re-calling `create_automation` in one turn → duplicate; legacy auto-retry path creates `created2` (`Chat.jsx:2058`) with no dedupe; "Send to Agent" edit text can trigger create instead of update; no unique constraint on `(created_by_id, name)` | Contrast: `execute_automation` HAS an in-flight guard (`automation_chat_tool.py:59-78`) |
| Scheduler discovery | Dispatcher polls DB every 60s: `SELECT ... status='active' AND next_run_at <= now` (`automation_dispatcher.py:353-360`) | New task is discovered within ≤60s of creation; no registration step needed (poll model) |
| Restart | `next_run_at` persisted on the row; dispatcher fires overdue on next tick (fire-once, no backfill) | Schedules survive restart; missed occurrences do not |
| Browser closed mid-creation | Draft card lost (client state). If the tool-call already committed → task survives. If stream died before tool-call → **nothing exists; silent orphan** | The chat screenshot's "Pending automation" card is not evidence a task exists |
| Status values on creation | `active` if cron parsed else `paused` (agent path :1730-1739); entity path defaults `active` (`entities.py:127-141`) | A paused task never fires; page shows it as paused only |

**Ownership of the automation specification:** the LLM (via tool-call schema server-side; via JSON-schema client-side in the legacy path). There is no server-side canonical spec object, no draft table, no versioned spec.

**When is an automation "created"?** At the moment of the server-side INSERT inside the chat turn — which the user may never have intended to confirm, and which the pending card visually contradicts.

---

## 5. Automation Management Page Data Flow (Phase 3)

Page: `/automation` (`frontend/src/pages/AutomationTasks.jsx`).

- **Fetch:** `base44.entities.AutomationTask.list('-updated_date', 200)` → `GET /api/apps/{app_id}/entities/AutomationTask?sort=-updated_date&limit=200` (`:50-54`). Generic entity router `entities.py:331-374`.
- **Scoping:** `AutomationTask` ∈ `USER_SCOPED_ENTITIES` → list filtered to `created_by_id == caller.id` (admin sees all) + `org_id`/`app_id` tenant wall (`entities.py:198-258, 349-374`; `entity_service.py:440-454`).
- **Project filter:** client-side only, `t.project === selectedProject` (`:149-151`) — no server param.
- **Status filter:** none.
- **Refresh:** `useEffect(() => { load(); }, [])` only (`:50`). The `zhanlu:automation-created` event is consumed by `SessionList.jsx:170-178`, **not** by this page.
- **Screenshot comparison ("No automation tasks" vs. chat showing a created task):** the two screens ARE connected to the same table, but disagreement has five substantiated mechanisms:
  1. **Draft ≠ persisted task** — the chat card can render while nothing was committed (stream died before tool-call).
  2. **Owner mismatch** — a task committed with `created_by_id` unset (service-mode identity) or by another user is invisible to the owner-scoped list, yet remains runnable via org-scoped `/api/automations/*` endpoints.
  3. **Project mismatch** — chat path stamps a legacy `project` **string**; page filters client-side on it. An LLM-echoed wrong project name orphans the task under the selected project (present under "All").
  4. **Stale page** — an open tab never learns about chat-created tasks (no event listener, no polling).
  5. **Hard cap 200** — silently truncates older tasks.
- **Intended lifecycle conclusion:** pending drafts are *intentionally* excluded (they were never persisted). But the page's owner-scoped, string-project-filtered, cap-limited view disagrees with both the write surface and the org-scoped run/toggle API for the same rows.

---

## 6. Scheduling Architecture (Phase 4)

| Concern | Finding | Evidence |
|---|---|---|
| Cron parsing | 5-field cron → regex NL rules → LLM fallback (creation-time only; tick path is rules-only, never LLM) | `schedule_parser.py:40-51, 138-292, 299-340, 404-431`; enforced `automation_dispatcher.py:399-403` |
| Timezone | Per-task IANA tz via `zoneinfo`; stored UTC-naive; invalid tz **silently falls back to UTC** | `schedule_parser.py:448-497` |
| DST | Handled correctly for future fires because `next_run_at` is recomputed at every fire via croniter | `automation_dispatcher.py:405-411` |
| Displayed next-run vs actual fire | **Same code** (`next_run_at`) used by creation preview, `/parse-schedule`, `/recompute`, toggle-resume, and the dispatcher | `entities.py:162-165`; `automation_api.py:152-161, 955-970, 1293-1301`; `automation_dispatcher.py:832-863` |
| Human description | `describe_schedule()` ignores timezone — "Daily at 09:00" without which tz | `schedule_parser.py:500-538` |
| Missed-execution recovery | **None.** Fire-once per occurrence; a 3-day outage fires each task once, late, with a log warning only (`AUTOMATION_MISFIRE_WARN_SECONDS=120`) | `automation_dispatcher.py:353-360, 413-424`; `config.py:2195` |
| Duplicate trigger prevention (same occurrence) | CAS `UPDATE tasks SET next_run_at=real_next WHERE id=? AND next_run_at=<orig>`; loser skips | `automation_dispatcher.py:429-445` — **[CONFIRMED] sound for the same occurrence, and untested** (§13) |
| Multi-instance safety | Dispatcher & WorkflowScheduler: **no lock, no leader election**. Only `start_scheduled_tasks` holds pg advisory lock 380630 (never re-checked; lost on connection recycle). WorkflowScheduler dedup is a process-local set → **duplicate workflow fires across workers** | `automation_dispatcher.py:708-732`; `scheduled_tasks.py:875-905`; `workflow_scheduler.py:74, 112-115` |
| `lease_owner` | Written at claim, **never consulted** — diagnostics only, not a fence | `automation_execution.py:44-47` (code's own comment) |
| Persistence across restart | `next_run_at` on task row → schedules survive; in-memory retry queue does not (recovered via janitor ≥20 min later) | `automation_dispatcher.py:613-617, 508-514` |
| Crash windows | (a) CAS committed, execution INSERT not → occurrence silently lost; (b) startup reap kills other workers' live runs (force_all_active ignores lease_owner) | `automation_dispatcher.py:442 vs 447-459`; `:603-606, 667-676` |
| Schedule modification / pause / resume | `/toggle` validates + recomputes next_run_at on resume; `/recompute` exists | `automation_api.py:1264-1315, 955-970` |
| Backlog starvation | 20 due tasks/tick, 45s tick budget — large overdue backlogs can starve within budget | `automation_dispatcher.py:56, 360, 691-704` **[SUSPECTED]** |
| Silent +1h fallbacks | croniter failure or invalid cron → next_run pushed +1h with only a log warning | `schedule_parser.py:482-483, 495-497`; `automation_dispatcher.py:407-411` |

---

## 7. Execution Runtime Architecture (Phase 5)

**Does automation invoke the normal agent runtime?** Yes — `execute_automation` → `_run_agent_in_conversation` (`automation_executor.py:2045-2524`) directly calls the production v3 chat handler `add_message_stream` (`:2241, 2281-2302`), iterating its SSE stream. `force_planning=True` routes runs through the SynexiaFSM planning pipeline. There is **no separate execution engine** — the executor is an orchestrator around the chat path, run in a worker thread with a fresh event loop per run.

**Concept separation (not merged, verified):**
- *Automation definition* = `automation_tasks` row (schedule, prompt, output format, agent binding).
- *Workflow definition/execution* = workflow engine tables, fired by the separate `workflow_scheduler` — **not** by the automation dispatcher.
- *Agent run* = the chat conversation + SSE turn produced inside the executor; no `AgentRun`/`WorkflowRun` ID is stored on the execution (§8 provenance gap).
- *Automation execution* = `automation_executions` row (claimed, timed, retried).

**Agent/tool selection:** `task.agent_id` if valid+active, else a hidden per-(org,app) `automation_runtime_agent` (`automation_executor.py:725-762`; `automation_runtime.py:239-308`) carrying a parity toolset with admin + automation-CRUD tools forbidden (`automation_runtime.py:74-78`) and `requires_confirmation: False` (`:117-125`).

**Identity:** acting user = task creator (`_resolve_task_user`, `:171-187`); ghost creator ⇒ fail-fast, no retry. Conversation carries task's `org_id/app_id`; project resolved from FK with legacy-name fallback (`:379-441`).

**Unattended permissions:** the run executes **as the creator** against the org's KB credentials; `skip_confirmation=true` auto-approves up to 5 governance pauses via `ApprovalService.approve(reviewed_by="automation:skip_confirmation")` (`:1905-1915, 2406-2430`); `create_agent` decision pauses are never auto-skipped (`:2376-2386`).

**Caps:** 600s wall clock (`config.py:2185`), semaphore 3/process, 20 due tasks/tick, recursion depth 3 (`automation_dispatcher.py:757-786`), output 200KB (`:1439`), 5 auto-approvals.

**Multiple datasources:** `_resolve_bound_data_source_ids` (`:444-476`) merges agent bindings + workspace auto-bind + project KBs + pinned source; preflight connectivity probes (SELECT 1 / HTTP HEAD) fail fast before LLM spend (`:520-602, 1154-1174`).

**Outputs & delivery:** HTML/DOCX rendered to **local filesystem** `<generated_path>/automation/<task>/<exec>/` + `AutomationFile` rows with authenticated download URLs (`:2617-2645`) — no MinIO/S3. Delivery: (1) chat bubble via `_persist_run_to_chat` (`:2760-2930`; `_notify_chat` at `:3061-3133` is **dead code**), (2) email via flag-gated gateway, (3) frontend status panel.

---

## 8. State Machine and Lifecycle (Phase 6)

**AutomationTask** (`automation_task.py:21`): `active | paused | failed | completed` (default `paused`, nullable). Out-of-band: `"deleted"` written by DELETE (`automation_api.py:1339`) — **[CONFIRMED]** outside `VALID_STATUSES` and clashing with the optional Postgres CHECK `ck_automation_tasks_status_valid` (`automation_dispatcher.py:254-277`) → DELETE can 500 on Postgres when the constraint flag is on. NULL/invalid statuses are self-healed to `active` by the dispatcher (`:330-350`).

**AutomationExecution** (`automation_execution.py:22-23`): `queued | running | completed | failed | cancelled` (+ legacy `succeeded` read-only; `skipped` declared terminal but **never written by any code path**; frontend `timed_out` is dead vocabulary — backend writes `failed` with a timeout error).

```
Task:  create ──> active (cron) | paused (no cron)   ⇄ toggle   DELETE ──> "deleted" ⚠ out-of-enum
       NULL/invalid ──self-heal──> active
       failed/completed ── never set automatically (manual toggle only)

Exec:  queued ──CAS──> running ──CAS──> completed | failed (truthfulness gate)
       queued/running ──cancel (CAS)──> cancelled      [cooperative; sub-thread finishes current call]
       queued/running ──janitor/reap──> failed (+retry if attempts remain)
       failed ──schedule_retry──> NEW queued row (attempt+1, backoff 60s→30m, capped by task.max_retries)
```

- **Terminal immutability: yes [CONFIRMED]** — every terminal write is CAS-guarded on non-terminal states (`automation_executor.py:1557-1576, 1627-1632, 684-688`; `automation_dispatcher.py:623-628`; `automation_api.py:1014-1019`).
- **Stuck "running" forever: no [CONFIRMED]** — `timeout_at` + janitor + `reap_on_startup` + three no-timeout fallback branches cover all rows. Frontend corner: the drawer can poll a never-created execution indefinitely (`useAutomationExecutionStatus.js:38-54`; `{exists:false}` is non-terminal) **[SUSPECTED]**.
- **Task marked completed before agent finishes: no** — completion CAS happens after the stream ends and the truthfulness gate passes.
- **Retries as new attempts: yes** — new `queued` row with `attempt+1` and `parent_execution_id` link.
- **Partial results: mostly honest** — partial tool failures ship with a `⚠ N of M tool calls failed` warning and a `tool_outcome` tally (`automation_executor.py:1542-1556, 2653-2669`); all-tools-failed → run failed (truthfulness gate).

**Provenance [CONFIRMED GAPS]:** no `WorkflowRun`/`AgentRun` FK on `AutomationExecution`; `output_data.conversation_id` persisted **only on the success path** (`:1440`); artifact IDs captured in `fsm_meta.artifact_ids` but not persisted on the execution row; `notified_session_id` column has no writer (legacy); SkillRun linkage exists via contextvar (`automation_api.py:529-543`). Observability is **partially linked**: activity steps, phases, tool tallies and cross-run context are excellent; structured run-to-run identity is not.

**Schema governance [CONFIRMED]:** three parallel DDL mechanisms — Alembic (001, 028, 051, 055, 059, 061, 071), `main.py:289-314` hand-maintained raw ALTERs (admits duplication in its own comments), and `_ensure_schema()` (`automation_dispatcher.py:137-297`, introspection for tasks + manual list for executions). Demonstrated divergence: `parent_execution_id` absent from `main.py`'s list; `email_notified_at` absent from `_ensure_schema`'s list; `_to_ddl` cannot create indexes/FKs/defaults. Migration 059's own text admits executions/files were previously created only by `create_all`.

---

## 9. Agent and Workflow Integration (Phases 5–6 synthesis)

- One **dedicated chat session + AgentConversation per task** ("one-chat-per-task", `automation_executor.py:2106-2221`; `automation_sessions.ensure_task_chat_session`), reused across runs — run outputs land in a persistent, inspectable conversation.
- Per-run request marker bubble pre-created, then updated in place by `_persist_run_to_chat` — the user sees each run in the task's chat.
- Cross-run memory: per-(agent, project) hidden ledger conversation accumulating run summaries, trimmed to 50 (`automation_runtime.py:356-436`); `output_data.carried_context_from` records the prior successful execution.
- The **workflow engine** (`workflow_scheduler.py`) is a *parallel* scheduling system for workflow/agent triggers with weaker guarantees (in-memory dedup, no claim) — an independently-evolved sibling, not an integrated layer.
- The **cronjob tool** (`tool_handlers/cronjob_tool.py`) is a third, dead scheduling notion: jobs are persisted to a hardcoded JSON file that nothing consumes.

---

## 10. Datasource and Governance Integration (Phase 8)

| Control | State | Evidence |
|---|---|---|
| Endpoint authentication | All automation endpoints use `get_current_user_required` **except** `POST /parse-schedule` (unauthenticated, pure function) and intentional HMAC-token surfaces (7-day email download, 15-min SSE stream token) | `automation_api.py:144-164, 754-839, 1218-1253` |
| Tenant isolation | `_assert_tenant` org check on task/execution/file endpoints (**[CONFIRMED]** at 15 call sites) | `automation_api.py:56-65` |
| Per-user ownership | **Absent on `/api/automations/*`** — any org member can toggle/delete/cancel/trigger/download another user's task; the entity API is owner-scoped and the chat tool is owner-scoped → three different policies for the same rows | `automation_api.py:56-65` vs `entities.py:226-236` vs `automation_chat_tool.py:36-38` |
| Datasource credentials | Connectors read `kb.username/kb.password` rows directly — **org-shared credentials, no per-user layer** in unattended runs | `services/db/mysql.py:24-29`, `postgres.py:35-40`, `connector_factory.py:80-98` |
| Tool permissions | Runtime agent excludes admin + automation-CRUD tools; parity toolset otherwise matches interactive chat | `automation_runtime.py:41-78` |
| Human approval | Unattended pauses terminal unless `skip_confirmation` (auto-approves ≤5, unaudited); decision-summary pauses protected | `automation_executor.py:1905-1915, 2376-2430` |
| Audit records | **Zero `AuditEvent` writes in the entire automation subsystem** (grep: 0 across api/executor/dispatcher). Only task-delete triggers a generic `resource_deleted` enterprise event | `automation_api.py:1345-1351` |
| Secret exposure | `to_dict()` strips credential fields from API output (`models/base.py:79-83`); artifact URLs authenticated; signed tokens time-boxed | Confirmed |
| Recursion | Nested `execute_automation` capped at depth 3, double-enforced | `automation_dispatcher.py:757-786`; `automation_executor.py:1035-1057` |

**Verdict:** an unattended automation does not gain authority beyond the creator's identity and org's bindings — but `skip_confirmation` converts approval gates into silent auto-approvals, and the absence of audit records means **unattended actions are not attributable after the fact**.

---

## 11. Failure and Recovery Analysis (Phase 7 — 15 scenarios)

| # | Scenario | Current behavior | Handled? | Auto-recovery? | Key risk |
|---|---|---|---|---|---|
| 1 | Same task triggered twice | Scheduled + chat paths CAS/in-flight-guarded; **HTTP "Run Now" unprotected** (`trigger_now` always creates a new execution, no running-check) | Partial | n/a | Two rapid Run-Nows run concurrently in one conversation **[CONFIRMED]** |
| 2 | Worker crash mid-execution | `reap_on_startup` force-reaps all queued/running → failed + retry | Yes | Yes | Destructive in multi-instance (kills live runs of other workers) |
| 3 | Agent timeout | Thread future timeout 600s; row failed + retry; `timeout_at` for janitor | Yes | Yes | **Sub-thread not killed** — keeps burning LLM/DB, side effects land late |
| 4 | Datasource unavailable | Preflight probes fail fast before LLM spend; data_sync without sources blocked | Yes | Retry | Well covered (9 tests) |
| 5 | Tool error | Tally + partial-ship warning; total failure → retryable failed | Yes | Yes | — |
| 6 | LLM malformed spec | Parse fallback validates shape → 4xx at creation; malformed tool output → retryable classification | Yes | Yes | No LLM spec-generation step inside executor |
| 7 | Task edited while executing | **No guard** — executor reads live row; mid-run edits partially apply; fire-CAS only protects schedule edits | No | n/a | **[CONFIRMED] untested** |
| 8 | Task deleted while queued/running | Soft-delete does **not** cancel in-flight; run completes and writes bubbles/files; retry re-checks `is_deleted` | Partial | n/a | Post-delete side effects **[CONFIRMED]** |
| 9 | App restart mid-run | Force-reap → failed + retry | Yes | Yes | Same multi-instance caveat |
| 10 | Redis unavailable | **N/A — automation does not use Redis** | n/a | n/a | In-memory retry queue dies with process (guarded: no loop → no doomed row) |
| 11 | DB transaction fails mid-run | Top-level rollback → re-query → `_mark_failed`; per-tick fire failure pushes next_run +1h | Yes | Retry | Untested |
| 12 | Network interruption | Substring transient classifier → backoff retry | Yes | Yes | Well tested |
| 13 | Child agent orphaned | Intentionally orphaned on timeout; children are independent rows (`parent_execution_id`, depth ≤3); nothing kills live children when parent ends | Partial | n/a | **[CONFIRMED] by design comment** |
| 14 | Artifact generation fails | **Unguarded `generate_document` call** → whole run marked failed; text output lost despite existing | No | Retry | **[CONFIRMED]** |
| 15 | Notification fails | Chat/email both best-effort, never break the run path | Yes | n/a | Well tested |

---

## 12. Security Assessment (Phase 8 condensed)

- **Authorization:** consistent `get_current_user_required` except `parse-schedule` (low risk) and deliberate signed-token surfaces. Org isolation solid at the API layer.
- **Authorization inconsistency:** the same task row has three access policies depending on surface (owner-only entity API / org-wide `/api/automations/*` / owner-only chat tool). Within an org, any member controls any task — acceptable for small teams, a governance hole for enterprises.
- **Unattended authority:** bounded to creator identity + org credentials; admin tools excluded; recursion capped; auto-approvals capped. But auto-approvals are **unaudited**, and there are **no audit events at all** for create/toggle/cancel/run — an enterprise requirement failure independent of exploitability.
- **Secrets:** KB passwords used directly from rows for preflight + execution; stripped from API output. At-rest encryption of KB credentials was already flagged as a P0 in the 2026-10-20 full-system audit (`docs/audits/2026-10-20…`) and remains outside this subsystem's scope.
- **HMAC download links (7-day) and SSE stream tokens (15-min) are time-boxed and tenant-checked** (`test_automation_preview_endpoint.py` covers token tenancy).

---

## 13. Test Coverage Assessment (Phase 9)

**Inventory:** ~35 backend automation test files, ≈250 tests (real temp-file SQLite per `conftest.py` T1 contract), plus 6 frontend files (~32 tests). Strong: cancel semantics (12), truthful-failure gate (16), transient classification, DS preflight (9), schedule normalization at API boundary (11), runtime agent fallback (22), skip-confirmation races (8), notification hooks.

**Critical untested core (ranked):**
1. **The dispatcher's exactly-once CAS** (`_fire`, rowcount race, two workers on the same due row) — zero tests. This is the system's central reliability guarantee.
2. **Janitor + `reap_on_startup`** — zero tests for the zombie machinery the code itself labels the "original P0 bug" fix.
3. **Multi-worker semantics** — nothing tests the docstring's CAS-portability claim; `lease_owner` fencing is never exercised (it doesn't exist).
4. Edit-while-running, delete-while-running, mid-run DB rollback, artifact-render failure — untested, and (per §11) unhandled or unguarded in code.
5. Cron edge cases (DST, `99 * * * *`, croniter-missing) — only API-level coverage; silent +1h fallback untested.
6. Cross-org isolation on list/run/cancel/toggle/download — only the preview-token tenancy test exists.
7. Frontend draft-card lifecycle — 2 format tests only.

**Frontend tests are wrapper-level** (authFetch mocked); none exercise the page→endpoint→DB path.

**Honesty note (per audit rules):** passing unit tests here prove SQLite-level logic only. Nothing proves Postgres CAS behavior under contention, multi-process behavior, or the 60s-tick timing envelope in production.

---

## 14. Confirmed Bugs and Suspected Issues

### Confirmed bugs

| ID | Severity | Bug | Root cause | Evidence |
|---|---|---|---|---|
| **B1** | **P0** | `DELETE /automations/{id}` writes `status="deleted"`, outside `VALID_STATUSES`; under `AUTOMATION_STATUS_CHECK_CONSTRAINT_ENABLED=1` on Postgres, DELETE fails with a constraint violation (500) | Enum drift between API and model/CHECK constraint | `automation_api.py:1339` vs `automation_task.py:21` vs `automation_dispatcher.py:254-277` |
| **B2** | **P0** | No creation idempotency anywhere: LLM re-calling `create_automation`, legacy auto-retry second `create_resource`, or "Send to Agent" text triggering create-vs-update → duplicate tasks | No dedupe, no unique constraint, no confirmation token | `agent_tools.py:1996-2024`; `Chat.jsx:1964-2066, 2058`; `AutomationDraftCard.jsx:79-83` |
| **B3** | **P0** | Workflow scheduler fires duplicates across processes (in-memory dedup set only) | No DB claim/lock in `WorkflowScheduler` | `workflow_scheduler.py:74, 112-115` |
| **B4** | **P0** | `reap_on_startup` force-reaps ALL queued/running executions ignoring `lease_owner` → rolling deploys / second containers mark live runs failed and spawn duplicate retries | Startup reap designed for single-process | `automation_dispatcher.py:603-606, 667-676` |
| **B5** | **P1** | Lost-fire window: crash between fire-CAS commit and execution INSERT silently consumes an occurrence (no backfill) | Two-step fire not atomic | `automation_dispatcher.py:442 vs 447-459` |
| **B6** | **P1** | Timeout/cancel do not kill the agent sub-thread; it keeps spending LLM tokens and its late side effects (chat writes, files) land after the row is terminal | `ThreadPoolExecutor.shutdown(cancel_futures=True)` can't interrupt running threads; cooperative checkpoints only | `automation_executor.py:1263-1300, 1571-1576, 2303-2312` |
| **B7** | **P1** | Artifact render failure fails the entire run (unguarded `generate_document`), losing already-produced text output | No local exception handling | `automation_executor.py:2616-2624, 1602-1612` |
| **B8** | **P1** | Task deletion does not cancel in-flight executions; post-delete runs still write chat bubbles, files, `last_run` | Delete path omits `request_cancel`; executor never re-checks `task.is_deleted` mid-run | `automation_api.py:1318-1359`; `automation_executor.py:1579-1591` |
| **B9** | **P1** | `/automation` page can legitimately hide persisted tasks: owner-scoped entity list + client-side string project filter + 200 cap + no refresh event | List/write surface mismatch | `AutomationTasks.jsx:50-54, 149-151`; `entities.py:226-236` |
| **B10** | **P1** | `POST /api/automations/{id}/run` (Run Now) has no in-flight check → concurrent runs of the same task sharing one conversation | `trigger_now` unconditional | `automation_api.py:878-916`; `automation_dispatcher.py:789-829` |
| **B11** | **P1** | Zero audit events for create/toggle/cancel/run/notification changes | No audit integration in subsystem | grep AuditEvent in `automation_*.py` = 0 |
| **B12** | **P2** | Silent +1h schedule corruption when croniter fails / tz invalid / fire-parse fails | Fallback-by-design with log-only warning | `schedule_parser.py:482-483, 495-497`; `automation_dispatcher.py:407-411` |
| **B13** | **P2** | Divergent unparseable-schedule UX: agent path → silently `paused` task; entity path → 400 | Two creation paths with different validation | `agent_tools.py:1730-1739` vs `entities.py:156-160` |
| **B14** | **P2** | Dead/duplicated code & vocab: `_notify_chat` never called; cronjob tool persists jobs no one dispatches; `skipped` never written; frontend `timed_out` never set; `notified_session_id` never written; `succeeded` legacy-only | Evolution without pruning | `automation_executor.py:3061-3133`; `cronjob_tool.py:30-32`; `automation_api.py:672-680`; `automationStatusBus.js:13-20` |
| **B15** | **P2** | Triple DDL mechanisms with demonstrated divergence (`parent_execution_id`, `email_notified_at`) | Runtime ALTERs as migration substitutes | `main.py:289-314`; `automation_dispatcher.py:137-297` |
| **B16** | **P2** | Bool/int task columns stored as strings (`notify_chat`, `max_retries`, `skip_confirmation`); parse failure silently degrades retries to 0 | Legacy column typing | `automation_task.py:59-69`; `automation_dispatcher.py:82-98` |
| **B17** | **P2** | Task `failed`/`completed` statuses never set automatically (one-shot tasks never complete themselves) | No completion logic | Only `toggle` writes them (`automation_api.py:1280`) |
| **B18** | **P3** | `/automation` page staleness (no `zhanlu:automation-created` listener), `describe_schedule` ignores timezone, unauthenticated `/parse-schedule` | Minor gaps | `AutomationTasks.jsx:50`; `schedule_parser.py:500-538`; `automation_api.py:144-164` |

### Suspected issues

| ID | Severity | Issue | Evidence |
|---|---|---|---|
| S1 | P1 | Retry spawned by janitor reap can overlap the still-alive orphan thread in the same per-task conversation (duplicate side effects) | `automation_dispatcher.py:623-653` + `automation_executor.py:1263-1266` |
| S2 | P2 | Advisory lock for `start_scheduled_tasks` is bound to one pooled connection, never re-checked → silent loss on connection recycle → duplicate maintenance schedulers | `scheduled_tasks.py:887-905` |
| S3 | P2 | >20-task backlog can starve within the 45s tick budget, compounding misfires | `automation_dispatcher.py:56, 360, 691-704` |
| S4 | P2 | Frontend drawer polls forever if `trigger_now` commits fail after API success (non-terminal `pending`) | `useAutomationExecutionStatus.js:38-54`; `automation_api.py:508-523` |
| S5 | P2 | `conversation_id` provenance missing on failure/cancel paths → post-mortem can't locate the conversation | `automation_executor.py:1440` (success-path only) |
| S6 | P3 | Legacy `project` string field diverges from `project_id` FK; adoption is partial | `automation_task.py:24-27`; `automation_executor.py:379-441` |

---

## 15. Root-Cause Analysis

1. **Two creation paths, no single spec owner** (B2, B13): intent/spec/persist logic is split between a frontend regex+JSON-schema path and a backend agent tool-call path, evolved at different times, with different validation and status semantics. The pending card's mental model ("review, then confirm") was never implemented in the persistence layer.
2. **List/write/route surface mismatch** (B9, B16): the generic entity API (owner-scoped, string-typed legacy fields) coexists with a purpose-built REST API (org-scoped) and an agent tool (owner-scoped) over the same rows.
3. **Single-process assumptions in a multi-process-capable deployment** (B3, B4, S1, S2): CAS guards were retrofitted for the hot loop, but dedup/lock/lease machinery is inconsistent across the three schedulers; `lease_owner` was recorded but fencing was never finished.
4. **Cooperative interruption everywhere** (B6, S1): the executor wraps a streaming chat handler in a thread; threads can't be killed, so timeout/cancel are checkpoint-based, leaving orphans as accepted behavior (the code comments say so).
5. **Runtime DDL instead of migrations** (B15, B16): schema evolution was repeatedly patched outside Alembic, producing three sources of truth.
6. **Observability without provenance** (B11, S5): rich per-run telemetry, but no audit trail and no structured run-identity links — you can see *what* happened, not provably *who/which-run* did it.

---

## 16. Architectural Debt

- ~3,200-line executor embedding a full chat agent loop, preflight, rendering, delivery, and lifecycle management in one module.
- Three schedulers, three DDL mechanisms, three access policies — each individually defensible, collectively incoherent.
- String-typed booleans/integers and dual identity fields (`project` vs `project_id`, `agent_id` without FK) as permanent compat layers.
- Dead features shipped in the tool registry (cronjob) and dead code paths (`_notify_chat`, `skipped`, `timed_out`).
- Retry queue split between DB rows and in-memory delayed calls — correctness depends on janitor fallback timing (≥20 min).

## 17. Missing Capabilities

- Server-side draft/confirmation lifecycle (spec object, confirmation token, exactly-once creation).
- Missed-run backfill policy (at-least-once with dedup, or explicit skip ledger).
- True leases/heartbeats or leader election for multi-instance safety.
- Hard cancellation (process-level kill of orphaned run loops).
- Audit events for all automation mutations.
- Per-user credential layer for unattended datasource access (currently org-shared KB rows).
- Automated one-shot completion, and mid-run edit/delete guards (task versioning).
- Structured provenance: AgentRun/WorkflowRun/artifact IDs persisted on the execution row.
- Artifact storage in object storage (currently local FS, lost on container recreation).

## 18. Prioritized Findings Summary

- **P0:** B1 (delete/constraint clash), B2 (duplicate creation), B3 (workflow scheduler duplicate fires), B4 (startup reap kills live runs).
- **P1:** B5–B11, S1 — lost occurrences, unstoppable orphans, whole-run failure on render error, post-delete side effects, page-hides-tasks, Run-Now concurrency, zero audit.
- **P2:** B12–B17, S2–S5 — silent schedule corruption, dead vocab/code, DDL divergence, string typing, provenance gaps.
- **P3:** B18, S6 — UX and minor consistency.

## 19. Evidence-Backed Recommendations (assessment only — no implementation)

1. **Fix B1 immediately** (one-line enum change or stop writing "deleted") — cheapest correctness win.
2. **Add creation idempotency** (unique constraint or creation token) and decide the fate of the legacy `create_resource` path — most likely retire it; it duplicates the agent path with weaker guarantees.
3. **Unify list/read access** behind the purpose-built API (add server-side project/status filters and a created-event refresh); keep the entity API for generic CRUD only.
4. **Finish the fencing story:** either enforce single-instance for the dispatcher (advisory lock like `start_scheduled_tasks`) or implement `lease_owner` checking + heartbeat before scaling out.
5. **Isolate artifact-render failures** from run success; wrap `generate_document`.
6. **Cancel in-flight executions on task delete**; check `task.is_deleted` at delivery time.
7. **Add audit events** at the API boundary (create/toggle/cancel/run/delete/skip_confirmation).
8. **Consolidate DDL into Alembic**; keep `_ensure_schema` as a guarded fallback with a drift report.
9. **Test the core:** dispatcher CAS race, janitor/reaper, and cross-org isolation — the guarantees everything else rests on.
10. **Then** evaluate structural refactoring: single scheduler layer with a persisted job/claim model would subsume dispatcher + workflow_scheduler + cronjob remnants.

## 20. Source Files a Future Redesign Would Touch

Backend: `backend/app/routers/automation_api.py`, `backend/app/services/automation_dispatcher.py`, `backend/app/services/automation_executor.py`, `backend/app/services/automation_runtime.py`, `backend/app/services/automation_sessions.py`, `backend/app/services/automation_chat_tool.py`, `backend/app/services/schedule_parser.py`, `backend/app/services/workflow_scheduler.py`, `backend/app/services/tool_handlers/cronjob_tool.py`, `backend/app/services/agent_tools.py` (`_create_automation`), `backend/app/routers/entities.py` + `entity_service.py` (AutomationTask surface), `backend/app/models/automation_{task,execution,file}.py`, `backend/main.py` (startup wiring :766-802, :289-314), `backend/app/services/notification_gateway/gateway.py`, `document_generator.py` call site.

Frontend: `frontend/src/pages/Chat.jsx` (automation branches :1463-1471, :1850-2066, :3758-3869), `frontend/src/components/chat/AutomationDraftCard.jsx`, `frontend/src/pages/AutomationTasks.jsx`, `AutomationTaskDetail.jsx`, `NewAutomationTask.jsx`, `frontend/src/lib/{runAutomationTask,automationStatusBus,useAutomationExecutionStatus,cancelAutomationExecution}.js`, `frontend/src/components/automation/AutomationExecutionStatusDrawer.jsx`.

---

## Final Architectural Assessment

**A. Fundamentally sound, or structurally fragmented?**
Structurally fragmented at the edges, sound in the core. The fire→claim→execute→finalize pipeline has real exactly-once machinery (CAS), honest failure gates, and recovery semantics. Fragmentation lives in: two creation paths, three access policies, three schedulers, three DDL mechanisms, and dead siblings (cronjob, workflow_scheduler's weaker guarantee) around that core.

**B. Genuine agent-driven capability, or scheduled-task wrapper?**
A genuine agent-driven capability. The executor invokes the production v3 chat agent (`add_message_stream`, FSM planning), with a real toolset, datasource bindings, preflight, and per-task persistent conversations. It is *not* a cron wrapper — but the workflow engine and cronjob tool are the wrapper-shaped siblings that create the confusion.

**C. Is the scheduler reliable enough for enterprise unattended execution?**
For a **single-process deployment: conditionally yes** — CAS-claimed fires, janitor, startup reaping, bounded retries. For enterprise/multi-instance: **no** — duplicate workflow fires are guaranteed, startup reaping is destructive to other workers' live runs, there is no lease fencing, no missed-run backfill, and silent +1h fallbacks corrupt schedules. Exactly-once core (dispatcher CAS) is also completely untested.

**D. Is execution state persistent, recoverable, and observable?**
Persistent and recoverable: yes (DB rows, CAS transitions, janitor + startup reap, retries as new attempts). Observable: partially — rich activity steps, phases, tool tallies, and a polling panel; but no audit trail, no AgentRun/WorkflowRun/artifact provenance on the execution row, and conversation provenance only on success. Not fully consistent.

**E. One coherent lifecycle?**
No. Creation (two paths, client-only draft, no confirmation), management (different access surface), scheduling (in-process poll), execution (chat-handler embedding), and delivery (three channels, one dead code path) each work, but the seams leak: tasks invisible on the page, duplicates, post-delete side effects, and unconfirmed mid-turn commits.

**F. What is duplicated across automation engine, workflow engine, and agent runtime?**
- Scheduling/triggering duplicated between `automation_dispatcher` (60s asyncio, CAS) and `workflow_scheduler` (30s thread, in-memory dedup) — plus a third dead notion in `cronjob_tool`.
- Agent invocation: the executor embeds the chat agent loop rather than sharing an extracted run API — coupling automation reliability to chat-route internals (e.g., `force_planning`).
- Schedule parsing exists in `schedule_parser` for automation while workflow triggers carry their own timing logic.
- DDL/schema maintenance triplicated (Alembic / main.py ALTERs / `_ensure_schema`).
- Delivery/notification logic duplicated between `_persist_run_to_chat` (live) and `_notify_chat` (dead).

**G. Retain, repair, consolidate, or replace?**
- **Retain:** executor's agent invocation model, per-task conversations, truthfulness gate, preflight, retry classification, status CAS discipline, schedule_parser.
- **Repair:** delete/enum clash, idempotent creation, artifact-render isolation, delete-cancels-runs, Run-Now dedupe, audit events, lease fencing.
- **Consolidate:** one scheduler layer with a persisted claim model (absorb workflow_scheduler; delete cronjob tool); one creation path; one access-policy layer; DDL into Alembic.
- **Replace:** the legacy `create_resource` frontend creation path, dead code (`_notify_chat`, `skipped`/`timed_out` vocab), and string-typed legacy columns (via migration).

**H. Option selection.**
**Recommendation: Option 2 — architectural refactoring, preceded by a one-week P0 fix pass (Option 1 scoped to B1–B4).**

Reasoning:
- Option 1 alone is insufficient: the creation-path duplication, access-surface split, and multi-instance gaps are structural; patching them piecemeal has already produced the current three-of-everything landscape.
- Option 3/4 (subsystem replacement or full redesign) are not justified by the evidence: the executor/dispatcher core implements the hard parts (claiming, honesty gates, recovery) with reasonable quality and ~250 tests around much of it. Replacing it would discard working reliability machinery and re-expose the exact classes of bugs this audit found.
- Option 2 targets precisely the fragmentation: single creation path, single access policy, single scheduler with persisted claims and fencing, provenance + audit on the execution row, DDL consolidation. The audit-verified component inventory in §3 and §20 defines the refactoring blast radius.

---

*End of report. No implementation was performed; working tree untouched.*
