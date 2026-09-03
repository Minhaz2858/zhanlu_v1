# Workflow Fast-Iterate Loop Implementation Plan (Test Node + Rerun-From-Node)

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Give workflow builders n8n-style fast iteration — test a single node in isolation with editable input, and re-run from any completed node reusing upstream outputs from a chosen run.

**Architecture:** Add an optional partial-execution mode to the existing pure DAG runner (`execute_flow_graph(..., partial_from, seed_outputs)`), an `ephemeral` flag on `WorkflowExecutionContext` so single-node tests never create run rows, two thin runtime orchestrators (`test_node`, rerun path through `run_workflow_agent`), two router endpoints, one alembic migration tagging rerun runs, and NDV/RunDrawer UI wired to the existing SSE stream.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2 (backend), React 18 + @xyflow/react (frontend), pytest (backend), Vitest source-contract tests (frontend), docker compose backend + nginx frontend.

**Design doc:** `docs/superpowers/specs/2026-09-02-workflow-iterate-loop-design.md` (commit 8e320bf).

---

## Conventions (READ FIRST — from zhanlu-workflow-designer/canvas skills)

- App id path params are cosmetics; `_get_owned_workflow(db, app_id, workflow_id, user)` is the owner guard in routers/workflows.py.
- Background run threads open their OWN `SessionLocal()` (request session closes).
- `WorkflowRunStep.output_json` holds step output; `WorkflowRun` has NO output column.
- Handler contract: sync `(ctx, node) -> dict`; ctx carries `current_upstream`, `outputs`, `record_step`, deps (`llm_complete`, `run_agent`, `run_tool`, `kb_search`, `db_query`).
- Do NOT copy n8n code — patterns only (Sustainable Use License).
- Every frontend edit must be verified in-browser (build success ≠ runtime success).
- Backend test commands run INSIDE the container (`docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest ...'`); `backend/tests/` is bind-mounted so edits appear instantly.

---

### Task 1: Add `partial_from` + `seed_outputs` to the executor (pure partial mode)

**Objective:** `execute_flow_graph` can re-execute a node-and-downstream subgraph seeded by a prior run, without changing full-run behavior.

**Files:**
- Modify: `backend/app/services/workflow_executor.py:62-70` (signature + docstring), `:92-108` (preload/activation), `:108-118` (loop entry)
- Test: `backend/tests/test_workflow_executor.py`

**Step 1: Write failing tests** (append to test_workflow_executor.py; FakeCtx already has `outputs`/`current_upstream`/`record_step` per existing conventions):

```python
def _chain_flow(n1_out=None):
    # trigger(n1) -> llm(n2) -> output(n3); handlers: h1 records calls, h2/h3 echo upstream
    ...

def test_partial_rerun_reuses_seeded_upstreams():
    # partial_from='n2' with seed {'n1': {...}} -> n1 handler NOT called, n2/n3 run
def test_partial_rerun_live_node_overwrites_own_seed():
    # seed includes n2; partial_from='n2' -> n2 executes again (handler called)
def test_partial_rerun_seeded_router_keeps_reference_route():
    # router n1 seeded with {'_route': [1]} -> only port-1 branch node runs
def test_partial_rerun_missing_upstream_seed_fails_clearly():
    # partial_from='n2', NO seed for n1 -> failed with "requires upstream" message
def test_partial_no_args_keeps_full_run_behavior():
    # partial_from=None -> identical to today
```

**Step 2:** Run `python3 -m pytest tests/test_workflow_executor.py -q` — new tests FAIL (no kwargs yet).

**Step 3: Implement** in `backend/app/services/workflow_executor.py`:

- Signature: `def execute_flow_graph(flow, ctx, handlers, *, partial_from: str | None = None, seed_outputs: dict | None = None) -> dict`
- After `active` init (line ~93), when `partial_from` is set:
  ```python
  # BFS over main_outgoing from the start node -> rerun_set
  rerun_set = {partial_from}
  queue = [partial_from]
  while queue:
      cur = queue.pop(0)
      for tgt, _idx in main_outgoing.get(cur, []):
          if tgt not in rerun_set:
              rerun_set.add(tgt); queue.append(tgt)
  # Seeds = reference outputs for nodes NOT re-run; activate their edges
  seeded = {
      nid: out for nid, out in (seed_outputs or {}).items()
      if nid in node_by_id and nid not in rerun_set
  }
  ctx.outputs.update(seeded)
  for nid, out in seeded.items():
      _activate(nid, node_by_id[nid]["type"], out)
  # Pre-check: every rerun node's non-rerun upstream must be seeded
  for node in ordered:
      if node["id"] not in rerun_set:
          continue
      for src, _idx in main_upstream_map.get(node["id"], []):
          if src not in rerun_set and src not in seeded:
              return {"status": "failed",
                      "error": f"rerun from {partial_from} requires upstream {src} output from a completed run — run the full workflow first",
                      "output": None, "failed_node_id": node["id"], "failed_node_type": node["type"]}
  ```
