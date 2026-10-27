# SSE Early Termination — Lifecycle Trace and Root Cause

**Date:** 2026-10-27 · **Mode:** READ-ONLY (no code changed, nothing committed, no restart)
**Subject:** conversation `1414bb16-dcd1-43ed-8477-41f1b35534e9`, session `b39f6a62-609b-407b-80a1-cec178cc294c`
**Preserves:** dynamic-database support, grounded-result guarantees, the NL2SQL result contract — none are implicated or need changing.

---

## 0. Root cause in one sentence

**The FSM route's StreamingResponse omits `_guarantee_done`, which the ReAct route has; when the FSM
generator finishes its work but terminates before emitting the `done` frame, the client
receives a clean `200 OK` with no terminal event, and the assistant message — already
persisted with whatever content the stream had produced (nothing) — stays empty.**

The response was **not** closed early and the client did **not** disconnect — the `200 OK`
log fires at `StreamingResponse` *return*, not at body completion (see §3 H2). The real
defect is that the FSM generator **never emitted a terminal `done` frame**, so the client's
stream ended with no terminal event and the optimistically-created empty assistant bubble
was never filled.

---

## 1. Lifecycle diagram

```
POST /v3/{conv}/messages/stream
   │
   ├─ add_message_stream (agents.py:11584)  ── returns StreamingResponse IMMEDIATELY (:12869)
   │     └─ generator body has NOT run yet
   │
   └─ StreamingResponse(
        _sse_with_heartbeat(                              ← emits ": ping" every N s
          _disconnect_safe_stream(                        ← pump task, detached by design
            _fsm_event_stream                             ← THE WORK
          )))

FSM branch stack (agents.py:12870):
   _sse_with_heartbeat(_disconnect_safe_stream(_fsm_event_stream))        ← no _guarantee_done
ReAct branch stack (agents.py:20163):
   _sse_with_heartbeat(_guarantee_done(_disconnect_safe_stream(event_stream)))   ← HAS it
```

Runtime sequence for turn 1:

```
03:10:52.245  ChatMessage(assistant, content="") created by the FRONTEND          [F5]
03:10:53.29   route = SynexiaFSM (data-bound override, agents.py:12301)
03:11:09.28   POST .../messages/stream -> 200 OK                                   [T1]
              ^ response completed while _fsm_event_stream still had 8 min of work
03:11:16.58   executions 8874021a + 98427a20 created (query_database tool calls)
              ^ the FSM generator is running in the detached pump task
03:14:17/42   both executions reach DONE, confidence 1.00
03:14:42.76   result_contract: status=succeeded/assembled rows=2                  [ok]
03:14:42.82   v3 stream checkpoint saved                                          [T2]
03:14:46.07   d54ee447 created  (ANOTHER query_database call — new sub-question)
03:19:56.69   v3 stream: wall target reached
             ...  no "done" frame, no invocation record for turn 1 ...
```

`agent_invocations` — written **after** the `done` yield in `_fsm_event_stream`:

```
03:23:24  conversation  completed
03:26:45  conversation  completed
```

**Only two invocation rows exist, both from 03:23+.** Turn 1's FSM generator **never reached
the code after `yield done_payload`** (`agents.py:12814`) — that is the verified proof it
terminated before the terminal frame.

---

## 2. Answers to the six questions

