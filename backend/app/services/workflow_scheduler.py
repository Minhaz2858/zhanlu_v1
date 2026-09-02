"""Schedule Trigger daemon — fires published workflows on cron expressions.

n8n Schedule-Trigger parity: a background thread polls published flow_mode
agents for ``trigger_schedule`` nodes and runs any whose cron expression is
due. Runs are executed in worker threads (same pattern as run-async) with
``WorkflowRun.trigger='schedule'``.

Limitations (documented): the scheduler is in-memory — a fire missed while
the backend is down is skipped, not backfilled. Cron evaluation uses
croniter's ``get_next(datetime)`` which is strictly-after-now, so each
expression fires at most once per poll even if the poll interval drifts.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("zhanlu.workflow_scheduler")

POLL_INTERVAL_SECONDS = 30


def find_due_scheduled_workflows(
    db, now: datetime | None = None, since: datetime | None = None,
) -> list[tuple[Any, dict, dict]]:
    """Return [(agent, schedule_node, node_config)] whose cron is due at ``now``.

    A workflow is due when the cron's next fire after ``since`` (the previous
    poll) is at or before ``now``. Defaults: now = UTC now, since = one poll
    interval earlier — this catches minute-level crons like ``* * * * *``
    without firing early (an early-fire would need ``next <= now + interval``).

    Pure query + cron evaluation — unit-testable without the thread.
    """
    from app.models.agent_app import AgentApp
    from croniter import croniter

    now = now or datetime.now(timezone.utc)
    since = since or (now - timedelta(seconds=POLL_INTERVAL_SECONDS))
    agents = db.query(AgentApp).filter(
        AgentApp.flow_mode.is_(True),
        AgentApp.status == "published",
        AgentApp.is_deleted.is_(False),
    ).all()
    due: list[tuple[Any, dict, dict]] = []
    for agent in agents:
        flow = agent.flow if isinstance(agent.flow, dict) else {}
        for node in flow.get("nodes") or []:
            if node.get("type") != "trigger_schedule":
                continue
            config = (node.get("data") or {}).get("config") or {}
            cron_expr = config.get("cron")
            if not cron_expr:
                continue
            try:
                itr = croniter(cron_expr, since)
                next_fire = itr.get_next(datetime)
            except (ValueError, KeyError) as exc:
                logger.warning("Schedule %s: bad cron %r: %s", agent.id, cron_expr, exc)
                continue
            if next_fire <= now:
                due.append((agent, node, config))
    return due


class WorkflowScheduler:
    """Background cron poller for Schedule Trigger nodes."""

    def __init__(self, poll_interval: int = POLL_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="workflow-scheduler", daemon=True,
        )
        self._thread.start()
        logger.info("Workflow scheduler started (poll=%ds)", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Workflow scheduler stopped")

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Workflow scheduler tick failed")

    def tick(self) -> int:
        """One poll: fire all due scheduled workflows. Returns fired count."""
        from app.database import SessionLocal
        from concurrent.futures import ThreadPoolExecutor

        fired = 0
        db = SessionLocal()
        try:
            due = find_due_scheduled_workflows(db)
        finally:
            db.close()
        with self._lock:
            pending = [d for d in due if d[0].id not in self._in_flight]
            for agent, _node, _cfg in pending:
                self._in_flight.add(agent.id)
        if not pending:
            return 0

        def _run(agent_id: str, node_config: dict) -> None:
            from app.database import SessionLocal
            from app.models.agent_app import AgentApp
            from app.services.workflow_runtime import run_workflow_agent

            thread_db = SessionLocal()
            try:
                agent = thread_db.query(AgentApp).filter(
                    AgentApp.id == agent_id, AgentApp.is_deleted.is_(False),
                ).first()
                if agent is None:
                    return
                run_workflow_agent(
                    thread_db, agent.org_id, agent.app_id, agent,
                    str(node_config.get("input", "")),
                    created_by_id=agent.created_by_id,
                    trigger="schedule",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Scheduled run failed for workflow %s", agent_id)
            finally:
                thread_db.close()
                with self._lock:
                    self._in_flight.discard(agent_id)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for agent, _node, config in pending:
                pool.submit(_run, agent.id, config)
                fired += 1
        return fired


workflow_scheduler = WorkflowScheduler()
