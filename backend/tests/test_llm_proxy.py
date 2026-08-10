"""Unit tests for the host-side LLM Unix-socket proxy.

Covers:
- _SlidingWindow rate limiter logic (sync, no fixtures needed)
- Proxy protocol / model allowlist / rate limit / size cap (async,
  using a thread-based mock upstream so the proxy's asyncio loop is
  never blocked by test code)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.sandbox.llm_proxy import (
    LLMProxy,
    MAX_REQUEST_BYTES,
    RATE_LIMIT_REQUESTS,
    _SlidingWindow,
)


# ── _SlidingWindow unit tests ────────────────────────────────────────────


def test_sliding_window_allows_below_limit():
    win = _SlidingWindow(limit=3, window_seconds=60)
    assert win.allow("k1") is True
    assert win.allow("k1") is True
    assert win.allow("k1") is True


def test_sliding_window_blocks_over_limit():
    win = _SlidingWindow(limit=2, window_seconds=60)
    assert win.allow("k1") is True
    assert win.allow("k1") is True
    assert win.allow("k1") is False


def test_sliding_window_isolates_keys():
    win = _SlidingWindow(limit=1, window_seconds=60)
    assert win.allow("k1") is True
    assert win.allow("k1") is False
    assert win.allow("k2") is True


def test_sliding_window_releases_after_window():
    win = _SlidingWindow(limit=1, window_seconds=1)
    now = 1000.0
    assert win.allow("k1", now=now) is True
    assert win.allow("k1", now=now + 0.5) is False
    assert win.allow("k1", now=now + 1.5) is True


# ── LLMProxy integration tests (async) ───────────────────────────────────


@pytest_asyncio.fixture
async def proxy_with_mocked_api():
    """Spin up a real LLMProxy on a temp socket with upstream HTTP stubbed.

    We bypass the real httpx client entirely by patching ``_forward``
    directly — the proxy's HTTP plumbing is not what these tests cover;
    we care about the protocol / allowlist / rate-limit / size-cap
    logic that wraps it.
    """
    sock_path = tempfile.mktemp(prefix="test-llm-proxy-", suffix=".sock")
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    proxy = LLMProxy(socket_path=sock_path, allowed_models=("gpt-4o-mini",))

    # Replace ``_forward`` with a stub that records the call and
    # returns a canned successful response.  This isolates the proxy
    # logic from the httpx transport (which is awkward to mock cleanly).
    call_log: list[dict] = []

    async def fake_forward(*, model, messages, temperature, max_tokens):
        call_log.append({"model": model, "n_messages": len(messages)})
        return "Hello from mock LLM", {"prompt_tokens": 5, "completion_tokens": 7}

    proxy._forward = fake_forward  # type: ignore[assignment]
    proxy._http = AsyncMock()  # never actually used
    proxy.call_log = call_log  # attach for tests to inspect

    await proxy.start()
    try:
        yield proxy, call_log
    finally:
        await proxy.stop()
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass


def _send_request_sync(sock_path: str, payload: dict, timeout: float = 5.0) -> dict:
    """Send one JSON request, read one JSON response via stdlib socket.

    Running in a thread (the proxy's asyncio loop is in another thread)
    is fine because socket.send/recv block independently of asyncio.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        return json.loads(buf.decode("utf-8").strip())


@pytest.mark.asyncio
async def test_proxy_responds_to_valid_request(proxy_with_mocked_api):
    proxy, _ = proxy_with_mocked_api
    resp = await asyncio.to_thread(_send_request_sync, proxy.socket_path, {
        "id": "test-1",
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.3,
        "max_tokens": 100,
    })
    assert resp["id"] == "test-1"
    assert resp["success"] is True
    assert resp["content"] == "Hello from mock LLM"
    assert resp["usage"]["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_proxy_rejects_unknown_model(proxy_with_mocked_api):
    proxy, call_log = proxy_with_mocked_api
    resp = await asyncio.to_thread(_send_request_sync, proxy.socket_path, {
        "id": "test-2",
        "model": "gpt-999-not-allowed",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    })
    assert resp["success"] is False
    assert "allowlist" in resp["error"].lower()
    # Upstream must NOT have been called — model gate short-circuited.
    assert call_log == []


@pytest.mark.asyncio
async def test_proxy_rejects_oversized_request(proxy_with_mocked_api):
    proxy, _ = proxy_with_mocked_api
    big_messages = [{"role": "user", "content": "x" * (MAX_REQUEST_BYTES + 1000)}]
    resp = await asyncio.to_thread(_send_request_sync, proxy.socket_path, {
        "id": "test-3",
        "model": "gpt-4o-mini",
        "messages": big_messages,
        "max_tokens": 10,
    })
    assert resp["success"] is False


@pytest.mark.asyncio
async def test_proxy_rejects_empty_messages(proxy_with_mocked_api):
    proxy, _ = proxy_with_mocked_api
    resp = await asyncio.to_thread(_send_request_sync, proxy.socket_path, {
        "id": "test-4",
        "model": "gpt-4o-mini",
        "messages": [],
        "max_tokens": 10,
    })
    assert resp["success"] is False


@pytest.mark.asyncio
async def test_proxy_enforces_rate_limit(proxy_with_mocked_api):
    proxy, _ = proxy_with_mocked_api
    # Lower the limit so this test runs quickly.  Production rate-limit
    # is 30/min — too slow for a test loop.
    proxy._limiter = _SlidingWindow(limit=5, window_seconds=60)

    # Use asyncio for the client side to avoid blocking-socket
    # backpressure deadlocking against the proxy's writer.drain().
    reader, writer = await asyncio.open_unix_connection(proxy.socket_path)
    try:
        last_resp = None
        for i in range(8):  # 5 allowed + 3 rejected
            payload = {
                "id": f"rl-{i}",
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 5,
            }
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
            line = await reader.readline()
            last_resp = json.loads(line.decode("utf-8").strip())
        assert last_resp is not None
        assert last_resp["success"] is False, "rate limit never kicked in"
        assert "rate" in last_resp["error"].lower()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_proxy_handles_malformed_json(proxy_with_mocked_api):
    proxy, _ = proxy_with_mocked_api
    def _send_garbage():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(proxy.socket_path)
            sock.sendall(b"{not valid json}\n")
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            return json.loads(buf.decode("utf-8").strip())
    resp = await asyncio.to_thread(_send_garbage)
    assert resp["success"] is False
    assert "json" in resp["error"].lower()