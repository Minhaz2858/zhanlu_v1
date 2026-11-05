"""Runner protocol — fenced execution claim + finalization mechanics (R6, TS §6.2/§6.2b).

Normative state machine (only the runner writes these transitions):

    claimed/queued → running            (fenced CAS + lease in same tx)
    running        → finalizing         (output persisted BEFORE this CAS)
    finalizing     → succeeded|failed|timed_out|cancelled   (fenced CAS)

Finalization is IDEMPOTENT and re-runnable (§6.2b): recovery replays it on
the same execution whenever ``output_text`` is present; duplicate
finalization is structurally impossible (exactly one fenced CAS winner).

This module is the mechanism R6's executor rewrite consumes; the executor's
7 audited plain-CAS write sites migrate onto these functions behind the
``AUTOMATION_RUNNER_PROTOCOL`` flag in the next work chunk (the executor is
foreign-dirty with uncommitted owner work — flagged, not bypassed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.automation_scheduling import ExecutionLease
from app.services.automation.scheduler_core import (
    acquire_lease,
    new_fencing_token,
    renew_lease,
    release_lease,
)

logger = logging.getLogger("zhanlu.automation.runner_protocol")

NON_TERMINAL_STATUSES = ("queued", "claimed", "running", "finalizing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def claim_running(db: Session, execution_id: str, owner: str, execution_cls,
                  ttl_seconds: int = 90) -> Optional[str]:
    """Fenced CAS queued/claimed → running + lease acquire (§6.2 step 2).

    Returns the fencing token, or None when another worker won (lease held,
    row already running, or the CAS lost the race).
    """
    token = acquire_lease(db, execution_id, owner, ttl_seconds=ttl_seconds)
    if token is None:
        return None  # live lease held elsewhere — never double-claim
    rc = db.execute(
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status.in_(("queued", "claimed")),
        )
        .values(status="running", started_at=_utcnow(), lease_owner=owner)
    )
    db.commit()
    if rc.rowcount != 1:
        release_lease(db, execution_id, token)
        return None
    return token


def heartbeat(db: Session, execution_id: str, token: str,
              ttl_seconds: int = 90) -> bool:
    """Lease renewal — continues through finalization (§6.2b ownership)."""
    return renew_lease(db, execution_id, token, ttl_seconds=ttl_seconds)


def current_token(db: Session, execution_id: str, owner: str) -> Optional[str]:
    """Resolve the caller's live fencing token: the lease row must exist,
    be unexpired, and be owned by ``owner``.

    The in-process executor IS the lease owner (the dispatcher acquired the
    lease under the same worker id before spawning it), so worker-owned
    transitions resolve their token from the lease table — no signature
    threading through the call chain is required.
    """
    lease = db.get(ExecutionLease, execution_id)
    if lease is None:
        return None
    if lease.owner != owner:
        return None
    if lease.lease_expires_at <= _utcnow():
        return None
    return lease.fencing_token


def finalize_from_running(
    db: Session, execution_id: str, token: str, execution_cls, *,
    terminal_status: str,
    duration_seconds: Optional[float] = None,
    error: Optional[str] = None,
) -> bool:
    """§6.2b worker-owned finalization from ``running``: the output must
    ALREADY be persisted on the row (the executor writes it before calling
    this); two fenced steps — running→finalizing, then finalizing→terminal.
    A stale token (recovery took over) aborts with False and leaves the
    row for the recovery plane."""
    lease = db.get(ExecutionLease, execution_id)
    if lease is None or lease.fencing_token != token:
        return False
    rc = db.execute(
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status == "running",
        )
        .values(status="finalizing")
    )
    db.commit()
    if rc.rowcount != 1:
        # Not running anymore (recovery/cancellation won the race) — bail.
        return False
    ok = finalize_terminal(
        db, execution_id, token, execution_cls,
        terminal_status=terminal_status,
        duration_seconds=duration_seconds,
        error=error,
    )
    return ok


def persist_output_and_enter_finalizing(
    db: Session, execution_id: str, token: str, execution_cls, *,
    output_text: str, output_data: Optional[dict] = None,
) -> bool:
    """§6.2b ordering guarantee: PERSIST output first, THEN the fenced
    running→finalizing CAS. A crash after the persist but before the CAS
    leaves ``running`` + output — recovery treats that as finalize-replay.

    Provenance ids (``agent_run_id`` etc.) are carried in the event payload
    until the executor rewrite's migration adds the execution columns.

    Returns False when the caller lost the lease (a recovering worker owns
    the execution now) — the caller must abort silently.
    """
    # Lease check WITHOUT renewal: only the live token holder proceeds.
    lease = db.get(ExecutionLease, execution_id)
    if lease is None or lease.fencing_token != token:
        return False
    values = {"output_text": output_text, "output_data": output_data or {}}
    db.execute(
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status.in_(("running", "finalizing")),
        )
        .values(**values)
    )
    # fenced CAS running→finalizing (token check).
    lease2 = db.get(ExecutionLease, execution_id)
    if lease2 is None or lease2.fencing_token != token:
        db.rollback()
        return False
    rc = db.execute(
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status == "running",
        )
        .values(status="finalizing")
    )
    db.commit()
    if rc.rowcount != 1:
        # Already finalizing (crash-race duplicate) — finalization is
        # idempotent; treat as success for the caller's purposes.
        row = db.get(execution_cls, execution_id)
        return row is not None and row.status == "finalizing"
    return True


def finalize_terminal(
    db: Session, execution_id: str, token: str, execution_cls, *,
    terminal_status: str,
    duration_seconds: Optional[float] = None,
    error: Optional[str] = None,
) -> bool:
    """Fenced CAS finalizing → terminal (§6.2b step 4). IDEMPOTENT:
    re-running after a lost race or a crash returns False without
    corrupting state; exactly one fenced write ever lands."""
    if terminal_status not in ("succeeded", "completed", "failed", "timed_out",
                               "cancelled"):
        # "completed" is the legacy vocabulary — the R8 cutover renames it
        # to "succeeded"; both are accepted here for parity.
        raise ValueError(f"not a terminal execution status: {terminal_status!r}")
    lease = db.get(ExecutionLease, execution_id)
    if lease is None or lease.fencing_token != token:
        return False  # stale runner — rejected by fencing
    values = {"status": terminal_status, "completed_at": _utcnow()}
    if duration_seconds is not None:
        values["duration_seconds"] = duration_seconds
    if error is not None:
        values["error"] = error[:500]
    rc = db.execute(
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status == "finalizing",
        )
        .values(**values)
    )
    db.commit()
    if rc.rowcount == 1:
        release_lease(db, execution_id, token)
        return True
    # Terminal already set by the fence winner — verify immutability.
    row = db.get(execution_cls, execution_id)
    return row is not None and row.status == terminal_status


def finalize_replay(db: Session, execution, execution_cls) -> bool:
    """§6.2b recovery path: re-run finalization steps 1–5 on an execution
    whose output is already persisted (idempotent — never re-invokes the
    agent). Used by ``recover_expired_leases`` (output present branch).

    The terminal status is derived from the row (error present ⇒ failed,
    else succeeded); the fenced write uses a recovery-issued token so a
    genuinely dead runner cannot race it."""
    row = db.get(execution_cls, execution.id) if not getattr(execution, "_sa_instance_state", None) else execution
    if row is None:
        return False
    if not getattr(row, "output_text", None):
        return False  # protocol violation guard — caller handles the retry path
    terminal = "failed" if getattr(row, "error", None) else "succeeded"
    # Take over the (expired) lease so the terminal write is fenced.
    token = acquire_lease(db, row.id, "recovery-finalize")
    if token is None:
        # A live lease exists — the original runner is alive after all.
        return False
    ok = finalize_terminal(
        db, row.id, token, execution_cls, terminal_status=terminal,
        error=getattr(row, "error", None),
    )
    if not ok:
        release_lease(db, row.id, token)
    return ok
