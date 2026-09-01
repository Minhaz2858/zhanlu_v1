# Automation Phase 4 — Manus Parity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the three highest-impact gaps between Zhanlu's automation engine and Manus-style unattended agents: (1) make `skip_confirmation` actually auto-proceed past approval pauses, (2) push live run progress over SSE instead of 30s polling, (3) notify the user's chat on failure and preserve partial output when a run times out.

**Architecture:** All three changes reuse existing seams — no new infrastructure. The auto-skip reuses the existing `/resume` endpoint (the executor already calls `add_message_stream` by direct import, so it can call `resume_conversation` the same way). The SSE live-progress endpoint mirrors the already-working `sandbox.py` job-events stream + `SandboxTimeline.jsx` `EventSource` pattern. Failure notification generalizes the existing `_notify_chat` success path and reuses `_persist_run_progress`'s per-write session to preserve partial text on timeout.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / pytest (backend, `backend/tests/`); React + JSX / `EventSource` (frontend, `frontend/src/components/chat/`).

---

## Codebase orientation (read these once before starting)

These are the load-bearing files. Do not guess their contents — read them.

| File | Why it matters |
|---|---|
| `backend/app/services/automation_executor.py` | The run lifecycle. `_run_agent_in_conversation` (line 500) consumes the agent SSE stream and currently raises `_AutomationPaused` on any `paused` event (line 603-620). `_persist_run_progress` (line 470) writes activity steps to the DB row. `_notify_chat` (line 729) runs only on the success path (called at line 338). Timeout path at line 284-290 discards `assistant_text`. |
| `backend/app/services/automation_dispatcher.py` | `_run_executor` (line 275) spawns unbounded `asyncio.create_task` per due task (line 272) — no concurrency cap. |
| `backend/app/routers/agents.py` | `add_message_stream` SSE endpoint (line 4516). Emits `paused` events at lines 5418/5843/5965 (`reason: "awaiting_decision_summary"`) and **line 5914** (approval pause, **no** `reason` field). `resume_conversation` endpoint at **line 3316** reads `conv.metadata_["_resume_state"]` and continues the loop, returning `conv.to_dict()`. |
| `backend/app/routers/automation_api.py` | REST surface for the Scheduled panel. New SSE endpoint lives here. |
| `backend/app/routers/sandbox.py` | **Template to copy** for SSE: `StreamingResponse(event_generator(), media_type="text/event-stream", headers={...})` (line 203). |
| `backend/app/models/automation_execution.py` | Columns: `activity_steps` (JSON), `current_phase` (String), `output_text` (Text), `status`, `error`, `notified_session_id`. |
| `backend/app/config.py` | `AUTOMATION_RUN_TIMEOUT_SECONDS = 600` (line 252). |
| `frontend/src/components/chat/ScheduledPanel.jsx` | Polls every 30s via `setInterval(fetchPanel, 30_000)` (line 280). |
| `frontend/src/components/chat/SandboxTimeline.jsx` | **Template to copy** for `EventSource` + polling fallback (line 77). |

**Test conventions** (see `backend/tests/test_scheduled_and_alerting.py`): each test file manipulates `sys.path` to the backend root and `os.chdir(_BACKEND_ROOT)`, uses `unittest.mock`. Async tests are auto-mode via `pytest_asyncio` (`conftest.py`). Run from `backend/`: `python -m pytest tests/test_<file>.py -v`.

---

## Task 1 (P0): Make `skip_confirmation` auto-proceed past approval pauses

**Why:** This is a broken promise. The UI exposes "Always skip" and the API persists it (`automation_api.py:274`), but a run that hits an approval pause **still fails** even with the flag on (`automation_executor.py:609-614`). Manus auto-proceeds. This is the single biggest functional gap.

**Design:** Two pause types exist. They need different treatment:
- **Approval pause** (emitted at `agents.py:5914`, no `reason` field): a tool returned `requires_approval`. With `skip_confirmation=true`, auto-resume by calling the existing `resume_conversation` endpoint in a bounded loop, then read the final assistant text. This reuses battle-tested code — no new approval logic.
- **Decision-summary pause** (`reason: "awaiting_decision_summary"`, lines 5418/5843/5965): a `create_agent` interception. **Never** auto-proceed — silently creating agents unattended is dangerous. Fail fast as today, but with a clearer message.

The executor already imports and calls `add_message_stream` directly (line 557). It will call `resume_conversation` the same way.

**Files:**
- Modify: `backend/app/services/automation_executor.py` (`_run_agent_in_conversation`, lines 500-648)
- Test: `backend/tests/test_automation_skip_confirmation.py` (create)

---

**Step 1: Write the failing test**

Create `backend/tests/test_automation_skip_confirmation.py`:

