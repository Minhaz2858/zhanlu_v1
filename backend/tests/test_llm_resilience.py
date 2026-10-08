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
