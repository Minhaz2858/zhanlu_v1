"""WebSocket connection manager + hash-based change detection for dashboard apps.

Each generated dashboard app mounts a ``/api/dashboards/apps/{slug}/ws`` endpoint
that registers its sockets here. The per-app background poller (generated
``realtime.poll_loop``) re-runs the metric queries on an interval, computes a
stable hash of the result rows, and calls :meth:`ConnectionManager.broadcast`
only when the hash changed — so clients update within ~2 s of a DB change.

Phase-2 upgrade path: swap the poller for Postgres LISTEN/NOTIFY; the
``ConnectionManager`` API stays the same.
"""

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from app.config import settings

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks WebSocket connections per dashboard slug and broadcasts frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: Dict[str, Set[Any]] = {}

    async def connect(self, slug: str, websocket: Any) -> None:
        await websocket.accept()
        with self._lock:
            self._channels.setdefault(slug, set()).add(websocket)

    def disconnect(self, slug: str, websocket: Any) -> None:
        with self._lock:
            chan = self._channels.get(slug)
            if chan:
                chan.discard(websocket)
                if not chan:
                    self._channels.pop(slug, None)

    async def broadcast(self, slug: str, payload: dict) -> int:
        """Send a frame to every socket on a channel; drop dead sockets. Returns count sent."""
        with self._lock:
            chan = list(self._channels.get(slug, set()))
        sent = 0
        for ws in chan:
            try:
                await ws.send_json(payload)
                sent += 1
            except Exception:
                self.disconnect(slug, ws)
        return sent

    def channel_size(self, slug: str) -> int:
        with self._lock:
            return len(self._channels.get(slug, set()))


def compute_rows_hash(rows: list[Any]) -> str:
    """Stable hash of query result rows — the change-detection fingerprint."""
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def touch_last_data_change(slug: str, db=None) -> None:
    """Bump ``last_data_change_at`` for a dashboard app.

    Called by the generated poller (``realtime.poll_loop``) right after a
    change is detected and broadcast. Writes are swallowed on failure — the
    poller must never die because the timestamp bookkeeping failed.

    ``db`` may be injected for tests; when omitted a fresh session is opened.
    """
    from app.database import SessionLocal
    from app.models.dashboard_app import DashboardApp

    session = db or SessionLocal()
    try:
        record = session.query(DashboardApp).filter(DashboardApp.slug == slug).first()
        if record is not None:
            record.last_data_change_at = datetime.now(timezone.utc)
            session.commit()
    except Exception as exc:  # pragma: no cover - defensive, poller must survive
        logger.warning("touch_last_data_change failed for %s: %s", slug, exc)
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        if db is None:
            try:
                session.close()
            except Exception:
                pass


_manager: "ConnectionManager | None" = None
_manager_lock = threading.Lock()


def get_connection_manager() -> ConnectionManager:
    """Singleton accessor for the shared connection manager."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ConnectionManager()
        return _manager


# --- T11: Postgres LISTEN/NOTIFY push layer --------------------------------
# Upgrades the generated poller from pure interval polling to event-driven
# refresh when the app DB is Postgres AND DASHBOARD_PG_LISTEN_ENABLED. The
# ConnectionManager API is unchanged; only the polling strategy differs.
#
# Channel naming: one channel per dashboard, derived from its slug. NOTIFY is
# sent on the SAME database the poller LISTENs on — the app's own Postgres
# (settings.DATABASE_URL), not the external datasource. Any backend writer
# that mutates dashboard-relevant data can call notify_data_changed(slug) to
# wake the poller instantly; interval hash-polling remains as the fallback so
# external writers that never NOTIFY still get picked up within one interval.

_PG_CHANNEL_PREFIX = "zhanlu_dashboard_"


def pg_listen_channel(slug: str) -> str:
    """Deterministic Postgres channel name for one dashboard (<=63 bytes)."""
    return f"{_PG_CHANNEL_PREFIX}{slug}"


def pg_async_dsn() -> "str | None":
    """Return an asyncpg-compatible DSN for the app DB, or None when the app
    DB is not Postgres (asyncpg cannot talk to SQLite/MySQL).

    Converts the SQLAlchemy dialect prefix (``postgresql+psycopg2://``,
    ``postgresql://``, ``postgres://``) to asyncpg's ``postgresql://``.
    """
    url = (getattr(settings, "DATABASE_URL", "") or "").strip()
    if "postgres" not in url.split("://", 1)[0]:
        return None
    return re.sub(r"^postgres(?:ql)?(?:\+[a-z0-9_]+)?://", "postgresql://", url)


def is_pg_listen_supported() -> bool:
    """True when the T11 push layer is enabled AND the app DB is Postgres."""
    return bool(getattr(settings, "DASHBOARD_PG_LISTEN_ENABLED", False)) and pg_async_dsn() is not None


def notify_data_changed(slug: str, db=None) -> None:
    """Best-effort NOTIFY on the app's Postgres DB to wake dashboard pollers.

    Idempotent no-op when the push layer is disabled or the app DB is not
    Postgres. Any backend writer that changes data consumed by a dashboard
    can call this after committing to trigger an immediate refresh + broadcast
    (the poller still hash-checks, so an empty wake is harmless).

    ``db`` may be injected for tests; when omitted a short-lived connection is
    opened and closed.
    """
    if not is_pg_listen_supported():
        return
    channel = pg_listen_channel(slug)
    try:
        if db is not None:
            db.execute(_pg_notify_stmt(channel))
            return
        from sqlalchemy import text
        from app.database import engine
        with engine.connect() as conn:
            conn.execute(text(f'NOTIFY "{channel}"'))
    except Exception as exc:  # pragma: no cover - defensive; never break writers
        logger.warning("notify_data_changed(%s) failed: %s", slug, exc)


def _pg_notify_stmt(channel: str):
    """Build a SQLAlchemy statement for NOTIFY (kept importable without engine)."""
    from sqlalchemy import text
    return text(f'NOTIFY "{channel}"')
