# Workflow Fast-Iterate Loop — Test Node + Rerun-From-Node (n8n test/rerun parity)

Date: 2026-09-02
Status: Design (pending review — NO implementation until approved)
Component: Agent Workflow subsystem (backend executor + routers, canvas NDV + RunDrawer)
Reference: n8n v2.37 "Execute node" (test step), "Rerun from node", pinned data

## Problem

Building a workflow today is a full-graph loop: edit a node → Run the whole
workflow → read the run log → edit again. For LLM-heavy graphs (classifier
prompts, NL db_query questions, agent input_templates) each full run costs
15–90s and multiple LLM calls, so iterating on ONE node is slow and
expensive. n8n solves this with three primitives we lack:

1. **Test node** — run a single node in isolation with a sample input, no
   side-effect run history, instant feedback on that node's prompt/config.
2. **Rerun from node** — re-execute a node and everything downstream of it,
   reusing the upstream nodes' outputs from a chosen previous run instead of
   re-running them.
3. **Pinned data** — override a node's output so later runs use fixed data.

Gap confirmed by comparison to n8n v2.37 (see session gap analysis, cluster
#1). This spec ships #1 and #2; #3 is explicitly deferred (YAGNI — the two
fast primitives already cover the iterate loop; pinning adds a persistent
override store that deserves its own review).

## Goals / Non-goals

Goals:
- One-click "Test node" from the NDV: run exactly one node with an editable
  sample input, see its output JSON instantly. No new workflow_runs row.
- "Rerun from node" from the runs list / RunDrawer: pick a completed run and
  a node in it; re-execute that node + its downstream subgraph live with the
  current flow, seeding everything upstream from the chosen run's step
  outputs. Records a NEW workflow_runs row (so it shows in history) with a
  `rerun_of`/`from_node` marker.
- Both respect branch routing semantics (nodes never reached stay skipped).

Non-goals (defer to later specs):
- Pinned data / manual output override (n8n pinning).
- Mock-trigger test events (n8n "test workflow" with saved event).
- Execution-level pause/resume, per-run cost telemetry.
- Changes to the node catalog, the DAG executor's normal full-run path, or
  chat integration (chat runs stay full-graph).

## Design

### Backend

#### A. Executor: optional `partial` mode (minimal, non-invasive)

`execute_flow_graph(flow, ctx, handlers)` stays the ONLY runner. Add an
optional param:

```python
def execute_flow_graph(flow, ctx, handlers, *, partial_from: str | None = None,
                       seed_outputs: dict[str, dict] | None = None) -> dict:
```

- `seed_outputs` — preloaded `{node_id: output}` (from a reference run's
  steps). The runner writes them into `ctx.outputs` BEFORE the topo walk, and
  the topo walk does NOT re-execute nodes whose output is already seeded —
  except any node at/after `partial_from`, which executes and overwrites its
  seed.
- `partial_from` — node id. Nodes whose topo index is BEFORE the start node
  are never executed; their outputs come from `seed_outputs` (a missing seed
  = node absent = same skipped semantics downstream as today, but no skipped
  step is recorded for it — it simply does not belong to the rerun).
- Nodes from `partial_from` onward execute normally; a completed node's live
  output OVERWRITES its seed. Skipped recording unchanged.
- Router correctness: nodes upstream of the start node are never executed, so
  their `_route` decision is the reference run's decision — exactly n8n's
  "rerun keeps the taken path" behavior. The active edge set is derived the
  same way as today (from outputs present in `ctx.outputs`).

Topo-order note: `_topo_order` is already stable; "before/after" is by
topological index, not edge distance. A node in a parallel branch upstream in
topo order but unrelated to the rerun path is seeded, not re-run — correct and
cheap. If `partial_from` has no seed for a REQUIRED upstream (user deleted the
node / changed wiring since the reference run), the executor falls back to a
clear error: `failed` with "rerun from <node> requires upstream <id> output
from a completed run — re-run the full workflow first". Never silently empty.

#### B. Runtime entrypoints (`workflow_runtime.py`)

Add two thin orchestrators beside `run_workflow_agent`:

