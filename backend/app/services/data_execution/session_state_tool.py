"""session_state_query tool — let the LLM inspect a session's cached executions.

Sibling of ``prompt_block.build_session_state_block``: the block handles the
"latest execution" case for re-export, this tool lets the LLM enumerate recent
executions (newest first) and pick the right ``execution_id`` to pass to
``create_artifact(source_execution_id=...)``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.data_execution.session_state import SessionStateService

SESSION_STATE_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_state_query",
        "description": (
            "Return the recent data tool executions in this session, "
            "newest first. Use this when the user wants to re-export "
            "or re-format a SPECIFIC previous analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max number of recent executions to return.",
                },
            },
        },
    },
}


def session_state_query(
    db: Session,
    *,
    session_id: str,
    limit: int = 10,
) -> list[dict]:
    """Return recent data_execution rows for the session (newest first).

    ``session_id`` is intentionally NOT part of the tool schema: it is injected
    by the dispatcher from the conversation context, mirroring how
    ``create_artifact`` receives conversation_id / execution_id.
    """
    return SessionStateService.get_history(db, session_id=session_id, limit=limit)
