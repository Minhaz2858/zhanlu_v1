"""Phase 1c — the chat turn path on the harness run spine.

Before this, ``agent_runs`` / ``agent_run_steps`` were written only by the
delegated execution paths, so an interactive chat turn persisted no per-call
history and ``GET /agent-runs/conversations/{id}`` was empty for chat.

These tests pin the two things that matter: the EVIDENCE is correct (one run per
turn, one step per model call / tool call, correct linkage), and RECORDING
CANNOT CHANGE A TURN (fail-open) — the same contract as the Phase 0 rule events.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.services.harness import turn_recorder as tr
from app.services.harness.turn_recorder import (
    ChatTurnRecorder,
    chat_runs_enabled,
    preview_text,
    start_recorder,
)


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    """A real SQLite DB with the run-spine tables, wired into every SessionLocal
    the recorder touches (run_service imports it at module level; the checkpoint
    sink imports it per call from app.database)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    monkeypatch.setattr("app.database.SessionLocal", Session)
    monkeypatch.setattr("app.services.harness.run_service.SessionLocal", Session)
    monkeypatch.setenv("HARNESS_CHAT_RUNS_ENABLED", "1")
    return Session


def _rec(**kw):
    base = dict(agent_name="general_assistant", conversation_id="conv-1", task="hello")
    base.update(kw)
    return ChatTurnRecorder(**base)


# ---------------------------------------------------------------------------
# Flag
# ---------------------------------------------------------------------------

def test_flag_on_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_CHAT_RUNS_ENABLED", raising=False)
    assert chat_runs_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " off "])
def test_flag_disables_recording(monkeypatch, value):
    monkeypatch.setenv("HARNESS_CHAT_RUNS_ENABLED", value)
    assert chat_runs_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_non_falsy_values_keep_recording_on(monkeypatch, value):
    monkeypatch.setenv("HARNESS_CHAT_RUNS_ENABLED", value)
    assert chat_runs_enabled() is True


def test_disabled_recorder_writes_nothing(session_factory):
    Session = session_factory
    rec = _rec(enabled=False)
    assert rec.start() == ""
    rec.model_call(iteration=0, response={"content": "hi"})
    rec.tool_call(iteration=0, tool_name="web_search")
    rec.finish(status="completed")

    db = Session()
    try:
        assert db.query(AgentRun).count() == 0
        assert db.query(AgentRunStep).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# One run per turn — and the linkage run history depends on
# ---------------------------------------------------------------------------

def test_start_creates_a_running_chat_run(session_factory):
    Session = session_factory
    rec = _rec(user_id="u1", org_id="org-1", app_id="app-1")
    run_id = rec.start()
    assert run_id

    db = Session()
    try:
        row = db.query(AgentRun).filter(AgentRun.run_id == run_id).one()
        assert row.agent_name == "general_assistant"
        assert row.mode == "chat"
        assert row.status == "running"
        assert row.started_at is not None
        # list_runs_by_conversation() matches on this key.
        assert '"conversation_id": "conv-1"' in row.caller_context
        assert '"user_id": "u1"' in row.caller_context
    finally:
        db.close()


def test_start_records_what_the_user_asked(session_factory):
    Session = session_factory
    rec = _rec(task="对一下上周的销售额")
    run_id = rec.start()
    db = Session()
    try:
        assert db.query(AgentRun).filter(AgentRun.run_id == run_id).one().task == "对一下上周的销售额"
    finally:
        db.close()


def test_start_is_idempotent(session_factory):
    Session = session_factory
    rec = _rec()
    first = rec.start()
    second = rec.start()
    assert first == second
    db = Session()
    try:
        assert db.query(AgentRun).count() == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# One step per model call / tool call
# ---------------------------------------------------------------------------

