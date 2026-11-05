"""R6 Phase 4 — §6.5 subprocess event-durability protocol (F-22, Option C).

JSONL control pipe between the automation runner (child) and the executor
(parent). Ownership per §6.5: the PARENT persists every accepted event into
``automation_execution_events``; the child NEVER touches automation tables.

Verified behaviors (crash-injection matrix #1, #2, #4, #6, #7, #9 + ACK rules):
1. Frame codec: encode/decode JSONL; partial frames discarded (matrix #2).
2. Critical events: child BLOCKS until the parent ACKs, and the parent ACKs
   ONLY after the event row is committed (matrix #4/#5 — ACK-after-commit).
3. Duplicate replay of the same (execution_id, attempt, seq, event_id) is
   absorbed by the unique index and STILL ACKed (matrix #6, §37-41); the
   same seq for a DIFFERENT event_id is a protocol violation (no second row).
4. ACK frames carry the attempt (F-26) — a stale ACK for a foreign attempt
   is skipped by the child, never applied to its own sequence.
5. Telemetry gaps are permitted + counted (``telemetry_gaps``).
6. Pipe EOF (child crash) ⇒ parent outcome ``crashed`` (maps to
   failed(transient); matrix #1). A terminal critical event before EOF ⇒
   ``completed``.
7. Bounded ingest: telemetry dropped (counted) when the queue is full;
   critical events are never dropped — they persist through the DB path
   (matrix #9).
8. Child ACK wait is bounded: silence ⇒ bounded replays (§6.5), then a
   TimeoutError — never a silent infinite hang.
9. True cross-process E2E: a real child process speaking JSONL over
   stdin/stdout with the parent broker — critical event durable + ACKed.
"""
from __future__ import annotations

import json
import os
import subprocess
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

from app.database import Base  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import (  # noqa: E402
    AutomationExecutionEvent,
)
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import subprocess_runner as sp  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Frame codec
# ---------------------------------------------------------------------------

