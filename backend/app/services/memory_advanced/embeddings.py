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


# ── Local embedding fallback (2026-10-09) ─────────────────────────────────
# When no EMBEDDING_API_KEY is configured (or the API keeps failing), fall
# back to an in-process sentence-transformers model — BAAI/bge-m3 by default
# (multilingual, strong Chinese, already cached for document ingestion).
# Consumers (semantic recall, tool-search, CBR) get real vectors instead of
# degrading to lexical scoring.
_LOCAL_MODEL: Any = None
_LOCAL_LOCK = threading.Lock()
_API_FAIL_UNTIL: float = 0.0  # circuit breaker: skip the API while broken
_API_FAIL_COOLDOWN_S = 300.0


def _local_fallback_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "EMBEDDING_LOCAL_FALLBACK_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def _provider_mode() -> str:
    """local | auto | api — see config.EMBEDDING_PROVIDER (2026-10-15).

    Bug this fixes: get_embedding used to inherit LLM_API_KEY /
    OPENAI_API_KEY as the embeddings key, so a DeepSeek-key deployment
    hammered api.openai.com on EVERY embedding and ate a doomed 401
    before the local fallback engaged. Now the remote API is only used
    when a DEDICATED embeddings key/base exists (auto), or the operator
    forces it (api) — otherwise local-first, zero doomed calls.
    """
    try:
        from app.config import settings

        mode = str(getattr(settings, "EMBEDDING_PROVIDER", "auto") or "auto").strip().lower()
    except Exception:  # noqa: BLE001
        mode = "auto"
    return mode if mode in ("local", "auto", "api") else "auto"


def _should_call_remote_api(dedicated_key_set: bool, api_base_explicit: bool) -> bool:
    """``dedicated_key_set`` = a DEDICATED embeddings key exists (the
    EMBEDDING_API_KEY env or an explicit caller-supplied api_key — NOT the
    inherited LLM/OPENAI key, which was the doomed-401 bug)."""
    mode = _provider_mode()
    if mode == "local":
        return False
    if mode == "api":
        return dedicated_key_set
    # auto: a DEDICATED embeddings key wins; an explicitly configured
    # base implies the operator pointed embeddings somewhere.
    return dedicated_key_set or api_base_explicit


