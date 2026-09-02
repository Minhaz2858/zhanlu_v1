# Sandboxed Workflow Code Node — Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. Direct execution, targeted pytest (RAM-tight).

**Goal:** Run the workflow Code node's user Python inside the REAL Docker sandbox — the same path agents use for skills (`SandboxService → SandboxJob → sandbox-worker → container --network none → destroy`). The in-process thread sandbox stays ONLY as a dev/test fallback (mirroring the worker's own `_execute_in_process`).

**Why this node:** it is the only node that executes user-authored code. db_query/http_request/llm are governed in-process data/LLM access (the sandbox is network-none; sandboxing them would require opening network and defeat the isolation — same boundary the agent system uses).

## Architecture (reuses the existing skills-runner pattern)

```
workflow_runtime.run_code(code, item)
  └─ workflow_sandbox.run_code_in_sandbox(db, code, item, timeout)
       ├─ input_package = { runner_script: <workflow_code_runner.py b64>,
       │                   runner_script_name: "workflow_code_runner.py",
       │                   config.json: { code, input_item } }
       ├─ SandboxService(db).create_job(skill_name="workflow_code",
       │      image_name="zhanlu-sandbox-python:latest",
       │      timeout_seconds=timeout+30)
       ├─ poll get_job(status) every 1s until completed/failed (cap)
       ├─ read event_type == "workflow_code_result" → data_json["result"]
       └─ return same dict shape as today: {success, text, items} | {success:False, text}
  fallback (Docker unavailable / enqueue fail): existing thread sandbox

sandbox_worker.execute_job
  └─ after collect_outputs: if skill_name == "workflow_code":
       read /output/result.json → record_event("workflow_code_result",
       data_json={"result": {...}})

workflow_code_runner.py (runs INSIDE the container)
  └─ reads /input/config.json {code, input_item}
     executes with the SAME restricted globals as today (safe builtins,
     json, math, input=item; no __import__/open/network)
     writes /output/result.json {"output": <json>} or {"error": "<msg>"}
```

## Task 1: Runner script + workflow_sandbox service (TDD)

**Files:**
- Create: `backend/app/services/workflow_sandbox.py` (runner script constant + `run_code_in_sandbox`)
- Create: `backend/tests/test_workflow_sandbox.py`

**Step 1: Failing tests:**
- Runner semantics (embedded script executed with restricted globals in-process to test its logic): assigns output → JSON; non-serializable → error; missing output → error; exception → error.
- `run_code_in_sandbox` success path with mocked `SandboxService` (create_job returns job; get_job eventually completed; events include workflow_code_result) → returns `{success, text, items}`.
- `run_code_in_sandbox` fallback when `create_job` raises → runs the thread sandbox (existing behavior) and still returns the dict.

**Step 2:** Implement:
- `WORKFLOW_CODE_RUNNER` script: reads `/input/config.json`; restricted `exec`; writes `/output/result.json` (exit 0 on success, 1 + {"error"} on failure).
- `run_code_in_sandbox(db, code, item, timeout)`:
  - builds package (b64 runner + config.json via prepare-style dict)
  - `SandboxService(db).create_job(skill_name="workflow_code", image_name=settings sandbox python image, timeout_seconds=min(timeout+30, 300))`
  - poll loop: `get_job(job_id)` every 1.0s; cap at timeout+30; on `completed` → find event `workflow_code_result` → `data_json["result"]`; on `failed` → `{success: False, text: job.error_message or "Sandbox failed"}`; on timeout → `{success: False, text: "Code timed out in sandbox"}`
  - any exception → fallback to thread sandbox (import from workflow_runtime lazily to avoid cycles; or keep the restricted-exec helper in workflow_sandbox and have run_code call it)
- Refactor: move the restricted-exec logic into a shared helper `_run_restricted(code, item, timeout)` in `workflow_sandbox.py`; `workflow_runtime.run_code` calls `workflow_sandbox.run_code_sandboxed(...)` first, falls back to `_run_restricted`.

**Step 3:** Run — pass. Commit.

## Task 2: Worker result hook (TDD-lite)

**Files:**
- Modify: `backend/sandbox_worker/main.py`

**Step 1:** Add after `collect_outputs(output_dir)` (line ~283):
```python
if job.skill_name == "workflow_code":
    _record_workflow_code_result(service, job_id, outputs)
```
Helper: find `result.json` in outputs → `json.loads` → `service.record_event(job_id, "workflow_code_result", "Workflow code result captured", {"result": parsed})`. Missing file → record `{"result": {"success": False, "text": "runner produced no result.json"}}`.

**Step 2:** Unit test the helper with a fake service (record events list) + fake outputs. (No Docker needed.)

**Step 3:** Commit.

## Task 3: E2E verify + commit

- Restart backend (new service) — worker does NOT need restart? It DOES (main.py change) → `docker compose restart backend sandbox-worker`.
- Manual: create a workflow with Code node (`output = {"greeting": "hi " + str(input.get("name", ""))}`), connect a Manual Trigger → Run → confirm: SandboxJob created (`workflow_code`), status completed, node output correct, container destroyed.
- Confirm the in-process fallback path still passes the existing workflow runtime tests (`tests/test_workflow_runtime.py`).
- Commit.

## Notes / decisions
- Latency: container spawn adds ~1-4s per Code node — acceptable (matches n8n local workers); the graph stays synchronous.
- Sandbox job timeout = code timeout + 30s container overhead (cap 300s).
- No migration; output flows through the existing `SandboxJobEvent.data_json` column.
- Follow-ups (documented, NOT this batch): route LLM node through the sandbox LLM proxy (skill-driven runners already do this); async workflow runs via the worker for full graph isolation.