def test_model_call_persists_a_step_with_the_request_and_reply(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.model_call(
        iteration=0,
        messages=[{"role": "user", "content": "how many orders?"}],
        response={"content": "Let me look.", "tool_calls": [
            {"function": {"name": "ask_data_agent"}},
        ]},
        duration_ms=1234,
    )

    db = Session()
    try:
        step = db.query(AgentRunStep).one()
        assert step.step_type == "llm_call"
        assert step.step_index == 1
        assert step.iteration == 0
        assert step.duration_ms == 1234
        # the request is recoverable from the persisted row
        assert "how many orders?" in step.messages_snapshot
        # and so is what the model said and which tool it reached for
        assert "Let me look." in step.result_preview
        assert "ask_data_agent" in step.result_preview
    finally:
        db.close()


def test_step_index_increments_per_call(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.model_call(iteration=0, response={"content": "a"})
    rec.tool_call(iteration=0, tool_name="web_search")
    rec.model_call(iteration=1, response={"content": "b"})

    db = Session()
    try:
        steps = db.query(AgentRunStep).order_by(AgentRunStep.step_index).all()
        assert [s.step_index for s in steps] == [1, 2, 3]
        assert [s.step_type for s in steps] == ["llm_call", "tool_call", "llm_call"]
    finally:
        db.close()


def test_tool_call_step_carries_name_args_and_result(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.tool_call(
        iteration=2,
        tool_name="kanban",
        tool_args='{"action": "add_card"}',
        result_preview='{"success": true}',
        status="ok",
    )
    db = Session()
    try:
        step = db.query(AgentRunStep).one()
        assert step.tool_name == "kanban"
        assert "add_card" in step.tool_args
        assert "success" in step.result_preview
        assert step.iteration == 2
        assert step.status == "ok"
    finally:
        db.close()


def test_usage_tokens_are_captured(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.model_call(iteration=0, response={"content": "x", "usage": {
        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
    }})
    db = Session()
    try:
        step = db.query(AgentRunStep).one()
        assert (step.prompt_tokens, step.completion_tokens, step.total_tokens) == (10, 4, 14)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------

def test_finish_completes_the_run(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.finish(status="completed", iterations=3, tool_call_count=2)

    db = Session()
    try:
        row = db.query(AgentRun).one()
        assert row.status == "completed"
        assert row.finished_at is not None
        assert row.iterations == 3
        assert row.tool_call_count == 2
    finally:
        db.close()


def test_paused_is_a_valid_terminal_state(session_factory):
    """An approval pause parks the turn; it is not a completion."""
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.finish(status="paused")
    db = Session()
    try:
        assert db.query(AgentRun).one().status == "paused"
    finally:
        db.close()


def test_finish_is_idempotent(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.finish(status="paused")
    rec.finish(status="completed")  # must not overwrite the real outcome
    db = Session()
    try:
        assert db.query(AgentRun).one().status == "paused"
    finally:
        db.close()


def test_finish_records_the_error(session_factory):
    Session = session_factory
    rec = _rec()
    rec.start()
    rec.finish(status="failed", error="upstream 500")
    db = Session()
    try:
        row = db.query(AgentRun).one()
        assert row.status == "failed"
        assert "upstream 500" in row.error
    finally:
        db.close()


def test_finish_without_start_is_a_noop(session_factory):
    Session = session_factory
    rec = _rec()
    rec.finish(status="completed")  # never started
    db = Session()
    try:
        assert db.query(AgentRun).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Fail-open: recording must never change a turn
# ---------------------------------------------------------------------------

def test_recording_failure_does_not_raise_into_the_turn(session_factory, monkeypatch):
    rec = _rec()
    rec.start()

    def _boom(*a, **k):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(rec, "_sink", _boom)
    rec.model_call(iteration=0, response={"content": "x"})      # must not raise
    rec.tool_call(iteration=0, tool_name="web_search")          # must not raise
    assert rec.stats()["failures"] == 2


def test_sink_persist_failure_is_swallowed(session_factory, monkeypatch):
    """Even a DB failure inside the sink cannot break the turn."""
    from app.services.harness import checkpoint_sink as cs

    rec = _rec()
    rec.start()
    monkeypatch.setattr(
        cs.CheckpointSink, "_persist_step",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    rec.model_call(iteration=0, response={"content": "x"})  # must not raise


def test_start_failure_is_contained(monkeypatch, session_factory):
    """If the run record cannot be created, the turn carries on unrecorded."""
    monkeypatch.setattr(
        "app.services.harness.run_service.AgentRunService.create_run_record",
        lambda self, **k: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    rec = _rec()
    assert rec.start() == ""
    assert rec.stats()["failures"] == 1
    rec.finish(status="completed")  # no-op, must not raise


def test_start_recorder_helper_never_raises(monkeypatch, session_factory):
    monkeypatch.setattr(
        "app.services.harness.run_service.AgentRunService.create_run_record",
        lambda self, **k: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    rec = start_recorder(agent_name="a", conversation_id="c", task="t")
    assert isinstance(rec, ChatTurnRecorder)
    assert rec.run_id == ""


def test_stats_report_evidence_and_losses(session_factory):
    rec = _rec()
    rec.start()
    rec.model_call(iteration=0, response={"content": "x"})
    rec.tool_call(iteration=0, tool_name="web_search")
    stats = rec.stats()
    assert stats["model_calls"] == 1
    assert stats["tool_calls"] == 1
    assert stats["failures"] == 0
    assert stats["enabled"] is True
    assert stats["run_id"] == rec.run_id


def test_calls_before_start_are_ignored(session_factory):
    """No run_id means nothing to attach to — no crash, and nothing counted.

    A pre-start call is not *lost evidence* (the recorder never claimed it), so
    it is deliberately not added to the failure counter either.
    """
    rec = _rec()
    rec.model_call(iteration=0, response={"content": "x"})
    rec.tool_call(iteration=0, tool_name="web_search")
    assert rec.stats()["model_calls"] == 0
    assert rec.stats()["tool_calls"] == 0
    assert rec.stats()["failures"] == 0
    assert rec.run_id == ""


# ---------------------------------------------------------------------------
# Resume attaches to the SAME run
# ---------------------------------------------------------------------------

def test_attach_continues_an_existing_run(session_factory):
    Session = session_factory
    first = _rec()
    run_id = first.start()
    first.finish(status="paused")

    resumed = _rec()
    resumed.attach(run_id)
    assert resumed.run_id == run_id
    resumed.model_call(iteration=3, response={"content": "resumed"})

    db = Session()
    try:
        # same run, not a second one
        assert db.query(AgentRun).count() == 1
        assert db.query(AgentRunStep).count() == 1
    finally:
        db.close()


def test_attach_ignores_empty_run_id(session_factory):
    rec = _rec()
    rec.attach("")
    assert rec.run_id == ""
    rec.model_call(iteration=0, response={"content": "x"})  # no-op, no raise


def test_attach_does_not_downgrade_status(session_factory):
    """A paused run stays paused until the resumed turn finishes it."""
    Session = session_factory
    first = _rec()
    run_id = first.start()
    first.finish(status="paused")

    resumed = _rec()
    resumed.attach(run_id)
    db = Session()
    try:
        assert db.query(AgentRun).one().status == "paused"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# preview_text
# ---------------------------------------------------------------------------

def test_preview_text_handles_dicts_and_caps_length():
    assert preview_text({"a": 1}) == '{"a": 1}'
    assert preview_text(None) == ""
    long = preview_text({"blob": "x" * 5000}, limit=50)
    assert len(long) == 50


def test_preview_text_survives_unserializable_values():
    class Weird:
        def __repr__(self):
            return "<weird>"
    assert "weird" in preview_text({"obj": Weird()})
