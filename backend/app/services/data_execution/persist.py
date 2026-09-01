"""Best-effort persistence for session-cached data executions.

``persist_data_execution`` writes one ``DataExecution`` row per tool call so
later turns in the same session can reuse the result without re-running the
query. It is deliberately fail-open: any database error is logged and the
function returns ``None`` — the caller (tool loop) must never crash because
the cache write failed.

Migrated from the module ``app/services/data_execution.py`` (Task 3 collision
fix): function renamed ``cache_data_execution`` → ``persist_data_execution``;
behavior unchanged.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import SessionLocal
from app.models.data_execution import DataExecution

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600


def persist_data_execution(
    session_id: str,
    tool_name: str,
    args: Optional[dict] = None,
    result: Optional[dict] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Optional[DataExecution]:
    """Persist a tool execution under an ``evt_`` id.

    Returns the persisted row, or ``None`` on any DB error (logged, never
    raised). ``expires_at`` = now + ``ttl_seconds``.
    """
    entry_id = "evt_" + uuid.uuid4().hex[:12]
    try:
        entry = DataExecution(
            id=entry_id,
            session_id=session_id,
            tool_name=tool_name,
            args=args or {},
            result=result or {},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )
        db = SessionLocal()
        try:
            db.add(entry)
            db.commit()
            db.refresh(entry)
            return entry
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — best-effort cache
        logger.warning(
            "persist_data_execution failed for session=%s tool=%s: %s",
            session_id,
            tool_name,
            exc,
        )
        return None
