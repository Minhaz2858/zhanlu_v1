# Phase 9 — Zhanlu Enterprise Reliability & End-to-End Acceptance Audit

**Date:** 2026-11-03 · **Branch:** `fix/automation-chat-artifacts-ordering` · **Base:** `7865c9662` (Phase 8.2)

Scope: reliability and acceptance of EXISTING capabilities. Phase 8.3 Automation is
handled separately and was not touched.

## 1. Method

1. Recovered the two prior deep audits (full-system audit vs DeepSeek Harness
   2026-10-20; execution-consistency audit 2026-10-31) as the candidate-defect list.
2. Re-verified every P0 on current HEAD (subagent verification with file:line
   evidence) — several had already been fixed by intervening phases.
3. Established a reproducible full-backend-sweep baseline — which itself exposed
   two test-infrastructure P0s that had made the full sweep meaningless
   (~12.8k collection/setup errors).
4. Fixed confirmed defects TDD-style (RED → GREEN), scoped away from foreign WIP
   (249 dirty files preserved; surgical staging used for every commit).

## 2. Verified status of the 2026-10-20 P0 list

| # | Finding (old audit) | Verdict on HEAD |
|---|---------------------|-----------------|
| B1 | FSM NL2SQL schema-only fallback reports success for never-executed SQL | **FIXED** (`capability_router.py:1178-1214`, honesty guard 2026-10-21) |
| A2 | `ask_data_agent` lacks the warehouse preflight probe | **FIXED** (`delegation_tools.py:634-658`, probe shared with `query_database`) |
| A3 | `delegate_task` has no wall-clock timeout | **FIXED** (`delegate_tool.py:798-810`, `asyncio.wait_for` + hard ceiling) |
| D4 | FSM dual-persist duplicates each chat turn row | **FIXED** (`agents.py:1030-1114`, id-adoption UPDATE-in-place) |
| C1 | Frontend silent EOF persists empty assistant row | **FIXED** (90s read watchdog in `agentEnhanced.js`; `Chat.jsx` blocks empty writes) |
| E1 | Datasource credentials plaintext at rest + in API responses | **FIXED** (encrypt-on-write property in `models/knowledge_base.py`; `to_dict()` pops credential columns) |
| A1 | Caller's SQLAlchemy Session shared across worker threads in every parallel tool path | **STILL PRESENT → FIXED this phase** (see 3.1) |
| D3 | Automation "fake success" — zero-data-call runs complete with fabricated reports | **PARTIAL on HEAD → FIXED this phase** (see 3.2) |
| D1/D2 | Cross-tenant skills directory; sandbox host fallback | NOT re-verified this phase (security track, Phase 8.x covered MCP; sandbox hardening tracked separately) |
| E2 | Entity API tenant filter hardcoded | NOT re-verified this phase (7C workspace authz work covers parts) |

`QUERY_TIMEOUT_DISABLED=True` (`config.py:545`) remains a **documented, deliberate
decision** (timeout-removal phase) with compensations (preflight probe, circuit
breaker, wall-clock ceilings); recorded as accepted-risk, not a defect.

## 3. Confirmed defects fixed this phase (TDD)

### 3.1 A1 — Session shared across worker threads (P0)

`agent_tools._execute_with_reliability` dispatched every SYNC handler via
`asyncio.to_thread(handler, args, db, user_id)` — the caller's single Session
handed to worker threads while parallel batches run several dispatches
concurrently. Intermittent `InvalidRequestError`, misclassified as retryable.

**Fix:** sync handlers now run on their OWN per-call `SessionLocal()` (closed in
`finally`, failure path included). Contract note: tools run against committed
data — the production contract; the caller's session is no longer used from
threads at all.

**Tests:** `tests/test_tool_dispatch_session_isolation.py` (4) — fresh session
not the caller's; distinct sessions per concurrent handler; closed on failure;
registry-level integration.

### 3.2 D3 — Ungrounded automation reports (P0)

