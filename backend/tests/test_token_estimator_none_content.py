"""Regression tests for the content=None crash (2026-08-24).

Assistant tool-call echo messages legitimately carry ``content=None``
(OpenAI spec). ``_estimate_input_tokens`` used to call len(None) → TypeError
that killed the whole turn ("Sorry, I hit an error while responding").

Also covers ``sanitize_messages`` Pass 1.5 which normalizes None → "".
"""
from __future__ import annotations

from app.routers.agents import _estimate_input_tokens
from app.services.message_sanitization import sanitize_messages


def _tool_echo_msg(**overrides) -> dict:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "ask_data_agent", "arguments": "{}"},
            }
        ],
    }
    msg.update(overrides)
    return msg


def test_estimate_input_tokens_does_not_crash_on_none_content():
    """content=None in the transcript must not raise TypeError."""
    messages = [
        {"role": "system", "content": "You are a helpful analyst."},
        {"role": "user", "content": "Give me the sales report."},
        _tool_echo_msg(),
        {"role": "tool", "content": "data", "tool_call_id": "call_1"},
        _tool_echo_msg(content="ok"),
    ]
    est = _estimate_input_tokens(messages)
    assert isinstance(est, int)
    assert est > 0


def test_estimate_input_tokens_skips_non_string_content():
    """Multimodal list content and missing content must not crash either."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "user"},  # missing content key entirely
    ]
    est = _estimate_input_tokens(messages)
    assert isinstance(est, int)
    assert est > 0


def test_estimate_input_tokens_ignores_non_dict_entries():
    """Defensive: non-dict entries in the transcript are skipped."""
    messages = [
        {"role": "user", "content": "hello"},
        "this-is-not-a-dict",
        None,
    ]
    est = _estimate_input_tokens(messages)
    assert isinstance(est, int)
    assert est > 0


def test_sanitize_messages_normalizes_none_content():
    """Pass 1.5 coerces content=None → "" so downstream never sees None."""
    messages = [
        {"role": "system", "content": "sys"},
        _tool_echo_msg(),
    ]
    changed = sanitize_messages(messages)
    assert changed is True
    assert messages[1]["content"] == ""


def test_sanitize_messages_keeps_real_content():
    """Non-None content is untouched by Pass 1.5."""
    messages = [
        {"role": "user", "content": "keep me"},
        {"role": "assistant", "content": "answer"},
    ]
    changed = sanitize_messages(messages)
    assert messages[0]["content"] == "keep me"
    assert messages[1]["content"] == "answer"
    # No surrogate/tool-arg issues → changed may still be False here.
    assert changed is False
