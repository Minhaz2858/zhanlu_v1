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


def connection_fingerprint(db: Session, kb_id: str) -> str:
    """Stable fingerprint of a KB's connection identity.

    Used in every schema cache key so that re-pointing a KnowledgeBase at a
    DIFFERENT database (same kb_id, new host/port/db) is an automatic cache
    miss — the agent can never be served the old database's table names.

    Identity = (db_type, host, port, database_name, schema). The password is
    deliberately excluded: a credential rotation must not invalidate the
    schema cache, and a secret must never appear in a cache key.

    Fail-soft: any error or missing row returns "" so callers degrade to the
    legacy kb_id-only key instead of raising into the hot path.
    """
    try:
        kb = db.get(KnowledgeBase, kb_id) if db is not None else None
        if kb is None:
            return ""
        parts = [
            kb.db_type or "",
            kb.host or "",
            str(kb.port or ""),
            kb.database_name or "",
            kb.schema or "",
        ]
        if not any(parts):
            return ""
        import hashlib

        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


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
        fp = connection_fingerprint(self._db, kb_id)
        key = (kb_id, fp, "list_tables")
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
        fp = connection_fingerprint(self._db, kb_id)
        key = (kb_id, fp, "table", table)
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
        # Generic freshness: annotate the single table so the agent knows
        # whether it still receives data (works for any connected DB).
        try:
            from app.services.knowledge_graph.freshness import (
                annotate_tables, stale_flag,
            )
            _probe = [{"table_name": table, "columns": columns}]
            annotate_tables(self._db, kb_id, _probe)
            if _probe[0].get("last_data_date"):
                result["freshness"] = {
                    "last_data_date": _probe[0]["last_data_date"],
                    "stale_days": _probe[0].get("stale_days"),
                    "flag": stale_flag(_probe[0]),
                }
        except Exception:
            pass
        _cache_put(key, result)
        return result

    def describe_all(self, kb_id: str, max_tables: int = 50) -> dict:
        """Return schema for every table (capped at max_tables).

        ``all_table_names`` always carries the FULL table list (names only —
        one cheap query, no per-table introspection), so the agent knows every
        table exists even when the detailed entries are capped. Without this,
        alphabetical truncation hides business views past the cap and the LLM
        guesses table names instead of describing them.
        """
        key = (kb_id, "all", max_tables)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            all_names = conn.list_tables()
            tables = all_names[:max_tables]
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
        # Generic freshness: probe MAX(date) concurrently (TTL-cached,
        # fail-soft) so STALE tables are flagged in the schema the LLM sees.
        try:
            from app.services.knowledge_graph.freshness import (
                annotate_tables_parallel, stale_flag,
            )
            annotate_tables_parallel(self._db, kb_id, out)
            for _t in out:
                if _t.get("last_data_date"):
                    _t["freshness"] = {
                        "last_data_date": _t["last_data_date"],
                        "stale_days": _t.get("stale_days"),
                        "flag": stale_flag(_t),
                    }
        except Exception as exc:
            logger.debug("describe_all: freshness annotation skipped: %s", exc)
        result = {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "tables": out,
            "all_table_names": all_names,
            "truncated": len(tables) >= max_tables,
        }
        _cache_put(key, result)
        return result
