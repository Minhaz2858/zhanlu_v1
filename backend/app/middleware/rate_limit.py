"""In-memory sliding-window rate limiter + FastAPI dependency factory (plan 2026-07-27)."""
import threading
import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request


class RateLimiter:
    """Thread-safe in-memory sliding-window limiter.

    Not distributed — fine for a single-process deployment. If the app ever
    scales horizontally, swap the storage for Redis (the interface stays the
    same).
    """

    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> bool:
        """Return True if the request is allowed (under limit), False if blocked."""
        if limit <= 0:
            return True  # disabled
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            # Drop expired entries
            self._buckets[key] = [t for t in bucket if t > now - window_s]
            if len(self._buckets[key]) >= limit:
                return False
            self._buckets[key].append(now)
            return True


# Module-level singleton reused by the dependency factory.
_limiter = RateLimiter()


def rate_limit(limit: int, window_s: int) -> Callable:
    """FastAPI dependency: raises 429 if the requesting IP is over `limit` in `window_s`."""
    def dep(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{client_ip}"
        if not _limiter.check(key, limit=limit, window_s=window_s):
            raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    return dep
