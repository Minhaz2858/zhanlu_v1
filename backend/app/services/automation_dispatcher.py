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
        due: list[AutomationTask] = db.query(AutomationTask).filter(
            AutomationTask.status == "active",
            AutomationTask.is_deleted == False,  # noqa: E712
            AutomationTask.next_run_at.isnot(None),
            AutomationTask.next_run_at <= now,
        ).limit(20).all()  # bound per-tick

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


async def _fire(db, task: AutomationTask, now: datetime) -> None:
    """Create the execution row, advance next_run_at, spawn the executor.

    Uses a compare-and-swap on ``next_run_at`` to guarantee exactly-once
    firing even if two dispatcher workers SELECT the same due row.
    """
    orig_next = task.next_run_at

    # Compute the real next_run_at WITHOUT calling the LLM. The LLM-based
    # parse is reserved for task creation (API path); the tick only ever
    # uses the already-persisted cron_expression or a cheap rule-based pass.
    cron = task.cron_expression or safe_parse_schedule_rules_only(task.schedule or "")
    cron_to_persist = cron or task.cron_expression
    if cron:
        try:
            real_next = next_run_at(cron, after=now, tz_name=task.timezone)
        except Exception as e:
            logger.warning("dispatcher: next_run_at compute failed for %s: %s", task.id, e)
            real_next = now + _backoff(task)
    else:
        real_next = now + _backoff(task)

    # Misfire visibility: warn (don't silently skip) when firing late.
    try:
        from app.config import settings
        late_by = (now - orig_next).total_seconds()
        if late_by > settings.AUTOMATION_MISFIRE_WARN_SECONDS:
            logger.warning(
                "automation misfire: task %s '%s' fired %.0fs late (scheduled %s) "
                "— run was likely delayed by downtime or backlog",
                task.id, (task.name or "")[:40], late_by, orig_next.isoformat(),
            )
    except Exception:
        pass

    # CAS claim: atomically advance next_run_at only if it hasn't changed
    # since we read it. rowcount == 1 ⇒ we won the race; 0 ⇒ another worker
    # (or a schedule edit) beat us — skip silently.
    rc = db.execute(
        update(AutomationTask)
        .where(
            AutomationTask.id == task.id,
            AutomationTask.next_run_at == orig_next,
        )
        .values(
            next_run_at=real_next,
            last_run_at=now,
            last_run=now.isoformat(),
            cron_expression=cron_to_persist,
        )
    )
    db.commit()
    if rc.rowcount != 1:
        logger.info("dispatcher: lost CAS race for task %s — skipping", task.id)
        return

    execution = AutomationExecution(
        id=str(uuid.uuid4()),
        automation_task_id=task.id,
        status="queued",
        attempt=0,
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
        lease_owner=_worker_id(),
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    # Spawn the executor in the background so multiple tasks can run in
    # parallel (subject to asyncio + LLM provider concurrency).
    asyncio.create_task(_run_executor(execution.id))


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


# ---------------------------------------------------------------------------
# Retry scheduling (called from the executor thread + the janitor)
# ---------------------------------------------------------------------------

def schedule_retry(
    task_id: str, prev_execution_id: str, prev_attempt: int, error: str
) -> Optional[str]:
    """Create a retry execution row (attempt + 1, queued) and schedule its run.

    Called from the executor THREAD (via ``asyncio.to_thread``) and from the
    janitor. The actual run is scheduled on the main event loop via
    ``run_coroutine_threadsafe`` with exponential backoff.

    Returns the new execution id, or ``None`` if ``max_retries`` is
    exhausted or no running main loop is available (in the latter case no
    row is created, so we don't strand an un-runnable retry).
    """
    # Don't create a doomed retry row if there's no loop to run it on.
    loop = _main_loop
    if loop is None or not loop.is_running():
        logger.warning(
            "schedule_retry: no running main loop — retry for %s not created",
            prev_execution_id,
        )
        return None

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
        exec_id = str(uuid.uuid4())
        execution = AutomationExecution(
            id=exec_id,
            automation_task_id=task.id,
            status="queued",
            attempt=next_attempt,
            org_id=task.org_id,
            app_id=task.app_id,
            created_by_id=task.created_by_id,
            lease_owner=_worker_id(),
        )
        db.add(execution)
        db.commit()

        asyncio.run_coroutine_threadsafe(_delayed_run(exec_id, backoff), loop)
        logger.info(
            "schedule_retry: task %s retry #%d queued (%.0fs backoff) exec=%s (prev=%s)",
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


async def _delayed_run(execution_id: str, delay: float) -> None:
    """Sleep ``delay`` then run the executor. Scheduled on the main loop."""
    try:
        await asyncio.sleep(delay)
        await _run_executor(execution_id)
    except Exception as e:
        logger.exception("_delayed_run %s failed: %s", execution_id, e)


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
    """Startup hook: ensure schema, then reap all mid-flight executions.

    Called from ``main.py`` startup. Any execution left ``queued``/``running``
    from a previous process is a zombie (the in-process executor died with
    the old container). Mark them failed and retry if attempts remain —
    otherwise they'd sit in "running" forever (the original P0 bug).
    """
    _ensure_schema()
    return await _reap_stale_executions(force_all_active=True)


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


async def trigger_now(
    task_id: str, parent_execution_id: Optional[str] = None
) -> Optional[str]:
    """Force an immediate run of the given task. Returns the execution id.

    ``parent_execution_id`` stamps the recursion chain when this run is spawned
    from inside another run via the ``execute_automation`` tool (NULL for
    top-level manual/scheduled triggers). The depth cap itself is enforced by
    the caller (``execute_automation_tool``) before spawning; this function
    only records the parent so the chain can be walked later.
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
