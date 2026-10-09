"""A2A protocol endpoints — expose Zhanlu agents to external agents.

Surface::

  GET  /a2a/agent-card            — machine-readable AgentCard (discovery)
  GET  /.well-known/agent.json    — conventional discovery path (same card)
  POST /a2a                       — JSON-RPC 2.0: tasks/send, tasks/get, tasks/cancel

External agents (Claude, Gemini, other enterprise AI OSes) discover this
card, then delegate tasks via ``tasks/send``. Delegations run through the
Synexia FSM via the pluggable A2A executor.

Auth (IBM agent-protocols workstream #2): ``POST /a2a`` accepts EITHER a
user JWT (``Authorization: Bearer``, the original path) OR a server-to-server
API key (``X-API-Key`` matching ``A2A_API_KEY``) so machine callers don't
need an interactive login. Discovery stays public by design.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.deps import get_current_user_optional, get_db
from app.models.agent_app import AgentApp
from app.services.a2a import tasks as tasks_mod
from app.services.a2a.agent_card import build_platform_card

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a")
# Discovery must live at the conventional well-known root path.
wellknown_router = APIRouter()

_JSONRPC_VERSION = "2.0"


def get_a2a_auth(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    user=Depends(get_current_user_optional),
) -> dict[str, str]:
    """Authenticated caller identity for A2A tasks (overridable in tests).

    Order: (1) ``X-API-Key`` matching ``A2A_API_KEY`` → machine identity
    ``a2a-api-key``; (2) valid user JWT → the user's id; (3) 401.
    """
    from app.config import settings

    expected_key = getattr(settings, "A2A_API_KEY", "") or ""
    if expected_key and x_api_key and x_api_key == expected_key:
        return {"user_id": "a2a-api-key"}
    if user is not None:
        return {"user_id": getattr(user, "id", "anonymous")}
    raise HTTPException(
        status_code=401,
        detail="A2A auth required: provide a user token or X-API-Key",
    )


def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC_VERSION, "id": req_id, "error": {"code": code, "message": message}}


def _result_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC_VERSION, "id": req_id, "result": result}


def _build_platform_agent_card(request: Request, db: Session) -> dict[str, Any]:
    agents = (
        db.query(AgentApp)
        .filter(AgentApp.status == "published", AgentApp.is_deleted == False)  # noqa: E712
        .all()
    )
    base_url = str(request.base_url).rstrip("/")
    return build_platform_card(
        [
            {
                "id": a.id,
                "name": a.name,
                "description": a.description or "",
                "capabilities": a.capabilities or [],
            }
            for a in agents
        ],
        base_url=base_url,
    )


@router.get("/agent-card")
def agent_card(request: Request, db: Session = Depends(get_db)):
    """A2A AgentCard for the whole platform (one skill per published agent)."""
    return _build_platform_agent_card(request, db)


@wellknown_router.get("/.well-known/agent.json")
def wellknown_agent_card(request: Request, db: Session = Depends(get_db)):
    """Conventional discovery path (IBM/A2A step 1) — same card as /a2a/agent-card."""
    return _build_platform_agent_card(request, db)


@router.post("")
async def a2a_jsonrpc(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    auth: dict = Depends(get_a2a_auth),
):
    """JSON-RPC 2.0 dispatcher: tasks/send, tasks/get, tasks/cancel."""
    if payload.get("jsonrpc") != _JSONRPC_VERSION:
        return _error_response(payload.get("id"), -32600, "Invalid Request: jsonrpc must be 2.0")
    method = payload.get("method")
    params = payload.get("params") or {}
    req_id = payload.get("id")

    if method == "tasks/send":
        return await _tasks_send(req_id, params, db, auth)
    if method == "tasks/get":
        return _tasks_get(req_id, params, db)
    if method == "tasks/cancel":
        return _tasks_cancel(req_id, params, db)
    return _error_response(req_id, -32601, f"Method not found: {method}")


async def _tasks_send(
    req_id: Any, params: dict[str, Any], db: Session, auth: dict[str, str],
) -> dict[str, Any]:
    agent_ref = params.get("agent_id")
    message = params.get("message") or {}
    parts = message.get("parts") if isinstance(message, dict) else None
    if not agent_ref or not isinstance(parts, list):
        return _error_response(req_id, -32602, "Invalid params: agent_id and message.parts required")

    from app.models.agent_app import AgentApp
    from app.services.agent_lifecycle import rollout_allows

    # Interop fix (2026-10-09): external clients pass the agent id exactly as
    # the agent card advertises it — skill ids are "agent-<uuid>" display
    # form, and some callers send the agent NAME. Resolve all three forms;
    # anything unknown returns a clean JSON-RPC error, never a 500.
    raw = str(agent_ref).strip()
    candidate = raw[len("agent-"):] if raw.startswith("agent-") else raw
    agent_row = None
    if candidate:
        agent_row = db.query(AgentApp).filter(
            AgentApp.id == candidate,
            AgentApp.is_deleted == False,  # noqa: E712
        ).first()
    if agent_row is None:
        agent_row = db.query(AgentApp).filter(
            AgentApp.name == raw,
            AgentApp.status == "published",
            AgentApp.is_deleted == False,  # noqa: E712
        ).first()
    if agent_row is None:
        return _error_response(req_id, -32602, f"Unknown agent: {raw}")

    if not rollout_allows(agent_row, auth.get("user_id")):
        return _error_response(
            req_id, -32001,
            f"Agent '{agent_row.name}' is in a canary rollout "
            f"({agent_row.rollout_percent}%); this caller is not in the bucket. Retry later.",
        )

    store = tasks_mod.A2aTaskStore(db)
    try:
        task = store.create(
            task_id=params.get("id"),
            agent_app_id=agent_row.id,
            message_parts=parts,
            metadata=params.get("metadata"),
            external=True,
            created_by_id=auth.get("user_id"),
        )
        # Await dispatch inline so callers can immediately tasks/get a settled
        # task; long-running agents can switch to spawn_dispatch for backgrounding.
        await tasks_mod.dispatch_task(db, task.id)
        db.refresh(task)
    except Exception as exc:  # noqa: BLE001 — JSON-RPC surfaces errors, never 500s
        logger.exception("a2a: tasks/send dispatch failed")
        return _error_response(req_id, -32000, f"Task dispatch failed: {exc}")
    return _result_response(req_id, store.to_dict(task))


def _tasks_get(req_id: Any, params: dict[str, Any], db: Session) -> dict[str, Any]:
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, -32602, "Invalid params: id required")
    store = tasks_mod.A2aTaskStore(db)
    task = store.get(task_id)
    if task is None:
        return _error_response(req_id, -32004, f"Task not found: {task_id}")
    return _result_response(req_id, store.to_dict(task))


def _tasks_cancel(req_id: Any, params: dict[str, Any], db: Session) -> dict[str, Any]:
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, -32602, "Invalid params: id required")
    store = tasks_mod.A2aTaskStore(db)
    task = store.get(task_id)
    if task is None:
        return _error_response(req_id, -32004, f"Task not found: {task_id}")
    store.cancel(task)
    return _result_response(req_id, store.to_dict(task))


__all__ = ["router", "wellknown_router", "get_a2a_auth"]
