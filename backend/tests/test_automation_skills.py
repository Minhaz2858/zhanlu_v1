"""Tests for skill-orchestrated automation tasks.

Covers the four integration points introduced by the skill-orchestration
plan:

1. ``automation_executor._build_skills_context`` — progressive-disclosure
   metadata index injection (empty skills -> ""; loader failure -> "").
2. ``SkillExecutionRecorder`` — stamps ``execution_id`` on SkillRun rows and
   falls back to the executor's contextvar in ``record_from_context``.
3. ``automation_api.get_execution_status`` — 404-tolerant "pending" payload
   when the row is not yet committed, and skill_calls mapping when it is.
4. ``agent_tools`` — skills normalization on create/update (comma-separated
   string -> list, non-list -> []).
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# 1. Progressive-disclosure metadata index injection
# ---------------------------------------------------------------------------

class _FakeTask:
    def __init__(self, skills=None):
        self.skills = skills
        self.name = "Test Task"
        self.id = "task-1"


def test_build_skills_context_empty_for_no_skills(monkeypatch):
    from app.services import automation_executor
    called = []

    def _fake_metadata(names, db=None):
        called.append(names)
        return "## Available Skills\n- **foo**: does foo"

    monkeypatch.setattr(
        "app.services.skills_loader.get_skill_metadata_for_agent", _fake_metadata
    )
    assert automation_executor._build_skills_context(_FakeTask(None), None) == ""
    assert automation_executor._build_skills_context(_FakeTask([]), None) == ""
    assert called == [], "loader must not be called for empty skills"


def test_build_skills_context_injects_metadata(monkeypatch):
    from app.services import automation_executor

    def _fake_metadata(names, db=None):
        return "## Available Skills\n- **weekly_report**: builds weekly ERP reports"

    monkeypatch.setattr(
        "app.services.skills_loader.get_skill_metadata_for_agent", _fake_metadata
    )
    out = automation_executor._build_skills_context(
        _FakeTask(["weekly_report"]), None
    )
    assert "weekly_report" in out


def test_build_skills_context_degrades_on_loader_failure(monkeypatch):
    from app.services import automation_executor

    def _boom(names, db=None):
        raise RuntimeError("skill registry unavailable")

    monkeypatch.setattr(
        "app.services.skills_loader.get_skill_metadata_for_agent", _boom
    )
    assert automation_executor._build_skills_context(_FakeTask(["x"]), None) == ""


# ---------------------------------------------------------------------------
# 2. SkillExecutionRecorder execution_id stamping
# ---------------------------------------------------------------------------

class _FakeSkillSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def close(self):
        pass


def test_record_stamps_execution_id(monkeypatch):
    from app.services import skill_execution_recorder

    fake_session = _FakeSkillSession()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)

    skill_execution_recorder.SkillExecutionRecorder.record(
        skill_name="weekly_report",
        action="load",
        status="completed",
        conversation_id="conv-1",
        execution_id="exec-42",
    )
    assert fake_session.added, "record must persist a SkillRun row"
    run = fake_session.added[0]
    assert run.execution_id == "exec-42"
    assert run.conversation_id == "conv-1"


def test_record_from_context_uses_contextvar_fallback(monkeypatch):
    """When the tool context has no execution_id, the recorder must fall back
    to the executor's contextvar so automation skill calls are linkable."""
    from app.services import skill_execution_recorder

    fake_session = _FakeSkillSession()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        "app.services.automation_executor.get_current_execution_id",
        lambda: "exec-from-contextvar",
    )

    skill_execution_recorder.SkillExecutionRecorder.record_from_context(
        context={"conversation_id": "conv-9", "agent_name": "automation_runtime"},
        skill_name="weekly_report",
        action="run",
        status="completed",
    )
    run = fake_session.added[0]
    assert run.execution_id == "exec-from-contextvar"


def test_record_from_context_prefers_explicit_context_execution_id(monkeypatch):
    from app.services import skill_execution_recorder

    fake_session = _FakeSkillSession()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        "app.services.automation_executor.get_current_execution_id",
        lambda: "exec-from-contextvar",
    )

    skill_execution_recorder.SkillExecutionRecorder.record_from_context(
        context={"conversation_id": "c", "execution_id": "exec-explicit"},
        skill_name="x",
        action="load",
        status="completed",
    )
    assert fake_session.added[0].execution_id == "exec-explicit"


