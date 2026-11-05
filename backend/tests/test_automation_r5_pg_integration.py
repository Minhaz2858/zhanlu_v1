"""R5 — REAL PostgreSQL integration: occurrence-claim and lease concurrency.

These tests prove the PostgreSQL-only guarantees that SQLite cannot fake:
row-lock serialization, unique-arbiter races across independent sessions,
and fencing-token ordering under takeover. They run against a DISPOSABLE
database (``automation_r5_test`` on the local zhanlu-postgres stack) —
never against application data. Skipped entirely when that database is
unreachable, and NEVER substituted with SQLite equivalents.
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

PG_DSN = os.environ.get(
    "AUTOMATION_R5_PG_DSN",
    "postgresql+psycopg2://zhanlu:zhanlu123@localhost:5400/automation_r5_test",
)

from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import AutomationOccurrence  # noqa: E402


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
def pg_engine():
    from app.database import Base
    import app.models  # noqa: F401 — register all models

    eng = create_engine(PG_DSN, pool_pre_ping=True, pool_size=10, max_overflow=10)
    Base.metadata.create_all(eng)
    yield eng
    # Raw schema reset — drop_all chokes on nameless use_alter FKs.
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.commit()
    eng.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


def _task(s):
    from app.models.automation_task import AutomationTask

    t = AutomationTask(name=f"R5PG {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="active")
    s.add(t)
    s.commit()
    return t


def _exec(s, task_id, status="running"):
    from app.models.automation_execution import AutomationExecution

    ex = AutomationExecution(automation_task_id=task_id, status=status, attempt=0)
    s.add(ex)
    s.commit()
    return ex


def test_concurrent_occurrence_claim_single_winner(pg_session_factory):
    """Two independent workers race for the same occurrence key — the
    UNIQUE arbiter + claim CAS yield EXACTLY ONE winner (§6.1)."""
    from app.services.automation.scheduler_core import claim_occurrence

    s0 = pg_session_factory()
    t = _task(s0)
    s0.close()
    key = f"scheduled:{datetime.utcnow().replace(microsecond=0).isoformat()}"

    results = []
    barrier__ready = []

    def worker(i: int):
        s = pg_session_factory()
        try:
            occ = claim_occurrence(
                s, automation_task_id=t.id, occurrence_key=key, scheduled_at=None,
            )
            results.append((i, occ.id if occ is not None else None))
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    winners = [i for i, occ_id in results if occ_id is not None]
    assert len(winners) == 1, f"expected exactly one claim winner, got {results}"
    losers = [r for r in results if r[1] is None]
    assert len(losers) == 5

    s = pg_session_factory()
    count = (
        s.query(AutomationOccurrence)
        .filter_by(automation_task_id=t.id, occurrence_key=key)
        .count()
    )
    s.close()
    assert count == 1


def test_fencing_token_ordering_under_takeover(pg_session_factory):
    """Worker A's lease expires; worker B takes over with a fresh token;
    A's terminal write MUST lose the fencing check even though it arrives
    after B's acquisition (§6.2b duplicate-finalization impossibility)."""
    import time

    from app.services.automation.scheduler_core import (
        acquire_lease,
        fenced_status_write,
    )

    s0 = pg_session_factory()
    t = _task(s0)
    ex = _exec(s0, t.id)
    s0.close()

    sa = pg_session_factory()
    token_a = acquire_lease(sa, ex.id, "worker-a", ttl_seconds=1)
    sa.close()

    time.sleep(1.2)  # lease A expires

    sb = pg_session_factory()
    token_b = acquire_lease(sb, ex.id, "worker-b")
    assert token_b is not None and token_b != token_a
    sb.close()

    sc = pg_session_factory()
    # A (stale token) tries to finalize → rejected.
    a_won = fenced_status_write(
        sc, ex.id, token_a, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    )
    # B (live token) finalizes → accepted.
    b_won = fenced_status_write(
        sc, ex.id, token_b, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    )
    sc.close()
    assert a_won is False, "stale token must never win the fenced write"
    assert b_won is True

    s = pg_session_factory()
    final = s.query(AutomationExecution).get(ex.id).status
    s.close()
    assert final == "succeeded"


def test_lease_renewal_is_token_scoped(pg_session_factory):
    from app.services.automation.scheduler_core import acquire_lease, renew_lease

    s0 = pg_session_factory()
    t = _task(s0)
    ex = _exec(s0, t.id)
    s0.close()

    sa = pg_session_factory()
    token_a = acquire_lease(sa, ex.id, "worker-a")
    sa.close()

    sb = pg_session_factory()
    # A stale/foreign token must not renew someone else's lease.
    assert renew_lease(sb, ex.id, "forged-token") is False
    assert renew_lease(sb, ex.id, token_a) is True
    sb.close()


def test_double_exec_attempt_unique_arbiter(pg_session_factory):
    """UNIQUE(occurrence_id, attempt): two attempts at the same attempt
    number cannot both exist (crash-replay safety)."""
    from sqlalchemy.exc import IntegrityError

    s0 = pg_session_factory()
    t = _task(s0)
    ex1 = _exec(s0, t.id)
    ex1.occurrence_id = "occ-pg-1"
    s1 = pg_session_factory()
    ex2 = AutomationExecution(
        automation_task_id=t.id, status="queued", attempt=0, occurrence_id="occ-pg-1",
    )
    s1.add(ex2)
    with pytest.raises(IntegrityError):
        s1.commit()
    s1.rollback()
    s0.close()
    s1.close()
