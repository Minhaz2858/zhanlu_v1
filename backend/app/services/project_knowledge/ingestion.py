"""Ingestion orchestrator -- runs the 4-step pipeline for one (project, KB) pair.

Pipeline:
1. catalog_indexer.index_kb_catalog(kb, db)        [existing, flag-gated]
2. entity_linker.seed_products_as_entities(...)     [new]
3. entity_linker.link_entities_to_catalog_for_project(...) [new]
4. registry_indexer.index_knowledge_base(...)       [existing, flag-gated]

Every step is independently flag-gated. Failures in steps 2-4 produce
CacheStatus.partial; only step 1 failure is propagated.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase

from .entity_linker import (
    link_entities_to_catalog_for_project,
    seed_products_as_entities,
)
from .models import CacheStatus

logger = logging.getLogger(__name__)


def _get_kb_or_none(db: Session, kb_id: str) -> KnowledgeBase | None:
    if not kb_id:
        return None
    return (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .first()
    )


async def ingest_for_project(
    project_id: str, kb_id: str, db: Session
) -> CacheStatus:
    """Idempotent ingestion. Never raises into the caller."""
    t0 = time.monotonic()
    status = CacheStatus(status="indexing")

    if not getattr(settings, "PROJECT_KNOWLEDGE_CACHE_ENABLED", False):
        status.status = "pending"
        status.error = "PROJECT_KNOWLEDGE_CACHE_ENABLED is False"
        return status

    kb = _get_kb_or_none(db, kb_id)
    if kb is None:
        status.status = "error"
        status.error = f"KB {kb_id} not found"
        return status

    # 1. catalog — skip if already ready, run only if pending/error
    try:
        from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
        # If the KB is already catalogued and complete, don't re-run the
        # (expensive) introspection+LLM description step. Re-run only when
        # the catalog is in a non-terminal state.
        _current_status = getattr(kb, "catalog_status", None) or "pending"
        if _current_status not in ("ready",):
            await index_kb_catalog(kb, db)
        else:
            logger.info(
                "ingest_for_project: catalog already ready (%d tables); skipping",
                getattr(kb, "item_count", 0) or 0,
            )
    except Exception as e:
        logger.exception("ingest_for_project: catalog step failed")
        status.status = "error"
        status.error = f"catalog: {e}"
        return status

    # 2. seed
    try:
        ents = await asyncio.to_thread(seed_products_as_entities, db, project_id)
        status.entities = ents
    except Exception as e:
        logger.warning("ingest_for_project: seed step failed (non-fatal): %s", e)

    # 3. link
    try:
        links = await asyncio.to_thread(
            link_entities_to_catalog_for_project, db, project_id, kb_id
        )
        status.links = links
    except Exception as e:
        logger.warning("ingest_for_project: link step failed (non-fatal): %s", e)

    # 4. registry
    try:
        from app.services.knowledge_graph.registry_indexer import index_knowledge_base
        if getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False):
            await asyncio.to_thread(
                index_knowledge_base,
                db,
                kb,
                project_id=project_id,
                table_count=kb.item_count or 0,
            )
    except Exception as e:
        logger.warning("ingest_for_project: registry step failed (non-fatal): %s", e)

    # read catalog table count for stats
    try:
        from app.models.knowledge_catalog import KBTableMeta
        status.tables = (
            db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted == False)  # noqa: E712
            .count()
        )
    except Exception:
        pass

    try:
        db.commit()
    except Exception as e:
        logger.warning("ingest_for_project: commit failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    status.status = "ready" if status.entities and status.tables else "partial"
    status.elapsed_s = round(time.monotonic() - t0, 3)
    logger.info(
        "ingest_for_project: project=%s kb=%s -> %s (tables=%d entities=%d links=%d in %.2fs)",
        project_id, kb_id, status.status, status.tables, status.entities, status.links, status.elapsed_s,
    )
    return status


__all__ = ["ingest_for_project"]
