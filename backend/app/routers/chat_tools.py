"""Chat tools router — global chat-history search.

GET /api/chat/search?q=...&limit=20 — user-scoped ILIKE across
chat_messages joined to chat_sessions. Returns the caller's sessions
whose messages contain the query, grouped by session with a bounded
snippet around the first match.

Scope rule (multi-tenant safe): only sessions with
``created_by_id == caller`` and ``is_deleted == False`` are searchable.
Messages carry no owner column — ownership resolves through the session.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.models.chat_session import ChatSession
from app.models.chat_message import ChatMessage

router = APIRouter(prefix="/chat", tags=["chat-tools"])

_SNIPPET_RADIUS = 60    # chars before/after the first match
_SNIPPET_CAP = 160      # hard cap on the returned snippet


def _escape_like(q: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _make_snippet(content: str, needle_lower: str) -> str:
    """Bounded snippet centered on the first case-insensitive match."""
    idx = content.lower().find(needle_lower)
    if idx < 0:
        return content[:_SNIPPET_CAP]
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(content), idx + len(needle_lower) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}"[:_SNIPPET_CAP + 2]


@router.get("/search")
async def search_chat_history(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Search the caller's chat history by message content."""
    needle = q.strip()
    if not needle:
        raise HTTPException(status_code=400, detail="query q must not be blank")

    pattern = f"%{_escape_like(needle)}%"
    needle_lower = needle.lower()

    rows = (
        db.query(
            ChatSession.id,
            ChatSession.title,
            ChatSession.agent_name,
            ChatSession.last_message_at,
            ChatMessage.role,
            ChatMessage.content,
            ChatMessage.created_date,
        )
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .filter(
            ChatSession.created_by_id == user.id,
            ChatSession.is_deleted.is_(False),
            ChatMessage.content.ilike(pattern, escape="\\"),
        )
        .order_by(ChatMessage.created_date.desc())
        .limit(limit * 20)
        .all()
    )

    results = []
    seen = set()
    for sid, title, agent_name, last_message_at, role, content, created_date in rows:
        if sid in seen:
            continue
        seen.add(sid)
        results.append(
            {
                "session_id": sid,
                "title": title,
                "agent_name": agent_name,
                "last_message_at": last_message_at,
                "matches": [
                    {
                        "role": role,
                        "snippet": _make_snippet(content, needle_lower),
                        "created_date": created_date.isoformat() if created_date else None,
                    }
                ],
            }
        )
        if len(results) >= limit:
            break

    return {"query": needle, "results": results}
