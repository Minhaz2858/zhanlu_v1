# P0 — SSE Streaming Protocol + Reasoning Extraction

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend the v3 SSE chat endpoint so clients see the agent's reasoning **as it happens** — by (a) extracting the model's `reasoning_content` (DeepSeek-R1 style) and surfacing it via SSE, and (b) emitting incremental `trace_step` events as tool calls complete instead of only shipping the full trace in the final `done` payload.

**Architecture:** Add new additive SSE event types (`reasoning_done`, `trace_step`) on top of the existing protocol. The protocol envelope stays plain `data: {"type": ..., ...}\n\n` — same shape the v3 endpoint already emits. Reasoning is stored as a separate field on the assistant message (not mixed into `assistant_content`) so context compaction doesn't feed it back to the model.

**Tech Stack:** Python 3 + FastAPI + OpenAI-compatible async client (DeepSeek), React + Vite + Vitest frontend. No new backend or frontend dependencies. AST-based targeted pytest runs (RAM constraint established earlier today). The `_derive_trace_from_response` step shape is reused verbatim so the existing `ReasoningSummary` component needs no schema change.

---

## Status snapshot (verified 2026-07-21)

- [x] v3 `add_message_stream` endpoint exists at `backend/app/routers/agents.py:3036`
- [x] v3 streaming uses non-streaming `_call_llm_with_tools` (line 2841) — `llm_response` is the full response, captured to `assistant_content` at line 3362
- [x] Final LLM call returns `{content, tool_calls}` from `data["choices"][0]["message"]` (line 2906-2915)
- [x] Five existing SSE event types: `delta`, `tool_progress`, `paused`, `error`, `done`
- [x] `_derive_trace_from_response` at `backend/app/routers/agents.py:791` — returns the `{step, type, title, detail, status, duration_ms}` shape; called once at line 3952 before the `done` event
- [x] `assistant_msg["trace"]` is attached at line 3952, then re-shipped in the `done` payload (line 3991)
- [x] Marker-stripping `_strip_markers` runs on `assistant_content` at line 3922, then `delta` yields the stripped text (line 3929) — invariant we must preserve
- [x] Orphaned streaming helper `_stream_llm_final_response` at lines 2966-3032 — NOT called from `add_message_stream` (DSML-leak fix), kept for future streaming migration
- [x] `app/services/llm_service.py` is the shared LLM calling utility — already imported by `agents.py:35` as `from app.services.llm_service import llm_headers, llm_url, get_model`
- [x] v2 `add_message` is at `agents.py:1231` — NOT a streaming endpoint, returns the full response in one shot. Untouched by this plan.
- [x] Frontend SSE parser `agentEnhanced.js::streamAgentResponse` is a transparent pass-through — it just `yield`s each `JSON.parse(jsonStr)` event. Event filtering happens in each consumer.
- [x] Frontend SSE consumers (all three filter on `event.type` directly):
  - `frontend/src/pages/Chat.jsx:553` (uses `streamAgentResponse`)
  - `frontend/src/pages/SkillAgent.jsx:205`
  - `frontend/src/hooks/useAgentBuilder.js:268`
- [x] `ReasoningSummary` frontend component consumes the existing trace step shape

## Real gaps this plan closes

1. **No reasoning extraction** — provider `reasoning_content` (DeepSeek-R1) is dropped; reasoning is not visible to users, only the final text.
2. **No reasoning surfaced via SSE** — even though DeepSeek-R1 returns reasoning in the non-streaming response's `message.reasoning_content`, the v3 endpoint ignores it.
3. **Trace is post-hoc** — `_derive_trace_from_response` runs once after the loop, so the user gets all trace steps at `done`, not as they happen.
4. **No protocol room for FSM-in-SSE** — flagged as a separate follow-up; we reserve `fsm_state` event type in the protocol doc but don't implement it.

## Architecture reality check (v3 streaming is non-token-by-token today)

Today's v3 endpoint calls `_call_llm_with_tools` (line 2841), which is **non-streaming** httpx. The final assistant text is captured in `assistant_content` and emitted as a SINGLE `delta` event at line 3929. There is no per-token stream from LLM to user. The orphaned `_stream_llm_final_response` helper (line 2966) is NOT called from `add_message_stream` — its use was removed by the DSML-leak fix (test `test_v3_streaming_no_dsml_leak.py`).

