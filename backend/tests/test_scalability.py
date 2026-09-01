"""Regression tests for Phase 3: Scalability improvements.

Covers:
- rate_limiter.py sliding window
- task_queue.py enqueue/dequeue/mark
- _run_db_sync helper in agents.py
"""

import pytest
from unittest.mock import patch, MagicMock


# ── rate_limiter.py ────────────────────────────────────────────────────


class TestRateLimiter:
    """Verify sliding-window rate limiting."""

    def test_disabled_always_allows(self):
        from app.services.rate_limiter import check_rate_limit, _is_enabled
        assert not _is_enabled()
        allowed, retry = check_rate_limit("user-1")
        assert allowed is True
        assert retry == 0

    def test_is_rate_limited_shorthand(self):
        from app.services.rate_limiter import is_rate_limited
        assert is_rate_limited("user-1") is False

    def test_whitelist_user_always_allowed(self):
        """Whitelisted users bypass rate limiting."""
        from app.services.rate_limiter import _whitelist, _is_enabled
        # _whitelist reads from settings, so mock
        with patch("app.services.rate_limiter._whitelist", return_value={"admin-1"}):
            from app.services.rate_limiter import check_rate_limit
            with patch("app.services.rate_limiter._is_enabled", return_value=True):
                with patch("app.services.rate_limiter._get_redis_client", return_value=None):
                    allowed, _ = check_rate_limit("admin-1")
                    assert allowed is True


# ── task_queue.py ──────────────────────────────────────────────────────


class TestTaskQueue:
    """Verify Redis task queue with memory fallback."""

    def test_enqueue_dequeue_memory(self):
        from app.services.task_queue import enqueue, dequeue, mark_complete
        # Ensure memory fallback (no Redis)
        task_id = enqueue("test", {"key": "value"})
        assert task_id is not None
        assert len(task_id) == 16

        info = dequeue("test")
        assert info is not None
        assert info.task_type == "test"
        assert info.payload == {"key": "value"}

        assert mark_complete(info.task_id) is True

    def test_dequeue_empty_returns_none(self):
        from app.services.task_queue import dequeue
        info = dequeue("nonexistent_queue")
        assert info is None

    def test_queue_length(self):
        from app.services.task_queue import queue_length, enqueue
        enqueue("count-test", {"a": 1})
        enqueue("count-test", {"b": 2})
        # Memory queue — should have 2 items
        length = queue_length("count-test")
        assert length == 2

    def test_queue_status(self):
        from app.services.task_queue import queue_status
        status = queue_status()
        assert isinstance(status, dict)


# ── _run_db_sync helper ────────────────────────────────────────────────


class TestRunDBSyncHelper:
    """Verify the _run_db_sync bridge function exists and is callable."""

    def test_helper_is_importable(self):
        """_run_db_sync is defined and callable."""
        from app.routers.agents import _run_db_sync
        assert callable(_run_db_sync)