```python
"""Tests for skip_confirmation auto-proceed behavior (Phase 4, Task 1)."""
import asyncio
import os
import sys
from unittest.mock import patch, AsyncMock, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def _stream_chunks(events):
    """Build a list of SSE 'data: {json}' strings from event dicts."""
    import json
    return [f"data: {json.dumps(e)}" for e in events]


def test_skip_confirmation_auto_proceeds_past_approval_pause():
    """With skip_confirmation=true, an approval pause is auto-resumed and the
    run completes instead of failing."""
    task = MagicMock()
    task.id = "t1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"; task.name = "Daily Report"
    task.skip_confirmation = "true"; task.session_id = None
    agent = MagicMock(); agent.id = "ag"; agent.name = "Reporter"

    # First stream: one approval pause (no reason field). After resume, the
    # loop re-streams and gets a 'done'.
    pause_evt = {"type": "paused"}  # no reason => approval pause
    done_evt = {"type": "done", "content": "Report finished."}
    first_stream = _stream_chunks([pause_evt])
    second_stream = _stream_chunks([done_evt])

    stream_calls = iter([first_stream, second_stream])

    async def fake_add_message_stream(**kwargs):
        return iter(next(stream_calls))

    # The resume endpoint returns a conv dict whose status is NOT
    # awaiting_approval (run completed) and whose last assistant message
    # holds the final text.
    conv_after_resume = MagicMock()
    conv_after_resume.status = "active"
    conv_after_resume.messages = [{"role": "assistant", "content": "Report finished."}]

    async def fake_resume(*, app_id, conversation_id, db, user):
        return conv_after_resume

    with patch("app.routers.agents.add_message_stream", side_effect=fake_add_message_stream), \
         patch("app.routers.agents.resume_conversation", side_effect=fake_resume):
        text, conv_id = ax._run_agent_in_conversation(task, agent, "prompt", "exec1")

    assert text == "Report finished."
    assert conv_id  # a conversation was created


def test_skip_confirmation_fails_fast_on_decision_summary_pause():
    """Decision-summary pauses (create_agent) are never auto-proceeded, even
    with skip_confirmation=true — creating agents unattended is unsafe."""
    task = MagicMock()
    task.id = "t1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"; task.name = "Daily Report"
    task.skip_confirmation = "true"; task.session_id = None
    agent = MagicMock(); agent.id = "ag"; agent.name = "Reporter"

    stream = _stream_chunks([
        {"type": "paused", "reason": "awaiting_decision_summary"},
    ])

    async def fake_add_message_stream(**kwargs):
        return iter(stream)

    with patch("app.routers.agents.add_message_stream", side_effect=fake_add_message_stream):
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused as e:
            assert "decision_summary" in str(e).lower() or "decision" in str(e).lower()


def test_skip_confirmation_bounds_auto_approvals():
    """A run that pauses for approval more than MAX_AUTO_APPROVALS times fails
    rather than looping forever."""
    task = MagicMock()
    task.id = "t1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"; task.name = "Loop"; task.skip_confirmation = "true"
    task.session_id = None
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    pause_evt = _stream_chunks([{"type": "paused"}])

    async def fake_add_message_stream(**kwargs):
        return iter(pause_evt)  # always pauses again

    conv = MagicMock()
    conv.status = "awaiting_approval"  # never completes
    conv.messages = []

    async def fake_resume(*, app_id, conversation_id, db, user):
        return conv

    with patch("app.routers.agents.add_message_stream", side_effect=fake_add_message_stream), \
         patch("app.routers.agents.resume_conversation", side_effect=fake_resume):
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused as e:
            assert "max" in str(e).lower() or "too many" in str(e).lower()


def test_skip_confirmation_false_still_raises_on_approval_pause():
    """Without skip_confirmation, an approval pause fails as before (legacy
    behavior preserved)."""
    task = MagicMock()
    task.id = "t1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"; task.name = "X"; task.skip_confirmation = "false"
    task.session_id = None
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    stream = _stream_chunks([{"type": "paused"}])

    async def fake_add_message_stream(**kwargs):
        return iter(stream)

    with patch("app.routers.agents.add_message_stream", side_effect=fake_add_message_stream):
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused:
            pass  # expected
```

