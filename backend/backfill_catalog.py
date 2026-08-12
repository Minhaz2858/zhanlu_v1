"""One-off backfill: run catalog indexer for all existing DB KBs stuck in pending/error.

Usage:
  docker exec zhanlu-backend python -m backend.backfill_catalog

This is a one-shot migration.  After running it, all supported database-type
KnowledgeBases that were in 'pending' or 'error' catalog_status will have
their catalog discovered and status set to 'ready' (or 'error' if connection
fails).
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.deps import get_db
from app.models.knowledge_base import KnowledgeBase
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill_catalog")

SUPPORTED_DB_TYPES = {"mysql", "postgres", "postgresql"}
STATUSES_TO_BACKFILL = {"pending", "error"}


async def _index_one(kb_id: str) -> None:
    """Index a single KB: open session, fetch, run catalog indexer."""
    from app.services.knowledge_graph.catalog_indexer import index_kb_catalog

    db: Session = next(get_db())
    try:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        ).first()
        if kb is None:
            logger.warning("KB %s not found — skipped", kb_id)
            return
        if kb.catalog_status == "indexing":
            logger.info("KB %s (%s) already indexing — skipped", kb.id, kb.name)
            return
        logger.info("KB %s (%s) → starting catalog index", kb.id, kb.name)
        await index_kb_catalog(kb, db)
        logger.info("KB %s (%s) → done (status=%s, item_count=%d)", kb.id, kb.name, kb.catalog_status, kb.item_count)
    except Exception:
        logger.exception("KB %s → failed", kb_id)
    finally:
        db.close()


async def main() -> None:
    if not settings.SEMANTIC_CATALOG_ENABLED:
        logger.warning("SEMANTIC_CATALOG_ENABLED is False — enabling temporarily for backfill")
        # We don't modify settings here; the backfill script triggers indexing directly

    db: Session = next(get_db())
    try:
        candidates = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.db_type.in_(SUPPORTED_DB_TYPES),
                KnowledgeBase.catalog_status.in_(STATUSES_TO_BACKFILL),
            )
            .all()
        )
        logger.info("Found %d KB(s) to backfill", len(candidates))
        for kb in candidates:
            logger.info("  - %s (%s) [status=%s, db_type=%s]", kb.id, kb.name, kb.catalog_status, kb.db_type)
    finally:
        db.close()

    if not candidates:
        logger.info("Nothing to backfill.")
        return

    for kb in candidates:
        await _index_one(kb.id)
        # Small pause between KBs to avoid hammering DB connections
        await asyncio.sleep(0.5)

    logger.info("Backfill complete — %d KB(s) processed", len(candidates))


if __name__ == "__main__":
    asyncio.run(main())
