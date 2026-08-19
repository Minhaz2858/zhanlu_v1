# Goal-Contract Architecture — Implementation & Migration Note (2026-08-19)

## Goal

Make every system agent behave like "Hermes": loop, re-plan, refine until the
user's goal is achieved. The v3 agent loop previously exited when the model
stopped emitting tool calls — not when the goal was met. Six failure modes all
shared that root cause: premature "let me…" text stops, missing deliverables,
deliverables built from empty data, zero-row text-filter dead ends, greetings
triggering 223s data runs, and the "Dashbord" typo blinding four separate
dashboard regexes.

## Solution

A machine-checkable **Goal Contract** is built once per turn, updated at
runtime from tool results, and checked before any loop exit. Unmet criteria
trigger forced remediation steps, bounded by `GOAL_CONTRACT_MAX_FORCES`.

New module: `backend/app/services/goal_contract.py` (338 lines)
- `GoalContract` dataclass: `deliverable`, `requires_data`, `expects_rows`,
  `text_filters_used`, `zero_row_events`, `artifacts_produced`,
  `pending_action_phrase`, `forces_used`, `max_forces`, `catalog_meta`,
  `distinct_executor`, `_announced_executed`
- `build_goal_contract()` — pure string matching, no I/O
- `normalize_deliverable_intent()` — single typo-tolerant normalizer
  (`dashbo` catches dashboard / Dashbord / dahsboard / 看板 / 数据看板 /
  仪表盘 / 仪表板 / 数据面板 / 大屏) — the **one source of truth**
- Three remediation paths in `unmet()`:
  (a) force artifact/dashboard tool when deliverable unproduced
  (b) zero-rows-with-text-filters → re-query including actual distinct values
      (catalog `value_samples` first, live `SELECT DISTINCT` fallback, cap 50)
  (c) force the announced tool class for unexecuted "let me…" promises
- Artifact content gate: only an artifact of the requested kind built from a
  non-empty, non-failed payload satisfies; report cards never satisfy explicit
  doc-format deliverables

## Flags (all default OFF → flag-off = bit-for-bit current behavior)

| Flag | Default | Location |
|---|---|---|
| `GOAL_CONTRACT_ENABLED` | `False` | `config.py:451` |
| `GOAL_CONTRACT_MAX_FORCES` | `3` | `config.py:452` |

Documented in `.env.example:152-158`.

## Redirected / wired paths (file:line before → after)

| Path | Before | After |
|---|---|---|
| `agents.py` import `build_goal_contract` | — | `agents.py:131` |
| `agents.py` `_is_dashboard_request` | 339-349 (kw list + `fuzzy_dashboard_request`) | 347-350 (flag-gated delegation to `normalize_deliverable_intent`) |
| `agents.py` v3 turn-start contract build | 7450-7454 (no contract) | 7455-7467 (`build_goal_contract` in try/except, `_contract_force_tool=None`) |
| `agents.py` soft-cap break | 7452-7459 (break on dynamic cap) | 7480-7500 (`continue` when flag on and `_contract.unmet(...)` non-empty; hard cap `MAX_TOOL_ITERATIONS` still bounds) |
| `agents.py` force machinery | 7601-7624 (one-shot `_pptx_force_next_iteration`) | 7665-7694 (generic `_contract_force_tool` → `tool_choice`; `_contract_forced` feeds `_finish_line_tool_choice`; consumed after one use) |
| `agents.py` exit checker (no-tool-calls branch) | 7835 (guardrail first) | 7917-7924 (contract check FIRST; unmet + force → record_force, remediation message, `continue`) |
| `agents.py` runtime updates | 8520-8525 (no updates) | 8536-8565 (`record_tool_executed`, `record_query_result` rows+sql, `record_artifact` for artifact/dashboard/dashboard_app successes) |
| `dashboard_turn_guard.py` `fuzzy_dashboard_request` | 40-56 (consonant skeleton, gated by `DASHBOARD_FUZZY_MATCH_ENABLED`) | 59-63 (delegates to normalizer when flag on; legacy fallback otherwise) |
| `pptx_turn_guard.py` `is_pptx_request` | 40-50 (`detect_file_intent(...) == "pptx"`) | 57-61 (delegates to normalizer when flag on; legacy fallback otherwise) |
| `synexia/intent_router.py` `detect_file_intent` | 44-48 (`_COMPILED` priority loop) | 88-94 (normalizer first when flag on; legacy `_COMPILED` loop fallback) |
| `generation_orchestrator.py` content gate | 685-734 (`already_created` blanket preemption; strict mode = type match only) | 107-126 (`_artifact_satisfies_deliverable` helper), 742-780 (`already_created` same-kind+success check; `artifact_ids` strict mode adds non-empty/non-failed payload check when flag on; legacy type-match kept for flag-off) |
| `turn_action.py` | 175/254 (`LIVE_DASHBOARD_PATTERN` + `fuzzy_dashboard_request`) | **no change needed** — both detectors delegate transitively to the normalizer when the flag is on |

`goal_contract.py` `_DASHBOARD_RE` extended with `数据面板|数据看板|仪表板`
to keep flag-on a superset of the legacy Chinese variants.

## Greeting / empty contract

Reuses the existing chitchat signals (intent_classifier CHITCHAT,
`_is_greeting_only`) so a greeting builds an empty contract → `satisfied()`
trivially true → zero forcing → immediate text answer. No 223s data run.

## Hard constraints honored

- Agent-agnostic, database-agnostic (schema specifics come from catalog/kb
  metadata at runtime; SQL literal extraction is generic regex).
- No new vendor dependencies (reuses existing `_pptx_force_next_iteration`/
  `should_force_create_pptx` tool_choice machinery, SSE events, artifact
  pipeline, catalog `value_samples`).
- Tool-presence rule preserved: never force a tool the agent wasn't granted.
- Out of scope (unchanged): NL2SQL prompt wording, measure semantics,
  July totals reconciliation, SSE hardening, frontend.

## Verification evidence (exact counts)

Run with `backend/venv/bin/python -m pytest`:

- `tests/test_goal_contract.py` — **56 passed** (builder per-deliverable +
  typos, greeting, plain data question, pending-action match/reject,
  text-filter extraction, distinct-values catalog/fallback/cap, unmet
  deliverable + content gate + force-cap + budget + announced-tool clearing)
- Regression batch 1: `test_pptx_turn_guard.py`, `test_dashboard_fuzzy_match.py`,
  `services/dashboard_app/test_dashboard_turn_guard.py`, `test_turn_action.py`
  — **91 passed**
- Regression batch 2: `test_synexia_file_intent.py`,
  `test_generation_orchestrator.py`, `test_dashboard_create_guard.py`,
  `test_synexia_multi_agent_fsm.py` — **86 passed**
- **Total: 233 passed, 0 failed** — all with flags default OFF, proving
  flag-off parity.

## Rollout

1. Enable `GOAL_CONTRACT_ENABLED=True` (+ optional `GOAL_CONTRACT_MAX_FORCES=3`)
   in `.env`, then `docker compose up -d --force-recreate backend`.
2. Run the six acceptance criteria live from the frontend with one test agent
   (premature stop, missing deliverable, empty-data deliverable, zero-row
   dead end, greeting, "Dashbord" typo).
3. Enable globally once green.
