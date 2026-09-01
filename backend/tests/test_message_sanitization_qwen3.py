"""Tests for qwen3.6-27b-specific message sanitization.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.routers.agents import _inject_reflexion_critique


def test_reflexion_critique_uses_user_role_not_system():
    """Regression: reflexion critique must NOT inject a system message mid-list.
    vLLM rejects mid-list system messages with HTTP 400.
    """
    llm_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What's my sales?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "0 rows returned"},
    ]
    calls = [{"tool_name": "ask_data_agent", "args_str": "{}"}]
    results = [{"success": False, "error": "Invalid arguments"}]

    _inject_reflexion_critique(llm_messages, calls, results)

    # The injected message must be role=user, NOT role=system
    injected = [m for m in llm_messages if m["role"] == "user" and "failed" in m.get("content", "").lower()]
    assert injected, f"No user-role critique found; messages: {llm_messages}"
    system_msgs = [m for m in llm_messages if m["role"] == "system"]
    assert len(system_msgs) == 1, f"Expected 1 system msg (the initial), got {len(system_msgs)}"


# ── Task 2 + 3: web-search handler + sanitize_messages mid-list guard ──────


def test_web_search_handler_uses_user_role_not_system():
    """The web-search followup handler must not inject a mid-list system
    message. vLLM rejects this with HTTP 400. This test feeds a mid-list
    system to sanitize_messages and verifies the guard demotes it."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "search the web for X"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "tc1", "content": "no results"},
        # This is what the web-search handler used to append:
        {"role": "system", "content": "You DO have access to web_search."},
        {"role": "user", "content": "Here are live web search results..."},
    ]
    changed = sanitize_messages(messages)
    assert changed, "sanitize_messages should have demoted the mid-list system msg"
    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1, f"Expected 1 system msg after sanitize, got {len(system_msgs)}"
    # The mid-list system should now be role=user
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("web_search" in m.get("content", "") for m in user_msgs)


def test_sanitize_messages_demotes_mid_list_system_to_user():
    """Defense-in-depth: any system message not at index 0 gets demoted
    to role=user. Catches future code paths that re-introduce the bug."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "mid-list system msg — should be demoted"},
        {"role": "user", "content": "bye"},
    ]
    changed = sanitize_messages(messages)
    assert changed, "sanitize_messages should have demoted the mid-list system msg"
    assert messages[0]["role"] == "system", "index 0 system msg must stay"
    assert messages[2]["role"] == "user", f"index 2 should be user, got {messages[2]['role']}"
    assert "mid-list system msg" in messages[2]["content"]


def test_sanitize_messages_no_change_when_system_only_at_index_0():
    """No false positives: when system is only at index 0, sanitize_messages
    should not change anything (for the system-message pass)."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    original_roles = [m["role"] for m in messages]
    sanitize_messages(messages)
    new_roles = [m["role"] for m in messages]
    assert original_roles == new_roles, f"roles changed: {original_roles} -> {new_roles}"


def test_sanitize_messages_multiple_mid_list_systems_all_demoted():
    """Multiple mid-list system messages all get demoted."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "initial"},
        {"role": "system", "content": "second system — demote"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "third system — demote"},
    ]
    sanitize_messages(messages)
    system_count = sum(1 for m in messages if m["role"] == "system")
    assert system_count == 1, f"Expected 1 system msg, got {system_count}"
    # 2 demoted + 1 original user = 3 user messages
    user_count = sum(1 for m in messages if m["role"] == "user")
    assert user_count == 3, f"Expected 3 user msgs (1 original + 2 demoted), got {user_count}"


# ── Task 4: single-quoted Python-literal tool_call args repair ──────────────


def test_repair_single_quoted_python_dict_literal():
    """qwen3.6-27b emits args as Python-style dict literals with single
    quotes: 'query': 'show me sales'. Standard JSON parsers reject this.
    ast.literal_eval handles it and we re-serialize as valid JSON."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "{'query': 'show me sales'}"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)  # must be valid JSON
    assert parsed == {"query": "show me sales"}


def test_repair_single_quoted_no_braces():
    """qwen3.6-27b sometimes emits args without braces: 'query': 'value'."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "'query': 'show me sales'"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)
    assert parsed == {"query": "show me sales"}


def test_repair_mixed_quotes():
    """Mixed quotes: {"query": 'show me sales'} — valid JSON brace but
    single-quoted value."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = '{"query": \'show me sales\'}'
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)
    assert parsed == {"query": "show me sales"}


def test_repair_valid_json_unchanged():
    """Already-valid JSON must pass through as valid JSON (may be normalized
    — spaces removed — but must parse to the same dict)."""
    from app.services.message_sanitization import repair_tool_call_arguments
    import json
    raw = '{"query": "show me sales"}'
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    # Semantic equality: both must parse to the same dict
    assert json.loads(result) == json.loads(raw)


def test_repair_unrepairable_returns_empty_object():
    """Genuinely unrepairable args still return {} (existing behavior)."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "not even close to json or python"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    assert result == "{}"
