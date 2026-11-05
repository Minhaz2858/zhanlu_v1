"""R6 Phase 3 — durable execution-event recorder (TS §1.4, F-26).

Append-only observability ledger over ``AutomationExecutionEvent``
(migration 128; attempt-scoped uniqueness proven in Stage-2 S2-6):

* **Attempt-scoped seq** — ``MAX(seq)+1`` per ``(execution_id, attempt)``;
  different attempts restart at 0 and cross-attempt reuse is legal (F-26).
  Computing seq from MAX (not an in-memory counter) makes crash recovery
  resume the sequence without replaying a used seq.
* **Duplicate absorb** — a racing recovery writer that claims the same seq
  hits the UNIQUE constraint; the recorder absorbs it as a COUNTED drop
  (returns False) and never corrupts state. Telemetry is bounded-loss by
  design (§ Option C) — a lost progress frame never fails a run.
* **Critical events ACK-after-commit** — ``critical=True`` (run_started /
  run_finished / run_failed / run_cancelled) commits BEFORE returning, so
  the row is durable at the ACK boundary; a commit failure PROPAGATES
  (fail-closed) — a critical lifecycle event is never silently dropped.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.automation_scheduling import AutomationExecutionEvent

logger = logging.getLogger(__name__)

CRITICAL_EVENT_TYPES = ("run_started", "run_finished", "run_failed",
                        "run_cancelled", "approval_requested")


def _session_factory():
    """Dedicated short-lived session for the convenience wrapper (mirrors
    the executor's own-session discipline — never the agent stream's)."""
    from app.database import SessionLocal
    return SessionLocal()


def next_seq(db: Session, execution_id: str, attempt: int) -> int:
    """MAX(seq)+1 per (execution_id, attempt); 0 when the attempt is new."""
    last = (db.query(AutomationExecutionEvent.seq)
            .filter(AutomationExecutionEvent.execution_id == execution_id,
                    AutomationExecutionEvent.attempt == attempt)
            .order_by(AutomationExecutionEvent.seq.desc())
            .first())
    return (last[0] + 1) if last else 0


def record_execution_event(db: Session, execution_id: str, attempt: int,
                           event_type: str, payload: Optional[dict] = None,
                           *, critical: bool = False) -> bool:
    """Append one ledger row. Returns True on durable success, False on an
    absorbed duplicate (counted drop). ``critical=True`` ACKs after commit
    and propagates commit failures (fail-closed)."""
    try:
        db.add(AutomationExecutionEvent(
            execution_id=execution_id,
            attempt=attempt,
            seq=next_seq(db, execution_id, attempt),
            event_type=event_type,
            payload=payload or {},
        ))
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        db.rollback()
        if critical:
            # Fail-closed: a lifecycle event that cannot be made durable is
            # an error the caller must see — never a silent drop.
            raise
        logger.debug("execution_event(%s) dropped (bounded-loss): %s",
                     event_type, e)
        return False


def record_telemetry_event(execution_id: str, event_type: str,
                           payload: Optional[dict] = None,
                           attempt: Optional[int] = None) -> bool:
    """Bounded-loss convenience wrapper: opens its OWN short-lived session,
    resolves the attempt from the execution row when not supplied, and never
    raises. Returns False on any failure (counted drop)."""
    s = None
    try:
        s = _session_factory()
        if attempt is None:
            from app.models.automation_execution import AutomationExecution
            row = s.query(AutomationExecution.attempt).filter(
                AutomationExecution.id == execution_id).one_or_none()
            if row is None:
                return False
            attempt = row.attempt
        return record_execution_event(s, execution_id, attempt, event_type,
                                      payload)
    except Exception as e:  # noqa: BLE001 — telemetry must never break the run
        logger.debug("telemetry event %s dropped: %s", event_type, e)
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Critical lifecycle emitters (ACK-after-commit, called from the executor's
# fenced transition sites right after a successful state write)
# ---------------------------------------------------------------------------

def emit_run_started(db: Session, execution_id: str, attempt: int,
                     **payload) -> bool:
    return record_execution_event(db, execution_id, attempt, "run_started",
                                  payload, critical=True)


def emit_run_finished(db: Session, execution_id: str, attempt: int, *,
                      status: str, **payload) -> bool:
    return record_execution_event(db, execution_id, attempt, "run_finished",
                                  {"status": status, **payload},
                                  critical=True)


def emit_run_failed(db: Session, execution_id: str, attempt: int, *,
                    classification: str, error: Optional[str] = None,
                    **payload) -> bool:
    return record_execution_event(
        db, execution_id, attempt, "run_failed",
        {"classification": classification, "error": error, **payload},
        critical=True)


def emit_run_cancelled(db: Session, execution_id: str, attempt: int,
                       **payload) -> bool:
    return record_execution_event(db, execution_id, attempt, "run_cancelled",
                                  payload, critical=True)
