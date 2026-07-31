"""OpenAI-compatible embedding client with Redis cache.

Wraps ``/v1/embeddings`` requests behind a small, testable surface:

* one call per text (or batched)
* a tiny per-(text_hash) Redis cache so repeat queries don't re-bill
* graceful fallback: when the embedding endpoint is unreachable the
  caller gets ``None`` and falls back to lexical scoring (the existing
  keyword-overlap scorer in ``memory_advanced``).

The module is dependency-light: it only requires ``httpx`` (already used
elsewhere in the backend) and an optional Redis client.  If Redis is
unavailable the cache silently degrades to an in-process LRU.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Lazy imports to keep the cold path light.  The module is only loaded
# when ``get_embedding`` is actually called, not at import time.


@dataclass
class EmbeddingResult:
    """A single embedding plus provenance metadata."""

    text: str
    vector: list[float]
    model: str
    cached: bool
    duration_ms: float


class _LRUCache:
    """Tiny in-process LRU used when Redis is unavailable."""

    def __init__(self, max_size: int = 512) -> None:
        self._max = max_size
        self._data: dict[str, tuple[float, list[float]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[list[float]]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            self._data[key] = (time.time(), entry[1])
            return list(entry[1])

    def set(self, key: str, value: list[float]) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            if len(self._data) > self._max:
                # Drop the oldest by insertion time.
                oldest = min(self._data.items(), key=lambda kv: kv[1][0])
                self._data.pop(oldest[0], None)


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def _redis_client():
    """Best-effort Redis client; returns None if not reachable."""
    try:
        import redis  # type: ignore

        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        client.ping()
        return client
    except Exception as exc:
        logger.debug("embeddings: redis unavailable (%s); using in-process cache", exc)
        return None


_REDIS = _redis_client()
_LRU = _LRUCache()


def _cache_get(key: str) -> Optional[list[float]]:
    if _REDIS is not None:
        try:
            raw = _REDIS.get(f"emb:{key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    return _LRU.get(key)


def _cache_set(key: str, value: list[float], ttl_seconds: int = 24 * 3600) -> None:
    if _REDIS is not None:
        try:
            _REDIS.setex(f"emb:{key}", ttl_seconds, json.dumps(value))
            return
        except Exception:
            pass
    _LRU.set(key, value)


def _post_embeddings(texts: list[str], model: str, api_base: str, api_key: str) -> list[list[float]]:
    import httpx  # type: ignore

    url = api_base.rstrip("/") + "/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "input": texts}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    # OpenAI returns data sorted by index; preserve input order just in case.
    vectors: list[list[float]] = [[] for _ in texts]
    for item in data.get("data", []):
        idx = item.get("index", 0)
        if 0 <= idx < len(vectors):
            vectors[idx] = item.get("embedding", [])
    return vectors


def get_embedding(
    text: str,
    *,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    use_cache: bool = True,
) -> Optional[EmbeddingResult]:
    """Return an embedding for ``text`` or ``None`` on any failure.

    Never raises — callers fall back to lexical scoring when this returns
    ``None``.
    """
    if not text or not text.strip():
        return None

    model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    api_base = api_base or os.environ.get("EMBEDDING_API_BASE", os.environ.get("LLM_API_BASE", "https://api.openai.com"))
    api_key = api_key or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    if not api_key:
        logger.debug("embeddings: no API key; skipping")
        return None

    cache_key = f"{model}:{_hash_text(text)}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return EmbeddingResult(
                text=text, vector=cached, model=model, cached=True, duration_ms=0.0,
            )

    started = time.time()
    try:
        vectors = _post_embeddings([text], model=model, api_base=api_base, api_key=api_key)
    except Exception as exc:
        logger.warning("embeddings: API call failed (%s); lexical fallback", exc)
        return None

    if not vectors or not vectors[0]:
        return None
    duration_ms = (time.time() - started) * 1000
    if use_cache:
        _cache_set(cache_key, vectors[0])
    return EmbeddingResult(
        text=text, vector=vectors[0], model=model, cached=False, duration_ms=duration_ms,
    )


def get_embeddings_batch(
    texts: list[str],
    *,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_batch: int = 16,
) -> list[Optional[list[float]]]:
    """Batch embed; respects ``max_batch`` so we never time out a single call.

    Returns one entry per input text (in order); ``None`` for any
    individual failure (including missing API key).  Cache hits short
    circuit the network call per item.
    """
    if not texts:
        return []
    out: list[Optional[list[float]]] = [None] * len(texts)
    pending: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        cached = _cache_get(f"{model or 'text-embedding-3-small'}:{_hash_text(t)}") if model or True else None
        if cached is not None:
            out[i] = cached
        else:
            pending.append((i, t))
    for chunk_start in range(0, len(pending), max_batch):
        chunk = pending[chunk_start : chunk_start + max_batch]
        try:
            vectors = _post_embeddings(
                [t for _, t in chunk],
                model=model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                api_base=api_base or os.environ.get("EMBEDDING_API_BASE", os.environ.get("LLM_API_BASE", "https://api.openai.com")),
                api_key=api_key or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
            )
        except Exception as exc:
            logger.warning("embeddings: batch call failed (%s)", exc)
            continue
        for (orig_idx, text), vec in zip(chunk, vectors):
            if vec:
                out[orig_idx] = vec
                _cache_set(
                    f"{model or 'text-embedding-3-small'}:{_hash_text(text)}",
                    vec,
                )
    return out


__all__ = ["EmbeddingResult", "get_embedding", "get_embeddings_batch"]
