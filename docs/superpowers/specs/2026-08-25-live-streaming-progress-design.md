# Live-Streaming Agent Progress — Design Spec

**Date**: 2026-08-25
**Status**: Approved (all 6 sections reviewed)
**Author**: brainstorming session
**Project**: zhanlu_7_30 (backend + frontend)

---

## Problem

The zhanlu agent's chat response takes 60-228 seconds for complex queries. The current UI only shows high-level phase labels (`Fabricating`, `Building deliverable`, `Querying data source`) with timings appearing all at once at the end. The user feels the agent is "hung" or unresponsive, even when it's actively working.

Modern AI agents (Kimi K2, Claude 3.7, Perplexity) solve this with **live-streaming progress**: the user sees what the agent is doing RIGHT NOW as it happens — reasoning streaming, plan building, search query typing, results previewing.

Reference: Perplexity's live progress UI (collapsible sections, character-by-character typing, live search).

---

## Goal

Add 4 live-streaming capabilities to zhanlu's chat UI, matching the look-and-feel of Kimi/Claude/Perplexity, adapted to zhanlu's existing visual design language:

1. **Live reasoning/thinking streaming** — chain-of-thought streams token-by-token
2. **Live search query typing** — search query appears character-by-character (simulated typing)
3. **Live plan formation** — plan steps appear as the agent decides them
4. **Live data preview** — query results show immediately when a tool completes

**Non-goals**: True streaming SQL result sets (requires data_source_runtime refactor — out of scope).

---

## Architecture

The system already has a typed **live-event system** (`_push_live_event` + `_LIVE_EVENT_CAP=100`) consumed by `LiveActivityStream`. We extend it additively.

**Key insight**: `_LIVE_EVENT_CAP=100` is too low for per-token streaming (qwen3-27b produces 500-3000 reasoning tokens). So we add **dedicated delta events** (not subject to the cap) for character-level streaming, while keeping the existing `live_event` system for higher-level events.

```
Backend                       SSE                          Frontend
─────────────────────────────────────────────────────────────────────
LLM tokens                  ─► reasoning_delta       ─►  LiveActivityStream
                                                            ReasoningPanel
Search query build          ─► search_query_delta    ─►  (extend existing)
Plan step detected          ─► plan_step_added       ─►
Tool result ready           ─► data_preview          ─►
```

---

## Section 1: Backend SSE Event Schema

### New event types (NOT subject to `_LIVE_EVENT_CAP`)

| Event | When | Payload | Throttle |
|---|---|---|---|
| `reasoning_delta` | Each reasoning token from LLM | `{content: str}` | None — every token |
| `search_query_delta` | When a search/web tool builds a query | `{tool_call_id: str, partial: str}` | 25ms between chunks (~300ms total) |
| `plan_step_added` | When orchestrator adds a plan step | `{step_index: int, title: str}` | Per step |
| `data_preview` | When a tool returns partial rows | `{tool_call_id: str, columns: list, sample_rows: list, rows_so_far: int}` | Per tool result |

### Existing event types stay unchanged

- `live_event` (with `_LIVE_EVENT_CAP=100`) for `phase_enter`, `tool_call_started`, etc.
- `delta`, `content_replace`, `done` for final answer streaming
- `plan_summary` (existing) — keep for backward compat
- `reasoning_done` (existing) — keep for persistence

---

## Section 2: Implementation Feasibility

### `reasoning_delta` — EASY ✅

**Source**: `_stream_llm_with_tools` already yields `("reasoning", ev_data)` per token. Currently only stored in `final_reasoning`, never sent to client.

**Change**: 1 line — yield each token as `reasoning_delta`.

```python
# agents.py line ~10314
elif ev_type == "reasoning":
    final_reasoning += ev_data
    yield f'data: {json.dumps({"type": "reasoning_delta", "content": ev_data})}\n\n'
```

**Effort**: 1 line. Ships 100% reliably.

