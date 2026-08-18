"""Generated background poller for dashboard `e2e-erp-sales-smoke`. DO NOT EDIT.

Runs as an asyncio task started by ``DashboardAppManager``. Every
``REFRESH_INTERVAL_S`` it re-runs each metric, hashes the rows, and
broadcasts only on change (clients update within ~2 s of a DB change).
"""
import asyncio
import logging

from app.database import SessionLocal
from app.services.dashboard_app.realtime import compute_rows_hash, get_connection_manager

from .queries import METRICS, run_metric

logger = logging.getLogger(__name__)

SLUG = 'e2e-erp-sales-smoke'
REFRESH_INTERVAL_S = 30


async def poll_loop() -> None:
    mgr = get_connection_manager()
    last_hashes: dict[str, str] = {}
    while True:
        db = SessionLocal()
        try:
            for m in METRICS:
                try:
                    result = await run_metric(db, m["id"])
                    rows = result.get("rows", [])
                    h = compute_rows_hash(rows)
                    if last_hashes.get(m["id"]) != h:
                        last_hashes[m["id"]] = h
                        await mgr.broadcast(SLUG, {
                            "metric_id": m["id"],
                            "title": m["title"],
                            "data": result,
                        })
                except Exception:
                    logger.exception("dashboard %s poll failed for %s", SLUG, m.get("id"))
        finally:
            db.close()
        await asyncio.sleep(REFRESH_INTERVAL_S)
