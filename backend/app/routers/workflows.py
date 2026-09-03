"""Workflows router — flow-mode agent CRUD + validate / run / publish / runs.

Workflow agents are ordinary AgentApp rows with ``flow_mode=true`` and the
graph JSON in ``flow``. This router adds the canvas-specific surface on top
of the generic entity CRUD (which still owns AgentApp row management).

All endpoints are user-scoped (AgentApp is a USER_SCOPED_ENTITY): non-admins
only see/edit their own workflow agents.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.config import settings
from app.models.agent_app import AgentApp
from app.models.user import User
from app.models.workflow_run import WorkflowRun, WorkflowRunStep
from app.services.workflow_runtime import run_workflow_agent
from app.services.workflow_validation import validate_flow
from app.services.workflow_node_types import (
    CONNECTION_TYPE_META,
    list_node_types,
)

router = APIRouter(prefix="/apps/{app_id}/workflows", tags=["workflows"])


@router.get("/node-types")
def workflow_node_types(app_id: str):
    """Schema registry for the canvas palette + auto-generated settings form.

    Mirrors n8n's INodeTypeDescription: each entry carries inputs/outputs
    (typed connection ports) and properties (parameter schema). One registry
    drives palette, form, validation and runtime — no client-side switch
    statements.
    """
    return {
        "connectionTypes": CONNECTION_TYPE_META,
        "nodeTypes": list_node_types(),
    }


def _get_owned_workflow(db: Session, app_id: str, workflow_id: str, user: User) -> AgentApp:
    # app_id is URL cosmetics here (matches the generic entity router: rows
    # are user-scoped, NOT app-scoped). The browser's appId param differs per
    # deployment (e.g. local-zhanlu-app) from stored rows (default-app), so
    # filtering on it would hide every workflow.
    q = db.query(AgentApp).filter(
        AgentApp.id == workflow_id,
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    )
    if user.role != "admin":
        q = q.filter(AgentApp.created_by_id == user.id)
    agent = q.first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return agent


def _trigger_source(agent: AgentApp) -> str:
    """Map the workflow's trigger node type to a WorkflowRun.trigger value."""
    flow = agent.flow if isinstance(agent.flow, dict) else {}
    for node in flow.get("nodes") or []:
        ntype = node.get("type", "")
        if ntype.startswith("trigger_"):
            return ntype.removeprefix("trigger_")
    return "chat"


def _find_webhook_node(agent: AgentApp) -> dict | None:
    flow = agent.flow if isinstance(agent.flow, dict) else {}
    for node in flow.get("nodes") or []:
        if node.get("type") == "trigger_webhook":
            return node
    return None


class RunBody(BaseModel):
    input: str = ""