### `search_query_delta` — MEDIUM ⚠️

**Source**: Search tools (`_web_search`, `_ask_rag_research`, `_search_documents`) take a query string built from the LLM's tool_call arguments. The query is fully formed before the tool runs.

**Pragmatic approach**: When the LLM emits a search tool call, simulate typing by chunking the query string over ~300ms.

```python
async def _stream_typing_effect(query: str, tool_call_id: str):
    chunk_size = max(1, len(query) // 12)
    for i in range(0, len(query), chunk_size):
        yield f'data: {json.dumps({"type": "search_query_delta", "tool_call_id": tool_call_id, "partial": query[:i+chunk_size]})}\n\n'
        await asyncio.sleep(0.025)
```

**Effort**: 1 helper + 4 callsites.

### `plan_step_added` — MEDIUM ⚠️

**Source**: LLM writes a plan in its response (`1. ...`, `2. ...`, `### Step 1`). Currently emitted as `plan_summary` once at the end (line 9077).

**Pragmatic approach**: A small parser watches streaming text for numbered list items or `### Step N:` patterns.

```python
_PLAN_STEP_RE = re.compile(
    r'(?m)^(?:(\d+)\.|[-*])\s+(.+)$|^(?:Step|步骤)\s+(\d+)\s*[:：]\s*(.+)$'
)
```

**Effort**: ~30 lines. Works ~80% reliably (heuristic).

### `data_preview` — POST-QUERY (not true streaming)

**Source**: SQL queries return all rows at once. `data_source_runtime/` has no streaming API.

**Pragmatic approach**: Wire existing `_sse_data_offer` machinery to fire IMMEDIATELY when SQL tool returns (instead of waiting for synthesis step).

**Effort**: ~20 lines. Not true live streaming during query, but results appear within ~1s of query completion.

---

## Section 3: Frontend Changes

### State in `Chat.jsx`

```javascript
// Streaming content state (lives in Chat.jsx so SSE handlers can update it)
const [streamingReasoning, setStreamingReasoning] = useState('');
const [streamingSearchQueries, setStreamingSearchQueries] = useState({});
const [streamingPlanSteps, setStreamingPlanSteps] = useState([]);
const [streamingDataPreviews, setStreamingDataPreviews] = useState({});
```

> **Note**: Collapse/open state is owned by `LiveActivityStream` (per-section `useState`), not `Chat.jsx`. The streaming content is owned by `Chat.jsx` and passed down as props.

### SSE event handlers

```javascript
case 'reasoning_delta':
  setStreamingReasoning(prev => prev + (data.content || ''));
  break;
case 'search_query_delta':
  setStreamingSearchQueries(prev => ({...prev, [data.tool_call_id]: data.partial}));
  break;
case 'plan_step_added':
  setStreamingPlanSteps(prev => [...prev, data.title]);
  break;
case 'data_preview':
  setStreamingDataPreviews(prev => ({
    ...prev, [data.tool_call_id]: {columns: data.columns, sample_rows: data.sample_rows}
  }));
  break;
```

### UI rendering in `LiveActivityStream.jsx`

Each section is **independently collapsible** (Kimi/Claude behavior):
- Default **open** while streaming
- Default **collapsed** once turn ends
- User can re-expand to review

**Per-section collapse state** (defined inside `LiveActivityStream` component, not lifted to `Chat.jsx`):

```jsx
const [reasoningOpen, setReasoningOpen] = useState(true);
const [searchOpen, setSearchOpen] = useState(true);
const [planOpen, setPlanOpen] = useState(true);
const [dataPreviewOpen, setDataPreviewOpen] = useState(true);
```

When the turn ends (SSE `done` event), call `setAllSectionsOpen(false)` to collapse all 4 by default.

#### Live reasoning (replace static block at line 545-568)