**Step 2: Run the test to verify it fails**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_skip_confirmation.py -v
```
Expected: all 4 FAIL — `_run_agent_in_conversation` currently raises on every `paused` event regardless of `skip_confirmation`, and has no resume loop.

**Step 3: Implement the auto-proceed loop**

In `backend/app/services/automation_executor.py`, add a module constant near the top (after `_PREV_CONTEXT_MAX_CHARS`, ~line 136):

```python
# Cap on consecutive auto-approvals per run when skip_confirmation=true.
# Bounds runaway loops where the agent keeps hitting approval gates.
MAX_AUTO_APPROVALS = 5
```

Then rewrite the `paused` event branch inside `_consume()` (currently `automation_executor.py:603-620`) and restructure `_run_agent_in_conversation` so the SSE consumer is wrapped in a loop that can resume. Replace the `elif etype == "paused":` block with:

```python
                    elif etype == "paused":
                        reason = evt.get("reason", "")
                        if reason == "awaiting_decision_summary":
                            # Never auto-create agents unattended — even with
                            # skip_confirmation. Fail fast with a clear reason.
                            raise _AutomationPaused(
                                "Agent paused for a decision summary "
                                "(create_agent). This pause type is never "
                                "auto-skipped — trigger the run manually to "
                                "approve agent creation."
                            )
                        # Approval pause (no reason). Auto-proceed only when
                        # skip_confirmation is on; otherwise fail as before.
                        if not skip_conf:
                            raise _AutomationPaused(
                                "Agent paused for user confirmation. For "
                                "unattended scheduled runs, enable "
                                "skip_confirmation on the task; otherwise "
                                "trigger the run manually when you can "
                                "approve it."
                            )
                        # skip_conf=True: signal the outer loop to resume.
                        raise _ApprovalPausedSignal()
```

Add a small sentinel exception above `_run_agent_in_conversation` (after the `_AutomationPaused` class, ~line 48):

```python
class _ApprovalPausedSignal(Exception):
    """Internal control-flow signal: an approval pause was hit and
    skip_confirmation is on. The outer loop catches it, calls the resume
    endpoint, and re-consumes the stream. NOT raised to the executor."""
```

Now restructure the body of `_run_agent_in_conversation` so the stream consumption + resume is a loop. Replace the `try: ... asyncio.set_event_loop(loop)` block (lines ~564-622, the part that defines `_consume` and calls `loop.run_until_complete(_consume())`) with:

```python
        approvals = 0
        try:
            asyncio.set_event_loop(loop)

            async def _consume():
                nonlocal final_text, current_phase
                async for chunk in add_message_stream(
                    app_id=task.app_id or "default-app",
                    conversation_id=conv.id,
                    body={"role": "user", "content": prompt},
                    db=db,
                    user=None,
                ):
                    evt = _parse_sse_chunk(chunk)
                    if not evt:
                        continue
                    etype = evt.get("type")
                    if etype == "activity_step":
                        step = evt.get("step") or {}
                        num = step.get("number")
                        replaced = False
                        if num is not None:
                            for i, s in enumerate(activity_steps):
                                if s.get("number") == num:
                                    activity_steps[i] = {**s, **step}
                                    replaced = True
                                    break
                        if not replaced:
                            activity_steps.append(dict(step))
                        _persist_run_progress(execution_id, activity_steps, current_phase)
                    elif etype == "phase":
                        current_phase = evt.get("state") or current_phase
                        _persist_run_progress(execution_id, activity_steps, current_phase)
                    elif etype == "done":
                        final_text = evt.get("content") or ""
                    elif etype == "error":
                        raise RuntimeError(
                            f"agent stream error: {str(evt.get('message', ''))[:500]}"
                        )
                    elif etype == "paused":
                        reason = evt.get("reason", "")
                        if reason == "awaiting_decision_summary":
                            raise _AutomationPaused(
                                "Agent paused for a decision summary "
                                "(create_agent). This pause type is never "
                                "auto-skipped — trigger the run manually to "
                                "approve agent creation."
                            )
                        if not skip_conf:
                            raise _AutomationPaused(
                                "Agent paused for user confirmation. For "
                                "unattended scheduled runs, enable "
                                "skip_confirmation on the task; otherwise "
                                "trigger the run manually when you can "
                                "approve it."
                            )
                        raise _ApprovalPausedSignal()

            # Initial turn.
            try:
                loop.run_until_complete(_consume())
            except _ApprovalPausedSignal:
                pass  # handled by the resume loop below

            # Auto-approve loop: each resume continues the turn and may pause
            # again for another approval. Bound by MAX_AUTO_APPROVALS.
            from app.routers.agents import resume_conversation as _resume
            while final_text == "" and approvals < MAX_AUTO_APPROVALS:
                approvals += 1
                logger.info(
                    "execute_automation: %s auto-resuming after approval pause "
                    "(#%d/%d)", execution_id, approvals, MAX_AUTO_APPROVALS,
                )

                async def _do_resume():
                    return await _resume(
                        app_id=task.app_id or "default-app",
                        conversation_id=conv.id,
                        db=db,
                        user=None,
                    )

                resumed_conv = loop.run_until_complete(_do_resume())
                # resume_conversation returns conv.to_dict(); check status.
                still_paused = (
                    (resumed_conv or {}).get("status") == "awaiting_approval"
                ) if isinstance(resumed_conv, dict) else False
                if still_paused:
                    continue  # loop: resume again
                # Run completed. Read the final assistant text below.
                break

            if final_text == "" and approvals >= MAX_AUTO_APPROVALS:
                raise _AutomationPaused(
                    f"Run hit the auto-approval cap ({MAX_AUTO_APPROVALS}) — "
                    f"too many consecutive approval pauses. Trigger the run "
                    f"manually to investigate."
                )
        except _AutomationPaused:
            raise
        except Exception as e:
            logger.warning("add_message_stream failed: %s\n%s", e, traceback.format_exc())
            raise
        finally:
            try:
                loop.close()
            except Exception:
                pass