| # | Question | Answer | Evidence |
|---|---|---|---|
| **1** | Why does HTTP complete before the NL2SQL worker finishes? | **`_disconnect_safe_stream` detaches the pump by design** (`agents.py:3958-4026`): the outer generator yields frames from a queue while `_pump()` runs the real generator in a background task. When the response ends, the pump continues. So "response complete" ≠ "work complete" **by construction**. | `:4020-4026` `finally: # detached by design` |
| **2** | Cancelled, closed, returned early, exception, or lost consumer? | **The FSM generator terminated after its work but without emitting `done`.** Not a client cancel (see Q3), not a pump exception (would log `background agent-loop pump failed` — absent). | no pump-failure log; no invocation row; `content_len=0` |
| **3** | Is `request.is_disconnected()` true? Who closes? | **`is_disconnected()` is never called anywhere in this path** (no call site in `agents.py`). The response closed **server-side** with `200 OK`, which is what a normally-completing ASGI response logs — a client abort would not present this way and would surface as `CancelledError`/`GeneratorExit` in the pump. | `grep is_disconnected` → 0 hits in the stream path |
| **4** | Does the worker continue after the generator closes? Where does its result go? | **Yes during 03:11-03:19** (executions kept completing: 03:14:17, 03:14:42, 03:14:46 new call, 03:19:56). Results go to **`execution_records` + `observation_records` + `conv.messages`**; the *user-visible* `ChatMessage` is written only by `_fsm_event_stream` **after** streaming, so it was never updated. | executions table; `content_len=0` persists to now |
| **5** | Is the empty message persisted before or after the generator can no longer deliver? | **BEFORE.** The empty `ChatMessage` is created by the **frontend** at send time (`Chat.jsx` creates `aiMsg` with `content: ''`), i.e. at `03:10:52.245` — 17 s before the response completed and ~2 min before any answer existed. The backend's own persist (`agents.py:12716`) never ran. | `chat_messages` row at 03:10:52 with len 0 |
| **6** | Which finally/exception/timeout/cancel paths are involved? | `_sse_with_heartbeat.finally` → cancels the pending `__anext__` and `aclose()`s `_disconnect_safe_stream` (`:3866-3873`). `_disconnect_safe_stream.finally` → **deliberately does not cancel** the pump (`:4020-4026`). `_fsm_event_stream` has **no `finally`** and its `try/except` blocks swallow all errors — including any failure between finishing the FSM and yielding `done`. | structure scan of `:12486-12870` |

---

## 3. Verified facts vs hypotheses

### Verified facts
1. The FSM route is wrapped **without** `_guarantee_done`; the ReAct route **with** it. (`:12870` vs `:20163`)
2. Turn 1 routed to the FSM branch (`agents.py:12301` log).
3. `_disconnect_safe_stream` intentionally keeps the pump alive after the response closes (`:4020-4026`).
4. `_fsm_event_stream` contains **no `finally`**; every `try` swallows exceptions (`_conv_msg_err`, `_fsm_xp_err`, `_fsm_inv_err`, `_art_emit_err`).
5. `agent_invocations` has **no row for turn 1** → the generator never executed the code after `yield done_payload`.
6. All 8 executions reached `done`, several with confidence 1.00; `assistant_content` length 0 on all.
7. `request.is_disconnected()` is never consulted in this code path.

### H2 — SETTLED (and it corrects my earlier report)

**The `200 OK` access-log line is emitted when the `StreamingResponse` object is RETURNED,
not when its body finishes.** Proof: turn 2's request logged `200 OK` at `03:18:03`, while
its executions ran until `03:23:10` — a 5-minute gap on a turn that *did* deliver an answer.
Turn 1 shows the same pattern at `03:11:09`.

**Therefore my earlier claim — "the SSE response ended at 03:11:09, seven seconds before the
first execution was created" — was an artefact of misreading the access log.** The response
was still open; the endpoint returned the `StreamingResponse` at 03:11:09 and the detached
pump continued streaming. The real defect is not an early-closed response but a **missing
terminal frame**: the FSM generator never yielded `done`, so the client's stream never
received its terminal event and the empty optimistic bubble was never filled.

### Hypotheses (not yet proven)
- **H1 — what exactly raised before `done`.** The FSM branch's post-processing
  (`_record_turn_experience`, `_store_turn_cache`, `conv.to_dict()` inside `done_payload`,
  artifact serialization) is large and unguarded *between* the persist and the `done`
  yield. A failure in `conv.to_dict()` or artifact serialization would end the generator
  with no terminal frame. **Would be confirmed by an exception log** — none exists because
  the wrapping `except Exception` blocks swallow at a level that logs only `debug`/`warning`
  in some cases.
