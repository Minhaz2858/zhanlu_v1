# Zhanlu — Chat UI & Agent Execution Experience Repair

(2026-10-31. Zero LLM credits consumed: every test is deterministic; no live
calls were made. Root causes were identified by tracing one full turn —
submit → v3 stream → ReAct loop → delegation → SSE → render — before any edit.)

## 1. Original defects (from the screenshot + code trace)

| # | Symptom | Surface |
|---|---------|---------|
| 1 | Assistant response cards too narrow / shrink-wrapped | MessageBubble |
| 2 | "Agent Execution" panel: summary said "3 events, 3 completed" while 4 visible sub-agent entries all showed "Running" | AgentExecutionTree + LiveActivityStream |
| 3 | A "hi" greeting produced a multi-sub-agent execution tree | Backend ReAct loop |

## 2. Root causes (all traced to source, no frontend guessing)

### D1 — Synthetic phantom nodes (the "4 Running sub-agents")
`AgentExecutionTree.buildTreeFromLiveEvents` iterated over EVERY standalone
branch from `groupIntoBranches` — including `phase_enter`, `tool_call_*` and
`plan_meta` events, which become their own branches — and turned each into a
node labelled `sub.agent_label || sub.target || 'sub-agent'` with
`status: returned ? 'completed' : 'running'`. Non-delegation branches can
never match a `subagent_returned`, so each became a **permanent Running
"sub-agent" node**. The panel therefore showed nodes for events that were
never delegations.

Additionally, `MessageBubble` never passed `conversationId` to
`AgentExecutionTree`, so the authoritative REST tree
(`/api/executions/tree?conversation_id=…`) was unreachable — the buggy
live-synthesis path rendered even for historical messages.

The "3 events · 3 completed" footer came from `LiveActivityStream`'s
row-level counter (feed rows), a DIFFERENT dataset from the tree's per-agent
counters — two independent counters with no shared source.

### D2 — Narrow assistant cards
The assistant bubble column was `max-w-[90%]` and the bubble itself sized
fit-content (shrink-wrap), so answers rendered as narrow bordered cards
inside the `max-w-5xl` transcript column instead of using the readable
column width.