# ---------------------------------------------------------------------------
# 3. Status endpoint (404-tolerant + skill_calls mapping)
# ---------------------------------------------------------------------------

class _Query:
    def __init__(self, model, results):
        self._model = model
        self._results = results

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        rows = self._results.get(self._model) or []
        return rows[0] if rows else None

    def all(self):
        return list(self._results.get(self._model) or [])


class _FakeStatusDB:
    def __init__(self, results):
        self._results = results

    def query(self, model, *args, **kwargs):
        return _Query(model, self._results)


def test_status_endpoint_pending_when_row_missing():
    from app.models.automation_execution import AutomationExecution
    from app.routers.automation_api import get_execution_status

    db = _FakeStatusDB({AutomationExecution: []})
    payload = get_execution_status("exec-missing", db)
    assert payload["exists"] is False
    assert payload["status"] == "pending"
    assert payload["steps"] == []
    assert payload["skill_calls"] == []


class _ExecutionRow:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_status_endpoint_maps_skill_calls():
    from datetime import datetime, timezone
    from app.models.automation_execution import AutomationExecution
    from app.models.skill_run import SkillRun
    from app.routers.automation_api import get_execution_status

    exec_row = _ExecutionRow(
        status="running",
        activity_steps=[{"no": 1, "text": "loading skill"}],
        current_phase="executing",
        started_at=datetime.now(timezone.utc),
        completed_at=None,
        error=None,
        output_data=None,
    )
    skill_run = _ExecutionRow(
        skill_profile_id=None,
        input_json={"skill_name": "weekly_report", "action": "load", "agent_name": "automation_runtime"},
        status="completed",
        duration_ms=123,
        created_date=datetime.now(timezone.utc),
    )
    db = _FakeStatusDB({
        AutomationExecution: [exec_row],
        SkillRun: [skill_run],
    })

    payload = get_execution_status("exec-1", db)
    assert payload["exists"] is True
    assert payload["status"] == "running"
    assert payload["skill_calls"][0]["skill_name"] == "weekly_report"
    assert payload["skill_calls"][0]["action"] == "load"
    assert payload["skill_calls"][0]["status"] == "completed"
    assert payload["elapsed_sec"] is not None


def test_status_endpoint_running_with_naive_started_at():
    """Regression: real DB DateTime columns are timezone-naive (UTC). The
    running-branch elapsed computation must not raise "can't subtract
    offset-naive and offset-aware datetimes" (observed as a 500 on the live
    status endpoint for in-flight executions)."""
    from datetime import datetime, timezone
    from app.models.automation_execution import AutomationExecution
    from app.models.skill_run import SkillRun
    from app.routers.automation_api import get_execution_status

    exec_row = _ExecutionRow(
        status="running",
        activity_steps=[{"no": 1, "text": "loading skill"}],
        current_phase="executing",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),  # naive, like Postgres
        completed_at=None,
        error=None,
        output_data=None,
    )
    db = _FakeStatusDB({AutomationExecution: [exec_row], SkillRun: []})

    payload = get_execution_status("exec-1", db)
    assert payload["exists"] is True
    assert payload["status"] == "running"
    assert isinstance(payload["elapsed_sec"], float)
    assert payload["elapsed_sec"] >= 0.0
    assert payload["skill_calls"] == []


# ---------------------------------------------------------------------------
# 4. agent_tools skills normalization
# ---------------------------------------------------------------------------

def test_normalize_skills_comma_string():
    from app.services.agent_tools import _normalize_skills
    assert _normalize_skills("weekly_report, erp_writeback") == [
        "weekly_report", "erp_writeback"
    ]


def test_normalize_skills_list_and_empty():
    from app.services.agent_tools import _normalize_skills
    assert _normalize_skills(["a", "b", "c"]) == ["a", "b", "c"]
    assert _normalize_skills([]) is None
    assert _normalize_skills(None) is None
    assert _normalize_skills("  ") is None
    assert _normalize_skills(12345) is None
    # Non-string entries are dropped (defensive).
    assert _normalize_skills(["a", 5, " b "]) == ["a", "b"]