1. `test_node(db, org_id, app_id, agent, node_id, input_payload, created_by_id)`
   - Resolve the node from `agent.flow`; missing → ValueError.
   - Build a `WorkflowExecutionContext` with `trigger_input = ""` and the
     node's handler dependencies (llm_complete/run_agent/run_tool/kb_search)
     — same real bindings as full runs.
   - `ctx.outputs[node_id]` is NOT needed; the handler reads `current_upstream`
     which the executor sets. Because only ONE node executes, seed
     `ctx.current_upstream` from `input_payload` mapped to the node's direct
     MAIN upstreams: `{upstream_id: input_payload}` when the node HAS upstream
     edges, else the payload is passed as the node's own input via a synthetic
     trigger-style seed. Contract: the payload is a node OUTPUT shape
     `{"text": ..., "items": [...]}`; the NDV prefills it from the last run.
   - `record_step` is a NO-OP (test runs never touch workflow_runs/
     workflow_run_steps). Returns `{status, output, node_id, node_type}`.
   - Supplemental provider wires (ai_llm/ai_tool/ai_kb on agent nodes) are
     resolved from config like today; if they point at live entities the test
     uses them (that is the point of testing).

2. `rerun_from_node(db, org_id, app_id, agent, run_id, start_node_id,
   input_text, created_by_id, on_event)`
   - Load the reference `WorkflowRun` + its `WorkflowRunStep`s (own session —
     background thread pattern from run-async, pitfall 15).
   - Build `seed_outputs` from steps with status `completed` (read
     `output_json`, pitfall 18: steps store `output_json`).
   - Execute with `partial_from=start_node_id, seed_outputs=...`; record a
     NEW run row (`trigger='manual'`) tagged with the rerun relationship
     (`rerun_of` = reference run id, `rerun_from_node` = start node id). The
     exact storage column is resolved in the plan against the WorkflowRun
     model (a dedicated column if one exists, else a `rerun` field carried in
     the run payload) — the API response and frontend marker are stable either
     way.
   - Router nodes upstream of start are seeded (not re-run): their step in the
     reference run must be `completed`, or the rerun errors with the clear
     message above.

#### C. Router endpoints (`routers/workflows.py`)

- `POST /{workflow_id}/nodes/{node_id}/test`
  Body `{input: {text, items?}}` (both optional; absent → `{text: ""}`).
  Creator-only (same `_get_owned_workflow` guard). Returns test result JSON.
  Sync (single node = fast). This is an ephemeral diagnostic — NO run row.
- `POST /{workflow_id}/rerun`
  Body `{run_id, start_node_id, input?}`. Creates a background run (reuse the
  run-async ThreadPoolExecutor + own SessionLocal pattern) and returns
  `{run_id}` immediately; the client streams `GET /runs/{run_id}/stream` for
  live steps (existing SSE surface — zero new streaming code).

No changes to node-types, validation, publish, or the sync `/run` path.

### Frontend

#### D. NDV → "Test node" (components/workflow/NodeDetailView.jsx + RunDrawer)

- Parameters tab toolbar gains a primary button "▶ Test node" (zh: 测试节点),
  visible on every node type. Clicking opens a small drawer (reuse RunDrawer
  shell) with:
  - A JSON textarea = the node's INPUT payload, PREFILLED from the latest run:
    the merged outputs of the node's direct MAIN upstreams from the most
    recent `completed` run step of each upstream (same data the Output tab
    already loads via `GET /workflows/{id}/runs`). If no prior run exists,
    prefill `{"text": "..."}` with the node's placeholder example.
  - "▶ Run test" → `POST nodes/{node_id}/test` → renders the returned
    `output` as JSON (pretty) + status badge. Errors (ValueError, LLM
    failures, connector errors) render in the drawer, never a toast crash.
  - Buttons: "Reload from last run" (refetch prefill), Close.
- Test runs are NOT appended to the runs list (they have no run_id).

#### E. RunDrawer / runs list → "Rerun from node"

- Each step row in the RunDrawer (and the runs-history list row expansion)
  gains a context action "↻ Rerun from here" enabled only when that step's
  status is `completed`.
