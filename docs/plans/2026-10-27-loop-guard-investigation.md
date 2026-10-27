# Step 3 — Investigation: `_detect_tool_call_loop` (second loop guard)

**Date:** 2026-10-27
**Status:** INVESTIGATION ONLY — no code changed, no containers restarted
**Question:** Do the three pre-existing `TestSuccessAwareLoopGuard` failures reveal a
detection defect, or outdated test expectations?

---

## 0. Answer up front

**Outdated test expectations.** The guard's *detection logic is sound* — it is
success-aware, key-based, and cap-driven, and it behaves correctly at the current caps.

But the investigation found **one real defect in the neighbouring guard** and **one
architectural fact that changes the risk picture**:

- **Finding A (test staleness):** all three failures are stale literal expectations after
  the caps were raised. No production behavior is wrong because of them.
- **Finding B (real defect):** `reliability.LoopState` is **never fed failures** — the only
  `record()` call site passes `success=True`, and every failure path `return`s before it.
  The failure counting added in P1 is therefore **inert**.
- **Finding C (wiring gap):** the chat/v3 path **never sets the conversation `LoopState`**,
  so the reliability guard is effectively per-call and inert there regardless.

Net: the *only* loop guard doing real work on the chat path today is
`_detect_tool_call_loop` (and a downstream `guards` path). Findings B can't be exploited
until C is fixed, and C must therefore precede or accompany any B fix.

---

## 1. What `_detect_tool_call_loop` actually does

`backend/app/routers/agents.py:4104-4189`

| Aspect | Behaviour |
|---|---|
| **Input** | `llm_messages` (full conversation), `start_idx` (turn scoping), `dynamic_caps` |
| **Key** | `(tool_name, canonicalized_args)` — JSON re-serialized with `sort_keys=True`, so `{"a":1,"b":2}` == `{"b":2,"a":1}` (`:4173-4177`) |
| **Name-only tools** | `_NAME_ONLY_KEYED_TOOLS = {"ask_data_agent", "fetch_data_batch"}` — args are *ignored* in the key (`:4163-4164, 3430`) |
| **Success awareness** | **YES.** Pairs `tool_call_id` → outcome (`:4140-4152`); unknown/in-progress ids default to `True` (`:4180`) |
| **Trip rule** | `succ >= cap` **or** `fail >= cap + 1` (`:4187`) |
| **Caps** | `dynamic_caps` → `TOOL_CALL_CAPS` → `TOOL_CALL_HARD_CAP` (`:4186`) |
| **Returns** | `(tool_name, count)` or `None` |

So the guard **does** recognize failures and gives them one extra chance
(`cap + 1`) for reformulation/self-heal. It is not failure-blind.

### Call sites (3, all in `agents.py`)

| Line | Path |
|---|---|
| `7225` | v2 buffered loop |
| `8974` | resume path |
| `14379` | **v3 streaming loop** (`start_idx=_turn_start_idx`, `dynamic_caps=_dynamic_tool_caps`) |

---

## 2. Reproducing the three failures

Runtime caps (measured in the running container):

```
TOOL_CALL_HARD_CAP = 10        # was 6
TOOL_CALL_CAPS = {'memory': 3, 'ask_data_agent': 6, 'fetch_data_batch': 3,
                  'interrupt': 2, 'clarify': 3}
```

| Test | Asserts | Actual behaviour | Verdict |
|---|---|---|---|
| `test_default_tool_success_trips_at_6` | `skills` trips at 6 successes | trips at **10** (`('skills', 10)`) | **stale literal** |
| `test_default_tool_failure_trips_at_7` | `skills` trips at 7 failures | trips at **11** (`('skills', 11)`) = cap+1 | **stale literal** |
| `test_ask_data_agent_name_only_keying` | trips at **2** | `ask_data_agent` cap is **6** → `None` | **stale literal** |

### History — when each drifted

| Change | Commit |
|---|---|
| `TOOL_CALL_HARD_CAP = 6` at initial snapshot | `23d88a7dd` |
| `ask_data_agent: 3` → `6` | `6a032a800` |
| `_NAME_ONLY_KEYED_TOOLS` introduced (was `set()`) | `bc8114263` |
| hard cap 6 → policy default 10 | `e781bbeed` "Phase 1a — turn budgets become policy" |

**The tests were correct at `23d88a7dd` and were never updated when the caps moved.**
They have been red since the caps changed — this is test drift, not a regression, and not
caused by the 2026-10-27 P0/P1 work (confirmed separately by stashing that patch).

### Behavioural matrix (measured, not assumed)

| Sequence | Result | Correct? |
|---|---|---|
| 6 identical **successes** (`skills`) | no trip | ✅ (cap 10) |
| 10 identical **successes** | trips | ✅ |
| 7 identical **failures** | no trip | ✅ (failure cap 11) |
| 11 identical **failures** | trips | ✅ |
| 3 failures with **different args** | no trip | ✅ exploration preserved |
| 2 `ask_data_agent` calls, different questions | no trip | ✅ (cap 6, name-keyed) |

---

## 3. Guard comparison

| | `agents._detect_tool_call_loop` | `reliability.LoopState.is_loop` |
|---|---|---|
| **State source** | `llm_messages` (durable, reconstructable) | in-memory `history` list |
| **Scope** | turn-scoped via `start_idx` (v3); whole history (v2) | per-conversation contextvar |
| **Key** | `(name, canonical args)`; name-only for 2 tools | `(name, args_hash)` |
| **Failure aware** | **Yes** (`cap + 1`) | **Yes in logic (P1), but see Finding B** |
| **Recovery margin** | +1 extra for failures | none (same threshold) |
| **Trigger point** | pre-call scan of message history | post-success check inside the wrapper |
| **Conflict risk** | none — read-only over messages | writes `success: False` into the result |

