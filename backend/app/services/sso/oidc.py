"""OIDC provider config + discovery + ID-token verification.

Provider definitions come from ``SSO_PROVIDERS`` (env JSON list). Discovery
hits ``{issuer}/.well-known/openid-configuration`` (or an explicit
``discovery_url``); ID tokens are verified with PyJWT against the
provider's JWKS (signature, ``iss``, ``aud``, ``exp``, ``nonce``) and the
email claim contract (``email`` present, ``email_verified`` not False).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = "openid email profile"


@dataclass
class SsoProvider:
    id: str
    name: str
    client_id: str
    client_secret: str
    issuer: str
    discovery_url: str = ""
    scopes: str = DEFAULT_SCOPES

    @property
    def well_known_url(self) -> str:
        if self.discovery_url:
            return self.discovery_url
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


def get_providers() -> list[SsoProvider]:
    """Return configured providers ([] when SSO is disabled)."""
    if not settings.SSO_ENABLED:
        return []
    providers: list[SsoProvider] = []
    for raw in settings.SSO_PROVIDERS or []:
        if not isinstance(raw, dict):
            continue
        try:
            providers.append(SsoProvider(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or raw.get("id") or ""),
                client_id=str(raw.get("client_id") or ""),
                client_secret=str(raw.get("client_secret") or ""),
                issuer=str(raw.get("issuer") or ""),
                discovery_url=str(raw.get("discovery_url") or ""),
                scopes=str(raw.get("scopes") or DEFAULT_SCOPES),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SSO provider config invalid: %s", exc)
    return [p for p in providers if p.id and p.client_id and p.issuer]


def get_provider(provider_id: str) -> Optional[SsoProvider]:
    for provider in get_providers():
        if provider.id == provider_id:
            return provider
    return None


async def fetch_discovery(provider: SsoProvider) -> dict:
    """Fetch OIDC discovery metadata; {} on any failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(provider.well_known_url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("authorization_endpoint"):
                    return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("OIDC discovery failed for %s: %s", provider.id, exc)
    return {}


async def fetch_jwks(jwks_uri: str) -> dict:
    """Fetch the provider JWKS; {"keys": []} on any failure."""
    if not jwks_uri:
        return {"keys": []}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(jwks_uri)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("keys"), list):
                    return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("JWKS fetch failed for %s: %s", jwks_uri, exc)
    return {"keys": []}


def verify_id_token(
    id_token: str,
    *,
    provider: SsoProvider,
    jwks: dict,
    nonce: str,
) -> Optional[dict]:
    """Verify an OIDC ID token; returns claims or None.

    Checks signature (each key in the JWKS), ``iss``, ``aud``, ``exp``,
    then the ``nonce`` and email claim contract.
    """
    if not id_token:
        return None
    keys = (jwks or {}).get("keys") or []
    for jwk_dict in keys:
        try:
            jwk = jwt.PyJWK(jwk_dict)
            claims = jwt.decode(
                id_token,
                key=jwk.key,
                algorithms=["RS256", "ES256"],
                audience=provider.client_id,
                issuer=provider.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.InvalidTokenError:
            continue
        except Exception as exc:  # noqa: BLE001 — malformed JWK etc.
            logger.debug("ID-token verification attempt failed: %s", exc)
            continue
        if claims.get("nonce") != nonce:
            logger.warning("SSO ID-token nonce mismatch for %s", provider.id)
            return None
        email = claims.get("email")
        if not email or claims.get("email_verified") is False:
            logger.warning("SSO ID-token missing/Unverified email for %s", provider.id)
            return None
        return claims
    logger.warning("SSO ID-token signature/issuer/audience verification failed for %s", provider.id)
    return None


__all__ = ["SsoProvider", "get_providers", "get_provider", "fetch_discovery",
           "fetch_jwks", "verify_id_token", "DEFAULT_SCOPES"]
