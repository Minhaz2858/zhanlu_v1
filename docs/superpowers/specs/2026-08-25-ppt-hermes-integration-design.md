# PPT Generation Flow + Hermes Re-Plan Integration Design

**Date:** 2026-08-25
**Status:** Approved (User selected "Spec + plan first, then implement")
**Goal:** Fix the bug where adding "in PPT format" to a data query causes the system to return "No data available." even when the data was successfully retrieved.

## Context

The user's two queries demonstrate the bug:

**Query 1 (WORKS — 101s):**
> "Weekly market reports — full CEO-level C5/C9 rundown i need comprehensive answer"

Produces a comprehensive CEO-level weekly market report with:
- Executive Summary (¥2.883亿 revenue, 46,584 tons shipped, +108% MoM)
- Key Metrics (revenue, volume, orders, ASP)
- Product Breakdown (8 products with detailed numbers)
- Inventory Status (4 products with stock-out warnings)
- Risks & Opportunities (DCPD inventory crisis)
- Recommended Actions (3 specific actions)

**Query 2 (FAILS — 335s):**
> "Weekly market reports — full CEO-level C5/C9 rundown i need comprehensive answer **i need in ppt formate**"

Produces: **"No data available."** — the user waited 335s for nothing.

The 3 ask_data_agent calls ran and returned data (203.2s × 2 + 23.5s × 1), but the synthesis path got an empty `_last_rows` and fell through to the empty-data fallback.

## Root Cause (5-Whys)

**Q1:** Why does the user see "No data available."?
**A:** `_build_deterministic_fallback` returned "No data available." because `data_rows` was empty.

**Q2:** Why was `data_rows` empty when 3 ask_data_agent calls succeeded?
**A:** The v3 stream loop's `_last_rows` was reset/never populated because the **PPT no-data finalize path** fired on the FIRST 0-row result and short-circuited the loop.

**Q3:** Why did the PPT path fire on the first 0-row result?
**A:** `agents.py:12332` has:
```python
elif tool_name == "ask_data_agent" and _should_finalize_no_data(result, _orch_doc_format):
```
This fires as soon as ANY `ask_data_agent` call returns 0 rows AND the user asked for a file format. It calls `build_no_data_payload()` and `finalize_into_artifact()`, finalizing an "empty" PPT/Word.

**Q4:** Why does the first ask_data_agent call return 0 rows?
**A:** The LLM picked the wrong table on the first try. The Hermes re-plan loop is designed to handle this — it re-reads the catalog and retries with a better table. But the PPT finalize fires BEFORE the re-plan can run.

**Q5:** Why was the PPT flow designed without considering Hermes re-plan?
**A:** The PPT turn-guard (`pptx_turn_guard.py`) and `_should_finalize_no_data` were designed assuming a single `ask_data_agent` call per turn. The Hermes re-plan can require 2-3 calls (the user even sees this in Query 1: 3 calls × 83.5s = 250.5s of data retrieval). The PPT flow doesn't account for this.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Component 1: Track data presence across iterations (foundation) │
│  • Add `_any_ask_data_with_rows` flag in v3 stream loop          │
│  • Set on EVERY ask_data_agent call that returns rows            │
│  • Never reset between iterations                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 2: Defer no-data finalize until all retries done     │
│  • Pass `_any_ask_data_with_rows` to `_should_finalize_no_data`  │
│  • If True: skip the no-data finalize (data exists elsewhere)    │
│  • Only fire when ALL ask_data calls in the turn are exhausted   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 3: Synthesis path must use ALL data from turn        │
│  • `_last_rows` must include data from the LAST successful call  │
│  • Synthesis prompt sees the full picture (not just one call)    │
│  • Deterministic fallback fires if synthesis fails              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 4: PPT/Word finalize must wait for synthesis          │
│  • Move `build_no_data_payload` + `finalize_into_artifact` call  │
│    AFTER the v3 stream loop ends                                │
│  • Synthesis produces a report → PPT is built from that report  │
│  • The artifact path uses synthesized data, not raw rows         │
└─────────────────────────────────────────────────────────────────┘
```

## Component 1: Track Data Presence Across Iterations

### Why

The v3 stream loop iterates up to `MAX_TOOL_ITERATIONS` times. The LLM may make multiple `ask_data_agent` calls in a single turn. Some calls may return 0 rows (wrong table on first try), others may return the real data (Hermes re-plan finding the right table). We need to remember that data EXISTS even if the most recent call returned 0 rows.

### Implementation

**File:** `backend/app/routers/agents.py`

**Change 1.1:** Add a turn-level flag to track data presence.

```python
# In the v3 stream loop, BEFORE the `for iteration in range(MAX_TOOL_ITERATIONS):` loop
_any_ask_data_with_rows = False  # FIX 2026-08-25: set on every ask_data call with rows
_any_ask_data_call_made = False   # for diagnostics
```

**Change 1.2:** Update the flag inside the iteration loop.

```python
# Inside the `for iteration in range(MAX_TOOL_ITERATIONS):` loop, after ask_data_agent result
if tool_name == "ask_data_agent":
    _any_ask_data_call_made = True
    result_rows = result.get("rows") if isinstance(result, dict) else None
    if result_rows:  # non-empty list
        _any_ask_data_with_rows = True
        logger.info(
            "v3 stream: ask_data_agent iter %d returned %d rows (any_with_rows=True)",
            iteration, len(result_rows),
        )
