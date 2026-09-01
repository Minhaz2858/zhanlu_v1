"""Behavioral tests for ``_poll_execution_status`` in
``app.services.automation_chat_tool``.

The Run-Automation-Task acceptance criteria require the chat agent to
deliver the end-to-end result without manual intervention and to give a
clear output confirming delivery. Two behaviors pin that down:

1. The poll window must be long enough to cover typical runs (the
   previous 5s window expired long before ~95s runs finished, so the
   agent could only report "Running" and improvised misleading
   check-back-later text). Configurable via
   ``ZHANLU_EXECUTE_AUTOMATION_POLL_S``.

2. When the run is STILL in progress at timeout, the result must carry
   an explicit ``note`` telling the LLM that the completion will be
   posted to the same chat automatically — so the agent never tells the
   user to manually check back.
"""
import importlib
import os
import sys
import types

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class _FakeExecution:
    def __init__(self, status, output_text="", error=None):
        self.status = status
        self.output_text = output_text
        self.error = error


class _FakeQuery:
    def __init__(self, execution):
        self._execution = execution

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._execution


class _FakeDB:
    def __init__(self, execution):
        self._execution = execution

    def query(self, *args, **kwargs):
        return _FakeQuery(self._execution)

    # The poll must open a FRESH session per read (context-manager style),
    # so the fake supports ``with`` blocks.
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _tool_module():
    from app.services import automation_chat_tool
    return importlib.reload(automation_chat_tool)


def _patch_session_factory(monkeypatch, mod, execution):
    """Point the module's SessionLocal at a fake returning ``execution``."""
    monkeypatch.setattr(mod, "SessionLocal", lambda: _FakeDB(execution))


def test_completed_run_returns_status_and_output(monkeypatch):
    mod = _tool_module()
    _patch_session_factory(monkeypatch, mod, _FakeExecution("completed", output_text="synced 42 rows"))
    result = mod._poll_execution_status(None, "exec-1", timeout=1.0)
    assert result["status"] == "completed"
    assert "synced 42 rows" in result["output_text"]
    assert result["error"] is None


def test_failed_run_returns_status_and_error(monkeypatch):
    mod = _tool_module()
    _patch_session_factory(monkeypatch, mod, _FakeExecution("failed", error="ERP connection refused"))
    result = mod._poll_execution_status(None, "exec-2", timeout=1.0)
    assert result["status"] == "failed"
    assert "ERP connection refused" in (result["error"] or "")


def test_timeout_result_explains_automatic_completion_post(monkeypatch):
    """A still-running result must carry a note the LLM can relay so the
    user is never told to manually check back."""
    mod = _tool_module()
    _patch_session_factory(monkeypatch, mod, _FakeExecution("running"))
    result = mod._poll_execution_status(None, "exec-3", timeout=0.6)
    assert result["status"] == "running"
    note = (result.get("note") or "").lower()
    assert note, "timeout result must include a note for the LLM"
    assert "automatic" in note or "will be posted" in note, (
        f"note must explain the completion is posted automatically, got: {note!r}"
    )


def test_poll_observes_status_committed_by_another_session():
    """REGRESSION: the poll reused the request-scoped session, whose
    identity map cached the AutomationExecution row on first read — the
    status stayed "running" forever even after the dispatcher committed
    "completed", so the agent always reported a stale "Running" status.

    Fix: each poll iteration reads through a FRESH session. This test
    commits a real row, poisons a request-style session's identity map
    with the stale "running" object, flips the status via a SECOND
    session, then asserts the poll sees the new state.
    """
    import uuid
    from app.database import SessionLocal
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_task import AutomationTask

    exec_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(AutomationTask(id=task_id, name="Poll Test Task", type="data_sync"))
        s.commit()  # parent first — no ORM relationship orders the inserts
        s.add(AutomationExecution(
            id=exec_id,
            automation_task_id=task_id,
            status="running",
        ))
        s.commit()

    # Poison a request-style session: its identity map now holds the
    # stale "running" object (exactly what the first poll read did).
    stale_session = SessionLocal()
    stale_obj = stale_session.query(AutomationExecution).filter(
        AutomationExecution.id == exec_id,
    ).first()
    assert stale_obj.status == "running"

    # Another session (the dispatcher) completes the run.
    with SessionLocal() as s:
        row = s.query(AutomationExecution).filter(
            AutomationExecution.id == exec_id,
        ).first()
        row.status = "completed"
        row.output_text = "synced 100 rows"
        s.commit()

    try:
        mod = _tool_module()
        result = mod._poll_execution_status(stale_session, exec_id, timeout=3.0)
        assert result["status"] == "completed", (
            "poll returned a stale status — the request session's identity "
            "map hid the committed state change"
        )
        assert "synced 100 rows" in result.get("output_text", "")
    finally:
        stale_session.close()
        with SessionLocal() as s:
            row = s.query(AutomationExecution).filter(
                AutomationExecution.id == exec_id,
            ).first()
            if row:
                s.delete(row)
            task = s.query(AutomationTask).filter(
                AutomationTask.id == task_id,
            ).first()
            if task:
                s.delete(task)
            s.commit()