- Loop entry (line ~108): after `nid = node["id"]`, add:
  ```python
  if partial_from is not None and nid not in rerun_set:
      continue  # seeded (or reference-skipped); not part of this rerun
  ```
- Nothing else changes; full-run path (`partial_from=None`) is byte-identical behavior. `ctx.outputs` seeds make `_render` of `{{inputs.<src>.text}}` resolve from the reference run.

**Step 4:** Run the executor tests — all PASS (old + new).

**Step 5: Commit:** `git add backend/app/services/workflow_executor.py backend/tests/test_workflow_executor.py && git commit -m "feat(workflow): executor partial mode (partial_from + seed_outputs) for rerun-from-node"`

---

### Task 2: Migration — `rerun_of` / `rerun_from_node` columns on workflow_runs

**Objective:** Tag rerun runs so history/NDV can show provenance.

**Files:**
- Create: `backend/alembic/versions/086_workflow_rerun.py`
- Modify: `backend/app/models/workflow_run.py:14-25`

**Step 1:** Write migration modeled on `085_a2a_tasks.py` (copy header/style): `revision = "086_workflow_rerun"`, `down_revision = "085_a2a_tasks"`; `op.add_column("workflow_runs", sa.Column("rerun_of", sa.String(36), nullable=True))` and `sa.Column("rerun_from_node", sa.String(64), nullable=True)`.

**Step 2:** Model: add `rerun_of: Mapped[str | None] = mapped_column(String(36), nullable=True)` and `rerun_from_node: Mapped[str | None] = mapped_column(String(64), nullable=True)` to `WorkflowRun`.

**Step 3:** Verify migration file imports (`from alembic import op; import sqlalchemy as sa`) and dry-run locally via `docker exec zhanlu-backend sh -c 'cd /app && alembic upgrade head'` (restart applies it on next boot anyway; prestart runs upgrade head).

**Step 4: Commit:** `git add backend/alembic/versions/086_workflow_rerun.py backend/app/models/workflow_run.py && git commit -m "feat(workflow): workflow_runs rerun_of/rerun_from_node columns"`

---

### Task 3: `ephemeral` mode on WorkflowExecutionContext

**Objective:** Test-node runs reuse ALL handler dependency bindings but never create a WorkflowRun/WorkflowRunStep row.

**Files:**
- Modify: `backend/app/services/workflow_runtime.py:299-379` (ctor + record_step + finish_run)
- Test: `backend/tests/test_workflow_runtime.py`

**Step 1: Failing test:** build ctx with `ephemeral=True` (monkeypatch `_sync_text_completion`); assert no `WorkflowRun` row is committed (count via db query before/after), `record_step` is a no-op, `finish_run` is a no-op.

**Step 2:** Run — FAIL (no `ephemeral` kwarg).

**Step 3: Implement:**
- Add `ephemeral: bool = False` kwarg. When True: do NOT construct/persist `self.run`, do NOT publish `run_started`; `record_step` returns immediately; `finish_run` returns immediately (set `self.run = None`; guard finish). When False — byte-identical to today.

**Step 4:** Run runtime tests — PASS.

**Step 5: Commit:** `git add backend/app/services/workflow_runtime.py backend/tests/test_workflow_runtime.py && git commit -m "feat(workflow): ephemeral execution context for single-node tests"`

---

### Task 4: Runtime `test_node` orchestrator

**Objective:** Execute ONE node with editable upstream payload, no run row, real service deps.

**Files:**
- Modify: `backend/app/services/workflow_runtime.py` (new function near run_workflow_agent)
- Test: `backend/tests/test_workflow_runtime.py` (or new `backend/tests/test_workflow_test_node.py`)