```jsx
<div className="border-t border-purple-500/30 bg-purple-500/5 px-3 py-2">
  <button onClick={() => setReasoningOpen(v => !v)}>
    <Brain className="h-3 w-3 text-purple-500/80" />
    <span>Thinking · {streamingReasoning.length.toLocaleString()} chars</span>
    <ChevronDown className={`h-3 w-3 transition-transform ${reasoningOpen ? 'rotate-180' : ''}`} />
  </button>
  {reasoningOpen && (
    <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded bg-secondary/30 px-3 py-2 font-mono text-xs text-muted-foreground">
      {streamingReasoning}
      {isStreaming && <span className="animate-pulse text-purple-400">▍</span>}
    </pre>
  )}
</div>
```

#### Live search query (sub-row inside `tool_call_started`)

```jsx
{tool.type === 'tool_call_started' && streamingSearchQueries[tool.params?.tool_call_id] && (
  <div className="ml-6 mt-1 text-xs text-muted-foreground font-mono border-l-2 border-gray-500/30 pl-2">
    🔍 {streamingSearchQueries[tool.params.tool_call_id]}
  </div>
)}
```

#### Live plan (replace existing `plan_summary` row)

```jsx
<div className="border-t border-blue-500/30 bg-blue-500/5 px-3 py-2">
  <button onClick={() => setPlanOpen(v => !v)}>
    <ClipboardList className="h-3 w-3 text-blue-500/80" />
    <span>Plan · {streamingPlanSteps.length} steps</span>
    <ChevronDown />
  </button>
  {planOpen && streamingPlanSteps.map((step, i) => (
    <div key={i} className="flex items-start gap-2 py-1.5 px-2 rounded hover:bg-secondary/40 transition-colors">
      <span className="font-mono text-xs text-blue-500/70 mt-0.5">{i+1}.</span>
      <span className="text-xs text-foreground/80 flex-1">{step}</span>
      {i === streamingPlanSteps.length - 1 && isStreaming && (
        <Loader2 className="h-3 w-3 text-blue-500/60 animate-spin" />
      )}
    </div>
  ))}
</div>
```

#### Live data preview (inline with `tool_call_finished`)

```jsx
{tool.type === 'tool_call_finished' && streamingDataPreviews[tool.params?.tool_call_id] && (
  <div className="ml-6 mt-1 rounded border border-green-500/30 bg-green-500/5 px-2 py-1 text-xs">
    <div className="text-green-700 dark:text-green-400 mb-1">
      📊 {streamingDataPreviews[tool.params.tool_call_id].sample_rows.length} rows shown · {streamingDataPreviews[tool.params.tool_call_id].rows_so_far ?? '?'} total
    </div>
    <div className={`grid grid-cols-${Math.min(5, streamingDataPreviews[tool.params.tool_call_id].columns.length)} gap-1 text-muted-foreground`}>
      {streamingDataPreviews[tool.params.tool_call_id].sample_rows.slice(0, 3).map((row, i) => (
        <div key={i} className="truncate">{JSON.stringify(row).slice(0, 50)}</div>
      ))}
    </div>
  </div>
)}
```

### Auto-scroll

```jsx
useEffect(() => {
  if (scrollRef.current && isStreaming) {
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }
}, [streamingReasoning, streamingSearchQueries, streamingPlanSteps, streamingDataPreviews]);
```

---

## Section 4: Modern UX Polish (Kimi/Claude-tier)

### Color tokens (reuse zhanlu palette)

| Section | Border | Background |
|---|---|---|
| Reasoning | `border-purple-500/30` | `bg-purple-500/5` |
| Plan | `border-blue-500/30` | `bg-blue-500/5` |
| Search | `border-gray-500/30` | `bg-gray-500/5` |
| Data preview | `border-green-500/30` | `bg-green-500/5` |

### Animations (CSS, no library)

- `transition-all duration-200 ease-out` for section open/close
- `animate-pulse text-purple-400` for live cursor
- `data-state="open/closed"` for chevron rotation