```

The existing fallback block (lines ~636-642) that reads the last assistant message from `conv` when `final_text` is empty already covers the post-resume case — `resume_conversation` persists the final assistant message, so `db.refresh(conv)` + the reversed scan picks it up. No change needed there.

**Step 4: Run the tests to verify they pass**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_skip_confirmation.py -v
```
Expected: all 4 PASS.

**Step 5: Run the linter / compile check**

```bash
cd /root/zhanlu/backend && python -m py_compile app/services/automation_executor.py && python -m pytest tests/ -k "paused or approval or scheduled" -v
```
Expected: compiles clean; no existing pause/approval tests regress.

**Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_skip_confirmation.py
git commit -m "feat(automation): skip_confirmation auto-proceeds past approval pauses (P0)

Approval pauses are now auto-resumed via the existing /resume endpoint
(bounded by MAX_AUTO_APPROVALS=5) when skip_confirmation=true, instead of
failing the run. Decision-summary pauses (create_agent) are never
auto-skipped. Closes the broken-promise gap where 'Always skip' failed runs
anyway."
```

---

## Task 2 (P1): Push live run progress over SSE instead of 30s polling

**Why:** The "live" activity feed is up to 30s stale (`ScheduledPanel.jsx:280`). A 10-minute run shows progress in 30s jumps, not real-time. Manus streams live steps. The infrastructure already exists — `sandbox.py` streams job events over SSE and `SandboxTimeline.jsx` consumes them with `EventSource` + polling fallback. Mirror it exactly.

**Design:** Add `GET /api/automations/executions/{id}/events/stream` — an SSE endpoint that tails the execution row (polls the DB every ~1.5s server-side) and yields `activity_step`/`phase`/`done`/`error` events as they change. The executor already persists progress via `_persist_run_progress`, so there's nothing to change on the producer side. The frontend switches from 30s `setInterval` to `EventSource` while a run is `queued`/`running`, keeping the 30s poll as a fallback when SSE fails or no run is active.

**Files:**
- Modify: `backend/app/routers/automation_api.py` (add SSE endpoint)
- Modify: `frontend/src/components/chat/ScheduledPanel.jsx` (poll → EventSource)
- Test: `backend/tests/test_automation_live_stream.py` (create)

---

**Step 1: Write the failing test**

Create `backend/tests/test_automation_live_stream.py`:

```python
"""Tests for the automation execution live SSE stream (Phase 4, Task 2)."""
import json
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from fastapi.testclient import TestClient


def _collect_stream(client, url, max_events=10):
    """Open an SSE stream and collect up to max_events data payloads."""
    events = []
    with client.stream("GET", url) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
                if len(events) >= max_events:
                    break
    return events


def test_live_stream_yields_terminal_done_event():
    """A completed execution's stream yields a 'done' event immediately."""
    from app.main import app
    from app.database import SessionLocal
    from app.models.automation_execution import AutomationExecution

    db = SessionLocal()
    try:
        ex = AutomationExecution(
            id="exec-stream-1", automation_task_id="t1", status="completed",
            attempt=0, org_id="o", app_id="a", created_by_id="u",
            output_text="hello", activity_steps=[{"number": 1, "title": "done"}],
        )
        db.add(ex); db.commit()
    finally:
        db.close()

    client = TestClient(app)
    events = _collect_stream(client, "/api/automations/executions/exec-stream-1/events/stream", max_events=3)
    types = [e.get("type") for e in events]
    assert "done" in types


def test_live_stream_404_for_missing_execution():
    """Unknown execution id returns 404, not an infinite empty stream."""
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/automations/executions/does-not-exist/events/stream")
    assert r.status_code == 404
```

**Step 2: Run the test to verify it fails**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_live_stream.py -v
```
Expected: FAIL — route doesn't exist (404 for the first case too, or connection error).

**Step 3: Implement the backend SSE endpoint**

In `backend/app/routers/automation_api.py`, add the import at the top with the other FastAPI imports (line 25):

