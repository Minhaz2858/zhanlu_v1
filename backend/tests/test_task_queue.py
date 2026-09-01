"""Regression tests for task_queue.py (Part 2 — Phase 3 scalability)."""

import json
from unittest.mock import MagicMock, patch

from app.services import task_queue as tq


def _reset_queue():
    """Drop module-level Redis singleton and memory state."""
    tq._memory_queues.clear()
    tq._memory_dlq.clear()
    tq._task_meta.clear()


class TestEnqueue:
    """Tests for enqueue."""

    def test_returns_task_id_with_redis(self):
        _reset_queue()
        mock_redis = MagicMock()
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            task_id = tq.enqueue("compile_report", {"work": "test"})
            assert isinstance(task_id, str)
            assert len(task_id) > 0

    def test_returns_task_id_with_memory_fallback(self):
        _reset_queue()
        with patch.object(tq, "_get_redis_client", return_value=None):
            task_id = tq.enqueue("compile_report", {"work": "test"})
            assert isinstance(task_id, str)
            assert len(task_id) > 0

    def test_stores_in_memory_when_redis_none(self):
        _reset_queue()
        with patch.object(tq, "_get_redis_client", return_value=None):
            task_id = tq.enqueue("compute", {"priority": 3})
            assert len(tq._memory_queues["compute"]) == 1
            assert tq._memory_queues["compute"][0]["task_id"] == task_id


class TestDequeue:
    """Tests for dequeue."""

    def test_returns_none_on_empty_queue(self):
        _reset_queue()
        mock_redis = MagicMock()
        mock_redis.blpop.return_value = None
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            result = tq.dequeue("default", timeout=0)  # non-blocking
            assert result is None

    def test_returns_task_on_populated_queue(self):
        _reset_queue()
        mock_redis = MagicMock()
        task_id_bytes = b"abc123"
        meta = json.dumps({
            "task_id": "abc123", "task_type": "default", "status": "queued",
            "created_at": 1000.0, "payload": {"work": "compile"}, "retry_count": 0,
        })
        mock_redis.blpop.return_value = (b"task:queue:default", task_id_bytes)
        mock_redis.get.return_value = bytes(meta, "utf-8")
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            task = tq.dequeue("default", timeout=0)
            assert task is not None
            assert task.task_id == "abc123"
            assert task.payload["work"] == "compile"

    def test_returns_task_from_memory_fallback(self):
        _reset_queue()
        with patch.object(tq, "_get_redis_client", return_value=None):
            tq.enqueue("test_type", {"key": "val"})
            task = tq.dequeue("test_type", timeout=0)
            assert task is not None
            assert task.task_type == "test_type"
            assert task.payload["key"] == "val"
            # Queue should be empty after dequeue
            assert len(tq._memory_queues["test_type"]) == 0


class TestMarkComplete:
    """Tests for mark_complete."""

    def test_removes_from_redis(self):
        _reset_queue()
        mock_redis = MagicMock()
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            result = tq.mark_complete("abc-123")
            assert result is True
            mock_redis.delete.assert_called()

    def test_removes_from_memory(self):
        _reset_queue()
        tq._task_meta["abc"] = {"task_id": "abc"}
        with patch.object(tq, "_get_redis_client", return_value=None):
            assert tq.mark_complete("abc") is True
            assert "abc" not in tq._task_meta


class TestMarkFailed:
    """Tests for mark_failed."""

    def test_re_enqueues_when_retries_left(self):
        _reset_queue()
        mock_redis = MagicMock()
        meta = json.dumps({
            "task_id": "task-1", "task_type": "default", "retry_count": 1,
            "payload": {"work": "x"}, "status": "running", "created_at": 1000.0,
        })
        mock_redis.get.return_value = bytes(meta, "utf-8")
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            tq.mark_failed("task-1", "timeout", max_retries=3)
            # Should rpush back to queue (retry_count 1 < 3)
            assert mock_redis.rpush.called

    def test_moves_to_dlq_when_exhausted(self):
        _reset_queue()
        mock_redis = MagicMock()
        meta = json.dumps({
            "task_id": "task-1", "task_type": "default", "retry_count": 2,
            "payload": {"work": "x"}, "status": "running", "created_at": 1000.0,
        })
        mock_redis.get.return_value = bytes(meta, "utf-8")
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            tq.mark_failed("task-1", "permanent error", max_retries=3)
            # Should be moved to DLQ, not re-enqueued
            assert mock_redis.delete.called

    def test_memory_fallback_exhausted(self):
        _reset_queue()
        tq._task_meta["task-a"] = {
            "task_id": "task-a", "task_type": "default", "retry_count": 3,
            "payload": {"work": "x"}, "status": "running", "created_at": 1000.0,
        }
        with patch.object(tq, "_get_redis_client", return_value=None):
            tq.mark_failed("task-a", "error", max_retries=3)
            # retry_count 3 + 1 = 4 >= 3 → moved to DLQ
            assert "default" in tq._memory_dlq


class TestQueueLength:
    """Tests for queue_length."""

    def test_returns_llen_value(self):
        _reset_queue()
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 5
        with patch.object(tq, "_get_redis_client", return_value=mock_redis):
            assert tq.queue_length("default") == 5

    def test_returns_0_when_redis_unavailable(self):
        _reset_queue()
        with patch.object(tq, "_get_redis_client", return_value=None):
            assert tq.queue_length("default") == 0