# ---------------------------------------------------------------------------
# Idempotency: re-calling execute_automation while a run is in flight must
# ATTACH to the existing execution instead of triggering a duplicate run.
# Regression: the chat LLM re-called the tool when the first result came
# back "running", and each call spawned a NEW execution (5 duplicate runs
# observed in one chat turn).
# ---------------------------------------------------------------------------

class _FakeQueryMap:
    """Query stub returning per-model results."""

    def __init__(self, results, model):
        self._results = results
        self._model = model

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        rows = self._results.get(self._model) or []
        return rows[0] if rows else None

    def all(self):
        return list(self._results.get(self._model) or [])


class _FakeDBMap:
    def __init__(self, results):
        self._results = results

    def query(self, model, *args, **kwargs):
        return _FakeQueryMap(self._results, model)


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeTask:
    def __init__(self, tid, owner):
        self.id = tid
        self.name = "Daily Sales Data Sync"
        self.created_by_id = owner


async def test_recall_attaches_to_in_flight_execution(monkeypatch):
    """While a run is queued/running, the tool must not trigger a new one —
    it polls the in-flight execution and reports ``attached: True``."""
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_task import AutomationTask
    from app.models.user import User

    mod = _tool_module()
    in_flight = _FakeExecution("running")
    in_flight.id = "exec-in-flight"
    task = _FakeTask("task-1", "user-1")
    fake_db = _FakeDBMap({
        User: [_FakeUser("user-1")],
        AutomationTask: [task],
        AutomationExecution: [in_flight],
    })

    triggered = []

    async def _fake_trigger_now(task_id):
        triggered.append(task_id)
        return "exec-new"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _fake_trigger_now
    )
    # Poll sees the in-flight execution complete on the first read.
    monkeypatch.setattr(mod, "SessionLocal", lambda: _FakeDB(_FakeExecution("completed", output_text="done")))

    result = await mod.execute_automation_tool(
        {"task_id": "task-1"}, fake_db, "user-1"
    )
    assert triggered == [], "must not trigger a duplicate run while one is in flight"
    assert result["execution_id"] == "exec-in-flight"
    assert result.get("attached") is True
    assert result["status"] == "completed"


async def test_final_result_forbids_recall(monkeypatch):
    """Every tool result (completed AND still-running) must carry an
    explicit directive not to call the tool again for this task in this
    turn — the chat LLM otherwise retries in a loop."""
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_task import AutomationTask
    from app.models.user import User

    mod = _tool_module()
    task = _FakeTask("task-1", "user-1")
    fake_db = _FakeDBMap({
        User: [_FakeUser("user-1")],
        AutomationTask: [task],
        AutomationExecution: [],  # nothing in flight → fresh trigger
    })

    async def _fake_trigger_now(task_id):
        return "exec-new"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _fake_trigger_now
    )
    monkeypatch.setattr(mod, "SessionLocal", lambda: _FakeDB(_FakeExecution("completed", output_text="done")))

    result = await mod.execute_automation_tool(
        {"task_id": "task-1"}, fake_db, "user-1"
    )
    assert result["status"] == "completed"
    note = (result.get("note") or "").lower()
    assert "do not call" in note or "do not re-call" in note or "again" in note, (
        f"completed result must forbid re-calling the tool, got note={note!r}"
    )


def test_default_poll_window_covers_typical_runs():
    """The default window must be >= 60s (typical runs here take ~95s;
    nginx allows 120s and the SSE heartbeat holds the connection)."""
    mod = _tool_module()
    assert mod._MAX_POLL_S >= 60.0, (
        f"_MAX_POLL_S={mod._MAX_POLL_S}s is too short — runs take ~95s; "
        "the agent must see the final state to confirm delivery."
    )
    assert mod._MAX_POLL_S <= 110.0, (
        f"_MAX_POLL_S={mod._MAX_POLL_S}s leaves no headroom under nginx's "
        "120s proxy_read_timeout for the follow-up LLM call."
    )


def test_poll_window_env_override(monkeypatch):
    monkeypatch.setenv("ZHANLU_EXECUTE_AUTOMATION_POLL_S", "42")
    mod = _tool_module()
    assert mod._MAX_POLL_S == 42.0
    monkeypatch.delenv("ZHANLU_EXECUTE_AUTOMATION_POLL_S", raising=False)
    _tool_module()  # restore default
