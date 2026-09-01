"""Q→SQL example CRUD + retrieval service."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.q_sql_example import QSqlExample
from app.services.retrieval.in_memory import InMemoryRetriever

logger = logging.getLogger(__name__)


class QSqlExampleService:
    """Manage Q→SQL training-pair examples with semantic retrieval."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ── CRUD ───────────────────────────────────────────────────────────

    def add(
        self,
        question: str,
        sql: str,
        datasource_id: str | None = None,
        agent_id: str | None = None,
    ) -> QSqlExample:
        """Insert a new Q→SQL example pair."""
        entry = QSqlExample(
            question=question,
            sql=sql,
            datasource_id=datasource_id,
            agent_id=agent_id,
            embedding_text=question,
            enabled=True,
        )
        self._db.add(entry)
        self._db.commit()
        return entry

    def list_for_datasource(
        self, datasource_id: str
    ) -> list[QSqlExample]:
        """Return all enabled examples for a datasource."""
        return (
            self._db.query(QSqlExample)
            .filter(
                QSqlExample.datasource_id == datasource_id,
                QSqlExample.is_deleted == False,
                QSqlExample.enabled == True,
            )
            .all()
        )

    def top_k(
        self,
        question: str,
        datasource_id: str,
        k: int = 3,
    ) -> list[tuple[str, str, float]]:
        """Semantic search for the top-k most similar Q→SQL pairs.

        Returns ``[(question, sql, score), ...]`` sorted by descending similarity.
        """
        rows = self.list_for_datasource(datasource_id)
        if not rows:
            return []

        retriever = InMemoryRetriever(dim=128)
        for r in rows:
            retriever.index(r.question, "")

        results = retriever.query(question, top_k=k)
        sql_map = {r.question: r.sql for r in rows}
        return [
            (text, sql_map.get(text, ""), score)
            for text, score in results
        ]
