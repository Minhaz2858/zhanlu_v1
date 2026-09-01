"""FastAPI dependency injection: database sessions and current user extraction."""

from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.user import User
from app.services.auth_service import auth_service


def get_db():
    """Yield a database session and ensure it's closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@dataclass
class AnonymousIdentity:
    """Lightweight stand-in for ``User`` when no auth token is present.

    .. deprecated:: 2026-07-27
        Anonymous identities are no longer supported — login is required for
        everything (plan 2026-07-27). This class is kept defined so existing
        imports do not break, but :func:`get_current_user_optional` never
        returns it anymore (it returns ``None`` instead).

    Returned by :func:`get_current_user_optional` so the caller can stamp
    ``created_by_id`` on rows it creates (e.g. a user clicking the
    ``+`` button on the Skills marketplace while logged out) without
    breaking the existing ``user.id if user else None`` access pattern.

    The ``id`` is a per-browser UUID sent in the ``X-Anonymous-Id`` header
    by the frontend, so each browser tab has its own stable namespace
    even without a real account.
    """

    id: str
    is_anonymous: bool = True


def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    x_base44_anonymous_id: Optional[str] = Header(None, alias="X-Base44-Anonymous-Id"),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Extract the authenticated user from the ``Authorization`` header.

    Returns a real ``User`` when a valid ``Authorization: Bearer <token>``
    header is present, otherwise ``None``.

    Anonymous identities are **no longer supported** — login is required for
    everything (plan 2026-07-27). The ``x_base44_anonymous_id`` parameter is
    retained in the signature for backward compatibility with existing
    callers/FastAPI route declarations but is intentionally ignored: any
    ``X-Base44-Anonymous-Id`` header sent by the client is discarded.

    The ``AnonymousIdentity`` class is kept defined (marked deprecated) so
    existing imports do not break, but it is never returned here.
    """
    if not (authorization and authorization.startswith("Bearer ")):
        return None
    token = authorization[7:]
    user_id = auth_service.verify_token(token, db)
    if not user_id:
        return None
    return db.query(User).filter(
        User.id == user_id, User.is_deleted == False  # noqa: E712
    ).first()


def get_current_user_required(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    """Require a valid authenticated user — raises 401 if not authenticated.

    Anonymous identities are rejected; only a real ``User`` (i.e. someone
    who logged in with email+password) passes this gate. Endpoints that
    must work without login should depend on ``get_current_user_optional``
    directly instead.
    """
    if not user or getattr(user, "is_anonymous", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(
    user: User = Depends(get_current_user_required),
) -> User:
    """Admin-only guard — raises 403 if the caller is not an admin.

    Use this on endpoints that should only be accessible to the super-admin
    (e.g. user management, system settings).  Normal users get 403.
    """
    if getattr(user, "role", "user") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
