"""ACP (Agent Communication Protocol) adapter — IBM/BeeAI REST layer.

IBM agent-protocols workstream #3 (2026-10-08). ACP is the low-friction
horizontal agent↔agent alternative to A2A: plain REST over HTTP,
async-first runs, multimodal message parts. This adapter exposes the
published Zhanlu agents in ACP shape by reusing the A2A task machinery
(store, executor, event bus) — one governed runtime, two protocol faces.

Surface (no prefix — ACP paths are root-relative):
  GET  /acp/agents                     — discovery (agent manifests)
  POST /acp/agents/{agent_id}/runs     — start a run (async-first: returns
                                          immediately with run id + state;
                                          ``?wait=true`` settles inline)
  GET  /acp/runs/{run_id}              — run state
  GET  /acp/runs/{run_id}/messages     — result messages (multimodal parts)
  POST /acp/runs/{run_id}/cancel       — cancel

Auth: same trust boundary as ``POST /a2a`` — user JWT or ``X-API-Key``
matching ``A2A_API_KEY``. Discovery (GET /acp/agents) is public.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.deps import get_current_user_optional, get_db
from app.models.agent_app import AgentApp
from app.services.a2a import tasks as tasks_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/acp", tags=["acp"])


def acp_auth(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    user=Depends(get_current_user_optional),
) -> dict[str, str]:
    """Authenticated caller identity for ACP runs (JWT or platform API key)."""
    from app.config import settings

    expected = getattr(settings, "A2A_API_KEY", "") or ""
    if expected and x_api_key and x_api_key == expected:
        return {"user_id": "acp-api-key"}
    if user is not None:
        return {"user_id": getattr(user, "id", "anonymous")}
    raise HTTPException(status_code=401, detail="ACP auth required: provide a user token or X-API-Key")


def _published_agents(db: Session) -> list[AgentApp]:
    return (
        db.query(AgentApp)
        .filter(AgentApp.status == "published", AgentApp.is_deleted == False)  # noqa: E712
        .order_by(AgentApp.name)
        .all()
    )


@router.get("/agents")
def acp_agents(request: Request, db: Session = Depends(get_db)):
    """Discovery: ACP agent manifests for every published Zhanlu agent."""
    base_url = str(request.base_url).rstrip("/")
    return [
        {
            "name": a.name,
            "description": a.description or "",
            "capabilities": a.capabilities or [],
            "metadata": {
                "agent_id": a.id,
                "protocol": "acp",
                "runs_url": f"{base_url}/acp/agents/{a.id}/runs",
            },
        }
        for a in _published_agents(db)
    ]


async def _create_run(
    agent_id: str,
    body: dict[str, Any],
    db: Session,
    auth: dict[str, str],
    wait: bool,
) -> dict[str, Any]:
    agent = (
        db.query(AgentApp)
        .filter(AgentApp.id == agent_id, AgentApp.is_deleted == False)  # noqa: E712
        .first()
    )
    if agent is None or agent.status != "published":
        raise HTTPException(404, f"No published agent {agent_id!r}")

    message = body.get("message") or {}
    parts = message.get("parts") if isinstance(message, dict) else body.get("parts")
    if not isinstance(parts, list) or not parts:
        raise HTTPException(
            422, "message.parts required (multimodal: [{type: text, text: ...}, ...])"
        )

    store = tasks_mod.A2aTaskStore(db)
    task = store.create(
        agent_app_id=agent_id,
        message_parts=parts,
        metadata={"protocol": "acp", **(body.get("metadata") or {})},
        external=True,
        created_by_id=auth.get("user_id"),
    )
    if wait:
        # Inline settle — convenient for tests and short agents.
        await tasks_mod.dispatch_task(db, task.id)
        db.refresh(task)
    else:
        # Async-first: return immediately; poll GET /acp/runs/{id}.
        tasks_mod.spawn_dispatch(db, task.id)
    return _run_dict(store, store.get(task.id) or task)


def _run_dict(store: tasks_mod.A2aTaskStore, task) -> dict[str, Any]:
    d = store.to_dict(task)
    d["protocol"] = "acp"
    d["run_url"] = f"/acp/runs/{task.id}"
    return d


@router.post("/agents/{agent_id}/runs")
async def acp_create_run(
    agent_id: str,
    body: dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(acp_auth),
):
    wait = request.query_params.get("wait", "false").lower() in ("1", "true", "yes")
    return await _create_run(agent_id, body, db, auth, wait)


def _get_task_or_404(db: Session, run_id: str):
    task = tasks_mod.A2aTaskStore(db).get(run_id)
    if task is None:
        raise HTTPException(404, f"Run not found: {run_id}")
    return task


@router.get("/runs/{run_id}")
def acp_get_run(
    run_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(acp_auth),
):
    task = _get_task_or_404(db, run_id)
    return _run_dict(tasks_mod.A2aTaskStore(db), task)


@router.get("/runs/{run_id}/messages")
def acp_run_messages(
    run_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(acp_auth),
):
    """Result messages + artifacts as ACP message parts (multimodal passthrough)."""
    task = _get_task_or_404(db, run_id)
    d = tasks_mod.A2aTaskStore(db).to_dict(task)
    return {
        "run_id": run_id,
        "status": d["status"],
        "messages": d.get("messages") or [],
        "parts": [
            part
            for artifact in (d.get("artifacts") or [])
            for part in (artifact.get("parts") or [])
        ],
        "error": d.get("error"),
    }


@router.post("/runs/{run_id}/cancel")
def acp_cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(acp_auth),
):
    task = _get_task_or_404(db, run_id)
    tasks_mod.A2aTaskStore(db).cancel(task)
    return {"run_id": run_id, "status": task.status}


__all__ = ["router", "acp_auth"]
