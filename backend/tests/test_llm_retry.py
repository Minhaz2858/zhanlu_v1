"""llm_retry primitives: classification gate, Retry-After, backoff, loop."""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import httpx

from app.services.api_error_classifier import FailoverReason, classify_api_error
from app.services import llm_retry as lr


def _http_error(status: int, headers=None) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(status, headers=headers or {}),
    )


def test_is_transient_gate():
    assert lr.is_transient(classify_api_error(_http_error(429)))
    assert lr.is_transient(classify_api_error(_http_error(503)))
    assert lr.is_transient(classify_api_error(_http_error(500)))
    assert not lr.is_transient(classify_api_error(_http_error(400)))
    assert not lr.is_transient(classify_api_error(_http_error(401)))
    assert not lr.is_transient(classify_api_error(_http_error(404)))
    # unknown errors are fail-safe: no retry storm
    assert not lr.is_transient(classify_api_error(ValueError("weird")))


def test_extract_status_code():
    assert lr.extract_status_code(_http_error(429)) == 429
    assert lr.extract_status_code(ValueError("nope")) is None


def test_retry_after_seconds():
    assert lr.retry_after_seconds(_http_error(429, {"Retry-After": "7"})) == 7.0
    assert lr.retry_after_seconds(_http_error(429)) is None
    assert lr.retry_after_seconds(_http_error(429, {"Retry-After": "junk"})) is None


def test_max_attempts_budget():
    assert lr.max_attempts_for(classify_api_error(_http_error(429))) == 3
    assert lr.max_attempts_for(classify_api_error(_http_error(500))) == 2
    assert lr.max_attempts_for(classify_api_error(ValueError("x"))) == 1


def test_next_backoff_honors_retry_after_and_caps(monkeypatch):
    monkeypatch.setattr(lr.random, "uniform", lambda a, b: b)
    assert lr.next_backoff(0, None) == 1.0
    assert lr.next_backoff(1, None) == 2.0
    assert lr.next_backoff(10, None) == 30.0  # capped
    assert lr.next_backoff(0, 5.0) == 6.0     # retry-after + <=1s jitter
    assert lr.next_backoff(0, 100.0) == 30.0  # capped


def test_retry_loop_succeeds_after_transients():
    calls = {"n": 0}
    retries = []

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)  # budget: 3 attempts
        return "ok"

    async def fake_sleep(_):
        pass

    async def on_retry(next_attempt, delay, ce):
        retries.append((next_attempt, ce.reason))

    result = asyncio.run(
        lr.call_with_transient_retry(factory, on_retry=on_retry, _sleep=fake_sleep)
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert retries == [(2, FailoverReason.rate_limit), (3, FailoverReason.rate_limit)]


def test_retry_loop_raises_permanent_immediately():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _http_error(400)

    async def fake_sleep(_):
        pass

    try:
        asyncio.run(lr.call_with_transient_retry(factory, _sleep=fake_sleep))
        assert False, "should have raised"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1


def test_retry_loop_exhausts_budget():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _http_error(500)  # budget: 2 attempts

    async def fake_sleep(_):
        pass

    try:
        asyncio.run(lr.call_with_transient_retry(factory, _sleep=fake_sleep))
        assert False, "should have raised"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 2
