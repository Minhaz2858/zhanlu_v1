# Investigation: "Agent answers, collapses, then generates the answer again"

**Date:** 2026-10-27 · **Mode:** READ-ONLY (no code changed, nothing committed, no restart)
**Conversation:** `1414bb16-dcd1-43ed-8477-41f1b35534e9` (session `b39f6a62-…`, "give me last week sales report please")
**Constraint honored:** no assumption of a fixed schema; the engine is schema-agnostic and stays so.

---

## 0. Verdict

**The defect is in BACKEND ORCHESTRATION, with a FRONTEND rendering consequence.**
It is **not** a display bug and **not** caused by the 2026-10-27 result-contract fix.

Three confirmed defects compound:

| # | Defect | Layer | Evidence |
|---|---|---|---|
| **D1** | One user turn creates **two full FSM executions** — a second complete plan+generate cycle starts right after the first finalizes | backend orchestration | 6 `Execution` rows for 2 user messages; second created 3.5 s after the first finished |
| **D2** | On the SSE path `executions.assistant_content` is **always empty by design**, so nothing can finalize the bubble if the streaming step is skipped/interrupted | backend (FSM) | all 8 executions `done`, confidence 1.0, `assistant_content` length **0** |
| **D3** | The first assistant `ChatMessage` row was persisted with **0 chars**, so the UI has nothing to render and the "answer" appears to collapse | persistence | `content_len=0` at 03:10:52 |

D2 + D3 explain the **collapse**. D1 explains why **the answer regenerates**.

---

## 1. Architecture — actual execution and rendering flow

```
POST /agents/conversations/v3/{conv}/messages/stream        (agents.py:11442 add_message_stream)
  │
  ├─ router: turn_router.decide_turn_route()        (V3_LLM_ROUTER_MODE=on)
  ├─ planning_trigger.should_trigger_planning() / is_followup_refinement()  (agents.py:12264)
  ├─ data-bound override  → route = FSM           (agents.py:12286)
  │
  └─ SynexiaFSM route                               (agents.py:12486-12600)
       ├─ ExecutionRequest(... generate_response not passed)
       ├─ await asyncio.to_thread(_fsm.run, req, on_state, False, on_plan_node)   ← 3rd arg False
       │     └─ FSM: INIT→GOAL→CONTEXT→PLAN→ACT→OBSERVE→VERIFY→FINALIZE
       │           └─ finalize(generate_response=False):
       │                 • confidence computed
       │                 • _deferred_response_prompt = _build_response_prompt()
       │                 • assistant_content = ""          ← D2 (fsm.py:851)
       │                 • execution.current_state = DONE
       │                 • db.commit()
       │           ⇒ returns ExecutionResult(assistant_content="")
       │
       ├─ async for (_et,_data) in _fsm.stream_final_response(req)                (agents.py:12625)
       │     • builds prompt → stream_chat_completion()  ⇒ SECOND LLM generation
       │     • assistant_content accumulates from SSE deltas
       │
       ├─ persist ChatMessage(session_id, content=assistant_content)             (agents.py:12716)
       └─ yield done
```

**Two separate LLM generations per data turn are by design:**
1. `fsm.run()` — plans and executes nodes (each nl2sql node is its own generation).
2. `stream_final_response()` — narrates the answer token-by-token.

`executions.assistant_content` is intentionally empty on the SSE path — the text exists
only in the streamed `delta` events. That is fine **as long as the stream completes**.
It is the point of failure.

---

## 2. Timeline (from `chat_messages` + `executions` + backend logs)

| Time | Event |
|---|---|
| 03:10:52.210 | user msg 1 persisted |
| 03:10:52.245 | assistant msg 1 created — **content = 0 chars** |
| 03:10:53 | `v3 add_message_stream: data-bound` → FSM route chosen |
| 03:11:16 | **executions `98427a20` + `8874021a` created** (2 rows, 4 ms apart) |
| 03:12–03:17 | repeated NL2SQL runs; `FSTATUS not found in any referenced table`; ER self-heals |
| 03:14:17 | `98427a20` → DONE, confidence 1.00 |
| 03:14:29 | narration guard: *"claimed unsupported data ['order_count: 65'] — rewriting"* (one extra LLM call) |
| 03:14:42 | `8874021a` → DONE, confidence 1.00; `v3 stream checkpoint saved … msgs=2 tool_calls=2` |
| **03:14:43.29** | one more chat-completions call begins (the `stream_final_response` narration) |
| **03:14:46.10** | **execution `d54ee447` created** — `TaskSpec parsed`, `Plan generated (v1): 5 nodes` ⇒ **a SECOND full plan cycle starts** |
| 03:17:32 | `NLAnswer: repaired SQL after DB error` (`WITH sales AS …`) + background DB error |
| 03:17:58.798 | **user msg 2** persisted (the user re-sent the same text) |
| 03:18:09 | executions `9378f5d7` + `23c598b2` created (turn 2) |
| 03:19:56 | `d54ee447` DONE, confidence 0.88; **`turn refinement budget exhausted (2/2 plans used) — keeping existing results and not re-planning`** |
| 03:19:56 | `v3 stream: wall target reached` |
| 03:20–03:23 | further executions `e6258e3f`, `1a3d03da`, `5b4ef2d5` |

