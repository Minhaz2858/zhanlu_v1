"""Catalog triggers — lifecycle hooks for the semantic catalog indexer.

When SEMANTIC_CATALOG_ENABLED is True, these fire on KB create/update/reindex
and spawn a background catalog index pass for database-type KBs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.config import settings
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

# Connection fields that, when changed, should trigger catalog reindex.
# Changing name/description/status should NOT reindex.
_CONNECTION_FIELDS = frozenset({
    "db_type", "host", "port", "database_name", "username", "password", "api_url",
})


# ── dialect support — THE single source of truth ────────────────────────────
# Catalog indexing is dialect-agnostic: the indexer drives everything through
# get_connector(kb) (table list, describe_table, FKs, row counts), so any
# dialect the connector factory can talk to can be catalogued.
#
# This used to be a hardcoded ("mysql","postgres","postgresql") tuple repeated
# in FIVE places (this module, the entities create hook, the entities update
# hook, the /catalog/reindex endpoint and the frontend), which meant a user
# adding MariaDB / SQL Server / Oracle / SQLite got no auto-index, no UI index
# call, and an HTTP 400 on Refresh — their database was never catalogued.
# Everything now derives from the connector registry, so adding a dialect to
# connector_factory. _REGISTRY automatically enables catalog indexing for it.

def kb_field(kb, name: str, default=None) -> Any:
    """Read a field from a KnowledgeBase ORM row OR its ``to_dict()`` payload.

    Entity-CRUD hooks hand us the serialized **dict** (``create_record``
    returns ``record.to_dict()``) while background jobs hand us the **ORM row**.
    Both shapes must work: reading ``getattr(record, "db_type", "")`` off a dict
    silently yields ``""``, which is what made the auto-index a no-op for every
    KB created through the generic entity API (the UI masked it by calling
    /catalog/reindex itself).
    """
    if isinstance(kb, dict):
        return kb.get(name, default)
    return getattr(kb, name, default)


def supports_catalog(db_type: str | None) -> bool:
    """True when the semantic catalog can index this dialect.

    Derived from the connector registry — never a hardcoded tuple. Callers must
    use this instead of comparing db_type themselves.
    """
    dt = (db_type or "").strip().lower()
    if not dt:
        return False
    try:
        from app.services.db.connector_factory import supported_db_types
        return dt in supported_db_types()
    except Exception:
        logger.warning("supports_catalog: connector registry unavailable", exc_info=True)
        return False


def catalog_db_types() -> list[str]:
    """Dialects the semantic catalog can index (exposed via /capabilities)."""
    try:
        from app.services.db.connector_factory import supported_db_types
        return supported_db_types()
    except Exception:
        logger.warning("catalog_db_types: connector registry unavailable", exc_info=True)
        return []


def is_catalogable_record(record) -> bool:
    """Feature flag on + supported dialect, for an ORM row OR a dict payload.

    The one decision every entry point (entity create/update hooks, manual
    reindex) should ask. Shape-agnostic on purpose — see ``kb_field``.
    """
    if not settings.SEMANTIC_CATALOG_ENABLED:
        return False
    return supports_catalog(kb_field(record, "db_type"))

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


def _should_index(kb) -> bool:
    """Shared guard: feature flag on + supported db_type + not already indexing.

    Accepts a KnowledgeBase ORM row or its ``to_dict()`` dict (see ``kb_field``).
    """
    if not settings.SEMANTIC_CATALOG_ENABLED:
        return False
    db_type = kb_field(kb, "db_type")
    if not supports_catalog(db_type):
        logger.info(
            "catalog_triggers: kb=%s db_type=%r not catalogable — skipping",
            kb_field(kb, "id"), db_type,
        )
        return False
    if kb_field(kb, "catalog_status") == "indexing":
        logger.info("catalog_triggers: kb=%s already indexing — skipping", kb_field(kb, "id"))
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


async def maybe_reindex_catalog(kb) -> None:
    """Fire-and-forget catalog reindex for a database KB.

    Only spawns when the feature flag is on and kb.db_type is supported.
    Caller does NOT need to await — this is a background task.
    Accepts an ORM row or its ``to_dict()`` dict.
    """
    if not _should_index(kb):
        return

    kb_id = kb_field(kb, "id")
    if not kb_id:
        logger.warning("catalog_triggers: record has no id — skipping")
        return
    kb_id = str(kb_id)
    lock = _get_async_lock(kb_id)
    if lock.locked():
        logger.info("catalog_triggers: kb=%s lock already held — skipping", kb_id)
        return

    logger.info("catalog_triggers: spawning catalog index for kb=%s", kb_id)
    asyncio.create_task(_reindex_catalog_safe(kb_id))


def maybe_reindex_catalog_bg(kb) -> None:
    """Sync-context variant: spawns a daemon thread running asyncio.run().

    Use from threadpool/sync endpoints (entities CRUD) where no event loop
    is available for asyncio.create_task.  Same guards as maybe_reindex_catalog.
    Accepts an ORM row or its ``to_dict()`` dict.
    """
    if not _should_index(kb):
        return

    kb_id = kb_field(kb, "id")
    if not kb_id:
        logger.warning("catalog_triggers: record has no id — skipping")
        return
    kb_id = str(kb_id)
    lock = _get_thread_lock(kb_id)
    if lock.locked():
        logger.info("catalog_triggers: kb=%s lock already held — skipping", kb_id)
        return

    logger.info("catalog_triggers: spawning catalog index (bg thread) for kb=%s", kb_id)
    t = threading.Thread(
        target=lambda: asyncio.run(_reindex_catalog_safe(kb_id)),
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
                # Lifecycle entry point: a source that has been asked to
                # prepare itself is CONNECTED/queued, not implicitly ready.
                # The indexer advances it from here (discovering → indexed →
                # enriched → validated → nl2sql_ready) and the readiness gate
                # refuses to answer from it until it gets there.
                try:
                    from app.services.db import readiness as src_readiness

                    src_readiness.set_state(
                        db, kb, src_readiness.STATE_CONNECTED,
                        reason="preparation queued",
                    )
                except Exception:
                    logger.debug("catalog_triggers: readiness init skipped", exc_info=True)
                from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
                await index_kb_catalog(kb, db)
            finally:
                db.close()
        except Exception:
            logger.exception("catalog_triggers: background reindex failed for kb=%s", kb_id)
        finally:
            release_kb_lock(kb_id)