A data-expected automation run whose datasource calls ALL failed — or that made
zero data calls — could still end `completed` with a substantive fabricated
report (the historical ERP incident). The generic tool-failure gates on HEAD
count tools, not datasource grounding.

**Fix:**
- `_summarize_tool_outcomes` now tallies `data_calls` / `data_failures` — a
  payload carrying executed-query evidence (`rows` key or `executed: true`);
  schema/metadata tools excluded (they describe capability, not figures).
- New `_should_fail_for_ungrounded_report`: a data-expected run (task pins
  `data_source_id`, or the run itself attempted data tools) producing a
  substantive report with zero successful data calls is marked **failed
  (retryable)** at BOTH status points (post-run gate + final CAS). Honest
  failure explanations pass — they describe what happened instead of
  inventing numbers.

**Tests:** `tests/test_automation_ungrounded_report_gate.py` (13).

### 3.3 Test infrastructure — the full sweep was broken (P0 for "reproducible baseline")

Three defects made `pytest tests/` produce ~12.8k errors, i.e. NO usable
regression baseline:

1. `tests/test_functions_artifact_registration.py` overwrote
   `os.environ["DATABASE_URL"]` at module-import (collection) time and never
   restored it → T1 isolation guard tripped for the whole sweep.
   **Fixed:** rely on the T1 file-backed DB (the old in-memory rationale is
   obsolete); obsolete cleanup removed.
2. `tests/test_dashboard_binding.py` leaked the same env var at test time.
   **Fixed:** save/restore in `finally`.
3. Models registered lazily by mid-sweep modules (e.g. `automation_file`)
   appeared in `Base.metadata` only AFTER the session-scoped bootstrap ran
   `create_all` → `_reset_database` truncated a nonexistent table →
   `no such table: automation_files` × 12,224 setup errors.
   **Fixed:** `_reset_database` runs idempotent `create_all` before truncating.
4. `tests/migrations/test_020` used an undefined `engine` fixture (17 errors).
   **Fixed:** fixture added.

## 4. Confirmed findings deferred (foreign-WIP collision)

| Finding | Severity | Why deferred |
|---------|----------|--------------|
| Migration 020 drops the legacy `project` string column from 5 tables, but models still declare it (`AgentApp.project`, `KnowledgeBase.project`, …) and untracked foreign-WIP service `project_kb_membership.py` actively queries `KnowledgeBase.project`. On a fully-migrated DB those queries break. | P1 | Fix = model surgery on files with active foreign WIP (`models/agent_app.py` modified, `project_kb_membership.py` untracked). Owner coordination required; `tests/migrations/test_020_*` (31 failures) document the drift precisely. |
| `test_functions_artifact_registration` — 6 tests pin an artifact-registration contract the legacy doc endpoints do not implement on this branch | P2 (missing feature or misplaced tests) | Pre-existing at HEAD; feature work, not reliability |
| `test_query_database_scope_inheritance` hint-text drift (2 tests) | P2 | Foreign uncommitted WIP in `query_database_tool.py` changed the hint contract; preserved untouched |

## 5. Open items intentionally NOT started here

- Sandbox hardening (host fallback, pids-limit) — security track.
- Skills directory tenancy — security track.
- Report-card template vocabulary (cosmetic) — tracked from the 2026-10-20 audit.
- `nl2sql_query_logs.row_count/duration_ms` population — observability debt.

## 6. Baseline numbers

Filled in the completion report after the Stage-5 sweep (see
`docs/plans/2026-11-03-phase-9-completion-report.md`).

## 7. Closure (2026-11-03, same day)

Phase 9 CLOSED at HEAD `2acd748fb`. The full disposition table (including
the full-sweep order-dependence finding and the migration-020 drift status)
lives in the closure addendum of
`docs/plans/2026-11-03-phase-9-completion-report.md`. Summary: both P0
fixes re-verified green (22 tests), zero Phase 9 regressions (matrix
299/307, all 8 failures stash-verified pre-existing; document-family
cluster suites pass standalone), full sweep honestly reported as in-flight
with an order-dependence limitation deferred to the test-infra track.
