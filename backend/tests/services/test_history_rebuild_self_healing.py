"""History rebuild self-healing regression tests.

2026-08-29: the persisted-conversation → OpenAI-messages rebuild
(``_rebuild_v3_history_messages``) used to re-emit EVERY tool_call on a
persisted assistant message, even when the call carried NO embedded
``results`` (the error path persists the frontend display shape; an
interrupted turn can also leave one). The resulting assistant message had
tool_calls with no following tool response — DeepSeek/OpenAI reject that
with 400 "insufficient tool messages following tool_calls", killing every
subsequent turn on the conversation.

The rebuild now only re-emits tool_calls that carry results; result-less
calls keep the assistant's visible text (or drop the message if there is
none) and are never emitted as dangling calls.
"""

from app.routers.agents import _rebuild_v3_history_messages


def test_rebuild_drops_result_less_tool_calls():
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "id": "call_ok",
                    "name": "ask_data_agent",
                    "arguments_string": "{}",
                    "results": {"success": True, "answer": "7 rows"},
                },
                {
                    "id": "call_nores",
                    "name": "ask_data_agent",
                    "arguments_string": "{}",
                    "status": "blocked",  # no results key
                },
            ],
        },
        {"role": "user", "content": "next"},
    ]
    out = _rebuild_v3_history_messages("SYS", msgs)

    asst_with_calls = [
        m for m in out
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(asst_with_calls) == 1
    ids = [tc["id"] for tc in asst_with_calls[0]["tool_calls"]]
    assert ids == ["call_ok"], f"result-less call leaked into tool_calls: {ids}"

    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert tool_ids == ["call_ok"]


def test_rebuild_keeps_text_when_all_calls_result_less():
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "Blocked report.",
            "tool_calls": [
                {"id": "c1", "name": "create_artifact",
                 "arguments_string": "{}", "status": "blocked"},
            ],
        },
    ]
    out = _rebuild_v3_history_messages("SYS", msgs)

    # Assistant survives as text-only — no dangling tool_calls, no tool msgs.
    assert {"role": "assistant", "content": "Blocked report."} in out
    assert not any(m.get("role") == "tool" for m in out)
    assert not any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in out
    )


def test_rebuild_result_less_without_text_drops_message():
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "name": "ask_data_agent",
                 "arguments_string": "{}", "status": "blocked"},
            ],
        },
    ]
    out = _rebuild_v3_history_messages("SYS", msgs)

    assert not any(m.get("role") == "assistant" and m.get("tool_calls") for m in out)
    assert not any(m.get("role") == "tool" for m in out)
    # The result-less call must not corrupt the sequence: no dangling calls.
    assert len([m for m in out if m.get("role") == "assistant"]) == 0
