"""Tests for the P1-5 smart_truncate hook in the v3 stream path.

The fix: ``_stream_llm_with_tools`` calls ``smart_truncate`` on the
incoming ``messages`` list before building the request payload, so
oversized tool results carried forward by the v3 FSM get capped to the
per-model limit (qwen3.6-27b: 12,288 tokens, deepseek: 24,576, etc.).

This complements the same hook in ``llm_service.call_llm`` (non-stream
path).  Without it, the stream path can still send 50k+ tokens of
ask_data_agent SQL responses / fetch_data_batch payloads to a
small-context model and trigger the same context_overflow 400 the
non-stream pre-flight already protects against.
"""
import os
import sys
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.compaction.pre_api_prune import smart_truncate


def _big_tool_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def test_smart_truncate_caps_stream_messages_for_qwen():
    """A 50k-token tool result must be truncated to qwen's 12,288 cap."""
    big = "X" * 200_000  # 50,000 tokens
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        _big_tool_msg("a", big),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        _big_tool_msg("b", "small"),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]},
        _big_tool_msg("c", "small"),
    ]
    out = smart_truncate(messages, model="qwen3.6-27b")
    tool_a = next(m for m in out
                  if m.get("role") == "tool" and m.get("tool_call_id") == "a")
    assert len(tool_a["content"]) <= 50_000
    assert len(tool_a["content"]) < 200_000


def test_smart_truncate_preserves_recent_messages():
    """The most recent tool messages must be protected (not truncated)."""
    big = "X" * 200_000
    messages = [
        _big_tool_msg("a", big),
        _big_tool_msg("b", "small"),
        _big_tool_msg("c", "small"),
    ]
    out = smart_truncate(messages, model="qwen3.6-27b")
    tool_b = next(m for m in out
                  if m.get("role") == "tool" and m.get("tool_call_id") == "b")
    tool_c = next(m for m in out
                  if m.get("role") == "tool" and m.get("tool_call_id") == "c")
    assert tool_b["content"] == "small"
    assert tool_c["content"] == "small"


def test_smart_truncate_returns_unchanged_for_big_models():
    """A 30k-token result is under deepseek's 24,576 cap? No — 30k > 24.5k
    so it WOULD be truncated. Use a smaller payload that fits within
    the cap, plus 3 messages, to confirm the smaller messages are
    unchanged.
    """
    small = "X" * 4_000  # 1k tokens
    messages = [
        _big_tool_msg("a", small),
        _big_tool_msg("b", small),
        _big_tool_msg("c", small),
    ]
    out = smart_truncate(messages, model="deepseek-v4-flash")
    for m in out:
        if m.get("role") == "tool":
            assert m["content"] == small
