"""SchemaService — wraps a connector for schema introspection.

Used by the `describe_schema` and `answer_from_database` tools.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process TTL cache for live schema introspection.
#
# describe_all() on a remote warehouse takes seconds (one round trip per
# table) and was repeated on EVERY query. Schema rarely changes, so we cache
# results per KB for SCHEMA_CACHE_TTL_SECONDS (default 1h, 0 disables).
# Entries are keyed by (kb_id, op, *args). Thread-safe: callers run inside
# asyncio.to_thread worker threads.
# ---------------------------------------------------------------------------
_SCHEMA_CACHE: dict[tuple, tuple[float, dict]] = {}
_SCHEMA_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> dict | None:
    ttl = settings.SCHEMA_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _SCHEMA_CACHE_LOCK:
        hit = _SCHEMA_CACHE.get(key)
        if hit is None:
            return None
        ts, value = hit
        if now - ts > ttl:
            _SCHEMA_CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple, value: dict) -> None:
    if settings.SCHEMA_CACHE_TTL_SECONDS <= 0:
        return
    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE[key] = (time.monotonic(), value)


def invalidate_schema_cache(kb_id: str | None = None) -> None:
    """Drop cached schema entries — one KB, or everything when kb_id is None."""
    with _SCHEMA_CACHE_LOCK:
        if kb_id is None:
            _SCHEMA_CACHE.clear()
            return
        for key in [k for k in _SCHEMA_CACHE if k and k[0] == kb_id]:
            _SCHEMA_CACHE.pop(key, None)


class SchemaService:
    """High-level schema introspection for a KnowledgeBase.

    All methods are sync — callers in async code should wrap with
    `asyncio.to_thread()`.
    """

    def __init__(self, db: Session):
        self._db = db

    def _load_kb(self, kb_id: str) -> KnowledgeBase:
        kb = self._db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        if not kb:
            raise ValueError(f"KnowledgeBase not found: {kb_id}")
        if (kb.source_kind or "").lower() != "database":
            raise ValueError(
                f"KnowledgeBase {kb_id!r} is not a database source "
                f"(source_kind={kb.source_kind!r})"
            )
        return kb

    def list_tables(self, kb_id: str) -> dict:
        """Return {"source": {...}, "tables": [...]} for a KB."""
        key = (kb_id, "list_tables")
        cached = _cache_get(key)
        if cached is not None:
            return cached
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            tables = conn.list_tables()
        result = {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
                "database_name": kb.database_name,
            },
            "tables": tables,
        }
        _cache_put(key, result)
        return result

    def describe_table(self, kb_id: str, table: str) -> dict:
        """Return full column metadata for one table."""
        key = (kb_id, "table", table)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            columns = conn.describe_table(table)
        result = {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "table": table,
            "columns": columns,
        }
        _cache_put(key, result)
        return result

    def describe_all(self, kb_id: str, max_tables: int = 50) -> dict:
        """Return schema for every table (capped at max_tables)."""
        key = (kb_id, "all", max_tables)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            tables = conn.list_tables()[:max_tables]
            out = []
            for t in tables:
                try:
                    out.append({
                        "table": t,
                        "columns": conn.describe_table(t),
                    })
                except Exception as e:
                    logger.debug("describe_all: skipping %s: %s", t, e)
                    out.append({"table": t, "error": str(e)})
        result = {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "tables": out,
            "truncated": len(tables) >= max_tables,
        }
        _cache_put(key, result)
        return result
