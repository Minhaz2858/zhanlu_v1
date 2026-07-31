"""call_llm: permanent 4xx fails fast (no pointless failover); transient
errors still fail over to the next provider. The historical HTTPException
surface is preserved either way."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import httpx
from fastapi import HTTPException

from app.services import llm_service


def _providers():
    return [
        SimpleNamespace(name="p1", base_url="http://p1", api_key="k1", model="m1"),
        SimpleNamespace(name="p2", base_url="http://p2", api_key="k2", model="m2"),
    ]


def _http_error(status):
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(status),
    )


class _FakeClient:
    """AsyncContextManager mimicking httpx.AsyncClient.post per-URL."""
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(url)
        outcome = self.behavior[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok_response():
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "hi"}}], "usage": {}},
        request=httpx.Request("POST", "http://x"),
    )


def test_permanent_400_raises_without_failover():
    client = _FakeClient({
        "http://p1/chat/completions": _http_error(400),
        "http://p2/chat/completions": _ok_response(),
    })
    with patch.object(llm_service, "get_llm_providers", return_value=_providers()), \
         patch.object(llm_service.httpx, "AsyncClient", lambda timeout: client):
        try:
            asyncio.run(llm_service.call_llm(prompt="x"))
            assert False, "should have raised"
        except HTTPException as exc:
            assert exc.status_code == 400
    assert client.calls == ["http://p1/chat/completions"]  # no failover on 4xx


def test_transient_503_fails_over():
    client = _FakeClient({
        "http://p1/chat/completions": _http_error(503),
        "http://p2/chat/completions": _ok_response(),
    })
    with patch.object(llm_service, "get_llm_providers", return_value=_providers()), \
         patch.object(llm_service.httpx, "AsyncClient", lambda timeout: client):
        result = asyncio.run(llm_service.call_llm(prompt="x"))
    assert client.calls == ["http://p1/chat/completions", "http://p2/chat/completions"]
    assert result["response"] == "hi"
