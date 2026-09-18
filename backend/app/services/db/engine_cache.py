"""Per-connection-signature SQLAlchemy engine cache.

Historically each connector built a brand-new Engine (and therefore a fresh
connection pool + TLS/handshake) on every ``with get_connector(kb) as conn:``
and disposed it on exit. For arbitrary user-bound databases this added
50-300ms of connection setup to *every* query and schema introspection call.

We now keep ONE Engine per unique connection signature and reuse its built-in
pool across requests. This preserves DB-agnostic behaviour (MySQL / Postgres /
MSSQL / Oracle / SQLite all go through the same cache) and never touches the
user's schema — it only changes connection lifecycle.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Keyed by a stable signature of the connection (db_type + url + per-connection
# options such as a Postgres search_path). Value is the cached Engine.
_CACHE: dict[str, Engine] = {}
# Monotonic last-use timestamp per key — drives idle + LRU eviction.
_LAST_USED: dict[str, float] = {}
_LOCK = threading.Lock()

# Conservative pool sizing: enough concurrency for one agent turn (which may
# run several queries) without exhausting a small user database's max_connections.
_POOL_SIZE = 5
_MAX_OVERFLOW = 10

# Bounded cache: a long-lived process must not hold a pool per credential ever
# seen (passwords are part of the key, so rotating one strands the old engine).
_MAX_ENGINES = 32
_IDLE_TTL_S = 3600.0


def _cache_key(db_type: str, url: str, connect_args: dict | None) -> str:
    extra = repr(sorted(connect_args.items())) if connect_args else ""
    return f"{db_type}|{url}|{extra}"


def acquire_engine(
    db_type: str,
    url: str,
    *,
    connect_args: dict | None = None,
) -> Engine:
    """Return a (cached) SQLAlchemy Engine for the given connection signature.

    The first call for a signature creates the Engine with a real connection
    pool; subsequent calls reuse it. ``pool_pre_ping`` defends against dead
    connections, and the cache is bounded (see ``_evict_locked``) so a
    long-lived process cannot accumulate one engine — and up to
    ``_POOL_SIZE + _MAX_OVERFLOW`` connections — per credential ever used.
    """
    key = _cache_key(db_type, url, connect_args)
    now = time.monotonic()
    with _LOCK:
        eng = _CACHE.get(key)
        if eng is not None:
            # The engine may have been closed by a prior explicit reset.
            try:
                if getattr(eng, "pool", None) is not None and getattr(eng.pool, "_closed", False):
                    raise Exception("pool closed")
            except Exception:
                _CACHE.pop(key, None)
            else:
                _LAST_USED[key] = now
                return eng
        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            future=True,
            connect_args=connect_args or {},
        )
        _CACHE[key] = eng
        _LAST_USED[key] = now
        _evict_locked(now, keep=key)
        return eng


def _evict_locked(now: float, *, keep: str | None = None) -> int:
    """Dispose idle engines and cap the cache size. Caller holds ``_LOCK``.

    A rotated password or a re-pointed KB produces a NEW cache key while the old
    engine keeps its handshake pool alive for the process lifetime; before this
    the cache grew without bound (``reset_engine`` existed but nothing called
    it). Eviction is by idleness first, then least-recently-used.
    """
    dropped = 0
    victims = [
        k for k in _CACHE
        if k != keep and now - _LAST_USED.get(k, now) > _IDLE_TTL_S
    ]
    over = len(_CACHE) - len(victims) - _MAX_ENGINES
    if over > 0:
        by_age = sorted(
            (k for k in _CACHE if k != keep and k not in victims),
            key=lambda k: _LAST_USED.get(k, 0.0),
        )
        victims.extend(by_age[:over])
    for k in victims:
        eng = _CACHE.pop(k, None)
        _LAST_USED.pop(k, None)
        if eng is not None:
            try:
                eng.dispose()
                dropped += 1
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("engine_cache: dispose failed during eviction: %s", e)
    if dropped:
        logger.info(
            "engine_cache: disposed %d engine(s) (idle > %.0fs or over cap %d); %d cached",
            dropped, _IDLE_TTL_S, _MAX_ENGINES, len(_CACHE),
        )
    return dropped


def prune_engines() -> int:
    """Public hook: evict idle/over-cap engines (ops + tests)."""
    with _LOCK:
        return _evict_locked(time.monotonic())


def engine_count() -> int:
    """Return the number of cached engines (diagnostics only)."""
    with _LOCK:
        return len(_CACHE)


def reset_engine(db_type: str, url: str, *, connect_args: dict | None = None) -> None:
    """Drop a cached engine (e.g. after credential rotation / KB delete).

    Called best-effort; callers should not depend on it for correctness.
    """
    key = _cache_key(db_type, url, connect_args)
    with _LOCK:
        eng = _CACHE.pop(key, None)
    if eng is not None:
        try:
            eng.dispose()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("engine_cache: dispose failed: %s", e)
