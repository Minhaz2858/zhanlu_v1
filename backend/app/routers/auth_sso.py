"""SSO / OIDC router — provider list, authorize redirect, callback.

Browser-native OIDC authorization-code flow:

  GET /apps/{app_id}/auth/sso/providers
      → [{id, name}] for the frontend "Continue with …" buttons
  GET /apps/{app_id}/auth/sso/{provider}/authorize?next=
      → 302 to the IdP (state + nonce stored server-side)
  GET /apps/{app_id}/auth/sso/{provider}/callback?code&state
      → exchange code → verify ID token → find-or-create user →
        302 to ``{FRONTEND_URL}/sso/callback#access_token=…&refresh_token=…``

Tokens are only ever placed in the URL **fragment** (never query strings /
server logs). ``SSO_ENABLED=false`` ⇒ providers returns [] and the
authorize/callback routes return 403; local email/password login is
untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db
from app.services.auth_service import auth_service
from app.services.mcp_client.oauth import build_authorize_url, exchange_code
from app.services.sso.account import find_or_create_sso_user
from app.services.sso.oidc import (
    fetch_discovery,
    fetch_jwks,
    get_provider,
    get_providers,
    verify_id_token,
)
from app.services.sso.state import sso_state_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth-sso"])


def _sso_enabled() -> None:
    if not settings.SSO_ENABLED:
        raise HTTPException(status_code=403, detail="SSO is disabled")


def _callback_uri(app_id: str, provider_id: str) -> str:
    """Public callback URL the IdP redirects to (frontend origin proxies /api)."""
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/api/apps/{app_id}/auth/sso/{provider_id}/callback"


def _error_redirect(reason: str) -> RedirectResponse:
    base = settings.FRONTEND_URL.rstrip("/")
    return RedirectResponse(f"{base}/sso/callback?error={reason}", status_code=302)


@router.get("/apps/{app_id}/auth/sso/providers")
async def sso_providers(app_id: str) -> dict:
    """List enabled SSO providers for the login/register pages."""
    if not settings.SSO_ENABLED:
        return {"providers": []}
    return {"providers": [{"id": p.id, "name": p.name} for p in get_providers()]}


@router.get("/apps/{app_id}/auth/sso/{provider_id}/authorize")
async def sso_authorize(
    app_id: str,
    provider_id: str,
    next: Optional[str] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Start the OIDC flow — 302 to the IdP's authorization endpoint."""
    _sso_enabled()
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown SSO provider: {provider_id}")
    discovery = await fetch_discovery(provider)
    endpoint = discovery.get("authorization_endpoint")
    if not endpoint:
        raise HTTPException(
            status_code=502,
            detail="Provider discovery failed (no authorization_endpoint)",
        )
    entry = sso_state_store.create(provider_id, next_url=next or "")
    auth_url = build_authorize_url(
        discovery,
        client_id=provider.client_id,
        redirect_uri=_callback_uri(app_id, provider_id),
        scopes=provider.scopes,
        state=entry["state"],
    )
    sep = "&" if "?" in auth_url else "?"
    return RedirectResponse(f"{auth_url}{sep}nonce={entry['nonce']}", status_code=302)


@router.get("/apps/{app_id}/auth/sso/{provider_id}/callback")
async def sso_callback(
    app_id: str,
    provider_id: str,
    code: Optional[str] = "",
    state: Optional[str] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Finish the OIDC flow — exchange code, verify, issue Zhanlu tokens."""
    _sso_enabled()
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown SSO provider: {provider_id}")
    pending = sso_state_store.consume(state or "")
    if not pending or pending.get("provider_id") != provider_id:
        raise HTTPException(status_code=400, detail="Invalid or expired SSO state")

    try:
        discovery = await fetch_discovery(provider)
        tokens = await exchange_code(
            discovery,
            client_id=provider.client_id,
            client_secret=provider.client_secret,
            code=code or "",
            redirect_uri=_callback_uri(app_id, provider_id),
            code_verifier="",
        )
        jwks = await fetch_jwks(discovery.get("jwks_uri") or "")
        claims = verify_id_token(
            tokens.get("id_token") or "",
            provider=provider,
            jwks=jwks,
            nonce=pending["nonce"],
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("sso callback token exchange failed for %s: %s", provider_id, exc)
        return _error_redirect("token_exchange_failed")
    if not claims:
        return _error_redirect("invalid_id_token")

    email = claims.get("email")
    if not email or claims.get("email_verified") is False:
        return _error_redirect("email_not_verified")
    user = find_or_create_sso_user(
        db,
        email=email,
        full_name=claims.get("name") or str(claims.get("sub") or email),
        provider_id=provider_id,
    )
    if user is None:
        return _error_redirect("email_not_allowed")

    access_token = auth_service.create_access_token(user.id, db)
    refresh_token = auth_service.create_refresh_token(user.id, db)
    fragment = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": json.dumps(user.to_dict(), ensure_ascii=False),
    }
    next_url = (pending.get("next_url") or "").strip()
    if next_url:
        fragment["next"] = next_url
    base = settings.FRONTEND_URL.rstrip("/")
    return RedirectResponse(f"{base}/sso/callback#{urlencode(fragment)}", status_code=302)
