# Concurrent Multi-Session Chat — Reference Architecture

**Date:** 2026-08-31
**Status:** Approved design (pending implementation)
**Scope:** Framework-agnostic reference architecture. The host repo (Zhanlu / Synexia
platform) is used only as a concrete illustration of each pattern. No code is
included here; this is the spec to implement against.

---

## 0. Decisions locked with the user

| Dimension | Decision |
|---|---|
| Scope | Generic reference architecture (repo = example, not the deliverable) |
| Transport | N parallel SSE fetches (keep the existing `streamAgentResponse`); **no** WebSocket multiplex, **no** backend-owned run table for the common path |
| Concurrency | 2–3 simultaneously *running* sessions per user → per-user semaphore cap = 3, no backend queue |
| Fidelity | **Full** — content + activity steps + live trace + reasoning + artifacts, exactly as if the session was never left |
| Failure mode | Switch-away keeps the run alive; only network *drop* triggers reconnect/replay |

---

## 1. Terminology (so the spec is unambiguous)

| Term | Meaning |
|---|---|
| **Chat session** | A container the user navigates between. Backed by a conversation row + history. Has an id, a project, persisted messages. |
| **Run** | One execution of the agent loop — a single interactive LLM stream **or** one automation execution. Owns exactly one SSE connection (interactive) or one server-side background task (automation). A session has **at most one run** (active) or **no run** (idle). |
| **Session Manager** | Client-side registry: the list of sessions, which have an active run, which is "focused" in the UI. |
| **Run Controller** | Per-run object: `AbortController`, generation counter, in-memory **buffer** (full-fidelity state), status. |
| **Focused session** | The single session currently rendered in the chat view. Switching focus must **never** touch other runs. |

The host repo already has the shape: `ChatSession` + `AgentConversation` are the
session; `automation_executor` runs automations as background asyncio tasks;
`ChatSessionContext` tracks an `activeId`. The design's job is to remove the
**singleton runtime object** and key everything per run.

---

## 2. Session Management

**Principle: the stream lives above the UI; sessions are just focus targets.**

```
AppRoot
 └─ SessionManagerProvider                 // replaces the singleton PersistentStreamContext
     ├─ RunRegistry: Map<sessionId, RunController>   // <= KEY CHANGE: per-run, not single
     ├─ activeSessionId  (the FOCUSED session, for rendering only)
     └─ <Router>
         └─ ChatPage(sessionId = activeSessionId)    // renders only the focused session
```

- **Open N sessions** — each is a row in the session list; only `activeSessionId`
  is rendered. Switching focus = `setActiveSessionId(x)`; **no run is touched**.
- **Begin a run in session S** — `RunRegistry.set(S, new RunController(...))`.
  The controller opens its own SSE fetch and writes into `RunRegistry.get(S).buffer`.
- **Switch away from S while its run runs** — nothing is aborted. The fetch keeps
  writing to `buffer[S]`. The UI re-renders `buffer[newFocus]`, which is
  independently populated. This is what makes a background automation (or a slow
  agent reply) continue uninterrupted.
- **Return to S** — UI reads `buffer[S]` (still live) → seamless, full fidelity.
- **Close / delete session S** — only *then* `RunRegistry.get(S)?.abort()` and
  `delete buffer[S]`.

### 2.1 Sidebar row affordance — "running silently" (one-panel UX)

The user always sees a **single chat panel** (the focused session). Other
sessions keep running in the background and surface as a **per-row indicator in
the session list** (a small spinner/dot on the right of the row), not as
separate windows or split panes. This mirrors the ChatGPT pattern from the
reference screenshots and is the literal meaning of "running silently":

