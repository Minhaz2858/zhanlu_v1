"""Catalog triggers — lifecycle hooks for the semantic catalog indexer.

When SEMANTIC_CATALOG_ENABLED is True, these fire on KB create/update/reindex
and spawn a background catalog index pass for database-type KBs.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from app.config import settings
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

# Connection fields that, when changed, should trigger catalog reindex.
# Changing name/description/status should NOT reindex.
_CONNECTION_FIELDS = frozenset({
    "db_type", "host", "port", "database_name", "username", "password", "api_url",
})

# Per-KB in-process locks — prevent overlapping indexing jobs when the user
# clicks "Refresh" multiple times rapidly.  Maps kb_id → asyncio.Lock (for
# async contexts) / threading.Lock (for sync contexts).  Entries are pruned
# by the watchdog after the indexing completes.
_async_locks: dict[str, asyncio.Lock] = {}
_thread_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_async_lock(kb_id: str) -> asyncio.Lock:
    """Get or create the asyncio.Lock for *kb_id* (async contexts)."""
    with _locks_guard:
        lock = _async_locks.get(kb_id)
        if lock is None:
            lock = asyncio.Lock()
            _async_locks[kb_id] = lock
        return lock


def _get_thread_lock(kb_id: str) -> threading.Lock:
    """Get or create the threading.Lock for *kb_id* (sync contexts)."""
    with _locks_guard:
        lock = _thread_locks.get(kb_id)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[kb_id] = lock
        return lock


def release_kb_lock(kb_id: str) -> None:
    """Drop the per-KB lock entries (call after indexing completes)."""
    with _locks_guard:
        _async_locks.pop(kb_id, None)
        _thread_locks.pop(kb_id, None)


def _should_index(kb: KnowledgeBase) -> bool:
    """Shared guard: feature flag on + supported db_type + not already indexing."""
    if not settings.SEMANTIC_CATALOG_ENABLED:
        return False
    if (kb.db_type or "").lower() not in ("mysql", "postgres", "postgresql"):
        return False
    if kb.catalog_status == "indexing":
        logger.info("catalog_triggers: kb=%s already indexing — skipping", kb.id)
        return False
    return True


def connection_fields_changed(payload: dict, prev_record: dict | None) -> bool:
    """Return True if any connection field in payload differs from prev_record."""
    if prev_record is None:
        return True
    for f in _CONNECTION_FIELDS:
        new_val = payload.get(f)
        old_val = prev_record.get(f)
        if new_val != old_val:
            return True
    return False


async def maybe_reindex_catalog(kb: KnowledgeBase) -> None:
    """Fire-and-forget catalog reindex for a database KB.

    Only spawns when the feature flag is on and kb.db_type is supported.
    Caller does NOT need to await — this is a background task.
    """
    if not _should_index(kb):
        return

    lock = _get_async_lock(kb.id)
    if lock.locked():
        logger.info("catalog_triggers: kb=%s lock already held — skipping", kb.id)
        return

    logger.info("catalog_triggers: spawning catalog index for kb=%s", kb.id)
    asyncio.create_task(_reindex_catalog_safe(kb.id))


def maybe_reindex_catalog_bg(kb: KnowledgeBase) -> None:
    """Sync-context variant: spawns a daemon thread running asyncio.run().

    Use from threadpool/sync endpoints (entities CRUD) where no event loop
    is available for asyncio.create_task.  Same guards as maybe_reindex_catalog.
    """
    if not _should_index(kb):
        return

    lock = _get_thread_lock(kb.id)
    if lock.locked():
        logger.info("catalog_triggers: kb=%s lock already held — skipping", kb.id)
        return

    logger.info("catalog_triggers: spawning catalog index (bg thread) for kb=%s", kb.id)
    t = threading.Thread(
        target=lambda: asyncio.run(_reindex_catalog_safe(kb.id)),
        daemon=True,
    )
    t.start()


async def _reindex_catalog_safe(kb_id: str) -> None:
    """Background task: reindex catalog, handling its own session lifecycle."""
    from sqlalchemy.orm import Session
    from app.deps import get_db

    lock = _get_async_lock(kb_id)
    async with lock:
        try:
            db: Session = next(get_db())
            try:
                kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
                if kb is None:
                    logger.warning("catalog_triggers: kb=%s not found", kb_id)
                    return
                from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
                await index_kb_catalog(kb, db)
            finally:
                db.close()
        except Exception:
            logger.exception("catalog_triggers: background reindex failed for kb=%s", kb_id)
        finally:
            release_kb_lock(kb_id)
