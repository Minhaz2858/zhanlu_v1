"""Integration test: call_llm() applies smart_truncate before sending.

P1-5 acceptance: when the conversation has an oversized tool result and
the routed model is qwen3.6-27b (12,288-token cap), the LLM call sees
the truncated result, not the raw 50k-token result.
"""
import os
import sys
import asyncio
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def test_call_llm_applies_smart_truncate_to_tool_results(monkeypatch):
    """When qwen3.6-27b is routed, oversized tool results must be capped
    to 12,288 tokens (49,152 chars) before the HTTP send.
    """
    from app.services import llm_service as svc

    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _FakeResp()

    import sys as _sys, types as _types
    _fake_cache = _types.ModuleType("app.services.llm_cache")
    _fake_cache.get_cached_response = lambda *a, **kw: None
    _fake_cache.set_cached_response = lambda *a, **kw: None
    monkeypatch.setitem(_sys.modules, "app.services.llm_cache", _fake_cache)
    _fake_router = _types.ModuleType("app.services.model_router")
    _fake_router.get_model_for_request = lambda *a, **kw: "qwen3.6-27b"
    monkeypatch.setitem(_sys.modules, "app.services.model_router", _fake_router)

    def _get_providers():
        return [SimpleNamespace(name="p1", base_url="http://x", api_key="k", model="m")]
    monkeypatch.setattr(svc, "get_llm_providers", _get_providers)
    monkeypatch.setattr(svc, "is_healthy", lambda name: True)
    monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeClient)

    # Conversation with 3 tool messages: one oversized (50k tokens),
    # plus two small ones so the keep_recent=2 protection kicks in.
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "X" * 200_000},  # 50,000 tokens
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "b", "content": "small"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c", "content": "small"},
    ]

    asyncio.run(svc.call_llm(prompt="", messages=messages, temperature=0.0))
    sent = captured["json"]
    sent_messages = sent["messages"]
    # The tool result for 'a' must be truncated to qwen's cap (12,288
    # tokens = 49,152 chars), not the original 200,000 chars.
    tool_a = next(m for m in sent_messages
                  if m.get("role") == "tool" and m.get("tool_call_id") == "a")
    assert len(tool_a["content"]) <= 50_000, (
        f"qwen3.6-27b tool result should be capped to 12,288 tokens "
        f"(49,152 chars); got {len(tool_a['content'])} chars"
    )
    # Smaller than 200,000 (the original).
    assert len(tool_a["content"]) < 200_000
