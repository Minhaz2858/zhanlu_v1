"""Session-5 Phase 0 — critical fencing-ownership verification.

Brief requirements:
1. Worker A obtains token 10; worker B takes over with token 11; A resumes:
   - A cannot write with token 10.
   - A cannot obtain token 11 by a database lookup (current_token is
     ownership-scoped — it returns None for a lease it does not own).
   - B writes with token 11.
2. Production database errors must NOT trigger the legacy fallback — a
   real lease-backed execution that cannot establish ownership FAILS
   CLOSED (the error propagates; the unfenced CAS never runs).
"""
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
from app.models.automation_scheduling import ExecutionLease  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import runner_protocol as rp  # noqa: E402
from app.services.automation.scheduler_core import acquire_lease  # noqa: E402


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


def _run(db, status="running"):
    t = AutomationTask(name=f"OWN {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    db.add(t)
    db.commit()
    ex = AutomationExecution(automation_task_id=t.id, status=status, attempt=0)
    db.add(ex)
    db.commit()
    return ex


def test_token_10_worker_cannot_write_after_token_11_takeover(db):
    ex = _run(db)
    token10 = acquire_lease(db, ex.id, "worker-a")  # "token 10"
    assert token10
    # Worker A "pauses" — its lease expires.
    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    # Worker B takes over.
    token11 = acquire_lease(db, ex.id, "worker-b")
    assert token11 and token11 != token10

    # A resumes: cannot write with token 10 (stale). finalize_from_running
    # is the worker-owned primitive (fenced running→finalizing→terminal).
    assert rp.finalize_from_running(
        db, ex.id, token10, AutomationExecution, terminal_status="succeeded",
    ) is False
    # A's stale write never landed — the row is untouched for B.
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"
    # A cannot obtain token 11 through a database lookup — current_token is
    # OWNERSHIP-scoped, not "latest row"-scoped.
    assert rp.current_token(db, ex.id, "worker-a") is None
    assert rp.current_token(db, ex.id, "worker-b") == token11
    # B can write with token 11 (fenced two-step).
    assert rp.finalize_from_running(
        db, ex.id, token11, AutomationExecution, terminal_status="succeeded",
    ) is True
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "succeeded"


def test_current_token_never_returns_another_workers_token(db):
    """Property test: across takeover chains, a worker's lookup yields ONLY
    its own live token — never a newer one minted by someone else."""
    ex = _run(db)
    seen_tokens = []
    for i, owner in enumerate(["w0", "w1", "w2"]):
        if i > 0:
            lease = db.get(ExecutionLease, ex.id)
            lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        tok = acquire_lease(db, ex.id, owner)
        seen_tokens.append(tok)
    # w0's view: its token is long expired → None (never w2's token).
    assert rp.current_token(db, ex.id, "w0") is None
    # w2's view: its own live token only.
    assert rp.current_token(db, ex.id, "w2") == seen_tokens[2]
    assert len(set(seen_tokens)) == 3  # every takeover minted a fresh token


def test_db_error_fails_closed_no_legacy_fallback(db, monkeypatch):
    """A production database failure inside the fencing path must propagate
    — it must NEVER silently degrade the write to the unfenced legacy CAS."""
    import app.services.automation_executor as aexec
    from sqlalchemy.exc import OperationalError

    t = AutomationTask(name=f"FC {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    db.add(t)
    db.commit()
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    acquire_lease(db, ex.id, aexec._worker_id())

    def _boom(*a, **kw):
        raise OperationalError("SELECT 1", {}, "connection reset")

    monkeypatch.setattr(
        "app.services.automation.runner_protocol.current_token", _boom,
    )
    # _mark_failed_no_retry with a real DB error: the error propagates —
    # the row is NOT terminalized via the unfenced CAS.
    with pytest.raises(OperationalError):
        aexec._mark_failed_no_retry(db, ex, "should fail closed")
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"
