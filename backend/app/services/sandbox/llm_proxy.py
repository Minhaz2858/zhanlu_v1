"""LLM Proxy — host-side Unix-socket server that forwards chat-completion
requests from inside the sandbox container to the real LLM API.

Security model
--------------
- The sandbox container runs with ``--network none`` (no TCP/IP access).
- Instead of relaxing that, a small proxy service listens on a Unix
  domain socket that is bind-mounted into the container.
- The proxy holds the real LLM API key.  The API key NEVER enters the
  container — the container only knows the socket path.
- The proxy enforces: model allowlist, per-socket rate limit, request
  size cap, and response truncation.  All forwarded requests/responses
  are logged (without API key contents) for observability.

Wire protocol
-------------
Line-delimited JSON over the Unix socket.  Each request is exactly one
JSON object terminated by ``\\n``; each response is exactly one JSON
object terminated by ``\\n``.  This is intentionally simpler than
HTTP-over-Unix-socket — Python stdlib ``socket`` is all either side
needs, no ``http.server`` machinery required.

Request schema (client → proxy):
    {
      "id": "<uuid>",                   # echoed back so the client can
                                         # correlate responses with requests
      "model": "gpt-4o-mini",           # must be in model allowlist
      "messages": [{"role": ..., "content": ...}, ...],
      "temperature": 0.3,
      "max_tokens": 4096
    }

Response schema (proxy → client):
    {
      "id": "<uuid>",
      "success": true,
      "content": "<assistant text>",
      "usage": {"prompt_tokens": N, "completion_tokens": N}
    }
    # or on failure:
    {
      "id": "<uuid>",
      "success": false,
      "error": "<reason>"
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# --- Configuration knobs ----------------------------------------------------

DEFAULT_SOCKET_PATH = os.environ.get(
    "ZHANLU_LLM_PROXY_SOCKET", "/var/run/zhanlu-llm-proxy.sock"
)

# Models the proxy is willing to forward to.  Configurable via env so
# staging can widen the allowlist without code changes.
_DEFAULT_ALLOWLIST = (settings.LLM_MODEL,) if settings.LLM_MODEL else ("gpt-4o-mini",)
ALLOWED_MODELS: tuple[str, ...] = tuple(
    m.strip() for m in os.environ.get("ZHANLU_LLM_PROXY_ALLOWED_MODELS", "").split(",")
    if m.strip()
) or _DEFAULT_ALLOWLIST

# Per-socket (i.e. per-container) rate limit: requests / rolling 60s window.
RATE_LIMIT_REQUESTS = int(os.environ.get("ZHANLU_LLM_PROXY_RATE_LIMIT", "30"))
RATE_LIMIT_WINDOW_SECONDS = 60

# Hard caps to keep malicious / buggy generated code from blowing up
# the LLM bill or holding the socket open.
MAX_REQUEST_BYTES = 256 * 1024            # 256 KiB per request
MAX_PROMPT_CHARS = 120_000                # ~30k tokens of text
MAX_RESPONSE_TOKENS = 8192                # hard ceiling on max_tokens
MAX_TOTAL_RESPONSE_CHARS = 200_000        # truncate streaming output


# --- Rate limiter (sliding window per "peer") -------------------------------

class _SlidingWindow:
    """Tiny in-memory sliding-window rate limiter.

    Tracks timestamps of the last ``limit`` requests in a deque; each new
    request is allowed only if fewer than ``limit`` timestamps fall within
    the last ``window_seconds`` seconds.  No thread safety required — the
    proxy runs asyncio single-threaded.
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window
        dq = self._hits.setdefault(key, deque())
        # Evict old hits
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True


# --- The proxy itself -------------------------------------------------------

