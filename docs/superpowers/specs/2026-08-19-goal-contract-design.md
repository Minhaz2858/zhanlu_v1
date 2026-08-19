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

---

# Follow-up: Closed-Loop Remediation (same day, 2026-08-19)

Trigger: the 2026-08-19 sales-overview automation turn dead-ended at an empty
card. Diagnosis: the contract was documented-but-dormant, AND a
header-only/all-null/all-zero query snapshot wrongly reset the zero-row
counter, so a "zero usable rows" dead end never forced a re-query.

## Changes (all flag-gated, flags now ON in `.env`)

| Flag | Value in `.env` | Meaning |
|---|---|---|
| `GOAL_CONTRACT_ENABLED` | `true` | contract active in the v3 loop |
| `GOAL_CONTRACT_MAX_FORCES` | `3` | remediation bound per turn |

`backend/app/services/goal_contract.py`:
- `is_effective_empty(rows)` — pure helper: empty/None rows OR every row has
  only None/""/numeric-0/zero-string values → counts as a zero-row event.
  ANY non-empty value → NOT empty (conservative).
- `extract_tables_from_sql(sql)` — regex `FROM|JOIN` table extraction
  (schema-qualified + quoted identifiers reduced to bare names).
- `catalog_oracle_feedback(filters, tables, catalog_meta, distinct_executor,
  table_executor, cap=50)` — distinct-value hints (existing) PLUS, only when
  no text filters, per-table live-coverage lines via `table_executor`.
- `GoalContract` gains `candidate_tables: list[str]` and
  `table_executor: Callable[[str], str] | None`.
- `record_query_result(rows, sql)` — records candidate tables from the SQL and
  uses `is_effective_empty`: zero-row **or** all-null/all-zero snapshot
  increments `zero_row_events` (previously only literal empty rows did); any
  real signal resets it.
- `_unmet_zero_rows(tools)` — fires on `requires_data and expects_rows and
  zero_row_events` (the `text_filters_used` requirement is **removed**): the
  remediation now also covers no-filter queries that returned only null/zero
  snapshots. Message includes `catalog_oracle_feedback`; force tool is
  `execute_query`.
- `refresh_pending_action(assistant_text)` — at loop exit, re-evaluates the
  pending-action ("let me…") marker from assistant prose; only a live
  first-person-future announcement overrides it (past-tense text leaves it
  untouched, so an announced-but-unexecuted tool still gets forced).
- `build_goal_contract(...)` gains `table_executor` and forwards it.

`backend/app/routers/agents.py`:
- New `_make_goal_contract_table_executor(db, kb_ids)` — memoized live probe
  `SELECT COUNT(*) FROM <table>` via `QueryService` against the bound KB;
  only wired when **exactly one** bound KB exists (ambiguous multi-KB turns
  degrade to static hints). Table names must match a plain-identifier regex
  (injection-safe), best-effort with short timeout, failures → "".
- Contract build call site passes `table_executor=_make_goal_contract_table_executor(db, _bound_kb_ids)`.
- No-tool-calls exit branch calls `_contract.refresh_pending_action(assistant_content)`
  BEFORE `_contract.unmet(...)` so the "announce but don't execute" pattern is
  caught at exit.

## Tests (71 in `tests/test_goal_contract.py`, all passing)

Added/replaced:
- `is_effective_empty` (no rows / all-null / all-zero / partial signal)
- `extract_tables_from_sql` (FROM, JOIN, None)
- `record_query_result` candidate-table recording + all-null-numeric zero-row
- `catalog_oracle_feedback` (filters+tables combined, degrades without executors)
- zero-rows WITHOUT filters now forces re-query (replaced the old
  "no force without filters" test — behavior intentionally changed)
- zero-rows + no filters + table-coverage → forces re-query
- pending-action: live announcement match, re-evaluation from assistant text,
  past-tense ignored
- `_make_goal_contract_table_executor`: None on 0/>1 KBs, COUNT probe +
  memoization, injection-safe identifier rejection, graceful failure

Plus pre-existing regression suites:
- `test_goal_contract.py` — **75 passed**
- Batch: `test_agents_responder_bug.py`, `test_runtime_context_in_all_agents.py`,
  `test_system_agents_is_system_flag.py` (+ JSONB-on-SQLite compile recipe
  added to the last — pre-existing harness gap unrelated to goal-contract),
  `test_persist_run_to_chat.py` — **95 passed**

Known pre-existing failure (NOT caused by this work, verified via `git stash`):
`test_agents_token_streaming.py::test_stream_retries_without_tools_when_provider_rejects_tools`
— the test expects a 400 on a tool-bearing payload to retry with `tools=None`,
but `_stream_llm_with_tools` only retries by popping `tool_choice`. Separate
follow-up.

## Addendum: Four degradation-mode fixes (2026-08-19)

Observed in a live goal-contract-enabled turn (conv 86d8861c, 718.5s): a PPT
with real data was built but degraded by phantom-dimension flags, artifact
contamination from superseded queries, card bias toward the last tool result,
and an unclean exit with unexecuted promises.

