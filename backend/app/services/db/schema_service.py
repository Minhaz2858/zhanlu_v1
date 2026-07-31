"""SchemaService — wraps a connector for schema introspection.

Used by the `describe_schema` and `answer_from_database` tools.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

logger = logging.getLogger(__name__)


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
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            tables = conn.list_tables()
        return {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
                "database_name": kb.database_name,
            },
            "tables": tables,
        }

    def describe_table(self, kb_id: str, table: str) -> dict:
        """Return full column metadata for one table."""
        kb = self._load_kb(kb_id)
        with get_connector(kb) as conn:
            columns = conn.describe_table(table)
        return {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "table": table,
            "columns": columns,
        }

    def describe_all(self, kb_id: str, max_tables: int = 50) -> dict:
        """Return schema for every table (capped at max_tables)."""
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
        return {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "tables": out,
            "truncated": len(tables) >= max_tables,
        }
