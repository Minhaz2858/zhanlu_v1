"""Stage 2 — R5 acceptance on REAL PostgreSQL: multi-process scheduling.

Scenarios (brief §Stage 2):
1. duplicate occurrence dispatch across independent processes
2. worker crash + restart (lease expiry → recovery)
3. scheduler leader failure (orphaned claim → recover_stuck_claims)
4. lease takeover + stale worker terminal writes
5. workflow-schedule claim via the WorkflowOccurrence adapter
6. attempt-scoped event replay (F-26 unique index absorbs same-attempt
   replays; cross-attempt seq reuse legal)
7. application restart with pending occurrences (pending rows survive and
   are re-claimable; nothing is backfilled or lost)

Real PG only — never faked on SQLite. Disposable scratch DB
(``automation_r5_test``); skipped without a DSN.
"""
from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

PG_DSN = os.environ.get(
    "AUTOMATION_R5_PG_DSN",
    "postgresql+psycopg2://r5test:r5test_only_disposable@localhost:5400/automation_r5_test",
)

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import (  # noqa: E402
    AutomationExecutionEvent,
    AutomationOccurrence,
    ExecutionLease,
    WorkflowOccurrence,
)
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import scheduler_core as sc  # noqa: E402


def _pg_available() -> bool:
    try:
        eng = create_engine(PG_DSN, pool_pre_ping=True)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL test database not reachable"
)


@pytest.fixture(scope="module")
def pg():
    from app.database import Base
    import app.models  # noqa: F401

    eng = create_engine(PG_DSN, pool_pre_ping=True, pool_size=12, max_overflow=8)
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng, expire_on_commit=False)
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.commit()
    eng.dispose()


