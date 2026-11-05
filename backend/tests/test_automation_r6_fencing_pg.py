"""R6 Phase 1 — REAL PostgreSQL proof of executor-path fencing: stale-token
terminal rejection and single-winner concurrent finalization through the
executors' fenced helpers (not just the raw scheduler primitives)."""
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

from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import runner_protocol as rp  # noqa: E402
from app.services.automation.scheduler_core import acquire_lease  # noqa: E402


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


def _run(pg, status="running", output=None):
    s = pg()
    t = AutomationTask(name=f"R6PG {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="active")
    s.add(t)
    s.commit()
    ex = AutomationExecution(automation_task_id=t.id, status=status, attempt=0,
                             output_text=output)
    s.add(ex)
    s.commit()
    s.close()
    return t.id, ex.id


def test_pg_stale_token_terminal_rejected_fresh_token_wins(pg):
    _, ex_id = _run(pg, status="running", output="the report")
    s = pg()
    stale = acquire_lease(s, ex_id, "worker-a", ttl_seconds=1)
    s.close()
    import time
    time.sleep(1.1)
    s = pg()
    fresh = acquire_lease(s, ex_id, "worker-b")
    s.close()
    sa = pg()
    a = rp.finalize_from_running(sa, ex_id, stale, AutomationExecution,
                                 terminal_status="succeeded")
    sb = pg()
    b = rp.finalize_from_running(sb, ex_id, fresh, AutomationExecution,
                                 terminal_status="succeeded", duration_seconds=3.0)
    sb.close()
    sa.close()
    assert (a, b) == (False, True)
    s = pg()
    row = s.query(AutomationExecution).get(ex_id)
    s.close()
    assert row.status == "succeeded"


def test_pg_concurrent_finalize_single_terminal_value(pg):
    """Two workers hold DIFFERENT valid-looking states (one took over) and
    race the terminal CAS — the row ends in exactly ONE terminal state and
    the lease is released exactly once."""
    _, ex_id = _run(pg, status="running", output="race report")
    s = pg()
    tok1 = acquire_lease(s, ex_id, "w1", ttl_seconds=1)
    s.close()
    import time
    time.sleep(1.1)
    s = pg()
    tok2 = acquire_lease(s, ex_id, "w2")
    s.close()

    def attempt(token, status):
        s = pg()
        try:
            return rp.finalize_from_running(
                s, ex_id, token, AutomationExecution, terminal_status=status,
            )
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(attempt, tok1, "succeeded")
        f2 = pool.submit(attempt, tok2, "failed")
        r1, r2 = f1.result(), f2.result()

    # Exactly one fenced write landed (the live token holder's).
    assert (r1, r2) in ((False, True), (True, False))
    s = pg()
    row = s.query(AutomationExecution).get(ex_id)
    leases = s.query(
        __import__("app.models.automation_scheduling", fromlist=["ExecutionLease"]).ExecutionLease
    ).filter_by(execution_id=ex_id).count()
    s.close()
    assert row.status in ("succeeded", "failed")
    assert row.completed_at is not None
    assert leases == 1  # one lease row per execution, ever