```

## Component 2: Defer No-Data Finalize Until All Retries Done

### Why

`agents.py:12332` fires the no-data finalize on the FIRST 0-row result. With Component 1's flag, we can skip this if any other call in the turn returned data.

### Implementation

**File:** `backend/app/routers/agents.py`

**Change 2.1:** Pass the data-presence flag to `_should_finalize_no_data`.

```python
elif tool_name == "ask_data_agent" and _should_finalize_no_data(
    result, _orch_doc_format,
    any_with_rows=_any_ask_data_with_rows,  # NEW: suppress if data exists elsewhere
):
```

**Change 2.2:** Update `_should_finalize_no_data` to accept the new parameter.

**File:** `backend/app/routers/agents.py` (line 3474)

```python
def _should_finalize_no_data(
    result: object,
    doc_format: object,
    *,
    any_with_rows: bool = False,  # NEW 2026-08-25
) -> bool:
    """True only when ask_data_agent RAN a query that returned 0 rows
    AND no other ask_data_agent call in this turn has returned rows.

    FIX 2026-08-25: previously this fired on the first 0-row result, which
    broke the Hermes re-plan flow when the user asked for a PPT/Word. Now
    we check `any_with_rows` so the no-data path only fires when ALL
    ask_data_agent calls in the turn returned empty.
    """
    if any_with_rows:
        # Some ask_data_agent call in this turn returned rows. The 0-row
        # result we're looking at now is just a wrong-table attempt.
        # Don't finalize an empty artifact — wait for synthesis to see
        # the good data.
        return False
    return (
        isinstance(result, dict)
        and bool(result.get("success"))
        and result.get("rows") == []
        and bool(doc_format)
    )
```

## Component 3: Synthesis Path Must Use ALL Data From Turn

### Why

The user gets the comprehensive CEO report when synthesis sees all 234 rows. If synthesis only sees the last successful call's data, it might miss important context.

### Implementation

**File:** `backend/app/routers/agents.py`

**Change 3.1:** Track `_last_rows` separately from any "rows" returned by ask_data_agent.

```python
# In the iteration loop, when an ask_data_agent call returns rows:
if tool_name == "ask_data_agent":
    if isinstance(result, dict):
        result_rows = result.get("rows")
        if result_rows:  # non-empty
            _last_rows = result_rows  # update _last_rows to the latest successful data
            _last_rows_source = f"ask_data_agent iter {iteration}"
