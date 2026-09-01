"""Signed, short-lived preview tokens for iframe-friendly file previews.

Iframes and <img> tags cannot send Authorization headers, so the
authenticated preview endpoint also accepts a ``?token=`` query param
carrying one of these HMAC-signed tokens. Tokens are stateless: the
payload is bound to (file_id, user_id) and carries an expiry; the
endpoint still runs the tenant check against the resolved user.

Signed with ``settings.JWT_SECRET`` (stdlib hmac — no new dependency).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from app.config import settings

_DEFAULT_TTL_SECONDS = 900  # 15 minutes


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(payload_b64: str) -> str:
    digest = hmac.new(
        settings.JWT_SECRET.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(digest)


def mint_preview_token(
    *, file_id: str, user_id: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> str:
    payload = {
        "fid": str(file_id),
        "uid": str(user_id),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_preview_token(token: str, file_id: str) -> Optional[str]:
    """Return the user_id bound to ``token`` for ``file_id``, else None."""
    if not token or token.count(".") != 1:
        return None
    payload_b64, sig = token.split(".", 1)
    if not hmac.compare_digest(_sign(payload_b64), sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if payload.get("fid") != str(file_id):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    uid = payload.get("uid")
    return str(uid) if uid else None