**Step 1: Failing tests:**
```python
def test_test_node_executes_handler_with_upstream_payload():  # llm node, upstreams={'n1': {'text': 'hi'}}; monkeypatch llm -> asserts prompt contains 'hi'; returns output
def test_test_node_no_run_rows_created(db):  # count workflow_runs before/after == same
def test_test_node_unknown_node_raises()  # ValueError
def test_test_node_passes_retryless_single_attempt()  # handler raising -> {"status": "failed", "error": ...}, no crash
def test_test_node_trigger_uses_trigger_input()  # trigger node, trigger_input='hello'
```

**Step 2:** Run — FAIL.

**Step 3: Implement** (following the documented handler contract, NOT the mini-graph idea):
```python
def test_node(db, org_id, app_id, agent, node_id, *, trigger_input="", upstreams=None, created_by_id=None) -> dict:
    flow = _flow_dict(agent.flow)
    node = next((n for n in (flow.get("nodes") or []) if n.get("id") == node_id), None)
    if node is None:
        raise ValueError(f"node {node_id!r} not found in workflow")
    upstreams = upstreams or {}
    ctx = WorkflowExecutionContext(db, org_id, app_id, agent, trigger_input or "",
                                   created_by_id=created_by_id, ephemeral=True)
    ctx.outputs.update(upstreams)
    ctx.current_upstream = dict(upstreams)   # direct MAIN upstreams the caller seeded
    handler = NODE_HANDLERS.get(node["type"])
    if handler is None:
        return {"status": "failed", "node_id": node_id, "node_type": node["type"],
                "output": None, "error": f"no handler for type {node['type']!r}"}
    try:
        out = handler(ctx, node)
        return {"status": "completed", "node_id": node_id,
                "node_type": node["type"], "output": out, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "node_id": node_id, "node_type": node["type"],
                "output": None, "error": str(exc)}
```
Note: `run_agent`/`db_query` deps resolve entities via the real session — the point of testing is live behavior.

**Step 4:** Run — PASS (ephemeral ctx from Task 3 makes no-row assertion true).

**Step 5: Commit:** `git add backend/app/services/workflow_runtime.py backend/tests/test_workflow_runtime.py && git commit -m "feat(workflow): test_node single-node orchestrator"`

---

### Task 5: Thread partial params through `run_workflow_agent` (+ rerun tagging)

**Objective:** Rerun-from-node uses the SAME orchestrator as full runs with the same error-workflow + finish semantics.

**Files:**
- Modify: `backend/app/services/workflow_runtime.py:665-714` (signature + ctx wiring + execute call)
- Modify: `backend/app/services/workflow_runtime.py` ctor area (accept `rerun_of`/`rerun_from_node` → set on `self.run`)
- Test: `backend/tests/test_workflow_runtime.py`

**Step 1: Failing tests:**
```python
def test_run_workflow_agent_partial_rerun():  # 3-node flow; run once (recorded); call again with partial_from='n2', seed_outputs={'n1': <step output>}; assert n1 handler not called again, new run row has rerun_of/rerun_from_node set
def test_run_workflow_agent_full_run_has_null_rerun_columns()
```

**Step 2:** Run — FAIL.

**Step 3: Implement:**
- `WorkflowExecutionContext.__init__` accepts `rerun_of: str | None = None`, `rerun_from_node: str | None = None`; when a run row IS created, set `rerun_of=rerun_of, rerun_from_node=rerun_from_node`.
- `run_workflow_agent(..., partial_from: str | None = None, seed_outputs: dict | None = None, rerun_of: str | None = None)`; pass `rerun_of` and `rerun_from_node=partial_from` into the ctx; call `execute_flow_graph(flow, ctx, NODE_HANDLERS, partial_from=partial_from, seed_outputs=seed_outputs)`.
- Error-workflow trigger stays unchanged (a failed rerun may still invoke the error workflow — consistent with full runs).

**Step 4:** Run — PASS.

**Step 5: Commit:** `git add backend/app/services/workflow_runtime.py backend/tests/test_workflow_runtime.py && git commit -m "feat(workflow): run_workflow_agent partial rerun + rerun tagging"`

---

### Task 6: Router — `POST /{workflow_id}/rerun`

**Objective:** HTTP entry: reference run + start node → background rerun streaming via existing SSE.