- Action: `POST /rerun {run_id: <current run>, start_node_id: <node>}` →
  switch the drawer into live-stream mode (existing run-async + SSE wiring in
  RunDrawer — imports already exist, verify in browser) showing the NEW run's
  steps; a badge on the run header shows "Rerun from n<id> (of <refid8>)".

#### F. State / wiring rules

- NDV already receives `nodes` + `node._flowEdges` from the page. Test-node
  prefill computes upstream ids from `_flowEdges` + node id — no new page
  state.
- Follow the pitfall 12 rule: after ANY frontend edit verify in-browser
  (build success ≠ runtime success).

### Error handling

- Unknown node / unknown run / run not owned → 404 (match existing guards).
- Node has no upstream AND payload has no `text`/`items` → test runs with
  empty input (handler decides) — documented, not blocked.
- Rerun with missing upstream seed → executor `failed` + clear message (see
  A); SSE stream stays open (run_finished carries the error), RunDrawer shows
  the error state.
- LLM/connector failures inside a tested node return the node's normal
  failure shape `{success: false, error}` — the node's own
  retryOnFail/continueOnFail do NOT apply in test mode (single-node, no DAG).
  Document on the button: "test ignores retry/on-error settings".

### Testing

Backend (targeted pytest, existing conventions — pure executor tests use
FakeCtx; handler tests already populate `ctx.outputs`):
1. Executor partial mode:
   - seed outputs present, no execution of pre-start nodes (count handler
     calls via FakeCtx)
   - live node overwrites its own seed
   - missing upstream seed → clear failed error
   - router branch: reference-seeded inactive branch nodes stay skipped
   - partial_from == full-run behavior when seed empty + start at first node
2. Runtime `test_node`: single-node execution through a REAL handler
   (llm mocked), no workflow_runs row created, current_upstream seeded from
   payload.
3. Runtime `rerun_from_node`: two-node chain — rerun from node 2 reuses node
   1's reference output (handler for node 1 NOT called); records new run with
   rerun_of/rerun_from_node metadata.
4. Router: test endpoint auth guard (creator-only), 404s, payload shape;
   rerun returns run_id + background execution completes (SSE drain helper —
   see pitfall 21: join str vs bytes chunks).
5. Frontend source-contract tests (existing convention, no RTL): NDV test
   button renders per node type; prefill URL uses `/runs`; RunDrawer rerun
   action only on completed steps (regex over source, like PlusMenu tests).

### Verification (browser recipe — follow zhanlu-workflow-canvas Verify)

1. `docker compose -p zhanlu_sep_1_n8n restart backend`
2. `cd frontend && npm run build`
3. Backend tests: `docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_executor_partial.py tests/test_workflow_test_node.py tests/test_workflow_rerun.py tests/routers/test_workflows_router.py -q'`
4. Browser: open a 3-node flow (trigger → llm → output). Run it once (full).
   NDV on the llm node → "Test node" → payload prefilled from the trigger's
   output → edit prompt wording in Parameters → Run test → output JSON
   updates without a new history row. Then in RunDrawer click "Rerun from
   here" on the llm step → live SSE shows trigger step as seeded (badge) and
   llm/output re-run → history shows the new run with rerun marker.
5. Chunk-hash verification: confirm the browser loaded the new bundle (grep
   the fetched chunk for the string literal 'Test node', NOT a function name —
   pitfall 13).

### Risks / known pitfalls to honor

- Thread runs need their own DB session (pitfall 15).
- Step payload key is `output_json` on steps, `output` on runs (pitfall 18).
- SSE drain in tests joins str OR bytes chunks (pitfall 21).
- JSON columns: any persisted rerun metadata goes on a NEW dict assignment
  (pitfall 27).
- UI option/entity values are IDs not names (pitfall 24) — irrelevant here
  (no new pickers) but the test-input JSON is raw node-output shape, document
  it in the drawer.
- Never copy n8n code — patterns only (Sustainable Use License).

### Out of scope (future specs)

- Pinned data / output override store.
- Mock trigger event testing.
- Cost/token telemetry per run; pause/resume; workflow version history
  (separate cluster #4).
