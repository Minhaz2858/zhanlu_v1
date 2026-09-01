"""Tests for ``_sanitize_tool_call_pairing`` defense-in-depth validator.

Reproduces the "qwen3.6-27b agent not responding" bug: when a message-rebuild
or compaction path strips tool-result messages but leaves ``assistant.tool_calls``
intact, DeepSeek (the catalog default for background calls) returns HTTP 400
"insufficient tool messages following tool_calls message" -> ``record_failure``
opens the circuit-breaker -> all providers exhausted -> 502 with no detail ->
"Sorry, I hit an error while responding."

The validator is the cheapest universal fix: it runs RIGHT BEFORE the HTTP
request inside ``call_llm`` and strips orphan tool_calls.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.llm_service import _sanitize_tool_call_pairing


def _asst_with_tcs(tc_ids, content=""):
    """Build an assistant message carrying tool_calls."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            for tid in tc_ids
        ],
    }


def _tool_msg(tid, content="result"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


def test_passthrough_when_no_assistant_tool_calls():
    """Messages with no assistant.tool_calls must be returned unchanged."""
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "bye"},
    ]
    out = _sanitize_tool_call_pairing(messages)
    assert out == messages
    # Must not mutate the input
    assert out is not messages or all(o is m for o, m in zip(out, messages))


def test_valid_paired_tool_call_round_trip():
    """Well-formed assistant.tool_calls + matching tool results are preserved."""
    messages = [
        {"role": "user", "content": "do thing"},
        _asst_with_tcs(["a", "b"]),
        _tool_msg("a", "result-a"),
        _tool_msg("b", "result-b"),
        {"role": "assistant", "content": "done"},
    ]
    out = _sanitize_tool_call_pairing(messages)
    assert out == messages


def test_strips_orphan_tool_calls_when_tool_results_missing():
    """Bug repro: assistant has tool_calls but no tool result messages after it."""
    messages = [
        {"role": "user", "content": "do thing"},
        _asst_with_tcs(["a", "b"]),
        {"role": "assistant", "content": "(lost the results)"},
    ]
    out = _sanitize_tool_call_pairing(messages)
    # The first assistant must have tool_calls stripped (no matching tool results)
    asst1 = out[1]
    assert asst1["role"] == "assistant"
    assert "tool_calls" not in asst1, f"orphan tool_calls should be stripped: {asst1}"
    # Content preserved
    assert asst1["content"] == ""
    # Other messages unchanged
    assert out[2]["role"] == "assistant"


def test_strips_only_unmatched_tool_calls_keeps_matched_ones():
    """Assistant has 2 tool_calls but only 1 has a matching tool result."""
    messages = [
        _asst_with_tcs(["a", "b"]),
        _tool_msg("a", "result-a"),
        # 'b' has no matching tool result
        {"role": "assistant", "content": "next turn"},
    ]
    out = _sanitize_tool_call_pairing(messages)
    asst1 = out[0]
    assert "tool_calls" in asst1
    kept_ids = [c["id"] for c in asst1["tool_calls"]]
    assert kept_ids == ["a"], f"only matched tool_call should be kept: {kept_ids}"


def test_drops_stray_tool_messages_without_preceding_assistant_tc():
    """Stray tool messages (e.g. from compaction) must be removed."""
    messages = [
        {"role": "user", "content": "hi"},
        # Stray tool message — no preceding assistant.tool_calls mentions 'x'
        _tool_msg("x", "leftover"),
        {"role": "assistant", "content": "hi back"},
    ]
    out = _sanitize_tool_call_pairing(messages)
    assert len(out) == 2
    assert all(m.get("role") != "tool" for m in out)


def test_tool_results_must_be_contiguous():
    """If a non-tool message intervenes between assistant.tool_calls and its
    tool results, the tool results are invalid -> strip the tool_calls."""
    messages = [
        _asst_with_tcs(["a"]),
        {"role": "user", "content": "wait"},  # intervenes
        _tool_msg("a", "result-a"),  # too late — not adjacent
    ]
    out = _sanitize_tool_call_pairing(messages)
    asst1 = out[0]
    assert "tool_calls" not in asst1, (
        f"non-contiguous tool results must invalidate tool_calls: {asst1}"
    )


def test_empty_messages_returns_empty():
    assert _sanitize_tool_call_pairing([]) == []
    assert _sanitize_tool_call_pairing(None) == []  # type: ignore[arg-type]


def test_does_not_mutate_input():
    """The function must return a new list, never mutate the caller's list."""
    original = [
        _asst_with_tcs(["a"]),
        # missing tool result for 'a'
    ]
    snapshot = [dict(m) for m in original]
    _sanitize_tool_call_pairing(original)
    # The original list's assistant message must still carry the orphan tool_calls
    assert "tool_calls" in original[0]
    assert original[0]["tool_calls"][0]["id"] == "a"
    # Sanity: snapshot untouched
    assert snapshot[0]["tool_calls"][0]["id"] == "a"


def test_integration_with_call_llm_sends_clean_payload(monkeypatch):
    """End-to-end: call_llm must send a sanitized payload even if caller
    passes orphan tool_calls.  We assert by inspecting the request_payload
    handed to the HTTP layer.
    """
    from types import SimpleNamespace
    from app.services import llm_service as svc

    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _FakeResp()

    def _get_providers():
        return [SimpleNamespace(name="p1", base_url="http://x", api_key="k", model="m")]

    # get_cached_response / llm_cache are deferred-imported inside call_llm,
    # so we must inject a fake module under the name call_llm will look up.
    import sys
    import types
    fake_cache = types.ModuleType("app.services.llm_cache")
    fake_cache.get_cached_response = lambda *a, **kw: None
    fake_cache.set_cached_response = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "app.services.llm_cache", fake_cache)
    # Same for get_model_for_request which is also deferred-imported.
    fake_router = types.ModuleType("app.services.model_router")
    fake_router.get_model_for_request = lambda *a, **kw: "m"
    monkeypatch.setitem(sys.modules, "app.services.model_router", fake_router)

    monkeypatch.setattr(svc, "get_llm_providers", _get_providers)
    monkeypatch.setattr(svc, "is_healthy", lambda name: True)
    monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeClient)

    bad_messages = [
        {"role": "user", "content": "do thing"},
        _asst_with_tcs(["orphan-1", "orphan-2"]),  # NO matching tool results
    ]

    import asyncio
    result = asyncio.run(
        svc.call_llm(prompt="", messages=bad_messages, temperature=0.0)
    )
    sent_messages = captured["json"]["messages"]
    # The orphan tool_calls must have been stripped before sending
    sent_asst = sent_messages[1]
    assert "tool_calls" not in sent_asst, (
        f"call_llm must sanitize orphan tool_calls before send; got {sent_asst}"
    )
    assert result["response"] == "ok"
