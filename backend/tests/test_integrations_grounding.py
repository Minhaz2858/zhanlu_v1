"""Tests for the live-web-grounding hook added to the raw InvokeLLM /
InvokeLLMStream endpoints.

The raw integration path used to be a tool-less LLM proxy. When the user
message was time-sensitive ("today's news", "latest price") the base model
would refuse with "I don't have real-time browsing". This hook forces
grounding by running ``web_search`` and prepending the results to the
prompt — but only when the message matches the time-sensitive heuristic,
only when the search tool is registered, and never if anything goes wrong
(it must be fail-safe: a search API outage must not break the chat).

These tests are the spec for the hook. Run them with:
    cd /root/zhanlu/backend && python -m pytest tests/test_integrations_grounding.py -v
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Drive an awaitable to completion in a sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_search_result(*, success: bool = True, results: list[dict] | None = None) -> dict:
    return {
        "success": success,
        "query": "today's news",
        "results": results
        or [
            {"title": "Top story A", "url": "https://example.com/a", "description": "A happened"},
            {"title": "Top story B", "url": "https://example.com/b", "description": "B happened"},
        ],
        "count": len(results or []),
    }


# ---------------------------------------------------------------------------
# Pure-function tests (no FastAPI, no httpx)
# ---------------------------------------------------------------------------


def test_grounding_helper_module_importable():
    """The helper must be importable from the router module."""
    from app.routers import integrations

    assert hasattr(integrations, "_maybe_ground_prompt"), (
        "Expected _maybe_ground_prompt to be defined in app.routers.integrations"
    )
    assert asyncio.iscoroutinefunction(integrations._maybe_ground_prompt), (
        "Expected _maybe_ground_prompt to be an async function (it awaits the web_search handler)"
    )


def test_grounding_skips_when_prompt_has_no_time_sensitive_keyword():
    """A prompt like 'explain photosynthesis' must NOT trigger a web search."""
    from app.routers.integrations import _maybe_ground_prompt

    handler = MagicMock()
    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = handler
        out = _run(_maybe_ground_prompt("Explain photosynthesis in two paragraphs."))

    assert out == "Explain photosynthesis in two paragraphs."
    handler.assert_not_called()
    mock_reg.get_handler.assert_not_called()


def test_grounding_skips_when_search_handler_not_registered():
    """If the web_search tool isn't registered, leave the prompt alone."""
    from app.routers.integrations import _maybe_ground_prompt

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = None
        out = _run(_maybe_ground_prompt("Give me today's latest news."))

    assert out == "Give me today's latest news."


def test_grounding_injects_results_on_time_sensitive_prompt():
    """A 'today's news' prompt must come back with a [Live web results] block
    prepended, citing the URLs returned by the search tool."""
    from app.routers.integrations import _maybe_ground_prompt

    async def fake_handler(args, db, user_id, context=None):
        assert "query" in args, "handler must receive a query"
        return _fake_search_result()

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        out = _run(
            _maybe_ground_prompt(
                "Please summarize today's latest news for me.",
                messages=[{"role": "user", "content": "Please summarize today's latest news for me."}],
            )
        )

    assert out.startswith("[Live web results"), (
        f"Expected output to start with grounding block, got: {out[:120]!r}"
    )
    assert "Top story A" in out, "Grounded prompt must include result titles"
    assert "https://example.com/a" in out, "Grounded prompt must cite source URLs"
    assert "Please summarize today's latest news for me." in out, (
        "Original prompt must still be present after the grounding block"
    )