- **H2 — the response ended at 03:11:09 because the endpoint had already produced its
  frames.** `_sse_with_heartbeat` breaks on `StopAsyncIteration` from `_disconnect_safe_stream`,
  which breaks on the `_SENTINEL` the pump puts in its `finally`. So the response ends when
  **the pump ends** — meaning `_fsm_event_stream` exhausted. That contradicts the fact that
  work continued to 03:19 unless the FSM generator yielded `done` and *then* something later
  failed. **Needs one instrumented run to settle.**

> H2 is the important open question: it determines whether we are fixing
> "no terminal frame on path X" (F3) or "the generator ends before the work" (F1b).

---

## 4. Minimal remediation (NOT implemented)

**R1 — add `_guarantee_done` to the FSM route (1-line, highest value).**
```
_sse_with_heartbeat(_disconnect_safe_stream(_fsm_event_stream))
→
_sse_with_heartbeat(_guarantee_done(_disconnect_safe_stream(_fsm_event_stream)))
```
`_guarantee_done` already exists (`:3885`) and is documented to emit a minimal `done`
before closing cleanly. Its absence on the FSM route is a **latent asymmetry**, not a design
choice. Effect: the client always receives a terminal frame; the frontend stops waiting.
- File: `backend/app/routers/agents.py:12870`.
- Side effects: one extra `done` frame on failure paths; no change to success paths.
- Preserves: result contract, grounded results, dynamic DB.

**R2 — make the FSM generator's post-work section fail-soft.** Wrap the block between the
assistant persist and `yield done_payload` in `try/except` that logs at `warning` **with
`exc_info=True`** and still yields `done`. Today a failure there is silent.
- File: `agents.py:12753-12820`.

**R3 — grounded fallback (the previously agreed F1).** Independent of R1/R2 and still
correct: if `assistant_content` is empty at `done` time, populate it from verified
observations via `grounded_fallback_content` (never model prose). Keeps the model narrative
and the evidence-grounded recovery distinguishable.

**R4 — add disconnect visibility.** Log `is_disconnected()` and whether the generator
completed, since neither is observable today. Small, diagnostics-only.

---

## 5. Regression tests proposed

| # | Test | Expected |
|---|---|---|
| 1 | FSM route: inner generator raises before `done` | client still receives a `done` frame (R1) |
| 2 | FSM route: inner generator completes normally | exactly **one** `done` frame, no duplicate |
| 3 | `_fsm_event_stream` post-work section raises | `done` still yielded; warning logged with traceback (R2) |
| 4 | `assistant_content` empty at `done` | `grounded_fallback_content` used; built only from observation `result_data` (R3) |
| 5 | Both paths produce no answer | explicit `error` frame, never an empty `done` |
| 6 | Wrapper parity | FSM and ReAct stacks both include `_guarantee_done` (guards against regression of R1) |
| 7 | Response completes while pump still running | pump not cancelled; result persisted to `conv.messages` (locks in current intended behavior) |

---

## 6. Explicit attribution

| Layer | Role |
|---|---|
| **Backend orchestration** | **Primary.** Missing `_guarantee_done` on the FSM route + unguarded post-work section + empty-by-design `assistant_content` on the SSE path. |
| **Streaming/event handling** | **Primary (same code).** Detached pump is correct; the *terminal-frame guarantee* is missing on one of two routes. |
| **Frontend** | **Contributing, not causal.** It creates the empty assistant row optimistically and has no timeout/terminal-frame watchdog, so a response with no `done` leaves the bubble empty forever. |

---

## 7. Repro commands (read-only)

```bash
# proof the FSM route lacks the guarantee wrapper
sed -n '12870p;20163p' backend/app/routers/agents.py

# proof turn 1's generator never completed
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
 "select to_char(created_date,'HH24:MI:SS'), invocation_type, status
  from agent_invocations where conversation_id='1414bb16-dcd1-43ed-8477-41f1b35534e9'
  order by created_date;"

# proof the message was persisted empty by the client, long before work
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
 "select to_char(created_date,'HH24:MI:SS.MS'), role, length(coalesce(content,''))
  from chat_messages where session_id='b39f6a62-609b-407b-80a1-cec178cc294c'
  order by created_date;"

# is_disconnected is never consulted on this path
grep -n "is_disconnected" backend/app/routers/agents.py
```