They track **different signals at different layers** and do not double-count. They can
disagree: the reliability guard can flip a *successful* tool result to
`success: False` ("loop guard: …") — a *result-rewriting* guard — whereas the agents guard
only nudges control flow.

---

## 4. Findings that matter more than the three tests

### Finding B — `LoopState` is never fed failures (real defect)

```python
# reliability/__init__.py — the ONLY record() call site
loop_state.record(tool_name, current_args, success=True)     # :431
```

Every failure path returns earlier without recording:

| Line | Failure path |
|---|---|
| `:372-378` | retries exhausted, no repair |
| `:379-385` | repair produced nothing |
| `:386-392` | second retry exhausted |
| `:394-401` | output verification failed |
| `:404-414` | inside try, pre-record |

**Consequence:** the failure-counting added in P1 (§`is_loop`) can never fire, because
`history` never contains a `success=False` entry. It is correct code that is **dead at the
call site**. The P1 unit tests exercise `LoopState` directly, so they pass — which is exactly
the "tests green, production inert" gap worth flagging.

### Finding C — the chat path never wires a conversation `LoopState`

```python
loop_state = get_conversation_loop_state()
if loop_state is None:
    loop_state = LoopState()   # transient — no cross-call history (chat path)
```
`agent_tools.py:239-241`

`set_conversation_loop_state` is called **only** in `automation_executor.py:2085`.
A repo-wide grep of `routers/` finds **zero** callers.

**Consequence:** on the chat/v3 path every tool call gets a *fresh* `LoopState`, so
`history` has length 1, always below the threshold of 3. Measured:

```
call 1..6: history=1  is_loop=False
```

So on the chat path the reliability loop guard **cannot ever trip** — not for successes and
not for failures.

### Combined implication

Findings B and C mean the production behaviour of the 2026-10-27 "failure-aware loop guard"
change is: **no behavioural change on the chat path** (it was already inert for successes
too). The real 18-execution loop in the investigation was bounded by
`_detect_tool_call_loop` / iteration budgets, not by `LoopState`.

This does **not** retract the RC-3 fix (typed retry classification), which operates on the
result dict and is unaffected.

---

## 5. Minimal corrective direction (NOT applied)

Ordered so each step is independently verifiable and low-risk.

**Step 3a — fix the stale tests (trivial, zero production risk).**
Bind expectations to the constants rather than literals:
`cap = TOOL_CALL_CAPS.get("skills", TOOL_CALL_HARD_CAP)` and assert at `cap` / `cap + 1`.
For `ask_data_agent`, assert against `TOOL_CALL_CAPS["ask_data_agent"]`. This makes the
tests track policy instead of freezing it.

**Step 3b — wire the conversation `LoopState` on the chat path (Finding C).**
Set it once per v3 turn alongside the existing `LoopState`-style contextvar patterns
(the codebase already does this for the dedup and loop-state contextvars), then reset in
`finally`. Without this, no improvement to `LoopState` can have production effect.

**Step 3c — record failures (Finding B), *after* 3b.**
Add a `loop_state.record(..., success=False)` on each failure return, preserving the
existing "failures get their own bounded retry" semantics. Only meaningful once 3b lands.

**Explicitly NOT recommended:** raising `TOOL_CALL_HARD_CAP` down to 6 to satisfy the tests —
the policy value went up deliberately, and pinning code to old test literals inverts the
dependency.

---

## 6. Regression tests needed (for the eventual fix)

| # | Test | Expected |
|---|---|---|
| 1 | `skills` successes at `TOOL_CALL_CAPS.get(...)` | trips exactly at cap (constant-derived, not literal) |
| 2 | `skills` failures at `cap + 1` | trips exactly at cap+1 |
| 3 | `ask_data_agent` at its configured cap | trips at that value, name-only keying preserved |
| 4 | Same tool, N different args, N > cap | **never** trips (exploration preserved) |
| 5 | `LoopState` records a failure on each failure return | `history` contains `success=False` |
| 6 | Chat path sets a per-turn `LoopState` | second identical call sees `history` length ≥ 2 |
| 7 | Guard does not fire on 2–3 transient failures before recovery | bounded retry still works |
| 8 | Turn-scoping: identical call in a *previous* turn does not trip | `start_idx` respected |

Test 4 and 7 are the anti-overcorrection guards — the risk of a loop-guard fix is
suppressing legitimate exploration or transient recovery.

---

## 7. Interaction with the P0/P1 work

| Change | Interaction |
|---|---|
| RC-1/RC-2 (result contract) | none |
| RC-3 (typed retry classification) | none — operates on result dicts, before the loop guard |
| RC-4 (`is_loop` failure counting) | **inert until Finding B and C are fixed**; unit tests pass but production is unaffected |
| In-flight dedup | independent (per-turn contextvar, explicitly scoped) |

**Recommendation:** treat 3a (tests) as an immediate small commit; treat 3b/3c as a
separate, tested change and do not fold them into the `ask_data_agent` integration.

---

## 8. Reproduce commands (read-only)

```bash
docker exec zhanlu-backend python -c "
from app.routers.agents import TOOL_CALL_HARD_CAP, TOOL_CALL_CAPS
print(TOOL_CALL_HARD_CAP, TOOL_CALL_CAPS)"          # 10  {'ask_data_agent': 6, ...}

docker exec zhanlu-backend python -m pytest \
  tests/test_phase1_tool_reliability.py::TestSuccessAwareLoopGuard -q

grep -n "loop_state.record(" backend/app/services/reliability/__init__.py   # 1 hit
grep -rn "set_conversation_loop_state" backend/app/routers/                 # 0 hits
```
