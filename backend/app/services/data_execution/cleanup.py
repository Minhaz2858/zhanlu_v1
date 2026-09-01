"""Cleanup service for DataExecution rows — TTL sweep + per-session cap.

All public functions are best-effort: they never raise, roll back on failure,
and return the number of rows deleted (0 on error). The per-session cap uses
portable ORM queries (no ROW_NUMBER / window-function SQL) so it works on both
Postgres and the in-memory SQLite used by tests.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution

logger = logging.getLogger(__name__)

PER_SESSION_CAP = 20
SWEEP_INTERVAL_S = 3600


def cleanup_expired_data_executions(db: Session) -> int:
    """Hard-delete non-deleted DataExecution rows whose TTL has passed."""
    try:
        now = datetime.now(timezone.utc)
        expired = (
            db.query(DataExecution)
            .filter(
                DataExecution.is_deleted == False,  # noqa: E712
                DataExecution.expires_at < now,
            )
            .all()
        )
        count = len(expired)
        if count:
            for row in expired:
                db.delete(row)
            db.commit()
            logger.info("data_executions cleanup: %d rows deleted", count)
        return count
    except Exception as exc:
        logger.warning("data_executions cleanup failed: %s", exc)
        db.rollback()
        return 0


def enforce_per_session_cap(db: Session) -> int:
    """Delete oldest rows beyond PER_SESSION_CAP per session_id (portable)."""
    try:
        rows = (
            db.query(DataExecution)
            .filter(DataExecution.is_deleted == False)  # noqa: E712
            .order_by(DataExecution.session_id, DataExecution.created_date.desc())
            .all()
        )
        deleted = 0
        rows_by_session: dict[str, list[DataExecution]] = {}
        for row in rows:
            rows_by_session.setdefault(row.session_id, []).append(row)
        for bucket in rows_by_session.values():
            for row in bucket[PER_SESSION_CAP:]:
                db.delete(row)
                deleted += 1
        if deleted:
            db.commit()
            logger.info("data_executions cap: %d rows deleted", deleted)
        return deleted
    except Exception as exc:
        logger.warning("data_executions cap failed: %s", exc)
        db.rollback()
        return 0


async def scheduled_cleanup_loop() -> None:
    """Background loop: TTL sweep + per-session cap, one fresh session each tick.

    Not wired into app startup (that is the Task 18 wiring step). Imported
    lazily so importing this module never depends on the database engine.
    """
    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                cleanup_expired_data_executions(db)
                enforce_per_session_cap(db)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("scheduled cleanup loop error: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_S)
