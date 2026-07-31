"""Tests for message sanitization."""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.message_sanitization import (
    sanitize_surrogates,
    sanitize_messages_surrogates,
    repair_tool_call_arguments,
    close_interrupted_tool_sequence,
    sanitize_messages,
)


def test_sanitize_surrogates_replaces_lone_surrogates():
    """Lone surrogate code points are replaced with U+FFFD."""
    text = "hello\ud800world\udc00"
    sanitized = sanitize_surrogates(text)
    assert "\ud800" not in sanitized
    assert "\udc00" not in sanitized
    assert "\ufffd" in sanitized


def test_sanitize_surrogates_noop_on_clean_text():
    """Clean text passes through unchanged."""
    text = "hello world"
    assert sanitize_surrogates(text) == text


def test_sanitize_messages_surrogates_in_content():
    """Surrogates in message content are replaced."""
    messages = [{"role": "user", "content": "hello\ud800world"}]
    found = sanitize_messages_surrogates(messages)
    assert found is True
    assert "\ud800" not in messages[0]["content"]


def test_sanitize_messages_surrogates_in_tool_args():
    """Surrogates in tool_call arguments are replaced."""
    messages = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "/\ud800file"}'},
        }],
    }]
    found = sanitize_messages_surrogates(messages)
    assert found is True
    assert "\ud800" not in messages[0]["tool_calls"][0]["function"]["arguments"]


def test_sanitize_messages_surrogates_noop_on_clean():
    """Clean messages pass through unchanged."""
    messages = [{"role": "user", "content": "hello"}]
    found = sanitize_messages_surrogates(messages)
    assert found is False


def test_repair_tool_call_arguments_empty():
    """Empty arguments return {}."""
    assert repair_tool_call_arguments("") == "{}"
    assert repair_tool_call_arguments("   ") == "{}"


def test_repair_tool_call_arguments_none():
    """Python None returns {}."""
    assert repair_tool_call_arguments("None") == "{}"


def test_repair_tool_call_arguments_valid_json():
    """Valid JSON passes through (re-serialized)."""
    result = repair_tool_call_arguments('{"path": "/a.txt"}')
    parsed = json.loads(result)
    assert parsed == {"path": "/a.txt"}


def test_repair_tool_call_arguments_trailing_comma():
    """Trailing commas are fixed."""
    result = repair_tool_call_arguments('{"path": "/a.txt",}')
    parsed = json.loads(result)
    assert parsed == {"path": "/a.txt"}


def test_repair_tool_call_arguments_unclosed_brace():
    """Unclosed braces are fixed."""
    result = repair_tool_call_arguments('{"path": "/a.txt"')
    parsed = json.loads(result)
    assert parsed == {"path": "/a.txt"}


def test_repair_tool_call_arguments_unrepairable():
    """Completely broken arguments return {}."""
    result = repair_tool_call_arguments("not json at all {{{")
    assert result == "{}"


def test_repair_tool_call_arguments_control_chars():
    """Literal control characters inside JSON strings are handled."""
    result = repair_tool_call_arguments('{"text": "hello\tworld"}')
    parsed = json.loads(result)
    assert parsed == {"text": "hello\tworld"}


def test_close_interrupted_tool_sequence():
    """An orphaned tool message at the end gets a closing assistant turn."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "file contents"},
    ]
    appended = close_interrupted_tool_sequence(messages)
    assert appended is True
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"]  # non-empty


def test_close_interrupted_no_op_on_assistant_tail():
    """No-op when the last message is already an assistant message."""
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    appended = close_interrupted_tool_sequence(messages)
    assert appended is False
    assert len(messages) == 2


def test_close_interrupted_empty_messages():
    """No-op on empty message list."""
    assert close_interrupted_tool_sequence([]) is False


def test_sanitize_messages_combined():
    """The combined sanitize_messages runs all passes."""
    messages = [
        {"role": "user", "content": "hello\ud800world"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "/a",}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "content"},
    ]
    changed = sanitize_messages(messages)
    assert changed is True
    # Surrogates replaced
    assert "\ud800" not in messages[0]["content"]
    # Tool args repaired (trailing comma fixed)
    args = messages[1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"path": "/a"}
    # Interrupted tool sequence closed
    assert messages[-1]["role"] == "assistant"