def test_grounding_uses_last_user_message_not_whole_prompt():
    """When the body has a `messages` list, the query sent to web_search
    must come from the last USER message — not the system prompt + history
    that makes up `body['prompt']`."""
    from app.routers.integrations import _maybe_ground_prompt

    captured: dict[str, Any] = {}

    async def fake_handler(args, db, user_id, context=None):
        captured["args"] = args
        return _fake_search_result()

    big_system = (
        "SYSTEM: you are an expert chef. " * 50
        + "USER: what should I cook? "
        + "ASSISTANT: depends on the cuisine. "
        + "USER: what's the weather in Paris today?"
    )

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        _run(
            _maybe_ground_prompt(
                prompt=big_system,
                messages=[
                    {"role": "system", "content": "you are an expert chef."},
                    {"role": "user", "content": "what should I cook?"},
                    {"role": "assistant", "content": "depends on the cuisine."},
                    {"role": "user", "content": "what's the weather in Paris today?"},
                ],
            )
        )

    assert "args" in captured, "handler must be called"
    query = captured["args"]["query"].lower()
    assert "weather in paris" in query, (
        f"Query should come from the last user message; got: {captured['args']['query']!r}"
    )
    # Must NOT include the system-prompt noise
    assert "expert chef" not in query


def test_grounding_falls_back_to_prompt_when_no_messages():
    """When only body['prompt'] is provided (legacy client), use the prompt
    itself (truncated) as the search query."""
    from app.routers.integrations import _maybe_ground_prompt

    captured: dict[str, Any] = {}

    async def fake_handler(args, db, user_id, context=None):
        captured["args"] = args
        return _fake_search_result()

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        _run(_maybe_ground_prompt("What is the latest price of Bitcoin?"))

    assert "bitcoin" in captured["args"]["query"].lower()


def test_grounding_truncates_query_to_avoid_huge_queries():
    """Very long prompts must not produce enormous search queries."""
    from app.routers.integrations import _maybe_ground_prompt

    captured: dict[str, Any] = {}

    async def fake_handler(args, db, user_id, context=None):
        captured["args"] = args
        return _fake_search_result()

    long_user_msg = "Tell me today's news. " + ("padding " * 200)

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        _run(_maybe_ground_prompt(long_user_msg, messages=[{"role": "user", "content": long_user_msg}]))

    assert len(captured["args"]["query"]) <= 300, (
        f"Query should be truncated; got length {len(captured['args']['query'])}"
    )


def test_grounding_returns_unchanged_prompt_when_handler_returns_failure():
    """If web_search returns success=False (e.g. missing API key), the
    prompt must come back exactly as it went in. The user still gets an
    answer, just ungrounded — never a 500 from a search outage."""
    from app.routers.integrations import _maybe_ground_prompt

    async def fake_handler(args, db, user_id, context=None):
        return {"success": False, "error": "no key configured"}

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        out = _run(_maybe_ground_prompt("What is today's weather in Tokyo?"))

    assert out == "What is today's weather in Tokyo?"
    assert "[Live web results" not in out


def test_grounding_returns_unchanged_prompt_when_handler_raises():
    """Fail-safe: an exception inside the handler must not propagate to
    the chat. The user should still get an LLM response (ungrounded)."""
    from app.routers.integrations import _maybe_ground_prompt

    async def fake_handler(args, db, user_id, context=None):
        raise RuntimeError("search API down")

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        out = _run(_maybe_ground_prompt("Latest score of the game?"))

    assert out == "Latest score of the game?"


def test_grounding_returns_unchanged_prompt_when_results_empty():
    """If web_search returns success=True but with no results, do not
    inject a header — it would be a confusing empty block."""
    from app.routers.integrations import _maybe_ground_prompt

    async def fake_handler(args, db, user_id, context=None):
        return {"success": True, "results": [], "count": 0}

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        out = _run(_maybe_ground_prompt("Tell me today's news."))

    assert out == "Tell me today's news."
    assert "[Live web results" not in out


def test_grounding_caps_results_to_five():
    """Even if the handler returns 20 results, only the top 5 are injected
    — keeps the prompt size bounded."""
    from app.routers.integrations import _maybe_ground_prompt

    big_results = [
        {"title": f"Story {i}", "url": f"https://example.com/{i}", "description": f"d{i}"}
        for i in range(20)
    ]

    async def fake_handler(args, db, user_id, context=None):
        return _fake_search_result(results=big_results)

    with patch("app.routers.integrations.registry") as mock_reg:
        mock_reg.get_handler.return_value = fake_handler
        out = _run(_maybe_ground_prompt("Today's top news?"))

    # Count the "1." "2." ... prefixes — must be ≤ 5
    import re
    numbered = re.findall(r"^\d+\.\s", out, re.MULTILINE)
    assert len(numbered) <= 5, f"Expected ≤ 5 results in the grounding block, got {len(numbered)}"
    assert len(numbered) >= 1, "Expected at least one numbered result"


