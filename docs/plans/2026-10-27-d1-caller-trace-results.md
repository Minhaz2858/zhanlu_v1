# D1 Caller Trace — RESULTS: there is no duplicate FSM entry

**Date:** 2026-10-27 · **Mode:** READ-ONLY (no code changed, nothing committed, no restart)
**Supersedes:** the D1 hypothesis in `2026-10-27-answer-regeneration-investigation.md` §3
**Method:** instrumented `SynexiaFSM.__init__` in an isolated process (the live process was not
modified — the patch lives only in the throwaway `docker exec python -c` run) + DB correlation.

---

## 0. Headline: D1 as stated was WRONG

I previously reported "one user turn creates two full FSM executions" and inferred a
**duplicate entry path**. **That inference was incorrect.** The instrumentation disproves it.

| Claim (earlier) | Verdict | Evidence |
|---|---|---|
| Duplicate FSM entry — turn entered twice | **DISPROVEN** | 1 `query_database` call → exactly **1** `SynexiaFSM.__init__`, stack `query_database_tool.py:340 → _run_pipeline` |
| Second plan cycle = re-plan/refinement of the same question | **DISPROVEN** | every execution has a **distinct `user_message`** (different sub-question) |
| Executions 4 ms apart = race | **DISPROVEN** | distinct questions issued in parallel by the model's tool batch |

**What each `Execution` row actually is:** one `query_database` tool call, which internally
spins up its own `SynexiaFSM` to run the NL2SQL pipeline for *that sub-question*.

```python
# query_database_tool.py:273-293
req = ExecutionRequest(..., user_message=question, mode="dynamic", ...)
fsm = SynexiaFSM(fsm_db)
result = await asyncio.to_thread(fsm.run, req, None, False, None)
```

So the execution table is a **per-tool-call ledger**, not a per-turn ledger. Eight rows for
two user messages means the model issued eight `query_database` calls — which matches the
screenshot's `(1/2)`, `(2/2)` parallel batches across several plan attempts.

---

## 1. Instrumented stack trace (the requested evidence)

Patched `SynexiaFSM.__init__` to capture `traceback.extract_stack()`, then ran **one**
`_query_database(...)` call:

```
SynexiaFSM.__init__ call count for ONE query_database call: 1
--- init #1 ---
    app/services/tool_handlers/query_database_tool.py:340 in _query_database
    app/services/tool_handlers/query_database_tool.py:303 in _run_pipeline
```

No `routers/agents.py` frame. No second init. **The turn's FSM route does not re-enter.**

A second probe with a 3-call concurrent batch (2 identical questions + 1 distinct):

```
FSM inits for 3 concurrent calls (2 identical + 1 distinct): 2
callers: ['_run_pipeline', '_run_pipeline']
```

Exactly as designed: the in-flight dedup collapsed the identical pair, and the distinct
question got its own run. **The P1 dedup works.** No evidence of an uncontrolled duplicate
entry path.

---

## 2. Timestamp correlation — the near-simultaneous pairs are distinct questions

```
8874021a | 03:11:16.583 | For the sales order view …, what were total revenue … and order count
98427a20 | 03:11:16.589 | Top 10 materials by revenue …          ← DIFFERENT question
d54ee447 | 03:14:46.077 | From … daily revenue totals by PLANDATE, and separately the …
9378f5d7 | 03:18:09.849 | Daily revenue … and order count for each day …
23c598b2 | 03:18:09.859 | Top 10 customers by revenue …          ← DIFFERENT question
e6258e3f | 03:20:01.890 | Daily revenue for 2026-10-19 …
1a3d03da | 03:21:09.369 | … week 2026-10-19 to 2026-10-25 versus the week before …
5b4ef2d5 | 03:21:09.377 | … revenue booked on 2026-10-23 alone …
```

The 4–10 ms gaps are **parallel tool calls**, not duplicate entries. The model batched two
`query_database` calls per iteration and ran them concurrently.

---

## 3. Re-planning: real, but per-execution and bounded

```
execution | plans
98427a20  | 1
8874021a  | 1
9378f5d7  | 1
d54ee447  | 2   ← re-planned once
e6258e3f  | 2
23c598b2  | 2
5b4ef2d5  | 2
1a3d03da  | 2
```

Re-planning **does** happen (1 extra plan) inside individual executions — that is the
`refinement_budget` working as designed (`2/2 plans used`), and it is **scoped to a single
tool call's own FSM**, not a duplicated turn. It is bounded correctly.

---

## 4. The real failure: the HTTP turn ended before the work finished

This is the finding that replaces D1.

```
03:10:52.21  user msg 1 persisted
03:10:52.245 assistant msg 1 created (0 chars)
03:11:09.28  POST .../v3/1414bb16.../messages/stream   -> 200 OK   ← REQUEST COMPLETED
03:11:16.58  executions 8874021a + 98427a20 CREATED               ← work starts AFTER completion
03:14:17    98427a20 DONE
03:14:42    8874021a DONE
03:14:46    d54ee447 created
03:19:56    d54ee447 DONE
```