```
Sidebar                                 Panel
┌──────────────────────────┐   ┌──────────────────────────┐
│ New chat          · (●)  │   │                          │
│ New chat          · (●)  │   │   Focused session S      │
│ New chat                 │   │   (only S is rendered)   │
│ PhD News Search  · (●)   │   │                          │
│ Dallas Cowboys   · (●)   │   │                          │
│ Resume …                 │   │                          │
└──────────────────────────┘   └──────────────────────────┘
        ▲   ▲   ▲   ▲
        └───┴───┴───┴── RunRegistry.get(sid).status drives
                       a per-row spinner / dot / queued badge
```

- Each sidebar row is subscribed to `RunRegistry.get(row.id)?.status` for its
  session. No badge, no popup, no extra panel — the user knows a session is
  busy purely from the row's indicator.
- The focused row additionally shows the in-panel streaming UI (typing dots,
  stop button, live content from `buffer[activeSessionId]`).
- The host repo's `ChatSessionContext` already exposes the row list; only the
  per-row status hook is new.
- This makes "concurrent" a **status**, not a **layout** — multiple sessions are
  never shown side-by-side. The user can freely switch focus without disturbing
  any other run.

Status → indicator mapping:

| `RunController.status` | Row indicator |
|---|---|
| `running` | small spinner (lucide `Loader2` / `CircleDashed`) |
| `queued` (semaphore full, §5.1) | queued icon (e.g. clock) with tooltip "Waiting (N ahead)" |
| `done` (just finished) | brief check-flash, then disappears (~2s) |
| `error` | red dot, stays until row opened |
| idle (no controller) | none |

**Why this works against the existing backend:** request handling is already
stateless per-request and has no per-conversation lock (verified — the only
`Lock()`s in the backend are narrow per-resource guards, e.g. KB refresh, not
conversation-wide). A second concurrent run is simply a second
`POST /v3/.../messages/stream` with a different `conversation_id`. The *only*
thing that previously prevented concurrency was the client-side singleton.

### 2.1 Repo mapping (illustrative)

| Current (blocking) | This design |
|---|---|
| `PersistentStreamContext` singleton (`abortRef`, `genRef`, `streamGenRef`, `activeSessionId`) | `SessionManagerProvider` exposing `RunRegistry: Map<sessionId, RunController>` |
| `activeSessionId` global | `activeSessionId` = focus only; each run has its own controller |
| `ChatSessionContext.selectSession` rewrites URL, no abort | unchanged — focus switch is independent of runs |

---

## 3. State Isolation Between Chats

Three layers, all keyed by `sessionId` / `conversation_id`.

### 3.1 Stream / runtime isolation — Run Controller per run

```ts
interface RunController {
  sessionId: string;
  abortController: AbortController;   // OWN — never shared across runs
  genRef: number;                     // OWN invalidation counter
  buffer: FullFidelityState;          // see 3.2
  status: 'queued' | 'running' | 'done' | 'error';
}
```

No module-level mutable `abortRef` / `genRef` / `activeSessionId`. This is the
direct fix for the host repo's blocker #1 (singleton stream slot).

### 3.2 Full-fidelity buffer (satisfies the "Full fidelity" choice)

Each run's `buffer` holds *everything* the UI needs, kept in memory for the life
of the run:

```ts
interface FullFidelityState {
  messages: ChatMessage[];          // assembled conversation (stable ids)
  pendingAssistantId: string | null;
  content: string;                  // streaming text delta target
  activitySteps: ActivityStep[];    // activity rail / tool calls
  liveTraceSteps: TraceStep[];      // trace panel
  reasoningText: string;            // chain-of-thought
  artifacts: Artifact[];            // files / slides produced
  phase: string;                    // current agent phase
  lastEventId: string | null;       // for replay (Section 6)
}
```