### Execution table (the smoking gun)

```
 id       created    state  conf  assistant_content_len  completed
 8874021a 03:11:16   done   1.0   0                      03:14:42
 98427a20 03:11:16   done   1.0   0                      03:14:17
 d54ee447 03:14:46   done   0.88  0                      03:19:56
 9378f5d7 03:18:09   done   1.0   0                      03:20:35
 23c598b2 03:18:09   done   0.82  0                      03:20:43
 e6258e3f 03:20:01   done   0.82  0                      03:23:10
 1a3d03da 03:21:09   act    —     0                      (still running)
 5b4ef2d5 03:21:09   act    —     0                      (still running)
```

**Two user messages. Eight executions.** Every `done` execution has
`assistant_content` length 0 — confirming D2 for *all* of them, not one anomaly.

---

## 3. Confirmed findings

### D1 (confirmed) — a second full plan cycle per turn

`agents.py:12625` calls `_fsm.stream_final_response(_fsm_req)` after `run()` returns, and
then execution `d54ee447` is created 3 s later with a **fresh TaskSpec parse and plan
generation** (`fsm.py:507`, `fsm.py:593`). That is a *new* execution row, not a continuation
of `8874021a`.

The `refinement_budget` module (`app/services/synexia/refinement_budget.py`) documents
exactly this class of bug — *"A single user turn could create SIX plans… Each failure
bought another plan"* — and caps it at `SYNEXIA_TURN_REPLAN_MAX=2`. The log confirms the cap
did fire (`2/2 plans used`), i.e. **the budget bounded the damage but did not prevent the
second cycle.** The budget is shared across *both* re-plan paths per `execution_id`; a
second *execution* starts a fresh budget-bearing row.

> **Hypothesis (not yet proven):** `98427a20` + `8874021a` created 4 ms apart, and
> `9378f5d7` + `23c598b2` likewise, suggests the FSM is invoked twice per turn (parallel
> tool-batch or a doubled entry), while `d54ee447`/`e6258e3f` come from the post-answer
> refinement path. Confirming which caller creates the *second* execution requires
> instrumenting `SynexiaFSM.__init__` with a stack trace — next investigation step.

### D2 (confirmed) — `assistant_content` is empty by design on SSE

`fsm.py:864-875`: when `generate_response` is False (the SSE path passes `False` as the
3rd arg at `agents.py:12591`), `assistant_content = ""` and the prompt is deferred to
`stream_final_response`. **Consequence:** if `stream_final_response` raises, times out, or
its consumer disconnects, the answer exists nowhere persisted — `executions.assistant_content`
is empty and the `ChatMessage` gets whatever was streamed (possibly nothing).

The code does have a fallback:
```python
except Exception as _stream_err:
    assistant_content = fsm_result.assistant_content or assistant_content   # agents.py:12632
```
`fsm_result.assistant_content` is **""** on this path (D2), so the fallback assigns `""`.
**The fallback is structurally incapable of recovering the answer** — it falls back from
empty to empty. That is the direct cause of the 0-char `ChatMessage` at 03:10:52.

### D3 (confirmed) — the persisted row is empty

```sql
session b39f6a62 …
03:10:52.245 | assistant | content_len = 0 | tool_calls = NULL | live_events = NULL
03:17:58.815 | assistant | content_len = 81
```
The first answer was never written. The second row has 81 chars — still not a report.

### What is NOT the cause

- **Not the frontend.** `Chat.jsx` appends deltas and never clears assistant content;
  `plan_summary` only *adds* a plan object (`Chat.jsx:2316-2320`). The automation auto-retry
  guard (`Chat.jsx:1906`) is gated on `isCreateAutomationRequest` and cannot fire here.
