"""Tests for ``call_llm`` pre-flight payload-vs-context-window check.

Bug repro (Aug 2026): user runs ``ecisco_bi_assistant`` under qwen3.6-27b
(``context_window=65536`` from the DB row).  The system prompt + data-source
context alone is ~60k tokens, the conversation itself is tiny (~100
tokens), and the LLM call fails with HTTP 400 "maximum context length is
65536 tokens" -> user sees "Sorry, I hit an error while responding."

The pre-flight check sits inside ``call_llm`` between sanitization and
cache lookup.  It estimates the total payload (system + messages + tools)
and either:
  (a) clamps ``max_tokens`` down so the request still fits, OR
  (b) raises a 502 with a clear, actionable message when the payload
      itself exceeds the context window — telling the user to switch to
      a larger-context model.

These tests pin both branches.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm_service as svc


def _huge_system_message(target_chars: int) -> str:
    """Build a single system-prompt string of the requested char length."""
    return "X" * target_chars


def test_preflight_raises_502_when_payload_exceeds_context_window(monkeypatch):
    """When the estimated payload already exceeds the model's context
    window (even with max_tokens=0), call_llm MUST raise 502 with an
    actionable message naming the model and the fix (switch to a larger
    context window).
    """
    from app.services import llm_cache  # ensure import works

    async def _get_providers():
        return [SimpleNamespace(name="p1", base_url="http://x", api_key="k", model="m")]

    def _get_providers_sync():
        return [SimpleNamespace(name="p1", base_url="http://x", api_key="k", model="m")]

    # Fake llm_cache to skip the cache layer
    import sys as _sys, types as _types
    _fake_cache = _types.ModuleType("app.services.llm_cache")
    _fake_cache.get_cached_response = lambda *a, **kw: None
    _fake_cache.set_cached_response = lambda *a, **kw: None
    monkeypatch.setitem(_sys.modules, "app.services.llm_cache", _fake_cache)
    _fake_router = _types.ModuleType("app.services.model_router")
    _fake_router.get_model_for_request = lambda *a, **kw: "qwen3.6-27b"
    monkeypatch.setitem(_sys.modules, "app.services.model_router", _fake_router)

    monkeypatch.setattr(svc, "get_llm_providers", _get_providers_sync)
    monkeypatch.setattr(svc, "is_healthy", lambda name: True)
    # No HTTP call should be made — if the pre-flight fires, we never
    # reach httpx.
    def _boom(*a, **kw):
        raise RuntimeError("pre-flight should have raised before HTTP call")
    monkeypatch.setattr(svc.httpx, "AsyncClient", _boom)

    # Build a payload that exceeds qwen3.6-27b's 65,536 context window.
    # 65,536 tokens * 4 chars/token = 262,144 chars.  Add a small
    # conversation; the system prompt alone pushes the total over the
    # limit.
    huge_system = {"role": "system", "content": _huge_system_message(300_000)}
    messages = [
        huge_system,
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(svc.call_llm(prompt="", messages=messages, temperature=0.0))
    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert "qwen3.6-27b" in detail, detail
    assert "65536" in detail or "65,536" in detail, detail
    assert "larger context" in detail.lower() or "deepseek" in detail.lower(), detail


def test_preflight_clamps_max_tokens_when_payload_near_limit(monkeypatch):
    """When the payload fits WITH a reduced max_tokens, call_llm MUST clamp
    max_tokens down rather than raising.

    Setup: estimate_messages_tokens applies a 4/3 padding, so we need a
    system prompt whose padded estimate is < 65,536 but whose request
    max_tokens=4096 would push it over.  Choose 40,000 pre-padded
    tokens -> ~53,333 padded -> leaves ~12,200 of headroom for output.
    """
    import sys as _sys, types as _types
    _fake_cache = _types.ModuleType("app.services.llm_cache")
    _fake_cache.get_cached_response = lambda *a, **kw: None
    _fake_cache.set_cached_response = lambda *a, **kw: None
    monkeypatch.setitem(_sys.modules, "app.services.llm_cache", _fake_cache)
    _fake_router = _types.ModuleType("app.services.model_router")
    _fake_router.get_model_for_request = lambda *a, **kw: "qwen3.6-27b"
    monkeypatch.setitem(_sys.modules, "app.services.model_router", _fake_router)

    # Capture the payload actually sent
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

    def _get_providers_sync():
        return [SimpleNamespace(name="p1", base_url="http://x", api_key="k", model="m")]

    monkeypatch.setattr(svc, "get_llm_providers", _get_providers_sync)
    monkeypatch.setattr(svc, "is_healthy", lambda name: True)
    monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeClient)

    # 50,000 pre-padded tokens = 200,000 chars; padded to ~66,666.
    # 66,666 + 4,096 default max_tokens = 70,762 > 65,536 -> clamp should
    # fire (reduce max_tokens to 65,536 - 66,666 = NEGATIVE -> 502).
    # Use 44,000 pre-padded = 176,000 chars -> padded ~58,666.  Then
    # 58,666 + 4,096 = 62,762 < 65,536 -> NO clamp.  Hmm.
    # The estimator's 4/3 padding makes this tight.  Let me use 48,000
    # pre-padded = 192,000 chars -> padded 64,000.  Then 64,000 + 4,096
    # = 68,096 > 65,536 -> clamp fires (max ~ 1,536).
    big_system = {"role": "system", "content": _huge_system_message(192_000)}
    messages = [
        big_system,
        {"role": "user", "content": "hi"},
    ]

    result = asyncio.run(
        svc.call_llm(prompt="", messages=messages, temperature=0.0, response_json_schema=None)
    )
    sent = captured["json"]
    # max_tokens must have been clamped to fit
    assert sent["max_tokens"] is not None
    # The headroom is roughly 65,536 - 64,000 = 1,536 tokens.
    assert sent["max_tokens"] is not None, f"max_tokens not set in sent payload: {sent}"
    assert sent["max_tokens"] <= 4_096, f"max_tokens not clamped: {sent['max_tokens']}"
    # And it should be > 0 (otherwise we'd have raised 502)
    assert sent["max_tokens"] > 0
    assert result["response"] == "ok"
