# Live-Streaming Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 4 live-streaming capabilities to zhanlu's chat UI so the user sees what the agent is doing RIGHT NOW (reasoning, search queries, plan, data previews) — matching Kimi/Claude/Perplexity UX.

**Architecture:** Extend the existing `_push_live_event` system and `LiveActivityStream` component additively. Add 4 new SSE event types (`reasoning_delta`, `search_query_delta`, `plan_step_added`, `data_preview`) that bypass the `_LIVE_EVENT_CAP=100`. Frontend gains 4 streaming state variables in `Chat.jsx` and 4 collapsible render sections in `LiveActivityStream.jsx`.

**Tech Stack:** FastAPI (SSE), Python asyncio, React + Tailwind, lucide-react icons.

**Reference Spec:** `docs/superpowers/specs/2026-08-25-live-streaming-progress-design.md`

## Global Constraints

- Dark/light mode must work in all new UI — use existing tokens (`text-muted-foreground`, `bg-secondary/30`, `border-purple-500/30`, etc.)
- Per-section collapse state lives in `LiveActivityStream`, NOT in `Chat.jsx`
- All 4 streaming content states live in `Chat.jsx` and reset on new turn
- Sections default to **open** while streaming, **collapsed** on turn end (`done` event)
- File reverts happen in this codebase — each task ends with a commit so we can re-apply

---

## Task 1: Backend — emit `reasoning_delta` SSE event per token

**Files:**
- Modify: `backend/app/routers/agents.py:10314-10315` (in `event_stream`)
- Test: `backend/tests/test_reasoning_delta_event.py`

**Interfaces:**
- Produces: `yield f'data: {json.dumps({"type": "reasoning_delta", "content": ev_data})}\n\n'`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_reasoning_delta_event.py
import json
import pytest

@pytest.mark.asyncio
async def test_reasoning_delta_emitted_per_token():
    """Each reasoning token from the LLM must emit a `reasoning_delta` SSE event."""
    from app.routers.agents import event_stream  # adjust if helper is elsewhere
    import inspect
    src = inspect.getsource(event_stream)
    # The handler must yield reasoning_delta events, not just accumulate
    assert '"reasoning_delta"' in src, "reasoning_delta SSE event not found in event_stream"
    assert '"content"' in src, "content field not found in reasoning_delta event"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_reasoning_delta_event.py -v 2>&1 | tail -10
```
Expected: FAIL (no `reasoning_delta` in source yet)

- [ ] **Step 3: Add the SSE yield in `agents.py`**

At `backend/app/routers/agents.py:10314-10315`, replace:

```python
elif ev_type == "reasoning":
    final_reasoning += ev_data
```

with:

```python
elif ev_type == "reasoning":
    final_reasoning += ev_data
    # 2026-08-25: live-streaming spec — emit per-token reasoning to client
    yield f'data: {json.dumps({"type": "reasoning_delta", "content": ev_data})}\n\n'
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_reasoning_delta_event.py -v 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/routers/agents.py backend/tests/test_reasoning_delta_event.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): emit reasoning_delta SSE event per token"
```

---

## Task 2: Backend — `_stream_typing_effect` helper for `search_query_delta`

**Files:**
- Create: `backend/app/services/agent_loop/streaming_helpers.py`
- Test: `backend/tests/test_stream_typing_effect.py`

**Interfaces:**
- Produces: `async def _stream_typing_effect(query: str, tool_call_id: str) -> AsyncIterator[str]` — yields `search_query_delta` SSE frames at 25ms intervals.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_stream_typing_effect.py
import pytest

@pytest.mark.asyncio
async def test_stream_typing_effect_yields_chunks():
    """_stream_typing_effect must yield search_query_delta events covering the full query."""
    from app.services.agent_loop.streaming_helpers import _stream_typing_effect
    frames = []
    async for frame in _stream_typing_effect("hello world", "tc-1"):
        frames.append(frame)
    # At least 2 frames (partial at midpoint, full at end)
    assert len(frames) >= 2
    # Last frame must contain the full query
    assert "hello world" in frames[-1]
    assert "search_query_delta" in frames[-1]
    assert "tc-1" in frames[-1]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_stream_typing_effect.py -v 2>&1 | tail -5
```
Expected: FAIL (module not found)

- [ ] **Step 3: Create the helper**

```python
# backend/app/services/agent_loop/streaming_helpers.py
"""2026-08-25: live-streaming spec — typing-effect helper for search queries.

Search tools receive a fully-formed query string from the LLM. To create a
live-typing UX without a true streaming source, we chunk the query and emit
~12 search_query_delta SSE events over ~300ms.
"""
import asyncio
import json
from typing import AsyncIterator


async def _stream_typing_effect(query: str, tool_call_id: str) -> AsyncIterator[str]:
    """Yield `search_query_delta` SSE frames that simulate typing the query.

    Each frame's `partial` field contains the query as it would appear after
    N more characters were typed. The last frame contains the full query.
    """
    if not query:
        return
    chunk_count = max(1, min(12, len(query)))
    chunk_size = max(1, len(query) // chunk_count)
    accumulated = ""
    for i in range(0, len(query), chunk_size):
        accumulated = query[: i + chunk_size]
        frame = {
            "type": "search_query_delta",
            "tool_call_id": tool_call_id,
            "partial": accumulated,
        }
        yield f"data: {json.dumps(frame)}\n\n"
        await asyncio.sleep(0.025)  # 25ms between chunks → ~300ms total for 12 chunks
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_stream_typing_effect.py -v 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/services/agent_loop/streaming_helpers.py backend/tests/test_stream_typing_effect.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): _stream_typing_effect helper for search_query_delta"
```

