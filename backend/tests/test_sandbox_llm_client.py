"""Unit tests for the in-container LLM client (SandboxLLMClient).

Tests the line-delimited JSON protocol, retry behavior on transient
connection failures, and request format — no real LLM is contacted.

Implementation note: the client uses blocking stdlib sockets, so the
stub server is run in a background thread (not asyncio) to avoid
starving the event loop.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.sandbox.llm_client import (
    LLMProxyError,
    SandboxLLMClient,
    get_default_client,
)


# ── Thread-based stub server ──────────────────────────────────────────────


class _StubServer:
    """Synchronous Unix-socket echo server running in its own thread.

    The LLM client uses blocking stdlib sockets, so the stub server must
    NOT share an event loop with the client (the blocking recv() would
    starve the loop and the server would never reply).
    """

    def __init__(self, handler):
        self.handler = handler
        self.sock_path = tempfile.mktemp(prefix="test-llm-stub-", suffix=".sock")
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self):
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        srv.settimeout(0.1)  # let us poll for stop
        self._server = srv
        try:
            os.chmod(self.sock_path, 0o666)
        except OSError:
            pass
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    def _serve(self):
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_one, args=(conn,), daemon=True)
            t.start()

    def _handle_one(self, conn: socket.socket):
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return
            req = json.loads(buf.decode("utf-8").strip())
            resp = self.handler(req)
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ── Tests ────────────────────────────────────────────────────────────────


def test_chat_returns_assistant_content_on_success():
    def handler(req):
        return {"id": req["id"], "success": True, "content": "pong", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with _StubServer(handler) as srv:
        client = SandboxLLMClient(socket_path=srv.sock_path, model="gpt-4o-mini", timeout=3)
        text = client.chat(system="sys", user="hello")
    assert text == "pong"


def test_chat_raises_on_proxy_error():
    def handler(req):
        return {"id": req["id"], "success": False, "error": "upstream blew up"}

    with _StubServer(handler) as srv:
        client = SandboxLLMClient(socket_path=srv.sock_path, timeout=3)
        with pytest.raises(LLMProxyError) as exc:
            client.chat(system="sys", user="hello")
    assert "upstream" in str(exc.value).lower()


def test_chat_includes_model_in_request():
    seen: list[str] = []

    def handler(req):
        seen.append(req["model"])
        return {"id": req["id"], "success": True, "content": "ok", "usage": {}}

    with _StubServer(handler) as srv:
        client = SandboxLLMClient(socket_path=srv.sock_path, model="custom-model", timeout=3)
        client.chat(system="sys", user="hello")
    assert seen == ["custom-model"]


def test_chat_retries_on_missing_socket():
    """A missing socket should surface an OSError after retries."""
    client = SandboxLLMClient(
        socket_path="/tmp/test-llm-stub-missing-xyz.sock",
        timeout=1,
    )
    with pytest.raises((FileNotFoundError, ConnectionRefusedError, OSError)):
        client.chat(system="sys", user="hello", max_tokens=5)


def test_get_default_client_is_singleton():
    c1 = get_default_client()
    c2 = get_default_client()
    assert c1 is c2


def test_chat_supports_custom_messages_list():
    seen_messages: list[list] = []

    def handler(req):
        seen_messages.append(req["messages"])
        return {"id": req["id"], "success": True, "content": "ok", "usage": {}}

    with _StubServer(handler) as srv:
        client = SandboxLLMClient(socket_path=srv.sock_path, timeout=3)
        client.chat(
            system="ignored",
            user="ignored",
            messages=[
                {"role": "system", "content": "explicit"},
                {"role": "user", "content": "explicit-user"},
            ],
        )
    assert seen_messages[0][0]["content"] == "explicit"
    assert seen_messages[0][1]["content"] == "explicit-user"