**The streaming request returned at 03:11:09, seven seconds before the first sub-question
execution was even created.** The generator that was supposed to emit `delta` events and
persist the assistant `ChatMessage` had already finished. Everything executed afterwards
ran *detached from the response* — it could not modify the message the user was looking at.

That is why turn 1 persisted `0 chars / 0 live_events`, while turn 2 (which stayed open)
persisted `5,875 chars / 26 live_events`.

### Why the request ended early — the precise code path to examine

`agents.py:12591` runs the FSM on a worker thread:

```python
fsm_result = await asyncio.to_thread(_fsm.run, _fsm_req, _on_state, False, _on_plan_node)
```

`_on_state` / `_on_plan_node` are callbacks that append SSE strings to a shared `collected`
list from that worker thread. If the emitting generator is not awaited to completion — or if
an exception path returns before the `async for ... stream_final_response` block — the
response closes while the thread keeps running. The 200 is logged at request *completion*, so
a 17 s completion with 8 minutes of remaining work means the generator stopped yielding.

> **Hypothesis (now the top candidate):** a keep-alive/send failure or an early `return` in
> the generator ends the SSE response while `asyncio.to_thread` work continues detached.
> Distinguishing "client disconnected" from "server closed" requires logging
> `request.is_disconnected()` at generator close — not yet instrumented.

---

## 5. Answers to the specific questions asked

| Question | Answer |
|---|---|
| **Which caller creates each duplicate execution?** | There are **no duplicates**. Every row is one `query_database` call creating its own FSM (`query_database_tool._run_pipeline`). |
| **Concurrent entry, continuation/refinement, or another trigger?** | **None of these.** Parallel tool calls (distinct questions) + per-tool-call FSM. Re-planning inside a call is bounded by `refinement_budget`. |
| **Is repeat planning independent of the failed narration stream?** | **Yes, and inversely:** the narration stream ended *early*, while planning continued. The two are independent; the narration failure is not caused by re-planning and vice-versa. |
| **Which regression test reproduces the duplicate entry?** | **None can, because the defect does not exist.** The reproducible defect is instead: *"SSE generator ends while FSM work is still in flight → assistant message persisted with 0 chars."* |

---

## 6. Revised remediation priority

The earlier F1/F2/F3 stand, but **F2 is now demoted** — there is no duplicate entry to bound.
The priority order becomes:

**F1 — make the deferred-response fallback real (NOW THE PRIMARY FIX).**
Implement exactly as you directed, with your naming adjustment:

```
grounded_fallback_content   ← built ONLY from verified observations
                              (successful nl2sql result_data, row counts,
                               source ids, report_card_payload if present)
```
1. Attempt normal streamed narration (`stream_final_response`).
2. On failure/timeout/empty → use `grounded_fallback_content`.
3. Persist the answer **actually delivered** (streamed text if any, else the fallback).
4. If neither yields a valid answer → emit an explicit error, never an empty `done`.
- Do **not** overload `executions.assistant_content` as the fallback carrier; keep the
  model narrative and the evidence-grounded recovery distinguishable (your point).
- Files: `backend/app/services/synexia/fsm.py` (finalize + a new
  `grounded_fallback_content` builder), `backend/app/routers/agents.py:12625-12640`.

**F1b — detect the early-close explicitly.** Log `is_disconnected()` and whether the
generator completed, so the "response ended while work continued" class is visible instead
of silent. This is what actually explains turn 1.

**F3 — explicit empty-answer error** (unchanged, low risk).

**F2 — deferred.** No duplicate entry exists to bound.

---

## 7. Correction to the previous document

`2026-10-27-answer-regeneration-investigation.md` §3 D1 and §2's timeline interpretation
("**execution `d54ee447` created … a SECOND full plan cycle starts**") should be read as
**one more `query_database` sub-question**, not a duplicated turn. D2 and D3 in that document
remain **confirmed and unchanged** — they are the real persistence defect.

## 8. Repro commands (read-only)

```bash
# instrumented trace (isolated process; live service untouched)
docker exec zhanlu-backend python -c "
import traceback, asyncio
from app.services.synexia import fsm as m
O=m.SynexiaFSM.__init__; calls=[]
def p(self, db, *a, **k):
    calls.append([f'{x.filename.split(\"/app/\")[-1]}:{x.lineno} {x.name}'
                  for x in traceback.extract_stack()[:-1] if 'app/' in x.filename][-4:])
    return O(self, db, *a, **k)
m.SynexiaFSM.__init__=p
from app.services.tool_handlers.query_database_tool import _query_database
from app.database import SessionLocal
db=SessionLocal()
asyncio.run(_query_database({'question':'What was total revenue for 2026-10-19 to 2026-10-25?'},
  db=db, user_id='t', context={'org_id':'default-org','app_id':'default-app',
  'conversation_id':'probe','bound_kb_ids':['74c8d56d-f796-4004-8268-5648cbaf3955'],
  'agent_name':'general_assistant'}))
print(len(calls)); [print(c) for c in calls]"

# the decisive timeline fact
docker logs zhanlu-backend --since 5h 2>&1 | grep -E "messages/stream HTTP|created \(agent=" | grep 1414bb16
```
