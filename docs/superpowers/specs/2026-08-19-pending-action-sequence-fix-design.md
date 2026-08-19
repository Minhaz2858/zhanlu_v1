# Pending-Action Sequence Fix — Design (2026-08-19)

## Goal

Stop the v3 agent loop from ending a turn with an unexecuted promise as the
final answer. Reproduced live: user asked "Give me supply chain data for last
30 days"; the agent ran `ask_data_agent` twice (23.8s stale-table query, then
a 115.8s live-table re-query against `erp_t_sal_outstock` /
`erp_t_stk_inventory`), then closed the turn with the mid-loop narration
"The first attempt used the stale, low-volume erp_product_sales_details
table… Let me re-query against the live, current tables…" as the final
assistant message — a promise, not data.

## Root cause (4 layers)

1. **Model emitted announce-but-don't-execute prose at the synthesis
   iteration.** The loop did reach an LLM call after query #2 (all early
   exits ruled out: `conv_budget`=30 for `general_agent`, dynamic soft cap=6
   via `calculate_agent_budget` + "supply chain" keyword, tool-loop guard=10
   calls, guardrail halt=5 identical calls). The model repeated its mid-loop
   promise with no tool call — plausibly because query #2's result was
   Layer-2 persisted/truncated (`persist_result_str`,
   `TOOL_MAX_OUTPUT_CHARS=8000`), so it barely saw the data.

2. **ROOT BUG: the GoalContract pending-action catcher is turn-scoped, not
   announcement-scoped.** `record_tool_executed()`
   (`goal_contract.py:374-378`) sets `_announced_executed=True` after ANY
   query tool returns usable rows, unconditionally. At the exit branch,
   `refresh_pending_action()` (`agents.py:8170`) re-arms the phrase from the
   final prose ("Let me re-query…" matches `_PENDING_RE`) but does NOT reset
   the flip. `_unmet_pending_action()` (`goal_contract.py:496`) then returns
   `[]` because `_announced_executed` is still True from the EARLIER query —
   even though nothing executed after the promise made in the final answer.

3. **The self-eval gate is advisory and exhaustible.**
   `SELF_EVAL_REPLAN_ENABLED=true`, `SELF_EVAL_MAX_REPLANS=3`,
   `SELF_EVAL_LLM_GATE_TIMEOUT_S=15.0`. Gate nudges are plain user-message
   text — a deflecting model can burn all 3 replans in prose (invisible in
   the step UI; consistent with ~115s of unaccounted LLM time in the 255.3s
   turn).

4. **No final-answer quality floor.** Post-loop only checks
   `assistant_content.strip()` non-empty (`agents.py:9263`). A one-sentence
   promise passes.

Cosmetic: the "Generating response" activity-step duration copies the
previous step's start time (`agents.py:9305`), making a 0ms step display
115.8s.

## Solution (Approach B — approved)

Three changes in existing modules + one cosmetic fix. No new flags, no new
services. Everything rides the existing goal-contract force machinery and
the self-eval gate. Flag-off = bit-for-bit legacy.

### Component 1 — Announcement-scoped tracking (`goal_contract.py`)

Replace the boolean flip with sequence stamps:

- New fields (internal, `repr=False`): `_seq: int = 0`,
  `_armed_seq: int = 0`, `_executed_seq: int = 0`,
  `_armed_by: Optional[str] = None` (`"user"` | `"model"`),
  `_usable_results: int = 0`.
- Removed fields: `_announced_executed`, `_last_query_quality`.
- `record_tool_executed(tool_name, result_quality)`: on a query/artifact/
  dashboard tool with quality != `RESULT_QUALITY_NO_DATA` →
  `_executed_seq = ++_seq`, `_usable_results += 1`. `NO_DATA` → no stamp
  (quality rule preserved: a dead query does not fulfill an announcement).
- `refresh_pending_action(assistant_text)`:
  - new/different phrase → arm, `_armed_seq = ++_seq`, `_armed_by = "model"`
  - same phrase → no re-stamp
  - **no phrase in final prose + armed by model → disarm** (clear
    `pending_action_phrase`, `_armed_by = None`): the model chose to
    deliver; prevents force loops after a successful forced synthesis.
  - user-armed phrases (set by `build_goal_contract` from user text, e.g.
    "I'd like to…") are never disarmed by prose — only execution satisfies
    them. `build_goal_contract` stamps `_armed_by = "user"` when it sets the
    initial phrase.
- `_unmet_pending_action()`: fires iff phrase armed AND
  `_armed_seq > _executed_seq`.
- **Remediation type selection**: usable data exists this turn
  (`_usable_results > 0`) →
  `UnmetCriterion("pending_action", msg, force_tool=None, force_synthesis=True)`
  with message: "You already retrieved the data — write the final answer
  now using it. Do not announce future actions." Otherwise the existing
  query-tool force is used (`_pick_query_tool`, unchanged message).

### Component 2 — Forced-synthesis consumption (`agents.py` v3 loop)

- `UnmetCriterion` gains `force_synthesis: bool = False`
  (`goal_contract.py:308-313`).
- Exit checker (`agents.py:8191-8203`): when `_gc_crit.force_synthesis` →
  arm `_contract_force_synthesis = True` (new turn-scoped flag initialized
  next to `_contract_force_tool = None` at `agents.py:7705`) instead of
  `_contract_force_tool`; otherwise unchanged.
- Pre-LLM force consumption (`agents.py:7924-7940`, after the
  `_contract_force_tool` block): if `_contract_force_synthesis` →
  `tool_choice = "none"`, `_contract_forced = True`, pop the flag
  (consume-at-top, mirroring the `_contract_force_tool` pattern and the
  pptx-guard `_pptx_force_next_iteration` pattern at `agents.py:7898-7916`).
  Finish-line guard `_finish_line_tool_choice` receives
  `_contract_forced` as today.
