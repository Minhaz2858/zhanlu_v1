"""Generated background poller for dashboard `sales-performance-dashboard-pro`. DO NOT EDIT.

Runs as an asyncio task started by ``DashboardAppManager``. Every
``REFRESH_INTERVAL_S`` it re-runs each metric, hashes the rows, and
broadcasts only on change (clients update within ~2 s of a DB change).

T11 upgrade: when the app DB is Postgres AND ``DASHBOARD_PG_LISTEN_ENABLED``,
the poller subscribes to the per-dashboard channel (``zhanlu_dashboard_<slug>``)
via Postgres LISTEN/NOTIFY. A NOTIFY wakes the poller immediately (push), and
the interval timeout acts as the polling fallback so changes made by writers
that never NOTIFY are still picked up within one interval. When the app DB is
not Postgres (SQLite/MySQL) the poller uses pure interval polling.
"""
import asyncio
import logging

from app.database import SessionLocal
from app.services.dashboard_app.realtime import (
    compute_rows_hash,
    get_connection_manager,
    is_pg_listen_supported,
    pg_async_dsn,
    pg_listen_channel,
    touch_last_data_change,
)

from .queries import METRICS, run_metric

logger = logging.getLogger(__name__)

SLUG = 'sales-performance-dashboard-pro'
REFRESH_INTERVAL_S = 30


async def _refresh_once(mgr, last_hashes: dict[str, str]) -> None:
    """Re-run every metric and broadcast on change. One full pass.

    Each metric gets its OWN short-lived session (closed in ``finally``) so a
    slow or unreachable external datasource can never hold an app-pool
    connection across the whole pass — a hung query leaks at most one pooled
    connection for one metric and releases it as soon as the driver gives up.
    """
    for m in METRICS:
        db = SessionLocal()
        try:
            result = await run_metric(db, m["id"])
            rows = result.get("rows", [])
            h = compute_rows_hash(rows)
            if last_hashes.get(m["id"]) != h:
                last_hashes[m["id"]] = h
                # Record that the underlying data changed so the
                # My Files "unread" badge can light up (Phase 2 T4).
                try:
                    await asyncio.to_thread(touch_last_data_change, SLUG)
                except Exception:
                    logger.exception("dashboard %s touch_last_data_change failed", SLUG)
                await mgr.broadcast(SLUG, {
                    "metric_id": m["id"],
                    "title": m["title"],
                    "data": result,
                })
        except Exception:
            logger.exception("dashboard %s poll failed for %s", SLUG, m.get("id"))
        finally:
            db.close()


async def _interval_poll_loop(mgr, last_hashes: dict[str, str]) -> None:
    """Pure interval polling — used for non-Postgres app DBs or when the
    LISTEN connection cannot be established."""
    while True:
        await _refresh_once(mgr, last_hashes)
        await asyncio.sleep(REFRESH_INTERVAL_S)


async def _listen_poll_loop(mgr, last_hashes: dict[str, str]) -> None:
    """Postgres LISTEN/NOTIFY loop: wake on NOTIFY, poll on interval timeout.

    A NOTIFY (sent via ``notify_data_changed`` by any writer) triggers an
    immediate refresh; the timeout keeps interval hash-polling as a fallback
    so external writers that never NOTIFY still get picked up.
    """
    import asyncpg

    conn = await asyncpg.connect(pg_async_dsn())
    channel = pg_listen_channel(SLUG)
    notified = asyncio.Event()

    def _on_notify(conn, pid, payload):  # asyncpg notification callback
        notified.set()

    try:
        await conn.add_listener(channel, _on_notify)
        while True:
            notified.clear()
            try:
                await asyncio.wait_for(notified.wait(), timeout=REFRESH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass  # interval fallback: poll anyway
            await _refresh_once(mgr, last_hashes)
    finally:
        try:
            await conn.remove_listener(channel, _on_notify)
        except Exception:
            pass
        await conn.close()


async def poll_loop() -> None:
    mgr = get_connection_manager()
    last_hashes: dict[str, str] = {}
    if is_pg_listen_supported():
        try:
            await _listen_poll_loop(mgr, last_hashes)
            return
        except Exception:
            logger.exception(
                "dashboard %s LISTEN/NOTIFY failed — falling back to interval polling", SLUG
            )
    await _interval_poll_loop(mgr, last_hashes)
