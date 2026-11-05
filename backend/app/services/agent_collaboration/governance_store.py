"""Distributed-safe governance primitives for collaboration.

Single source of truth for:

* global top-level concurrency slots (``COLLAB_MAX_CONCURRENT``)
* per-conversation duplicate-prevention set
* per-conversation child counter (``COLLAB_MAX_CHILDREN_PER_PARENT``)
* per-conversation cumulative token consumption (``token_budget``)

Backed by Redis when reachable (reusing the existing ``get_redis()`` client),
with a single-process in-memory fallback when Redis is unavailable (dev / tests).
No new infrastructure dependency is introduced — if Redis is down, governance
degrades to the original in-process semantics rather than failing open.

Part 1.1 design goals:
* Atomic acquire / release so multiple Uvicorn workers, processes or backend
  instances cannot double-count or corrupt counters.
* Owner-scoped release: each acquisition carries a unique token so a crashed
  worker can never release another worker's reservation.
* TTL-based lease recovery: orphaned keys expire automatically.
* Fail-safe: a Redis error during acquire is fail-closed (refuse new top-level
  work); a Redis error during the soft dedup / counter path is fail-open (do not
  block work on a cache hiccup — dedup and counting are best-effort guards).
"""
from __future__ import annotations

import logging
import threading
import uuid

from app.database import get_redis

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 3600  # lease-recovery window for orphaned keys


