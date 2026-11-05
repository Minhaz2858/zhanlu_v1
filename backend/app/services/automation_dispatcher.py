"""Automation dispatcher — background scheduler that fires due automations.

This is the Manus-style execution engine: a single asyncio background task
that wakes up every ``TICK_INTERVAL`` seconds, finds ``AutomationTask`` rows
whose ``next_run_at`` has passed, creates an ``AutomationExecution``, and
spawns an executor task for each.

Phase 1 reliability additions (vs. Manush AI gap analysis):
  * **CAS-based claiming** — each fire atomically advances ``next_run_at``
    via ``UPDATE ... WHERE next_run_at = <orig>``. Two dispatcher workers
    can SELECT the same due rows but only one wins the CAS, so no task is
    ever fired twice. Portable across SQLite (serialized writes) and
    Postgres (row lock on UPDATE) — no ``FOR UPDATE SKIP LOCKED`` needed.
  * **No LLM in the tick** — schedule parsing uses rules-only
    (``safe_parse_schedule_rules_only``); the LLM fallback is reserved for
    task *creation* time. A bad schedule no longer eats the tick budget.
  * **Retry machinery** — failed executions honor ``AutomationTask.max_retries``;
    each retry is a new ``AutomationExecution`` row with ``attempt + 1``,
    scheduled on the main loop with exponential backoff.
  * **Janitor (zombie reaper)** — a periodic sweep marks any execution still
    ``queued``/``running`` past its ``timeout_at`` as failed (and retries it).
    ``reap_on_startup`` reaps everything left mid-flight by a previous
    process (e.g. a ``docker restart`` deploy), so no run is stuck forever.

The dispatcher is the only path that can transition an automation from
"active and waiting" → "running" → "completed/failed". It is started in
``main.py``'s ``startup`` event and stopped in ``shutdown``.

Failure isolation: each tick is wrapped in a try/except so a single bad task
never breaks the loop. The dispatcher itself is never killed by application
errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, update

from app.models.automation_task import AutomationTask
from app.models.automation_execution import AutomationExecution
from app.services.schedule_parser import next_run_at, safe_parse_schedule_rules_only

logger = logging.getLogger(__name__)

# How often the dispatcher wakes up. 60s is fine for minute-level schedules
# and is gentle on the DB. Lower if you need sub-minute precision.
TICK_INTERVAL = 60  # seconds

# How long a single tick is allowed to take before we skip remaining tasks.
TICK_BUDGET = 45  # seconds


_main_loop: Optional[asyncio.AbstractEventLoop] = None
_running_tasks: list[asyncio.Task] = []
_shutdown_event: Optional[asyncio.Event] = None
_tick_count = 0

# Bounded concurrency (Phase 5): caps how many automation executions may run
# at once. Created lazily in start_dispatcher (needs the running loop) and
# acquired by _run_executor around every spawned run — both scheduled fires
# and manual trigger_now() calls — so a burst of due tasks can't OOM the box
# or rate-limit the LLM provider. None before start / after stop.
_concurrency_sem: Optional[asyncio.Semaphore] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _worker_id() -> str:
    """Stable per-process id stamped on execution.lease_owner for diagnostics."""
    host = os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "worker"
    return f"{host}:{os.getpid()}"


def parse_max_retries(task: AutomationTask) -> int:
    """Parse ``task.max_retries`` (stored as a string column) → int.

    Returns 0 on any parse failure (safer than retrying forever on a
    misconfigured value). ``max_retries`` is the number of *retries* after
    the initial attempt, so total attempts = 1 + max_retries.
    """
    raw = getattr(task, "max_retries", None)
    try:
        if raw is None:
            return 0
        s = str(raw).strip()
        if not s:
            return 0
        return max(0, int(s))
    except (ValueError, TypeError):
        return 0


def _backoff(task: AutomationTask) -> timedelta:
    """When parsing fails, push next_run_at out so the dispatcher doesn't
    re-attempt every tick.
    """
    return timedelta(hours=1)


# ---------------------------------------------------------------------------
# Schema ensure (portable: SQLite + Postgres)
# ---------------------------------------------------------------------------

def _to_ddl(col, is_pg: bool) -> Optional[str]:
    """Map a SQLAlchemy Column to a portable DDL string.

    Returns ``None`` for column types we don't know how to auto-add — the
    caller logs a warning and skips, leaving the human to write an alembic
    migration (foreign keys, indexes, custom types, etc.).
    """
    type_name = col.type.__class__.__name__
    if type_name == "String":
        return f"VARCHAR({getattr(col.type, 'length', None) or 64})"
    if type_name == "Text":
        return "TEXT"
    if type_name == "DateTime":
        return "TIMESTAMP" if is_pg else "DATETIME"
    if type_name == "Boolean":
        return "BOOLEAN" if is_pg else "INTEGER"
    if type_name == "JSON":
        return "JSON"
    if type_name in ("Integer", "BigInteger", "SmallInteger"):
        return "INTEGER"
    if type_name == "Float":
        return "FLOAT" if is_pg else "REAL"
    return None


def _ensure_schema() -> None:
    """Add missing columns at runtime for ``automation_executions`` and
    ``automation_tasks``.

    ``Base.metadata.create_all()`` only creates *new* tables — it doesn't
    add columns to existing ones. This detects missing columns via the
    SQLAlchemy inspector and issues ``ALTER TABLE ADD COLUMN`` for each,
    working on both SQLite and Postgres so existing deployments pick up
    new model columns without a manual migration step.

    - ``automation_executions``: explicit additions list (narrow, project
      pattern; covers phase-2 observability fields).
    - ``automation_tasks``: introspects the SQLAlchemy model so future
      column additions auto-sync at startup. Columns with types we can't
      safely DDL (FKs, exotic types) are logged and skipped — those
      should still get an alembic migration.

    Closes the loophole where a model-only column addition (like the
    Phase-4 ``timezone`` field) caused every AutomationTask list call
    to 500 with ``column does not exist``. Mirrors the runtime-sync
    pattern already used for ``automation_executions``.
    """
    try:
        from sqlalchemy import inspect, text
        from app.database import engine
        from app.config import settings

        insp = inspect(engine)
        is_pg = settings.database_dialect.startswith("postgres")
        table_names = set(insp.get_table_names())

        # --- automation_executions (explicit additions list) ----------------
        if "automation_executions" in table_names:
            existing = {c["name"] for c in insp.get_columns("automation_executions")}
            additions = [
                ("timeout_at", "TIMESTAMP" if is_pg else "DATETIME"),
                ("lease_owner", "VARCHAR(64)"),
                # Phase 2 live-run observability.
                ("activity_steps", "JSON"),
                ("current_phase", "VARCHAR(50)"),
                # Recursion chain for execute_automation nesting cap.
                ("parent_execution_id", "VARCHAR(36)"),
            ]
            with engine.begin() as conn:
                for col_name, ddl in additions:
                    if col_name not in existing:
                        conn.execute(
                            text(f"ALTER TABLE automation_executions ADD COLUMN {col_name} {ddl}")
                        )
                        logger.info(
                            "automation schema: added column automation_executions.%s", col_name,
                        )

        # --- automation_tasks (introspect model columns) ---------------------
        if "automation_tasks" in table_names:
            from app.models.automation_task import AutomationTask as _AutomationTaskModel

            existing = {c["name"] for c in insp.get_columns("automation_tasks")}
            # Explicit additions list for columns with FKs / types the
            # generic introspector below can't safely DDL. The auto-introspect
            # path logs-and-skips FKs (see _to_ddl docstring), so any column
            # whose DDL needs more than a bare type goes here.
            additions_tasks: list[tuple[str, str]] = [
                # data_source_id FK → knowledge_bases.id. Plain VARCHAR(36)
                # at the storage level; the FK relationship is enforced at
                # the ORM layer (knowledge_bases table is the source of
                # truth for the bound data source).
                ("data_source_id", "VARCHAR(36)"),
            ]
            with engine.begin() as conn:
                for col_name, ddl in additions_tasks:
                    if col_name in existing:
                        continue
                    conn.execute(
                        text(f"ALTER TABLE automation_tasks ADD COLUMN {col_name} {ddl}")
                    )
                    logger.info(
                        "automation schema: added column automation_tasks.%s (%s)",
                        col_name, ddl,
                    )
            # Refresh existing set after explicit adds.
            existing = {c["name"] for c in insp.get_columns("automation_tasks")}
            with engine.begin() as conn:
                for col in _AutomationTaskModel.__table__.columns:
                    if col.name in existing:
                        continue
                    # Standard audit/timestamp columns are inherited from
                    # TimestampedBase; we never need to re-add them here.
                    if col.name in {
                        "id", "created_date", "updated_date",
                        "created_by_id", "is_deleted", "org_id", "app_id",
                    }:
                        continue
                    ddl = _to_ddl(col, is_pg)
                    if ddl is None:
                        logger.warning(
                            "automation_tasks column %s: unsupported type %s, "
                            "skipping auto-add — add via alembic migration.",
                            col.name, col.type.__class__.__name__,
                        )
                        continue
                    conn.execute(
                        text(f"ALTER TABLE automation_tasks ADD COLUMN {col.name} {ddl}")
                    )
                    logger.info(
                        "automation schema: added column automation_tasks.%s (%s)",
                        col.name, ddl,
                    )

            # --- status CHECK constraint (defense-in-depth, opt-in) ----------
            # When AUTOMATION_STATUS_CHECK_CONSTRAINT_ENABLED is set, add a
            # DB-level CHECK so a non-canonical status can never be persisted
            # (the LLM create tool now validates too, but a future caller
            # could bypass it). Idempotent: skipped if the named constraint
            # already exists. Postgres only — SQLite cannot ADD CONSTRAINT
            # without a table rebuild. NULL status remains allowed (matches
            # the nullable column definition and the dispatcher self-heal).
            if (
                is_pg
                and settings.AUTOMATION_STATUS_CHECK_CONSTRAINT_ENABLED
            ):
                _ck_name = "ck_automation_tasks_status_valid"
                _existing_cks = {
                    ck["name"]
                    for ck in insp.get_check_constraints("automation_tasks")
                }
                if _ck_name not in _existing_cks:
                    _status_vals = ", ".join(
                        repr(v) for v in AutomationTask.VALID_STATUSES
                    )
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE automation_tasks ADD CONSTRAINT "
                                f"{_ck_name} CHECK (status IN ({_status_vals}) "
                                f"OR status IS NULL)"
                            )
                        )
                    logger.info(
                        "automation schema: added CHECK constraint %s", _ck_name,
                    )

        # --- chat_sessions (unread flag for automation-run notifications) -----
        # No alembic migration: matches the project convention of idempotent
        # raw SQL column sync at startup (see MEMORY.md "apply columns via
        # raw SQL to avoid tripping the unapplied chain"). Uses the same
        # inspector-driven pattern as the tables above so it works on both
        # SQLite and Postgres (SQLite has no ADD COLUMN IF NOT EXISTS).
        if "chat_sessions" in table_names:
            existing = {c["name"] for c in insp.get_columns("chat_sessions")}
            if "unread" not in existing:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE chat_sessions ADD COLUMN unread "
                            + ("BOOLEAN DEFAULT FALSE" if is_pg else "INTEGER DEFAULT 0")
                        )
                    )
                logger.info("automation schema: added column chat_sessions.unread")
    except Exception as e:
        logger.warning("automation schema ensure failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

async def _tick() -> None:
    """One pass: run the janitor, then find due tasks and launch executions."""
    global _tick_count

    # ── Janitor: reap zombie / timed-out executions before firing new work. ──
    _tick_count += 1
    try:
        from app.config import settings
        if _tick_count % max(1, settings.AUTOMATION_REAPER_INTERVAL_TICKS) == 0:
            await _reap_stale_executions()
    except Exception as e:
        logger.warning("automation_dispatcher: reaper error: %s", e)

    # ── Session (shared by the self-heal sweep and the due query below). ──
    from app.database import SessionLocal
    db = SessionLocal()

    # ── Self-heal: rescue tasks with unknown status values. ────────────
    # The due query below filters strictly on status == "active", so any
    # task carrying a non-canonical status (e.g. "running" written by the
    # LLM create tool before validation landed) would be silently skipped
    # on every tick — next_run_at advances forever and the task never
    # fires. Rather than loosening the filter, promote these orphans back
    # to "active" here (matches the always-on reap_on_startup precedent).
    # Scope: scheduled tasks only (next_run_at set); manual-only tasks with
    # a stray status are left alone.
    try:
        healed = db.query(AutomationTask).filter(
            AutomationTask.is_deleted == False,  # noqa: E712
            AutomationTask.next_run_at.isnot(None),
            or_(
                AutomationTask.status.is_(None),
                ~AutomationTask.status.in_(AutomationTask.VALID_STATUSES),
            ),
        ).limit(20).all()
        for t in healed:
            logger.warning(
                "automation_dispatcher: self-heal — task %s (%r) has invalid "
                "status %r, promoting to 'active'",
                t.id, t.name, t.status,
            )
            t.status = "active"
        if healed:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("automation_dispatcher: self-heal sweep failed: %s", e)

    # ── Due tasks. ──
    try:
        now = datetime.now(timezone.utc)
        # R5 persisted retries: promote any retry row whose backoff elapsed
        # (in-memory timers are retired — retries survive process death).
        try:
            _promote_due_retries(db, now)
        except Exception:
            logger.exception("promote_due_retries failed (non-fatal)")
        due = _due_tasks(db, now)

        if not due:
            return

        logger.info("automation_dispatcher: %d due task(s) at %s", len(due), now.isoformat())

        for task in due:
            try:
                await _fire(db, task, now)
            except Exception as e:
                logger.exception("dispatcher: fire failed for %s: %s", task.id, e)
                # Move next_run_at forward so we don't re-attempt next tick.
                try:
                    db.execute(
                        update(AutomationTask)
                        .where(AutomationTask.id == task.id)
                        .values(next_run_at=now + _backoff(task))
                    )
                    db.commit()
                except Exception:
                    db.rollback()
    except Exception as e:
        logger.exception("automation_dispatcher: tick failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _due_tasks(db, now: datetime) -> list[AutomationTask]:
    """Due-task selection (R4): active, not deleted, past next_run_at —
    and NOT schedule-degraded (degraded schedules never fire silently;
    the flag + error are surfaced for the user to fix, audit B12)."""
    from sqlalchemy import or_

    return (
        db.query(AutomationTask)
        .filter(
            AutomationTask.status == "active",
            AutomationTask.is_deleted == False,  # noqa: E712
            AutomationTask.next_run_at.isnot(None),
            AutomationTask.next_run_at <= now,
            or_(
                AutomationTask.schedule_degraded.is_(None),
                AutomationTask.schedule_degraded == False,  # noqa: E712
            ),
        )
        .limit(20)
        .all()
    )


async def _fire(db, task: AutomationTask, now: datetime) -> None:
    """Create the execution row, advance next_run_at, spawn the executor.

    Uses a compare-and-swap on ``next_run_at`` to guarantee exactly-once
    firing even if two dispatcher workers SELECT the same due row.
    """
    orig_next = task.next_run_at

    # R4/R5 trigger fidelity: resolve the effective trigger (legacy rows
    # default), then apply the deterministic §6.1a FULL-EPISODE collapse
    # (F-16) — grace measured against the LATEST missed slot, one decision
    # per downtime episode (not per slot).
    from app.services.automation.trigger_policy import (
        collapse_missed_interval as _collapse,
        resolve_trigger as _resolve_trigger,
        grace_seconds as _grace_seconds,
    )

    trigger = _resolve_trigger(task)
    recurring = trigger == "schedule"
    due_utc = orig_next if (orig_next and orig_next.tzinfo) else (
        orig_next.replace(tzinfo=timezone.utc) if orig_next else now
    )

    # Compute the cron WITHOUT calling the LLM. The LLM-based parse is
    # reserved for task creation (API path); the tick only ever uses the
    # already-persisted cron_expression or a cheap rule-based pass.
    cron = task.cron_expression or safe_parse_schedule_rules_only(task.schedule or "")
    cron_to_persist = cron or task.cron_expression
    next_run_compute_failed = False

    decision = _collapse(
        cron=cron if recurring else None,
        tz=task.timezone or "UTC",
        next_run_at=orig_next,
        now=now.replace(tzinfo=None),
        grace_seconds=_grace_seconds(task),
        recurring=recurring,
    )
    # None ⇒ nothing to collapse (marker missing or not yet due) — the
    # normal fire path proceeds with a synthetic fire_late decision.
    if decision is None:
        decision = type(decision)("fire_late", orig_next, 0, None, False)

    if cron and recurring:
        try:
            real_next = next_run_at(cron, after=now, tz_name=task.timezone)
        except Exception as e:
            logger.warning("dispatcher: next_run_at compute failed for %s: %s", task.id, e)
            real_next = now + _backoff(task)
            next_run_compute_failed = True
    else:
        real_next = now + _backoff(task)

    # Misfire visibility: warn (don't silently skip) when firing late.
    late_by_flag = False
    try:
        from app.config import settings
        late_by = (now - due_utc).total_seconds()
        late_by_flag = late_by > settings.AUTOMATION_MISFIRE_WARN_SECONDS
        if late_by_flag:
            logger.warning(
                "automation misfire: task %s '%s' fired %.0fs late (scheduled %s) "
                "— run was likely delayed by downtime or backlog",
                task.id, (task.name or "")[:40], late_by, orig_next.isoformat(),
            )
    except Exception:
        pass

    # R4/§6.1a collapse: a slot missed beyond grace never fires. Recurring
    # schedules advance to the next future slot; the whole episode is
    # recorded as ONE skipped occurrence row (bounded backlog by
    # construction); once-triggers terminalize unfired.
    from app.models.automation_scheduling import AutomationOccurrence

    def _record_skipped_occurrence() -> None:
        db.add(AutomationOccurrence(
            automation_task_id=task.id,
            occurrence_key=f"scheduled:{decision.representative.isoformat()}"
            if decision.representative else f"misfire:{uuid.uuid4().hex}",
            scheduled_at=decision.representative,
            status="skipped",
            cancel_reason="misfire",
            misfire_collapsed_count=decision.collapsed_count,
            completed_at=now.replace(tzinfo=None),
            org_id=task.org_id, app_id=task.app_id,
            created_by_id=task.created_by_id,
        ))

    if decision.action == "skip":
        if decision.catchup:
            catchup_to = decision.advance_to or (
                next_run_at(cron, after=now, tz_name=task.timezone)
                if cron else now + _backoff(task)
            )
            _record_skipped_occurrence()
            db.execute(
                update(AutomationTask)
                .where(
                    AutomationTask.id == task.id,
                    AutomationTask.next_run_at == orig_next,
                )
                .values(
                    next_run_at=catchup_to,
                    last_run=now.isoformat(),
                    last_result=f"misfire skipped (grace {int(_grace_seconds(task))}s "
                                f"exceeded by {(now - due_utc).total_seconds():.0f}s; "
                                f"{decision.collapsed_count} slot(s) collapsed)",
                    cron_expression=cron_to_persist,
                )
            )
            db.commit()
            logger.warning(
                "automation misfire collapse: task %s skipped slot %s "
                "(beyond grace, %d slot(s) collapsed); schedule advanced to %s",
                task.id, orig_next.isoformat(), decision.collapsed_count,
                catchup_to.isoformat() if catchup_to else "backoff",
            )
            return
        # once + beyond grace → terminal without firing.
        _record_skipped_occurrence()
        db.execute(
            update(AutomationTask)
            .where(
                AutomationTask.id == task.id,
                AutomationTask.next_run_at == orig_next,
            )
            .values(
                status="completed",
                next_run_at=None,
                last_run=now.isoformat(),
                last_result="once trigger missed beyond grace window — not fired",
            )
        )
        db.commit()
        logger.warning(
            "automation misfire: once task %s missed beyond grace — terminalized unfired",
            task.id,
        )
        return

    # Once-trigger fire path: terminal after this single fire.
    if trigger == "once":
        real_next = None

    # CAS claim: atomically advance next_run_at only if it hasn't changed
    # since we read it. rowcount == 1 ⇒ we won the race; 0 ⇒ another worker
    # (or a schedule edit) beat us — skip silently. R4: once-triggers
    # terminalize in the same CAS (status → completed, no next slot);
    # a next_run_at compute failure marks the schedule degraded loudly
    # (audit B12) instead of silently backing off forever.
    fire_values = {
        "next_run_at": real_next,
        "last_run_at": now,
        "last_run": now.isoformat(),
        "cron_expression": cron_to_persist,
    }
    if trigger == "once":
        fire_values["status"] = "completed"
    if next_run_compute_failed:
        fire_values["schedule_degraded"] = True
        fire_values["schedule_error"] = (
            f"next_run_at compute failed at {now.isoformat()}; "
            "task fires on backoff until the schedule is re-saved"
        )[:300]
    rc = db.execute(
        update(AutomationTask)
        .where(
            AutomationTask.id == task.id,
            AutomationTask.next_run_at == orig_next,
        )
        .values(**fire_values)
    )
    db.commit()
    if rc.rowcount != 1:
        logger.info("dispatcher: lost CAS race for task %s — skipping", task.id)
        return

    # R5 occurrence claim: the UNIQUE (task, occurrence_key) index is the
    # duplicate-claim arbiter. A lost claim (crash replay, double leader)
    # means the slot is already owned — no second execution is created.
    from app.database import SessionLocal
    from app.services.automation.scheduler_core import (
        acquire_lease,
        claim_occurrence,
        release_lease,
    )

    if trigger == "once":
        occ_key = f"manual:{uuid.uuid4().hex}"  # once = unique slot
    else:
        # §6.1a: the occurrence represents the LATEST missed slot of the
        # episode (R), not the stale persisted marker.
        rep = decision.representative or orig_next or now
        occ_key = f"scheduled:{rep.replace(tzinfo=None, microsecond=0).isoformat()}"
    occurrence = claim_occurrence(
        db,
        automation_task_id=task.id,
        occurrence_key=occ_key,
        scheduled_at=orig_next,
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
        misfire_collapsed_count=decision.collapsed_count if late_by_flag else 0,
    )
    if occurrence is None:
        logger.info(
            "dispatcher: occurrence %s already claimed for task %s — skipping",
            occ_key, task.id,
        )
        return

    execution = AutomationExecution(
        id=str(uuid.uuid4()),
        automation_task_id=task.id,
        status="queued",
        attempt=0,
        occurrence_id=occurrence.id,
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
        lease_owner=_worker_id(),
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    # R5 lease + launch: acquired at claim; heartbeat-renewed while the
    # execution is live; released at exit. Fencing-token enforcement of
    # terminal writes lands with the R6 runner rewrite (executor paths).
    _launch_with_lease(db, execution.id, occurrence)


async def _heartbeat_loop(execution_id: str, tok: Optional[str]) -> None:
    if not tok:
        return
    from app.services.automation.scheduler_core import renew_lease

    while True:
        await asyncio.sleep(30)
        try:
            fresh = SessionLocal()
            try:
                row = fresh.query(AutomationExecution).filter(
                    AutomationExecution.id == execution_id
                ).first()
                if row is None or row.status not in ("queued", "running"):
                    return
                renew_lease(fresh, execution_id, tok)
            finally:
                fresh.close()
        except Exception as e:  # noqa: BLE001 — heartbeat never kills the run
            logger.warning("dispatcher: lease heartbeat failed for %s: %s", execution_id, e)


def _launch_with_lease(db, execution_id: str, occurrence=None, *,
                       require_lease: bool = False) -> Optional[str]:
    """R5 launch path shared by fresh fires AND promoted retry rows:
    acquire the lease, start the heartbeat, spawn the executor, and on
    exit release the lease + terminalize the occurrence.

    ``require_lease=True`` (promotion path): a live lease held elsewhere
    (``acquire_lease`` → None) means another process already launched this
    retry — spawn NOTHING (multi-process safety). Fresh fires keep the
    permissive default until the R6 runner rewrite makes fencing
    unconditional."""
    from app.services.automation.scheduler_core import (
        acquire_lease as _acquire,
        complete_occurrence as _complete_occ,
        release_lease as _release_tok,
    )

    token = _acquire(db, execution_id, _worker_id())
    if require_lease and token is None:
        logger.info(
            "launch: lease for %s held elsewhere — not spawning (promotion lock)",
            execution_id,
        )
        return None
    heartbeat_task = asyncio.create_task(_heartbeat_loop(execution_id, token))

    def _release_lease_on_exit(_task_obj):
        try:
            fresh = SessionLocal()
            try:
                if token:
                    _release_tok(fresh, execution_id, token)
                row = fresh.query(AutomationExecution).filter(
                    AutomationExecution.id == execution_id
                ).first()
                if row is not None and occurrence is not None and occurrence.status == "claimed":
                    terminal = {
                        "completed": "completed",
                        "succeeded": "completed",
                        "failed": "failed_terminal",
                        "timed_out": "failed_terminal",
                        "skipped": "skipped",
                        "cancelled": "cancelled",
                    }.get(row.status)
                    if terminal:
                        _complete_occ(fresh, occurrence, terminal)
            finally:
                fresh.close()
        except Exception:  # noqa: BLE001
            logger.exception("dispatcher: lease/occurrence release failed for %s", execution_id)

    run_task = asyncio.create_task(_run_executor(execution_id))
    run_task.add_done_callback(lambda _t: (_release_lease_on_exit(_t), heartbeat_task.cancel()))
    return token

    # Spawn the executor in the background so multiple tasks can run in
    # parallel (subject to asyncio + LLM provider concurrency).


async def _run_executor(execution_id: str) -> None:
    """Call the synchronous executor in a thread so the dispatcher loop
    doesn't block on long agent runs.

    Bounded by the global concurrency semaphore so a burst of due tasks
    can't exhaust RAM or rate-limit the LLM provider. Tasks that arrive
    while the cap is saturated wait here (in FIFO order) rather than being
    dropped — they still run, just sequentially past the cap.
    """
    global _concurrency_sem
    sem = _concurrency_sem
    try:
        from app.config import settings
        if getattr(settings, "AUTOMATION_SUBPROCESS_MODE", False):
            # §6.5 stage 2 (flag-gated, default OFF): the harness runs in a
            # child process over the JSONL control pipe; the parent keeps
            # ALL automation-table ownership and finalizes fenced.
            if sem is not None:
                async with sem:
                    await asyncio.to_thread(
                        _run_subprocess_stage2, execution_id)
            else:
                await asyncio.to_thread(_run_subprocess_stage2, execution_id)
            return
        from app.services.automation_executor import execute_automation
        if sem is not None:
            async with sem:
                await asyncio.to_thread(execute_automation, execution_id)
        else:
            # Semaphore not initialized (e.g. trigger before start_dispatcher) —
            # run unbounded rather than dropping the task.
            await asyncio.to_thread(execute_automation, execution_id)
    except Exception as e:
        logger.exception("_run_executor: %s failed: %s", execution_id, e)


def _run_subprocess_stage2(execution_id: str) -> None:
    """Stage-2 runner (§6.5/F-22): spawn the child, broker its events, then
    finalize the execution FENCED as the parent (the child never writes
    automation state). Outcome mapping:
    - terminal run_finished → persist output + fenced succeeded
    - terminal run_failed   → persist partial content + fenced failed
    - crashed/terminated    → fenced failed(timed_out)/transient per §6.5
    """
    from app.database import SessionLocal
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_task import AutomationTask
    from app.services.automation import execution_events as ev
    from app.services.automation.runner_protocol import (
        current_token, finalize_terminal, persist_output_and_enter_finalizing,
    )
    from app.services.automation.scheduler_core import fenced_status_write

    db = SessionLocal()
    try:
        ex = db.get(AutomationExecution, execution_id)
        if ex is None:
            logger.error("subprocess stage2: execution %s missing", execution_id)
            return
        task = db.get(AutomationTask, ex.automation_task_id)
        from app.config import settings as _cfg
        job = {
            "execution_id": execution_id,
            "attempt": ex.attempt or 0,
            "user_id": getattr(task, "created_by_id", None),
            "org_id": getattr(ex, "org_id", None) or "default-org",
            "app_id": getattr(task, "app_id", None) or "default-app",
            "project_id": getattr(task, "project_id", None),
            "conversation_id": getattr(ex, "conversation_id", None),
            "automation_task_id": str(ex.automation_task_id),
            "message": getattr(task, "prompt", "") or "",
            "force_planning": bool(getattr(_cfg,
                                           "AUTOMATION_FORCE_PLANNING", True)),
        }
        from app.services.automation.subprocess_runner import (
            launch_subprocess_runner,
        )
        summary = launch_subprocess_runner(job)

        token = current_token(db, execution_id, _worker_id())
        terminal = summary.get("terminal_event") or {}
        if summary["outcome"] == "completed" and \
                terminal.get("type") == "run_finished" and token:
            payload = terminal.get("payload") or {}
            persist_output_and_enter_finalizing(
                db, execution_id, token, AutomationExecution,
                output_text=(payload.get("content") or "")[:200_000],
            )
            finalize_terminal(db, execution_id, token, AutomationExecution,
                              terminal_status="succeeded")
        elif summary["outcome"] == "completed" and \
                terminal.get("type") == "run_failed" and token:
            payload = terminal.get("payload") or {}
            persist_output_and_enter_finalizing(
                db, execution_id, token, AutomationExecution,
                output_text=(payload.get("content") or "")[:200_000],
            )
            finalize_terminal(db, execution_id, token, AutomationExecution,
                              terminal_status="failed",
                              error=(payload.get("error") or "")[:500])
        else:
            # crashed / terminated / protocol violation — §6.5 child-crash
            # mapping: failed(transient); retry per policy (F-27 barrier
            # applies before any retry attempt opens).
            from datetime import datetime, timezone

            if token:
                fenced_status_write(
                    db, execution_id, token,
                    from_statuses=("queued", "running"), to_status="failed",
                    extra_values={
                        "error": (f"subprocess {summary['outcome']}"
                                  f" rc={summary.get('returncode')}")[:500],
                        "completed_at": datetime.now(timezone.utc),
                    },
                    execution_cls=AutomationExecution,
                )
            else:
                logger.warning(
                    "subprocess stage2: %s %s with no ownable lease — "
                    "row left for recovery",
                    execution_id, summary["outcome"])
        ev.record_telemetry_event(
            execution_id, "stage2_summary",
            {k: v for k, v in summary.items() if k != "terminal_event"},
        )
    except Exception:
        logger.exception("subprocess stage2 failed for %s", execution_id)
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Retry scheduling (called from the executor thread + the janitor)
# ---------------------------------------------------------------------------

def schedule_retry(
    task_id: str, prev_execution_id: str, prev_attempt: int, error: str
) -> Optional[str]:
    """Create a PERSISTED retry execution row (attempt + 1, queued, with a
    persisted ``next_attempt_at`` backoff) — roadmap R5: retries survive
    process death because the run-when lives in the DB, not on an event
    loop. The next tick's ``_promote_due_retries`` runs it when due.

    Called from the executor THREAD (via ``asyncio.to_thread``) and from
    the janitor. No in-memory timer exists anymore (``_delayed_run`` is
    retired) — a retry row is always promotable by ANY dispatcher process
    after a restart.

    Returns the new execution id, or ``None`` if ``max_retries`` is
    exhausted.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        task = db.query(AutomationTask).filter(
            AutomationTask.id == task_id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).first()
        if not task:
            return None
        max_retries = parse_max_retries(task)
        next_attempt = prev_attempt + 1
        if next_attempt > max_retries:
            logger.info(
                "schedule_retry: task %s exhausted retries (%d/%d) — giving up",
                task_id, prev_attempt, max_retries,
            )
            return None

        backoff = min(60 * (2 ** prev_attempt), 1800)  # 60s, 120s, 240s… cap 30m
        prev = db.query(AutomationExecution).filter(
            AutomationExecution.id == prev_execution_id
        ).first()
        exec_id = str(uuid.uuid4())
        execution = AutomationExecution(
            id=exec_id,
            automation_task_id=task.id,
            status="queued",
            attempt=next_attempt,
            occurrence_id=prev.occurrence_id if prev else None,
            retry_of_execution_id=prev_execution_id,
            next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=backoff),
            org_id=task.org_id,
            app_id=task.app_id,
            created_by_id=task.created_by_id,
            lease_owner=_worker_id(),
        )
        db.add(execution)
        db.commit()
        logger.info(
            "schedule_retry: task %s retry #%d PERSISTED (next attempt in %.0fs) "
            "exec=%s (prev=%s)",
            task_id, next_attempt, backoff, exec_id, prev_execution_id,
        )
        return exec_id
    except Exception as e:
        logger.exception("schedule_retry failed: %s", e)
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def _promote_due_retries(db, now: datetime) -> int:
    """R5: run persisted retries whose backoff elapsed. The lease is the
    promotion lock — exactly one process launches each retry; crash-safety
    comes from the row itself (a restart re-promotes any un-launched row)."""
    from app.models.automation_scheduling import AutomationOccurrence

    due = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.status == "queued",
            AutomationExecution.retry_of_execution_id.isnot(None),
            AutomationExecution.next_attempt_at.isnot(None),
            AutomationExecution.next_attempt_at <= now,
        )
        .limit(20)
        .all()
    )
    launched = 0
    for ex in due:
        occ = (
            db.query(AutomationOccurrence)
            .filter(AutomationOccurrence.id == ex.occurrence_id)
            .first()
        ) if ex.occurrence_id else None
        token = _launch_with_lease(db, ex.id, occ, require_lease=True)
        if token is not None:
            launched += 1
        logger.info(
            "promote_due_retries: launched persisted retry exec=%s attempt=%d",
            ex.id, ex.attempt,
        )
    return launched