```

Note: this is the existing behavior, just being made explicit. `_last_rows` is already set this way in the codebase.

## Component 4: PPT/Word Finalize Must Wait For Synthesis

### Why

The PPT/Word file is the user's deliverable. It should be built from the SYNTHESIZED analysis, not from raw rows. Currently the flow is:

1. ask_data_agent → 0 rows → finalize empty PPT
2. ask_data_agent → 234 rows → ignored
3. Synthesis → "No data available." (empty)
4. User sees "No data available."

The fix: only finalize a PPT/Word AFTER synthesis has run (whether via LLM or deterministic fallback). This way, the PPT is built from a complete analysis.

### Implementation

**File:** `backend/app/routers/agents.py`

**Change 4.1:** Move the no-data finalize OUT of the iteration loop.

Currently (line 12332): inside `for iteration in range(MAX_TOOL_ITERATIONS):`
After change: AFTER the loop ends, in the post-loop synthesis path

```python
# REMOVE from inside the iteration loop:
# elif tool_name == "ask_data_agent" and _should_finalize_no_data(result, _orch_doc_format):
#     ... build_no_data_payload + finalize_into_artifact ...

# ADD to the post-loop synthesis path (after the v3 stream loop ends):
if not _any_ask_data_with_rows and _any_ask_data_call_made and _orch_doc_format:
    # All ask_data calls returned 0 rows. Finalize an empty artifact.
    try:
        no_data_payload = build_no_data_payload(...)
        artifact, file_exports = finalize_into_artifact(...)
        ...
    except Exception as e:
        logger.warning(...)
```

This ensures the PPT/Word is only finalized AFTER all data retrieval is done, and only when the data is truly empty.

## Implementation Order

1. **Component 1 first** (foundation) — add `_any_ask_data_with_rows` flag
2. **Component 2 second** — wire flag into `_should_finalize_no_data`
3. **Component 3 third** — verify `_last_rows` is correctly set
4. **Component 4 last** — move no-data finalize out of the iteration loop

## Test Coverage

**4 new test files (1 per component):**

1. `tests/test_any_ask_data_with_rows_flag.py` — 4 tests
2. `tests/test_finalize_no_data_with_rows.py` — 4 tests
3. `tests/test_last_rows_accumulation.py` — 3 tests
4. `tests/test_post_loop_no_data_finalize.py` — 3 tests

**Total: 14 new tests**

## Trade-offs

| Approach | Pros | Cons |
|----------|------|------|
| **Track data presence (Component 1+2)** | Low risk, small change, fixes the immediate symptom | Doesn't fix the deeper architectural issue |
| **Move no-data finalize out of loop (Component 4)** | Architectural fix, future-proof | Bigger change, more risk |
| **Both (recommended)** | Defense-in-depth, fixes both layers | More code to maintain |

**Recommendation:** Implement BOTH. Component 1+2 is the safety net; Component 4 is the proper fix.

## Files Modified

- `backend/app/routers/agents.py` — 4 sites (Component 1, 2, 3, 4)
- `backend/tests/test_any_ask_data_with_rows_flag.py` — new
- `backend/tests/test_finalize_no_data_with_rows.py` — new
- `backend/tests/test_last_rows_accumulation.py` — new
- `backend/tests/test_post_loop_no_data_finalize.py` — new

## Migration / Rollout

This is a behavior change in the PPT/Word generation flow. To minimize risk:

1. Implement Components 1+2 first (low risk, no behavior change for non-PPT)
2. Run full test suite to verify no regressions
3. Implement Components 3+4 (medium risk, changes PPT behavior)
4. Run full test suite + manual PPT query test
5. If issues, Component 1+2 alone fixes the user's reported bug

## What I Did NOT Do (would need follow-up)

1. **Smart retry for the FIRST ask_data_agent call** — would prevent the 0-row first attempt entirely. Separate work.
2. **Synthesis LLM timeout for PPT queries** — qwen3's 60s synthesis cap might be too tight for PPTs. Separate work.
3. **Streaming the artifact generation** — currently the artifact is finalized in one shot. Separate work.

## Expected Behavior Change

**Before (Query 2):**
- 3 ask_data_agent calls (1st 0 rows, 2nd 234 rows)
- No-data finalize fires on 1st call → empty artifact
- 2nd call's 234 rows are ignored
- Synthesis sees empty `_last_rows`
- User sees "No data available."

**After (Query 2):**
- 3 ask_data_agent calls (1st 0 rows, 2nd 234 rows)
- `_any_ask_data_with_rows` becomes True on 2nd call
- No-data finalize is SKIPPED
- Synthesis sees 234 rows
- User gets comprehensive CEO report (same as Query 1)
- PPT is built from the synthesized report
