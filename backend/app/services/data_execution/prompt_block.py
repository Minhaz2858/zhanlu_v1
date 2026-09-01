"""build_session_state_block — inject session state into the LLM system prompt."""
from __future__ import annotations
from typing import Optional

from sqlalchemy.orm import Session

from app.services.data_execution.session_state import SessionStateService


def build_session_state_block(db: Session, *, session_id: str) -> Optional[str]:
    state = SessionStateService.get_last(db, session_id=session_id)
    if not state or not state.last_execution_id:
        return None
    return (
        "\n\nSESSION STATE — last data tool execution in this conversation:\n"
        f"  execution_id: {state.last_execution_id}\n"
        f"  tool_name: {state.last_tool_name}\n"
        f"  data_signature: {state.last_data_signature or ''}\n"
        "  When the user asks to re-export or re-format a previous analysis,\n"
        "  pass this execution_id to create_artifact(source_execution_id=...)\n"
        "  instead of re-running data tools."
    )
