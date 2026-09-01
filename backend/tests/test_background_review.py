"""Tests for background review."""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.background_review import (
    spawn_background_review,
    _digest_history,
    _MEMORY_REVIEW_PROMPT,
    DEFAULT_REVIEW_INTERVAL,
)


def test_default_review_interval():
    """The default review interval is 5 turns."""
    assert DEFAULT_REVIEW_INTERVAL == 5


def test_digest_history_short_conversation():
    """Short conversations (under tail) pass through unchanged."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    digested = _digest_history(messages, tail=20)
    assert digested == messages


def test_digest_history_long_conversation():
    """Long conversations get older turns summarized into a digest."""
    messages = []
    for i in range(30):
        messages.append({"role": "user", "content": f"message {i}"})
        messages.append({"role": "assistant", "content": f"reply {i}"})

    digested = _digest_history(messages, tail=10)
    # Should be shorter than original
    assert len(digested) < len(messages)
    # First message should be the digest
    assert digested[0]["role"] == "user"
    assert "digest" in digested[0]["content"].lower()
    # Should contain recent messages verbatim
    assert messages[-1] in digested or messages[-1]["content"] == digested[-1]["content"]


def test_digest_history_preserves_role_alternation():
    """The digest doesn't start with a tool message (role alternation)."""
    messages = []
    for i in range(10):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": str(i), "type": "function",
                                         "function": {"name": "read_file", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": "result"})

    digested = _digest_history(messages, tail=5)
    # First message should NOT be a tool message
    assert digested[0].get("role") != "tool"


def test_memory_review_prompt_is_substantive():
    """The review prompt asks about user preferences and important facts."""
    assert "preferences" in _MEMORY_REVIEW_PROMPT.lower()
    assert "memory" in _MEMORY_REVIEW_PROMPT.lower()
    assert "nothing to save" in _MEMORY_REVIEW_PROMPT.lower()


def test_spawn_background_review_returns_none_without_loop():
    """Returns None when no event loop is running."""
    # This test runs outside an event loop
    result = spawn_background_review(
        "test-conv",
        [{"role": "user", "content": "hello"}],
    )
    assert result is None


def test_spawn_background_review_returns_task_with_loop():
    """Returns an asyncio Task when an event loop is running."""
    async def _test():
        # This will fail quickly because there's no real API key,
        # but it should return a Task object.
        task = spawn_background_review(
            "test-conv",
            [{"role": "user", "content": "hello"}],
            api_key="fake",
            base_url="http://localhost:9999",
            memory_tool_schema=[],
        )
        assert task is not None
        assert isinstance(task, asyncio.Task)
        # Cancel it immediately — we don't want it to actually run
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_test())
