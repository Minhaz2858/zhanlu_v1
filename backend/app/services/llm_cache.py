"""Optional Redis-backed LLM response cache.

Caches LLM responses for deterministic calls (temperature=0) to avoid
redundant API costs. Non-deterministic calls (temperature>0) are
NOT cached because the same prompt should produce different results.

Cache key = sha256(messages_json + model + temperature + schema_hash)
TTL = ``LLM_RESPONSE_CACHE_TTL_S`` (default 3600 = 1 hour).

Configuration:
- ``LLM_RESPONSE_CACHE_ENABLED`` (default False)
- ``LLM_RESPONSE_CACHE_TTL_S`` (default 3600)
- Redis URL: ``settings.REDIS_URL``

Redis is optional — when unavailable, caching silently degrades to
no-op (all cache misses).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "llm_cache:"


def _get_redis_client():
    """Return a Redis client or None if unavailable."""
    from app.database import get_redis
    return get_redis()


def _is_cache_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "LLM_RESPONSE_CACHE_ENABLED", False)


def _cache_ttl() -> int:
    from app.config import settings
    return getattr(settings, "LLM_RESPONSE_CACHE_TTL_S", 3600)


def _build_cache_key(
    messages: list[dict],
    model: str,
    temperature: float,
    schema_hash: str = "",
) -> str:
    """Build a deterministic cache key from request parameters."""
    canonical = json.dumps(messages, sort_keys=True, ensure_ascii=False)
    data = f"{canonical}|{model}|{round(temperature, 2)}|{schema_hash}"
    digest = hashlib.sha256(data.encode()).hexdigest()[:32]
    return f"{CACHE_KEY_PREFIX}{digest}"


def get_cached_response(
    messages: list[dict],
    model: str,
    temperature: float,
    schema: Optional[dict] = None,
) -> Optional[dict]:
    """Retrieve a cached LLM response, or None on cache miss.

    Only caches temperature=0 responses (deterministic).
    """
    if not _is_cache_enabled():
        return None
    if temperature > 0.01:
        return None  # non-deterministic — don't cache

    r = _get_redis_client()
    if r is None:
        return None

    schema_hash = hashlib.md5(
        json.dumps(schema or {}, sort_keys=True).encode()
    ).hexdigest()[:8]
    key = _build_cache_key(messages, model, temperature, schema_hash)

    try:
        raw = r.get(key)
        if raw is None:
            return None
        cached = json.loads(raw)
        logger.debug("LLM cache hit for model=%s key=%s", model, key[:16])
        return cached
    except Exception as e:
        logger.debug("LLM cache read failed (non-fatal): %s", e)
        return None


def set_cached_response(
    messages: list[dict],
    model: str,
    temperature: float,
    response: dict,
    schema: Optional[dict] = None,
) -> None:
    """Store an LLM response in the cache.

    Only caches temperature=0 responses (deterministic).
    """
    if not _is_cache_enabled():
        return
    if temperature > 0.01:
        return

    r = _get_redis_client()
    if r is None:
        return

    schema_hash = hashlib.md5(
        json.dumps(schema or {}, sort_keys=True).encode()
    ).hexdigest()[:8]
    key = _build_cache_key(messages, model, temperature, schema_hash)

    try:
        r.setex(key, _cache_ttl(), json.dumps(response))
        logger.debug("LLM cache set for model=%s key=%s ttl=%ds", model, key[:16], _cache_ttl())
    except Exception as e:
        logger.debug("LLM cache write failed (non-fatal): %s", e)


def invalidate_cache_for_model(model: str) -> int:
    """Invalidate all cached entries for a specific model. Returns count of keys deleted."""
    r = _get_redis_client()
    if r is None:
        return 0
    try:
        pattern = f"{CACHE_KEY_PREFIX}*"
        keys = list(r.scan_iter(match=pattern, count=100))
        deleted = 0
        for key in keys:
            raw = r.get(key)
            if raw:
                try:
                    entry = json.loads(raw)
                    if entry.get("model") == model:
                        r.delete(key)
                        deleted += 1
                except Exception:
                    pass
        if deleted:
            logger.info("Invalidated %d cache entries for model=%s", deleted, model)
        return deleted
    except Exception as e:
        logger.warning("LLM cache invalidation failed: %s", e)
        return 0


__all__ = [
    "get_cached_response",
    "set_cached_response",
    "invalidate_cache_for_model",
]
