"""2026-08-25: Test that the drop_old_tool_messages action doesn't orphan
assistant tool_calls.

Background: DeepSeek (and OpenAI API) rejects a request if any assistant
message with tool_calls doesn't have a following role:"tool" message
for each tool_call_id. The pre_api_prune escalate ladder previously
dropped the oldest half of tool-role messages blindly, which orphaned
their parent assistant(tool_calls) and made DeepSeek return 400
"insufficient tool messages following tool_calls message".
"""
import pytest
from app.services.compaction.pre_api_prune import escalate


def _make_messages_with_tool_calls():
    """Build a message list with 4 tool-call rounds. Each round has:
    assistant(tool_calls=[X]) -> tool(tool_call_id=X, result)
    """
    return [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_2", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_2", "content": "result2"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_3", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_3", "content": "result3"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_4", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_4", "content": "result4"},
    ]


def test_drop_old_tool_messages_preserves_assistant_tool_calls_completeness():
    """After dropping old tool messages, every remaining assistant(tool_calls)
    must still have a corresponding role:"tool" message for every tool_call_id.
    """
    messages = _make_messages_with_tool_calls()
    new_msgs, action, fallback = escalate(2, messages, model="deepseek-chat")
    assert action == "drop_old_tool_messages"
    for i, m in enumerate(new_msgs):
        if m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls") or []
        if not tool_calls:
            continue
        call_ids = {tc["id"] for tc in tool_calls if isinstance(tc, dict) and "id" in tc}
        following_tool_ids = {
            mm.get("tool_call_id")
            for mm in new_msgs[i + 1:]
            if mm.get("role") == "tool"
        }
        missing = call_ids - following_tool_ids
        assert not missing, (
            f"Assistant tool_calls {missing} are orphaned (no following role:tool "
            f"messages) — DeepSeek will reject this request with 400 'insufficient "
            f"tool messages following tool_calls message'."
        )
