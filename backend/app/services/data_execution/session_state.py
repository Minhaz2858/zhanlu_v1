"""SessionState service: read/write per-session state for the session-cached re-export feature.

All methods are best-effort: on any DB error they log a warning, roll back,
and return a safe default (None / 0 / []). No exceptions propagate to callers.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution
from app.models.session_state import SessionState
from app.database import SessionLocal

logger = logging.getLogger(__name__)


class SessionStateService:
    """Read/write per-session state for the session-cached re-export feature."""

    # --- Read API ---------------------------------------------------------

    @staticmethod
    def get_last(db: Session, session_id: str) -> Optional[SessionState]:
        """Return the latest SessionState row for a session, or None."""
        try:
            return db.execute(
                select(SessionState)
                .where(SessionState.session_id == session_id)
                .limit(1)
            ).scalar_one_or_none()
        except Exception:
            logger.warning(
                "get_last failed for session=%s", session_id, exc_info=True
            )
            db.rollback()
            return None

    @staticmethod
    def get_history(db: Session, session_id: str, limit: int = 5) -> list[dict]:
        """Return recent data_execution rows for a session (newest first)."""
        try:
            rows = (
                db.execute(
                    select(DataExecution)
                    .where(
                        DataExecution.session_id == session_id,
                        DataExecution.is_deleted.is_(False),
                    )
                    .order_by(DataExecution.created_date.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "execution_id": r.id,
                    "tool_name": r.tool_name,
                    "summary_text": (r.summary_text or "")[:200],
                    "created_date": r.created_date,
                }
                for r in rows
            ]
        except Exception:
            logger.warning(
                "get_history failed for session=%s", session_id, exc_info=True
            )
            db.rollback()
            return []

    # --- Write API --------------------------------------------------------

    @staticmethod
    def set_last(
        db: Session,
        session_id: str,
        execution_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        data_signature: Optional[str] = None,
        org_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> Optional[SessionState]:
        """Update the session's cached-execution counter and last-ref metadata.

        Portable SQLite-safe upsert (no pg_insert/on_conflict_do_update):
        SELECT the existing row, then either UPDATE it or INSERT a new one.
        ``execution_id`` / ``tool_name`` / ``data_signature`` / ``org_id`` /
        ``app_id`` are optional metadata recorded on the row. On UPDATE, only
        kwargs that are not None overwrite existing columns, so the original
        ``set_last(db, session_id)`` call stays backwards compatible.
        Returns the row on success, None on error.
        """
        try:
            state = db.execute(
                select(SessionState)
                .where(SessionState.session_id == session_id)
                .with_for_update()
            ).scalar_one_or_none()
            if state is None:
                state = SessionState(
                    session_id=session_id,
                    execution_count=1,
                    last_execution_id=execution_id,
                    last_tool_name=tool_name,
                    last_data_signature=data_signature,
                    org_id=org_id,
                    app_id=app_id,
                )
                db.add(state)
            else:
                state.execution_count += 1
                if execution_id is not None:
                    state.last_execution_id = execution_id
                if tool_name is not None:
                    state.last_tool_name = tool_name
                if data_signature is not None:
                    state.last_data_signature = data_signature
                if org_id is not None:
                    state.org_id = org_id
                if app_id is not None:
                    state.app_id = app_id
            db.commit()
            db.refresh(state)
            return state
        except Exception:
            logger.warning(
                "set_last failed for session=%s", session_id, exc_info=True
            )
            db.rollback()
            return None
