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
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

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
