"""Q→SQL example CRUD + retrieval service."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.q_sql_example import QSqlExample
from app.config import settings
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

    # ── OCI feedback loop: auto-promotion (2026-09-22) ────────────────────

    @staticmethod
    def _sql_hash(sql: str) -> str:
        """Stable dedupe key over whitespace-normalised, case-insensitive SQL.
        Case-insensitivity is deliberate: the same statement re-emitted with
        different keyword casing must not multiply examples."""
        import hashlib
        import re as _re

        normalized = _re.sub(r"\s+", " ", (sql or "").strip()).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def maybe_auto_promote(
        cls,
        db: Session,
        *,
        question: str,
        sql: str,
        datasource_id: str | None,
        agent_id: str | None = None,
    ) -> bool:
        """Promote a VERIFIED successful Q→SQL run into the few-shot corpus.

        Called by NLAnswerService only when the answer passed the deterministic
        checks (execution OK, non-empty rows, no repair loop) — learning from
        an unchecked answer would make the platform consistently wrong.

        Rules:
        * Flag-gated (NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED, default False).
        * Dedupe: one (datasource, sql_hash) row — a re-asked question does
          not multiply examples.
        * A manually authored row is never overwritten by this path.
        * Per-datasource cap (NL2SQL_EXAMPLE_AUTO_PROMOTION_MAX, default 100)
          keeps the retriever's top-k meaningful.

        Returns True when a new example row was inserted. Never raises.
        """
        try:
            if not getattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED", False):
                return False
            if not (question or "").strip() or not (sql or "").strip():
                return False
            digest = cls._sql_hash(sql)
            q = db.query(QSqlExample).filter(
                QSqlExample.is_deleted == False,  # noqa: E712
                QSqlExample.sql_hash == digest,
            )
            if datasource_id:
                q = q.filter(QSqlExample.datasource_id == datasource_id)
            else:
                q = q.filter(QSqlExample.datasource_id.is_(None))
            if q.first() is not None:
                return False
            scope = db.query(QSqlExample).filter(
                QSqlExample.is_deleted == False,  # noqa: E712
            )
            if datasource_id:
                scope = scope.filter(QSqlExample.datasource_id == datasource_id)
            else:
                scope = scope.filter(QSqlExample.datasource_id.is_(None))
            count = scope.count()
            cap = int(getattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_MAX", 100))
            if count >= cap:
                return False
            entry = QSqlExample(
                question=question.strip()[:2000],
                sql=sql.strip(),
                datasource_id=datasource_id,
                agent_id=agent_id,
                embedding_text=question.strip()[:2000],
                enabled=True,
                source="workload",
                sql_hash=digest,
            )
            db.add(entry)
            db.commit()
            logger.info(
                "QSqlExample: auto-promoted verified example for ds=%s "
                "(%d/%d slots used)",
                datasource_id, count + 1, cap,
            )
            return True
        except Exception as e:
            logger.debug("QSqlExample: auto-promotion skipped: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
            return False