**Files:**
- Modify: `backend/app/routers/workflows.py` (RunBody area + new endpoint near run_workflow_async)
- Test: `backend/tests/test_workflows_router.py`

**Step 1: Failing tests** (existing router test conventions — direct handler calls w/ SimpleNamespace user + StaticPool SQLite):
```python
def test_rerun_requires_owned_workflow_and_reference_run():  # foreign workflow/run -> 404
def test_rerun_returns_run_id_and_streams():  # seeded 2-node run -> rerun -> run_id; drain SSE stream (join str/bytes — pitfall 21); assert new run completed and n1 handler not re-invoked (check step list only has n2/n3 for the new run)
def test_rerun_unknown_start_node_404()
def test_rerun_reference_run_other_agent_404()
```

**Step 2:** Run — FAIL.

**Step 3: Implement** (mirror run_workflow_async structure exactly):
```python
class RerunBody(BaseModel):
    run_id: str
    start_node_id: str
    input: str = ""

@router.post("/{workflow_id}/rerun")
def rerun_workflow(workflow_id, body: RerunBody, app_id, db=Depends(get_db), user=Depends(get_current_user_required)):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    ref = db.query(WorkflowRun).filter(WorkflowRun.id == body.run_id,
                                       WorkflowRun.agent_app_id == agent.id,
                                       WorkflowRun.is_deleted.is_(False)).first()
    if ref is None:
        raise HTTPException(status_code=404, detail="Reference run not found")
    flow = _flow_dict(agent.flow)  # reuse helper
    if not any(n.get("id") == body.start_node_id for n in (flow.get("nodes") or [])):
        raise HTTPException(status_code=404, detail="Start node not found in workflow")
    steps = db.query(WorkflowRunStep).filter(
        WorkflowRunStep.run_id == ref.id, WorkflowRunStep.is_deleted.is_(False),
        WorkflowRunStep.status == "completed").all()
    seed_outputs = {s.node_id: (s.output_json or {}) for s in steps}
    # background thread (copy run_workflow_async pattern): new run_id, own SessionLocal,
    # run_workflow_agent(..., run_id=new_run_id, trigger="manual",
    #   partial_from=body.start_node_id, seed_outputs=seed_outputs, rerun_of=ref.id)
    # publish via on_event -> publish(run_id, ev)
    return {"run_id": new_run_id}
```

**Step 4:** Run — PASS. Verify against a live flow with the host script (`backend/scripts/verify_merge_run.py` pattern; remember to `docker cp` — scripts are NOT bind-mounted).

**Step 5: Commit:** `git add backend/app/routers/workflows.py backend/tests/test_workflows_router.py && git commit -m "feat(workflow): POST /workflows/{id}/rerun endpoint"`

---

### Task 7: Router — `POST /{workflow_id}/nodes/{node_id}/test`

**Objective:** HTTP entry for single-node tests; sync, ephemeral, no run row.

**Files:**
- Modify: `backend/app/routers/workflows.py`
- Test: `backend/tests/test_workflows_router.py`

**Step 1: Failing tests:**
```python
class TestNodeBody(BaseModel): trigger_input: str = ""; upstreams: dict = {}
def test_test_node_returns_output_sync():  # llm node w/ mocked completion -> {"status":"completed","output":{...}}
def test_test_node_creates_no_run_row()
def test_test_node_owner_guard_403_for_other_user()
def test_test_node_missing_node_404()
def test_test_node_failure_returns_failed_status():  # handler raises -> failed + error, HTTP 200 (diagnostic)
```

**Step 2:** Run — FAIL.

**Step 3: Implement:** endpoint resolves agent via `_get_owned_workflow`, calls `test_node(db, agent.org_id, agent.app_id, agent, body.node_id, trigger_input=..., upstreams=body.upstreams or {}, created_by_id=user.id)` in a try (ValueError → 404), returns the dict. `node_id` comes from the PATH, not body.

**Step 4:** Run — PASS.

**Step 5: Commit:** `git add backend/app/routers/workflows.py backend/tests/test_workflows_router.py && git commit -m "feat(workflow): POST /workflows/{id}/nodes/{node_id}/test endpoint"`

---

### Task 8: Surface rerun markers in runs list + SSE payloads

**Objective:** NDV/RunDrawer can read `rerun_of`/`rerun_from_node` off runs.

