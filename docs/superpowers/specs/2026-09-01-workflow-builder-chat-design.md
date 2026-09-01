# Workflow Builder Chat — In-Canvas AI Workflow Creation

Date: 2026-09-01
Status: Approved (design)
Related: `docs/superpowers/specs/2026-09-01-agent-workflow-designer-design.md` · `docs/plans/2026-09-01-agent-workflow-designer.md` · `docs/superpowers/specs/2026-09-01-workflow-chat-integration-design.md`

## Problem

Non-IT users cannot build agent workflows. The n8n-style canvas requires
understanding node types, connection ports, config schemas, and template
syntax. We want a path where a user simply **chats with one AI agent** inside
the canvas and the agent produces a draft workflow graph — while keeping full
manual building available for users who want it.

## Goal

A chat icon in the workflow canvas page (`/my-space/workflow/:id`). Clicking it
opens a slide-over chat panel. The user describes what they want in plain
language; a new `workflow_builder` system agent proposes the workflow; the
backend validates and saves it as a **draft** on the current workflow; the
canvas reloads the graph. Manual editing (palette, connections, NDV, run
drawer) is untouched.

## Approach

Reuse the proven `agent_builder` pattern end-to-end: intent routing via
`_SYSTEM_AGENT_INTENT_TABLE`, a `:::decision-summary` fenced block emitted by
the agent, backend parse → validate → persist, and a frontend refresh on the
`flow_saved` status. No new infrastructure.

## Architecture

### 1. New system agent: `workflow_builder`

- AgentApp row (`flow_mode=false`, `is_system=true`), created by the existing
  `ensure_system_agents()` path.
- System prompt in `backend/app/services/agent_prompts.py`:
  - Mission: "You design AI agent workflows from plain-English descriptions."
  - Must emit a single `:::decision-summary` fenced block containing
    `{"action": "save_flow", "name": <title>, "description": <short>, "flow": {version:1, nodes:[...], edges:[...]}}`
  - The prompt embeds the **node-type registry** (from
    `workflow_node_types.list_node_types()` → compact name/displayName/
    required-config summary) so the agent only emits real node types and only
    required config fields.
  - Rules: exactly one `trigger_chat` node; connect main ports `source-main`
    → `target-main`; config values may reference real entity NAMES
    (connector name, agent name, tool name, kb name) — backend resolves
    name→id; template syntax `{{inputs.<node>.<field>}}` allowed in string
    fields.
  - Non-goals (hard-coded in prompt): no auto-publish, no editing of
    arbitrary existing flows, no multi-workflow management. Always produce a
    full replacement graph for THIS workflow.

### 2. Intent routing

Add a row to `_SYSTEM_AGENT_INTENT_TABLE` in
`backend/app/routers/agents.py` (~line 2666):

```python
(
    "workflow_creation",
    "workflow_builder",
    _WORKFLOW_CREATION_HEADER_RE,
    None,
),
```

Header regex matches (EN + ZH):
- "create a workflow", "build me a workflow", "make an agent workflow",
  "design a workflow for ..."
- "做一个工作流", "创建智能体工作流", "帮我设计一个工作流"

The intent detector routes the chat message to `workflow_builder` exactly like
agent_creation routes to `agent_builder`.

### 3. Decision-summary persistence branch

In the same code path as `_persist_decision_summary_pause` (agents.py ~4618),
add a `workflow_builder` branch (mirroring the agent_builder branch at
~7361/~7758):

- Parse the `:::decision-summary` block via `parse_decision_summary_block`.
- If `payload.action == "save_flow"`:
  1. `name`/`description`/`flow` extracted; missing required keys → return
     validation errors to the agent (repair loop).
  2. `validate_flow(flow)` from `workflow_validation` — errors → return them
     in the assistant text (strip the block, append
     "The workflow draft failed validation: <errors>. Fix the flow JSON and
     emit a new decision-summary."), do NOT persist.
  3. Resolve entity references name→id using the `_resolve_entity` fallback
     pattern already in `workflow_runtime.py` (kb_id/agent_id/tool_id/
     server_id, id-first then unique-name, ambiguous raises → repair loop).
  4. Persist: `save_workflow(workflow_id, SaveBody(name=..., description=...,
     flow=...), ...)` on the CURRENT workflow id. The workflow id comes from
     the conversation's metadata (`metadata.workflow_id`, set when the chat
     was launched from the canvas) — fallback: conv.agent_name is NOT used
     here; the canvas panel always passes `workflow_id`.
  5. Emit status `flow_saved` (+ flow echo) in the stream so the frontend
     knows to reload.
