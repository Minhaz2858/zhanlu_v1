"""Scheduler core — shared claim/lease/fencing/recovery mechanics (R5).

Normative: TS §1.3 (identity model), §1.5 (leases), §6.1 (claim/lease),
§6.2b (fencing + finalize-replay), §6.3 (non-destructive recovery),
§11.10 (shared mechanics for automation AND workflow occurrences —
parameterized by table adapter, never a subject_type overload).

Every function takes an explicit session and works on BOTH PostgreSQL
(production guarantees: row locks, unique arbiters) and SQLite (unit tests).
The concurrency tests that prove the PostgreSQL guarantees live in
``tests/test_automation_r5_pg_integration.py`` (skipped without a PG DSN).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.automation_scheduling import (
    AutomationOccurrence,
    AutomationSideEffectOp,
    ExecutionLease,
)

logger = logging.getLogger("zhanlu.automation.scheduler_core")

OCCURRENCE_ACTIVE_STATUSES = ("pending", "claimed")
OCCURRENCE_TERMINAL_STATUSES = ("completed", "failed_terminal", "cancelled", "skipped")

DEFAULT_LEASE_TTL_SECONDS = 90  # heartbeat every 30s; 3 missed beats ⇒ expiry
RECONCILIATION_WINDOW_SECONDS = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_fencing_token() -> str:
    """ULID-ish monotonic token: sortable timestamp + randomness."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Occurrence claim (§1.3/§6.1) — the duplicate-claim arbiter
# ---------------------------------------------------------------------------


def claim_occurrence(
    db: Session,
    *,
    automation_task_id: str,
    occurrence_key: str,
    scheduled_at: Optional[datetime],
    org_id: str = "default-org",
    app_id: str = "default-app",
    created_by_id: Optional[str] = None,
    misfire_collapsed_count: int = 0,
    occurrence_cls=AutomationOccurrence,
    subject_fields: Optional[dict] = None,
) -> Optional[object]:
    """Create-and-claim one occurrence atomically.

    The UNIQUE (subject, occurrence_key) index is the arbiter: on a key
    collision (double leader, crash replay, double Run-Now) the existing
    row is re-read — if it is already claimed/terminal, returns None
    (someone else owns it); only a *pending* pre-existing row (e.g. left
    by a crash between INSERT and claim) is claimed here via CAS.

    Returns the claimed occurrence, or None when the slot is taken.
    """
    fields = dict(subject_fields or {})
    occ = occurrence_cls(
        occurrence_key=occurrence_key,
        scheduled_at=scheduled_at,
        status="pending",
        misfire_collapsed_count=max(0, misfire_collapsed_count),
        org_id=org_id,
        app_id=app_id,
        created_by_id=created_by_id,
        **fields,
    )
    if occurrence_cls is AutomationOccurrence:
        occ.automation_task_id = automation_task_id
    db.add(occ)
    try:
        db.flush()
    except IntegrityError:
        # Key already exists — full rollback (pysqlite-safe), then CAS-claim
        # the pending row if one exists.
        db.rollback()
        existing = (
            db.query(occurrence_cls)
            .filter_by(occurrence_key=occurrence_key, **fields)
            .first()
        )
        if existing is None or existing.status not in OCCURRENCE_ACTIVE_STATUSES:
            return None
        rc = db.execute(
            update(occurrence_cls)
            .where(
                occurrence_cls.id == existing.id,
                occurrence_cls.status == "pending",
            )
            .values(status="claimed", claimed_at=_utcnow())
        )
        db.commit()
        if rc.rowcount != 1:
            return None
        db.refresh(existing)
        return existing
    # Fresh insert: claim it in the same transaction.
    rc = db.execute(
        update(occurrence_cls)
        .where(occurrence_cls.id == occ.id, occurrence_cls.status == "pending")
        .values(status="claimed", claimed_at=_utcnow())
    )
    db.commit()
    if rc.rowcount != 1:
        return None
    db.refresh(occ)
    return occ


def complete_occurrence(db: Session, occurrence, status: str) -> None:
    """Terminalize an occurrence (claimed → terminal, guarded)."""
    if status not in OCCURRENCE_TERMINAL_STATUSES:
        raise ValueError(f"not a terminal occurrence status: {status!r}")
    db.execute(
        update(type(occurrence))
        .where(
            type(occurrence).id == occurrence.id,
            type(occurrence).status == "claimed",
        )
        .values(status=status, completed_at=_utcnow())
    )
    db.commit()