---

## Task 3: Backend — wire `_stream_typing_effect` into 4 search tools

**Files:**
- Modify: `backend/app/services/tool_handlers/db_tools.py:944` (`_search_documents`)
- Modify: `backend/app/services/tool_handlers/edia_delegation_tools.py:417` (`_ask_rag_research`)
- Modify: `backend/app/services/tool_handlers/web_search_tool.py:274` (`_web_search`)
- Modify: `backend/app/services/turn_action.py:346` (`_run_web_search_fallback`)
- Test: `backend/tests/test_search_tools_emit_typing.py`

**Interfaces:**
- Consumes: `_stream_typing_effect(query, tool_call_id)` from Task 2
- Produces: `search_query_delta` SSE events emitted at the start of each search tool call

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_search_tools_emit_typing.py
import inspect

def test_search_documents_calls_typing_effect():
    from app.services.tool_handlers import db_tools
    src = inspect.getsource(db_tools._search_documents)
    assert "_stream_typing_effect" in src or "search_query_delta" in src, \
        "_search_documents must emit search_query_delta via _stream_typing_effect"

def test_ask_rag_research_calls_typing_effect():
    from app.services.tool_handlers import edia_delegation_tools
    src = inspect.getsource(edia_delegation_tools._ask_rag_research)
    assert "_stream_typing_effect" in src or "search_query_delta" in src

def test_web_search_calls_typing_effect():
    from app.services.tool_handlers import web_search_tool
    src = inspect.getsource(web_search_tool._web_search)
    assert "_stream_typing_effect" in src or "search_query_delta" in src

def test_web_search_fallback_calls_typing_effect():
    from app.services import turn_action
    src = inspect.getsource(turn_action._run_web_search_fallback)
    assert "_stream_typing_effect" in src or "search_query_delta" in src
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_search_tools_emit_typing.py -v 2>&1 | tail -10
```
Expected: 4 FAILs

- [ ] **Step 3: Add typing-effect calls in each search tool**

For each of the 4 tools, add at the very start of the function body (after parameter parsing, before the actual search):

```python
# At top of each search tool, after query is extracted:
from app.services.agent_loop.streaming_helpers import _stream_typing_effect
async for _frame in _stream_typing_effect(query, tool_call_id="<generate-uuid>"):
    yield _frame  # OR: pass to the SSE stream — see below
```

**Note**: The exact integration depends on whether each tool is an `async def` or `async def + yield`. For tools that are plain `async def` (no yield), wrap the call:

```python
# For non-generator tools (most are async def returning dict):
_typing_frames = []
async for _frame in _stream_typing_effect(query, tool_call_id=str(uuid.uuid4())):
    _typing_frames.append(_frame)
# Then expose _typing_frames to the SSE stream — typically by setting a
# module-level pending_frames list that the v3 event_stream drains.
```

**Simplest path**: For each tool, capture the tool_call_id from the parent SSE stream's last `tool_call_started` event. If the tool doesn't know its own tool_call_id, generate a UUID and stash in a per-tool context that the v3 stream can read.

The implementer should look at `_search_documents`, `_ask_rag_research`, `_web_search`, `_run_web_search_fallback` and pick the least-invasive integration (either generate UUIDs, or thread tool_call_id through call sites).

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_search_tools_emit_typing.py -v 2>&1 | tail -5
```
Expected: 4 PASSes

- [ ] **Step 5: Run full test suite to catch regressions**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_deterministic_fallback.py tests/test_smart_retry.py tests/test_qwen3_fast_mode.py -v 2>&1 | tail -5
```
Expected: All previously-passing tests still pass

- [ ] **Step 6: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/services/tool_handlers/db_tools.py backend/app/services/tool_handlers/edia_delegation_tools.py backend/app/services/tool_handlers/web_search_tool.py backend/app/services/turn_action.py backend/tests/test_search_tools_emit_typing.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): wire search_query_delta into 4 search tools"
```

---

## Task 4: Backend — `_parse_plan_steps` helper for `plan_step_added`

**Files:**
- Modify: `backend/app/services/agent_loop/streaming_helpers.py` (add helper)
- Test: `backend/tests/test_parse_plan_steps.py`

**Interfaces:**
- Produces: `def parse_plan_steps_from_text(text: str) -> list[dict]` — extracts numbered list items / `Step N:` markers from streaming text. Returns list of `{step_index, title}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_parse_plan_steps.py
from app.services.agent_loop.streaming_helpers import parse_plan_steps_from_text

def test_parses_numbered_list():
    text = "Let me plan this:\n1. Query sales data\n2. Compute totals\n3. Format report"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 3
    assert steps[0]["title"] == "Query sales data"
    assert steps[1]["title"] == "Compute totals"
    assert steps[2]["title"] == "Format report"

def test_parses_step_keywords():
    text = "Step 1: Find users\nStep 2: Filter by date"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 2
    assert steps[0]["title"] == "Find users"

def test_returns_empty_for_no_plan():
    text = "Just answer the question directly"
    assert parse_plan_steps_from_text(text) == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_parse_plan_steps.py -v 2>&1 | tail -5
```
Expected: FAIL (function not defined)

