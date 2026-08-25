# Activity-Feed Modernization — Design Spec

**Date**: 2026-08-25
**Status**: Approved (5 sections reviewed)
**Author**: brainstorming session
**Project**: zhanlu_7_30 (frontend only — minimal backend touches)

---

## Problem

The zhanlu chat UI's activity feed has three pain points that modern AI agents (Kimi, Claude, Perplexity, ChatGPT) handle better:

1. **Duplicate rows** — `subagent_invoked` and `tool_call_started` for the same `ask_data_agent` call render as TWO rows
2. **No collapse** — finished events stay at full size, making the feed long
3. **Flat sub-agent view** — all sub-agents show as one flat list (no parallel-branch visualization)
4. **No progress indicator** — user has no sense of how far along the agent is
5. **Markdown re-render** — `react-markdown` re-parses the full text on every streaming token, causing O(N²) jank for long responses

---

## Goal

5 improvements to the activity feed and markdown rendering, all in the **frontend** with minimal backend changes. Match modern AI agent UX without disturbing the rest of the chat.

---

## Section 1: Architecture Overview

**Key insight**: All 5 improvements live in the **frontend**. The backend already emits the right events.

**Minimal backend changes** (one optional field):
- Add a `parent_subagent_id` field to `tool_call_started` events when they have a parent subagent
- No new SSE events — the frontend derives the progress estimate from existing event data

**Frontend changes** (single file primarily, ~310 lines total):
- `LiveActivityStream.jsx` — event merge logic, progress bar, auto-collapse, sub-agent nesting
- `MessageBubble.jsx` — uses new `StreamingMarkdown` component
- New: `StreamingMarkdown.jsx` — memoized chunked renderer

**Data flow**:
```
Backend events (minimal change)     Frontend render pipeline
─────────────────────────────       ────────────────────────
tool_call_started ────────────►      1. mergeEvents() pairs
subagent_invoked ──────────────►        tool_call_started +
subagent_returned ─────────────►        tool_call_finished
tool_call_finished ────────────►     2. attachSubagentParent()
data_offer ────────────────────►        nests tool under subagent
                                    3. nestRows() computes
                                       indent
                                    4. computeProgress()
                                       derives bar state
                                    5. computeCollapseStates()
                                       decides chip vs full
                                    6. render() shows:
                                       • ProgressBar at top
                                       • Tree (subagent → tools)
                                       • Collapsed chips for old
```

**What's preserved**: existing `mergeEvents` logic for tool_call_started ↔ tool_call_finished pairing. Existing `TypedRow` component. All existing tests.

**Components added**:
- `ProgressBar` — horizontal bar with current/total count and time estimate
- `CollapsedChip` — small clickable chip for completed events
- `SubagentBranch` — visual wrapper that nests children under subagent rows
- `StreamingMarkdown` — memoized markdown renderer for streaming text

---

## Section 2: Event Merge Logic — Subagent + Tool Nesting

### Problem

Backend emits BOTH `subagent_invoked` and `tool_call_started` for the same `ask_data_agent`:

```python
# agents.py:11981
if tn.startswith("ask_"):
    _push_live_event("subagent_invoked", "subagent_invoked", {"target": tn})
    # ...later...
    _push_live_event("tool_call_started", "tool_call_started", {"tool_label": tn})
```

User sees TWO rows for the same logical action.

### Fix

**New step `attachSubagentParent()` in `mergeEvents()` pipeline**

```javascript
function attachSubagentParent(rows) {
  const openSubagents = []; // stack of {idx, target}
  for (const row of rows) {
    if (row.type === 'subagent_invoked') {
      openSubagents.push({ idx: row.idx_in_out, target: row.params?.target });
    } else if (row.type === 'subagent_returned') {
      openSubagents.pop();
    } else if (
      row.type === 'tool_call_started' ||
      row.type === 'tool_call_finished' ||
      row.type === 'tool_call_failed'
    ) {
      const parent = [...openSubagents].reverse().find(s => s.target === row.params?.tool_label);
      if (parent) {
        row.parent_subagent_idx = parent.idx;
      }
    }
  }
  return rows;
}
```