def recover_stuck_claims(
    db: Session,
    *,
    occurrence_cls=AutomationOccurrence,
    execution_cls,
    stuck_after_seconds: int = 300,
    now: Optional[datetime] = None,
    limit: int = 50,
) -> int:
    """Leader-crash recovery: occurrences stuck in ``claimed`` whose
    execution never materialized (crash between claim and execution INSERT)
    or whose every attempt terminalized without completing the occurrence
    become ``pending`` again — re-claimable by any process's next tick.

    A claimed occurrence with a LIVE (non-terminal) execution is left
    alone — that run is in flight. Guarded by claimed_at age, never by
    wall-clock guessing about live workers.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=stuck_after_seconds)
    # Claimed occurrences with NO non-terminal execution.
    rows = (
        db.query(occurrence_cls)
        .filter(
            occurrence_cls.status == "claimed",
            occurrence_cls.claimed_at <= cutoff,
            ~db.query(execution_cls.id).filter(
                execution_cls.occurrence_id == occurrence_cls.id,
                execution_cls.status.in_(("queued", "claimed", "running", "finalizing")),
            ).exists(),
        )
        .limit(limit)
        .all()
    )
    for occ in rows:
        db.execute(
            update(occurrence_cls)
            .where(
                occurrence_cls.id == occ.id,
                occurrence_cls.status == "claimed",
            )
            .values(status="pending", claimed_at=None)
        )
    db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Leases (§1.5) — govern RUNNING executions only
# ---------------------------------------------------------------------------


def acquire_lease(
    db: Session, execution_id: str, owner: str, ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
) -> Optional[str]:
    """Insert the lease row (PK = execution_id). Returns the fencing token,
    or None if a live lease already exists."""
    now = _utcnow()
    lease = ExecutionLease(
        execution_id=execution_id,
        owner=owner,
        lease_expires_at=now + timedelta(seconds=ttl_seconds),
        fencing_token=new_fencing_token(),
        acquired_at=now,
    )
    db.add(lease)
    try:
        db.commit()
        return lease.fencing_token
    except IntegrityError:
        db.rollback()
        existing = db.get(ExecutionLease, execution_id)
        if existing is not None and existing.lease_expires_at > now:
            return None  # live lease held elsewhere
        # Expired lease → take over with a fresh token.
        token = new_fencing_token()
        db.execute(
            update(ExecutionLease)
            .where(
                ExecutionLease.execution_id == execution_id,
                ExecutionLease.lease_expires_at <= now,
            )
            .values(
                owner=owner,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                fencing_token=token,
                acquired_at=now,
            )
        )
        db.commit()
        return token


def renew_lease(
    db: Session, execution_id: str, fencing_token: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> bool:
    """Heartbeat: extend expiry ONLY if the caller still holds the lease."""
    rc = db.execute(
        update(ExecutionLease)
        .where(
            ExecutionLease.execution_id == execution_id,
            ExecutionLease.fencing_token == fencing_token,
        )
        .values(lease_expires_at=_utcnow() + timedelta(seconds=ttl_seconds))
    )
    db.commit()
    return rc.rowcount == 1


def release_lease(db: Session, execution_id: str, fencing_token: str) -> bool:
    rc = db.execute(
        update(ExecutionLease)
        .where(
            ExecutionLease.execution_id == execution_id,
            ExecutionLease.fencing_token == fencing_token,
        )
        .values(lease_expires_at=_utcnow() - timedelta(seconds=1))
    )
    db.commit()
    return rc.rowcount == 1


def fenced_status_write(
    db: Session,
    execution_id: str,
    fencing_token: str,
    *,
    from_statuses: tuple,
    to_status: str,
    extra_values: Optional[dict] = None,
    execution_cls,
) -> bool:
    """Fenced CAS on the execution row (§6.2b): the write lands only when
    the row is in an expected source state AND the caller's lease token
    matches the live lease. Orphaned workers lose this check by construction."""
    lease = db.get(ExecutionLease, execution_id)
    if lease is None or lease.fencing_token != fencing_token:
        return False
    values = {"status": to_status, **(extra_values or {})}
    q = (
        update(execution_cls)
        .where(
            execution_cls.id == execution_id,
            execution_cls.status.in_(from_statuses),
        )
        .values(**values)
    )
    rc = db.execute(q)
    db.commit()
    return rc.rowcount == 1


# ---------------------------------------------------------------------------
# §6.3 recovery — non-destructive lease expiry handling + F-27 barrier
# ---------------------------------------------------------------------------


def reconcile_side_effects(
    db: Session,
    execution_id: str,
    attempt: int,
    *,
    provider_check: Optional[Callable[[dict], Optional[str]]] = None,
    now: Optional[datetime] = None,
    reconciliation_window_seconds: int = RECONCILIATION_WINDOW_SECONDS,
) -> dict:
    """F-27 pre-retry reconciliation barrier (TS §6.6 rule 6).

    Resolves every ``automation_side_effect_ops`` row of the attempt:
    (a) provider_check (injected — queries the external system), then
    (b) ledger_replay (outcome rows already committed), then (c) age-out to
    ``unknown``. NEVER invents a successful reconciliation.

    Returns {"blocked": bool, "unresolved_non_idempotent": [op_key...],
             "outcomes": {op_key: status}}.
    """
    now = now or _utcnow()
    rows = (
        db.query(AutomationSideEffectOp)
        .filter(
            AutomationSideEffectOp.execution_id == execution_id,
            AutomationSideEffectOp.attempt == attempt,
            AutomationSideEffectOp.status.in_(
                ("dispatched", "started", "unknown")
            ),
        )
        .all()
    )
    outcomes: dict = {}
    unresolved_non_idempotent: list = []
    for row in rows:
        status = row.status
        via = row.reconciled_via
        # (b) ledger_replay: a committed outcome row (confirmed_complete /
        # failed / confirmed_not_started) is authoritative evidence.
        # (handled by the status filter above — only unresolved states here.)
        # (a) provider_check — the external system is asked, never assumed.
        if provider_check is not None and status in ("dispatched", "started"):
            try:
                verdict = provider_check({
                    "op_key": row.op_key,
                    "tool_name": row.tool_name,
                    "idempotency_key": row.idempotency_key,
                    "status": row.status,
                })
            except Exception as e:  # noqa: BLE001 — provider outage ≠ reconciliation
                logger.warning("provider_check failed for %s: %s", row.op_key, e)
                verdict = None
            if verdict in ("confirmed_complete", "confirmed_not_started", "unknown"):
                status, via = verdict, "provider_check"
        # (c) age-out: no provider check available/decisive and the row is
        # older than the window → unknown (timeout_ageout).
        if status in ("dispatched", "started"):
            updated = getattr(row, "updated_date", None) or row.created_date
            if updated is not None:
                age = (now - (updated if updated.tzinfo is None else updated.replace(tzinfo=None))).total_seconds()
                if age > reconciliation_window_seconds:
                    status, via = "unknown", "timeout_ageout"
        if status in ("dispatched", "started"):
            # Still unresolved — treat per Gate B metadata for the decision.
            outcomes[row.op_key] = "unresolved"
            if not row.idempotent:
                unresolved_non_idempotent.append(row.op_key)
            continue
        row.status = status
        row.reconciled_via = via
        row.reconciled_at = now
        outcomes[row.op_key] = status
        # F-27: an UNKNOWN outcome on a non-idempotent op blocks the
        # automatic retry — no matter HOW we arrived at unknown (age-out
        # or a provider that couldn't establish the outcome).
        if status == "unknown" and not row.idempotent:
            unresolved_non_idempotent.append(row.op_key)
    db.commit()
    return {
        "blocked": bool(unresolved_non_idempotent),
        "unresolved_non_idempotent": unresolved_non_idempotent,
        "outcomes": outcomes,
    }


def recover_expired_leases(
    db: Session,
    *,
    execution_cls,
    owner: str,
    finalize_replay: Optional[Callable[[Session, object], bool]] = None,
    new_attempt: Optional[Callable[[Session, object], object]] = None,
    now: Optional[datetime] = None,
    limit: int = 20,
) -> dict:
    """§6.3 non-destructive recovery (replaces ``reap_on_startup``).

    For each execution whose lease expired while non-terminal:
    - output persisted → **finalize-replay** (the SAME execution; steps
      idempotent) via ``finalize_replay`` when provided;
    - no output → **F-27 barrier first**: reconcile the attempt's
      side-effect ledger. Blocked (unknown non-idempotent) ⇒ mark the
      execution failed with ``blocked_on_side_effect_reconciliation`` —
      automatic retry is forbidden; otherwise a new attempt opens via
      ``new_attempt`` (caller creates the retry row, honoring
      UNIQUE(occurrence_id, attempt)).

    Unexpired leases owned elsewhere are NEVER touched (fixes audit B4).
    """
    now = now or _utcnow()
    stats = {"finalized": 0, "retried": 0, "blocked": 0, "skipped_live": 0}
    expired = (
        db.query(execution_cls, ExecutionLease)
        .join(ExecutionLease, ExecutionLease.execution_id == execution_cls.id)
        .filter(
            ExecutionLease.lease_expires_at <= now,
            execution_cls.status.in_(("queued", "claimed", "running", "finalizing")),
        )
        .limit(limit)
        .all()
    )
    for execution, lease in expired:
        # Finalize-replay branch (§6.2b): output persisted before finalizing.
        if getattr(execution, "output_text", None):
            if finalize_replay is not None and finalize_replay(db, execution):
                stats["finalized"] += 1
                continue
        # No output ⇒ barrier before any new attempt (output_text NULL is
        # never evidence that no side effect occurred).
        attempt = getattr(execution, "attempt", 0) or 0
        barrier = reconcile_side_effects(db, execution.id, attempt)
        if barrier["blocked"]:
            if fenced_status_write(
                db, execution.id, lease.fencing_token,
                from_statuses=("queued", "claimed", "running", "finalizing"),
                to_status="failed",
                extra_values={
                    "error": "blocked_on_side_effect_reconciliation: "
                             + ",".join(barrier["unresolved_non_idempotent"])[:280],
                    "completed_at": now,
                },
                execution_cls=execution_cls,
            ):
                stats["blocked"] += 1
            continue
        if new_attempt is not None:
            new_attempt(db, execution)
            stats["retried"] += 1
        else:
            # No retry callback: at minimum release the execution from its
            # zombie state as a transient failure.
            fenced_status_write(
                db, execution.id, lease.fencing_token,
                from_statuses=("queued", "claimed", "running", "finalizing"),
                to_status="failed",
                extra_values={"error": "lease_expired_transient", "completed_at": now},
                execution_cls=execution_cls,
            )
            stats["retried"] += 1
    return stats
