"""Regression tests for the realtime layer — hash-based change detection + broadcast."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.dashboard_app.realtime import (
    ConnectionManager,
    compute_rows_hash,
    get_connection_manager,
    is_pg_listen_supported,
    notify_data_changed,
    pg_async_dsn,
    pg_listen_channel,
    touch_last_data_change,
)


def test_compute_rows_hash_stable_for_same_data():
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert compute_rows_hash(rows) == compute_rows_hash(list(rows))


def test_compute_rows_hash_changes_on_data_change():
    r1 = [{"a": 1}]
    r2 = [{"a": 2}]
    assert compute_rows_hash(r1) != compute_rows_hash(r2)


def test_compute_rows_hash_key_order_independent():
    # sort_keys makes key order within a row irrelevant, but row ORDER in the
    # list still matters (a different sequence is a different result set).
    a = compute_rows_hash([{"b": 2, "a": 1}])
    b = compute_rows_hash([{"a": 1, "b": 2}])
    assert a == b  # same row, keys reordered -> identical
    c = compute_rows_hash([{"a": 1, "b": 2}])
    d = compute_rows_hash([{"b": 2, "a": 1}])
    assert c == d


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_connection_manager_connect_broadcast_disconnect():
    mgr = ConnectionManager()
    ws = _FakeSocket()
    await mgr.connect("slug-a", ws)
    assert mgr.channel_size("slug-a") == 1
    # broadcast returns count sent
    n = await mgr.broadcast("slug-a", {"type": "data", "rows": [1, 2, 3]})
    assert n == 1
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "data"
    # different slug has no listeners
    assert await mgr.broadcast("slug-b", {"x": 1}) == 0
    mgr.disconnect("slug-a", ws)
    assert mgr.channel_size("slug-a") == 0


@pytest.mark.asyncio
async def test_broadcast_drops_dead_socket():
    mgr = ConnectionManager()
    good = _FakeSocket()
    await mgr.connect("s", good)
    # make a socket whose send_json raises
    class DeadSocket(_FakeSocket):
        async def send_json(self, payload):
            raise RuntimeError("closed")
    dead = DeadSocket()
    await mgr.connect("s", dead)
    assert mgr.channel_size("s") == 2
    n = await mgr.broadcast("s", {"v": 1})
    # dead socket removed, good one still receives
    assert n == 1
    assert len(good.sent) == 1
    assert mgr.channel_size("s") == 1


def test_singleton_manager():
    assert get_connection_manager() is get_connection_manager()


def test_touch_last_data_change_updates_timestamp():
    """T4: the poller's change event bumps last_data_change_at so My Files
    can show the "unread" badge (data changed since last viewed)."""
    record = MagicMock()
    record.last_data_change_at = None
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = record
    with patch(
        "app.database.SessionLocal",
        return_value=session,
    ):
        touch_last_data_change("slug-a")

    assert record.last_data_change_at is not None
    assert record.last_data_change_at.tzinfo is not None
    session.commit.assert_called_once()


def test_touch_last_data_change_missing_record_is_noop():
    """T4: unknown slug (app record deleted mid-flight) must not crash the
    poller loop."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    with patch(
        "app.database.SessionLocal",
        return_value=session,
    ):
        # Must not raise
        touch_last_data_change("does-not-exist")


def test_touch_last_data_change_db_error_swallowed():
    """T4: a DB failure on the timestamp bookkeeping must not kill the
    poller — broadcast continues."""
    session = MagicMock()
    session.query.side_effect = RuntimeError("db down")
    with patch(
        "app.database.SessionLocal",
        return_value=session,
    ):
        # Must not raise
        touch_last_data_change("slug-a")


# ── T11: Postgres LISTEN/NOTIFY push layer ──


def test_pg_listen_channel_deterministic_and_short():
    """Channel names are deterministic and stay under PG's 63-byte limit for
    realistic slugs (PG truncates over-long channel identifiers silently; the
    app's slug generator caps slug length far below the limit)."""
    assert pg_listen_channel("sales-overview") == "zhanlu_dashboard_sales-overview"
    assert pg_listen_channel("sales-overview") == pg_listen_channel("sales-overview")
    assert len(pg_listen_channel("sales-overview")) < 63
    assert pg_listen_channel("a" * 40) == f"zhanlu_dashboard_{'a' * 40}"


def test_pg_async_dsn_converts_dialect_prefix(monkeypatch):
    """SQLAlchemy dialect prefixes convert to asyncpg's postgresql://."""
    for raw, expected in [
        ("postgresql+psycopg2://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
    ]:
        monkeypatch.setattr("app.services.dashboard_app.realtime.settings.DATABASE_URL", raw)
        assert pg_async_dsn() == expected


def test_pg_async_dsn_returns_none_for_non_postgres(monkeypatch):
    """SQLite/MySQL app DBs can't use asyncpg — must return None."""
    for raw in ("sqlite:///./x.db", "mysql+pymysql://u:p@h/db", ""):
        monkeypatch.setattr("app.services.dashboard_app.realtime.settings.DATABASE_URL", raw)
        assert pg_async_dsn() is None


def test_is_pg_listen_supported_gates_on_flag_and_dialect(monkeypatch):
    """T11 push layer requires BOTH the flag AND a Postgres app DB."""
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", True)
    monkeypatch.setattr(real_settings, "DATABASE_URL", "postgresql+psycopg2://u:p@h/db")
    assert is_pg_listen_supported() is True

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", False)
    assert is_pg_listen_supported() is False

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", True)
    monkeypatch.setattr(real_settings, "DATABASE_URL", "sqlite:///./x.db")
    assert is_pg_listen_supported() is False


def test_notify_data_changed_sends_notify(monkeypatch):
    """A writer calling notify_data_changed must issue NOTIFY on the app DB."""
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", True)
    monkeypatch.setattr(real_settings, "DATABASE_URL", "postgresql+psycopg2://u:p@h/db")

    db = MagicMock()
    notify_data_changed("sales-overview", db=db)
    db.execute.assert_called_once()
    stmt = db.execute.call_args[0][0]
    assert str(stmt) == 'NOTIFY "zhanlu_dashboard_sales-overview"'


def test_notify_data_changed_disabled_is_noop(monkeypatch):
    """Flag off → no NOTIFY is issued (keeps writers lightweight)."""
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", False)
    db = MagicMock()
    notify_data_changed("sales-overview", db=db)
    db.execute.assert_not_called()


def test_notify_data_changed_swallows_errors(monkeypatch):
    """A failed NOTIFY must never break the writer's transaction path."""
    from app.config import settings as real_settings

    monkeypatch.setattr(real_settings, "DASHBOARD_PG_LISTEN_ENABLED", True)
    monkeypatch.setattr(real_settings, "DATABASE_URL", "postgresql+psycopg2://u:p@h/db")

    db = MagicMock()
    db.execute.side_effect = RuntimeError("db down")
    # Must not raise
    notify_data_changed("sales-overview", db=db)