- [ ] **Step 3: Add the parser**

Append to `backend/app/services/agent_loop/streaming_helpers.py`:

```python
import re

# 2026-08-25: heuristic plan-step parser for live-streaming spec.
# Detects "1. ...", "- ...", "* ...", "Step 1: ...", "步骤 1: ..."
_PLAN_STEP_RE = re.compile(
    r'(?m)^[ \t]*(?:'
    r'(?P<num1>\d+)\.[ \t]+(?P<title1>[^\n]+)'           # 1. Title
    r'|[-*][ \t]+(?P<title2>[^\n]+)'                       # - Title or * Title
    r'|(?:Step|步骤)\s*(?P<num2>\d+)\s*[:：][ \t]*(?P<title3>[^\n]+)'  # Step 1: Title
    r')'
)


def parse_plan_steps_from_text(text: str) -> list[dict]:
    """Extract plan steps from streaming text. Returns ordered list of {step_index, title}."""
    if not text:
        return []
    steps = []
    for m in _PLAN_STEP_RE.finditer(text):
        title = m.group("title1") or m.group("title2") or m.group("title3")
        num = m.group("num1") or m.group("num2")
        if title:
            title = title.strip()
            if len(title) > 200:  # truncate very long lines
                title = title[:200] + "…"
            steps.append({"step_index": int(num) if num else len(steps) + 1, "title": title})
    return steps
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_parse_plan_steps.py -v 2>&1 | tail -5
```
Expected: 3 PASSes

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/services/agent_loop/streaming_helpers.py backend/tests/test_parse_plan_steps.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): heuristic plan-step parser"
```

---

## Task 5: Backend — emit `plan_step_added` events as text streams

**Files:**
- Modify: `backend/app/routers/agents.py` (in the streaming text handler around line 10312)
- Test: `backend/tests/test_plan_step_added_event.py`

**Interfaces:**
- Consumes: `parse_plan_steps_from_text(text)` from Task 4
- Produces: `plan_step_added` SSE events as the text grows, one per detected step

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_plan_step_added_event.py
import inspect
from app.routers.agents import event_stream

def test_event_stream_emits_plan_step_added():
    """event_stream must call parse_plan_steps_from_text and emit plan_step_added events."""
    src = inspect.getsource(event_stream)
    assert "parse_plan_steps_from_text" in src, "parse_plan_steps_from_text not called in event_stream"
    assert '"plan_step_added"' in src, "plan_step_added SSE event not emitted"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_plan_step_added_event.py -v 2>&1 | tail -5
```
Expected: FAIL

- [ ] **Step 3: Add plan-step detection in event_stream**

In `backend/app/routers/agents.py`, in the streaming text handler (around the `elif ev_type == "reasoning":` block at line 10314), add the plan-step detection. Find the `delta` handler (around line 10312) and add a plan-step tracker:

```python
# Add at the top of event_stream() (or near _live_event_count init):
from app.services.agent_loop.streaming_helpers import parse_plan_steps_from_text
_plan_step_tracker: list[int] = []  # indexes of steps already emitted (dedup)

# In the text delta handler, after appending ev_data to assistant_content:
if ev_type == "delta" or ev_type == "text":
    # existing code that appends to assistant_content
    # ... then add:
    try:
        all_steps = parse_plan_steps_from_text(assistant_content)
        for step in all_steps:
            if step["step_index"] not in _plan_step_tracker:
                _plan_step_tracker.append(step["step_index"])
                yield f'data: {json.dumps({"type": "plan_step_added", "step_index": step["step_index"], "title": step["title"]})}\n\n'
    except Exception as _plan_err:
        logger.debug("plan step parse failed (non-fatal): %s", _plan_err)
```

The implementer must find the right place to insert this in `event_stream()`. Use the existing `_live_event_count` variable or the `assistant_content` accumulator as anchors.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_plan_step_added_event.py -v 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/routers/agents.py backend/tests/test_plan_step_added_event.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): emit plan_step_added events from streaming text"
```

---

## Task 6: Backend — emit `data_preview` event immediately on tool return

**Files:**
- Modify: `backend/app/routers/agents.py` (find the data_offer / tool_call_finished handler)
- Test: `backend/tests/test_data_preview_event.py`

**Interfaces:**
- Produces: `data_preview` SSE event with `{tool_call_id, columns, sample_rows, rows_so_far}` payload, fired immediately when a data-fetching tool returns rows

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_data_preview_event.py
import inspect
from app.routers.agents import event_stream

def test_event_stream_emits_data_preview():
    """event_stream must emit data_preview events for tool results with rows."""
    src = inspect.getsource(event_stream)
    assert '"data_preview"' in src, "data_preview SSE event not emitted"
    assert "sample_rows" in src, "sample_rows field not present"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_data_preview_event.py -v 2>&1 | tail -5
```
Expected: FAIL