def test_frame_codec_roundtrip_and_partial():
    frame = {"event_id": "e1", "execution_id": "ex-1", "attempt": 0,
             "seq": 3, "critical": True, "type": "run_started",
             "payload": {"worker": "w"}}
    wire = sp.encode_frame(frame)
    assert wire.endswith(b"\n")
    assert sp.decode_frame(wire) == frame
    # partial frame (matrix #2): discarded, never half-parsed
    assert sp.decode_frame(wire[: len(wire) // 2]) is None
    assert sp.decode_frame(b"not json\n") is None
    # two frames concatenated both decode (line-delimited stream)
    blob = wire + sp.encode_frame({**frame, "seq": 4})
    seqs = [sp.decode_frame(ln)["seq"] for ln in blob.splitlines()]
    assert seqs == [3, 4]


# ---------------------------------------------------------------------------
# Rig: parent broker over os.pipe pairs; child emitter driven in-thread
# ---------------------------------------------------------------------------

class _PipeRig:
    """child→parent event pipe + parent→child ack pipe (real os pipes)."""

    def __init__(self):
        ev_r, ev_w = os.pipe()
        ack_r, ack_w = os.pipe()
        self.event_read = os.fdopen(ev_r, "rb", buffering=0)
        self.event_write = os.fdopen(ev_w, "wb", buffering=0)
        self.ack_read = os.fdopen(ack_r, "rb", buffering=0)
        self.ack_write = os.fdopen(ack_w, "wb", buffering=0)


@pytest.fixture
def db(tmp_path):
    # File-backed: the broker thread and the test thread open SEPARATE
    # connections (SQLite serializes them; no shared-connection hazards).
    path = tmp_path / "ipc_test.db"
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s, engine, Session
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def rig_with_exec(db, tmp_path):
    s, engine, Session = db
    t = AutomationTask(name="IPC", type="report", prompt="p", status="active")
    s.add(t)
    s.commit()
    ex = AutomationExecution(automation_task_id=t.id, status="running",
                             attempt=0)
    s.add(ex)
    s.commit()
    exec_id = ex.id
    rig = _PipeRig()

    def _make_broker(queue_size=1000, ack_timeout=30.0):
        broker = sp.ParentEventBroker(
            event_stream=rig.event_read,
            ack_stream=rig.ack_write,
            session_factory=sessionmaker(bind=engine),
            ingest_queue_size=queue_size,
        )
        return broker

    return rig, _make_broker, exec_id


def _finish(broker, rig, timeout=10.0):
    """Close the child's write end (EOF) and let the broker conclude."""
    try:
        rig.event_write.close()
    except Exception:  # noqa: BLE001
        pass
    broker.run_to_eof(timeout)
    broker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# 2/4. Critical ACK-after-commit + attempt attribution
# ---------------------------------------------------------------------------

def test_critical_event_acked_only_after_commit(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    child.emit("run_started", {"worker": "w1"}, critical=True)
    # ACK-after-commit: the row must be durable BEFORE the unblock.
    row = (s.query(AutomationExecutionEvent)
           .filter(AutomationExecutionEvent.execution_id == exec_id,
                   AutomationExecutionEvent.seq == 0).one_or_none())
    assert row is not None and row.event_type == "run_started"
    _finish(broker, rig)
    # No terminal critical event before EOF ⇒ the child "died" mid-attempt
    # (matrix #1) — but the ACKed critical row is durable regardless.
    assert broker.outcome == "crashed"
    assert broker.protocol_violation is False
    assert broker.critical_count == 1
    assert broker.dropped_telemetry == 0


def test_stale_ack_never_applied_to_foreign_attempt(rig_with_exec, db):
    """F-26: ACK frames name their attempt — a foreign-attempt ACK is
    skipped by the child, never applied to its own sequence."""
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    # Stale ACK (attempt 0) enters the ack pipe BEFORE the child's event —
    # the child must skip it and wait for its own attempt-1 ACK.
    rig.ack_write.write(sp.encode_frame({
        "ack": True, "execution_id": exec_id, "attempt": 0,
        "ack_seq": 0, "committed_upto": 0}))
    rig.ack_write.flush()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=1,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    child.emit("run_started", {}, critical=True)  # skips stale, takes own
    row = (s.query(AutomationExecutionEvent)
           .filter(AutomationExecutionEvent.execution_id == exec_id,
                   AutomationExecutionEvent.attempt == 1,
                   AutomationExecutionEvent.seq == 0).one_or_none())
    assert row is not None
    _finish(broker, rig)


# ---------------------------------------------------------------------------
# 3. Duplicate replay absorbed + still ACKed (matrix #6 / §37-41)
# ---------------------------------------------------------------------------

def test_duplicate_replay_absorbed_and_re_acked(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    child.emit("run_started", {"n": 1}, critical=True)
    # reconnect replay: the IDENTICAL frame again (same event_id/seq) →
    # absorbed, ACKed — exactly what resend_unacked sends after a crash
    rig.event_write.write(sp.encode_frame(child.last_frame))
    rig.event_write.flush()
    rows = (s.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == exec_id).all())
    assert len(rows) == 1  # exactly-durable: no duplicate row
    assert broker.protocol_violation is False
    _finish(broker, rig)
    assert broker.critical_count == 1


def test_same_seq_different_event_is_protocol_violation(rig_with_exec, db):
    """Same (execution_id, attempt, seq) reused for a DIFFERENT event_id:
    still exactly one row (index absorb) but flagged as a violation."""
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    child.emit("run_started", {"n": 1}, critical=True)
    # a different event claiming the same seq
    rig.event_write.write(sp.encode_frame({
        "event_id": "OTHER-EVENT", "execution_id": exec_id, "attempt": 0,
        "seq": 0, "critical": True, "type": "run_failed",
        "payload": {"x": 1}}))
    rig.event_write.flush()
    _finish(broker, rig)
    assert broker.protocol_violation is True
    rows = (s.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == exec_id).all())
    assert len(rows) == 1  # never two rows for one seq


# ---------------------------------------------------------------------------
# 5. Gap rules (matrix #7)
# ---------------------------------------------------------------------------

def test_telemetry_gap_permitted_and_counted(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    child.emit("phase", {"state": "planning"}, seq=0)
    child.emit("phase", {"state": "running"}, seq=7)  # telemetry gap
    child.emit("run_finished", {"status": "succeeded"}, critical=True,
               seq=8)
    _finish(broker, rig)
    assert broker.protocol_violation is False
    assert broker.telemetry_count == 2
    assert broker.telemetry_gaps == 1  # counted, not silent
    assert broker.outcome == "completed"


# ---------------------------------------------------------------------------
# 6. EOF ⇒ crashed (matrix #1)
# ---------------------------------------------------------------------------

def test_child_eof_maps_to_crashed(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    rig.event_write.write(sp.encode_frame({
        "event_id": "t1", "execution_id": exec_id, "attempt": 0,
        "seq": 0, "critical": False, "type": "phase", "payload": {}}))
    _finish(broker, rig)
    assert broker.outcome == "crashed"
    assert broker.protocol_violation is False  # clean crash, not a violation


# ---------------------------------------------------------------------------
# 8. Bounded child ACK wait — never an infinite hang
# ---------------------------------------------------------------------------

def test_child_ack_silence_bounded_replays_then_times_out(rig_with_exec, db):
    """No broker running: the child re-sends the critical frame up to
    max_replays, then raises TimeoutError (§6.5 replay + protocol failure)."""
    rig, make_broker, exec_id = rig_with_exec
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=0.1, max_replays=2)
    import pytest as _pytest
    with _pytest.raises(TimeoutError):
        child.emit("run_started", {}, critical=True)
    # the frame was re-sent on each replay (3 total writes: 1 + 2 replays)
    rig.event_write.close()
    data = rig.event_read.read()
    assert data.count(b"run_started") == 3


# ---------------------------------------------------------------------------
# 9. Bounded ingest (matrix #9)
# ---------------------------------------------------------------------------

def test_telemetry_dropped_when_queue_full(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker(queue_size=2)
    broker.start()
    child = sp.ChildEventEmitter(
        execution_id=exec_id, attempt=0,
        event_stream=rig.event_write, ack_stream=rig.ack_read,
        ack_timeout=5.0)
    for i in range(50):
        child.emit("phase", {"i": i}, seq=i)  # telemetry: never blocks
    child.emit("run_finished", {}, critical=True, seq=100)
    _finish(broker, rig)
    assert broker.dropped_telemetry > 0  # counted loss — never silent
    # critical event survived the flood (never dropped)
    row = (s.query(AutomationExecutionEvent)
           .filter(AutomationExecutionEvent.execution_id == exec_id,
                   AutomationExecutionEvent.seq == 100).one_or_none())
    assert row is not None
    assert broker.outcome == "completed"


# ---------------------------------------------------------------------------
# 10. True cross-process E2E (real child process over stdin/stdout)
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = r"""
import sys, os
sys.path.insert(0, {backend_root!r})
os.chdir({backend_root!r})
from app.services.automation import subprocess_runner as sp

child = sp.ChildEventEmitter(
    execution_id={exec_id!r}, attempt=0,
    event_stream=sys.stdout.buffer, ack_stream=sys.stdin.buffer,
    ack_timeout=30.0)
child.emit("run_started", {{"worker": "child-proc"}}, critical=True)
child.emit("phase", {{"state": "planning"}}, seq=1)
child.emit("run_finished", {{"status": "succeeded"}}, critical=True, seq=2)
"""


def test_cross_process_jsonl_ipc_end_to_end(rig_with_exec, db):
    s, engine, Session = db
    rig, make_broker, exec_id = rig_with_exec
    broker = make_broker()
    broker.start()
    script = _CHILD_SCRIPT.format(backend_root=_BACKEND_ROOT, exec_id=exec_id)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=rig.ack_read, stdout=rig.event_write,
        stderr=subprocess.PIPE, cwd=_BACKEND_ROOT)
    rc = proc.wait(timeout=120)
    assert rc == 0, proc.stderr.read()[:2000]
    _finish(broker, rig, timeout=30.0)
    assert broker.outcome == "completed"
    assert broker.critical_count == 2
    rows = (s.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == exec_id)
            .order_by(AutomationExecutionEvent.seq).all())
    # parent persists ALL accepted events — critical AND telemetry
    assert [r.event_type for r in rows] == ["run_started", "phase",
                                            "run_finished"]
    assert rows[0].payload["worker"] == "child-proc"
    # the parent's committed_upto advanced to the last critical seq
    assert broker.last_committed_upto[(exec_id, 0)] == 2


# ---------------------------------------------------------------------------
# 11. Stage-2 launcher wiring (parent-owned persistence end-to-end)
# ---------------------------------------------------------------------------

def test_launch_subprocess_runner_parent_owned_persistence(tmp_path,
                                                           monkeypatch):
    """launch_subprocess_runner: real child process via the child
    entrypoint; the PARENT persists the child's critical event (the child
    itself never touches automation tables). Invalid job ⇒ honest
    run_failed, never a fabricated success."""
    import app.database as appdb

    path = tmp_path / "launch.db"
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")  # child env too
    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(bind=engine))

    s = sessionmaker(bind=engine)()
    t = AutomationTask(name="LNC", type="report", prompt="p", status="active")
    s.add(t)
    s.commit()
    s.add(AutomationExecution(automation_task_id=t.id, status="running",
                              attempt=0, id="ex-launch"))
    s.commit()
    task_id = str(t.id)
    s.close()

    summary = sp.launch_subprocess_runner({
        "execution_id": "ex-launch", "attempt": 0,
        "user_id": "no-such-user",  # invalid job ⇒ honest run_failed
        "org_id": "o", "app_id": "a", "conversation_id": "c",
        "automation_task_id": task_id, "message": "m",
    }, wait_timeout=180)

    assert summary["returncode"] != 0  # child failed honestly
    assert summary["protocol_violation"] is False
    assert summary["critical_count"] == 1
    assert summary["outcome"] == "completed"  # terminal critical was ACKed
    # the PARENT persisted the child's run_failed into the shared ledger
    s2 = sessionmaker(bind=engine)()
    rows = (s2.query(AutomationExecutionEvent)
            .filter(AutomationExecutionEvent.execution_id == "ex-launch")
            .order_by(AutomationExecutionEvent.seq).all())
    s2.close()
    assert [r.event_type for r in rows] == ["run_failed"]
    assert rows[0].payload["classification"] == "error"
