"""DeepSeek thinking-mode contract — regression guard (2026-09-17).

Every tool-calling turn on the non-streaming (v2) path used to die on its SECOND
LLM request:

    400 {"error":{"message":"The `reasoning_content` in the thinking mode must be
         passed back to the API.","code":"invalid_request_error"}}

The tool had already executed successfully, so the user saw
"Sorry, I encountered an error: Bad request (400). Check request format." *after*
the work was done. Cause: the contract was implemented only in the streaming path
(`_stream_llm_with_tools`), while `_call_llm_with_tools` — which the v2 loop uses
— never applied it, and the loop builds its assistant tool-call message as
`{role, content, tool_calls}` with no `reasoning_content` key.

These tests pin the shared helper both paths now call. The full end-to-end proof
is scripts/e2e/mcp_agent_tools_e2e.sh (asserts a tool turn reaches a final answer
that carries the tool's own output).
"""

import pytest


def test_stamps_the_key_on_assistant_messages_only():
    """The API insists on the field for assistant turns; other roles must not
    carry it (providers reject unknown fields on user/tool messages)."""
    from app.routers.agents import apply_reasoning_content_contract

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
    ]
    out = apply_reasoning_content_contract(messages)

    assert out[2]["reasoning_content"] == ""
    assert "reasoning_content" not in out[0]
    assert "reasoning_content" not in out[1]
    assert "reasoning_content" not in out[3]


def test_real_reasoning_is_preserved_not_overwritten():
    """When the caller echoes the true reasoning back, keep it — an empty string
    is the fallback for history we did not persist, not the goal."""
    from app.routers.agents import apply_reasoning_content_contract

    messages = [{
        "role": "assistant",
        "content": None,
        "reasoning_content": "The user wants 17+25, so I should call add.",
        "tool_calls": [{"id": "1"}],
    }]
    assert (apply_reasoning_content_contract(messages)[0]["reasoning_content"]
            == "The user wants 17+25, so I should call add.")


def test_does_not_mutate_the_callers_messages():
    """The same list is reused across loop iterations — stamping in place would
    leak a stale reasoning_content into later turns."""
    from app.routers.agents import apply_reasoning_content_contract

    messages = [{"role": "assistant", "content": None, "tool_calls": []}]
    out = apply_reasoning_content_contract(messages)

    assert "reasoning_content" not in messages[0]
    assert out[0] is not messages[0]


def test_handles_empty_and_non_assistant_only_lists():
    from app.routers.agents import apply_reasoning_content_contract

    assert apply_reasoning_content_contract([]) == []
    only_user = [{"role": "user", "content": "hi"}]
    assert apply_reasoning_content_contract(only_user) == only_user


@pytest.mark.asyncio
async def test_non_streaming_llm_call_applies_the_contract(monkeypatch):
    """Behavioral check on `_call_llm_with_tools`: the request it actually sends
    must carry the field. This is the path the v2 loop uses, and the one that
    regressed — a helper-level test alone would not have caught it."""
    import httpx

    from app.routers import agents as agents_mod

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            captured["payload"] = json
            return _Resp()

    monkeypatch.setattr(agents_mod.httpx, "AsyncClient", _Client)

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
    ]
    await agents_mod._call_llm_with_tools(messages, tools=None)

    sent = captured["payload"]["messages"]
    assistant_msgs = [m for m in sent if m.get("role") == "assistant"]
    assert assistant_msgs, "no assistant message reached the provider"
    for m in assistant_msgs:
        assert "reasoning_content" in m, (
            "assistant message sent without reasoning_content — DeepSeek will "
            "reject the follow-up request with 400"
        )


@pytest.mark.asyncio
async def test_sub_agent_wrapper_applies_the_contract(monkeypatch):
    """The DATA AGENT path — the one that was actually dead in production.

    ``ask_data_agent`` calls ``sub_agent_reliability.call_llm_with_reliability``,
    whose FIRST request already replays the parent turn's assistant tool-call
    messages. That wrapper had no copy of the contract, so every data turn 400'd
    on iteration 1 and the user was told "the data layer is down" / "the model
    backend returned 400". Measured on the copy stack before and after the fix.
    """
    from app.services import sub_agent_reliability as sa_mod

    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "usage": {},
            }

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            captured["payload"] = json
            return _Resp()

    monkeypatch.setattr(sa_mod.httpx, "AsyncClient", _Client)

    messages = [
        {"role": "system", "content": "data agent"},
        {"role": "user", "content": "top 3 customers by revenue"},
        # Replayed from the parent turn — this is the message DeepSeek rejects
        # when reasoning_content is absent.
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
    ]
    await sa_mod.call_llm_with_reliability(messages, None)

    sent = captured["payload"]["messages"]
    assistant_msgs = [m for m in sent if m.get("role") == "assistant"]
    assert assistant_msgs, "no assistant message reached the provider"
    for m in assistant_msgs:
        assert "reasoning_content" in m, (
            "sub-agent request sent an assistant message without "
            "reasoning_content — the data agent path 400s on its first call"
        )