Implication: **we cannot stream `reasoning_delta` tokens in v3 today without a major refactor** (switching the LLM call to streaming + replaying the non-streaming fallback path). For the P0 deliverable, we surface reasoning as a single `reasoning_done` event captured at the message level, and we surface trace steps incrementally as `trace_step` events. This is a significant UX improvement with minimal blast radius.

If the user wants true token-level reasoning streaming, that's a separate Phase 8 that requires re-introducing `_stream_llm_final_response` carefully (with `tools` passed explicitly, not `None`).

## Event protocol (additive — no existing event shape changes)

```jsonc
// New events emitted by add_message_stream
{"type": "reasoning_done", "reasoning": "<full text>", "step_count": N}
{"type": "trace_step", "step": {"step": N, "type": "...", "title": "...", "detail": "...", "status": "...", "duration_ms": N}}

// Reserved for future use (NOT emitted in this plan)
{"type": "fsm_state", "state": "ANALYZE", "data": {...}}
```

**Backward-compat invariant:** existing `delta`, `tool_progress`, `paused`, `error`, `done` payloads keep their current JSON shapes. Old clients ignore unknown `"type"` values.

---

## Phase 1 — Backend: reasoning extraction helper (TDD)

### Task 1: Add `extract_stream_parts` in `llm_service.py`

**Files:**
- Modify: `backend/app/services/llm_service.py`
- Create: `backend/tests/test_v3_reasoning_extraction.py`

**Step 1: Write the failing test** (already done in this commit; see `test_v3_reasoning_extraction.py`).

**Step 2: Run the test — verify it fails**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_reasoning_extraction.py -v
```

Expected: 12 failed (one passed: `test_add_message_stream_exists` because the function exists). All other tests fail with `ImportError: cannot import name 'extract_stream_parts'`.

**Step 3: Implement minimal `extract_stream_parts`**

Add to `backend/app/services/llm_service.py` (anywhere after the module docstring):

```python
def extract_stream_parts(chunk: dict) -> tuple[str, str]:
    """Return (content_delta, reasoning_delta) from a provider stream chunk.

    Tolerant of multiple provider formats. Returns ("", "") when the chunk
    has neither content nor reasoning. Pure function — safe in any async ctx.

    Supported reasoning field names (in order of preference):
      - delta.reasoning_content (DeepSeek-R1)
      - delta.thinking         (Claude-style)
      - delta.reasoning        (OpenAI o1)
    """
    try:
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
    except (IndexError, AttributeError, TypeError):
        return ("", "")

    content = delta.get("content") or ""
    reasoning = (
        delta.get("reasoning_content")
        or delta.get("thinking")
        or delta.get("reasoning")
        or ""
    )
    # Normalize to str — providers may yield None for empty fields
    if content is None:
        content = ""
    if reasoning is None:
        reasoning = ""
    return (str(content), str(reasoning))
```

**Step 4: Run the test — verify it passes**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_reasoning_extraction.py -v
```

Expected: 8/13 pass (the 7 extract_stream_parts tests + 1 add_message_stream_exists). The 5 remaining failures are the agents.py structural tests — those land in Task 2.

**Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/llm_service.py backend/tests/test_v3_reasoning_extraction.py && git commit -m "feat(llm): add extract_stream_parts for provider reasoning content"
```

Status: ✅ committed (commit `37dd886`).

---

## Phase 2 — Backend: wire new SSE events into `add_message_stream` + non-streaming reasoning extraction

### Task 2: Emit `reasoning_done` and `trace_step` events

**Files:**
- Modify: `backend/app/routers/agents.py` — three change sites:
  1. Line 35 import: add `extract_stream_parts`
  2. Line 2912-2915 (`_call_llm_with_tools` return): include `reasoning_content` from the message in the returned dict
  3. Line 2966-3032 (`_stream_llm_final_response`): use `extract_stream_parts` for chunk content extraction (preparatory, not called from v3 today)
  4. Line 3362 area (`add_message_stream` post-loop): capture reasoning from `llm_response`, emit `reasoning_done`, persist on `assistant_msg`
  5. First `tool_calls_for_frontend.append(...)` site in `add_message_stream`: emit `trace_step` after the append
- Modify: `backend/tests/test_v3_reasoning_extraction.py` (already has 5 failing structural tests)

**Step 1: Verify the test file's 5 structural tests fail**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_reasoning_extraction.py -v
```