- Budget: each force calls `record_force()` (already in the exit checker);
  when `forces_used >= max_forces`, `unmet()` returns `[]` and the existing
  Fix-4 clean-exit (strip trailing pending sentence + limitation note,
  `agents.py:8179-8189`) closes the turn honestly.

### Component 3 — Deterministic pending-promise detector (`answer_verification.py`)

`_detect_pending_action_promise(assistant_text) -> bool`: returns True when
`goal_contract.pending_action_phrase(assistant_text)` is not None. Wired
into `_evaluate_answer_inner` (`answer_verification.py:756-830`) alongside
the existing detectors:

```python
if _detect_pending_action_promise(assistant_text):
    signals.append("pending_action")
    gaps.append(
        "the draft answer announces a future action instead of answering "
        "with the retrieved data"
    )
```

Deterministic detectors run before and short-circuit the LLM evaluator
(`answer_verification.py:619-622`), so a promise draft = instant INCOMPLETE,
no 15s LLM call. This works even with `GOAL_CONTRACT_ENABLED=false`
(defense for flag-off deployments; gated only by the existing
`SELF_EVAL_REPLAN_ENABLED`).

### Component 4 — Step-duration cosmetic (`agents.py:9305`)

Record `_loop_exit_monotonic = time.monotonic()` immediately after the loop
(exit-branch `break` paths set it; initialized `None` before the loop).
"Generating response" duration =
`int((time.monotonic() - _loop_exit_monotonic) * 1000)` when set, else omit
`duration_ms`. No behavior change beyond the displayed number.

### Detector tightening (`goal_contract.py:46-50`)

New `_PENDING_OFFER_RE` exclusion applied inside `pending_action_phrase`
before returning a match: `let me know` / `feel free to` /
`don't hesitate to`. Required because the flip no longer masks these
non-action offers — without the exclusion, closing lines like "Let me know
if you'd like the breakdown by customer" would spuriously force
remediation.

## Edge cases

- Forced-synthesis answer still a promise → refresh re-arms → fires again
  (budget-capped by `GOAL_CONTRACT_MAX_FORCES`, default 3) → Fix-4 honest
  close.
- User-armed + zero tools + clean prose → still fires (forces a query) —
  preserves the original "promise with zero execution" case.
- Mid-loop narration never arms (refresh runs only at exit) — unchanged.
- `GOAL_CONTRACT_ENABLED=false` → `_contract` is None → bit-for-bit legacy;
  the gate detector still protects under `SELF_EVAL_REPLAN_ENABLED`.
- ZH promises ("让我再查一下") don't match the EN-only regex — documented
  future work, out of scope.
- Guardrail-halt path (`agents.py:9064-9079`) still skips the exit checker —
  unchanged, out of scope.

## Trace check (user's turn)

query OK (exec=1) → query OK (exec=2) → exit arms promise (armed=3) →
`3 > 2` → fires → `force_synthesis` (usable data exists) → next iteration
`tool_choice="none"` → model writes the data answer → refresh sees clean
prose → disarm (model-armed) → `unmet()` empty → save real answer. ✓

## Files touched

| File | Change |
|---|---|
| `backend/app/services/goal_contract.py` | seq-stamp fields; rewrite `record_tool_executed`, `refresh_pending_action`, `_unmet_pending_action`; `UnmetCriterion.force_synthesis`; `_PENDING_OFFER_RE`; `build_goal_contract` stamps `_armed_by="user"` |
| `backend/app/routers/agents.py` | `_contract_force_synthesis` flag + consumption; exit-checker force_synthesis branch; `_loop_exit_monotonic` + real step duration |
| `backend/app/services/answer_verification.py` | `_detect_pending_action_promise` + wiring in `_evaluate_answer_inner` |
| `backend/tests/test_pending_action_after_metadata.py` | rewrite to seq API |
| `backend/tests/test_goal_contract_pending_seq.py` | NEW: repro + semantics tests |
| `backend/tests/test_answer_gate_pending_promise.py` | NEW: gate detector test (evaluator mock not called) |
| `backend/tests/test_agents_force_synthesis.py` | NEW: v3 force-synthesis consumption test |

## Testing

- **Bug repro (RED→GREEN)**: contract fed two OK query results, then
  `refresh_pending_action("…Let me re-query against the live tables…")` →
  `unmet()` MUST return pending_action with `force_synthesis=True`
  (pre-fix: returns `[]`).
- Seq semantics: same-phrase no re-stamp; NO_DATA no stamp; exec-after-arm
  satisfies; offer exclusion ("Let me know…" → no force); user-armed never
  disarmed by prose; model-armed disarmed by clean final prose;
  synthesis-vs-query selection on `_usable_results`.
- Gate detector: promise draft → INCOMPLETE without calling the LLM
  evaluator (mock asserts zero calls).
- v3 consumption: armed synthesis force → `tool_choice="none"` once, then
  cleared.
- Regression: goal-contract + agents-exit + data-agent + answer-gate suites
  (~250 tests) stay green.
- Acceptance: restart backend, re-run "Give me supply chain data for last
  30 days" → final answer contains numbers, no unexecuted promise;
  "Generating response" shows a small real duration.

## Rollout

No new flags. `GOAL_CONTRACT_ENABLED=true` and
`SELF_EVAL_REPLAN_ENABLED=true` already set in `backend/.env`. Deploy:
`docker restart zhanlu-backend`, run the acceptance prompt, verify the
final answer is data + no pending promise.
