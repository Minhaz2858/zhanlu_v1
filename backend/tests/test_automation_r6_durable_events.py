"""R6 Phase 3 — durable AutomationExecutionEvent wiring (TS §1.4 / F-26).

The model + attempt-scoped uniqueness were proven in Stage-2 S2-6. This
phase wires the append-only ledger into the run loop:

* recorder service: attempt-scoped seq (MAX+1 per (execution_id, attempt)),
  duplicate absorb (racing recovery writer) as a COUNTED drop, bounded-loss
  telemetry (never raises), critical events ACK-after-commit (row visible
  from ANOTHER session before the caller proceeds; commit failure
  propagates — fail-closed), crash-recovery seq resume.
* executor telemetry-point wiring: ``_persist_run_progress`` mirrors each
  progress write as a ``progress`` telemetry event (attempt resolved from
  the execution row); terminalization emits critical ``run_started`` /
  ``run_finished`` / ``run_failed`` events.
"""
from __future__ import annotations

import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, SessionLocal  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import (  # noqa: E402
    AutomationExecutionEvent,
)
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import execution_events as ev  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _run(db, attempt=0):
    t = AutomationTask(name=f"DUR {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="active")
    db.add(t)
    db.commit()
    ex = AutomationExecution(automation_task_id=t.id, status="running",
                             attempt=attempt)
    db.add(ex)
    db.commit()
    return ex


# ---------------------------------------------------------------------------
# Recorder semantics
# ---------------------------------------------------------------------------

def test_attempt_scoped_seq_and_cross_attempt_reuse(db):
    ex = _run(db)
    for i in range(3):
        assert ev.record_execution_event(
            db, ex.id, 0, "tool_progress", {"i": i}) is True
    # attempt 1 restarts seq at 0 (F-26) — legal, no collision
    assert ev.record_execution_event(
        db, ex.id, 1, "run_started", {}) is True
    rows = (db.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == ex.id)
            .order_by(AutomationExecutionEvent.attempt,
                      AutomationExecutionEvent.seq).all())
    assert [(r.attempt, r.seq) for r in rows] == [(0, 0), (0, 1), (0, 2), (1, 0)]


def test_duplicate_seq_absorbed_as_counted_drop(db, monkeypatch):
    """Race window: a recovery writer claims the same MAX+1 seq between this
    writer's computation and its INSERT — the UNIQUE constraint arbitrates;
    the recorder absorbs it as a counted (returned) drop, no corruption."""
    ex = _run(db)
    assert ev.record_execution_event(db, ex.id, 0, "phase", {}) is True
    # The racing writer's committed row (seq 1) lands first...
    db.add(AutomationExecutionEvent(execution_id=ex.id, attempt=0, seq=1,
                                    event_type="tool_progress",
                                    payload={"other": True}))
    db.commit()
    # ...then this writer (stale MAX read) also tries seq 1 → collision.
    monkeypatch.setattr(ev, "next_seq", lambda db, eid, attempt: 1)
    assert ev.record_execution_event(db, ex.id, 0, "tool_progress",
                                     {"i": 1}) is False
    # Session usable again; state uncorrupted (exactly 2 rows so far).
    count = (db.query(AutomationExecutionEvent)
             .filter(AutomationExecutionEvent.execution_id == ex.id).count())
    assert count == 2
    # And the real sequence continues cleanly from MAX+1.
    monkeypatch.undo()
    assert ev.record_execution_event(db, ex.id, 0, "tool_progress",
                                     {"i": 2}) is True
    seqs = [r.seq for r in db.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == ex.id)
            .order_by(AutomationExecutionEvent.seq).all()]
    assert seqs == [0, 1, 2]


def test_telemetry_bounded_loss_never_raises(db, monkeypatch):
    ex = _run(db)
    # broken session factory → the convenience wrapper swallows and counts
    def _boom():
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(ev, "_session_factory", _boom)
    assert ev.record_telemetry_event(ex.id, "progress", {"x": 1}) is False