**Files:**
- Modify: `backend/app/routers/workflows.py` `list_runs` (~line 413-441) serialization
- Test: extend `test_workflows_router.py`

**Step 1: Failing test:** create two runs (second is rerun) → `list_runs` returns `rerun_of`/`rerun_from_node` on the rerun row and nulls on the normal row.

**Step 2:** Run — FAIL (fields absent).

**Step 3:** In `list_runs`, serialize `rerun_of` and `rerun_from_node` from the model (check the to_dict helper used; add keys explicitly next to status/trigger).

**Step 4:** Run — PASS.

**Step 5: Commit:** `git add backend/app/routers/workflows.py backend/tests/test_workflows_router.py && git commit -m "feat(workflow): surface rerun provenance in runs list"`

---

### Task 9: Frontend API wrappers

**Objective:** Canvas code can call the two endpoints.

**Files:**
- Modify: `frontend/src/api/workflows.js`

**Step 1: Write + run source-contract or plain unit test** (`frontend/src/api/__tests__/workflows.test.js` if exists, else add exports + rely on browser verify):
```js
export function testNode(workflowId, nodeId, payload) // POST .../nodes/${nodeId}/test
export function rerunWorkflow(workflowId, { run_id, start_node_id, input }) // POST .../rerun
```
Follow the exact auth/fetch wrapper style of `runWorkflowAsync` in the same file (read it first).

**Step 2: Commit:** `git add frontend/src/api/workflows.js && git commit -m "feat(workflow): frontend testNode + rerunWorkflow API wrappers"`

---

### Task 10: NDV — "▶ Test node" button + payload drawer

**Objective:** From any node's Parameters tab, run it alone with editable input prefilled from the last run.

**Files:**
- Modify: `frontend/src/components/workflow/NodeDetailView.jsx`
- Test: create `frontend/src/components/workflow/__tests__/NodeDetailView.workflow.test.jsx` (source-contract, regex over the source — PlusMenu convention)

**Step 1: Failing source-contract tests:** assert source contains `Test node`/`测试节点` button string AND a call to `testNode(` AND prefill derives upstream ids from `node._flowEdges` (assert the source references `_flowEdges` when building payload).

**Step 2:** Run the frontend test — FAIL.

**Step 3: Implement** (read NodeDetailView first — it already fetches runs in the Output/Input effect, lines ~52-75, and computes `srcSteps` per `e.source`):
- Toolbar (next to Parameters/Output/Input tabs): `<button>` "▶ Test node" / zh "测试节点".
- Click opens an inline drawer (reuse existing panel styles): JSON textarea for the input payload + "Run test" + "Reload from last run" + Close.
- Default payload shape: `{ trigger_input: "", upstreams: { <directMainUpstreamId>: <last completed output_json> } }` built from the latest run's steps (same data the Output/Input tab loads via `listWorkflowRuns`). Direct upstream ids come from `node._flowEdges` edges where `edge.target === node.id` (use the identical srcSteps computation already present).
- No prior run → prefill `upstreams: {}` and placeholder `{"text": "..."}` guidance text in the drawer.
- "Run test" → `testNode(workflowId, node.id, payload)` → pretty-print returned `output` JSON; on `status === 'failed'` render the error inline (never toast-crash).
- Add a small caption: "Test runs ignore retry / on-error settings and are not saved to run history."

**Step 4:** Verify in browser (npm run build first; then open NDV, Test node, confirm live LLM output for a classifier node with prefilled upstream text; confirm no new row in the runs list). Chunk-hash check: grep the fetched chunk for the string literal `Test node` (pitfall 13).

**Step 5: Commit:** `git add frontend/src/components/workflow/NodeDetailView.jsx frontend/src/components/workflow/__tests__/NodeDetailView.workflow.test.jsx && git commit -m "feat(workflow): NDV test-node drawer with last-run prefill"`

---

### Task 11: RunDrawer — "↻ Rerun from here" on completed steps

**Objective:** Re-run a node + its downstream live from the drawer, streaming the new run over SSE.

**Files:**
- Modify: `frontend/src/components/workflow/RunDrawer.jsx`
- Test: create `frontend/src/components/workflow/__tests__/RunDrawer.workflow.test.jsx` (source-contract)

**Step 1: Failing source-contract tests:** source contains `rerunWorkflow(` call and the rerun action renders only for completed steps (assert the step-status guard in source, e.g. `status === 'completed'` next to the rerun handler).