def _task(s):
    t = AutomationTask(name=f"S2 {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    s.add(t)
    s.commit()
    return t


def _exec(s, task_id, **kw):
    kw.setdefault("status", "running")
    ex = AutomationExecution(automation_task_id=task_id, attempt=0, **kw)
    s.add(ex)
    s.commit()
    return ex


def test_s2_1_duplicate_dispatch_across_processes(pg):
    """Six independent processes fire the same slot — ONE occurrence, ONE
    claimed, five refused (brief: duplicate occurrence dispatch)."""
    s0 = pg()
    t = _task(s0)
    s0.close()
    key = "scheduled:2026-11-03T08:00:00"

    def worker(i):
        s = pg()
        try:
            occ = sc.claim_occurrence(
                s, automation_task_id=t.id, occurrence_key=key, scheduled_at=None,
            )
            return occ.id if occ else None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert sum(1 for r in results if r) == 1
    s = pg()
    assert s.query(AutomationOccurrence).filter_by(occurrence_key=key).count() == 1
    s.close()


def test_s2_2_worker_crash_and_restart_recovery(pg):
    """Worker holds a lease then 'dies' (lease expires). The restarted
    application's recovery finalizes from persisted output — no lost work,
    no duplicate execution."""
    s0 = pg()
    t = _task(s0)
    ex = _exec(s0, t.id, output_text="crashed mid-finalize report", status="finalizing")
    s0.close()

    s = pg()
    sc.acquire_lease(s, ex.id, "dead-worker", ttl_seconds=1)
    s.get(ExecutionLease, ex.id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    s.commit()
    finalized = []
    stats = sc.recover_expired_leases(
        s, execution_cls=AutomationExecution, owner="restarted-app",
        finalize_replay=lambda sess, e: (finalized.append(e.id), True)[1],
    )
    s.close()
    assert stats["finalized"] == 1 and finalized == [ex.id]


def test_s2_3_leader_failure_orphaned_claim_recovered(pg):
    """Leader claims an occurrence then dies before creating the execution.
    After the stuck-claim window, another process re-opens the occurrence —
    the slot is not lost, and a live run is never disturbed."""
    s0 = pg()
    t = _task(s0)
    occ = sc.claim_occurrence(
        s0, automation_task_id=t.id,
        occurrence_key="scheduled:2026-11-03T09:00:00", scheduled_at=None,
    )
    # Leader dies: no execution row ever created. Age the claim.
    occ.claimed_at = datetime.utcnow() - timedelta(seconds=600)
    s0.commit()
    s0.close()

    s = pg()
    recovered = sc.recover_stuck_claims(
        s, execution_cls=AutomationExecution, stuck_after_seconds=300,
    )
    s.close()
    assert recovered >= 1
    s = pg()
    row = s.query(AutomationOccurrence).filter_by(
        occurrence_key="scheduled:2026-11-03T09:00:00").first()
    assert row.status == "pending"  # re-claimable by any process
    # And it CAN be re-claimed now.
    reocc = sc.claim_occurrence(
        s, automation_task_id=t.id,
        occurrence_key="scheduled:2026-11-03T09:00:00", scheduled_at=None,
    )
    s.close()
    assert reocc is not None and reocc.status == "claimed"


def test_s2_3b_live_claim_not_disturbed_by_stuck_recovery(pg):
    s0 = pg()
    t = _task(s0)
    occ = sc.claim_occurrence(
        s0, automation_task_id=t.id,
        occurrence_key="scheduled:2026-11-03T10:00:00", scheduled_at=None,
    )
    occ.claimed_at = datetime.utcnow() - timedelta(seconds=600)
    s0.commit()
    _exec(s0, t.id, occurrence_id=occ.id, status="running")  # live run
    s0.close()

    s = pg()
    sc.recover_stuck_claims(s, execution_cls=AutomationExecution, stuck_after_seconds=300)
    s.close()
    s = pg()
    row = s.query(AutomationOccurrence).filter_by(
        occurrence_key="scheduled:2026-11-03T10:00:00").first()
    s.close()
    assert row.status == "claimed"  # live execution ⇒ untouched


def test_s2_4_lease_takeover_and_stale_terminal_write(pg):
    s0 = pg()
    t = _task(s0)
    ex = _exec(s0, t.id)
    s0.close()
    sa = pg()
    token_a = sc.acquire_lease(sa, ex.id, "worker-a", ttl_seconds=1)
    sa.close()
    import time
    time.sleep(1.1)
    sb = pg()
    token_b = sc.acquire_lease(sb, ex.id, "worker-b")
    sb.close()
    sc_tw = pg()
    a_won = sc.fenced_status_write(
        sc_tw, ex.id, token_a, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    )
    b_won = sc.fenced_status_write(
        sc_tw, ex.id, token_b, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    )
    sc_tw.close()
    assert (a_won, b_won) == (False, True)


def test_s2_5_workflow_occurrence_claim_adapter(pg):
    """The workflow scheduler reuses the SAME claim mechanics through the
    table adapter — subject-keyed uniqueness, no subject_type overload."""
    s0 = pg()
    from app.models.agent_app import AgentApp

    app_row = AgentApp(name=f"s2wf-{uuid.uuid4().hex[:6]}")
    s0.add(app_row)
    s0.commit()
    s0.close()

    def worker(i):
        s = pg()
        try:
            return sc.claim_occurrence(
                s,
                automation_task_id="unused-for-workflow",
                occurrence_key="scheduled:2026-11-03T11:00:00",
                scheduled_at=None,
                occurrence_cls=WorkflowOccurrence,
                subject_fields={"agent_app_id": app_row.id, "node_id": "cron-node-1"},
            )
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    winners = [r for r in results if r]
    assert len(winners) == 1


def test_s2_6_attempt_scoped_event_replay(pg):
    """F-26: same-attempt replay absorbed by UNIQUE(execution_id, attempt,
    seq); cross-attempt seq reuse legal; ACK attribution per attempt."""
    s0 = pg()
    t = _task(s0)
    ex = _exec(s0, t.id)
    s0.close()

    def add_event(s, attempt, seq, etype="tool_progress"):
        s.add(AutomationExecutionEvent(
            execution_id=ex.id, attempt=attempt, seq=seq,
            event_type=etype, payload={"i": seq},
        ))
        s.commit()

    s = pg()
    add_event(s, 0, 0)
    add_event(s, 0, 1)
    # Same-attempt replay of seq 1 → absorbed (IntegrityError = duplicate).
    with pytest.raises(IntegrityError):
        add_event(s, 0, 1, "tool_progress")
    s.rollback()
    # Cross-attempt seq reuse is legal (F-26).
    add_event(s, 1, 0)
    add_event(s, 1, 1)
    rows = (
        s.query(AutomationExecutionEvent)
        .filter_by(execution_id=ex.id)
        .order_by(AutomationExecutionEvent.attempt, AutomationExecutionEvent.seq)
        .all()
    )
    s.close()
    assert [(r.attempt, r.seq) for r in rows] == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_s2_7_restart_with_pending_occurrences(pg):
    """Application restart: pending occurrences survive, are re-claimable,
    and the UNIQUE arbiter still prevents double-claim across processes."""
    s0 = pg()
    t = _task(s0)
    key = "scheduled:2026-11-03T12:00:00"
    # A crash left a PENDING row (insert committed, claim CAS not reached).
    s0.add(AutomationOccurrence(
        automation_task_id=t.id, occurrence_key=key, scheduled_at=None,
        status="pending", org_id="default-org", app_id="default-app",
    ))
    s0.commit()
    s0.close()

    # "Restarted" process claims the pending leftover (CAS from pending).
    s = pg()
    occ = sc.claim_occurrence(
        s, automation_task_id=t.id, occurrence_key=key, scheduled_at=None,
    )
    assert occ is not None and occ.status == "claimed"
    # A second process cannot double-claim the same restarted slot.
    second = sc.claim_occurrence(
        s, automation_task_id=t.id, occurrence_key=key, scheduled_at=None,
    )
    s.close()
    assert second is None
