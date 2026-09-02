"""A2A remote client — delegate tasks to external agents over JSON-RPC.

Speaks the A2A v1.0 wire protocol (JSON-RPC 2.0 over HTTPS) against an
external agent's ``tasks/send`` / ``tasks/get`` / ``tasks/cancel`` methods.
Streaming (SSE) is not yet implemented; polling via ``get_task`` is the
fallback path.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
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


__all__ = ["send_task", "get_task", "cancel_task"]
