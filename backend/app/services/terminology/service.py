"""Terminology CRUD + semantic search service."""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.terminology import Terminology
from app.services.retrieval.in_memory import InMemoryRetriever

logger = logging.getLogger(__name__)


class TerminologyService:
    """Manage business-glossary entries with semantic retrieval."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ── CRUD ───────────────────────────────────────────────────────────

    def upsert(
        self,
        word: str,
        description: str = "",
        *,
        datasource_ids: list[str] | None = None,
        agent_id: str | None = None,
        parent_id: str | None = None,
    ) -> Terminology:
        """Insert or update a terminology entry by word + datasource_ids scope."""
        existing = (
            self._db.query(Terminology)
            .filter(
                Terminology.word == word,
                Terminology.is_deleted == False,
            )
            .first()
        )

        if existing:
            existing.description = description
            if datasource_ids is not None:
                existing.datasource_ids = json.dumps(datasource_ids)
            existing.agent_id = agent_id
            existing.parent_id = parent_id
            existing.embedding_text = f"{word}: {description}"
            self._db.commit()
            return existing

        entry = Terminology(
            word=word,
            description=description,
            datasource_ids=json.dumps(datasource_ids) if datasource_ids else None,
            agent_id=agent_id,
            parent_id=parent_id,
            embedding_text=f"{word}: {description}",
            enabled=True,
        )
        self._db.add(entry)
        self._db.commit()
        return entry

    def list_for_datasource(self, datasource_id: str) -> list[Terminology]:
        """Return all enabled terminology entries for a datasource."""
        rows = (
            self._db.query(Terminology)
            .filter(
                Terminology.is_deleted == False,
                Terminology.enabled == True,
            )
            .all()
        )
        # Filter in Python because datasource_ids is a JSON text column
        result: list[Terminology] = []
        for row in rows:
            ds_ids = _parse_json_list(row.datasource_ids)
            if not ds_ids or datasource_id in ds_ids:
                result.append(row)
        return result

    def search_by_word(
        self,
        query: str,
        datasource_id: str,
        top_k: int = 3,
    ) -> list[tuple[str, str, float]]:
        """Semantic search over terminology entries.

        Returns ``[(word, description, score), ...]`` sorted by descending similarity.
        """
        entries = self.list_for_datasource(datasource_id)
        if not entries:
            return []

        retriever = InMemoryRetriever(dim=128)
        for e in entries:
            retriever.index(e.word, e.description)

        results = retriever.query(query, top_k=top_k)
        # Map text back to (word, description, score)
        desc_map = {e.word: e.description for e in entries}
        return [
            (text, desc_map.get(text, ""), score)
            for text, score in results
        ]


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v) for v in val]
    except (json.JSONDecodeError, TypeError):
        pass
    return []