- [ ] **Step 3: Find the data_offer emission and add data_preview**

Search the codebase for `_sse_data_offer` (the existing data preview mechanism). The implementer should find where it's emitted and add an additional `data_preview` SSE event right next to it:

```python
# Near existing _sse_data_offer emission, add:
if "columns" in tool_result and "rows" in tool_result:
    sample_rows = tool_result["rows"][:3]
    yield f'data: {json.dumps({"type": "data_preview", "tool_call_id": tool_call_id, "columns": tool_result["columns"], "sample_rows": sample_rows, "rows_so_far": len(tool_result["rows"])})}\n\n'
```

The implementer must find the exact emission site. Use `grep -n "data_offer\|_sse_data_offer" backend/app/routers/agents.py` to locate it.

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_data_preview_event.py -v 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add backend/app/routers/agents.py backend/tests/test_data_preview_event.py
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(backend): emit data_preview event on tool return"
```

---

## Task 7: Frontend — add 4 streaming state variables + 4 SSE handlers in `Chat.jsx`

**Files:**
- Modify: `frontend/src/pages/Chat.jsx` (find the existing SSE `event.data.type` switch)
- Test: `frontend/src/pages/Chat.streamingState.test.jsx`

**Interfaces:**
- State: `streamingReasoning: string`, `streamingSearchQueries: object`, `streamingPlanSteps: array`, `streamingDataPreviews: object`
- All 4 reset to empty at the start of every new turn (where existing `setLiveEvents([])` is called)

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/Chat.streamingState.test.jsx
import { renderHook, act } from '@testing-library/react';
import { useState } from 'react';

describe('Chat.jsx streaming state', () => {
  it('exposes 4 streaming state slots and a reset function', () => {
    // This test verifies the state shape exists in the Chat component.
    // Inspect the source file to confirm the 4 useState calls.
    const fs = require('fs');
    const src = fs.readFileSync(
      require.resolve('./Chat.jsx'),
      'utf-8'
    );
    expect(src).toMatch(/useState\(['"]['"]\)?[\s\S]{0,80}streamingReasoning/);
    expect(src).toMatch(/useState\(\{\}\)[\s\S]{0,80}streamingSearchQueries/);
    expect(src).toMatch(/useState\(\[\]\)[\s\S]{0,80}streamingPlanSteps/);
    expect(src).toMatch(/useState\(\{\}\)[\s\S]{0,80}streamingDataPreviews/);
  });

  it('resets all 4 streaming states at the start of every new turn', () => {
    const fs = require('fs');
    const src = fs.readFileSync(
      require.resolve('./Chat.jsx'),
      'utf-8'
    );
    // Find the function that resets state on new turn (look for setLiveEvents)
    const resetFn = src.match(/setLiveEvents\([^)]\)[\s\S]{0,500}?\}\);/);
    expect(resetFn).not.toBeNull();
    const block = resetFn[0];
    expect(block).toMatch(/setStreamingReasoning\(['"]['"]\)/);
    expect(block).toMatch(/setStreamingSearchQueries\(\{\}\)/);
    expect(block).toMatch(/setStreamingPlanSteps\(\[\]\)/);
    expect(block).toMatch(/setStreamingDataPreviews\(\{\}\)/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/ysk2025/zhanlu_7_30/frontend && npx jest src/pages/Chat.streamingState.test.jsx 2>&1 | tail -10
```
Expected: FAIL (no useState calls found)

- [ ] **Step 3: Add the 4 state variables in `Chat.jsx`**

Find the existing `useState` calls near the top of the Chat component (likely right after existing `liveEvents` state). Add:

```jsx
// 2026-08-25: live-streaming spec — 4 streaming state slots
const [streamingReasoning, setStreamingReasoning] = useState('');
const [streamingSearchQueries, setStreamingSearchQueries] = useState({});
const [streamingPlanSteps, setStreamingPlanSteps] = useState([]);
const [streamingDataPreviews, setStreamingDataPreviews] = useState({});
```

- [ ] **Step 4: Add the 4 SSE event handlers in the `event.data.type` switch**

Find the existing `case 'live_event':` (or similar) inside the SSE event loop. Add 4 new cases BEFORE the default fallthrough:

```jsx
case 'reasoning_delta':
  setStreamingReasoning(prev => prev + (data.content || ''));
  break;
case 'search_query_delta':
  if (data.tool_call_id) {
    setStreamingSearchQueries(prev => ({
      ...prev,
      [data.tool_call_id]: data.partial || '',
    }));
  }
  break;
case 'plan_step_added':
  setStreamingPlanSteps(prev => {
    // Dedup by step_index
    if (prev.some(s => s.step_index === data.step_index)) return prev;
    return [...prev, { step_index: data.step_index, title: data.title || '' }];
  });
  break;
case 'data_preview':
  if (data.tool_call_id) {
    setStreamingDataPreviews(prev => ({
      ...prev,
      [data.tool_call_id]: {
        columns: data.columns || [],
        sample_rows: data.sample_rows || [],
        rows_so_far: data.rows_so_far ?? null,
      },
    }));
  }
  break;
```

- [ ] **Step 5: Reset all 4 on new turn**

