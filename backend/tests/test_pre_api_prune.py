"""Tests for pre-API tool result pruning."""
import copy
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.compaction.pre_api_prune import (
    prune_tool_results_only,
    PRUNE_PLACEHOLDER,
)


def _make_tool_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _make_assistant_with_call(call_id: str, name: str, args: str = "{}") -> dict:
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": args}}],
    }


def test_no_prune_below_trigger_tokens():
    """Below the trigger token count, pruning is a no-op."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    msgs, n = prune_tool_results_only(messages, current_tokens=100, trigger_tokens=1000)
    assert n == 0
    assert msgs == messages


def test_summarize_old_results():
    """Old tool results above min_prune_chars are replaced with placeholder."""
    messages = []
    for i in range(10):
        cid = f"call_{i}"
        messages.append(_make_assistant_with_call(cid, "read_file"))
        # Use unique content so dedup doesn't fire -- test summarize only
        messages.append(_make_tool_msg(cid, f"unique_content_{i}_" * 100))

    msgs, n = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=3, min_prune_chars=500
    )
    assert n > 0
    # The most recent 3 should be preserved (not placeholder)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    cleared = [m for m in tool_msgs if m["content"] == PRUNE_PLACEHOLDER]
    preserved = [m for m in tool_msgs if m["content"] != PRUNE_PLACEHOLDER]
    assert len(cleared) == 7
    assert len(preserved) == 3


def test_dedup_identical_results():
    """Byte-identical tool results are deduped (older copy gets back-reference)."""
    identical = '{"success": true, "content": "' + "X" * 500 + '"}'
    messages = []
    for i in range(5):
        cid = f"call_{i}"
        messages.append(_make_assistant_with_call(cid, "read_file"))
        messages.append(_make_tool_msg(cid, identical))

    msgs, n = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=5, min_prune_chars=200
    )
    # At least one should be deduped (the older copies)
    back_refs = [m for m in msgs if m.get("role") == "tool" and "Duplicate" in m.get("content", "")]
    assert len(back_refs) >= 1
    # The newest copy should be preserved
    full_copies = [m for m in msgs if m.get("role") == "tool" and m["content"] == identical]
    assert len(full_copies) >= 1


def test_truncate_oversized_args():
    """Old tool_call arguments larger than max_args_chars are truncated."""
    big_args = '{"path": "' + "X" * 5000 + '"}'
    messages = []
    for i in range(8):
        messages.append(_make_assistant_with_call(f"call_{i}", "read_file", big_args))
        messages.append(_make_tool_msg(f"call_{i}", "result"))

    msgs, n = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=3, max_args_chars=500
    )
    assert n > 0
    # Old assistant messages should have truncated args
    assistants = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    old_assistants = assistants[:-3]  # exclude protected recent
    for a in old_assistants:
        args = a["tool_calls"][0]["function"]["arguments"]
        assert "[truncated]" in args
        assert len(args) <= 520  # 500 + "...[truncated]"


def test_small_results_not_summarized():
    """Tool results below min_prune_chars are not summarized."""
    small_content = '{"success": true}'
    messages = []
    for i in range(10):
        cid = f"call_{i}"
        messages.append(_make_assistant_with_call(cid, "read_file"))
        messages.append(_make_tool_msg(cid, small_content))

    msgs, n = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=3, min_prune_chars=500
    )
    # No summarization (all below threshold), but dedup may still fire
    # (small_content is < 200 chars so dedup also skips it)
    assert n == 0


def test_prune_preserves_recent_results():
    """The most recent keep_recent tool results are always preserved."""
    big_content = "X" * 1000
    messages = []
    for i in range(8):
        cid = f"call_{i}"
        messages.append(_make_assistant_with_call(cid, "read_file"))
        messages.append(_make_tool_msg(cid, f"content_{i}" * 200))

    msgs, n = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=5, min_prune_chars=100
    )
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    # Last 5 should be preserved (not placeholder)
    last_5 = tool_msgs[-5:]
    for m in last_5:
        assert m["content"] != PRUNE_PLACEHOLDER


def test_mutates_in_place():
    """The messages list is mutated in place (same object returned)."""
    messages = [
        {"role": "user", "content": "hello"},
    ]
    msgs, n = prune_tool_results_only(messages, current_tokens=20000, trigger_tokens=100)
    assert msgs is messages  # same object