### Section header format

Kimi/Claude style: icon + name + char count + chevron
- "Thinking · 1,234 chars"
- "Plan · 3 steps"
- "Search query · 45 chars"

### What we are NOT doing (scope discipline)

- ❌ Markdown rendering inside reasoning (Kimi/Claude use plain monospace)
- ❌ Syntax highlighting on data preview cells
- ❌ Voice playback
- ❌ Citation linkification (separate feature)
- ❌ True streaming SQL (requires runtime refactor)

---

## Section 5: Data Flow & Lifecycle

### Timeline for a single turn

```
T+0.0s   User sends message
         └─► Reset all 4 streaming states
         └─► Open SSE connection

T+0.1s   Orchestrator begins
         └─► LLM streams reasoning → reasoning_delta events
         └─► UI: thinking section grows

T+1.2s   Plan detected
         └─► plan_step_added events
         └─► UI: plan section grows

T+2.5s   LLM calls ask_rag_research
         └─► typing effect (300ms) → search_query_delta events
         └─► search executes → data_preview event
         └─► UI: query types in, then preview appears

T+25s    Sub-agent completes (httpx 25s cap)
T+60s    Wall-clock cap fires (from earlier fix)
T+60-90s Synthesis LLM runs (qwen3, 60s cap)
T+90s    Final answer streams
         └─► All 4 sections collapse by default
         └─► User can re-expand to review
```

### State lifecycle

| State | Owned by | Created | Updated | Reset |
|---|---|---|---|---|
| `streamingReasoning` | `Chat.jsx` | On new turn | On `reasoning_delta` | On new turn |
| `streamingSearchQueries` | `Chat.jsx` | On new turn | On `search_query_delta` | On new turn |
| `streamingPlanSteps` | `Chat.jsx` | On new turn | On `plan_step_added` | On new turn |
| `streamingDataPreviews` | `Chat.jsx` | On new turn | On `data_preview` | On new turn |
| `reasoningOpen` | `LiveActivityStream` | `useState(true)` | On user click | `setAllSectionsOpen(false)` on `done` |
| `searchOpen` | `LiveActivityStream` | `useState(true)` | On user click | Same |
| `planOpen` | `LiveActivityStream` | `useState(true)` | On user click | Same |
| `dataPreviewOpen` | `LiveActivityStream` | `useState(true)` | On user click | Same |

### Backward compatibility

- All existing `live_event` types still flow through unchanged
- Existing `liveEvents` array still populated
- `reasoning_done` event still fires for persistence
- Tests for existing event flow remain green

### Error / cancellation

- LLM call error: existing error path fires, streaming state cleared
- User clicks Stop: clear all 4 streams, fall back to existing "user stopped" UX
- SSE connection drops: existing reconnect preserves partial state (messages array persists)

---

## Section 6: Effort Estimate

| Layer | Lines | Risk |
|---|---|---|
| Backend (`agents.py` line ~10314) | 1 | Very low — pure additive |
| Backend search tools (4 callsites) | ~40 | Low — fake typing is safe |
| Backend plan parser | ~30 | Medium — heuristic, may misfire |
| Backend data preview wiring | ~20 | Low — reuse existing _sse_data_offer |
| Frontend state + handlers | ~50 | Low — pure additive |
| Frontend rendering | ~80 | Low — extends existing component |
| Tests | ~100 | Medium |
| **Total** | **~320** | |

**Risk**: 90% of the code is in 2 files (`agents.py`, `LiveActivityStream.jsx`). If a file revert happens (as in past sessions), the spec is the source of truth for re-application.

---

## Section 7: Out of Scope (Future)

- True streaming SQL result sets (requires data_source_runtime refactor)
- Voice playback
- Citation linkification
- Per-section char limits (might want to truncate very long reasoning)
- Reasoning summary mode (collapsed by default with one-line summary)
- Mobile-specific layout (the live sections may need to be more compact on small screens)