# ---------------------------------------------------------------------------
# Janitor — reap zombie / timed-out executions
# ---------------------------------------------------------------------------

async def _reap_stale_executions(force_all_active: bool = False) -> int:
    """Mark zombie / timed-out executions as failed and schedule retries.

    A zombie is any execution still ``queued``/``running`` whose
    ``timeout_at`` has passed. ``force_all_active=True`` reaps *every*
    queued/running row regardless of deadline — used at startup, where any
    mid-flight execution is a zombie because the in-process executor died
    with the previous container.

    Reaping + retry both use a CAS (``status IN (queued, running)``) so the
    executor and the janitor can't double-mark or double-retry the same row.
    """
    from app.database import SessionLocal
    from app.config import settings

    db = SessionLocal()
    reaped = 0
    try:
        # Postgres TIMESTAMP columns are tz-naive UTC; build a naive now so
        # comparisons don't raise "can't compare offset-naive and offset-aware".
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = db.query(AutomationExecution).filter(
            AutomationExecution.status.in_(["queued", "running"]),
        ).all()

        for ex in rows:
            reason = ""
            if force_all_active:
                reason = "Worker restarted (in-process executor died)"
            elif ex.timeout_at is not None and ex.timeout_at <= now:
                reason = f"Timed out (deadline {ex.timeout_at.isoformat()})"
            elif ex.timeout_at is None and ex.started_at is not None:
                # Migrated / pre-timeout run: use 2x run timeout as a safety net.
                if (now - ex.started_at).total_seconds() > 2 * settings.AUTOMATION_RUN_TIMEOUT_SECONDS:
                    reason = "Stale run (no timeout_at, exceeded safety window)"
            elif ex.timeout_at is None and ex.started_at is None:
                # Queued but never started for too long (e.g. orphaned by a restart).
                age = (now - ex.created_date).total_seconds() if ex.created_date else 0
                if age > 2 * settings.AUTOMATION_RUN_TIMEOUT_SECONDS:
                    reason = "Queued too long (worker likely restarted)"

            if not reason:
                continue

            # CAS transition to failed — only if still queued/running.
            rc = db.execute(
                update(AutomationExecution)
                .where(
                    AutomationExecution.id == ex.id,
                    AutomationExecution.status.in_(["queued", "running"]),
                )
                .values(
                    status="failed",
                    error=reason[:5000],
                    completed_at=now,
                )
            )
            db.commit()
            if rc.rowcount != 1:
                continue  # executor already finalized it

            db.refresh(ex)
            logger.warning(
                "reaper: reaped execution %s (attempt %s): %s",
                ex.id, ex.attempt, reason,
            )
            reaped += 1

            # Schedule a retry if attempts remain.
            task = db.query(AutomationTask).filter(
                AutomationTask.id == ex.automation_task_id
            ).first()
            if task and ex.attempt < parse_max_retries(task):
                # schedule_retry uses run_coroutine_threadsafe, which is
                # safe to call from this (main-loop) thread.
                schedule_retry(task.id, ex.id, ex.attempt, reason)
    except Exception as e:
        logger.exception("reaper: scan failed: %s", e)
    finally:
        try:
            db.close()
        except Exception:
            pass

    if reaped:
        logger.info("reaper: reaped %d zombie execution(s)", reaped)
    return reaped


