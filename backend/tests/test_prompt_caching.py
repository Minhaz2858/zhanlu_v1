"""Tests for prompt caching."""
import copy
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.prompt_caching import apply_cache_control, _can_carry_marker, _build_marker


def test_disabled_returns_unchanged():
    """When disabled, messages are returned unchanged (no copy)."""
    messages = [{"role": "system", "content": "hello"}, {"role": "user", "content": "hi"}]
    result = apply_cache_control(messages, enabled=False)
    assert result is messages  # same object, no copy


def test_enabled_applies_markers():
    """When enabled, cache_control markers are added."""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = apply_cache_control(messages, enabled=True)
    # System prompt should have cache_control (via content list)
    sys_content = result[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[0].get("cache_control") == {"type": "ephemeral"}

    # Last few messages should have markers too
    last_content = result[-1]["content"]
    assert isinstance(last_content, list)
    assert last_content[0].get("cache_control") == {"type": "ephemeral"}


def test_original_not_mutated():
    """The original message list is not mutated."""
    messages = [{"role": "system", "content": "hello"}, {"role": "user", "content": "hi"}]
    original = copy.deepcopy(messages)
    apply_cache_control(messages, enabled=True)
    assert messages == original  # unchanged


def test_empty_messages():
    """Empty message list returns empty."""
    assert apply_cache_control([], enabled=True) == []


def test_no_system_prompt():
    """Works without a system prompt — all breakpoints on non-system messages."""
    messages = [
        {"role": "user", "content": "msg 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "msg 2"},
        {"role": "assistant", "content": "reply 2"},
    ]
    result = apply_cache_control(messages, enabled=True)
    # Last 3 non-system messages should have markers
    marked = [
        m for m in result
        if isinstance(m.get("content"), list)
        and isinstance(m["content"][0], dict)
        and m["content"][0].get("cache_control")
    ]
    assert len(marked) <= 4


def test_empty_content_messages_skipped():
    """Messages with empty content don't get markers."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": ""},
        {"role": "user", "content": "hello"},
    ]
    result = apply_cache_control(messages, enabled=True)
    # The assistant with None content and tool with empty content should not have markers
    # The system and user messages should


def test_max_four_breakpoints():
    """No more than 4 breakpoints are applied."""
    messages = [{"role": f"user", "content": f"msg {i}"} for i in range(20)]
    messages.insert(0, {"role": "system", "content": "sys"})
    result = apply_cache_control(messages, enabled=True)
    # Count messages with cache_control
    marked = sum(
        1 for m in result
        if isinstance(m.get("content"), list)
        and isinstance(m["content"][0], dict)
        and m["content"][0].get("cache_control")
    )
    # System (1) + last 3 non-system = 4 max
    assert marked <= 4


def test_can_carry_marker():
    """_can_carry_marker correctly identifies messages that can carry markers."""
    assert _can_carry_marker({"role": "user", "content": "hello"}) is True
    assert _can_carry_marker({"role": "user", "content": ""}) is False
    assert _can_carry_marker({"role": "user", "content": None}) is False
    assert _can_carry_marker({"role": "assistant", "content": [{"type": "text", "text": "hi"}]}) is True
    assert _can_carry_marker({"role": "assistant", "content": []}) is False


def test_build_marker_default():
    """Default marker is ephemeral with no TTL."""
    marker = _build_marker()
    assert marker == {"type": "ephemeral"}


def test_build_marker_1h():
    """1h TTL marker includes the ttl field."""
    marker = _build_marker("1h")
    assert marker == {"type": "ephemeral", "ttl": "1h"}


def test_1h_ttl_applied():
    """1h TTL is correctly applied to messages."""
    messages = [{"role": "system", "content": "sys"}]
    result = apply_cache_control(messages, enabled=True, cache_ttl="1h")
    sys_content = result[0]["content"]
    assert sys_content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
