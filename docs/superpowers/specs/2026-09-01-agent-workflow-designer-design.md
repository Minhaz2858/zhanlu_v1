# Agent Workflow Designer — Design Spec

Date: 2026-09-01
Status: Draft (pending user review)
Scope: n8n-style visual workflow designer for Zhanlu, canvas-centered.

## 1. Overview

Users build AI agent workflows visually on an infinite canvas (n8n-style):
drag nodes, connect ports, configure each node, save/publish, then chat with
the resulting workflow agent from the main chatbot "+" dialog.

The workflow IS an agent: a normal `AgentApp` row with `flow_mode=true` and
the graph stored in the existing `flow` JSON column. This reuses the whole
agent stack for free — chat binding (`?agent=`), sessions, runs, memories,
sharing — nothing new to build for the integration surface.

## 2. Goals / Non-Goals

Goals:
- Canvas-centered editor (node palette left, canvas center, settings right)
- 7 MVP node types that cover real AI workflows
- Chat with a published workflow agent end-to-end (streaming)
- Zero migration: `flow` + `flow_mode` already exist on `agent_apps`

Non-goals (later phases):
- Connectors entity, schedule/webhook triggers, versioning, node marketplace
- Full n8n parity (loops, complex routing, sub-workflows)

## 3. UX — Canvas Center (per user requirement)

Editor route: `/my-space/workflow/:id` — opens STRAIGHT into the canvas.
The My Space "Agent Workflow" tab is only a list/grid picker that links here.

Layout (n8n-inspired, Zhanlu-styled):
- Top bar: workflow name (inline edit), status chip (draft/published),
  Validate / Save / Publish / Run buttons, runs history button
- Left rail: node palette, grouped (Triggers, AI, Logic, Output), searchable,
  collapsible. Drag onto canvas or click-to-add
- Center: infinite canvas (xyflow/react), grid background, zoom, minimap,
  node selection box. Nodes snap, ports auto-connect on drag
- Right: settings drawer for the selected node — per-type config form
  (LLM model/prompt/temperature, Agent picker, Tool picker, KB picker,
  Condition builder, Output format). Opens on node click
- Bottom: run drawer — live step-by-step execution log when testing from
  the editor (each node = one row, status, latency, output preview)
- Validation: red border + message on nodes with missing required config;
  cannot publish with invalid nodes or dangling edges

## 4. Data Model

No migration needed. `agent_apps` gains semantics only:

- `flow_mode = true` marks a workflow agent
- `flow` = JSON graph:

```json
{
  "version": 1,
  "nodes": [
    {
      "id": "n_1",
      "type": "trigger_chat",
      "position": {"x": 0, "y": 0},
      "data": {"label": "Chat Trigger", "config": {}}
    },
    {
      "id": "n_2",
      "type": "llm",
      "position": {"x": 300, "y": 0},
      "data": {"label": "LLM", "config": {
        "model": "automatic", "prompt": "You are...", "temperature": 0.3
      }}
    },
    {
      "id": "n_3",
      "type": "output",
      "position": {"x": 600, "y": 0},
      "data": {"label": "Output", "config": {"format": "markdown"}}
    }
  ],
  "edges": [
    {"id": "e_1", "source": "n_1", "sourceHandle": "out", "target": "n_2", "targetHandle": "in"},
    {"id": "e_2", "source": "n_2", "sourceHandle": "out", "target": "n_3", "targetHandle": "in"}
  ]
}
```

- `status` field reuses existing enum: draft / published (published = runnable
  from chat)
- Conventional agent columns (`name`, `description`, `prompt_identity`, …)
  stay usable; canvas LLM nodes carry their own prompts

Supporting tables (Phase 2+, new migration):
- `workflow_runs` (id, agent_app_id, trigger, status, started/finished,
  error, created_by) — one row per execution
- `workflow_run_steps` (id, run_id, node_id, node_type, status, latency_ms,
  output_json, error) — per-node results for the run drawer + audit

## 5. Node Catalog v1 (7 types)

| Type | Group | Config |
|---|---|---|
| trigger_chat | Triggers | (none — fires with user message) |
| llm | AI | model, prompt, temperature, max_tokens, system prompt |
| agent | AI | target AgentApp id (invokes existing agent runtime as sub-agent) |
| tool | AI | tool/skill id from Toolkit |
| kb_search | AI | KnowledgeBase id, top_k, query (or template) |
| condition | Logic | branches (if/else on upstream output, string/number/contains) |
| output | Output | format (text/markdown/json), optional final prompt |

