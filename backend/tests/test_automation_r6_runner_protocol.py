"""R6 — runner protocol mechanics (TS §6.2/§6.2b): fenced claim, output-
before-finalizing ordering, idempotent terminalization, duplicate-
finalization impossibility, finalize-replay."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import runner_protocol as rp  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _task(db):
    t = AutomationTask(name=f"R6 {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    db.add(t)
    db.commit()
    return t


def _queued(db, task):
    ex = AutomationExecution(automation_task_id=task.id, status="queued", attempt=0)
    db.add(ex)
    db.commit()
    return ex


def test_claim_running_mints_token_and_sets_running(db):
    t = _task(db)
    ex = _queued(db, t)
    token = rp.claim_running(db, ex.id, "w1", AutomationExecution)
    assert token
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"


def test_claim_running_refused_when_lease_held_elsewhere(db):
    t = _task(db)
    ex = _queued(db, t)
    assert rp.claim_running(db, ex.id, "w1", AutomationExecution)
    assert rp.claim_running(db, ex.id, "w2", AutomationExecution) is None


def test_output_persisted_before_finalizing_cas(db):
    t = _task(db)
    ex = _queued(db, t)
    token = rp.claim_running(db, ex.id, "w1", AutomationExecution)
    ok = rp.persist_output_and_enter_finalizing(
        db, ex.id, token, AutomationExecution,
        output_text="the report", output_data={"k": 1},
    )
    assert ok is True
    db.expire_all()
    row = db.query(AutomationExecution).get(ex.id)
    assert row.status == "finalizing"
    assert row.output_text == "the report"


def test_stale_token_cannot_enter_finalizing(db):
    t = _task(db)
    ex = _queued(db, t)
    token = rp.claim_running(db, ex.id, "w1", AutomationExecution)
    # Take over the lease (worker-a died) with a fresh token.
    from app.services.automation.scheduler_core import acquire_lease
    from app.models.automation_scheduling import ExecutionLease

    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    new_token = acquire_lease(db, ex.id, "w2")
    ok = rp.persist_output_and_enter_finalizing(
        db, ex.id, token, AutomationExecution, output_text="stale write",
    )
    assert ok is False
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"


def test_terminalization_and_immutability(db):
    t = _task(db)
    ex = _queued(db, t)
    token = rp.claim_running(db, ex.id, "w1", AutomationExecution)
    rp.persist_output_and_enter_finalizing(
        db, ex.id, token, AutomationExecution, output_text="out",
    )
    assert rp.finalize_terminal(
        db, ex.id, token, AutomationExecution, terminal_status="succeeded",
        duration_seconds=12.5,
    ) is True
    db.expire_all()
    row = db.query(AutomationExecution).get(ex.id)
    assert row.status == "succeeded"
    # No path re-opens a terminal execution: a second terminal CAS is a no-op.
    assert rp.finalize_terminal(
        db, ex.id, token, AutomationExecution, terminal_status="failed",
    ) is False
    assert db.query(AutomationExecution).get(ex.id).status == "succeeded"


def test_duplicate_finalization_exactly_one_winner(db):
    """Two 'recovering workers' race the finalizing→terminal CAS with the
    same token taken over at different times — fencing lets exactly one
    land; the loser observes the terminal state unchanged."""
    t = _task(db)
    ex = _queued(db, t)
    token0 = rp.claim_running(db, ex.id, "w1", AutomationExecution)
    rp.persist_output_and_enter_finalizing(
        db, ex.id, token0, AutomationExecution, output_text="out",
    )
    from app.services.automation.scheduler_core import acquire_lease, release_lease
    from app.models.automation_scheduling import ExecutionLease

    # Expire the original lease; recoverer-a takes over first.
    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    tok_a = acquire_lease(db, ex.id, "recoverer-a")
    assert tok_a
    # Recoverer-b CANNOT take over while a's lease is live — only one
    # fenced actor exists at a time (duplicate finalization is structurally
    # impossible: b never even gets a token).
    assert acquire_lease(db, ex.id, "recoverer-b") is None
    a = rp.finalize_terminal(db, ex.id, tok_a, AutomationExecution,
                             terminal_status="succeeded")
    assert a is True
    db.expire_all()
    row = db.query(AutomationExecution).get(ex.id)
    assert row.status == "succeeded"
    # b's late terminal write attempt with the (now invalid) original
    # runner token is rejected — terminal immutability.
    assert rp.finalize_terminal(db, ex.id, token0, AutomationExecution,
                                terminal_status="failed") is False
    assert db.query(AutomationExecution).get(ex.id).status == "succeeded"


def test_finalize_replay_requires_persisted_output(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="finalizing", attempt=0)
    db.add(ex)
    db.commit()
    # No output → protocol violation → replay refuses (caller handles retry).
    assert rp.finalize_replay(db, ex, AutomationExecution) is False
