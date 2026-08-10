"""Catalog triggers — lifecycle hooks for the semantic catalog indexer.

When SEMANTIC_CATALOG_ENABLED is True, these fire on KB create/update/reindex
and spawn a background catalog index pass for database-type KBs.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)


async def maybe_reindex_catalog(kb: KnowledgeBase) -> None:
    """Fire-and-forget catalog reindex for a database KB.

    Only spawns when the feature flag is on and kb.db_type is supported.
    Caller does NOT need to await — this is a background task.
    """
    if not settings.SEMANTIC_CATALOG_ENABLED:
        return
    if (kb.db_type or "").lower() not in ("mysql", "postgres", "postgresql"):
        return
    if kb.catalog_status == "indexing":
        logger.info("catalog_triggers: kb=%s already indexing — skipping", kb.id)
        return

    logger.info("catalog_triggers: spawning catalog index for kb=%s", kb.id)
    asyncio.create_task(_reindex_catalog_safe(kb.id))


async def _reindex_catalog_safe(kb_id: str) -> None:
    """Background task: reindex catalog, handling its own session lifecycle."""
    from sqlalchemy.orm import Session
    from app.deps import get_db

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