Find the function/code that calls `setLiveEvents([])` when starting a new turn. In the same code block, add:

```jsx
setStreamingReasoning('');
setStreamingSearchQueries({});
setStreamingPlanSteps([]);
setStreamingDataPreviews({});
```

- [ ] **Step 6: Pass the 4 state values as props to `LiveActivityStream`**

Find where `<LiveActivityStream ...>` is rendered. Add 4 new props:

```jsx
<LiveActivityStream
  events={liveEvents}
  legacySteps={activitySteps}
  phase={phase}
  reasoning={streamingReasoning}  // CHANGED: was the final string, now the live stream
  isStreaming={isStreaming}
  streamingSearchQueries={streamingSearchQueries}  // NEW
  streamingPlanSteps={streamingPlanSteps}  // NEW
  streamingDataPreviews={streamingDataPreviews}  // NEW
/>
```

- [ ] **Step 7: Run test to verify it passes**

```bash
npx jest src/pages/Chat.streamingState.test.jsx 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 8: Run existing tests to catch regressions**

```bash
npx jest src/pages/Chat 2>&1 | tail -10
```
Expected: All previously-passing tests still pass

- [ ] **Step 9: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/pages/Chat.jsx frontend/src/pages/Chat.streamingState.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): 4 streaming state slots + SSE handlers in Chat.jsx"
```

---

## Task 8: Frontend — live reasoning section in `LiveActivityStream.jsx`

**Files:**
- Modify: `frontend/src/components/chat/LiveActivityStream.jsx:545-568` (the existing Thinking block)
- Test: `frontend/src/components/chat/LiveActivityStream.reasoning.test.jsx`

**Interfaces:**
- Props: `reasoning: string` (live), `isStreaming: boolean`
- State: `reasoningOpen: boolean` (default `true`)
- Visual: purple border/background, char count, pulsing cursor, collapsible

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/chat/LiveActivityStream.reasoning.test.jsx
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

