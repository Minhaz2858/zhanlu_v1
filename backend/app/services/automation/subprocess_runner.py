"""R6 Phase 4 — §6.5 subprocess event-durability protocol (F-22, Option C).

JSONL control pipe between the automation runner child (which runs the real
agent harness) and the executor parent (which owns ALL automation-table
persistence: leases, fencing, state, events, delivery — §11.4).

Ownership (normative):
* The child NEVER writes automation tables. It emits typed frames:
  ``{event_id, execution_id, attempt, seq, critical, type, payload}``.
* The parent persists every accepted event into ``automation_execution_events``
  and ACKs ``{ack, execution_id, attempt, ack_seq, committed_upto}`` ONLY
  after the row is committed (ACK-after-commit — matrix #4/#5).
* Critical events BLOCK the child until the ACK (synchronous checkpoint,
  natural backpressure — critical events can never overflow or be dropped).
* Telemetry is bounded-loss: dropped when the ingest queue is full, counted
  in ``dropped_telemetry`` — never silently ignored (Option C, F-22).
* ACK frames carry the attempt (F-26): a stale ACK from a previous attempt
  is never applied to the current one.
* Gap rules: a critical seq jump > +1 over the last critical seq is a
  counted telemetry gap (permitted); the parent EOFs → ``crashed`` (maps to
  failed(transient) per §6.5 "Child crash").
* Duplicate replay of the same (execution_id, attempt, seq) is absorbed by
  the UNIQUE index and STILL ACKed (idempotent — matrix #6, §37-41).

Frame codec and both endpoints live here so tests can drive them over real
os.pipes in-process AND over a true cross-process JSONL channel.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import select
import threading
import uuid
from typing import BinaryIO, Optional

from app.models.automation_scheduling import AutomationExecutionEvent
from app.services.automation.execution_events import next_seq

logger = logging.getLogger(__name__)

TERMINAL_EVENT_TYPES = ("run_finished", "run_failed", "run_cancelled")


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------

def encode_frame(frame: dict) -> bytes:
    """One JSONL frame: compact JSON + newline."""
    return (json.dumps(frame, separators=(",", ":"), default=str)
            + "\n").encode("utf-8")


def decode_frame(raw) -> Optional[dict]:
    """Decode one JSONL frame; partial/corrupt frames → None (matrix #2)."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        s = raw.strip()
        if not s:
            return None
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Child side — the runner's event emitter (no DB access)
# ---------------------------------------------------------------------------

class ChildEventEmitter:
    """Emits §6.5 frames over the control pipe. Critical events block until
    the parent's attempt-matched ACK arrives (backpressured, exactly-durable
    delivery semantics as observed by the child).

    The wait is BOUNDED (§6.5 replay/reconnect): on ACK silence the un-ACKed
    critical frame is re-sent (the parent absorbs duplicates via the unique
    index); after ``max_replays`` unanswered replays a TimeoutError surfaces
    — a blocked-forever child is a protocol failure, never a silent hang.
    """

    def __init__(self, execution_id: str, attempt: int,
                 event_stream: BinaryIO, ack_stream: BinaryIO,
                 *, ack_timeout: float = 30.0, max_replays: int = 3):
        self.execution_id = execution_id
        self.attempt = attempt
        self.event_stream = event_stream
        self.ack_stream = ack_stream
        self.ack_timeout = ack_timeout
        self.max_replays = max_replays
        self._seq = 0
        self.unacked_critical: Optional[dict] = None

    def _next_seq(self) -> int:
        # F-26: each attempt's sequence starts at 0.
        seq = self._seq
        self._seq += 1
        return seq

    def emit(self, event_type: str, payload: Optional[dict] = None,
             *, critical: bool = False, seq: Optional[int] = None) -> None:
        frame = {
            "event_id": uuid.uuid4().hex,
            "execution_id": self.execution_id,
            "attempt": self.attempt,
            "seq": self._next_seq() if seq is None else seq,
            "critical": bool(critical),
            "type": event_type,
            "payload": payload or {},
        }
        self.last_frame = frame
        if seq is not None and seq > self._seq:
            self._seq = seq
        self.event_stream.write(encode_frame(frame))
        self.event_stream.flush()
        if not critical:
            return
        # Blocking checkpoint: wait for the attempt-matched ACK.
        self.unacked_critical = frame
        self._wait_for_ack(frame)
        self.unacked_critical = None

    def resend_unacked(self) -> None:
        """Reconnect replay (§6.5): re-send the un-ACKed critical event —
        the parent absorbs the duplicate via the unique index and re-ACKs."""
        if self.unacked_critical is not None:
            self.event_stream.write(encode_frame(self.unacked_critical))
            self.event_stream.flush()
            self._wait_for_ack(self.unacked_critical, _replayed=True)
            self.unacked_critical = None

    def _wait_for_ack(self, frame: dict, _replayed: bool = False) -> None:
        want_seq = frame["seq"]
        replays = 1 if _replayed else 0
        while True:
            # Bounded wait: select() with timeout — a silent/broken parent
            # can never wedge the child forever (protocol failure > hang).
            ready = select.select([self.ack_stream], [], [],
                                  self.ack_timeout)[0]
            if not ready:
                replays += 1
                if replays > self.max_replays:
                    self.unacked_critical = None
                    raise TimeoutError(
                        f"no parent ACK for {frame['type']}#{want_seq} "
                        f"after {replays} replays — protocol failure")
                # §6.5 replay: re-send; the parent absorbs the duplicate.
                self.event_stream.write(encode_frame(frame))
                self.event_stream.flush()
                continue
            line = self.ack_stream.readline()
            ack = decode_frame(line)
            if ack is None or not ack.get("ack"):
                continue
            # F-26: an ACK only applies to its own attempt — a stale ACK
            # from a previous attempt is never applied here.
            if (ack.get("execution_id") == self.execution_id
                    and ack.get("attempt") == self.attempt
                    and ack.get("ack_seq", -1) >= want_seq):
                return
            # foreign/stale ACK → keep waiting


# ---------------------------------------------------------------------------
# Parent side — the executor's ingest broker (owns persistence)
# ---------------------------------------------------------------------------

class ParentEventBroker:
    """Reads JSONL frames from the child's control pipe, persists them, and
    ACKs critical events after commit (§6.5 Option C).

    One broker instance per child process. ``outcome`` after EOF:
    ``completed`` (a terminal critical event was seen) or ``crashed``
    (child died mid-attempt → maps to failed(transient) per §6.5).
    """

    def __init__(self, event_stream: BinaryIO, ack_stream: BinaryIO,
                 session_factory, *, ingest_queue_size: int = 10_000):
        self.event_stream = event_stream
        self.ack_stream = ack_stream
        self.session_factory = session_factory
        self.ingest_queue_size = ingest_queue_size
        # state
        self.outcome: Optional[str] = None
        self.protocol_violation = False
        self.critical_count = 0
        self.telemetry_count = 0
        self.telemetry_gaps = 0
        self.dropped_telemetry = 0
        self.partial_frames = 0
        self.last_critical_seq: dict[tuple, int] = {}
        self.last_committed_upto: dict[tuple, int] = {}
        # The terminal critical frame (run_finished/run_failed/run_cancelled)
        # — the parent's finalization input (the child NEVER writes state).
        self.terminal_event: Optional[dict] = None
        self._telemetry_q: "queue.Queue[dict]" = queue.Queue(
            maxsize=ingest_queue_size)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="automation-event-broker")
        self._thread.start()

    def run_to_eof(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        try:
            self.event_stream.close()
        except Exception:  # noqa: BLE001
            pass
        self.run_to_eof(timeout)
        self._drain_telemetry(final=True)

    # -- internals ---------------------------------------------------------

    def _execution_exists(self, execution_id: str) -> bool:
        s = self.session_factory()
        try:
            from app.models.automation_execution import AutomationExecution
            return (s.query(AutomationExecution.id)
                    .filter(AutomationExecution.id == execution_id)
                    .one_or_none()) is not None
        except Exception:  # noqa: BLE001
            return False
        finally:
            s.close()

    def _loop(self) -> None:
        terminal_seen = False
        while not self._stop.is_set():
            try:
                line = self.event_stream.readline()
            except (OSError, ValueError):
                line = b""
            if not line:
                break  # EOF — child crash (matrix #1)
            frame = decode_frame(line)
            if frame is None:
                self.partial_frames += 1  # matrix #2: discard partial frame
                continue
            if not self._execution_exists(frame.get("execution_id", "")):
                self.protocol_violation = True
                continue
            attempt = frame.get("attempt")
            key = (frame.get("execution_id"), attempt)
            seq = frame.get("seq")
            if frame.get("critical"):
                self._flush_telemetry()
                last = self.last_critical_seq.get(key, -1)
                if isinstance(seq, int) and seq > last + 1:
                    # critical jumped — telemetry in between is permitted
                    # (bounded, counted); a critical never arrives with a
                    # LOWER seq than one already committed.
                    self.telemetry_gaps += 1
                if isinstance(seq, int):
                    self.last_critical_seq[key] = max(last, seq)
                status = self._persist(frame)
                if status == "new":
                    self.critical_count += 1
                elif status == "duplicate_mismatch":
                    # Same (execution_id, attempt, seq) reused for a
                    # DIFFERENT event — matrix #6 absorb still applies (no
                    # second row), but the pipe contract is broken.
                    self.protocol_violation = True
                committed = self.last_committed_upto.get(key, -1)
                if isinstance(seq, int) and seq > committed:
                    self.last_committed_upto[key] = seq
                    committed = seq
                self._ack(frame.get("execution_id"), attempt,
                          seq if isinstance(seq, int) else committed,
                          committed)
                if frame.get("type") in TERMINAL_EVENT_TYPES:
                    terminal_seen = True
                    self.terminal_event = frame
            else:
                self.telemetry_count += 1
                try:
                    self._telemetry_q.put_nowait(frame)
                except queue.Full:  # matrix #9: bounded-loss, counted
                    self.dropped_telemetry += 1
        self._drain_telemetry()
        self.outcome = "completed" if terminal_seen else "crashed"

    def _persist(self, frame: dict) -> str:
        """Parent-owned persistence. Returns 'new' | 'duplicate' |
        'duplicate_mismatch' | 'error'.

        Duplicate replay of the SAME event_id is absorbed (still ACKed)
        via the (execution_id, attempt, seq) unique index; the SAME seq
        for a DIFFERENT event_id is a protocol violation (flagged by the
        caller) — never two rows."""
        s = self.session_factory()
        try:
            existing = (s.query(AutomationExecutionEvent)
                        .filter(
                            AutomationExecutionEvent.execution_id
                            == frame["execution_id"],
                            AutomationExecutionEvent.attempt
                            == frame["attempt"],
                            AutomationExecutionEvent.seq == frame["seq"])
                        .one_or_none())
            if existing is not None:
                stored_id = (existing.payload or {}).get("_event_id")
                return ("duplicate" if stored_id == frame.get("event_id")
                        else "duplicate_mismatch")
            payload = dict(frame.get("payload") or {})
            payload["_event_id"] = frame.get("event_id")  # replay identity
            s.add(AutomationExecutionEvent(
                execution_id=frame["execution_id"],
                attempt=frame["attempt"],
                seq=frame["seq"],
                event_type=frame.get("type") or "unknown",
                payload=payload,
            ))
            s.commit()
            return "new"
        except Exception as e:  # noqa: BLE001 — broker must never die mid-pipe
            logger.warning("event broker persist failed: %s", e)
            try:
                s.rollback()
            except Exception:  # noqa: BLE001
                pass
            return "error"
        finally:
            s.close()

    def _ack(self, execution_id, attempt, ack_seq, committed_upto) -> None:
        """ACK AFTER commit (the caller persists before invoking this)."""
        try:
            self.ack_stream.write(encode_frame({
                "ack": True, "execution_id": execution_id,
                "attempt": attempt, "ack_seq": ack_seq,
                "committed_upto": committed_upto,
            }))
            self.ack_stream.flush()
        except (OSError, ValueError):
            pass  # child gone — EOF handling covers it

    def _flush_telemetry(self) -> None:
        self._drain_telemetry(final=False)

    def _drain_telemetry(self, final: bool = False) -> None:
        while True:
            try:
                frame = self._telemetry_q.get_nowait()
            except queue.Empty:
                return
            self._persist(frame)


# ---------------------------------------------------------------------------
# Stage-2 launcher: parent owns persistence/leases/fencing/cancellation/
# termination/finalization; the child only runs the harness (§6.5, §11.4)
# ---------------------------------------------------------------------------

def launch_subprocess_runner(job: dict, *, wait_timeout: float = 900.0,
                             ) -> dict:
    """Spawn the runner child (stage 2) and broker its events (parent side).

    The job spec is passed via the ``AUTOMATION_CHILD_JOB`` env var (stdin is
    reserved for ACKs). The broker persists every accepted event and ACKs
    criticals after commit; when the child EOFs, ``outcome`` maps to the
    parent's next action — ``completed`` (a terminal critical was seen) or
    ``crashed`` ⇒ failed(transient) + retry per policy (§6.5 "Child crash").

    Returns the broker summary: {outcome, critical_count, telemetry_count,
    telemetry_gaps, dropped_telemetry, partial_frames, protocol_violation,
    returncode}. The CALLER (dispatcher) owns fencing, cancellation,
    hard-termination (``proc.terminate()`` on deadline) and finalization —
    this function never writes execution state.
    """
    import subprocess as _subprocess
    import sys as _sys

    env = dict(os.environ)
    env["AUTOMATION_CHILD_JOB"] = json.dumps(job, default=str)

    ev_r, ev_w = os.pipe()
    ack_r, ack_w = os.pipe()
    proc = _subprocess.Popen(
        [_sys.executable, "-m", "app.services.automation.subprocess_child"],
        stdin=ack_r, stdout=ev_w, stderr=_subprocess.DEVNULL,
        env=env, cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))),
    )
    # The child holds its own dup'd fds; close the parent's ack-read copy
    # (unused — ACKs flow parent→child on ack_w) to avoid fd leaks.
    try:
        os.close(ack_r)
    except OSError:
        pass
    from app.database import SessionLocal
    broker = ParentEventBroker(
        event_stream=os.fdopen(ev_r, "rb", buffering=0),
        ack_stream=os.fdopen(ack_w, "wb", buffering=0),
        session_factory=SessionLocal,
    )
    broker.start()
    try:
        proc.wait(timeout=wait_timeout)
    except _subprocess.TimeoutExpired:
        # Wall-clock deadline (§6.5 hard termination): kill the child. Any
        # in-flight external request is UNKNOWN — Gate B reconciliation
        # (§6.6/F-27) owns the consequences, never this function.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except _subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        broker.outcome = "terminated"
    finally:
        try:
            os.close(ev_w)
        except OSError:
            pass
        broker.run_to_eof(timeout=60)
        broker.stop(timeout=10)
    return {
        "outcome": broker.outcome,
        "returncode": proc.returncode,
        "critical_count": broker.critical_count,
        "telemetry_count": broker.telemetry_count,
        "telemetry_gaps": broker.telemetry_gaps,
        "dropped_telemetry": broker.dropped_telemetry,
        "partial_frames": broker.partial_frames,
        "protocol_violation": broker.protocol_violation,
        "terminal_event": broker.terminal_event,
    }
