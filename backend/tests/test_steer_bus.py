"""Tests for the steer_bus in-process per-conversation queue registry.

P2 Task 1. The steer bus is a tiny in-memory module that owns one
asyncio.Queue per conversation_id. The new POST /steer endpoint enqueues;
the v3 SSE event_stream drains between tool-loop iterations.

Why in-memory + asyncio.Queue (not DB/Redis): the project runs single-
process for v3 streaming; persistence would just add latency. Bounded
queues prevent unbounded growth if the frontend spams steer while the
loop is busy with a long-running tool.
"""

from __future__ import annotations

import pytest

import app.services.steer_bus as sb


# --- Cleanup fixture --------------------------------------------------------
# Each test gets a fresh registry so prior tests' enqueues do not leak.
@pytest.fixture(autouse=True)
def _reset_bus():
    sb._QUEUES.clear()
    yield
    sb._QUEUES.clear()


# --- RED tests --------------------------------------------------------------


def test_enqueue_returns_true_and_drain_returns_messages_in_order():
    """First enqueue, then drain — messages come back FIFO in order."""
    sb.enqueue("conv-A", "first")
    sb.enqueue("conv-A", "second")
    sb.enqueue("conv-A", "third")
    out = sb.drain("conv-A")
    assert out == ["first", "second", "third"]


def test_drain_on_empty_conversation_returns_empty_list():
    """Drain on a conversation with no queued messages returns []."""
    assert sb.drain("conv-empty") == []


def test_drain_does_not_block_when_empty():
    """drain must be non-blocking (it uses get_nowait) so it never stalls
    the SSE event_stream's iteration boundary."""
    # If drain blocked, this would hang the test.
    out = sb.drain("conv-blocktest")
    assert out == []


def test_isolated_queues_per_conversation():
    """A steer on conversation A does not appear in conversation B's drain."""
    sb.enqueue("conv-A", "a-only")
    sb.enqueue("conv-B", "b-only")
    assert sb.drain("conv-A") == ["a-only"]
    assert sb.drain("conv-B") == ["b-only"]
    # Subsequent drains are empty (FIFO took everything).
    assert sb.drain("conv-A") == []


def test_discard_removes_queue():
    """discard wipes the queue so the next drain returns [] and the
    conversation is fully cleaned up (no leak across turns)."""
    sb.enqueue("conv-X", "x")
    assert sb.drain("conv-X") == ["x"]
    sb.enqueue("conv-X", "x2")
    sb.discard("conv-X")
    # After discard, drain returns [] and the next enqueue starts fresh.
    assert sb.drain("conv-X") == []
    sb.enqueue("conv-X", "x3")
    assert sb.drain("conv-X") == ["x3"]


def test_enqueue_returns_false_when_queue_full():
    """Bounded queue: when maxsize is reached, enqueue returns False so
    the endpoint can return 429 to the frontend instead of blocking."""
    # Fill the queue past its limit (default maxsize=20 per the plan).
    for i in range(20):
        assert sb.enqueue("conv-full", f"m{i}") is True
    # The 21st must fail without blocking.
    assert sb.enqueue("conv-full", "overflow") is False


def test_enqueue_creates_queue_lazily():
    """The first enqueue for a new conversation creates the queue
    implicitly (no separate init step)."""
    assert "conv-new" not in sb._QUEUES
    sb.enqueue("conv-new", "hello")
    assert "conv-new" in sb._QUEUES
    assert sb.drain("conv-new") == ["hello"]


def test_drain_does_not_remove_queue():
    """Drain is non-destructive: the conversation's queue still exists
    for subsequent enqueues. Only discard removes the entry."""
    sb.enqueue("conv-Y", "y")
    sb.drain("conv-Y")
    assert "conv-Y" in sb._QUEUES
    sb.enqueue("conv-Y", "y2")
    assert sb.drain("conv-Y") == ["y2"]
