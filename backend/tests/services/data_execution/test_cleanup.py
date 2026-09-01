"""Tests for the DataExecution cleanup service (TTL sweep + per-session cap).

Runs against the shared in-memory SQLite DB from ``tests/conftest.py``
(StaticPool). ``_ensure_table()`` creates just the ``data_executions`` table
(repo convention — see test_cache.py). The shared DB persists across tests, so
each test seeds its rows with a unique session_id.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.database import engine  # noqa: E402
from app.models.data_execution import DataExecution  # noqa: E402
from app.services.data_execution.cleanup import (  # noqa: E402
    PER_SESSION_CAP,
    cleanup_expired_data_executions,
    enforce_per_session_cap,
)


def _ensure_table():
    """Create just the data_executions table (idempotent)."""
    DataExecution.__table__.create(bind=engine, checkfirst=True)


def _seed(db, session_id, count, *, id_prefix="evt_seed", expired=False):
    """Seed ``count`` DataExecution rows with explicit created_date ordering."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        created = now + timedelta(seconds=i)
        expires = (
            now - timedelta(days=1)
            if expired
            else now + timedelta(days=7)
        )
        db.add(
            DataExecution(
                id=f"{id_prefix}_{session_id[-8:]}_{i:03d}",
                session_id=session_id,
                tool_name="ask_data_agent",
                args={},
                result={"rows": []},
                expires_at=expires,
                org_id="org1",
                app_id="default-app",
                is_deleted=False,
                created_date=created,
            )
        )
    db.commit()


def test_cleanup_deletes_expired_rows():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed(db, "cleanup-expired-1", 3, id_prefix="evt_exp", expired=True)
        _seed(db, "cleanup-expired-1", 2, id_prefix="evt_fresh")
        deleted = cleanup_expired_data_executions(db)
        assert deleted >= 3
        remaining = db.query(DataExecution).filter_by(
            session_id="cleanup-expired-1"
        ).all()
        assert len(remaining) == 2
        # SQLite round-trips drop tzinfo; use the model's own normalization.
        assert all(not r.is_expired() for r in remaining)
    finally:
        db.rollback()
        db.close()


def test_cap_enforced():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        session_id = "test-cap-1"
        _seed(db, session_id, PER_SESSION_CAP + 5, id_prefix="evt_cap")
        deleted = enforce_per_session_cap(db)
        assert deleted >= 5
        count = (
            db.query(DataExecution)
            .filter(DataExecution.session_id == session_id)
            .count()
        )
        assert count == PER_SESSION_CAP
    finally:
        db.rollback()
        db.close()


def test_cleanup_keeps_non_expired_rows():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed(db, "cleanup-fresh-1", 2, id_prefix="evt_fresh2")
        deleted = cleanup_expired_data_executions(db)
        assert deleted == 0
        count = (
            db.query(DataExecution)
            .filter(DataExecution.session_id == "cleanup-fresh-1")
            .count()
        )
        assert count == 2
    finally:
        db.rollback()
        db.close()


def test_cap_multiple_sessions():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed(db, "cap-sess-a", PER_SESSION_CAP + 3, id_prefix="evt_a")
        _seed(db, "cap-sess-b", PER_SESSION_CAP + 7, id_prefix="evt_b")
        deleted = enforce_per_session_cap(db)
        assert deleted >= 10
        count_a = (
            db.query(DataExecution)
            .filter(DataExecution.session_id == "cap-sess-a")
            .count()
        )
        count_b = (
            db.query(DataExecution)
            .filter(DataExecution.session_id == "cap-sess-b")
            .count()
        )
        assert count_a == PER_SESSION_CAP
        assert count_b == PER_SESSION_CAP
    finally:
        db.rollback()
        db.close()
