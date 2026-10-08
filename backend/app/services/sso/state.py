"""In-memory SSO authorization state store (state → nonce/next/created_at).

One-time use with a TTL, mirroring the ``_PENDING_OAUTH`` pattern in
``routers/mcp.py``. Single-process only — a Redis-backed store is the
Phase 2 upgrade for multi-worker deployments.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

from app.config import settings


class SsoStateStore:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl_seconds = max(0, int(ttl_seconds or 0))
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def create(self, provider_id: str, next_url: str = "") -> dict:
        """Create a state entry; returns ``{"state", "nonce"}``."""
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(16)
        with self._lock:
            # Sweep expired entries here (the TTL was only checked on consume,
            # so abandoned flows accumulated forever).
            if self._ttl_seconds:
                now = time.monotonic()
                expired = [
                    s for s, e in self._entries.items()
                    if now - e["created_at"] > self._ttl_seconds
                ]
                for s in expired:
                    self._entries.pop(s, None)
            self._entries[state] = {
                "provider_id": provider_id,
                "nonce": nonce,
                "next_url": next_url or "",
                "created_at": time.monotonic(),
            }
        return {"state": state, "nonce": nonce}

    def consume(self, state: str) -> Optional[dict]:
        """Pop and return the entry for ``state`` (None if unknown/expired).

        Entries are single-use: a second consume returns None.
        """
        with self._lock:
            entry = self._entries.pop(state, None)
        if not entry:
            return None
        if time.monotonic() - entry["created_at"] > self._ttl_seconds:
            return None
        return entry


# Singleton used by the SSO router (TTL from settings).
sso_state_store = SsoStateStore(ttl_seconds=settings.SSO_STATE_TTL_S)

__all__ = ["SsoStateStore", "sso_state_store"]