def _local_embed(texts: list[str]) -> list[list[float]]:
    """Embed with the local sentence-transformers model (lazy singleton)."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        with _LOCAL_LOCK:
            if _LOCAL_MODEL is None:
                try:
                    from app.config import settings

                    name = getattr(
                        settings, "EMBEDDING_LOCAL_MODEL",
                        "sentence-transformers/all-MiniLM-L6-v2",
                    )
                except Exception:  # noqa: BLE001
                    name = "sentence-transformers/all-MiniLM-L6-v2"
                from sentence_transformers import SentenceTransformer

                logger.info(
                    "embeddings: no API backend — loading local model %s "
                    "(first call may take a while) ...", name,
                )
                _LOCAL_MODEL = SentenceTransformer(name)
    vecs = _LOCAL_MODEL.encode(
        texts, convert_to_numpy=True, show_progress_bar=False,
        normalize_embeddings=True,
    )
    return [v.tolist() for v in vecs]


def _local_fallback_vectors(texts: list[str]) -> list[Optional[list[float]]]:
    """Local vectors for the given texts, or all-None if the fallback is
    disabled / the model fails to load. Never raises."""
    if not _local_fallback_enabled() or not texts:
        return [None] * len(texts)
    try:
        vectors = _local_embed(texts)
        try:
            from app.config import settings

            model = f"local:{getattr(settings, 'EMBEDDING_LOCAL_MODEL', 'bge-m3')}"
        except Exception:  # noqa: BLE001
            model = "local:bge-m3"
        out: list[Optional[list[float]]] = []
        for text, vec in zip(texts, vectors):
            out.append(vec)
            _cache_set(f"local:{_hash_text(text)}", vec)
        logger.info(
            "embeddings: local fallback produced %d vectors (%s)",
            len(vectors), model,
        )
        return out
    except Exception as exc:  # noqa: BLE001 — degrade to lexical scoring
        logger.warning("embeddings: local fallback failed: %s", exc)
        return [None] * len(texts)


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
    api_base_explicit = bool(api_base or os.environ.get("EMBEDDING_API_BASE"))
    # Deliberate key = caller-supplied or DEDICATED env — the LLM/OPENAI
    # inheritance below is convenience, never a reason to dial remotely.
    dedicated = bool(api_key or os.environ.get("EMBEDDING_API_KEY"))
    api_base = api_base or os.environ.get("EMBEDDING_API_BASE", os.environ.get("LLM_API_BASE", "https://api.openai.com"))
    api_key = api_key or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    if not _should_call_remote_api(dedicated, api_base_explicit):
        # Provider mode says local — no doomed remote calls (2026-10-15).
        logger.debug("embeddings: provider=local path for %d chars", len(text))
        vecs = _local_fallback_vectors([text])
        return EmbeddingResult(
            text=text, vector=vecs[0] or [], model="local",
            cached=False, duration_ms=0.0,
        ) if vecs[0] else None

    cache_key = f"{model}:{_hash_text(text)}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return EmbeddingResult(
                text=text, vector=cached, model=model, cached=True, duration_ms=0.0,
            )

    started = time.time()
    global _API_FAIL_UNTIL
    if time.time() < _API_FAIL_UNTIL:
        # Circuit breaker open: the API just failed — skip the doomed call.
        vecs = _local_fallback_vectors([text])
        return EmbeddingResult(
            text=text, vector=vecs[0] or [], model="local",
            cached=False, duration_ms=0.0,
        ) if vecs[0] else None
    try:
        vectors = _post_embeddings([text], model=model, api_base=api_base, api_key=api_key)
    except Exception as exc:
        # Circuit breaker: a persistently broken API must not add its failure
        # latency to every call — go local directly for the cooldown window.
        _API_FAIL_UNTIL = time.time() + _API_FAIL_COOLDOWN_S
        logger.warning(
            "embeddings: API call failed (%s); local fallback", exc,
        )
        vecs = _local_fallback_vectors([text])
        return EmbeddingResult(
            text=text, vector=vecs[0] or [], model="local",
            cached=False, duration_ms=0.0,
        ) if vecs[0] else None

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
    global _API_FAIL_UNTIL
    if not texts:
        return []
    out: list[Optional[list[float]]] = [None] * len(texts)
    pending: list[tuple[int, str]] = []

    # Provider gate (2026-10-15): same contract as get_embedding — no
    # doomed remote calls when no dedicated embeddings backend exists.
    api_base_explicit = bool(api_base or os.environ.get("EMBEDDING_API_BASE"))
    dedicated = bool(api_key or os.environ.get("EMBEDDING_API_KEY"))
    api_key = api_key or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    call_remote = _should_call_remote_api(dedicated, api_base_explicit)

    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        cached = _cache_get(f"{model or 'text-embedding-3-small'}:{_hash_text(t)}") if model or True else None
        if cached is not None:
            out[i] = cached
        else:
            pending.append((i, t))
    if not call_remote:
        local = _local_fallback_vectors([t for _, t in pending])
        for (orig_idx, _t), vec in zip(pending, local):
            if vec:
                out[orig_idx] = vec
        return out
    for chunk_start in range(0, len(pending), max_batch):
        chunk = pending[chunk_start : chunk_start + max_batch]
        try:
            vectors = _post_embeddings(
                [t for _, t in chunk],
                model=model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                api_base=api_base or os.environ.get("EMBEDDING_API_BASE", os.environ.get("LLM_API_BASE", "https://api.openai.com")),
                api_key=api_key,
            )
        except Exception as exc:
            _API_FAIL_UNTIL = time.time() + _API_FAIL_COOLDOWN_S
            logger.warning("embeddings: batch call failed (%s); local fallback", exc)
            local = _local_fallback_vectors([t for _, t in chunk])
            for (orig_idx, _t), vec in zip(chunk, local):
                if vec:
                    out[orig_idx] = vec
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
