"""Tests for ``_supports_parallel_tool_calls`` capability flag and the
``LLM_PARALLEL_TOOL_CALLS_ENABLED`` flag-driven injection of
``parallel_tool_calls: True`` into LLM request payloads.

Scope: this file covers the CAPABILITY-FLAG INJECTION only.  Two related
but distinct concepts live elsewhere:
  * ``test_force_pause_parallel_tools.py`` — force-pause trigger when
    the LLM streams multiple parallel tool_calls in one turn.
  * ``test_agents_token_streaming.py`` — SSE reassembly of multiple
    parallel tool_calls in one turn.
This file covers the upstream capability declaration that the LLM must
emit parallel tool_calls in the first place.
"""
import os
import sys
import asyncio
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services import llm_service as svc


# ── helper-level tests ─────────────────────────────────────────────────────

class TestSupportsParallelToolCalls:

    def test_deepseek_models_supported(self):
        for m in ["deepseek-chat", "deepseek-v4-flash", "deepseek-coder", "DEEPSEEK-X"]:
            assert svc._supports_parallel_tool_calls(m) is True, m

    def test_openai_models_supported(self):
        for m in ["openai/gpt-4o", "gpt-4o", "gpt-4o-mini", "openai/gpt-4-turbo"]:
            assert svc._supports_parallel_tool_calls(m) is True, m

    def test_anthropic_models_rejected(self):
        for m in ["claude-sonnet-4", "claude-opus-4", "claude-3-haiku",
                  "anthropic/claude-3-5-sonnet"]:
            assert svc._supports_parallel_tool_calls(m) is False, m

    def test_unknown_models_rejected(self):
        # Conservative: if we don't recognise the model, we DON'T inject.
        for m in ["qwen3.6-27b", "llama-3-70b", "mistral-large", ""]:
            assert svc._supports_parallel_tool_calls(m) is False, m


# ── payload-level integration tests ────────────────────────────────────────

class TestPayloadInjection:

    def _capture_payload(self, monkeypatch, model_name: str, flag_value: bool):
        """Stub out the LLM HTTP layer; return the JSON posted."""
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
        _fake_router.get_model_for_request = lambda *a, **kw: model_name
        monkeypatch.setitem(_sys.modules, "app.services.model_router", _fake_router)

        def _get_providers():
            return [SimpleNamespace(name="p1", base_url="http://x",
                                    api_key="k", model="m")]
        monkeypatch.setattr(svc, "get_llm_providers", _get_providers)
        monkeypatch.setattr(svc, "is_healthy", lambda name: True)
        monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeClient)
        # Force the flag
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_PARALLEL_TOOL_CALLS_ENABLED", flag_value)

        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        asyncio.run(
            svc.call_llm(prompt="", messages=messages, temperature=0.0)
        )
        return captured["json"]

    def test_flag_off_no_injection_for_any_model(
        self, monkeypatch,
    ):
        """Default behaviour (flag=False) must be UNCHANGED — no field
        added, regardless of model.
        """
        for m in ["deepseek-chat", "gpt-4o", "claude-sonnet-4", "qwen3.6-27b"]:
            payload = self._capture_payload(monkeypatch, m, flag_value=False)
            assert "parallel_tool_calls" not in payload, (
                f"flag off must NOT inject; got {payload.get('parallel_tool_calls')!r} "
                f"for model {m}"
            )

    def test_flag_on_deepseek_model_injects_true(self, monkeypatch):
        payload = self._capture_payload(
            monkeypatch, "deepseek-chat", flag_value=True,
        )
        assert payload.get("parallel_tool_calls") is True

    def test_flag_on_openai_model_injects_true(self, monkeypatch):
        payload = self._capture_payload(
            monkeypatch, "gpt-4o", flag_value=True,
        )
        assert payload.get("parallel_tool_calls") is True

    def test_flag_on_anthropic_model_does_not_inject(self, monkeypatch):
        """Anthropic rejects parallel_tool_calls; the helper must exclude
        them so we don't 400 the request.
        """
        payload = self._capture_payload(
            monkeypatch, "claude-sonnet-4", flag_value=True,
        )
        assert "parallel_tool_calls" not in payload

    def test_flag_on_unknown_model_does_not_inject(self, monkeypatch):
        """Conservative default: unrecognised models get no injection."""
        payload = self._capture_payload(
            monkeypatch, "qwen3.6-27b", flag_value=True,
        )
        assert "parallel_tool_calls" not in payload