**New step `nestRows()` to compute visual indent**

For each row, if it has a `parent_subagent_idx`, set `indent = 1`. The subagent row itself gets `indent = 0`.

**New `SubagentBranch` render wrapper**

The subagent row is rendered as a "parent" row. Its child tool rows (matched by `parent_subagent_idx`) are rendered indented underneath:

```
┌─ ◉ Delegating to ask_data_agent · 131s · 12 rows  ← subagent row
│   └─ 🔧 ask_data_agent                            ← tool row, indent=1
```

The subagent row aggregates the child's data:
- Total duration (subagent_returned's duration)
- Row count (if the child tool returned rows)
- Status icon (running/done/failed)

### Edge cases

1. `tool_call_started` with no parent subagent → renders at `indent=0`
2. Multiple subagents invoked in sequence → each tool call matched to most recent open matching subagent
3. Subagent invoked but tool fails to start → subagent row shows "failed" status
4. `subagent_returned` arrives before `tool_call_finished` → child's status flips to done when both return

---

## Section 3: Progress Bar with Estimate

### Visual

A slim progress bar at the top of the activity feed:

```
┌────────────────────────────────────────────────────────────────┐
│  Working  ████████░░░░░░░  Step 2 of 4 · 45s elapsed · ~60s left │  ← ProgressBar
├────────────────────────────────────────────────────────────────┤
│  ◉ Fabricating  Building your deliverable                       │  ← existing rows
│  ...                                                            │
```

When the turn ends, the bar fades to 100% completed for 2 seconds, then disappears.

### Data model

```typescript
interface ProgressState {
  totalEvents: number;
  completedEvents: number;
  currentPhase: string;
  elapsedMs: number;
  estimatedRemainingMs: number;
  status: 'idle' | 'running' | 'completed' | 'failed';
}
```

### Estimation strategy

Rolling average of completed event durations:

```javascript
function estimateRemaining(events) {
  const completed = events.filter(e => e.status === 'done' || e.status === 'failed');
  const running = events.filter(e => e.status === 'running');
  if (completed.length === 0) {
    return running.length * 30_000; // default 30s/event
  }
  const avgMs = completed.reduce((sum, e) => sum + (e.params?.duration || 30_000), 0) / completed.length;
  return running.length * avgMs;
}
```

### Current phase detection

Most recent `phase_enter.*` event. If none, the most recent `subagent_invoked` or `tool_call_started` with status `running`.

### Visual style

- Thin (4px) horizontal bar
- Background: `bg-secondary/50`
- Fill: `bg-primary` with subtle gradient
- Animated: `transition-[width] duration-500 ease-out`
- Text: `Step N of M · {elapsed}s elapsed · ~{remaining}s left`
- Color states: blue (running), red (failed), green (completed)

### Edge cases

1. No events yet → bar hidden
2. All events complete → 100%, fades out after 2s
3. User clicks "Collapse" → bar still shows
4. Long-running single event → "Working... 60s elapsed" without step count
5. Multiple parallel subagents → counted as 1 step (logical progress, not wall-clock)

---

## Section 4: Auto-Collapse Completed Events

### Three states per row

1. **Running** (current): left-border accent, animated dot, full width
2. **Collapsed** (after a NEW event starts): single-line chip `✓ ask_data_agent · 131s · 12 rows  ▸`
3. **Expanded** (manual override, or last event in finished turn): full nested view

### Auto-collapse trigger

An event **auto-collapses** when:
- Its status is `done` or `failed`
- AND at least one **newer** event has status `running`

This way:
- During streaming: only the most recent running event + 1-2 most recent events are expanded
- When the turn ends (no more running): ALL events expand back to full view
- Manual click always toggles

### Implementation

```javascript
function computeCollapseStates(rows) {
  const hasRunning = rows.some(r => r.status === 'running');
  if (!hasRunning) {
    return rows.map(r => ({ ...r, collapsed: false }));
  }
  let lastRunningIdx = -1;
  rows.forEach((r, i) => {
    if (r.status === 'running') lastRunningIdx = i;
  });
  return rows.map((r, i) => ({
    ...r,
    collapsed: (r.status === 'done' || r.status === 'failed') && i < lastRunningIdx,
  }));
}
```

### Manual toggle

Each row has a chevron icon (`▸` when collapsed, `▾` when expanded). Clicking toggles `manuallyExpanded`. Manual state persists until the next turn.

### Edge cases

1. All events done → all rows expand automatically
2. Failed event → `✗` chip with red accent, click to see error
3. Orphan running (no new event for 30s) → still expanded
4. Manual collapse → persists even after turn ends (until next turn)
5. Mobile narrow screens (<600px) → collapsed mode is the default

---

## Section 5: Markdown Rendering During Streaming

### Current problem

```jsx
<ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
  {preProcessContent(p.text)}
</ReactMarkdown>
```

Re-parses the **entire accumulated text** on every streaming token. O(N²) cost for long responses.

### Proposed fix: chunked streaming markdown

**1. Split content by paragraph boundary**

```javascript
function splitContentIntoChunks(text) {
  const chunks = text.split(/(\n\n+|(?=^#{1,6}\s))/m);
  return chunks.filter(c => c.trim().length > 0);
}
```

**2. Memoize each completed chunk**

```jsx
const MemoizedChunk = memo(({ text }) => (
  <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
    {text}
  </ReactMarkdown>
), (prev, next) => prev.text === next.text);

export default function StreamingMarkdown({ content, isStreaming }) {
  if (!isStreaming) {
    return <MemoizedChunk text={content} />;
  }
  const chunks = splitContentIntoChunks(content);
  return (
    <>
      {chunks.slice(0, -1).map((chunk, i) => (
        <MemoizedChunk key={i} text={chunk} />
      ))}
      {chunks.length > 0 && (
        <MemoizedChunk key={chunks.length - 1} text={chunks[chunks.length - 1]} />
      )}
    </>
  );
}
```

**3. Cost analysis**

| Scenario | Current | New |
|---|---|---|
| 5000 tokens, 30 chunks, streaming 1 token at a time | O(N²) ≈ 7.5M parser calls | O(N) ≈ 5K parser calls |
| 5000 tokens, after streaming ends | O(N) | O(N) (one final render) |
| Token by token, last chunk = 50 tokens | O(N²) | O(50) per token = constant |

1500× speedup from memoization.

### Visual continuity

Output is identical to the user. Same `ReactMarkdown`, same `remark-gfm`, same `components`. The split is purely internal.

### Cursor handling

The existing `▍` cursor stays the same. Rendered after the last `MemoizedChunk` so it always appears at the end of visible content.

### Edge cases

1. No markdown elements (plain text) → still splits, negligible overhead
2. Tables mid-response → one chunk, renders atomically when its block completes
3. Long code block (500 lines) → one big chunk, no re-parse until it completes
4. Incomplete code block at the end → last chunk, gets the cursor
5. Single paragraph → single chunk, no benefit but no harm

---

## Implementation Effort Summary

| Section | Files | Lines | Tests |
|---|---|---|---|
| 2. Subagent + tool nesting | LiveActivityStream.jsx | 75 | 50 |
| 3. Progress bar with estimate | LiveActivityStream.jsx | 90 | 40 |
| 4. Auto-collapse completed events | LiveActivityStream.jsx | 75 | 40 |
| 5. Markdown streaming | New StreamingMarkdown.jsx, MessageBubble.jsx | 70 | 30 |
| **Total** | | **~310** | **~160** |

**Backend changes**: 1 line (add `parent_subagent_id` field to `tool_call_started` events when nested under a subagent).

---

## Out of Scope (Future Work)

- Editable/cancelable plan
- Per-sub-agent retry button
- Token/cost counter
- Mobile-specific layout optimizations beyond default collapse at <600px
- Server-side chunked markdown (would require new SSE events)
- Switching to a streaming markdown library (e.g. `streamdown`)