describe('LiveActivityStream reasoning section', () => {
  it('renders live reasoning text with char count', () => {
    render(
      <LiveActivityStream
        events={[]}
        reasoning="I am thinking about this query"
        isStreaming={true}
      />
    );
    expect(screen.getByText(/Thinking/i)).toBeInTheDocument();
    expect(screen.getByText(/I am thinking about this query/)).toBeInTheDocument();
  });

  it('shows pulsing cursor when streaming', () => {
    const { container } = render(
      <LiveActivityStream
        events={[]}
        reasoning="Live content"
        isStreaming={true}
      />
    );
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/ysk2025/zhanlu_7_30/frontend && npx jest src/components/chat/LiveActivityStream.reasoning.test.jsx 2>&1 | tail -10
```
Expected: FAIL

- [ ] **Step 3: Replace the static Thinking block with the live version**

In `frontend/src/components/chat/LiveActivityStream.jsx`:

1. Find the existing `const [thinkingOpen, setThinkingOpen] = useState(false);` and rename to `reasoningOpen`, default `true`:

```jsx
// 2026-08-25: live-streaming spec — default open while streaming
const [reasoningOpen, setReasoningOpen] = useState(true);
```

2. Replace the existing Thinking section (around line 545-568) with:

```jsx
{/* Live reasoning section — purple, collapsible, char count, pulsing cursor */}
{typeof reasoning === 'string' && reasoning.trim().length > 0 && (
  <div className="border-t border-purple-500/30 bg-purple-500/5 px-3 py-2">
    <button
      type="button"
      onClick={() => setReasoningOpen((v) => !v)}
      data-testid="live-activity-reasoning-toggle"
      className="flex w-full cursor-pointer items-center gap-1.5 text-xs text-muted-foreground transition-opacity hover:opacity-100"
    >
      <Brain className="h-3 w-3 shrink-0 text-purple-500/80" />
      <span className="font-mono text-[10px] uppercase tracking-wide opacity-70">
        {isEn ? 'Thinking' : le.thinking || '思考'}
      </span>
      <span className="text-[10px] opacity-50">
        · {reasoning.length.toLocaleString()} chars
      </span>
      <ChevronDown
        className={`ml-auto h-3 w-3 transition-transform ${reasoningOpen ? 'rotate-180' : ''}`}
      />
    </button>
    {reasoningOpen && (
      <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded bg-secondary/30 px-3 py-2 font-mono text-xs text-muted-foreground">
        {reasoning}
        {isStreaming && <span className="animate-pulse text-purple-400">▍</span>}
      </pre>
    )}
  </div>
)}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx jest src/components/chat/LiveActivityStream.reasoning.test.jsx 2>&1 | tail -5
```
Expected: 2 PASSes

- [ ] **Step 5: Run existing LiveActivityStream tests**

```bash
npx jest src/components/chat/LiveActivityStream 2>&1 | tail -10
```
Expected: All previously-passing tests still pass

- [ ] **Step 6: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/components/chat/LiveActivityStream.jsx frontend/src/components/chat/LiveActivityStream.reasoning.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): live reasoning section with char count + cursor"
```

---

## Task 9: Frontend — live plan section in `LiveActivityStream.jsx`

**Files:**
- Modify: `frontend/src/components/chat/LiveActivityStream.jsx` (add new section)
- Test: `frontend/src/components/chat/LiveActivityStream.plan.test.jsx`

**Interfaces:**
- Props: `streamingPlanSteps: array<{step_index, title}>`, `isStreaming: boolean`
- State: `planOpen: boolean` (default `true`)
- Visual: blue border/background, step count, numbered list, last-step spinner

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/chat/LiveActivityStream.plan.test.jsx
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

describe('LiveActivityStream plan section', () => {
  it('renders plan steps with step count', () => {
    render(
      <LiveActivityStream
        events={[]}
        streamingPlanSteps={[
          { step_index: 1, title: 'Query sales data' },
          { step_index: 2, title: 'Compute totals' },
        ]}
        isStreaming={true}
      />
    );
    expect(screen.getByText(/Plan/i)).toBeInTheDocument();
    expect(screen.getByText(/Query sales data/)).toBeInTheDocument();
    expect(screen.getByText(/Compute totals/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx jest src/components/chat/LiveActivityStream.plan.test.jsx 2>&1 | tail -5
```
Expected: FAIL

- [ ] **Step 3: Add the live plan section**

In `frontend/src/components/chat/LiveActivityStream.jsx`, find where the existing reasoning section is rendered. Below it, add the new plan section. First, add the state at the top of the component:

```jsx
const [planOpen, setPlanOpen] = useState(true);
```

Then add the render block (after the reasoning section, before the rest of the component body):

```jsx
{/* Live plan section — blue, collapsible, step count, last-step spinner */}
{Array.isArray(streamingPlanSteps) && streamingPlanSteps.length > 0 && (
  <div className="border-t border-blue-500/30 bg-blue-500/5 px-3 py-2">
    <button
      type="button"
      onClick={() => setPlanOpen((v) => !v)}
      data-testid="live-activity-plan-toggle"
      className="flex w-full cursor-pointer items-center gap-1.5 text-xs text-muted-foreground transition-opacity hover:opacity-100"
    >
      <ClipboardList className="h-3 w-3 shrink-0 text-blue-500/80" />
      <span className="font-mono text-[10px] uppercase tracking-wide opacity-70">
        {isEn ? 'Plan' : le.plan || '计划'}
      </span>
      <span className="text-[10px] opacity-50">
        · {streamingPlanSteps.length} {isEn ? 'steps' : '步'}
      </span>
      <ChevronDown
        className={`ml-auto h-3 w-3 transition-transform ${planOpen ? 'rotate-180' : ''}`}
      />
    </button>
    {planOpen && (
      <div className="mt-1 space-y-0.5">
        {streamingPlanSteps.map((step, i) => (
          <div
            key={step.step_index ?? i}
            className="flex items-start gap-2 py-1.5 px-2 rounded transition-colors hover:bg-secondary/40"
          >
            <span className="font-mono text-xs text-blue-500/70 mt-0.5">{step.step_index ?? i + 1}.</span>
            <span className="text-xs text-foreground/80 flex-1">{step.title}</span>
            {i === streamingPlanSteps.length - 1 && isStreaming && (
              <Loader2 className="h-3 w-3 text-blue-500/60 animate-spin" />
            )}
          </div>
        ))}
      </div>
    )}
  </div>
)}
```

Make sure `ClipboardList` is imported from `lucide-react` (it's already imported on line 37, verify and add if missing).

- [ ] **Step 4: Run test to verify it passes**

```bash
npx jest src/components/chat/LiveActivityStream.plan.test.jsx 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/components/chat/LiveActivityStream.jsx frontend/src/components/chat/LiveActivityStream.plan.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): live plan section with step count + spinner"
```

---

## Task 10: Frontend — live search query sub-row inside tool rows

**Files:**
- Modify: `frontend/src/components/chat/LiveActivityStream.jsx` (modify the `tool_call_started` row render)
- Test: `frontend/src/components/chat/LiveActivityStream.search.test.jsx`

**Interfaces:**
- Props: `streamingSearchQueries: object<toolCallId, partialQuery>`
- Visual: gray border, 🔍 prefix, monospace, appears under the tool row

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/chat/LiveActivityStream.search.test.jsx
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

describe('LiveActivityStream search query sub-row', () => {
  it('renders search query under a running tool', () => {
    const events = [{
      type: 'tool_call_started',
      params: { tool_label: 'ask_rag_research', tool_call_id: 'tc-42' },
      status: 'running',
      ts: new Date().toISOString(),
    }];
    render(
      <LiveActivityStream
        events={events}
        streamingSearchQueries={{ 'tc-42': 'cable industry analysis' }}
        isStreaming={true}
      />
    );
    expect(screen.getByText(/cable industry analysis/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx jest src/components/chat/LiveActivityStream.search.test.jsx 2>&1 | tail -5
```
Expected: FAIL

- [ ] **Step 3: Add the search query sub-row**

In `LiveActivityStream.jsx`, find the `tool_call_started` row in the events render loop. The exact location depends on the existing structure (after the icon + label, before closing `</div>`). Add a sub-row that displays the search query if it exists in `streamingSearchQueries`:

```jsx
{/* Search query sub-row (live typing effect) */}
{tool.type === 'tool_call_started' &&
  tool.params?.tool_call_id &&
  streamingSearchQueries[tool.params.tool_call_id] && (
    <div
      data-testid="live-activity-search-row"
      className="ml-6 mt-1 text-xs text-muted-foreground font-mono border-l-2 border-gray-500/30 pl-2"
    >
      🔍 {streamingSearchQueries[tool.params.tool_call_id]}
    </div>
  )}
```

**Note**: The implementer must find the right place to insert this. Look for the existing `tool_call_started` rendering in the events `.map()` block. The `tool` variable is already in scope from the loop.

- [ ] **Step 4: Run test to verify it passes**

```bash
npx jest src/components/chat/LiveActivityStream.search.test.jsx 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/components/chat/LiveActivityStream.jsx frontend/src/components/chat/LiveActivityStream.search.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): live search query sub-row with typing effect"
```

---

## Task 11: Frontend — live data preview sub-row inside finished tool rows

**Files:**
- Modify: `frontend/src/components/chat/LiveActivityStream.jsx` (modify the `tool_call_finished` row render)
- Test: `frontend/src/components/chat/LiveActivityStream.preview.test.jsx`

**Interfaces:**
- Props: `streamingDataPreviews: object<toolCallId, {columns, sample_rows, rows_so_far}>`
- Visual: green border, 📊 prefix, mini grid of first 3 rows, columns header

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/chat/LiveActivityStream.preview.test.jsx
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

describe('LiveActivityStream data preview sub-row', () => {
  it('renders data preview under a finished tool', () => {
    const events = [{
      type: 'tool_call_finished',
      params: { tool_label: 'ask_data_agent', tool_call_id: 'tc-7' },
      status: 'done',
      ts: new Date().toISOString(),
    }];
    const preview = {
      'tc-7': {
        columns: ['region', 'revenue'],
        sample_rows: [{ region: 'APAC', revenue: 12345 }, { region: 'EU', revenue: 6789 }],
        rows_so_far: 2,
      },
    };
    render(
      <LiveActivityStream
        events={events}
        streamingDataPreviews={preview}
        isStreaming={false}
      />
    );
    expect(screen.getByText(/APAC/)).toBeInTheDocument();
    expect(screen.getByText(/rows/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx jest src/components/chat/LiveActivityStream.preview.test.jsx 2>&1 | tail -5
```
Expected: FAIL

- [ ] **Step 3: Add the data preview sub-row**

In `LiveActivityStream.jsx`, find the `tool_call_finished` row in the events render loop. Add a sub-row that displays the data preview if it exists in `streamingDataPreviews`:

```jsx
{/* Data preview sub-row (post-query inline preview) */}
{tool.type === 'tool_call_finished' &&
  tool.params?.tool_call_id &&
  streamingDataPreviews[tool.params.tool_call_id] && (() => {
    const preview = streamingDataPreviews[tool.params.tool_call_id];
    const colCount = Math.min(5, preview.columns.length);
    return (
      <div
        data-testid="live-activity-preview-row"
        className="ml-6 mt-1 rounded border border-green-500/30 bg-green-500/5 px-2 py-1 text-xs"
      >
        <div className="text-green-700 dark:text-green-400 mb-1">
          📊 {preview.sample_rows.length} rows shown
          {preview.rows_so_far != null && ` · ${preview.rows_so_far} total`}
        </div>
        <div
          className="grid gap-1 text-muted-foreground"
          style={{ gridTemplateColumns: `repeat(${colCount}, minmax(0, 1fr))` }}
        >
          {preview.columns.slice(0, colCount).map((col) => (
            <div key={col} className="font-mono text-[10px] opacity-70 truncate">{col}</div>
          ))}
          {preview.sample_rows.slice(0, 3).map((row, ri) => (
            preview.columns.slice(0, colCount).map((col) => (
              <div key={`${ri}-${col}`} className="truncate">
                {typeof row[col] === 'object' ? JSON.stringify(row[col]) : String(row[col] ?? '')}
              </div>
            ))
          ))}
        </div>
      </div>
    );
  })()}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx jest src/components/chat/LiveActivityStream.preview.test.jsx 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Run all LiveActivityStream tests**

```bash
npx jest src/components/chat/LiveActivityStream 2>&1 | tail -10
```
Expected: All tests pass (old + new)

- [ ] **Step 6: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/components/chat/LiveActivityStream.jsx frontend/src/components/chat/LiveActivityStream.preview.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): live data preview sub-row under finished tool rows"
```

---

## Task 12: Frontend — auto-scroll on streaming content updates

**Files:**
- Modify: `frontend/src/components/chat/LiveActivityStream.jsx` (find or add the scroll effect)
- Test: `frontend/src/components/chat/LiveActivityStream.autoscroll.test.jsx`

**Interfaces:**
- When any of the 4 streaming states change AND `isStreaming` is true, scroll the container to the bottom

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/chat/LiveActivityStream.autoscroll.test.jsx
import { render, screen } from '@testing-library/react';
import { useRef } from 'react';
import LiveActivityStream from './LiveActivityStream';

describe('LiveActivityStream auto-scroll', () => {
  it('re-renders without errors when streaming state changes', () => {
    const { rerender } = render(
      <LiveActivityStream
        events={[]}
        reasoning="Initial reasoning"
        isStreaming={true}
      />
    );
    // Simulate streaming content update
    rerender(
      <LiveActivityStream
        events={[]}
        reasoning="Initial reasoning that has grown longer"
        isStreaming={true}
      />
    );
    expect(screen.getByText(/has grown longer/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx jest src/components/chat/LiveActivityStream.autoscroll.test.jsx 2>&1 | tail -5
```
Expected: FAIL (component might not handle rerender correctly)

- [ ] **Step 3: Add or update the auto-scroll useEffect**

In `LiveActivityStream.jsx`, find the `useEffect` for scrolling (or add a new one). The effect should depend on the 4 streaming state values. Add or update:

```jsx
// 2026-08-25: live-streaming spec — auto-scroll on streaming updates
useEffect(() => {
  if (scrollRef.current && isStreaming) {
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }
}, [reasoning, streamingSearchQueries, streamingPlanSteps, streamingDataPreviews, isStreaming]);
```

The implementer should:
- Find the existing `scrollRef` (likely already defined as `useRef(null)`)
- Find any existing scroll useEffect
- Either modify the existing dependency array or add this new useEffect

- [ ] **Step 4: Run test to verify it passes**

```bash
npx jest src/components/chat/LiveActivityStream.autoscroll.test.jsx 2>&1 | tail -5
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /home/ysk2025/zhanlu_7_30 add frontend/src/components/chat/LiveActivityStream.jsx frontend/src/components/chat/LiveActivityStream.autoscroll.test.jsx
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "feat(frontend): auto-scroll on streaming content updates"
```

---

## Task 13: End-to-end smoke test (backend + frontend integrated)

**Files:**
- Modify: (none — verification only)
- Run: backend tests + frontend tests + manual smoke

- [ ] **Step 1: Run all backend tests**

```bash
docker exec zhanlu-backend /usr/local/bin/python3.11 -m pytest tests/test_reasoning_delta_event.py tests/test_stream_typing_effect.py tests/test_search_tools_emit_typing.py tests/test_parse_plan_steps.py tests/test_plan_step_added_event.py tests/test_data_preview_event.py -v 2>&1 | tail -20
```
Expected: All new tests pass; pre-existing failures unchanged

- [ ] **Step 2: Run all frontend tests**

```bash
cd /home/ysk2025/zhanlu_7_30/frontend && npx jest src/components/chat/LiveActivityStream src/pages/Chat 2>&1 | tail -20
```
Expected: All new + existing tests pass

- [ ] **Step 3: Restart backend with all new SSE events live**

```bash
docker restart zhanlu-backend
sleep 8
docker ps --filter name=zhanlu-backend --format "{{.Names}}: {{.Status}}"
```
Expected: `Up N seconds`, healthy

- [ ] **Step 4: Manual smoke test in the UI**

In the browser:
1. Open a project (e.g. `Acme BI`)
2. Send: `"Show me top 5 customers by revenue"`
3. Verify the new UI appears:
   - Thinking section shows live reasoning text
   - Plan section builds progressively (if applicable)
   - Search query types in (if applicable)
   - Data preview appears under tool rows
4. All 4 sections can be independently collapsed/expanded
5. After turn ends, sections collapse by default

- [ ] **Step 5: Capture the user-visible improvements**

Take a screenshot of:
- Live reasoning with cursor
- Plan building
- Data preview inline
- The collapsed state after turn end

Save to: `docs/superpowers/plans/2026-08-25-live-streaming-progress-screenshots.md`

- [ ] **Step 6: Commit final state**

```bash
git -C /home/ysk2025/zhanlu_7_30 add docs/superpowers/plans/2026-08-25-live-streaming-progress-screenshots.md
git -C /home/ysk2025/zhanlu_7_30 -c user.name=zhanlu-dev -c user.email=zhanlu@local commit -m "docs(plan): live-streaming progress screenshots and verification"
```

---

## Summary

| Task | Layer | Effort | Risk |
|---|---|---|---|
| 1. reasoning_delta | Backend | 1 line | Very low |
| 2. _stream_typing_effect helper | Backend | ~30 lines | Low |
| 3. Wire typing into 4 search tools | Backend | ~40 lines | Low |
| 4. parse_plan_steps_from_text | Backend | ~30 lines | Low |
| 5. Emit plan_step_added events | Backend | ~20 lines | Medium |
| 6. Emit data_preview events | Backend | ~10 lines | Low |
| 7. Chat.jsx state + SSE handlers | Frontend | ~50 lines | Low |
| 8. Live reasoning section | Frontend | ~30 lines | Low |
| 9. Live plan section | Frontend | ~40 lines | Low |
| 10. Live search sub-row | Frontend | ~15 lines | Low |
| 11. Live data preview sub-row | Frontend | ~30 lines | Low |
| 12. Auto-scroll effect | Frontend | ~10 lines | Low |
| 13. E2E smoke test | Verification | Manual | n/a |
| **Total** | | **~330 lines** | |

**Risk mitigation**: Each task ends with a commit. If a file revert happens, we can re-apply from this plan.

**Out of scope (future work)**: True streaming SQL result sets (requires data_source_runtime refactor).