async def reap_on_startup() -> int:
    """Startup hook (§6.3, R5): NON-DESTRUCTIVE recovery.

    - Expired-lease executions → ``recover_expired_leases``: output
      present ⇒ finalize-replay (idempotent); no output ⇒ F-27
      reconciliation barrier (unknown non-idempotent ⇒ BLOCKED, never
      auto-retried); otherwise a new attempt under the same occurrence.
    - Orphaned occurrence claims (leader died pre-execution) →
      ``recover_stuck_claims`` → back to ``pending``.
    - Unexpired leases owned elsewhere are NEVER touched (audit B4).

    Escape hatch ``AUTOMATION_FORCE_REAP_ON_STARTUP`` (default false) keeps
    the old destructive force-reap for single-instance emergencies only.
    """
    _ensure_schema()
    from app.config import settings

    if getattr(settings, "AUTOMATION_FORCE_REAP_ON_STARTUP", False):
        logger.warning("AUTOMATION_FORCE_REAP_ON_STARTUP=true — destructive force-reap engaged")
        return await _reap_stale_executions(force_all_active=True)

    from app.database import SessionLocal
    from app.services.automation.scheduler_core import (
        recover_expired_leases as _recover_leases,
        recover_stuck_claims as _recover_claims,
    )

    db = SessionLocal()
    try:
        stats = _recover_leases(
            db, execution_cls=AutomationExecution, owner=_worker_id(),
            finalize_replay=_runner_replay,
            new_attempt=_recovery_new_attempt,
        )
        stuck = _recover_claims(db, execution_cls=AutomationExecution)
        logger.info(
            "startup recovery (§6.3): finalized=%d retried=%d blocked=%d stuck_claims_reopened=%d",
            stats["finalized"], stats["retried"], stats["blocked"], stuck,
        )
        return stats["retried"] + stats["blocked"] + stuck
    finally:
        try:
            db.close()
        except Exception:
            pass