@router.get("")
def list_workflows(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    q = db.query(AgentApp).filter(
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    )
    if user.role != "admin":
        q = q.filter(AgentApp.created_by_id == user.id)
    return [a.to_dict() for a in q.order_by(AgentApp.updated_date.desc()).all()]


@router.post("")
def create_workflow(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = AgentApp(
        name="Untitled Workflow",
        description="",
        flow_mode=True,
        flow={"version": 1, "nodes": [], "edges": []},
        status="draft",
        # NOTE: store the tenant-scoped DEFAULT_APP_ID, NOT the URL-path
        # ``app_id``. The path segment is URL cosmetics (the frontend sends
        # e.g. ``local-zhanlu-app`` while rows are stored under ``default-app``);
        # the entity router's tenant wall filters reads by
        # ``app_id == settings.DEFAULT_APP_ID``, so a workflow created with
        # the cosmetic id would be INVISIBLE to ``AgentApp.list()`` — breaking
        # chat's agent auto-restore (workflow chip never reappears on reopen).
        app_id=settings.DEFAULT_APP_ID,
        org_id="default-org",
        created_by_id=user.id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent.to_dict()


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    return _get_owned_workflow(db, app_id, workflow_id, user).to_dict()


class SaveBody(BaseModel):
    name: str | None = None
    description: str | None = None
    flow: dict


@router.put("/{workflow_id}")
def save_workflow(
    workflow_id: str,
    body: SaveBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(body.flow)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    # The AI builder and hand-edited canvases occasionally omit edge ids —
    # xyflow requires a unique id per edge (null ids render as unconnected
    # nodes), so assign stable e1..eN ids before persisting.
    for i, edge in enumerate(body.flow.get("edges") or [], start=1):
        if not edge.get("id"):
            edge["id"] = f"e{i}"
    agent.flow = body.flow
    if body.name:
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description
    db.commit()
    db.refresh(agent)
    return agent.to_dict()


@router.post("/{workflow_id}/validate")
def validate_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(agent.flow or {})
    return {"valid": not errors, "errors": errors}


@router.post("/{workflow_id}/run")
def run_workflow(
    workflow_id: str,
    body: RunBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)
        # Fan out to live SSE subscribers (n8n push pattern).
        from app.services.workflow_events import publish
        publish(ev.get("run_id", ""), ev)

    result = run_workflow_agent(
        db, agent.org_id, agent.app_id, agent, body.input,
        created_by_id=user.id, on_event=on_event,
    )
    return {**result, "events": events}


@router.post("/{workflow_id}/run-async")
def run_workflow_async(
    workflow_id: str,
    body: RunBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Kick off a run in a background thread; returns the run_id immediately.

    The client then subscribes to GET /runs/{run_id}/stream for live
    per-node events (replayed from the DB first, then pushed live) — the
    n8n execution-stream pattern.
    """
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from app.database import SessionLocal
    from app.services.workflow_events import publish

    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    run_id = str(uuid.uuid4())
    input_text = body.input
    org_id, agent_app_id = agent.org_id, agent.app_id
    agent_id = agent.id
    trigger = _trigger_source(agent)

    def _run_in_thread():
        # Own session for the background thread (request session closes).
        thread_db = SessionLocal()
        try:
            thread_agent = thread_db.query(AgentApp).filter(
                AgentApp.id == agent_id, AgentApp.is_deleted.is_(False),
            ).first()
            if thread_agent is None:
                publish(run_id, {"type": "run_finished", "run_id": run_id,
                                 "status": "failed", "error": "workflow not found", "output": None})
                return
            run_workflow_agent(
                thread_db, org_id, agent_app_id, thread_agent, input_text,
                created_by_id=user.id,
                on_event=lambda ev: publish(ev.get("run_id", run_id), ev),
                run_id=run_id,
                trigger=trigger,
            )
        finally:
            thread_db.close()

    ThreadPoolExecutor(max_workers=4).submit(_run_in_thread)
    return {"run_id": run_id}


class RerunBody(BaseModel):
    run_id: str
    start_node_id: str
    input: str = ""


@router.post("/{workflow_id}/rerun")
def rerun_workflow(
    workflow_id: str,
    body: RerunBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Kick off a tagged partial rerun in a background thread (iterate loop).

    n8n "execute from this node": ``run_id`` names a prior completed run of
    this workflow; the completed step outputs of that reference run seed every
    node upstream of ``start_node_id``, and only ``start_node_id`` + its
    downstream subgraph executes again. The new WorkflowRun row is tagged with
    ``rerun_of`` (reference run id) and ``rerun_from_node`` so the run drawer
    can show the relationship. Returns the new run_id immediately — the client
    then subscribes to GET /runs/{run_id}/stream for live per-node events
    (replayed from the DB first, then pushed live).
    """
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from app.database import SessionLocal
    from app.services.workflow_events import publish

    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    ref = (
        db.query(WorkflowRun)
        .filter(
            WorkflowRun.id == body.run_id,
            WorkflowRun.agent_app_id == agent.id,
            WorkflowRun.is_deleted.is_(False),
        )
        .first()
    )
    if ref is None:
        raise HTTPException(status_code=404, detail="Reference run not found")
    flow = agent.flow if isinstance(agent.flow, dict) else {}
    if not any(n.get("id") == body.start_node_id for n in (flow.get("nodes") or [])):
        raise HTTPException(status_code=404, detail="Start node not found in workflow")
    # Seed map: completed step outputs of the reference run — the executor
    # pre-loads these for nodes OUTSIDE the rerun subgraph (they are not
    # re-executed; seeds for nodes inside the subgraph are ignored).
    steps = (
        db.query(WorkflowRunStep)
        .filter(
            WorkflowRunStep.run_id == ref.id,
            WorkflowRunStep.is_deleted.is_(False),
            WorkflowRunStep.status == "completed",
        )
        .all()
    )
    seed_outputs = {s.node_id: (s.output_json or {}) for s in steps}

    run_id = str(uuid.uuid4())
    input_text = body.input
    org_id, agent_app_id = agent.org_id, agent.app_id
    agent_id = agent.id
    trigger = _trigger_source(agent)
    start_node_id = body.start_node_id
    ref_id = ref.id
    created_by_id = user.id

    def _run_in_thread():
        # Own session for the background thread (request session closes).
        thread_db = SessionLocal()
        try:
            thread_agent = thread_db.query(AgentApp).filter(
                AgentApp.id == agent_id, AgentApp.is_deleted.is_(False),
            ).first()
            if thread_agent is None:
                publish(run_id, {"type": "run_finished", "run_id": run_id,
                                 "status": "failed", "error": "workflow not found", "output": None})
                return
            run_workflow_agent(
                thread_db, org_id, agent_app_id, thread_agent, input_text,
                created_by_id=created_by_id,
                on_event=lambda ev: publish(ev.get("run_id", run_id), ev),
                run_id=run_id,
                trigger=trigger,
                partial_from=start_node_id,
                seed_outputs=seed_outputs,
                rerun_of=ref_id,
            )
        finally:
            thread_db.close()

    ThreadPoolExecutor(max_workers=4).submit(_run_in_thread)
    return {"run_id": run_id}


@router.api_route("/{workflow_id}/webhook/{token}", methods=["POST", "GET"])
async def webhook_trigger(
    workflow_id: str,
    token: str,
    app_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public webhook endpoint (n8n Webhook Trigger).

    No auth — the token in the URL is the secret. The workflow's final
    output is returned as the HTTP response so external systems get a
    synchronous answer.
    """
    import json

    agent = db.query(AgentApp).filter(
        AgentApp.id == workflow_id,
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    ).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    webhook_node = _find_webhook_node(agent)
    if webhook_node is None:
        raise HTTPException(status_code=404, detail="Workflow has no webhook trigger")
    node_cfg = (webhook_node.get("data") or {}).get("config") or {}
    if not node_cfg.get("token") or node_cfg["token"] != token:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    method = (node_cfg.get("method") or "POST").upper()
    if request.method != method:
        raise HTTPException(status_code=405, detail=f"Webhook expects {method}")

    input_field = node_cfg.get("input_field") or "input"
    raw = (await request.body()).decode("utf-8", errors="replace") if request.method == "POST" else ""
    payload_text = raw
    if request.method == "GET":
        payload_text = request.query_params.get(input_field) or request.query_params.get("input") or ""
    elif raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                payload_text = (
                    data.get(input_field)
                    or data.get("text")
                    or data.get("message")
                    or json.dumps(data, ensure_ascii=False)
                )
            else:
                payload_text = json.dumps(data, ensure_ascii=False)
        except json.JSONDecodeError:
            payload_text = raw  # plain text body

    result = run_workflow_agent(
        db, agent.org_id, agent.app_id, agent, str(payload_text),
        created_by_id=agent.created_by_id,
        trigger="webhook",
    )
    if result["status"] == "failed":
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "status": "completed",
        "output": result["output"],
        "run_id": result["run_id"],
    }


@router.get("/{workflow_id}/runs/{run_id}/stream")
async def stream_run_events(
    workflow_id: str,
    run_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """SSE stream of run events (n8n push-connection pattern).

    Replays persisted steps first (so late subscribers see everything),
    then live events as the background run emits them. Closes after
    run_finished.
    """
    import asyncio
    import json

    from fastapi.responses import StreamingResponse
    from app.services.workflow_events import subscribe, unsubscribe

    agent = _get_owned_workflow(db, app_id, workflow_id, user)

    # Replay persisted steps (may already exist if the run finished fast).
    steps = (
        db.query(WorkflowRunStep)
        .filter(WorkflowRunStep.run_id == run_id, WorkflowRunStep.is_deleted.is_(False))
        .order_by(WorkflowRunStep.created_date.asc())
        .all()
    )
    replayed = []
    for s in steps:
        d = s.to_dict()
        replayed.append({
            "type": "step", "run_id": run_id, "node_id": s.node_id,
            "node_type": s.node_type, "status": s.status,
            "output": d.get("output_json"), "error": s.error,
        })
    run_row = db.query(WorkflowRun).filter(WorkflowRun.id == run_id).first()
    if run_row and run_row.status in ("completed", "failed"):
        # WorkflowRun has no output column — the final output lives in the
        # last completed step (the output node).
        final_output = None
        for s in reversed(steps):
            if s.status == "completed" and s.output_json is not None:
                final_output = s.output_json.get("text") or s.output_json
                break
        replayed.append({
            "type": "run_finished", "run_id": run_id,
            "status": run_row.status, "error": run_row.error,
            "output": final_output,
        })

    async def gen():
        q = subscribe(run_id)
        try:
            for ev in replayed:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            # If the run already finished, we're done after the replay.
            if replayed and replayed[-1].get("type") == "run_finished":
                return
            heartbeat = 0
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") == "run_finished":
                        return
                except asyncio.TimeoutError:
                    heartbeat += 1
                    yield f": heartbeat {heartbeat}\n\n"
        finally:
            unsubscribe(run_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{workflow_id}/runs")
def list_runs(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.agent_app_id == agent.id, WorkflowRun.is_deleted.is_(False))
        .order_by(WorkflowRun.created_date.desc())
        .limit(50)
        .all()
    )
    out = []
    for run in runs:
        steps = (
            db.query(WorkflowRunStep)
            .filter(WorkflowRunStep.run_id == run.id, WorkflowRunStep.is_deleted.is_(False))
            .order_by(WorkflowRunStep.created_date.asc())
            .all()
        )
        out.append({
            **run.to_dict(),
            "steps": [s.to_dict() for s in steps],
        })
    return out


@router.post("/{workflow_id}/publish")
def publish_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(agent.flow or {})
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    agent.status = "published"
    db.commit()
    db.refresh(agent)
    return agent.to_dict()


@router.delete("/{workflow_id}")
def delete_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Delete a workflow and cascade-delete its run history.

    Soft-deletes the AgentApp row (platform convention) AND every
    WorkflowRun / WorkflowRunStep — no orphans left behind.
    """
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    agent.is_deleted = True
    runs = db.query(WorkflowRun).filter(
        WorkflowRun.agent_app_id == agent.id,
        WorkflowRun.is_deleted.is_(False),
    ).all()
    run_ids = [r.id for r in runs]
    for r in runs:
        r.is_deleted = True
    if run_ids:
        db.query(WorkflowRunStep).filter(
            WorkflowRunStep.run_id.in_(run_ids),
            WorkflowRunStep.is_deleted.is_(False),
        ).update({WorkflowRunStep.is_deleted: True}, synchronize_session=False)
    db.commit()
    return {"deleted": True, "workflow_id": agent.id, "runs_cascaded": len(run_ids)}
