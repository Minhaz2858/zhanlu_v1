"""Cross-turn result cache for ask_data_agent queries.

Two-tier TTL cache: in-process dict (always available) + optional Redis
(shared across workers). Key = sha256(normalized_question + kb_id +
user_id_scope). Default TTL = QUERY_RESULT_CACHE_TTL_S (300s).

When Redis is unavailable, silently degrades to in-process only (like
the existing schema cache in schema_service.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process TTL cache
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _ttl_seconds() -> int:
    from app.config import settings
    return getattr(settings, "QUERY_RESULT_CACHE_TTL_S", 300)


def _is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "QUERY_RESULT_CACHE_ENABLED", True)


def _normalize_question(question: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = question.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cache_key(question: str, kb_id: str | None, user_id: str | None = None) -> str:
    norm = _normalize_question(question)
    raw = f"{norm}|{kb_id or ''}|{user_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_cached_result(
    question: str, kb_id: str | None, user_id: str | None = None,
) -> dict | None:
    """Return cached result or None (cache miss).

    The returned dict has an extra ``_cache_age_s`` key indicating how
    many seconds ago the result was cached.
    """
    if not _is_enabled():
        return None
    key = _cache_key(question, kb_id, user_id)
    now = time.monotonic()
    ttl = _ttl_seconds()

    # Tier 1: in-process
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            ts, value = hit
            age = now - ts
            if age > ttl:
                _CACHE.pop(key, None)
            else:
                result = dict(value)
                result["_cache_age_s"] = round(age, 1)
                logger.debug("query_result_cache: in-process hit (key=%s, age=%.1fs)", key[:8], age)
                return result

    # Tier 2: Redis (optional)
    try:
        from app.database import get_redis
        redis = get_redis()
        if redis:
            raw = redis.get(f"qrc:{key}")
            if raw:
                result = json.loads(raw)
                result["_cache_age_s"] = -1  # Redis doesn't track age easily
                logger.debug("query_result_cache: Redis hit (key=%s)", key[:8])
                return result
    except Exception:
        pass  # Redis unavailable — degrade silently

    return None


def put_result(
    question: str, kb_id: str | None, result: dict,
    user_id: str | None = None,
) -> None:
    """Store result in both cache tiers."""
    if not _is_enabled():
        return
    if not result.get("success") or not result.get("rows"):
        return  # don't cache empty/error results

    key = _cache_key(question, kb_id, user_id)
    # Strip non-serializable fields before caching
    cacheable = {k: v for k, v in result.items()
                 if k != "_cache_age_s"
                 and isinstance(v, (str, int, float, bool, list, dict, type(None)))}
    now = time.monotonic()

    # Tier 1: in-process
    with _CACHE_LOCK:
        _CACHE[key] = (now, cacheable)

    # Tier 2: Redis
    try:
        from app.database import get_redis
        redis = get_redis()
        if redis:
            redis.setex(f"qrc:{key}", _ttl_seconds(), json.dumps(cacheable, ensure_ascii=False))
    except Exception:
        pass


def invalidate(kb_id: str | None = None) -> None:
    """Drop cached entries — for one KB, or everything."""
    with _CACHE_LOCK:
        if kb_id is None:
            _CACHE.clear()
            return
        # In-process: can't filter by kb_id efficiently (key is hashed),
        # so we do a full scan (cache is small, typically <100 entries)
        to_drop = [k for k, (_, v) in _CACHE.items() if v.get("source_id") == kb_id]
        for k in to_drop:
            _CACHE.pop(k, None)

    # Redis: best-effort scan
    try:
        from app.database import get_redis
        redis = get_redis()
        if redis:
            cursor = 0
            while True:
                cursor, keys = redis.scan(cursor, match="qrc:*", count=100)
                if keys:
                    redis.delete(*keys)
                if cursor == 0:
                    break
    except Exception:
        pass
