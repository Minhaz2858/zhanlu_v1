"""Tests for LLM provider resilience: configurable timeouts + same-provider
transient backoff retry (IBM fault-tolerance workstream A).

A1: per-call timeouts come from settings (LLM_TIMEOUT_S / LLM_SYNC_TIMEOUT_S)
    instead of hard-coded 120.0/30.0.
A2: call_llm retries the SAME provider on transient errors (429/5xx/timeout)
    with backoff via llm_retry.call_with_transient_retry before failing over.
"""
import asyncio

import httpx
import pytest


# ---------------------------------------------------------------- A1 timeouts

def test_timeout_settings_exist():
    from app.config import settings
    assert getattr(settings, "LLM_TIMEOUT_S", None) == 120.0
    assert getattr(settings, "LLM_SYNC_TIMEOUT_S", None) == 30.0


@pytest.mark.asyncio
async def test_call_llm_uses_configured_timeout(monkeypatch):
    from app.config import settings
    from app.services import llm_service as ls

    monkeypatch.setattr(settings, "LLM_TIMEOUT_S", 7.5)
    captured = {}

    class FakeResponse:
        status_code = 429
        headers = {}
        text = "rate limited"

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            )

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, timeout=None, **kw):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(ls.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ls, "record_failure", lambda *a, **k: None)

    with pytest.raises(Exception):
        await ls.call_llm(prompt="hi", temperature=0.0)

    assert captured["timeout"] == 7.5  # wired from settings, not hard-coded


@pytest.mark.asyncio
async def test_get_embedding_uses_sync_timeout(monkeypatch):
    from app.config import settings
    from app.services import llm_service as ls

    monkeypatch.setattr(settings, "LLM_SYNC_TIMEOUT_S", 4.5)
    captured = {}

    class FakeClient:
        def __init__(self, timeout=None, **kw):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [{"embedding": [0.1]}]}

            return R()

    monkeypatch.setattr(ls.httpx, "Client", FakeClient)
    ls.get_embedding("hello")
    assert captured["timeout"] == 4.5  # wired from settings, not hard-coded


# ---------------------------------------------------------------- A3 tiers

def test_fallback_tier_filtering(monkeypatch):
    """Fallbacks are pinned to the primary's model tier so schemas stay
    consistent; fail-open when no same-tier fallback exists."""
    from types import SimpleNamespace
    from app.services import llm_service as ls

    providers = [
        ls.LLMProvider(name="primary", base_url="http://a", api_key="k", model="m-flagship", tier="flagship"),
        ls.LLMProvider(name="fb-reasoning", base_url="http://b", api_key="k", model="m-reasoning", tier="flagship"),
        ls.LLMProvider(name="fb-fast", base_url="http://c", api_key="k", model="m-fast", tier="fast"),
    ]
    filtered = ls.filter_providers_by_tier(providers)
    assert [p.name for p in filtered] == ["primary", "fb-reasoning"]

    # fail-open: no same-tier fallback -> all fallbacks kept
    providers_fast = [providers[0], providers[2]]
    assert [p.name for p in ls.filter_providers_by_tier(providers_fast)] == ["primary", "fb-fast"]

    # single provider: unchanged
    assert ls.filter_providers_by_tier([providers[0]]) == [providers[0]]


def test_get_llm_providers_parses_tier(monkeypatch):
    from app.config import settings
    from app.services import llm_service as ls

    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "http://a")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(settings, "LLM_MODEL", "m1")
    monkeypatch.setattr(
        settings, "LLM_FALLBACK_PROVIDERS",
        '[{"name":"fb","base_url":"http://b","api_key":"k2","model":"m2","tier":"flagship"}]',
    )
    providers = ls.get_llm_providers()
    assert providers[0].tier == "flagship"  # primary default tier
    assert providers[1].name == "fb"
    assert providers[1].tier == "flagship"


# ---------------------------------------------------------------- A2 retry

@pytest.mark.asyncio
async def test_call_llm_retries_same_provider_on_429(monkeypatch):
    """First two attempts on the primary provider get 429, third succeeds —
    must succeed WITHOUT needing a fallback provider."""
    from app.services import llm_service as ls

    calls = {"n": 0}
    sleeps = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    class FakeClient:
        def __init__(self, timeout=None, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.HTTPStatusError(
                    "429", request=httpx.Request("POST", "http://x"),
                    response=httpx.Response(
                        429, request=httpx.Request("POST", "http://x"),
                        headers={"retry-after": "0"},
                    ),
                )
            return Resp()

    monkeypatch.setattr(ls.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ls, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(ls, "record_failure", lambda *a, **k: None)

    import app.services.llm_retry as lr

    async def fast_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(lr.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(ls, "_get_tracer", lambda: type("T", (), {
        "record_llm_call": lambda self, **k: None})())

    result = await ls.call_llm(prompt="hi", temperature=0.0)

    assert result["response"] == "ok"
    assert calls["n"] == 3  # retried the SAME provider, did not need failover


@pytest.mark.asyncio
async def test_call_llm_no_retry_on_401(monkeypatch):
    """Auth errors are non-transient: fail immediately, exactly one attempt."""
    from app.services import llm_service as ls

    calls = {"n": 0}

    class FakeClient:
        def __init__(self, timeout=None, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            calls["n"] += 1
            raise httpx.HTTPStatusError(
                "401", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
            )

    monkeypatch.setattr(ls.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ls, "record_failure", lambda *a, **k: None)
    monkeypatch.setattr(ls, "_get_tracer", lambda: type("T", (), {
        "record_llm_call": lambda self, **k: None})())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await ls.call_llm(prompt="hi", temperature=0.0)
    assert exc_info.value.status_code == 401
    assert calls["n"] == 1
