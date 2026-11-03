"""Phase 8.2 — external MCP personal token issuance/revocation.

Smallest secure addition (per the Stage 1 spec): a long-lived personal
JWT in the EXISTING access-token format (same secret, same JTI revocation
blacklist) so external MCP clients authenticate without refresh flows.
Tokens are personal (user-scoped); revocation requires presenting the raw
token (server verifies the signature and that the subject is the caller —
a caller can never revoke another user's token); issuance and revocation
are audited with NO token material in the audit details.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_current_user_required, get_db
from app.models.audit_event import AuditEvent
from app.services.auth_service import AuthService

router = APIRouter(prefix="/api/mcp/tokens", tags=["mcp-tokens"])

DEFAULT_MCP_TOKEN_DAYS = 90
MAX_MCP_TOKEN_DAYS = 365


class McpTokenRequest(BaseModel):
    days: int = DEFAULT_MCP_TOKEN_DAYS


class McpTokenRevokeRequest(BaseModel):
    token: str


def _audit(db: Session, user: User, event_type: str, detail: dict) -> None:
    import uuid as _uuid
    from datetime import datetime as _dt

    db.add(AuditEvent(
        id=str(_uuid.uuid4()),
        event_type=event_type,
        event_source="mcp",
        actor_type="user",
        actor_id=user.id,
        org_id=user.org_id,
        resource_type="mcp_token",
        outcome="success",
        detail_json=detail,  # ids/metadata only — NEVER token material
        occurred_at=_dt.utcnow(),
    ))


@router.post("")
def issue_mcp_token(
    body: McpTokenRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Issue a personal long-lived MCP token (returned ONCE)."""
    days = min(max((body.days if body else DEFAULT_MCP_TOKEN_DAYS), 1),
               MAX_MCP_TOKEN_DAYS)
    svc = AuthService()
    token = svc.create_access_token(user.id, db)  # base claims (org/role)
    # extend the expiry for MCP operability, keeping the SAME format/secret;
    # the JTI stays revocable through the existing blacklist
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone
    from app.config import settings

    payload = pyjwt.decode(token, settings.JWT_SECRET,
                           algorithms=[settings.JWT_ALGORITHM])
    payload["exp"] = datetime.now(timezone.utc) + timedelta(days=days)
    token = pyjwt.encode(payload, settings.JWT_SECRET,
                         algorithm=settings.JWT_ALGORITHM)
    _audit(db, user, "mcp_token_issued", {"days": days})
    db.commit()
    return {"token": token, "days": days,
            "usage": "Authorization: Bearer <token> on /api/ext-mcp"}


@router.post("/revoke")
def revoke_mcp_token(
    body: McpTokenRevokeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Revoke a personal MCP token. Ownership is verified server-side: the
    RAW token must be presented, its signature verified, and its ``sub``
    must equal the caller — knowing a JTI grants no revocation power."""
    raw = (body.token or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="token is required")
    svc = AuthService()
    claims = svc.verify_token_claims(raw, db)
    if not claims:
        raise HTTPException(status_code=400,
                            detail="invalid or already-revoked token")
    if claims.get("sub") != user.id:
        # attempted cross-user revocation — refuse AND audit
        _audit(db, user, "mcp_token_revoke_denied",
               {"reason": "cross-user revocation attempt"})
        db.commit()
        raise HTTPException(status_code=403,
                            detail="not your token")
    from datetime import datetime, timezone

    exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    svc.revoke_access_token(claims["jti"], user.id, exp, db)
    db.commit()
    _audit(db, user, "mcp_token_revoked", {"jti_prefix": claims["jti"][:8]})
    db.commit()
    return {"revoked": True}