When the focused session's run is live, the chat view renders straight from
`buffer[activeSessionId]`. The host repo already keeps `content / toolCalls /
phase` in a `Map<sessionId, msg>` (`pendingMessageRef`); we **extend that map's
schema** to the full object above so activity steps, trace, and reasoning are no
longer lost on switch (blocker #2).

### 3.3 Agent runtime isolation (server-side, per run)

Each run gets its own conversation-scoped state: memory read/write, `tool_calls`
history, selected skill, session state block — all keyed by `conversation_id` /
run id. No cross-session module globals. The host repo's
`build_session_state_block(db, session_id=...)` is already per-session; the rule
is to **guarantee** no `global` caches are shared across concurrently executing
runs (e.g. in-memory skill caches, prompt builders) — each run must pass its own
state in, not read a shared singleton.

This also fixes blocker #3 (single-valued UI state): `streamingConvId`,
`loading`, and the thinking indicator become per-controller fields read from
`RunRegistry.get(activeSessionId).status`, never global.

---

## 4. Background Automation Continues While You Switch

This is the crux. Two run types share the same registry:

| Run type | How it starts | Where it lives | Streaming? |
|---|---|---|---|
| Interactive agent reply | user sends in session S | **client-owned SSE** (persists above the UI — see Section 2) | yes |
| Automation execution | `POST /automations/{id}/run` | **server-owned background task** (already async in the dispatcher) | no live SSE; polled |

The automation is already non-blocking in the host design: `trigger_now` fires
the executor as a background asyncio task and returns an `execution_id`; the
frontend registers it in a **module-level** store (`automationStatusBus`) that
lives outside React and survives navigation; a pinned drawer is mounted at the
app layout. So while it runs:

1. User clicks "Run" on task T → register `execution_id` in the status bus,
   navigate to T's session.
2. User switches to session S2 and starts chatting →
   `RunRegistry.set(S2, controller)`, opens its **own** SSE. The automation is
   unaffected — it is a server process, not a browser fetch.
3. The status bus keeps polling `GET /api/automations/by-session/{sid}` (its
   `decideRefresh` state machine) and the pinned drawer stays visible across
   navigation.
4. User returns to T's session → the poll already reloaded the new messages;
   full fidelity preserved.

**Key invariant to add:** *switching focus never calls `abort()` on any run
except the one explicitly stopped or deleted.* The host repo's `handleClear`
already guards with `if (stream.isActiveForSession(activeId))` — generalize that
to "operate only on the targeted session's controller, never on the
registry-global." This is the single behavioural rule that prevents a background
automation or slow reply from being killed by navigating away.

---

## 5. Transport — N Parallel SSE (minimal change) + required mitigations

The user chose to keep `streamAgentResponse` as-is and simply allow multiple
concurrent connections. That is correct and is ~80% of the work (the singleton
fix). But two risks from the host repo's own incident log — a
`QueuePool limit ... timed out` on 2026-08-31, and the HTTP/1.1 ~6-socket-per-origin
cap on the dev proxy — mean "allow multiple" alone will still fall over at 3
concurrent long agent loops. Two small mitigations:

### 5.1 Per-user run semaphore (cap = 3) — client side

```ts
const MAX_CONCURRENT_RUNS = 3;
if (RunRegistry.size >= MAX_CONCURRENT_RUNS) {
  // new run enters status 'queued', shown in its session as
  // "Waiting (1 ahead)"; promoted when a slot frees.
}
```

This satisfies the "2–3, simple semaphore, no queue needed" target. The *queue*
is only the visible `queued` state, not a backend queue.

### 5.2 Replay on reconnect (cheap insurance for full fidelity)

The backend already persists `ChatMessage` columns (`tool_calls`,
`activity_steps`, `phase`, `live_events`, `reasoning_content`, `artifacts`,
`sources`). On socket **drop** (not on switch — on drop), the controller
re-fetches `GET /messages?session=S&after=lastEventId` to backfill, then
re-subscribes. **No new DB table is required for the common case** (switch-away
and return) because the in-memory buffer already holds it. Replay is only for
network failure.

### 5.3 DB pool + socket hardening (deployment note, in-scope)

Required to actually survive 3 concurrent runs. Bump `pool_size` / `max_overflow`
in backend config and ensure every long-lived agent loop closes its DB `Session`
promptly (the data-agent and KB reingest loops are the usual leakers). Without
this, 3 concurrent 40-iteration loops will reproduce the QueuePool timeout. This
is a config/deployment change, not a client design choice — but it is listed as
in-scope because it is load-bearing for the concurrency target.

> **WebSocket note:** not chosen. SSE is sufficient at ≤3 concurrent runs per user;
> the socket cap only bites at ~6+, which the semaphore prevents. Multiplexing
> would be the migration path only if the concurrency target grows well beyond 3.

---

## 6. Lifecycle & Failure Semantics

- **Run done** — SSE `done` event, or automation terminal status: controller
  writes the final `ChatMessage`(s) to DB, flushes `buffer` into permanent
  history, sets `status='done'`, **releases its semaphore slot**. Buffer may be
  dropped after a short TTL (e.g. 60s) once persisted.
- **Run aborted** — Stop button / session delete: `controller.abort()`. The
  *other* runs in the registry are untouched.
- **Network drop** — reconnect + replay (5.2). If unrecoverable, mark
  `status='error'` in that session only; other sessions unaffected.
- **Tab close / reload** — registry is in-memory → lost. On reload,
  `loadMessages` re-reads persisted `ChatMessage` for each session (full fidelity
  for *finished* runs; live runs would restart only if the user re-triggers them).
  Survive-reload for *live* runs (a `run_event` table + `last_event_id` subscribe)
  is an **optional** extension, explicitly out of scope per the user's choices.

---

## 7. Blocker → Design mapping (host repo)

| # | Blocker (host repo) | Fix in this design |
|---|---|---|
| 1 | Singleton stream slot (`abortRef`/`genRef`/`activeSessionId`) | `RunRegistry: Map<sessionId, RunController>` |
| 2 | Rich stream state closure-local (activity/trace/reasoning lost on switch) | promote to `buffer[sessionId].{activitySteps,liveTraceSteps,reasoningText}` |
| 3 | Single-valued UI state (`streamingId`/`loading` ambiguous) | per-controller `status`; UI reads `RunRegistry.get(activeSessionId).status` |
| 4 | Mobile dies on navigate (local `useStreamState`) | mobile adopts the shared `SessionManagerProvider`, not local state |
| 5 | No SSE resume/replay | replay via persisted `ChatMessage` + `lastEventId` (failure-only) |
| 6 | Pool / socket exhaustion | per-user semaphore (3) + DB pool config (5.1, 5.3) |

---

## 8. Implementation checklist (for the next phase — not done here)

1. Replace `PersistentStreamContext` singleton with `SessionManagerProvider`
   exposing `RunRegistry` (Map of controllers) + `activeSessionId` (focus only).
2. Define `RunController` + `FullFidelityState` interfaces; extend the existing
   `pendingMessageRef` map schema to the full buffer.
3. `ChatPage` reads/writes `RunRegistry.get(activeSessionId)` instead of global
   stream state; `streamingConvId`/`loading` become per-controller.
4. `handleClear` / `handleDelete` / Stop button operate only on the targeted
   session's controller (never registry-global).
5. Add client-side per-user semaphore (cap 3) with visible `queued` state.
6. Mobile `MobileChatPage` consumes the shared provider instead of local
   `useStreamState`.
7. Add reconnect+replay on socket drop using persisted `ChatMessage`.
8. Deployment: raise DB `pool_size`/`max_overflow`; audit long-lived loops for
   unclosed `Session`s.
9. (Optional, out of scope) `run_event` table + survive-reload for live runs.
10. Sidebar row subscribes to `RunRegistry.get(rowId)?.status`; render the
    per-row "running silently" indicator (§2.1) — spinner for `running`,
    queued icon for `queued`, check-flash for freshly `done`, red dot for
    `error`, none when idle.