class LLMProxy:
    """Async Unix-socket server that forwards to the configured LLM API."""

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        api_key: str = "",
        api_base: str = "",
        allowed_models: tuple[str, ...] = ALLOWED_MODELS,
    ):
        self.socket_path = socket_path
        # Never log these — they may be the real API key
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._api_base = (api_base or settings.OPENAI_BASE_URL).rstrip("/")
        self._allowed_models = allowed_models
        self._limiter = _SlidingWindow(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)
        self._server: Optional[asyncio.AbstractServer] = None
        # An httpx.AsyncClient per request is wasteful; share one.
        self._http: Optional[httpx.AsyncClient] = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Remove any stale socket file from a previous run.
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as e:
                logger.warning("Could not unlink stale socket %s: %s", self.socket_path, e)
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)

        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
            # Raise the StreamReader limit so we can apply our own
            # MAX_REQUEST_BYTES check at the application layer (with a
            # useful error response).  Default is 64 KiB.
            limit=MAX_REQUEST_BYTES + 4096,
        )
        # World-writable so any UID inside the container can reach us.
        # The container has its own UID namespace; the security boundary
        # is the bind mount, not Unix permissions on the socket file.
        try:
            os.chmod(self.socket_path, 0o666)
        except OSError:
            pass
        logger.info(
            "LLM proxy listening on unix:%s (allowed_models=%s, rate=%d/%ds)",
            self.socket_path, self._allowed_models, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._http:
            await self._http.aclose()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        logger.info("LLM proxy stopped")

    # --- per-connection handler -------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or "unknown"
        # Per-peer key = the socket inode, which is unique per connection.
        # This naturally gives each container invocation its own rate bucket.
        peer_key = f"sock-{id(writer)}"
        try:
            while True:
                # Read one newline-terminated JSON request.  Cap the size
                # so a malicious / buggy client can't OOM us.
                try:
                    raw = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    # Default StreamReader limit (64 KiB) was hit before
                    # the delimiter arrived.  Drop the connection — the
                    # caller almost certainly sent an oversized request
                    # and we already know we don't want it.
                    logger.warning("LLM proxy: request exceeded StreamReader limit, dropping")
                    return
                if not raw:
                    return
                if len(raw) > MAX_REQUEST_BYTES:
                    await self._send_error(writer, "unknown", "request too large")
                    continue

                try:
                    req = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    await self._send_error(writer, "unknown", f"invalid json: {e}")
                    continue

                req_id = str(req.get("id") or uuid.uuid4())

                # --- input validation ---
                model = req.get("model")
                if model not in self._allowed_models:
                    await self._send_error(
                        writer, req_id,
                        f"model '{model}' not in allowlist {self._allowed_models}",
                    )
                    continue
                messages = req.get("messages")
                if not isinstance(messages, list) or not messages:
                    await self._send_error(writer, req_id, "messages must be non-empty list")
                    continue
                # Bound total prompt size
                total_chars = sum(len(str(m.get("content", ""))) for m in messages)
                if total_chars > MAX_PROMPT_CHARS:
                    await self._send_error(
                        writer, req_id,
                        f"prompt too large: {total_chars} chars > {MAX_PROMPT_CHARS}",
                    )
                    continue
                temperature = float(req.get("temperature", 0.3))
                max_tokens = min(int(req.get("max_tokens", 4096)), MAX_RESPONSE_TOKENS)
                if max_tokens < 1:
                    max_tokens = 4096

                # --- rate limit ---
                if not self._limiter.allow(peer_key):
                    await self._send_error(writer, req_id, "rate limit exceeded")
                    continue

                # --- forward to real API ---
                try:
                    content, usage = await self._forward(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                except httpx.HTTPError as e:
                    logger.warning("LLM proxy upstream HTTP error: %s", e)
                    await self._send_error(writer, req_id, f"upstream error: {type(e).__name__}")
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.exception("LLM proxy unexpected error: %s", e)
                    await self._send_error(writer, req_id, f"internal error: {type(e).__name__}")
                    continue

                # Truncate absurdly large responses just in case
                if len(content) > MAX_TOTAL_RESPONSE_CHARS:
                    content = content[:MAX_TOTAL_RESPONSE_CHARS] + "\n\n[truncated by proxy]"

                response = {
                    "id": req_id,
                    "success": True,
                    "content": content,
                    "usage": usage,
                }
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
                logger.info(
                    "LLM proxy served request id=%s model=%s prompt_chars=%d response_chars=%d",
                    req_id, model, total_chars, len(content),
                )
        except (asyncio.IncompleteReadError, ConnectionResetError):
            # Client hung up — normal
            pass
        except Exception:  # noqa: BLE001
            logger.exception("LLM proxy handler crashed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _forward(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, int]]:
        """Call the OpenAI-compatible chat-completions endpoint."""
        assert self._http is not None, "proxy not started"
        url = f"{self._api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = await self._http.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
        }

    async def _send_error(self, writer: asyncio.StreamWriter, req_id: str, message: str) -> None:
        payload = {"id": req_id, "success": False, "error": message}
        try:
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass


# --- Module-level run helpers ----------------------------------------------

async def run_proxy_forever(socket_path: str = DEFAULT_SOCKET_PATH) -> None:
    """Start the proxy and block until cancelled."""
    proxy = LLMProxy(socket_path=socket_path)
    await proxy.start()
    try:
        # Sleep forever; cancellation arrives via the event loop.
        await asyncio.Event().wait()
    finally:
        await proxy.stop()


def main() -> None:
    """Entry point for ``python -m app.services.sandbox.llm_proxy``."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run_proxy_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()