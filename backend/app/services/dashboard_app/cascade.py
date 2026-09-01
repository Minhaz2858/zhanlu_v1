"""Cascade deletion of chat conversations bound to a dashboard app.

When a full-stack dashboard app is hard-deleted, the AgentConversation rows
whose ``metadata_`` bind them to that dashboard (``mode == "dashboard"`` plus a
matching ``dashboard_slug`` or ``dashboard_id``) should go too — otherwise the
chat UI shows conversations whose artifact no longer exists.

Matching is done in PYTHON (iterate all rows) rather than SQL so the same
helper works on SQLite test DBs and Postgres production without dialect
specifics. The function never raises: on any failure it logs the traceback and
returns 0 so the caller's delete flow can proceed.
"""

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def delete_bound_conversations(db: Session, dashboard_slug: str, dashboard_id: str) -> int:
    """Delete conversations bound to a dashboard app; return the count deleted.

    A conversation is bound when ``metadata_`` is a dict with
    ``mode == "dashboard"`` and either ``dashboard_slug == dashboard_slug`` or
    ``dashboard_id == dashboard_id``. NULL ``metadata_`` rows are skipped.
    Commits only when at least one row was deleted. Never raises — failures
    are logged and yield a return value of 0.
    """
    try:
        from app.models.agent_conversation import AgentConversation

        matched = [
            conv
            for conv in db.query(AgentConversation).all()
            if _is_bound(conv, dashboard_slug, dashboard_id)
        ]
        if not matched:
            return 0
        for conv in matched:
            db.delete(conv)
        db.commit()
        return len(matched)
    except Exception:
        logger.exception(
            "delete_bound_conversations failed (slug=%s id=%s)",
            dashboard_slug, dashboard_id,
        )
        return 0


def _is_bound(conv, dashboard_slug: str, dashboard_id: str) -> bool:
    meta = conv.metadata_ or {}
    if not isinstance(meta, dict):
        # A malformed metadata_ row (JSON list/string instead of a dict) must
        # not crash the whole cascade — skip it so other matching rows still
        # get deleted instead of the top-level try/except aborting everything.
        return False
    if meta.get("mode") != "dashboard":
        return False
    return (
        meta.get("dashboard_slug") == dashboard_slug
        or meta.get("dashboard_id") == dashboard_id
    )
