"""HMAC-signed, time-limited download tokens for emailed output files.

Email recipients are not logged-in users, so the download route that serves an
automation's output file cannot rely on session auth. Instead we mint a short
token (file id + expiry, HMAC-SHA256 signed with ``JWT_SECRET``) and embed it in
the emailed URL. The route verifies the signature and expiry before streaming.

The signing key is ``JWT_SECRET`` (no new secret env var). Tokens expire after
``EMAIL_DOWNLOAD_LINK_TTL_DAYS`` (default 7).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import settings


def _secret() -> bytes:
    secret = getattr(settings, "JWT_SECRET", "") or "zhanlu-email-download"
    return secret.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    return hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def generate_download_token(file_id: str, ttl_days: int | None = None) -> str:
    """Return an opaque token proving access to ``file_id`` until expiry."""
    ttl = ttl_days if ttl_days is not None else int(settings.EMAIL_DOWNLOAD_LINK_TTL_DAYS)
    exp = int(time.time()) + int(ttl) * 86400
    payload = {"fid": str(file_id), "exp": exp}
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_download_token(token: str, file_id: str) -> bool:
    """Validate ``token`` for ``file_id`` (signature + expiry + binding)."""
    if not token or not file_id:
        return False
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(payload_b64), sig):
        return False
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return False
    if str(payload.get("fid")) != str(file_id):
        return False
    if int(payload.get("exp", 0)) < int(time.time()):
        return False
    return True


def build_download_url(file_id: str, token: str | None = None) -> str:
    """Absolute URL for the unauthenticated email-download route."""
    tok = token or generate_download_token(file_id)
    base = (getattr(settings, "APP_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}/api/automations/email-download/{file_id}?token={tok}"
