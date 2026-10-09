"""A2A remote client — delegate tasks to external agents over JSON-RPC.

Speaks the A2A v1.0 wire protocol (JSON-RPC 2.0 over HTTPS) against an
external agent's ``tasks/send`` / ``tasks/get`` / ``tasks/cancel`` methods.
Streaming (SSE) is not yet implemented; polling via ``get_task`` is the
fallback path.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0


def _headers(token: str | None) -> dict[str, str]:
    """Identity-carrying headers (IBM best practice: unique agent
    identifiers so remote parties can trace the caller)."""
    from app.config import settings

    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ZhanluAgent/1.0 ({getattr(settings, 'A2A_SELF_AGENT_ID', 'zhanlu-platform')})",
        "X-Agent-Id": getattr(settings, "A2A_SELF_AGENT_ID", "zhanlu-platform"),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(
    endpoint: str,
    payload: dict[str, Any],
    token: str | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    url = endpoint.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=payload, headers=_headers(token))
        resp.raise_for_status()
        body = resp.json()
    if body.get("jsonrpc") != "2.0":
        raise ValueError(f"A2A endpoint {url} is not a JSON-RPC 2.0 server")
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"A2A error {err.get('code')}: {err.get('message')}")
    return body.get("result", {})


async def send_task(
    endpoint: str,
    message_parts: list[dict[str, Any]],
    agent_id: str,
    token: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Send a task to an external A2A agent and return the task object."""
    import uuid as _uuid

    payload = {
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "tasks/send",
        "params": {
            "id": task_id or f"zhanlu-{_uuid.uuid4().hex[:8]}",
            "agent_id": agent_id,
            "message": {"role": "user", "parts": message_parts},
        },
    }
    if metadata:
        payload["params"]["metadata"] = metadata
    return await _request(endpoint, payload, token, timeout_s)


async def get_task(
    endpoint: str,
    task_id: str,
    token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll an external task by id."""
    payload = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "tasks/get",
        "params": {"id": task_id},
    }
    return await _request(endpoint, payload, token, timeout_s)


async def cancel_task(
    endpoint: str,
    task_id: str,
    token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Cancel an external task by id."""
    payload = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "tasks/cancel",
        "params": {"id": task_id},
    }
    return await _request(endpoint, payload, token, timeout_s)


# ── IBM agent-protocols #4: SSE streaming (2026-10-08) ──────────────────
# A2A servers may answer tasks/send with text/event-stream, pushing task
# snapshots as the work progresses. We parse the SSE frames incrementally,
# surface every intermediate snapshot via ``on_event``, and return the
# final task. Servers that answer with plain JSON keep working unchanged.

_STREAM_TERMINAL_STATES = {"completed", "failed", "canceled", "input-required"}


def _parse_sse_data_frames(lines) -> list[str]:
    """Join multi-line ``data:`` fields into complete frame payloads."""
    frames: list[str] = []
    buf: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            buf.append(line[5:].lstrip(" "))
        elif not line.strip():
            # blank line = end of event frame
            if buf:
                frames.append("\n".join(buf))
                buf = []
        # ignore event:/id:/retry:/comments
    if buf:
        frames.append("\n".join(buf))
    return frames


def _new_client(timeout_s: float) -> httpx.AsyncClient:
    """Client factory — tests monkeypatch this to inject mock transports."""
    return httpx.AsyncClient(timeout=timeout_s)


async def send_task_streaming(
    endpoint: str,
    message_parts: list[dict[str, Any]],
    agent_id: str,
    token: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    on_event=None,
) -> dict[str, Any]:
    """Send a task requesting an SSE stream of task snapshots.

    Returns the FINAL task object (last snapshot seen). Every intermediate
    snapshot is passed to ``on_event(task_dict)`` when provided. Falls back
    to the plain-JSON ``tasks/send`` path when the server does not stream.
    """
    import uuid as _uuid

    payload = {
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "tasks/send",
        "params": {
            "id": task_id or f"zhanlu-{_uuid.uuid4().hex[:8]}",
            "agent_id": agent_id,
            "message": {"role": "user", "parts": message_parts},
        },
    }
    if metadata:
        payload["params"]["metadata"] = metadata

    url = endpoint.rstrip("/")
    headers = _headers(token)
    headers["Accept"] = "text/event-stream, application/json"

    final_task: dict[str, Any] | None = None
    async with _new_client(timeout_s) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                # Non-streaming server — consume the JSON body directly.
                body = json.loads((await resp.aread()).decode())
                if body.get("jsonrpc") != "2.0":
                    raise ValueError(
                        f"A2A endpoint {url} is not a JSON-RPC 2.0 server")
                if "error" in body:
                    err = body["error"]
                    raise RuntimeError(
                        f"A2A error {err.get('code')}: {err.get('message')}")
                return body.get("result", {})

            async for frame in resp.aiter_lines():
                for data in _parse_sse_data_frames([frame]):
                    if data.strip() in ("[DONE]", ""):
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("a2a stream: skipping non-JSON frame")
                        continue
                    if isinstance(event, dict) and event.get("jsonrpc") == "2.0":
                        if "error" in event:
                            err = event["error"]
                            raise RuntimeError(
                                f"A2A error {err.get('code')}: {err.get('message')}")
                        event = event.get("result", {})
                    if not isinstance(event, dict):
                        continue
                    final_task = event
                    if on_event is not None:
                        try:
                            on_event(event)
                        except Exception as e:  # noqa: BLE001
                            logger.debug("a2a stream: on_event failed: %s", e)
                    state = event.get("status")
                    if isinstance(state, dict):
                        state = state.get("state")
                    if state in _STREAM_TERMINAL_STATES:
                        return final_task
    if final_task is None:
        raise RuntimeError(f"A2A stream from {url} ended without a task snapshot")
    return final_task


__all__ = ["send_task", "send_task_streaming", "get_task", "cancel_task"]