# ---------------------------------------------------------------------------
# Endpoint-level tests — verify the hook is wired into invoke_llm and
# invoke_llm_stream (so the raw path is actually grounded).
# ---------------------------------------------------------------------------


def _login_token() -> str:
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    r = client.post(
        "/api/apps/local-zhanlu-app/auth/login",
        json={"email": "admin@zhanlu.dev", "password": "admin123"},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json().get("access_token") or r.json().get("token")


def _fake_grounded_prompt(prompt: str, *args, **kwargs) -> str:
    """Pretend the grounding hook ran and prepended results."""
    return (
        "[Live web results (grounding)]\n"
        "1. Grounded story\n   Source: https://example.com/x\n\n"
        + prompt
    )


async def _fake_grounded_prompt_async(prompt: str, *args, **kwargs) -> str:
    """Async version of the fake — required because the endpoint does
    `await _maybe_ground_prompt(...)`. Using a sync replacement would make
    the endpoint try to await a string."""
    return _fake_grounded_prompt(prompt, *args, **kwargs)


def test_invoke_llm_endpoint_runs_grounding_hook():
    """The /InvokeLLM endpoint must call the grounding hook and use the
    grounded prompt when calling the LLM."""
    from fastapi.testclient import TestClient
    from main import app

    token = _login_token()
    client = TestClient(app)

    with patch("app.routers.integrations._maybe_ground_prompt", new=_fake_grounded_prompt_async), \
         patch("app.routers.integrations.httpx.AsyncClient.post") as mock_post:
        # Make the upstream LLM call return a simple response
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        r = client.post(
            "/api/apps/local-zhanlu-app/integration-endpoints/Core/InvokeLLM",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"prompt": "Tell me today's news.", "model": "gpt-4"},
        )

    assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
    # Inspect what was sent to the LLM — the request body must contain
    # the grounded prompt, not the original.
    sent = mock_post.call_args.kwargs["json"]
    sent_text = json.dumps(sent)
    assert "[Live web results" in sent_text, (
        f"Upstream LLM call should have received the grounded prompt; got: {sent_text[:200]!r}"
    )


def test_invoke_llm_stream_endpoint_runs_grounding_hook():
    """Same wiring check for the streaming endpoint."""
    from fastapi.testclient import TestClient
    from main import app

    token = _login_token()
    client = TestClient(app)

    # Streaming: patch httpx.AsyncClient to short-circuit the actual LLM call.
    # We assert the grounded prompt was sent on the wire.
    captured: dict[str, Any] = {}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured["json"] = json
            raise RuntimeError("STOP_AFTER_CAPTURE")

    with patch("app.routers.integrations._maybe_ground_prompt", new=_fake_grounded_prompt_async), \
         patch("app.routers.integrations.httpx.AsyncClient", _FakeAsyncClient), \
         patch("app.routers.integrations.settings.OPENAI_API_KEY", "sk-fake-test-key"):
        try:
            with client.stream(
                "POST",
                "/api/apps/local-zhanlu-app/integration-endpoints/Core/InvokeLLMStream",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"prompt": "Tell me today's news.", "model": "gpt-4"},
            ) as resp:
                # Iterate a few lines to drive the stream forward, then stop.
                # The fake client raises RuntimeError on stream(), which the
                # endpoint's `except httpx.RequestError` does NOT catch — so
                # we expect either the iteration to surface that error or
                # the stream to emit an error event. Either way, the
                # grounding hook must have run first.
                for _ in resp.iter_lines():
                    break
        except (RuntimeError, ExceptionGroup):
            # The fake client raises STOP_AFTER_CAPTURE; we already captured
            # the wire payload. That's the success condition.
            pass

    assert "json" in captured, "Hook did not run before httpx call"
    sent_text = json.dumps(captured["json"])
    assert "[Live web results" in sent_text, (
        f"Stream endpoint should have sent a grounded prompt; got: {sent_text[:200]!r}"
    )