**Step 2:** Run — FAIL.

**Step 3: Implement** (read RunDrawer first — it opens a NEW run via `runWorkflowAsync(workflowId, input || 'hello')` at ~line 43 and streams steps; keep that flow for the header Run button):
- Add `switchRun(newRunId)` that: clears `steps`, sets the active runId to `newRunId`, and re-subscribes to the SSE stream (the existing stream effect should key off that runId state — refactor minimally so both the initial run and the switch share one connect path; if the current code builds the stream URL inline from a local run_id, lift it to state).
- On each step row where `status === 'completed'`, render a small "↻" icon-button (title EN "Rerun from here" / zh "从此节点重跑"). Click: `rerunWorkflow(workflowId, { run_id: currentRunId, start_node_id: step.node_id })` → `switchRun(res.run_id)`.
- Show a header badge when the active run is a rerun: "Rerun from {rerun_from_node} · of {rerun_of short id}" — data comes from the run payload (Task 8) or from the switch response (add run metadata to the rerunWorkflow response by returning `{run_id, rerun_of, rerun_from_node}`).

**Step 4:** Run frontend test + browser verify: run a 3-node flow, click "Rerun from here" on the llm step → the drawer streams n2/n3 only; history shows the new run tagged. Chunk check string literal `Rerun from here`.

**Step 5: Commit:** `git add frontend/src/components/workflow/RunDrawer.jsx frontend/src/components/workflow/__tests__/RunDrawer.workflow.test.jsx && git commit -m "feat(workflow): rerun-from-here on RunDrawer completed steps"`

---

### Task 12: Full targeted suite + regression sweep

**Objective:** Nothing regressed; the iterate loop is green end-to-end.

**Files:** none (test runs)

**Step 1:** Backend (in container):
```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_executor.py tests/test_workflow_runtime.py tests/test_workflow_nodes.py tests/test_workflow_chat.py tests/test_workflow_scheduler.py tests/test_workflow_validation.py tests/test_workflows_router.py tests/test_workflow_builder_prompts.py -q'
```
Expected: ALL PASS (old + new).

**Step 2:** Frontend targeted: `cd frontend && npx vitest run src/components/workflow/__tests__/NodeDetailView.workflow.test.jsx src/components/workflow/__tests__/RunDrawer.workflow.test.jsx` (RAM-tight Mac: kill stray vitest first, run ONE file at a time if it hangs — read the log file).

**Step 3:** Full build: `cd frontend && npm run build` (expected: new hashed chunks).

**Step 4:** Restart + browser E2E (see design doc Verification recipe): full run → Test node (no history row) → Rerun from here (seeded upstream, live downstream) → history shows rerun tag. Verify chunk loaded via `performance.getEntriesByType('resource')`.

**Step 5: Commit:** `git add -A && git commit -m "test(workflow): iterate-loop regression sweep"` (only if edits surfaced during verification).

---

### Task 13: Skill docs update

**Objective:** Capture the new surfaces + pitfalls for the next builder.

**Files:**
- Modify: `.hermes/skills/zhanlu/zhanlu-workflow-designer/SKILL.md` (or wherever the local skills live per project convention — `.synexia/skills/` if this repo overrides)

**Step 1:** Patch the skill: add the two endpoints to the router list; note partial-execution semantics + ephemeral ctx; note test-node does NOT respect retryOnFail/continueOnFail; note rerun pre-check error; note WorkflowRun now carries rerun_of/rerun_from_node; note RunDrawer switchRun pattern; note migration 086.

**Step 2: Commit:** `git add <skill path> && git commit -m "docs(workflow): iterate-loop skill notes (test node + rerun)"`

---

## Definition of Done

- `execute_flow_graph` partial mode covered by executor tests (seeded reuse, router route preservation, live overwrite, missing-seed error, no-arg full-run equivalence).
- `test_node` runs any node with real deps, zero run rows; `/nodes/{id}/test` returns JSON synchronously.
- `/rerun` creates a tagged background run reusing reference upstream outputs; SSE streams live; runs list shows `rerun_of`/`rerun_from_node`.
- NDV Test-node drawer + RunDrawer rerun-from-here verified in browser; runs history clean for tests; chunks hash-verified.
- Backend + frontend targeted suites green; skill docs updated.
