"""Periodic catalog coverage refresh — audit B8 (2026-10-22).

The catalog is effectively *write-once* at index time: ``kb_table_meta.coverage_json``
(the ``MIN``/``MAX`` of each table's temporal column, probed by ``_probe_coverage``)
is captured once and then frozen on the row. As a warehouse's data keeps growing,
those frozen windows go stale, and ``table_retrieval.reroute_uncovered`` then swaps
out a perfectly good table that was convicted by a stale ``covers_window=False`` —
so recent-window questions misroute (live example: catalog said data stops
2026-10-08 while the warehouse had data through 10-19).

This module re-runs the *same* ``_probe_coverage`` used at index time on a schedule,
refreshing ``coverage_json`` (and ``indexed_at``) for tables whose coverage is older
than ``KG_COVERAGE_REFRESH_STALE_AFTER_S``. It mirrors the ``catalog_watchdog``
asyncio pattern: launched from the FastAPI lifespan in ``main.py``, a one-shot scan
run via ``asyncio.to_thread``, fully resilient per-KB / per-table so one slow or
dead warehouse never stalls the loop.

It is a no-op unless ``KG_COVERAGE_PROBE_ENABLED`` is on — that flag is what
populates ``coverage_json`` in the first place, so there is nothing to refresh
when it is off.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import settings
from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.services.db.connector_factory import get_connector
from app.services.knowledge_graph.catalog_indexer import _probe_coverage

logger = logging.getLogger(__name__)
# Ensure INFO logs are visible even when the root logger is at WARNING.
if logger.level in (logging.NOTSET, logging.WARNING, logging.ERROR, logging.CRITICAL):
    logger.setLevel(logging.INFO)


async def run_catalog_coverage_refresh(stop_event: asyncio.Event) -> None:
    """Run the coverage-refresh loop until ``stop_event`` is set.

    Launched as ``asyncio.create_task(run_catalog_coverage_refresh(...))`` from
    the FastAPI lifespan, exactly like ``run_catalog_watchdog``.
    """
    interval = max(60, int(settings.KG_COVERAGE_REFRESH_INTERVAL_S))
    logger.info(
        "catalog_coverage_refresh: starting (poll=%ds, stale_after=%ds, probe_enabled=%s)",
        interval, settings.KG_COVERAGE_REFRESH_STALE_AFTER_S, settings.KG_COVERAGE_PROBE_ENABLED,
    )
    while not stop_event.is_set():
        try:
            if settings.KG_COVERAGE_REFRESH_ENABLED and settings.KG_COVERAGE_PROBE_ENABLED:
                await asyncio.to_thread(_refresh_all_kbs)
            else:
                logger.debug(
                    "catalog_coverage_refresh: disabled "
                    "(KG_COVERAGE_REFRESH_ENABLED=%s, KG_COVERAGE_PROBE_ENABLED=%s) — skipped",
                    settings.KG_COVERAGE_REFRESH_ENABLED, settings.KG_COVERAGE_PROBE_ENABLED,
                )
        except Exception:  # noqa: BLE001 — loop must never die
            logger.exception("catalog_coverage_refresh: scan iteration failed")
        # Wait, but wake up immediately on stop.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop requested
        except asyncio.TimeoutError:
            continue
    logger.info("catalog_coverage_refresh: stopped")


def _refresh_all_kbs() -> int:
    """One shot: refresh coverage for every ready database KB with stale tables.

    Returns the total number of tables refreshed. Each KB is handled in its own
    transaction; a failure on one KB is logged and rolled back without affecting
    the others.
    """
    stale_after = max(60, int(settings.KG_COVERAGE_REFRESH_STALE_AFTER_S))
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after)
    db = SessionLocal()
    total = 0
    try:
        kbs = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.source_kind == "database",
                KnowledgeBase.catalog_status == "ready",
            )
            .all()
        )
        for kb in kbs:
            try:
                n = _refresh_kb_coverage(db, kb, cutoff)
                if n:
                    total += n
                    logger.info(
                        "catalog_coverage_refresh: kb=%s refreshed %d tables", kb.id, n
                    )
            except Exception:  # noqa: BLE001 — one bad KB must not kill the scan
                logger.exception("catalog_coverage_refresh: kb=%s failed", kb.id)
                db.rollback()
    finally:
        db.close()
    if total:
        logger.info("catalog_coverage_refresh: refreshed %d tables this scan", total)
    return total


def _refresh_kb_coverage(db, kb, cutoff) -> int:
    """Re-probe coverage for one KB's stale, already-covered tables.

    Only tables that already carry a ``coverage_json`` (i.e. were judged to have a
    temporal column at index time) and whose ``indexed_at`` is older than *cutoff*
    are refreshed — this is a freshness fix, not a re-classification, so tables
    with no temporal column are left alone (a full re-ingest covers new columns).

    Opens a fresh connector per KB; commits in batches of 50 and again at the end.
    Raises on connector/probe failure so the caller can roll back this KB only.
    """
    tables = (
        db.query(KBTableMeta)
        .filter(
            KBTableMeta.kb_id == kb.id,
            KBTableMeta.coverage_json.isnot(None),
            KBTableMeta.indexed_at < cutoff,
        )
        .all()
    )
    if not tables:
        return 0

    refreshed = 0
    # The connector owns its own connection; the db session is separate and is
    # only written to (coverage_json + indexed_at), so holding the connector open
    # across the loop is fine.
    with get_connector(kb) as conn:
        for meta in tables:
            cols = _table_columns(db, meta.id)
            if not cols:
                continue
            cov = _probe_coverage(conn, meta.table_name, cols)
            if not cov:
                continue
            meta.coverage_json = cov
            meta.indexed_at = datetime.utcnow()
            refreshed += 1
            if refreshed % 50 == 0:
                db.commit()
        db.commit()
    return refreshed


def _table_columns(db, table_meta_id) -> list[dict]:
    """Return this table's columns as ``{column_name, data_type}`` dicts.

    All columns are returned (not just known-temporal ones) so that
    ``_probe_coverage``'s own ``_pick_date_column`` heuristic — which considers
    both declared type and name keywords — can pick the right temporal column.
    """
    cols = (
        db.query(KBColumnMeta)
        .filter(KBColumnMeta.table_meta_id == table_meta_id)
        .all()
    )
    return [
        {"column_name": c.column_name, "data_type": c.data_type}
        for c in cols
    ]
