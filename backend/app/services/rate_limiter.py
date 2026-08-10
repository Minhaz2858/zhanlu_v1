"""Redis-based sliding window rate limiter.

Per-user throughput limiting to prevent API abuse. Uses a Redis sorted-set
for the sliding window — each request adds a timestamp score; expired entries
(before ``now - window_s``) are trimmed on each check.

Configuration:
- ``RATE_LIMIT_ENABLED`` — master toggle (default False)
- ``RATE_LIMIT_RPM`` — max requests per minute per user+app (default 60)
- ``RATE_LIMIT_WINDOW_S`` — sliding window duration in seconds (default 60)
- ``RATE_LIMIT_WHITELIST`` — JSON array of user IDs exempt from rate limiting

Degrades gracefully: if Redis is unavailable, all requests pass through.
HTTP 429 + Retry-After header on exceeded limit.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_REDIS_KEY_PREFIX = "rate_limit:"


def _get_redis_client():
    """Return sync Redis client or None."""
    from app.database import get_redis
    return get_redis()


def _is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "RATE_LIMIT_ENABLED", False)


def _window_s() -> int:
    from app.config import settings
    return getattr(settings, "RATE_LIMIT_WINDOW_S", 60)


def _max_requests() -> int:
    from app.config import settings
    return getattr(settings, "RATE_LIMIT_RPM", 60)


def _whitelist() -> set[str]:
    from app.config import settings
    raw = getattr(settings, "RATE_LIMIT_WHITELIST", "[]") or "[]"
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return set()


def check_rate_limit(
    user_id: str,
    app_id: str = "default-app",
    max_requests: Optional[int] = None,
    window_s: Optional[int] = None,
) -> tuple[bool, int]:
    """Check whether a request is within the rate limit.

    Returns:
        (allowed, retry_after_seconds):
            - (True, 0) when allowed
            - (False, seconds_to_wait) when rate-limited.

    Side-effect: records this request in Redis (when Redis is available
    and rate limiting is enabled).
    """
    if not _is_enabled():
        return True, 0

    if user_id in _whitelist():
        return True, 0

    max_reqs = max_requests or _max_requests()
    window = window_s or _window_s()
    now_s = time.monotonic()
    window_start = now_s - window

    key = f"{_REDIS_KEY_PREFIX}{user_id}:{app_id}"

    r = _get_redis_client()
    if r is None:
        return True, 0  # Redis unavailable — allow all

    try:
        # Trim expired entries from the sorted set
        r.zremrangebyscore(key, 0, window_start)
        # Count remaining entries (requests in the current window)
        count = r.zcard(key) or 0

        if count >= max_reqs:
            # Rate limited — find when the oldest entry expires
            oldest = r.zrange(key, 0, 0, withscores=True)
            if oldest:
                _, oldest_ts = oldest[0]
                retry_after = max(0, int(oldest_ts + window - now_s) + 1)
                return False, retry_after
            return False, window

        # Record this request
        r.zadd(key, {str(now_s): now_s})
        r.expire(key, window * 2)  # TTL = 2× window
        return True, 0
    except Exception as e:
        logger.warning("Rate limiter check failed (non-fatal): %s", e)
        return True, 0


def is_rate_limited(user_id: str, app_id: str = "default-app") -> bool:
    """Shorthand: return True if the user is rate-limited."""
    allowed, _ = check_rate_limit(user_id, app_id)
    return not allowed


def reset_rate_limit(user_id: str, app_id: str = "default-app") -> bool:
    """Reset the rate limit counter for a user. Admin use only."""
    r = _get_redis_client()
    if r is None:
        return False
    try:
        r.delete(f"{_REDIS_KEY_PREFIX}{user_id}:{app_id}")
        return True
    except Exception:
        return False


__all__ = [
    "check_rate_limit",
    "is_rate_limited",
    "reset_rate_limit",
]