```python
from fastapi.responses import FileResponse, StreamingResponse
```
(Replace the existing `from fastapi.responses import FileResponse` line.)

Add the endpoint after `get_execution_details` (after line 267), before `SkipConfirmationRequest`:

```python
@router.get("/executions/{execution_id}/events/stream")
def stream_execution_events(execution_id: str):
    """Server-Sent Events stream of one execution's live progress.

    Tails the execution row and yields events as activity_steps / phase /
    status change. Terminates with a ``done`` or ``error`` event when the
    run reaches a terminal status, then closes the stream.

    Mirrors the sandbox job-events stream (sandbox.py). The Scheduled panel
    consumes this with EventSource and falls back to 30s polling if SSE
    fails.
    """
    import json
    import time

    row = db_check = SessionLocal().query(AutomationExecution).filter(
        AutomationExecution.id == execution_id,
    ).first()
    db_check.close()
    if not row:
        raise HTTPException(status_code=404, detail="Execution not found")

    def _sse(obj):
        return f"data: {json.dumps(obj, default=str)}\n\n"

    def event_generator():
        last_steps_sig = None
        last_phase = None
        last_status = None
        deadline = time.time() + 3600  # hard cap 1h per stream
        while time.time() < deadline:
            s = SessionLocal()
            try:
                ex = s.query(AutomationExecution).filter(
                    AutomationExecution.id == execution_id,
                ).first()
                if not ex:
                    yield _sse({"type": "error", "message": "Execution deleted"})
                    return
                # Emit step/phase deltas.
                steps = ex.activity_steps or []
                sig = json.dumps(steps, default=str, sort_keys=True)
                if sig != last_steps_sig:
                    last_steps_sig = sig
                    yield _sse({"type": "activity_steps", "steps": steps})
                phase = ex.current_phase
                if phase != last_phase:
                    last_phase = phase
                    if phase:
                        yield _sse({"type": "phase", "state": phase})
                status = ex.status
                if status != last_status:
                    last_status = status
                if status in ("completed", "failed", "skipped"):
                    if status == "completed":
                        yield _sse({
                            "type": "done",
                            "content": (ex.output_text or "")[:4000],
                            "execution_id": execution_id,
                        })
                    else:
                        yield _sse({
                            "type": "error",
                            "message": (ex.error or "Run failed")[:500],
                            "status": status,
                            "execution_id": execution_id,
                        })
                    return
            finally:
                s.close()
            time.sleep(1.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Step 4: Run the backend tests to verify they pass**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_live_stream.py -v
```
Expected: both PASS.

**Step 5: Implement the frontend EventSource consumer**

In `frontend/src/components/chat/ScheduledPanel.jsx`, replace the 30s-poll `useEffect` (lines 278-282) with an SSE-driven refresh while a run is in progress. Read the file first to confirm current line numbers, then replace:

```javascript
  // Auto-refresh every 30s while open, so a run that just completed shows up
  // without the user having to close & re-open the panel.
  useEffect(() => {
    if (!open) return undefined;
    const id = setInterval(fetchPanel, 30_000);
    return () => clearInterval(id);
  }, [open, fetchPanel]);
```

with:

```javascript
  // Live progress: when a run is queued/running, stream updates over SSE
  // (Manus-style real-time feed). Falls back to 30s polling when no run is
  // active or SSE isn't available.
  const liveRun = useMemo(
    () => executions.find(
      (e) => e.status === "queued" || e.status === "running"
    ),
    [executions]
  );

  useEffect(() => {
    if (!open) return undefined;
    // 30s poll as the always-on fallback (covers SSE gaps + idle panels).
    const id = setInterval(fetchPanel, 30_000);

    // SSE: only while a live run exists.
    let source = null;
    if (liveRun) {
      try {
        source = new EventSource(
          `/api/automations/executions/${liveRun.id}/events/stream`
        );
        source.onmessage = (e) => {
          try {
            const evt = JSON.parse(e.data);
            if (["done", "error", "activity_steps", "phase"].includes(evt.type)) {
              fetchPanel(); // refresh from the source of truth
            }
          } catch { /* ignore malformed */ }
        };
        source.onerror = () => {
          // SSE failed — the 30s poll keeps working. Close so the browser
          // doesn't retry-spam.
          source && source.close();
        };
      } catch {
        source = null;
      }
    }
    return () => {
      clearInterval(id);
      source && source.close();
    };
  }, [open, fetchPanel, liveRun?.id]);
```

Add `useMemo` to the React import at the top of the file if it isn't already imported (check the existing `import { useState, useEffect, ... } from 'react';` line and add `useMemo`).

**Step 6: Verify the frontend compiles**

