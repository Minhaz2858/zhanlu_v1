"""LLM Client — minimal in-sandbox client for the LLM Unix-socket proxy.

This module runs INSIDE the sandbox container (which has ``--network none``).
It talks to the host-side ``llm_proxy.LLMProxy`` over a bind-mounted Unix
socket.  No external dependencies — only Python stdlib ``socket`` and
``json``.

Wire protocol
-------------
Line-delimited JSON.  Send one ``\\n``-terminated JSON object, read one
back.  See ``llm_proxy.py`` docstring for the full schema.

Why a custom client instead of ``openai`` SDK?
- The sandbox has no network and no pip-installable deps (everything is
  baked into the image).
- We want a hard guarantee that no real API key ever enters the
  container — this client only knows the proxy socket path.
- The proxy is OpenAI-compatible, so the SDK shape is straightforward.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


DEFAULT_SOCKET_PATH = os.environ.get(
    "LLM_PROXY_SOCKET", "/var/run/zhanlu-llm-proxy.sock"
)
DEFAULT_MODEL = os.environ.get("LLM_PROXY_MODEL", "gpt-4o-mini")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("LLM_PROXY_TIMEOUT", "90"))


class SandboxLLMClient:
    """Minimal LLM client that talks to the host-side proxy via Unix socket.

    Usage::

        client = SandboxLLMClient()
        text = client.chat(
            system="You are a document planner.",
            user="Plan a competitive analysis report...",
        )

    Raises ``ConnectionRefusedError`` if the proxy socket is not available,
    ``TimeoutError`` if the proxy does not respond in time, and
    ``LLMProxyError`` for any non-success response from the proxy.  Callers
    should treat all three as transient and fall back to a deterministic
    generator.
    """

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.socket_path = socket_path
        self.model = model
        self.timeout = timeout

    # --- public API --------------------------------------------------------

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        messages: Optional[list[dict[str, str]]] = None,
    ) -> str:
        """Send a single chat-completion request and return the assistant text.

        ``messages`` (if provided) overrides the ``system``/``user`` shorthand
        and is sent verbatim.  This is the escape hatch for multi-turn
        conversations (rare inside the sandbox — the runner is single-shot).
        """
        if messages is None:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            if user:
                messages.append({"role": "user", "content": user})

        req_id = str(uuid.uuid4())
        request = {
            "id": req_id,
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        response = self._round_trip(request)
        if not response.get("success"):
            raise LLMProxyError(response.get("error", "unknown proxy error"))
        return response.get("content", "")

    # --- transport ---------------------------------------------------------

    def _round_trip(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON request, read one JSON response, return the parsed dict.

        Retries once on transient connection failures (the proxy may not
        have finished accepting connections yet when the container first
        starts up).
        """
        last_err: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                return self._round_trip_once(request)
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout, TimeoutError) as e:
                last_err = e
                logger.warning(
                    "LLM proxy round-trip attempt %d failed: %s: %s",
                    attempt, type(e).__name__, e,
                )
                time.sleep(0.5 * attempt)
        # All retries exhausted — re-raise the last transient error so
        # callers can decide whether to fall back.
        assert last_err is not None
        raise last_err

    def _round_trip_once(self, request: dict[str, Any]) -> dict[str, Any]:
        if not os.path.exists(self.socket_path):
            raise FileNotFoundError(
                f"LLM proxy socket not found at {self.socket_path}"
            )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            payload = (json.dumps(request) + "\n").encode("utf-8")
            sock.sendall(payload)
            # Read one newline-terminated response.  We deliberately use a
            # buffered read rather than readuntil() because some kernels
            # can split a large JSON response across multiple packets.
            buf = bytearray()
            sock.settimeout(self.timeout)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break  # peer closed
                buf.extend(chunk)
                if buf.endswith(b"\n"):
                    break
            if not buf:
                raise ConnectionResetError("LLM proxy closed without responding")
            return json.loads(buf.decode("utf-8").strip())


class LLMProxyError(RuntimeError):
    """Raised when the LLM proxy returns a non-success response."""


# --- Module-level convenience ----------------------------------------------

_default_client: Optional[SandboxLLMClient] = None


def get_default_client() -> SandboxLLMClient:
    """Return a process-wide default client (lazy-initialized)."""
    global _default_client
    if _default_client is None:
        _default_client = SandboxLLMClient()
    return _default_client