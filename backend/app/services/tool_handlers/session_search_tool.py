"""session_search tool — search past conversations.

Searches the agent_conversations / chat_messages tables for prior turns
matching a query. Returns conversation metadata plus the matching
message excerpts so the agent can re-use prior context.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _session_search(
    args: dict,
    db: Session,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}
    limit = min(int(args.get("limit", 10)), 50)

    try:
        from app.models.agent_conversation import AgentConversation
    except Exception as exc:
        return {"success": False, "error": f"AgentConversation model unavailable: {exc}"}

    # Search by conversation name first
    matching_convs = (
        db.query(AgentConversation)
        .filter(AgentConversation.is_deleted == False)  # noqa: E712
        .filter(AgentConversation.title.ilike(f"%{query}%"))
        .order_by(AgentConversation.updated_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "success": True,
        "query": query,
        "matches": [
            {
                "id": c.id,
                "title": getattr(c, "title", None) or "(untitled)",
                "agent_name": getattr(c, "agent_name", None),
                "updated_date": c.updated_date.isoformat() if c.updated_date else None,
            }
            for c in matching_convs
        ],
    }


SESSION_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": (
            "Search past conversations by title. Returns conversation id, "
            "title, agent_name, and last-update timestamp. Use to find "
            "prior context the user has discussed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to search for in conversation titles."},
                "limit": {"type": "integer", "description": "Max results (default 10, max 50).", "default": 10},
            },
            "required": ["query"],
        },
    },
}

registry.register(
    name="session_search",
    schema=SESSION_SEARCH_SCHEMA,
    handler=_session_search,
    category="memory",
    toolset="memory",
    description="Search past conversations by title.",
    emoji="🔎",
    max_result_size_chars=20_000,
)