```bash
cd /root/zhanlu/frontend && npm run build 2>&1 | tail -20
```
Expected: build succeeds (no syntax/type errors). If the project uses a lint step, run it too.

**Step 7: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/automation_api.py backend/tests/test_automation_live_stream.py frontend/src/components/chat/ScheduledPanel.jsx
git commit -m "feat(automation): live run progress over SSE (P1)

Adds GET /api/automations/executions/{id}/events/stream (mirrors the sandbox
job-events pattern). ScheduledPanel now uses EventSource while a run is
queued/running and keeps the 30s poll as fallback. The activity feed is now
real-time instead of up to 30s stale."
```

---

## Task 3 (P1): Notify chat on failure + preserve partial output on timeout

**Why:** Today a failed run sits silently in the panel — the user only sees it if they happen to open Scheduled (`_notify_chat` runs only on the success path, `automation_executor.py:338`). Worse, when a run times out, all the assistant text produced before the hang is **discarded** (line 284-290). Manus pushes failure alerts into chat and preserves partial progress. Two related fixes, one task.

**Design:**
1. **Partial output:** extend `_persist_run_progress` to also write the accumulated `final_text` so far to `output_text` on each call. On timeout, the row already holds the partial — `_mark_failed` reads it and the failure notification includes it.
2. **Failure notification:** add `_notify_chat_failure` (mirrors `_notify_chat` but with a failure tone + the error + partial output). Call it from `_mark_failed` and `_mark_failed_no_retry` when `task.notify_chat` is true and a session exists.

**Files:**
- Modify: `backend/app/services/automation_executor.py` (`_persist_run_progress`, `_mark_failed`, `_mark_failed_no_retry`, timeout path, add `_notify_chat_failure`)
- Test: `backend/tests/test_automation_failure_notify.py` (create)

---

**Step 1: Write the failing test**

Create `backend/tests/test_automation_failure_notify.py`:

```python
"""Tests for failure chat notification + partial output (Phase 4, Task 3)."""
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def test_persist_run_progress_writes_partial_output():
    """_persist_run_progress stores partial text so a later timeout retains it."""
    with patch("app.database.SessionLocal") as FakeSession:
        fake_db = MagicMock()
        FakeSession.return_value = fake_db
        ax._persist_run_progress("exec1", [{"number": 1}], "Working",
                                  partial_text="partial report so far")
        # An UPDATE against output_text must have been issued.
        assert fake_db.execute.called
        stmt = fake_db.execute.call_args[0][0]
        # The compiled SQL should reference output_text.
        assert "output_text" in str(stmt).lower() or "output_text" in str(
            stmt.compile(compile_kwargs={"literal_binds": True})
        ).lower()


def test_notify_chat_failure_creates_message_with_error_and_partial():
    """_notify_chat_failure writes a ChatMessage containing the error + partial
    output (not a silent failure)."""
    from app.models.chat_message import ChatMessage
    task = MagicMock()
    task.name = "Daily Report"; task.session_id = "sess1"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"
    execution = MagicMock(); execution.id = "exec1234567890"
    execution.error = "Run timed out after 600s"
    partial = "## Half-finished report\nSome findings..."

    captured = []
    def fake_add(obj):
        captured.append(obj)
        return None

    with patch.object(ax, "ChatMessage", side_effect=lambda **kw: _CapturedMsg(**kw)):
        msg = ax._notify_chat_failure(
            MagicMock(),  # db (unused — patched ChatMessage + no commit path)
            task, execution, partial,
        )
    assert msg is not None
    assert "Daily Report" in msg.content
    assert "timed out" in msg.content.lower()
    assert "Half-finished report" in msg.content
    assert msg.role == "assistant"