Data flow between nodes: each node receives upstream outputs as
`inputs.<nodeId>`, reads/writes a shared run context. Node handlers are
pure functions of (config, inputs, context) → output.

## 6. Executor (backend)

New service `backend/app/services/workflow_executor.py`:

- Entry: `execute_workflow(agent, trigger_input, session_ctx)` — used by both
  the editor "Run" (test) and the chat path
- Topological sort of `flow.nodes` by `edges`; cycle detection → validation
  error; dangling edges rejected at validate time
- Node handlers: `trigger_chat`, `llm`, `agent`, `tool`, `kb_search`,
  `condition`, `output` — each in its own function, registered in a dispatch
  table (extensible for Phase 4 nodes)
- The `agent` node calls the existing agent pipeline (AgentApp → agent run),
  so sub-agent execution, tool use and memories work unchanged
- The `llm` node calls the same LLM provider layer as normal chat
- Streaming: step events pushed on the existing SSE/run-step channel so
  Chat.jsx renders node progress; `output` node result is the final message
- Failure handling: per-node try/except → step marked failed → error routed
  to `output` node (or default error bubble); run marked failed
- Editor "Run" reuses the same executor with a mock trigger input (prefill
  prompt box in the run drawer)

## 7. API

Entity CRUD comes free via Base44 (`/api/apps/{app}/entities/AgentApp`).
Additions (new router `backend/app/routers/workflows.py`):

- `GET  /api/apps/{app}/workflows` — list flow_mode agents
- `GET  /api/apps/{app}/workflows/{id}` — graph
- `PUT  /api/apps/{app}/workflows/{id}` — save graph (validates)
- `POST /api/apps/{app}/workflows/{id}/validate` — validation report
- `POST /api/apps/{app}/workflows/{id}/run` — test run (returns run_id,
  streams steps)
- `GET  /api/apps/{app}/workflows/{id}/runs` — run history
- `POST /api/apps/{app}/workflows/{id}/publish` — status → published

## 8. Chat Integration

- New Task dialog ("+" button): new "Workflow Agents" group listing
  published `flow_mode` agents — selecting one opens chat with
  `?agent=<id>` (existing mechanism, zero new plumbing)
- Chat.jsx already binds `activeAgent` from `?agent=`; when the agent has
  `flow_mode=true`, the send path calls `execute_workflow(...)` with the
  user message as trigger input instead of the normal agent loop
- Streaming UI: reuse existing agent run-step rendering in the message
  thread; workflow steps appear as a collapsible execution card
- Session/memory: workflow runs live under the same chat session /
  AgentConversation as any agent chat — history and re-runs just work

## 9. Testing

- Executor unit tests: linear chain, condition branching, agent-node
  invocation (mock AgentApp), cycle detection, dangling edge rejection,
  node failure → output fallback
- Validation tests: missing required config per node type
- Frontend: canvas render (palette, drag-add, connect, select), node
  settings drawer per type, save→validate→publish flow, chat "+" dialog
  lists published workflows only
- E2E: create workflow in canvas → publish → open chat → select workflow
  agent → message → streamed answer with step card (Playwright)

## 10. Build Order (MVP)

1. Backend: workflow executor + router + validate/run/publish + workflow_runs
   tables (migration) + tests
2. Frontend canvas: xyflow integration, palette, drawer, save/validate/
   publish, run drawer (against real API)
3. My Space: Agent Workflow tab (lists flow_mode agents, create button →
   empty canvas)
4. Chat integration: New Task dialog group + flow_mode send path + step card
5. Polish: node snap/undo/redo, minimap, keyboard, i18n (zh/en)

## 11. References

- UX reference: n8n editor (node palette, canvas, settings drawer, run log)
- License note: n8n is fair-code (Sustainable Use License) — patterns and
  UX are referenced, NO n8n code is imported; canvas uses xyflow/react (MIT)
- Existing assets: agent_apps.flow/flow_mode (unused schema), agent runtime
  (agent_runs/agent_run_steps/agent_invocations), automation dispatcher,
  streaming chat, Base44 entity CRUD
