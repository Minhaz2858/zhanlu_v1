# Chat with Agent Workflows from the Main Chatbot

Date: 2026-09-01
Status: Approved (design Q&A: context-injected multi-turn; chat-trigger workflows only; new-session binding)

## Problem

Users build n8n-style Agent Workflows on the canvas, but the main chatbot
(http://localhost:8088) cannot run them. The chat "+" menu already lists
workflow agents (they are ordinary `AgentApp` rows with `flow_mode=true`),
but the backend chat path (`routers/agents.py` v2/v3 message endpoints) has
NO `flow_mode` branch — it would run a workflow as a prompt agent with null
prompts. Goal: pick an Agent Workflow from "+", chat with it in a NEW chat
session that persists and can be reopened later to continue the conversation.

## Decisions (from brainstorming)

1. **Context-injected multi-turn (B)** — the workflow's chat trigger receives
   the current message PLUS the recent conversation history (last 10 turns),
   so a reopened session can continue meaningfully. Not stateless.
2. **Chat-trigger workflows only (A)** — only workflows with a
   `trigger_chat` node appear in the picker and can run from chat.
3. **New session binding** — selecting a workflow opens a fresh conversation
   bound to that workflow; nothing inherited from the previous session. The
   session shows in the session list and resumes normally.

## Architecture

### Backend

1. **History injection (workflow_runtime.py)**
   - `run_workflow_agent(..., history: list[dict] | None = None)` → passes to
     `WorkflowExecutionContext` → `ctx.trigger_history = history or []`.
   - `handle_trigger` (workflow_nodes.py): output gains `"history"` — a
     rendered transcript string of the last 10 turns
     (`User: …\nAssistant: …`). `{{inputs.<trigger>.history}}` resolves via
     the existing `_resolve_value` path — NO `_render` changes needed.
   - `trigger_chat` registry properties: add a notice documenting
     `{{inputs.<node>.history}}`. Backward compatible: workflows that never
     reference it are unaffected; manual/schedule/webhook triggers get no
     history key.
2. **Chat-path flow_mode branch (routers/agents.py)**
   - In BOTH `add_message` (v2, line ~6166) and the v3 stream endpoint
     (line ~10688), AFTER the agent is resolved (`agent_app` from
     `conv.agent_name`), branch on `agent_app.flow_mode`:
     - Defensive check: the flow must contain a `trigger_chat` node, else
       return a clear 400/error (picker only offers these).
     - Build history from `conv.messages` (last 10 turns, role
       user/assistant, excluding the current message).
     - Call `run_workflow_agent(..., trigger="chat", history=history)`.
     - Persist the assistant `ChatMessage` with the workflow's final output
       text; return the standard message shape (role assistant, content =
       output) so the frontend renders it like any reply.
     - Failure: reply content = `Workflow failed: <error>`; the workflow's
       configured error workflow (already built) handles alerting.
   - Factor a shared helper `_run_workflow_for_chat(db, conv, agent_app,
     user_content, user)` used by both v2 and v3 so the branch lives in one
     place.
   - v3 streaming: run synchronously; emit one message event with the final
     content (no token streaming). Per-node steps are NOT relayed into the
     chat activity rail in v1 (YAGNI) — they are visible in the workflow's
     run history (canvas).
3. **Run attribution** — `WorkflowRun` rows are created with
   `trigger="chat"` (already supported, no migration). Runs/debugging stay in
   the workflow's run history.

### Frontend

1. **PlusMenu (components/chat/PlusMenu.jsx)** — new "Agent Workflow" entry
   (icon: `Workflow`/`GitBranch`) → a 'workflow' view listing only
   `flow_mode` agents fetched via `listWorkflows()` (api/workflows.js),
   with search. New callback `onSelectWorkflow(wf)`.
2. **Agent picker hygiene** — exclude `flow_mode` rows from the existing
   Agent views in PlusMenu and InvokePicker so workflows appear ONLY in the
   dedicated section (no double entry / wrong-path execution).
3. **Chat.jsx / ChatSessionContext** — selecting a workflow creates a NEW
   conversation bound to it:
   - `metadata.workflow_id = wf.id` (stable rebinding — avoids name-collision
     issues of the existing `agent_name` convention) and
     `agent_name = wf.name` (keeps existing resolution working).
   - The new session opens immediately (fresh conversation); the session
     list shows it like any other; reopening resumes the same conversation;
     every message runs the workflow (backend resolves by
     `conv.agent_name` / metadata).
4. **Header/badge** — conversation title shows the workflow name (already
   driven by `conv.agent_name`); optional workflow icon polish.

## Data flow

1. User clicks + → Agent Workflow → picker lists `trigger_chat` workflows.
2. Pick one → new conversation created (`metadata.workflow_id`,
   `agent_name=wf.name`) → session opens.
3. User sends a message → v2/v3 message endpoint → agent resolved →
   `flow_mode` detected → `run_workflow_agent(trigger="chat",
   history=last 10 turns)` → nodes execute → final output text.
4. Assistant message = workflow output. `WorkflowRun` persisted
   (`trigger="chat"`) with per-node steps.
5. Reopen the session later → same conversation → next message runs the
   workflow again with updated history.

## Error handling

- Workflow run failed → chat reply shows the failure text; any configured
  error workflow already executed (existing feature).
- History injection is best-effort and backward compatible (only workflows
  referencing `{{inputs.<node>.history}}` see it).
- Non-chat-trigger workflows never appear in the picker; backend rejects
  them defensively with a clear error.

## Testing

- Backend unit: `handle_trigger` emits the history transcript (and omits it
  for non-chat triggers); `run_workflow_agent(history=...)` → `ctx`.
- Router tests (v2 `add_message`): flow_mode agent → assistant message =
  workflow output; history passed through (workflow = trigger_chat → code
  node returning the history text; assert previous turns appear); non-flow
  agents unchanged; flow_mode WITHOUT trigger_chat → clear error.
- Frontend: PlusMenu workflow section lists only flow_mode rows; selecting
  one creates a bound session (mocked listWorkflows / API).

## Out of scope (v1)

- Relaying live workflow node steps into the chat activity rail (workflow
  run history covers visibility).
- Mobile chat page workflow entry (verify PlusMenu usage; include only if
  trivial).
- Workflow→chat artifact delivery (e.g. dashboards generated by a workflow
  rendered in chat).