Expected: 5 failed: `test_add_message_stream_yields_reasoning_delta`, `test_add_message_stream_yields_trace_step`, `test_add_message_stream_yields_reasoning_done`, `test_add_message_stream_imports_extract_stream_parts`, `test_add_message_stream_persists_reasoning_on_assistant_msg`.

**Step 2: Wire the import**

In `backend/app/routers/agents.py` line 35, change:

```python
from app.services.llm_service import llm_headers, llm_url, get_model
```

to:

```python
from app.services.llm_service import llm_headers, llm_url, get_model, extract_stream_parts
```

**Step 3: Add `reasoning` to `_call_llm_with_tools` return**

In `backend/app/routers/agents.py` at line 2912-2915, change:

```python
    return {
        "content": content,
        "tool_calls": raw_tool_calls,
    }
```

to:

```python
    reasoning = message.get("reasoning_content") or ""
    return {
        "content": content,
        "tool_calls": raw_tool_calls,
        "reasoning": reasoning or "",
    }
```

**Step 4: Update `_stream_llm_final_response` to use `extract_stream_parts`** (preparatory)

In `backend/app/routers/agents.py` at line 3011-3021, change the chunk-content extraction block from:

```python
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        full_content += token
                        yield ("delta", token)
                    tc = delta.get("tool_calls")
                    if tc:
                        raw_tool_calls = tc
```

to:

```python
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    # P0: extract both content and reasoning in one call
                    content_delta, reasoning_delta = extract_stream_parts(chunk)
                    if content_delta:
                        full_content += content_delta
                        yield ("delta", content_delta)
                    if reasoning_delta:
                        yield ("reasoning_delta", reasoning_delta)
                    # Preserve tool_calls detection for the post-loop response
                    tc = choices[0].get("delta", {}).get("tool_calls")
                    if tc:
                        raw_tool_calls = tc
```

(Not exercised by `add_message_stream` today — DSML-leak fix removed the call. This change is preparatory for the Phase 8 streaming migration.)

**Step 5: Emit `reasoning_done` + persist on `assistant_msg` in `add_message_stream`**

Find the line where `assistant_content = llm_response.get("content", "")` is set in `add_message_stream` (around line 3362). Immediately after, add:

```python
            # P0: surface reasoning captured from the LLM message.
            # The non-streaming _call_llm_with_tools puts DeepSeek-R1's
            # reasoning_content at message.reasoning_content; we forward
            # it as a single 'reasoning_done' SSE event and persist it on
            # the assistant message.
            # NOTE: 'fsm_state' event type is reserved for the future SynexiaFSM-in-SSE
            # follow-up. It will share this envelope; the FSM design is separate.
            final_reasoning = llm_response.get("reasoning", "") or ""
            assistant_msg["reasoning"] = final_reasoning
            if final_reasoning:
                yield f'data: {json.dumps({"type": "reasoning_done", "reasoning": final_reasoning, "step_count": len(tool_calls_for_frontend)})}\n\n'
```

**Step 6: Emit `trace_step` after the first `tool_calls_for_frontend.append(...)` site**

Find the first `tool_calls_for_frontend.append(...)` site in `add_message_stream` (one of the lines around 3461/3481). Right after the append, insert:

```python
                # P0: incremental trace_step — reuses the same step shape as
                # _derive_trace_from_response so the frontend
                # ReasoningSummary component needs no schema change.
                if tool_calls_for_frontend:
                    last_tc = tool_calls_for_frontend[-1]
                    display_proj = last_tc.get("display_projection") or {}
                    if display_proj.get("hide_details"):
                        step_title = display_proj.get("label", last_tc.get("name", ""))
                        step_detail = display_proj.get("done_label", "")
                    else:
                        step_title = last_tc.get("name", "")
                        results = last_tc.get("results")
                        if isinstance(results, dict):
                            step_detail = str(results.get("summary") or results.get("text") or "")[:200]
                        elif isinstance(results, str):
                            step_detail = results[:200]
                        else:
                            step_detail = ""
                    step = {
                        "step": len(tool_calls_for_frontend),
                        "type": "tool_call",
                        "title": step_title,
                        "detail": step_detail,
                        "status": last_tc.get("status", "completed"),
                        "duration_ms": int(last_tc.get("duration_ms") or 0),
                    }
                    yield f'data: {json.dumps({"type": "trace_step", "step": step})}\n\n'
```