- The chat message flow returns `awaiting_decision_summary`-style status so
  the frontend can act on it.

### 4. Frontend: chat icon + slide-over panel

- `frontend/src/pages/WorkflowCanvasPage.jsx`:
  - Header gains a chat button (MessageSquare icon) next to Save/Validate/Run.
  - `const [builderOpen, setBuilderOpen] = useState(false);`
  - Renders `<WorkflowBuilderChat workflowId={id} onFlowSaved={reload} />`
    in a right slide-over (fixed panel, like RunDrawer but chat).
- New component `frontend/src/components/workflow/WorkflowBuilderChat.jsx`:
  - Message list + input + streaming state (reuse the same v3 chat stream
    helper the Chat page uses, with `agent_name="workflow_builder"` and
    `metadata.workflow_id = workflowId`).
  - On `flow_saved` stream event: toast "Workflow draft updated", close panel,
    call `onFlowSaved()` → `WorkflowCanvasPage` reloads the workflow
    (`getWorkflow(id)` → `flowToXY` → setXyNodes/setXyEdges), clears dirty.
  - On `awaiting_decision_summary` / validation-error content: just render the
    agent's message text normally (the repair loop shows as plain chat).
  - Sends conversation title "Workflow builder" (mirrors
    `createAgentConversation` contract used by Chat.jsx).

### 5. API surface

No new endpoints. Reuse:
- The **v3 streaming endpoint** used by the Chat page (v2 is non-streaming and
  cannot surface the live `flow_saved` event) with body
  `{role:"user", content: ...}` and agent binding `workflow_builder`
  (+ `metadata.workflow_id`).
- `GET /api/apps/{app}/workflows/{id}` for the canvas reload.
- `PUT /api/apps/{app}/workflows/{id}` (save_workflow) from the backend
  persistence branch.

## Data flow

```
User: "build me a workflow that queries open orders and emails a summary"
  → canvas chat panel → v3 stream (agent_name=workflow_builder,
    metadata.workflow_id=<id>)
  → intent detector routes to workflow_builder
  → LLM emits :::decision-summary {action:save_flow, flow:{...}}
  → backend parse → validate_flow → resolve name→id → save_workflow(draft)
  → stream event flow_saved
  → panel toast + close → WorkflowCanvasPage reloads graph → user edits/publishes
```

## Error handling

- Invalid graph JSON / missing keys / validation errors → repair loop: errors
  returned to the agent in assistant text, agent re-emits. No partial saves.
- Ambiguous entity names (two connectors with the same name) → resolve raises;
  error text asks the user to disambiguate (agent relays to chat).
- Save failure (DB error) → status `flow_save_failed` + toast; graph on canvas
  unchanged.
- Panel closed mid-stream → abort stream (same pattern as Chat page stop).

## Testing

Backend (pytest, router-direct pattern like `test_workflows_router.py`):
- `test_workflow_builder_intent_routing`: EN/ZH phrases → `workflow_builder`.
- `test_workflow_builder_persist_save_flow`: fake decision-summary payload →
  validate → save_workflow called with draft; `flow_saved` status emitted.
- `test_workflow_builder_invalid_flow_repair`: invalid graph → no persist,
  error text contains validation messages.
- `test_workflow_builder_ambiguous_entity`: duplicate names → raises → repair
  text.

Frontend (source-contract test, convention from PlusMenu.workflow.test.jsx):
- `WorkflowBuilderChat` wired in WorkflowCanvasPage (regex over SOURCE).
- Chat button opens panel; `onFlowSaved` prop present.

Browser verification:
- Open `/my-space/workflow/:id` → chat icon → type a request → workflow
  appears on canvas as draft after flow_saved → manual edit still works.

## Scope / non-goals

- No auto-publish (always draft).
- No guided input wizard; plain chat only for MVP.
- No multi-workflow management inside chat.
- No changes to db_query node form (deferred, optional polish).
- No new endpoints; reuse v2/v3 chat + workflows CRUD.

## Risks

- LLM graph JSON validity → mitigation: validate_flow repair loop (proven by
  agent_builder).
- Entity name→id ambiguity → mitigation: `_resolve_entity` raise → user
  disambiguates.
- The v3 stream already has a flow_mode branch; ensure `workflow_builder` is a
  REGULAR (non-flow_mode) agent so it does NOT hit that branch (it must run
  the normal agent loop). Verify branch ordering.
