"""Regression tests for the realtime layer — hash-based change detection + broadcast."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.dashboard_app.realtime import (
    ConnectionManager,
    compute_rows_hash,
    get_connection_manager,
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