**Step 7: Run the test — verify all 13 pass**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_reasoning_extraction.py -v
```

Expected: 13/13 pass in <1s.

**Step 8: Re-run marker-strip test to confirm no regression**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_add_message_stream_marker.py tests/test_v3_streaming_no_dsml_leak.py -v
```

Expected: all green (existing tests still pass).

**Step 9: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/agents.py backend/tests/test_v3_reasoning_extraction.py && git commit -m "feat(chat): surface reasoning_done and trace_step events on v3 SSE"
```

---

## Phase 3 — Backend: extend marker and no-leak tests for reasoning isolation

### Task 3: Reasoning never enters `delta.content`; marker parser is not triggered by reasoning

**Files:**
- Modify: `backend/tests/test_v3_streaming_no_dsml_leak.py`
- Modify: `backend/tests/test_add_message_stream_marker.py`

**Step 1: Add a case to `test_v3_streaming_no_dsml_leak.py`**

Append:

```python
def test_reasoning_event_does_not_pass_through_strip_markers():
    """The reasoning_done event must be emitted as-is, with no _strip_markers
    call. Reasoning is not user-visible artifact text and must never go through
    the marker parser."""
    import ast
    AGENTS = (pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    tree = ast.parse(AGENTS)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_message_stream":
            fn = node
            break
    assert fn is not None
    body = ast.unparse(fn)
    # The reasoning_done event must be present
    assert '"reasoning_done"' in body
    # Look at the 200 chars after the reasoning_done yield for _strip_markers
    rd_idx = body.find('"reasoning_done"')
    window = body[rd_idx:rd_idx + 300]
    assert "_strip_markers(" not in window, (
        "reasoning_done payload must NOT be processed by _strip_markers"
    )
```

**Step 2: Add a case to `test_add_message_stream_marker.py`**

Append:

```python
def test_reasoning_text_not_written_into_content():
    """assistant_msg['reasoning'] is a separate key from assistant_msg['content']
    — reasoning must not be appended into content (which would pollute the
    model context on compaction)."""
    import ast
    AGENTS = (pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    tree = ast.parse(AGENTS)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_message_stream":
            fn = node
            break
    assert fn is not None
    body = ast.unparse(fn)
    # Find the assistant_msg["reasoning"] = ... line
    assert '"reasoning"' in body
    # The reasoning assignment must NOT touch assistant_content or content
    # (it must be a separate key, not appended)
    rd_idx = body.find('"reasoning"')
    window = body[rd_idx:rd_idx + 200]
    # No + or += operators on content in the same window
    assert "content +=" not in window and "assistant_content +=" not in window, (
        "reasoning must not be appended into assistant_content"
    )
```

**Step 3: Run the tests — verify they pass**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_streaming_no_dsml_leak.py tests/test_add_message_stream_marker.py -v
```

Expected: all pass (existing + 2 new).

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/tests/test_v3_streaming_no_dsml_leak.py backend/tests/test_add_message_stream_marker.py && git commit -m "test(sse): verify reasoning stays separate from content"
```

---

## Phase 4 — Two-stage backend review

### Task 4: Code-reviewer subagent on `agents.py` SSE wiring

**Step 1: Spawn code-reviewer**

Use the `code-reviewer` subagent with the latest commit's diff:

```text
Review the diff of HEAD~1..HEAD in /root/zhanlu (feat: surface reasoning_done and trace_step events on v3 SSE).

Check:
1. Spec compliance: are delta / tool_progress / paused / error / done event shapes unchanged?
2. Is the new reasoning field in _call_llm_with_tools return value backward-compatible?
3. Is reasoning emitted on a SEPARATE event type (never mixed into delta.content)?
4. Is reasoning persisted on assistant_msg as a separate key (not mixed into content)?
5. Are trace_step events shaped exactly like _derive_trace_from_response returns?
6. Any risk that reasoning content could leak into the marker parser?
7. Is the fsm_state reservation comment present?
8. Is _stream_llm_final_response still unused (DSML-leak test still passes)?

Return: ✅ pass / 🟡 minor findings / 🔴 blockers.
```

**Step 2: Apply or document any findings**

If 🟡: fix inline. If 🔴: stop, write findings to this plan doc's "Post-review notes" section, and ask the user before continuing.

**Step 3: Commit fixes if any**

```bash
cd /root/zhanlu && git add -A && git commit -m "fix(sse): address code-reviewer findings on reasoning extraction"
```

(only if fixes were needed; otherwise skip this commit)

---

## Phase 5 — Frontend: each consumer adds the new event cases

### Task 5: Add `reasoning_done` / `trace_step` cases in each consumer

**Files:**
- (no change to `frontend/src/api/agentEnhanced.js` — its `streamAgentResponse` is a transparent pass-through that just `yield`s each `JSON.parse(...)` event)
- Modify: `frontend/src/pages/Chat.jsx` (consume the new events in the `for await (const evt of stream)` loop)
- Modify: `frontend/src/pages/SkillAgent.jsx` (same)
- Modify: `frontend/src/hooks/useAgentBuilder.js` (same)

**Step 1: In `Chat.jsx`**

(a) Add two new state vars near `fullContent`/`toolCalls`/`activitySteps` (around line 549-551):

```js
let reasoningText = '';
let liveTraceSteps = [];
```

(b) In the `for await (const evt of stream)` loop (line 562), add three new `if/else if` branches alongside the existing `evt.type === 'tool_progress'` / `'activity_step'` / `'delta'` / `'done'` cases:

```js
        if (evt.type === 'reasoning_done' && typeof evt.reasoning === 'string') {
          reasoningText = evt.reasoning;
          // Persist on the assistant message so the rail survives across re-renders
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id ? { ...m, reasoning: reasoningText } : m
          ));
        } else if (evt.type === 'trace_step' && evt.step) {
          liveTraceSteps.push(evt.step);
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id ? { ...m, trace_steps: liveTraceSteps } : m
          ));
        }