def test_critical_ack_after_commit_visible_cross_session(db):
    """ACK-after-commit: the row is durable BEFORE record returns — visible
    from an independent session (the ACK boundary)."""
    ex = _run(db)
    s2 = db.sessionmaker() if hasattr(db, "sessionmaker") else None
    engine = db.get_bind()
    Other = sessionmaker(bind=engine)
    ok = ev.record_execution_event(
        db, ex.id, 0, "run_started", {"worker": "w1"}, critical=True)
    assert ok is True
    other = Other()
    try:
        row = (other.query(AutomationExecutionEvent)
               .filter(AutomationExecutionEvent.execution_id == ex.id,
                       AutomationExecutionEvent.attempt == 0,
                       AutomationExecutionEvent.seq == 0).one_or_none())
        assert row is not None and row.event_type == "run_started"
    finally:
        other.close()


def test_critical_commit_failure_propagates_fail_closed(db, monkeypatch):
    ex = _run(db)

    def _boom_commit():
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "commit", _boom_commit)
    with pytest.raises(RuntimeError):
        ev.record_execution_event(db, ex.id, 0, "run_finished", {},
                                  critical=True)


def test_seq_resumes_after_crash_new_session(db):
    """Crash between events: a fresh session (recovery loop) continues the
    attempt-scoped sequence from MAX+1 — never replays a used seq."""
    ex = _run(db)
    assert ev.record_execution_event(db, ex.id, 0, "run_started", {}) is True
    assert ev.record_execution_event(db, ex.id, 0, "tool_progress", {}) is True
    fresh = db.sessionmaker() if hasattr(db, "sessionmaker") else None
    engine = db.get_bind()
    Other = sessionmaker(bind=engine)
    s2 = Other()
    try:
        ok = ev.record_execution_event(s2, ex.id, 0, "run_finished", {})
        assert ok is True
        seqs = [r.seq for r in s2.query(AutomationExecutionEvent)
                .filter(AutomationExecutionEvent.execution_id == ex.id,
                        AutomationExecutionEvent.attempt == 0)
                .order_by(AutomationExecutionEvent.seq).all()]
        assert seqs == [0, 1, 2]
    finally:
        s2.close()


def test_convenience_emitters(db):
    ex = _run(db)
    assert ev.emit_run_started(db, ex.id, 0, worker="w1") is True
    assert ev.emit_run_finished(db, ex.id, 0, status="succeeded") is True
    assert ev.emit_run_failed(db, ex.id, 0, classification="timeout",
                              error="deadline") is True
    types = [r.event_type for r in db.query(AutomationExecutionEvent)
             .filter(AutomationExecutionEvent.execution_id == ex.id)
             .order_by(AutomationExecutionEvent.seq).all()]
    assert types == ["run_started", "run_finished", "run_failed"]


# ---------------------------------------------------------------------------
# Executor wiring
# ---------------------------------------------------------------------------

def test_persist_run_progress_records_telemetry_event(monkeypatch):
    """Each _persist_run_progress call mirrors a bounded-loss ``progress``
    event; attempt is resolved from the execution row."""
    from app.services.automation_executor import _persist_run_progress

    s = SessionLocal()
    t = AutomationTask(name=f"WIR {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="active")
    s.add(t)
    s.commit()
    ex = AutomationExecution(automation_task_id=t.id, status="running",
                             attempt=2)
    s.add(ex)
    s.commit()
    ex_id = ex.id
    s.close()

    _persist_run_progress(ex_id, [{"number": 1, "description": "d",
                                   "status": "running"}], "planning")

    s2 = SessionLocal()
    try:
        rows = (s2.query(AutomationExecutionEvent)
                .filter(AutomationExecutionEvent.execution_id == ex_id)
                .order_by(AutomationExecutionEvent.seq).all())
        assert len(rows) >= 1
        evt = rows[0]
        assert evt.attempt == 2  # resolved from the execution row (F-26)
        assert evt.event_type == "progress"
        assert evt.payload["current_phase"] == "planning"
        assert evt.payload["steps"] == 1
    finally:
        s2.close()


def test_persist_run_progress_never_raises_on_event_failure(monkeypatch):
    """Telemetry stays bounded-loss even when the ledger rejects writes."""
    from app.services.automation_executor import _persist_run_progress

    monkeypatch.setattr(ev, "record_telemetry_event",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("ledger down")))
    _persist_run_progress("nonexistent-execution-id", [], "planning")  # no raise