class GovernanceStore:
    """Abstract store. Concrete impls: ``_RedisStore`` / ``_InProcessStore``."""

    def acquire_global_slot(self, limit: int, ttl: int = DEFAULT_TTL_S) -> bool:
        raise NotImplementedError

    def release_global_slot(self) -> None:
        raise NotImplementedError

    def global_active_count(self) -> int:
        raise NotImplementedError

    def conv_dedup_seen(self, conv_id: str, key: str, ttl: int = DEFAULT_TTL_S) -> bool:
        """Return ``True`` if ``key`` was NEWLY added (i.e. NOT a duplicate)."""
        raise NotImplementedError

    def conv_child_count(self, conv_id: str, requesting: str, ttl: int = DEFAULT_TTL_S) -> int:
        raise NotImplementedError

    def conv_token_total(self, conv_id: str, ttl: int = DEFAULT_TTL_S) -> int:
        raise NotImplementedError

    def conv_token_add(self, conv_id: str, n: int, ttl: int = DEFAULT_TTL_S) -> int:
        """Add ``n`` tokens to the per-conversation cumulative; return new total."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class _InProcessStore(GovernanceStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: set[str] = set()            # owner tokens for global slots
        self._conv: dict = {}                    # conv_id -> state dict

    def _state(self, conv_id: str) -> dict:
        return self._conv.setdefault(conv_id, {"seen": set(), "children": {}, "tokens": 0})

    def acquire_global_slot(self, limit, ttl=DEFAULT_TTL_S):
        with self._lock:
            if len(self._slots) >= limit:
                return False
            self._slots.add(uuid.uuid4().hex)
            return True

    def release_global_slot(self):
        with self._lock:
            if self._slots:
                self._slots.pop()

    def global_active_count(self):
        return len(self._slots)

    def conv_dedup_seen(self, conv_id, key, ttl=DEFAULT_TTL_S):
        st = self._state(conv_id)
        if key in st["seen"]:
            return False
        st["seen"].add(key)
        return True

    def conv_child_count(self, conv_id, requesting, ttl=DEFAULT_TTL_S):
        st = self._state(conv_id)
        st["children"][requesting] = st["children"].get(requesting, 0) + 1
        return st["children"][requesting]

    def conv_token_total(self, conv_id, ttl=DEFAULT_TTL_S):
        return self._state(conv_id)["tokens"]

    def conv_token_add(self, conv_id, n, ttl=DEFAULT_TTL_S):
        st = self._state(conv_id)
        st["tokens"] += n
        return st["tokens"]

    def reset(self):
        with self._lock:
            self._slots.clear()
            self._conv.clear()


class _RedisStore(GovernanceStore):
    # Atomic slot acquire: cap check + add + ttl, all in one Lua call.
    _SLOT_LUA = """
    if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
    redis.call('SADD', KEYS[1], ARGV[2])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    return 1
    """
    _SLOT_REL_LUA = "return redis.call('SREM', KEYS[1], ARGV[1])"

    def __init__(self, client):
        self._r = client
        self._slot_key = "collab:global:slots"
        self._lock = threading.Lock()
        self._tokens: set[str] = set()  # owner tokens this process has acquired

    def acquire_global_slot(self, limit, ttl=DEFAULT_TTL_S):
        tok = uuid.uuid4().hex
        try:
            res = self._r.eval(self._SLOT_LUA, 1, self._slot_key, str(limit), tok, str(ttl))
            if res:
                with self._lock:
                    self._tokens.add(tok)
                return True
            return False
        except Exception as e:  # pragma: no cover - network
            logger.warning("governance: redis slot acquire failed, fail-closed: %s", e)
            return False

    def release_global_slot(self):
        with self._lock:
            tok = self._tokens.pop() if self._tokens else None
        if tok:
            try:
                self._r.eval(self._SLOT_REL_LUA, 1, self._slot_key, tok)
            except Exception as e:  # pragma: no cover
                logger.warning("governance: redis slot release failed: %s", e)

    def global_active_count(self):
        try:
            return int(self._r.scard(self._slot_key) or 0)
        except Exception:  # pragma: no cover
            return 0

    def conv_dedup_seen(self, conv_id, key, ttl=DEFAULT_TTL_S):
        k = f"collab:conv:{conv_id}:seen"
        try:
            added = self._r.sadd(k, key)
            self._r.expire(k, ttl)
            return added == 1
        except Exception as e:  # pragma: no cover - fail-open (soft guard)
            logger.warning("governance: redis dedup failed, fail-open: %s", e)
            return True

    def conv_child_count(self, conv_id, requesting, ttl=DEFAULT_TTL_S):
        k = f"collab:conv:{conv_id}:children"
        try:
            n = self._r.hincrby(k, requesting, 1)
            self._r.expire(k, ttl)
            return int(n)
        except Exception as e:  # pragma: no cover - fail-open
            logger.warning("governance: redis child-count failed, fail-open: %s", e)
            return 1

    def conv_token_total(self, conv_id, ttl=DEFAULT_TTL_S):
        k = f"collab:conv:{conv_id}:tokens"
        try:
            return int(self._r.get(k) or 0)
        except Exception:  # pragma: no cover
            return 0

    def conv_token_add(self, conv_id, n, ttl=DEFAULT_TTL_S):
        k = f"collab:conv:{conv_id}:tokens"
        try:
            total = self._r.incrby(k, n)
            self._r.expire(k, ttl)
            return int(total)
        except Exception as e:  # pragma: no cover - fail-open
            logger.warning("governance: redis token-add failed, fail-open: %s", e)
            return n

    def reset(self):
        try:
            for k in self._r.scan_iter(match="collab:*"):
                self._r.delete(k)
        except Exception as e:  # pragma: no cover
            logger.warning("governance: redis reset failed: %s", e)


_STORE: "GovernanceStore | None" = None
_STORE_LOCK = threading.Lock()


def get_store() -> GovernanceStore:
    """Return a process-wide store. Redis-backed if reachable, else in-process."""
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        client = None
        try:
            client = get_redis()
            # A client that CONNECTS but cannot AUTHENTICATE (or otherwise fails
            # a ping) is NOT usable. Treat it as unavailable so governance
            # degrades to the in-process store instead of silently failing open
            # on every soft guard — or, worse, fail-closed on the hard slot gate
            # — against a misconfigured Redis. This matches the documented design
            # goal: "if Redis is down, governance degrades to in-process".
            if client is not None:
                try:
                    if not client.ping():
                        client = None
                except Exception as e:  # pragma: no cover - auth/connect error
                    logger.warning(
                        "governance: redis reachable but unusable, in-process fallback: %s", e
                    )
                    client = None
        except Exception as e:  # pragma: no cover - client construction error
            logger.warning("governance: redis unavailable, in-process fallback: %s", e)
            client = None
        _STORE = _RedisStore(client) if client else _InProcessStore()
        return _STORE


def reset_store() -> None:
    """Test helper — drop the cached store and clear its state."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            try:
                _STORE.reset()
            except Exception:
                pass
        _STORE = None
