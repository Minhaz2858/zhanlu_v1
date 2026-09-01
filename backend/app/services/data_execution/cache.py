"""Auto-cache for tool executions in the session-cached re-export feature.

``cache_data_execution`` is the async wrapper the tool loop calls after a
cacheable tool (see ``CACHEABLE_TOOL_PATTERNS``) produces data. It persists
one ``DataExecution`` row via ``persist_data_execution``'s model and records
the last-ref metadata on the session via ``SessionStateService.set_last``.

It is deliberately best-effort / fail-open: any DB error is logged and
``None`` is returned — the tool loop must never crash because caching failed.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution
from app.services.data_execution.session_state import SessionStateService

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7

CACHEABLE_TOOL_PATTERNS = frozenset(
    {
        "ask_data_agent",
        "ask_erp_kpi",
        "ask_perception",
        "ask_forecast",
        "ask_pricing",
        "ask_decision",
        "query_composer",
        "fetch_data_batch",
        "execute_query",
        "ask_intelligence",
        "ask_rag_research",
    }
)


def _compute_data_signature(tool_name: str, result: dict) -> str:
    """Compute a short deterministic signature of the tool output.

    Based on the tool name, the number of rows, and the first row's sorted
    column keys — stable across repeated runs of the same query, so a later
    turn can detect "same data as last time" without deep comparison.
    """
    try:
        rows = result.get("rows") or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            keys = sorted(rows[0].keys())
        else:
            keys = sorted((result or {}).keys())
        payload = f"{tool_name}|{len(rows)}|{','.join(keys)}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


async def cache_data_execution(
    *,
    db: Session,
    session_id: str,
    tool_name: str,
    args: Optional[dict],
    result: Optional[dict],
    summary_text: Optional[str],
    org_id: Optional[str],
    app_id: Optional[str],
) -> Optional[str]:
    """Persist a cacheable tool execution; return the ``evt_`` row id or None.

    Fail-open: returns ``None`` for non-cacheable tools, empty sessions, or
    any DB error. Never raises.
    """
    if tool_name not in CACHEABLE_TOOL_PATTERNS:
        return None
    if not session_id:
        return None

    data_signature = _compute_data_signature(tool_name, result or {})

    try:
        row = DataExecution(
            id="evt_" + uuid.uuid4().hex[:12],
            session_id=session_id,
            tool_name=tool_name,
            args=args or {},
            result=result or {},
            summary_text=summary_text,
            org_id=org_id,
            app_id=app_id,
            is_deleted=False,
            expires_at=datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS),
        )
        db.add(row)
        db.commit()

        SessionStateService.set_last(
            db,
            session_id=session_id,
            execution_id=row.id,
            tool_name=tool_name,
            data_signature=data_signature,
            org_id=org_id,
            app_id=app_id,
        )
        return row.id
    except Exception as exc:  # noqa: BLE001 — best-effort cache
        logger.warning(
            "cache_data_execution failed for session=%s tool=%s: %s",
            session_id,
            tool_name,
            exc,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