def _runner_replay(db, execution) -> bool:
    """Finalize-replay via the R6 runner protocol (idempotent steps 1–5)."""
    from app.services.automation.runner_protocol import finalize_replay

    return finalize_replay(db, execution, AutomationExecution)


def _recovery_new_attempt(db, execution) -> None:
    """Open the next attempt for a recovered execution: a persisted retry
    row (attempt+1, same occurrence) promotable by any process's tick —
    the F-27 barrier has already cleared at this point."""
    schedule_retry(
        execution.automation_task_id, execution.id,
        getattr(execution, "attempt", 0) or 0,
        error="lease_expired_recovered",
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _dispatcher_loop() -> None:
    """Main loop: sleep TICK_INTERVAL, then run a tick. Stops on shutdown."""
    global _main_loop
    logger.info("automation_dispatcher: started (tick=%ds, budget=%ds)", TICK_INTERVAL, TICK_BUDGET)
    assert _shutdown_event is not None
    # Capture the running loop so the executor thread can schedule retries
    # back onto it via run_coroutine_threadsafe.
    _main_loop = asyncio.get_running_loop()
    while not _shutdown_event.is_set():
        try:
            # Use wait_for so the loop can be cancelled promptly on shutdown.
            await asyncio.wait_for(_shutdown_event.wait(), timeout=TICK_INTERVAL)
            # If we get here, the event was set — exit.
            break
        except asyncio.TimeoutError:
            # Normal tick.
            try:
                await asyncio.wait_for(_tick(), timeout=TICK_BUDGET)
            except asyncio.TimeoutError:
                logger.warning("automation_dispatcher: tick exceeded budget; skipping")
            except Exception as e:
                logger.exception("automation_dispatcher: tick error: %s", e)
    logger.info("automation_dispatcher: stopped")


def start_dispatcher() -> None:
    """Start the dispatcher background task. Call from app startup."""
    global _running_tasks, _shutdown_event, _concurrency_sem
    if _running_tasks:
        logger.warning("automation_dispatcher: already running — skipping start")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("automation_dispatcher: no event loop; not started")
        return
    _shutdown_event = asyncio.Event()
    # Build the concurrency cap on the running loop. Default 3; tune via
    # AUTOMATION_MAX_CONCURRENCY. Must be (re)created per start because a
    # Semaphore is bound to the loop that created it.
    from app.config import settings
    try:
        cap = max(1, int(getattr(settings, "AUTOMATION_MAX_CONCURRENCY", 3) or 3))
    except (TypeError, ValueError):
        cap = 3
    _concurrency_sem = asyncio.Semaphore(cap)
    logger.info("automation_dispatcher: concurrency cap = %d", cap)
    task = loop.create_task(_dispatcher_loop(), name="automation-dispatcher")
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    _running_tasks = [task]


async def stop_dispatcher() -> None:
    """Stop the dispatcher and wait for the loop to exit cleanly."""
    global _running_tasks, _shutdown_event, _main_loop, _concurrency_sem
    if _shutdown_event is not None:
        _shutdown_event.set()
    if _running_tasks:
        await asyncio.gather(*_running_tasks, return_exceptions=True)
    _running_tasks = []
    _shutdown_event = None
    _main_loop = None
    _concurrency_sem = None


# ---------------------------------------------------------------------------
# Manual trigger
# ---------------------------------------------------------------------------

# Max nesting depth for execute_automation-spawned runs. A run at depth N may
# spawn a child at depth N+1; once the child's depth would EXCEED this cap the
# spawn is refused (see automation_chat_tool.execute_automation_tool and the
# defense-in-depth check in automation_executor.execute_automation). Default 3
# = E1→E2→E3 allowed, E4 refused. Override via ZHANLU_AUTOMATION_MAX_DEPTH.
AUTOMATION_MAX_RECURSION_DEPTH = max(
    1, int(os.environ.get("ZHANLU_AUTOMATION_MAX_DEPTH", "3"))
)


def compute_execution_depth(db, execution_id: Optional[str]) -> int:
    """Return the nesting depth of ``execution_id`` by walking its
    ``parent_execution_id`` chain.

    A top-level execution (parent_execution_id is NULL) has depth 1; a child
    spawned from it has depth 2; and so on. Returns 0 for a missing/None id.
    The walk is bounded by ``AUTOMATION_MAX_RECURSION_DEPTH + 3`` and a
    visited-set, so cyclic data can't loop forever.
    """
    if not execution_id:
        return 0
    depth = 0
    cur: Optional[str] = execution_id
    seen: set = set()
    guard = AUTOMATION_MAX_RECURSION_DEPTH + 3
    while cur and cur not in seen and depth < guard:
        seen.add(cur)
        row = db.query(AutomationExecution).filter(
            AutomationExecution.id == cur
        ).first()
        if row is None:
            break
        depth += 1
        cur = getattr(row, "parent_execution_id", None)
    return depth


class AutomationRunConflict(Exception):
    """Raised when a run-now request hits a task that already has a
    non-terminal execution (R1 P0 in-flight guard, audit B10).

    ``execution_id`` carries the in-flight execution for the 409 payload.
    """

    def __init__(self, execution_id: Optional[str] = None):
        self.execution_id = execution_id
        super().__init__(
            f"automation task already has an in-flight execution "
            f"({execution_id or 'unknown'})"
        )


# Non-terminal execution statuses used by the R1 in-flight guard and the
# delete-path cancellation sweep.
_NON_TERMINAL_EXECUTION_STATUSES = ("queued", "running")


async def trigger_now(
    task_id: str,
    parent_execution_id: Optional[str] = None,
    *,
    force: bool = False,
) -> Optional[str]:
    """Force an immediate run of the given task. Returns the execution id.

    ``parent_execution_id`` stamps the recursion chain when this run is spawned
    from inside another run via the ``execute_automation`` tool (NULL for
    top-level manual/scheduled triggers). The depth cap itself is enforced by
    the caller (``execute_automation_tool``) before spawning; this function
    only records the parent so the chain can be walked later.

    In-flight guard (R1, audit B10): when the task already has a queued or
    running execution, raises :class:`AutomationRunConflict` unless
    ``force=True``. The check runs under a ``SELECT ... FOR UPDATE`` lock on
    the task row so two concurrent triggers serialize (SQLite ignores the
    lock; PostgreSQL multi-process runs serialize on the task row).
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        task = db.query(AutomationTask).filter(
            AutomationTask.id == task_id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).with_for_update().first()
        if not task:
            return None
        if not force:
            inflight = (
                db.query(AutomationExecution)
                .filter(
                    AutomationExecution.automation_task_id == task.id,
                    AutomationExecution.status.in_(
                        _NON_TERMINAL_EXECUTION_STATUSES
                    ),
                )
                .first()
            )
            if inflight is not None:
                db.rollback()  # release the task lock
                raise AutomationRunConflict(inflight.id)
        execution = AutomationExecution(
            id=str(uuid.uuid4()),
            automation_task_id=task.id,
            status="queued",
            attempt=0,
            org_id=task.org_id,
            app_id=task.app_id,
            created_by_id=task.created_by_id,
            lease_owner=_worker_id(),
            parent_execution_id=parent_execution_id,
        )
        db.add(execution)
        db.commit()
        db.refresh(execution)
        asyncio.create_task(_run_executor(execution.id))
        return execution.id
    finally:
        try:
            db.close()
        except Exception:
            pass


def recompute_next_run(task_id: str) -> Optional[datetime]:
    """Recompute and persist ``next_run_at`` for a task. Useful when the
    user toggles it from paused → active.

    User-initiated (not in the hot tick), so the LLM-based fallback is
    acceptable here for tasks that never got a cron_expression at creation.
    """
    from app.services.schedule_parser import safe_parse_schedule
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        task = db.query(AutomationTask).filter(
            AutomationTask.id == task_id,
        ).first()
        if not task:
            return None
        cron = task.cron_expression or safe_parse_schedule(task.schedule or "")
        if not cron:
            return None
        task.cron_expression = cron
        task.next_run_at = next_run_at(cron, after=datetime.now(timezone.utc), tz_name=task.timezone)
        db.commit()
        return task.next_run_at
    except Exception as e:
        logger.warning("recompute_next_run: failed for %s: %s", task_id, e)
        db.rollback()
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


__all__ = [
    "start_dispatcher",
    "stop_dispatcher",
    "trigger_now",
    "recompute_next_run",
    "reap_on_startup",
    "schedule_retry",
    "parse_max_retries",
    "TICK_INTERVAL",
    "AUTOMATION_MAX_RECURSION_DEPTH",
    "compute_execution_depth",
]