- **Not the result-contract fix.** `query_timing` lines show `status=succeeded/assembled`
  (03:14:42) — the contract is working; `db_ms=None` only because those legacy observations
  predate the timing capture.
- **Not a duplicate-send race.** Message 2 at 03:17:58 is a genuine user re-send
  (identical text, 7 min later) — a *symptom* of the collapse, not its cause.

### Where the answers actually went

`result_text` on the observations holds grounded narration ("Retrieved N rows…", "## HEADLINE…").
The data was retrieved correctly; the **final answer assembly** is what never landed.

---

## 4. Smallest safe fix (NOT applied)

Ordered by risk, each independently verifiable.

**F1 — make the streaming fallback real (highest value, ~5 lines, backend only).**
In `fsm.py:finalize`, on the deferred path stash the observation-grounded narration as the
persistence fallback, e.g. set `self.execution.assistant_content` to a deterministic
digest of successful observations (a summary line + row counts + the `report_card_payload`
if present) *while still* streaming the LLM narration as the primary path. Then
`agents.py:12632`'s existing fallback has something to fall back **to**.
- Files: `backend/app/services/synexia/fsm.py` (finalize + `stream_final_response`),
  `backend/app/routers/agents.py:12625-12635`.
- Preserves grounded-result guarantees: the fallback is built **only** from observation
  `result_data`, never generated prose.
- Side effects: `executions.assistant_content` becomes non-empty for analytics/tests that
  assert emptiness — check `test_v3_fsm_stream.py`, `test_fsm_*`.

**F2 — bound the second plan cycle per TURN, not per execution (backend).**
`refinement_budget` keys on `execution_id`; make the per-turn counter key on
`(conversation_id, user_message_id/turn_key)` so a second `Execution` row cannot reset the
budget. Add an explicit log when a second execution is created within one turn.
- Files: `backend/app/services/synexia/refinement_budget.py`, caller in `fsm.py`.
- Side effect: legitimate long multi-step turns get a slightly harder ceiling; keep the
  budget configurable (`SYNEXIA_TURN_REPLAN_MAX`).

**F3 — fail loudly instead of silently shipping an empty answer (frontend + backend contract).**
If the `done` payload has empty `assistant_content`, emit an explicit
`{"type":"error","code":"empty_final_answer"}` rather than a `done` with empty content, so
the UI can show "the answer could not be finalized" instead of an empty bubble.
- Files: `agents.py` (done emit), `Chat.jsx` (render error state).

**Explicitly NOT recommended:** changing the schema-agnostic NL2SQL layer, the result
contract, or the frontend delta accumulation — none is implicated.

## 5. Regression tests to add

| # | Test | Expected |
|---|---|---|
| 1 | `finalize(generate_response=False)` then `stream_final_response` raises | `execution.assistant_content` non-empty, built from observations |
| 2 | Same, with 0 rows yet valid execution | honest "no matching rows" text, not empty |
| 3 | Two users messages → count FSM executions | ≤ 1 execution per turn (or explicit, logged reason) |
| 4 | Re-plan after a second execution row exists | budget still refuses (per-turn keying) |
| 5 | `done` with empty content | emits `error`/`empty_final_answer`, never a bare empty `done` |
| 6 | Persisted `ChatMessage` for a data turn | `content` non-empty OR an explicit error state |

## 6. Explicit attribution

| Question | Answer |
|---|---|
| Backend orchestration? | **Yes — primary.** D1 (second plan cycle), D2 (empty-by-design content + useless fallback) |
| Frontend rendering? | **Consequence only.** It faithfully renders the empty row; it does not clear or replace content |
| Streaming/event handling? | **Yes — contributing.** The SSE path is the only path where the answer is not persisted before streaming begins |
| Interaction? | The UI "collapse + regenerate" *appearance* is the interaction of an empty persisted row with a second plan cycle producing new activity events |

## 7. Reproduction / evidence commands (read-only)

```bash
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
  "select substr(id,1,8), to_char(created_date,'HH24:MI:SS'), current_state,
          confidence_score, length(coalesce(assistant_content,'')), to_char(completed_at,'HH24:MI:SS')
   from executions where conversation_id='1414bb16-dcd1-43ed-8477-41f1b35534e9' order by created_date;"

docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
  "select to_char(created_date,'HH24:MI:SS.MS'), role, length(coalesce(content,''))
   from chat_messages where session_id='b39f6a62-609b-407b-80a1-cec178cc294c' order by created_date;"

docker logs zhanlu-backend --since 4h 2>&1 | grep -E "created \(agent=|finalized \(confidence|refinement budget|checkpoint saved"
```