### Fix 1 — Phantom-dimension verification loop
- `backend/app/services/answer_verification.py`:
  - Added `_VERB_STOPWORDS`, `_FORMAT_STOPWORDS`, `_TIME_STOPWORDS` frozensets
    next to `_ASPECT_STOPWORDS`/`_CONTAINER_STOPWORDS`; included in
    `_detect_dimension_coverage`'s token filter (so "make a sales overview PPT
    for last month" extracts zero phantom dimensions).
  - `_detect_dimension_coverage(user_message, results, assistant_text, *,
    catalog_meta=None)`: when `catalog_meta` is provided, a `missing` token is
    kept only if it maps to a real catalog column (case-insensitive) or appears
    in a column's `value_samples`; unmappable tokens are dropped. Plumbed from
    `evaluate_answer` → `_evaluate_answer_inner` → `_check_answer_verification_gate`
    in `agents.py` (`data_ctx_extras`).
  - `_is_rebutted(missing_tok, assistant_text)`: suppression-only rebuttal
    regex (`not in the catalog|no such dimension|doesn't exist|...`) within
    ±40 chars of the token; applied to `missing` before flag emission.
- `backend/app/config.py`: `VERIFY_NUDGE_MAX: int = 2`.
- `backend/app/services/verification_stop.py`: `_DEFAULT_MAX_ATTEMPTS` replaced
  with `settings.VERIFY_NUDGE_MAX`.
- `backend/app/routers/agents.py`: verify-nudge call site now respects the
  **shared force budget** — a nudge is issued only while
  `_contract.forces_used < min(VERIFY_NUDGE_MAX, GOAL_CONTRACT_MAX_FORCES)`,
  then `_contract.record_force()`.

### Fix 2 — Artifact context contamination (superseded marker)
- `backend/app/routers/agents.py`: after each `ask_data_agent` result is
  processed, prior same-KB results that are empty/error (or were followed by
  retry hints `retry|re-query|重试|重新|再查|rerun` in assistant text) get
  `"__superseded": True` on the `tool_calls_for_frontend` entry.
- `backend/app/services/generation_orchestrator.py`:
  - `_mine_ask_data_rows` / `_mine_ask_data_result` gained
    `skip_superseded: bool = True` and skip `__superseded` entries.
  - Fallback payload's `methodology` now cites only the final non-superseded
    source (source name + clipped SQL + row count), never a merged list.

### Fix 3 — Final-card last-tool-result bias
- `_mine_ask_data_rows(tool_calls_for_frontend, skip_superseded=True)` at the
  L890 fallback so the closing card synthesizes from the deliverable's verified
  payload, not the last drill-down tool result; superseded drill-downs excluded.

### Fix 4 — Clean exit on budget exhaustion
- `backend/app/routers/agents.py`:
  - `_strip_trailing_pending(text, pending_sentence)` helper strips the trailing
    sentence containing a pending-action phrase (mirrors
    `goal_contract.pending_action_phrase` sentence split).
  - In the no-tool-calls exit branch, when
    `_contract.forces_used >= _contract.max_forces` and
    `pending_action_phrase(assistant_content)` matches, the trailing pending
    sentence is stripped and an explicit limitation note is appended
    ("this turn ended before completing the additional verification described
    above…"), so the closing text never promises work the budget can no longer
    perform.

### Tests (new, all passing)
- `tests/test_answer_verification_stopwords.py` — zero phantom flags; verbs/formats
  never extracted; "by region" with catalog still flags.
- `tests/test_answer_verification_catalog_gate.py` — unmappable suppressed;
  mappable kept; no catalog → legacy.
- `tests/test_answer_verification_rebuttal.py` — rebuttal suppresses; partial
  rebuttal only suppresses rebutted tokens.
- `tests/test_verification_stop_budget.py` — `VERIFY_NUDGE_MAX` cap + shared
  budget with `GOAL_CONTRACT_MAX_FORCES`; `record_force` increments.
- `tests/test_generation_orchestrator_superseded.py` — superseded filtered;
  methodology cites final table only.
- `tests/test_generation_orchestrator_card_bias.py` — card numbers match
  artifact, not last drill-down; superseded drill-down excluded.
- `tests/test_agents_clean_exit.py` — budget-exhausted + pending-action →
  stripped + limitation note; budget not exhausted → untouched.
- 3 legacy skip-tests in `tests/test_generation_orchestrator.py` pinned
  `GOAL_CONTRACT_ENABLED=False` (they asserted legacy flag-off behavior).
- Targeted suite: **199 passed**.

## Notes for future work

- True video transcription requires `ffmpeg` in Docker (extract audio track →
  Whisper). Add a follow-up plan.
- Vision-tool-based image description (`vision_tool.py`) is agent-driven; could
  auto-describe at ingestion when `IMAGE_OCR_ENABLED=False` + vision LLM.
- The `test_agents_token_streaming.py` tools-retry test/implementation mismatch
  above.