```

(c) On `evt.type === 'done'`, persist the final `liveTraceSteps`:

```js
        if (evt.type === 'done') {
          // ... existing done handling ...
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id
              ? { ...m, reasoning: reasoningText, trace_steps: liveTraceSteps }
              : m
          ));
        }
```

Use the existing `aiMsg.id` — confirm it's in scope at the `for await` loop (it is; set a few lines above the loop).

**Step 2: Mirror in `SkillAgent.jsx` (line 205)**

Apply the same three edits ((a)/(b)/(c)) to the `for await (const event of streamAgentResponse(...))` loop. Use the local message state shape.

**Step 3: Mirror in `useAgentBuilder.js` (line 268)**

Apply the same three edits to its `for await (const event of streamAgentResponse(...))` loop. The hook already tracks `streamingContentRef.current` — add a `reasoningRef.current` and a `traceStepsRef.current` array, mirror the same `if/else if` pattern, and call the local state setter on `reasoning_done` and `trace_step` for live rail rendering.

**Step 4: Run frontend tests**

```bash
cd /root/zhanlu/frontend && npx vitest run --reporter=basic
```

Expected: all existing tests pass; no regressions in the 9 affected frontend tests from the artifact-skills plan.

**Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/Chat.jsx frontend/src/pages/SkillAgent.jsx frontend/src/hooks/useAgentBuilder.js && git commit -m "feat(ui): consume reasoning_done and trace_step SSE events"
```

**Note on UI rendering**: Phase 6 (below) is where the live rail and the post-hoc trace are wired to the React render. This task only changes the **parsing/accumulation** side. The `reasoning` and `trace_steps` fields land on the message object; the next task renders them.

---

## Phase 6 — Frontend: render the live reasoning rail and the incremental trace

### Task 6: Render `reasoning` and `trace_steps` in the assistant bubble

**Files:**
- Modify: `frontend/src/pages/Chat.jsx` (assistant bubble render)
- Modify: `frontend/src/pages/SkillAgent.jsx` (mirror)
- Modify: `frontend/src/components/ReasoningSummary.jsx` if it doesn't already accept a `steps` prop

**Step 1: Locate the assistant-message render in each file**

- `Chat.jsx`: find where assistant messages are rendered (search for `role === 'assistant'` and the bubble JSX).
- `SkillAgent.jsx`: same.

**Step 2: Add the live reasoning rail**

Above the assistant bubble's main content, add a collapsible/monospace rail when `m.reasoning` is non-empty:

```jsx
{m.role === 'assistant' && m.reasoning && (
  <details className="text-xs text-muted-foreground mb-2">
    <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wide opacity-70">
      Reasoning ({m.reasoning.length} chars)
    </summary>
    <pre className="font-mono bg-muted/30 px-3 py-2 rounded mt-1 whitespace-pre-wrap max-h-48 overflow-y-auto">
      {m.reasoning}
    </pre>
  </details>
)}
```

**Step 3: Pass `trace_steps` to the existing `ReasoningSummary`**

If `ReasoningSummary` already accepts a `steps` prop (confirmed in today's pre-reads), pass `m.trace_steps || m.trace || []`. If it doesn't, add the prop (with default `[]`).

```jsx
<ReasoningSummary steps={m.trace_steps && m.trace_steps.length ? m.trace_steps : (m.trace || [])} />
```

**Step 4: Run frontend tests**

```bash
cd /root/zhanlu/frontend && npx vitest run --reporter=basic
```

Expected: all pass; the 9 affected frontend tests from the artifact-skills plan remain green.

**Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/Chat.jsx frontend/src/pages/SkillAgent.jsx frontend/src/components/ReasoningSummary.jsx && git commit -m "feat(ui): render reasoning rail and incremental trace steps"
```

---

## Phase 7 — Final verification

### Task 7: Run all targeted tests + summarize

**Step 1: Backend targeted tests**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -m pytest tests/test_v3_reasoning_extraction.py tests/test_v3_streaming_no_dsml_leak.py tests/test_add_message_stream_marker.py -v
```

Expected: all pass (full count visible in output).

**Step 2: Frontend tests**

```bash
cd /root/zhanlu/frontend && npx vitest run --reporter=basic
```

Expected: all pass.

**Step 3: Module import smoke check (mirrors today's earlier smoke pattern)**

```bash
cd /root/zhanlu/backend && PYTHONPATH=. python3 -c "
from app.services.llm_service import extract_stream_parts
from app.routers import agents
import inspect
src = inspect.getsource(agents.add_message_stream)
for ev in ['reasoning_done', 'trace_step']:
    assert ev in src, f'missing event: {ev}'
v2_src = inspect.getsource(agents.add_message)
for ev in ['reasoning_done', 'trace_step']:
    assert ev not in v2_src, f'v2 must NOT emit {ev} (it is not streaming)'
print('SMOKE OK: all events present in v3, absent from v2')
"
```

**Step 4: Memory checkpoint**

Append a note to `/root/.codebuddy/memory/2026-07-21.md` summarizing:
- Commits landed
- Tests passed (with exact counts)
- Any deviations from this plan
- Notes for next session

**Step 5: Final summary commit (docs only, no code change)**

```bash
cd /root/zhanlu && git add docs/plans/2026-07-21-p0-sse-reasoning.md && git commit -m "docs: P0 SSE streaming + reasoning extraction plan complete"
```

---

## Acceptance criteria (encoded from the plan-level checklist)

- [ ] New SSE event types emitted by v3 `add_message_stream`: `reasoning_done`, `trace_step`. All additive — existing event shapes unchanged.
- [ ] `_call_llm_with_tools` returns `reasoning` field from DeepSeek-R1 `message.reasoning_content`.
- [ ] `assistant_content` in the DB does NOT contain reasoning text; reasoning is on `assistant_msg["reasoning"]`.
- [ ] `extract_stream_parts` handles DeepSeek `reasoning_content`, Claude `thinking`, OpenAI o1 `reasoning`; no-ops gracefully when missing.
- [ ] `_stream_llm_final_response` uses `extract_stream_parts` for chunk content (preparatory for future streaming migration).
- [ ] Frontend accumulates `reasoning` and `trace_steps` on the assistant message; renders reasoning rail + `ReasoningSummary` (no schema change).
- [ ] Marker-stripping invariant preserved: `_strip_markers` still runs on `assistant_content`; reasoning chunks are NEVER passed through it.
- [ ] All new tests are AST-based and pass under targeted pytest; no full `pytest` run; no regression in the 41 backend + 9 frontend tests from the artifact-skills plan.
- [ ] One commit per logical task.
- [ ] `fsm_state` event type reserved in comments (NOT implemented — separate follow-up).

---

## Post-review notes

(filled in after Task 4 by the code-reviewer subagent)
