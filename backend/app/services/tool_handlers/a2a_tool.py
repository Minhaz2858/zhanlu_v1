"""A2A outbound tools — invoke_external_agent / list_external_agents.

IBM agent-protocols workstream #1 (2026-10-08): the A2A JSON-RPC client
(``app/services/a2a/client.py``) existed but was unreachable from the
agent loop. These tools let a Zhanlu agent delegate a task to a registered
external A2A agent (discovered via the ExternalAgent registry) or to an
ad-hoc URL, receiving the remote task object (state + artifacts) back.

Wire protocol: A2A v1.0 — JSON-RPC 2.0 over HTTPS (see the IBM
"AI agent protocols" taxonomy: A2A is the horizontal agent↔agent layer;
MCP — already implemented — is the vertical agent↔tools layer).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.external_agent import ExternalAgent
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _extract_task_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Condense an A2A task object into the fields an LLM can use."""
    if not isinstance(result, dict):
        return {"state": "unknown", "text": ""}
    artifacts = result.get("artifacts") or []
    text_parts: list[str] = []
    for art in artifacts:
        for part in (art.get("parts") or []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    # Also surface the latest agent message text (common for quick tasks).
    message = result.get("message") or {}
    for part in (message.get("parts") or []):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    return {
        "task_id": result.get("id"),
        "state": result.get("status", {}).get("state")
        if isinstance(result.get("status"), dict)
        else result.get("state", "unknown"),
        "text": "\n".join(text_parts)[:8000],
        "artifacts_count": len(artifacts),
        "metadata": result.get("metadata") or {},
    }


def _resolve_external_agent(
    db: Session, agent_ref: str | None
) -> ExternalAgent | None:
    """Resolve by id first, then by (case-insensitive) name."""
    if not agent_ref or db is None:
        return None
    row = db.query(ExternalAgent).filter(ExternalAgent.id == agent_ref).first()
    if row is None:
        row = (
            db.query(ExternalAgent)
            .filter(
                ExternalAgent.name.ilike(agent_ref),
                ExternalAgent.enabled == True,  # noqa: E712
            )
            .first()
        )
    return row


async def _invoke_external_agent(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Delegate a task to a registered external A2A agent."""
    from app.services.a2a import client as a2a_client

    task = (args.get("task") or args.get("message") or "").strip()
    if not task:
        return {"success": False, "error": "task is required"}

    agent_ref = args.get("agent") or args.get("agent_id") or args.get("name")
    endpoint_url = (args.get("endpoint_url") or "").strip()
    token = args.get("token")
    timeout_s = args.get("timeout_s")

    row: ExternalAgent | None = None
    if agent_ref:
        row = _resolve_external_agent(db, str(agent_ref))
        if row is not None:
            endpoint_url = row.endpoint_url
            token = token or row.auth_token
            timeout_s = timeout_s or row.timeout_s
            if not row.enabled:
                return {
                    "success": False,
                    "error": f"External agent '{row.name}' is disabled",
                }
    if not endpoint_url:
        return {
            "success": False,
            "error": (
                "No external agent found for "
                f"{agent_ref!r} and no endpoint_url given. "
                "Use list_external_agents to see registered remote agents."
            ),
        }

    try:
        stream_events: list[dict] | None = None
        if args.get("stream"):
            # SSE streaming: capture intermediate snapshots for progress
            # visibility, return the final task (IBM protocols #4).
            stream_events = []

            def _on_event(snapshot: dict) -> None:
                stream_events.append(_extract_task_payload(snapshot))

            result = await a2a_client.send_task_streaming(
                endpoint_url,
                message_parts=[{"type": "text", "text": task}],
                agent_id=str(agent_ref or endpoint_url),
                token=token,
                timeout_s=float(timeout_s or 60),
                on_event=_on_event,
            )
        else:
            result = await a2a_client.send_task(
                endpoint_url,
                message_parts=[{"type": "text", "text": task}],
                agent_id=str(agent_ref or endpoint_url),
                token=token,
                timeout_s=float(timeout_s or 60),
            )
    except Exception as e:  # noqa: BLE001 — remote failures are tool results
        logger.warning(
            "invoke_external_agent: %s failed: %s", agent_ref or endpoint_url, e
        )
        return {
            "success": False,
            "error": f"A2A delegation failed: {e}",
            "endpoint_url": endpoint_url,
        }

    payload = _extract_task_payload(result)
    if stream_events is not None:
        payload["progress_events"] = len(stream_events)
    if row is not None:
        try:
            row.last_task_id = payload.get("task_id")
            row.status = "ok" if payload.get("state") == "completed" else "in_progress"
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return {"success": True, "endpoint_url": endpoint_url, **payload}


async def _list_external_agents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """List the registered external A2A agents available for delegation."""
    rows = (
        db.query(ExternalAgent)
        .filter(ExternalAgent.enabled == True)  # noqa: E712
        .order_by(ExternalAgent.name)
        .all()
    )
    return {
        "success": True,
        "agents": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description or "",
                "endpoint_url": r.endpoint_url,
                "capabilities": r.capabilities or [],
                "status": r.status or "unknown",
            }
            for r in rows
        ],
    }


# ── Schemas ──────────────────────────────────────────────────────────────

INVOKE_EXTERNAL_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "invoke_external_agent",
        "description": (
            "Delegate a task to an EXTERNAL agent that speaks the A2A "
            "protocol (JSON-RPC over HTTPS). Pass a registered agent name "
            "(see list_external_agents) or an ad-hoc endpoint_url, plus a "
            "clear self-contained task description. Returns the remote "
            "task's state, response text, and artifact count. Use when the "
            "capability lives outside this platform."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Registered external agent id or name.",
                },
                "endpoint_url": {
                    "type": "string",
                    "description": "Ad-hoc A2A JSON-RPC endpoint URL (used when no registered agent matches).",
                },
                "task": {
                    "type": "string",
                    "description": "Self-contained task description for the remote agent.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Optional per-call timeout in seconds (default 60).",
                },
                "stream": {
                    "type": "boolean",
                    "description": "Request an SSE stream from the remote agent (progress snapshots; result is the final task). Default false.",
                },
            },
            "required": ["task"],
        },
    },
}

LIST_EXTERNAL_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_external_agents",
        "description": (
            "List the registered external A2A agents available for "
            "delegation via invoke_external_agent, with their capabilities."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

registry.register(
    name="invoke_external_agent",
    schema=INVOKE_EXTERNAL_AGENT_SCHEMA,
    handler=_invoke_external_agent,
    category="a2a",
    enabled_by_default=True,
    description="Delegate a task to a registered external A2A agent.",
)
registry.register(
    name="list_external_agents",
    schema=LIST_EXTERNAL_AGENTS_SCHEMA,
    handler=_list_external_agents,
    category="a2a",
    enabled_by_default=True,
    description="List registered external A2A agents.",
)

__all__ = ["_invoke_external_agent", "_list_external_agents", "_extract_task_payload"]