class _CapturedMsg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_mark_failed_notifies_chat_when_enabled():
    """_mark_failed calls _notify_chat_failure when notify_chat is true."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "true"; task.max_retries = "0"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    # CAS rowcount=1 (we won the mark), then task lookup returns task.
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.first.return_value = task

    with patch.object(ax, "schedule_retry", return_value=None) as sr, \
         patch.object(ax, "_notify_chat_failure") as nf:
        ax._mark_failed(db, execution, "boom")
    # No retry (max_retries=0) but failure notification should fire.
    nf.assert_called_once()
    sr.assert_not_called()
```

**Step 2: Run the test to verify it fails**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_failure_notify.py -v
```
Expected: all 3 FAIL — `_persist_run_progress` has no `partial_text` param, `_notify_chat_failure` doesn't exist, `_mark_failed` never calls it.

**Step 3: Implement partial-output persistence**

In `backend/app/services/automation_executor.py`, extend `_persist_run_progress` (line 470) to accept and write partial text. Replace its signature and the `values` construction:

```python
def _persist_run_progress(
    execution_id: str, steps: list, phase: Optional[str],
    partial_text: Optional[str] = None,
) -> None:
    """Write the current activity_steps + phase to the execution row so the
    Scheduled panel can poll live progress (Manus-style activity feed).

    Also persists ``partial_text`` (the assistant output accumulated so far)
    when supplied, so a hung-LLM timeout still retains whatever the agent
    produced before it stalled — the executor reads it back on the timeout
    path and includes it in the failure notification.

    Uses its OWN short-lived session — never the agent stream's session — to
    avoid interfering with the stream's transaction management. Safe to call
    from the executor sub-thread. Failures are non-fatal (progress is
    best-effort; the run itself must not depend on it).
    """
    if not execution_id:
        return
    try:
        from app.database import SessionLocal
        pdb = SessionLocal()
        try:
            values: dict = {"activity_steps": list(steps)}
            if phase:
                values["current_phase"] = phase[:50]
            if partial_text is not None:
                values["output_text"] = partial_text[:200_000]
            pdb.execute(
                update(AutomationExecution)
                .where(AutomationExecution.id == execution_id)
                .values(**values)
            )
            pdb.commit()
        finally:
            pdb.close()
    except Exception as e:
        logger.debug("_persist_run_progress: failed (non-fatal): %s", e)
```

Then thread partial text into the calls inside `_consume()`. Add a `partial_text` accumulator near `final_text`/`current_phase` (line 561 area):

```python
        final_text = ""
        partial_text = ""  # accumulated assistant text for timeout recovery
        activity_steps: list = []
        current_phase: Optional[str] = None
```

In `_consume()`, on `delta` and `done` events, accumulate and persist partial. Add this branch (before the `done` branch or extend `done`):

```python
                    elif etype == "delta":
                        partial_text += evt.get("content") or ""
                        # Persist periodically (every ~2000 chars) to bound DB writes.
                        if len(partial_text) % 2000 < 50:
                            _persist_run_progress(
                                execution_id, activity_steps, current_phase, partial_text
                            )
                    elif etype == "done":
                        final_text = evt.get("content") or ""
                        partial_text = final_text
```

(If the stream emits no `delta` events — only a final `done` — `partial_text` is still set on `done`. If the stream emits neither before a timeout, `partial_text` stays `""`, which is correct.)

Now persist the final partial before returning, so the row is current even if no timeout occurs. Just before the `return final_text or "(no response)", conv_id` line (line 643), add:

```python
        if partial_text and not final_text:
            final_text = partial_text
        _persist_run_progress(execution_id, activity_steps, current_phase, final_text or partial_text)
```

**Step 4: Implement failure notification + wire it into the failure paths**

Add `_notify_chat_failure` after `_notify_chat` (after line 800). It mirrors `_notify_chat` but with a failure tone and includes the error + partial output:

```python
def _notify_chat_failure(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    partial_output: str,
) -> Optional[ChatMessage]:
    """Drop a ChatMessage into the user's chat alerting them that a scheduled
    run failed, including the error and any partial output the agent produced
    before the failure (so the user isn't left with a silent gap).

    Mirrors ``_notify_chat`` (the success path) but is called from the
    failure paths. Returns the created ChatMessage, or None if there's no
    session to notify.
    """
    if not task.session_id:
        from app.models.chat_session import ChatSession
        sess = db.query(ChatSession).filter(
            ChatSession.created_by_id == task.created_by_id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(ChatSession.created_date.desc()).first()
        if not sess:
            return None
        session_id = sess.id
    else:
        session_id = task.session_id

    preview = (partial_output or "").strip()
    if len(preview) > 600:
        preview = preview[:600].rsplit(" ", 1)[0] + "…"

    err = (execution.error or "Run failed")[:500]
    body = (
        f"**⚠️ Scheduled run failed: {task.name}**\n\n"
        f"**Error:** {err}\n\n"
        + (f"**Partial output:**\n{preview}\n\n" if preview else "")
        + f"_Failed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · "
        f"execution id `{execution.id[:8]}`_"
    )

    msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=body,
        order=int(datetime.utcnow().timestamp()),
        tool_calls=None,
        activity_steps=None,
        artifacts=None,
        phase={
            "verb": "⚠️",
            "title": f"Scheduled run failed: {task.name}",
            "execution_id": execution.id,
            "automation_task_id": task.id,
            "failed": True,
        },
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
    )
    db.add(msg)
    db.commit()
    execution.notified_session_id = session_id
    db.commit()
    return msg
```

Wire it into `_mark_failed` (line 408) — after the retry decision, add a failure notification when `notify_chat` is enabled. After the `else:` block (line 442-446), before the function ends, add:

```python
    # Phase 4: alert the user's chat that the run failed (Manus parity).
    # Only when notify_chat is on and a session exists. Best-effort.
    if str(getattr(task, "notify_chat", "") or "").lower() in ("1", "true", "yes"):
        try:
            _notify_chat_failure(db, task, execution, execution.output_text or "")
        except Exception as ne:
            logger.warning("_mark_failed: failure notify failed: %s", ne)
```

Do the same in `_mark_failed_no_retry` (line 61) — after `db.commit()` (line 79), before the function ends, add the same block (re-fetch task if needed; `_mark_failed_no_retry` doesn't currently load `task`, so load it):

```python
    # Phase 4: alert the user's chat (no-retry failures like pauses).
    try:
        t = db.query(AutomationTask).filter(
            AutomationTask.id == execution.automation_task_id,
        ).first()
        if t and str(getattr(t, "notify_chat", "") or "").lower() in ("1", "true", "yes"):
            _notify_chat_failure(db, t, execution, execution.output_text or "")
    except Exception as ne:
        logger.warning("_mark_failed_no_retry: failure notify failed: %s", ne)
```

Finally, the timeout path (line 284-290) currently calls `_mark_failed` with only the timeout message. Since `_persist_run_progress` now writes `partial_text` to `output_text` during the run, `execution.output_text` already holds the partial at timeout time. But the execution object in `execute_automation` is stale (the sub-thread wrote to the row directly). Refresh it before marking failed so `_notify_chat_failure` sees the partial. Replace the timeout block (line 284-290):

```python
        except FuturesTimeout:
            pool.shutdown(wait=False, cancel_futures=True)
            # Refresh so we pick up the partial output the sub-thread
            # persisted via _persist_run_progress before the hang.
            db.refresh(execution)
            _mark_failed(
                db, execution,
                f"Run timed out after {settings.AUTOMATION_RUN_TIMEOUT_SECONDS}s",
            )
            return
```

**Step 5: Run the tests to verify they pass**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_automation_failure_notify.py -v
```
Expected: all 3 PASS.

**Step 6: Run the broader automation + executor tests to check for regressions**

```bash
cd /root/zhanlu/backend && python -m py_compile app/services/automation_executor.py && python -m pytest tests/ -k "automation or scheduled or paused or approval" -v
```
Expected: compiles clean; no regressions.

**Step 7: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/automation_executor.py backend/tests/test_automation_failure_notify.py
git commit -m "feat(automation): notify chat on failure + preserve partial output (P1)

Failed runs now drop a ChatMessage alert (error + partial output) instead of
failing silently. _persist_run_progress stores accumulated assistant text as
output_text during the run, so a timeout retains whatever the agent produced
before it hung. Closes the silent-failure gap."
```

---

## Verification (after all three tasks)

Run the full automation test surface + a compile check across all touched files:

```bash
cd /root/zhanlu/backend && \
  python -m py_compile app/services/automation_executor.py app/routers/automation_api.py && \
  python -m pytest tests/ -k "automation or scheduled or paused or approval or skip_confirmation or live_stream or failure_notify" -v
```

Then a quick manual smoke test of the SSE endpoint (with a running backend):

```bash
# Should stream events then close on a completed execution:
curl -N http://localhost:5002/api/automations/executions/<some-exec-id>/events/stream
```

---

## Out of scope (follow-up Phase 5)

These were identified but deliberately deferred — note them in the next plan:
- **Bounded concurrency / worker pool** (`automation_dispatcher.py:272` spawns unbounded tasks). Add an `asyncio.Semaphore(AUTOMATION_MAX_CONCURRENCY)` around `_run_executor`.
- **Cross-run context** is crude head+tail truncation (`_PREV_CONTEXT_MAX_CHARS=6000`). Replace with structured delta extraction.
- **Structured output validation** — automation output doesn't pass through the `SYNEXIA_QUALITY_GATE`.
- **Tick precision** is 60s (`TICK_INTERVAL`); sub-minute schedules are imprecise.
- **Single-process assumption** — CAS makes multi-worker safe but every worker ticks redundantly.

---

## Notes for the implementing engineer

- **Read each file before editing it.** Line numbers shift as you edit; the references here are anchors, not exact-after-edits.
- The executor is called from a **sub-thread with its own event loop** (`asyncio.new_event_loop()`). Any new `await` calls must go through `loop.run_until_complete(...)`, not a bare `await` — match the existing pattern.
- `_persist_run_progress` uses a **separate session per call** deliberately. Don't refactor it to share the stream's session — that corrupts the stream's transaction management.
- `resume_conversation` returns `conv.to_dict()` (a plain dict), not a stream. The auto-approve loop checks `status == "awaiting_approval"` on that dict to decide whether to loop again.
- If `EventSource` isn't available in the target browser environment, the 30s poll fallback already covers it — don't add a polyfill.
