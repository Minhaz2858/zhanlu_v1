"""Catalog indexer watchdog — recovers from stuck indexing status.

If the catalog indexer background task dies silently (container restart, OOM,
exception in a daemon thread, etc.), the KB's ``catalog_status`` stays at
``"indexing"`` forever and the UI shows "索引中" indefinitely.

This watchdog scans for KBs stuck at ``"indexing"`` for longer than
``STUCK_AFTER_SECONDS`` (default 10 minutes) and resets them:

* If the KB already has table metadata (``kb_table_meta`` rows) → reset to
  ``"ready"`` (the previous run completed enough to be usable).
* Otherwise → reset to ``"error"`` so the user can retry.

Runs as an asyncio task started from ``main.py`` lifespan.  Polls every
``POLL_INTERVAL_SECONDS`` (default 60s).  Self-disables when
``SEMANTIC_CATALOG_ENABLED`` is False.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta

logger = logging.getLogger(__name__)
# Ensure INFO logs are visible even when the root logger is at WARNING.
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

POLL_INTERVAL_SECONDS = 60
STUCK_AFTER_SECONDS = 600  # 10 minutes


async def run_catalog_watchdog(stop_event: asyncio.Event) -> None:
    """Run the watchdog loop until ``stop_event`` is set.

    Should be launched as ``asyncio.create_task(run_catalog_watchdog(...))``
    from the FastAPI lifespan.
    """
    logger.info("catalog_watchdog: starting (poll=%ds, stuck_after=%ds)",
                POLL_INTERVAL_SECONDS, STUCK_AFTER_SECONDS)
    while not stop_event.is_set():
        try:
            if settings.SEMANTIC_CATALOG_ENABLED:
                await asyncio.to_thread(_scan_and_reset_stuck)
        except Exception:
            logger.exception("catalog_watchdog: scan iteration failed")
        # Wait, but wake up immediately on stop
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            break  # stop requested
        except asyncio.TimeoutError:
            continue
    logger.info("catalog_watchdog: stopped")


def _scan_and_reset_stuck() -> None:
    """One-shot: find KBs stuck at indexing and reset them."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        stuck_kbs = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.catalog_status == "indexing",
                KnowledgeBase.source_kind == "database",
            )
            .all()
        )
        if not stuck_kbs:
            return

        for kb in stuck_kbs:
            # updated_date may be tz-naive; normalize.
            updated = kb.updated_date
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            stuck_seconds = (now - updated).total_seconds()
            if stuck_seconds < STUCK_AFTER_SECONDS:
                continue

            # Check if metadata exists from a previous run.
            n_tables = (
                db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count()
            )
            if n_tables > 0:
                kb.catalog_status = "ready"
                kb.item_count = n_tables
                logger.warning(
                    "catalog_watchdog: reset kb=%s (%s) from indexing → ready "
                    "(stuck %.0fs, %d tables already indexed)",
                    kb.id, kb.name, stuck_seconds, n_tables,
                )
            else:
                kb.catalog_status = "error"
                logger.warning(
                    "catalog_watchdog: reset kb=%s (%s) from indexing → error "
                    "(stuck %.0fs, no metadata)",
                    kb.id, kb.name, stuck_seconds,
                )
        db.commit()
    finally:
        db.close()