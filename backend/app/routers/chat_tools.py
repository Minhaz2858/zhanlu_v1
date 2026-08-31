"""Chat tools router — global chat-history search + conversation sharing.

GET /api/chat/search?q=...&limit=20 — user-scoped ILIKE across
chat_messages joined to chat_sessions. Returns the caller's sessions
whose messages contain the query, grouped by session with a bounded
snippet around the first match.

POST/DELETE /api/chat/shares — Kimi/GPT-style token sharing of a chat
session. The read-only public page lives at /share/c/<token> (no auth)
and is served by ``public_router`` (registered WITHOUT the /api prefix
in main.py so the URL is short and shareable).

Scope rule (multi-tenant safe): only sessions with
``created_by_id == caller`` and ``is_deleted == False`` are searchable.
Messages carry no owner column — ownership resolves through the session.
"""

import html
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.models.chat_session import ChatSession
from app.models.chat_message import ChatMessage
from app.models.chat_share import ChatShare

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


# ── Conversation sharing (Kimi/GPT-style) ─────────────────────────────

def _share_owner_guard(db: Session, user: User, session_id: str) -> ChatSession:
    """Return the session if the caller owns it, else 403/404."""
    session = (
        db.query(ChatSession)
        .filter(
            ChatSession.id == session_id,
            ChatSession.is_deleted.is_(False),
        )
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="You can only share your own conversations")
    return session


@router.post("/shares")
async def create_share(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create (or reuse) a public share token for one of the caller's sessions."""
    session_id = (body or {}).get("session_id")
    if not session_id:
        raise HTTPException(status_code=422, detail="session_id is required")
    _share_owner_guard(db, user, session_id)

    now = datetime.now(timezone.utc)
    existing = (
        db.query(ChatShare)
        .filter(
            ChatShare.session_id == session_id,
            ChatShare.created_by_id == user.id,
            ChatShare.is_deleted.is_(False),
        )
        .first()
    )
    if existing is not None:
        if existing.expires_at is not None and existing.expires_at < now:
            db.delete(existing)
            db.commit()
        else:
            return {
                "token": existing.token,
                "share_url": f"/share/c/{existing.token}",
                "created_date": existing.created_date.isoformat() if existing.created_date else None,
                "reused": True,
            }

    share = ChatShare(
        session_id=session_id,
        token=uuid.uuid4().hex,
        created_by_id=user.id,
        created_date=now,
        updated_date=now,
        org_id=getattr(user, "org_id", "default-org") or "default-org",
        app_id=getattr(user, "app_id", "default-app") or "default-app",
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return {
        "token": share.token,
        "share_url": f"/share/c/{share.token}",
        "created_date": share.created_date.isoformat() if share.created_date else None,
        "reused": False,
    }


@router.delete("/shares/{session_id}")
async def revoke_share(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Revoke (hard-delete) the share for one of the caller's sessions."""
    _share_owner_guard(db, user, session_id)
    share = (
        db.query(ChatShare)
        .filter(
            ChatShare.session_id == session_id,
            ChatShare.created_by_id == user.id,
            ChatShare.is_deleted.is_(False),
        )
        .first()
    )
    if share is not None:
        db.delete(share)
        db.commit()
    return {"success": True}


# ── Public read-only share page + data (NO auth) ──────────────────────

public_router = APIRouter(tags=["chat-shares-public"])


def _resolve_share(db: Session, token: str) -> ChatShare:
    share = (
        db.query(ChatShare)
        .filter(ChatShare.token == token, ChatShare.is_deleted.is_(False))
        .first()
    )
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    if share.expires_at is not None and share.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=404, detail="Share expired")
    return share


@public_router.get("/share/c/{token}/data")
async def share_data(token: str, db: Session = Depends(get_db)):
    """Public JSON payload for a shared conversation (no auth)."""
    share = _resolve_share(db, token)
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == share.session_id, ChatSession.is_deleted.is_(False))
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_date.asc(), ChatMessage.order.asc())
        .all()
    )
    return {
        "session_title": session.title,
        "agent_name": session.agent_name,
        "created_date": session.created_date.isoformat() if session.created_date else None,
        "messages": [
            {
                "role": m.role,
                "content": m.content or "",
                "created_date": m.created_date.isoformat() if m.created_date else None,
            }
            for m in messages
        ],
    }


@public_router.get("/share/c/{token}", response_class=HTMLResponse)
async def share_page(token: str, db: Session = Depends(get_db)):
    """Read-only HTML page for a shared conversation (no auth)."""
    share = _resolve_share(db, token)
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == share.session_id, ChatSession.is_deleted.is_(False))
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_date.asc(), ChatMessage.order.asc())
        .all()
    )

    def _bubble(role: str) -> str:
        if role == "user":
            return "align-self:flex-end;background:#2563eb;color:#fff;border-radius:14px 14px 4px 14px;"
        return "align-self:flex-start;background:#1e293b;color:#e2e8f0;border-radius:14px 14px 14px 4px;"

    def _label(role: str) -> str:
        return {"user": "You", "assistant": "Agent", "system": "System", "tool": "Tool"}.get(role, role)

    bubbles = "\n".join(
        f'<div style="display:flex;flex-direction:column;max-width:78%;{_bubble(m.role)};padding:10px 14px;margin:8px 0;white-space:pre-wrap;word-break:break-word;">'
        f'<div style="font-size:11px;opacity:.6;margin-bottom:4px;">{html.escape(_label(m.role))}</div>'
        f'{html.escape(m.content or "")}'
        f"</div>"
        for m in messages
    )

    title = html.escape(session.title or "Shared Conversation")
    agent = html.escape(session.agent_name or "")
    created = session.created_date.isoformat() if session.created_date else ""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
  body {{ margin:0; background:#0b1220; color:#e2e8f0; font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  .meta {{ font-size:12px; color:#94a3b8; margin-bottom:24px; }}
  .chat {{ display:flex; flex-direction:column; }}
  .footer {{ margin-top:32px; font-size:11px; color:#475569; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <div class="meta">{('Agent: ' + agent + ' · ') if agent else ''}{created}</div>
  <div class="chat">{bubbles}</div>
  <div class="footer">Shared via Zhanlu</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=page)