### D3 — "hi" fanned out to sub-agents
`build_turn_plan` ALREADY classifies greetings/smalltalk as
`kind=none / intent_type=conversational / execution_strategy=direct_response`
(`is_greeting` + `_SMALLTALK_RE`). But `agents.py` never referenced
`direct_response` — the strategy was advisory text only. The ReAct loop ran
with the full toolset (including `delegate_task` and `list_available_agents`,
actively advertised by the system prompt's department block), so the local
LLM was free to call `delegate_task(tasks=[...])` for a greeting. Each batch
item created one AgentInvocation row (`open_invocation=True`) — a real fan-out
with real rows, honestly rendered as fake-looking activity.

### D4 — Stale/duplicate execution state (backend)
- `subagent_invoked`/`subagent_returned` SSE pairs were only guaranteed when
  the tool call completed inside the streamed iteration: cancellation, client
  disconnect, or a tool crash left the badge open forever.
- `subagent_returned` carried no `subagent_id` — pairing with its
  `subagent_invoked` relied on fragile label matching.
- `_run_sub_agent`'s finally-fallback inserted a terminal row whenever
  `update_invocation_terminal` was REFUSED — even when the start row existed
  and was already terminal — producing a stale `running` row PLUS a terminal
  duplicate (both rendered by the tree).
- `GET /executions/{execution_id}/agent-tree` crashed with `NameError:
  exution_id` (typo) — the endpoint was dead.

## 3. Backend changes

`app/routers/agents.py`
- `_enforce_turn_execution_strategy(tools, turn_plan)` (pure, testable):
  `direct_response` turns run with an EMPTY toolset — no delegation, no DB
  access, no synthetic events. Applied in `event_stream` via IN-PLACE slice
  assignment (`tools[:] = …`) because `tools` is a closure variable
  (rebinding shadowed it and UnboundLocalError'd the loop — caught by
  test_react_stream_recovery_e2e, fixed). A short conversational-turn
  instruction is appended for tone. This ENFORCES the existing TurnPlan
  taxonomy — it is not keyword routing; multi-agent delegation for analysis
  and enterprise tasks is untouched.
- `_delegate_agent_identity(args)` (pure): extracts the real specialist
  names from `delegate_task` call args (single `agent_name`, batch
  `tasks[].agent_name`, dedup). `subagent_invoked` now carries
  `agent_name`/`agent_names` — the UI can show real identities.
- `subagent_returned` now carries `subagent_id` (deterministic pairing).
- Terminal-event guarantee: any still-open sub-agent badge is flushed with
  `outcome: "interrupted"` BEFORE the `done` frame (yielded), and — on
  abnormal exit (exception/disconnect) — persisted in the stream's `finally`
  (push-only; yielding is unsafe under GeneratorExit). A node can no longer
  stay Running because its return event was lost.
- Hoisted `_subagent_id_stack`/`_subagent_seq` to turn scope so the finally
  flush is always in scope.

`app/services/tool_handlers/delegate_tool.py`
- `_invocation_row_exists(db, id)` helper; the finally-fallback inserts a
  terminal row ONLY when the start row genuinely does not exist. A refused
  terminal update on an existing row (first-terminal-wins) logs a warning
  and inserts NOTHING — no more running+terminal duplicates.

`app/routers/executions.py`
- Typo fixed (`exution_id` → `execution_id`); the by-execution agent-tree
  endpoint works again.

## 4. Frontend changes

`AgentExecutionTree.jsx`
- `buildTreeFromLiveEvents` rewritten: a node exists ONLY for a real
  `subagent_invoked` event (standalone branches are never synthesized);
  identity = `agent_name → agent_label → target` (the generic "sub-agent"
  literal is gone; the delegation tool name is the honest last resort);
  deterministic terminal reconciliation — matched return → `completed`
  (`outcome:interrupted` → `cancelled`), unmatched at stream end →
  `cancelled`, NEVER Running; duplicates by `subagent_id` collapse to one
  node.
- `summarizeNodes(roots)` mirrors the backend `_summarize` — the summary is
  now computed from the SAME nodes the tree renders (live trees included;
  previously `summary: null` on the live path).

`LiveActivityStream.jsx`
- `outcome=interrupted` merges to status `interrupted` (honest amber row;
  SubagentRow + generic row renderer + style map updated).
- End-of-stream reconciliation: once the stream has ended, any still-running
  row becomes `interrupted` — no eternal spinners on historical messages.
- Footer relabelled "{n} steps · {m} done · {s}s" (en template) so the feed
  counter is no longer mistaken for the agent summary.
- `mergeEvents` exported (additive) for deterministic tests.

`MessageBubble.jsx`
- Assistant messages now fill the transcript column (`w-full`); user
  messages stay compact fit-content right-aligned (`max-w-[90%]`).
- Passes `conversationId` to `AgentExecutionTree` → historical messages
  restore the AUTHORITATIVE persisted tree.

`Chat.jsx`
- Resolves the AgentConversation id (`getSession(activeId)?.conversation_id`)
  — the same identity the v3 stream used — and passes it down. (Passing the
  ChatSession id would query the wrong key and look empty.)

`translations.js` — summary template value only.

## 5. Event lifecycle changes

- `subagent_invoked`: + `agent_name`, `agent_names`.
- `subagent_returned`: + `subagent_id`, and `outcome: "interrupted"` on the
  guarantee paths.
- Terminal-state rule: invoked + returned → completed; invoked +
  interrupted-outcome → cancelled; invoked + stream-end → cancelled;
  never Running. The AgentInvocation REST tree remains the authoritative
  source on reconnection/history.

## 6. Intent-handling changes

- Greetings/smalltalk: TurnPlan `direct_response` is now BINDING (toolset
  cleared + conversational instruction). "Hi" answers directly — no
  specialists, no plan workflow, no database access, no execution panel
  (the tree renders nothing when no delegation events exist).
- Information retrieval / analysis / complex enterprise tasks: unchanged —
  the ReAct loop, TurnPlan, delegation, peer collaboration and planning
  infrastructure are untouched. No keyword router was added; the
  classification used is the pre-existing `is_greeting`/`_SMALLTALK_RE`
  TurnPlan gate.

## 7. Test coverage (deterministic, zero credits)

- NEW backend `tests/test_chat_execution_experience.py` (11): strategy
  enforcement (incl. real TurnPlan "hi" → direct_response → empty toolset),
  identity extraction (single/batch/dup/absent), row-existence semantics,
  tree-summary consistency, endpoint typo, terminal-flush presence.
- NEW frontend `src/components/chat/agentExecutionTree.test.js` (12): zero
  agents → no panel; real identity (no "sub-agent" string); completed never
  Running; orphan → cancelled; interrupted honesty; duplicate events → one
  node; out-of-order pairing; multi-specialist counts; summary-from-same-
  nodes; mergeEvents interrupted/dedupe/order.
- Existing frontend `AgentExecutionTree.test.jsx` (7) passes unchanged
  against the rewrite.

## 8. Regression results

- Backend focused (edited paths): 194 passed.
- Backend broad sweep (44 suites, run ONCE): **587 passed**; 3 failures —
  the SAME pre-existing set stash-A/B-verified in Phase 3.5 (replan_policy
  ×2 foreign "2B-4" WIP; parallel_tools ×1 cross-test schema pollution).
  Zero new failures.
- Frontend full suite: **1029 passed**; 27 failures across 6 files — all
  pre-existing/foreign-WIP/environmental (Sidebar.jsx = foreign WIP not
  touched this session; ProjectDetail.jsx unmodified at HEAD;
  translations diff is a single value line; previewToken ECONNREFUSED is a
  test-env network call). Every chat/execution/translation suite touching
  edited files passes.
- Frontend production build: **exit 0**.

## 9. Remaining limitations

- The abnormal-exit flush persists interrupted returns, but if the crash
  happens before the assistant message row is written, replay relies on the
  REST tree (AgentInvocation rows) — which the orphan pass (600s) and
  first-terminal-wins keep honest. Extremely fresh orphans (<1h) can show
  `running` in the REST tree until the orphan pass flips them.
- LiveActivityStream and AgentExecutionTree still maintain two renderings
  of the same events (feed rows vs agent nodes); counters are now labelled
  and derived consistently, but a single shared execution-store would be the
  cleaner long-term shape (not done — avoid rebuilding working infra).
- Turn-scoped REST trees: the tree endpoint is conversation-scoped, so an
  execution panel may appear on historical messages of a conversation that
  had delegations in ANY turn. Turn scoping needs an invocation↔turn link
  (schema change) — deferred.
- Frontend auto-reconnection is still watchdog-only (no SSE replay client);
  reconnection correctness comes from the REST tree + persisted live_events.

## 10. Rollback

Revert this changeset (no migrations, no schema changes):
- `git revert` / restore: `backend/app/routers/agents.py`,
  `backend/app/services/tool_handlers/delegate_tool.py`,
  `backend/app/routers/executions.py`,
  `frontend/src/components/chat/AgentExecutionTree.jsx`,
  `frontend/src/components/chat/LiveActivityStream.jsx`,
  `frontend/src/components/chat/MessageBubble.jsx`,
  `frontend/src/pages/Chat.jsx` (2 small edits),
  `frontend/src/lib/translations.js` (1 line).
- Delete `backend/tests/test_chat_execution_experience.py` and
  `frontend/src/components/chat/agentExecutionTree.test.js`.
No DB state is affected.
