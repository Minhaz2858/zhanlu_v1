"""Schedule Trigger daemon — fires published workflows on cron expressions.

n8n Schedule-Trigger parity: a background thread polls published flow_mode
agents for ``trigger_schedule`` nodes and runs any whose cron expression is
due. Runs are executed in worker threads (same pattern as run-async) with
``WorkflowRun.trigger='schedule'``.

R6 Phase 5 — occurrence-claim adoption (§1.3b, migration 130): each fire is
preceded by ``claim_occurrence(occurrence_cls=WorkflowOccurrence)`` over the
UNIQUE ``(agent_app_id, node_id, occurrence_key)`` index, so N backend
processes poll the same cron and EXACTLY ONE wins each slot (the previous
in-memory in-flight set was per-process and could double-fire). The
occurrence_key is ``scheduled:{slot_iso}`` — the representative cron slot
inside the poll window, so repeated ticks near a boundary dedupe. The
occurrence is terminalized after the run (completed / failed_terminal);
crash recovery re-claims stale pending rows via ``recover_stuck_claims``.

Limitations (documented): a fire missed while the backend is down is
skipped, not backfilled (no misfire collapse for workflows yet). Cron
evaluation uses croniter's ``get_next(datetime)`` which is
strictly-after-now, so each expression fires at most once per poll.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("zhanlu.workflow_scheduler")

POLL_INTERVAL_SECONDS = 30

from app.models.automation_scheduling import WorkflowOccurrence  # noqa: E402


# Indirection hooks (module-level so tests can fake them; production paths
# resolve lazily inside the functions to avoid import cycles).
def _session_factory():
    from app.database import SessionLocal
    return SessionLocal()


def _claim_occurrence(db, **kwargs):
    from app.services.automation.scheduler_core import claim_occurrence
    return claim_occurrence(db, **kwargs)


def _complete_occurrence(db, occurrence, status):
    from app.services.automation.scheduler_core import complete_occurrence
    return complete_occurrence(db, occurrence, status)


def _run_workflow_agent(*args, **kwargs):
    from app.services.workflow_runtime import run_workflow_agent
    return run_workflow_agent(*args, **kwargs)


def find_due_scheduled_workflows(
    db, now: datetime | None = None, since: datetime | None = None,
) -> list[tuple[Any, dict, dict, datetime]]:
    """Return [(agent, schedule_node, node_config, slot)] whose cron is due.

    A workflow is due when the cron's next fire after ``since`` (the previous
    poll) is at or before ``now``. Defaults: now = UTC now, since = one poll
    interval earlier — this catches minute-level crons like ``* * * * *``
    without firing early (an early-fire would need ``next <= now + interval``).
    The 4th element is the representative slot (the fire time inside the
    window) — the occurrence_key source.

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
    due: list[tuple[Any, dict, dict, datetime]] = []
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
                due.append((agent, node, config, next_fire))
    return due


class WorkflowScheduler:
    """Background cron poller for Schedule Trigger nodes."""

    def __init__(self, poll_interval: int = POLL_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="workflow-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Workflow scheduler tick failed")

    def tick(self) -> int:
        """One poll: fire all due scheduled workflows (claimed exactly-once).

        Returns the fired count (claimed slots whose run thread was
        spawned). Slots lost to another process are silently skipped —
        the winner fires them.
        """
        from app.models.agent_app import AgentApp

        db = _session_factory()
        try:
            due = find_due_scheduled_workflows(db)
        finally:
            db.close()
        if not due:
            return 0

        fired = 0
        claims: list[tuple[Any, Any, dict, Any]] = []
        for agent, node, config, slot in due:
            # Claim the slot FIRST (cross-process arbiter). A separate
            # short session per claim — never the poll session.
            claim_db = _session_factory()
            try:
                occ = _claim_occurrence(
                    claim_db,
                    automation_task_id="unused-for-workflow",
                    occurrence_key=f"scheduled:{slot.isoformat()}",
                    scheduled_at=slot,
                    org_id=getattr(agent, "org_id", None) or "default-org",
                    app_id=getattr(agent, "app_id", None) or "default-app",
                    created_by_id=getattr(agent, "created_by_id", None),
                    occurrence_cls=WorkflowOccurrence,
                    subject_fields={
                        "agent_app_id": agent.id,
                        "node_id": str(node.get("id") or "trigger_schedule")[:80],
                    },
                )
            finally:
                claim_db.close()
            if occ is None:
                continue  # another process won this slot — never double-fire
            claims.append((agent, node, config, occ))

        if not claims:
            return 0
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as pool:
            for agent, node, config, occ in claims:
                pool.submit(self._run_claimed, agent.id, config, occ)
                fired += 1
        return fired

    def _run_claimed(self, agent_id: str, node_config: dict, occurrence) -> None:
        """Run one claimed slot; terminalize the occurrence either way."""
        from app.models.agent_app import AgentApp

        with self._lock:
            self._in_flight.add(agent_id)
        db = _session_factory()
        try:
            agent = db.query(AgentApp).filter(
                AgentApp.id == agent_id, AgentApp.is_deleted.is_(False),
            ).first()
            if agent is None:
                _complete_occurrence(db, occurrence, "skipped")
                return
            try:
                _run_workflow_agent(
                    db, agent.org_id, agent.app_id, agent,
                    str(node_config.get("input", "")),
                    created_by_id=agent.created_by_id,
                    trigger="schedule",
                )
                _complete_occurrence(db, occurrence, "completed")
            except Exception:  # noqa: BLE001 — honest failure, no fake success
                logger.exception("Scheduled run failed for workflow %s", agent_id)
                _complete_occurrence(db, occurrence, "failed_terminal")
        finally:
            db.close()
            with self._lock:
                self._in_flight.discard(agent_id)


def WorkflowOccurrence_cls():
    """Lazy model resolver (import-cycle safe)."""
    from app.models.automation_scheduling import WorkflowOccurrence
    return WorkflowOccurrence


def WorkflowOccurrence_cls():
    """Lazy model resolver (kept for callers importing the symbol)."""
    return WorkflowOccurrence


workflow_scheduler = WorkflowScheduler()
