"""MCP OAuth 2.0 client (authorization-code + PKCE, 2026 spec).

Implements the MCP authorization spec surface used by remote servers:
discovery via ``.well-known/oauth-authorization-server``, the
authorization-code flow with PKCE, and token refresh. Tokens are persisted
on the ``McpServer`` row (``oauth_token_json``) — never in prompts, logs,
or sandbox workspaces.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = "openid email profile"


def oauth_metadata_url(base_url: str) -> str:
    """RFC 8414 discovery URL for an MCP server base URL.

    The metadata path is rooted at the server's base URL (including any
    nested prefix such as ``/mcp``), per the MCP authorization spec.
    """
    base = base_url.rstrip("/")
    return f"{base}/.well-known/oauth-authorization-server"


async def fetch_metadata(base_url: str) -> dict[str, Any]:
    """Discover OAuth metadata (authorization/token endpoints, scopes)."""
    url = oauth_metadata_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("authorization_endpoint"):
                    return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("OAuth metadata discovery failed for %s: %s", base_url, exc)
    # Fallback to the bare `.well-known/oauth2` (less common).
    fallback = base_url.rstrip("/") + "/.well-known/oauth2"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(fallback)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("authorization_endpoint"):
                    return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("OAuth metadata fallback failed for %s: %s", base_url, exc)
    return {}


def build_authorize_url(
    metadata: dict[str, Any],
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str = DEFAULT_SCOPES,
    state: str | None = None,
    code_challenge: str | None = None,
) -> str:
    """Build the authorization URL for a browser redirect."""
    endpoint = metadata.get("authorization_endpoint", "")
    if not endpoint:
        raise ValueError("OAuth metadata missing authorization_endpoint")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state or secrets.token_urlsafe(16),
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{urlencode(params)}"


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def exchange_code(
    metadata: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens at the token endpoint."""
    endpoint = metadata.get("token_endpoint")
    if not endpoint:
        raise ValueError("OAuth metadata missing token_endpoint")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        auth = (client_id, client_secret)
    else:
        auth = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            endpoint,
            data=payload,
            headers=headers,
            auth=auth,
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_tokens(
    metadata: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Refresh an expired access token."""
    endpoint = metadata.get("token_endpoint")
    if not endpoint:
        raise ValueError("OAuth metadata missing token_endpoint")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = (client_id, client_secret) if client_secret else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(endpoint, data=payload, headers=headers, auth=auth)
        resp.raise_for_status()
        return resp.json()


def auth_headers(tokens: dict[str, Any] | None) -> dict[str, str]:
    """Build ``Authorization`` headers from stored tokens."""
    if not tokens or not tokens.get("access_token"):
        return {}
    token_type = (tokens.get("token_type") or "Bearer").capitalize()
    return {"Authorization": f"{token_type} {tokens['access_token']}"}


def tokens_to_json(tokens: dict[str, Any]) -> str:
    # IBM framework-audit #3: OAuth tokens encrypted at rest (Fernet,
    # key from CREDENTIAL_ENCRYPTION_KEY or derived from JWT_SECRET).
    from app.services.crypto import encrypt_value

    return encrypt_value(json.dumps(tokens, ensure_ascii=False, default=str)) or ""


def tokens_from_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    from app.services.crypto import decrypt_value

    payload = decrypt_value(raw) or raw  # legacy plaintext passes through
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "oauth_metadata_url",
    "fetch_metadata",
    "build_authorize_url",
    "generate_pkce",
    "exchange_code",
    "refresh_tokens",
    "auth_headers",
    "tokens_to_json",
    "tokens_from_json",
]
