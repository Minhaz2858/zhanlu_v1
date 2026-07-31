"""QueryService — wraps a connector for safe SQL execution.

In v1 we trust the LLM (per product decision) but enforce two
self-contained caps: `max_rows=1000` and `timeout_s=10` per statement.
These are documented as safety nets, not security controls.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_S = 10


class QueryService:
    """Run a SQL statement against a KnowledgeBase and return rows."""

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

    def execute(
        self,
        kb_id: str,
        sql: str,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Run `sql` against the KB and return rows + metadata.

        Returns:
            {
                "source": {"id", "name", "db_type"},
                "sql": str,
                "rows": [...],
                "row_count": int,
                "truncated": bool,
                "elapsed_ms": int,
            }
        """
        import time
        if not sql or not sql.strip():
            raise ValueError("Empty SQL")
        # Strip trailing semicolons (some drivers reject multi-statements)
        sql = sql.strip().rstrip(";").strip()

        kb = self._load_kb(kb_id)
        t0 = time.time()
        with get_connector(kb) as conn:
            rows = conn.execute(sql, max_rows=max_rows, timeout_s=timeout_s)
        elapsed_ms = int((time.time() - t0) * 1000)

        truncated = len(rows) >= max_rows
        return {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "sql": sql,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }
