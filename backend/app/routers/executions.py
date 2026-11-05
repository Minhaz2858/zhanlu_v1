"""Executions router — execution state, plan, and confirmation APIs.

Provides:
* GET /api/executions/{id} — get execution state + plan + observations
* POST /api/executions — create and run a new execution (FSM pipeline)
* GET /api/executions/{id}/plan — get the execution plan (PlanDAG)
* PATCH /api/executions/{id}/plan/approve — approve the plan (pass GATE)
* PATCH /api/executions/{id}/plan/reject — reject the plan
* GET /api/executions/{id}/observations — get observation records

Security: every endpoint requires an authenticated user.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.services.agent_invocations import build_agent_tree
from app.services.agent_collaboration.shared_evidence import (
    get_registry, get_evidence,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["executions"])


class CreateExecutionRequest(BaseModel):
    conversation_id: Optional[str] = None
    agent_name: str = "general_assistant"
    user_message: str
    user_id: Optional[str] = None
    mode: str = "dynamic"


def _user_org(user: User) -> str:
    """Authenticated org identity (IC-5, FSM tenant isolation).

    The org ALWAYS comes from the authenticated user — never from the
    request body. A user without an org fails closed.
    """
    org_id = getattr(user, "org_id", None)
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="ERR_ORG_MISSING: authenticated user has no organization context",
        )
    return org_id


def _org_scoped_execution(execution_id: str, db: Session, user: User):
    """Fetch an execution confined to the caller's org (IC-5).

    Foreign-org executions are indistinguishable from nonexistent ones
    (404) — identity is enforced at the resolution boundary, before any
    plan approval, rejection, observation read, clone or replay.
    """
    from app.models.execution import Execution

    execution = (
        db.query(Execution)
        .filter(
            Execution.id == execution_id,
            Execution.org_id == _user_org(user),
        )
        .first()
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


def _owns_conversation(db: Session, conversation_id: str, user: User) -> bool:
    """Best-effort tenant/project isolation for conversation-scoped data.

    AgentInvocation rows always carry the real ``user_id`` and the conversation
    (via ``conversations``) belongs to a single user/org. We treat a conversation
    as viewable by a user if (a) they own at least one invocation in it, or
    (b) an execution tied to it lives under their org. This keeps the execution
    tree / evidence reads confined to authorized conversations (no cross-tenant
    leak) without a dedicated conversation model dependency.
    """
    from app.models.agent_invocation import AgentInvocation

    hit = (
        db.query(AgentInvocation.id)
        .filter(
            AgentInvocation.conversation_id == conversation_id,
            AgentInvocation.user_id == user.id,
            AgentInvocation.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if hit:
        return True
    from app.models.execution import Execution

    ex = (
        db.query(Execution.id)
        .filter(
            Execution.conversation_id == conversation_id,
            Execution.org_id == _user_org(user),
        )
        .first()
    )
    return ex is not None


def _serialize_evidence(item) -> dict:
    if item is None:
        return None
    data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
    # Security: the shared-evidence contract promises peers/frontend receive a
    # *reference* — provenance metadata + compact findings + validation status —
    # never the raw result rows. ``to_dict()`` leaks ``query_provenance.rows``
    # (and any nested ``rows`` payload), so strip it here at the REST boundary.
    # Findings stay (they are compact, curated numbers, not raw DB rows).
    qp = data.get("query_provenance")
    if isinstance(qp, dict):
        cleaned = {k: v for k, v in qp.items() if k != "rows"}
        if "rows" in qp:
            cleaned.setdefault("row_count", len(qp["rows"]) if isinstance(qp["rows"], (list, tuple)) else None)
        data["query_provenance"] = cleaned
    # Belt-and-braces: never ship a top-level "rows" payload either.
    data.pop("rows", None)
    return data


from datetime import datetime  # noqa: E402 — used by the tree turn filter

@router.get("/executions/tree", tags=["executions"])
def agent_execution_tree(
    execution_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    started_after: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P2 Phase 3 — multi-agent execution tree for visualization.

    Reuses existing AgentInvocation records (no new event storage). Scoped by
    conversation_id + user_id; an execution_id is resolved to its conversation
    under the caller's org. Returns the parent→child tree plus a compact summary.

    ``started_after`` (consistency audit D1, ISO-8601, optional) scopes the
    tree to a single turn so one user turn renders exactly its own executions.
    """
    if not execution_id and not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="ERR_BAD_REQUEST: provide execution_id or conversation_id",
        )
    awaiting_approval = False
    conv_id = conversation_id
    if execution_id:
        ex = _org_scoped_execution(execution_id, db, user)
        conv_id = ex.conversation_id
        awaiting_approval = bool(getattr(ex, "needs_approval", False))
    if not conv_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not _owns_conversation(db, conv_id, user):
        raise HTTPException(status_code=404, detail="Conversation not found")
    _started_after = None
    if started_after:
        try:
            _started_after = datetime.fromisoformat(
                started_after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="ERR_BAD_REQUEST: invalid started_after (ISO-8601 expected)",
            )
    return build_agent_tree(
        db, conversation_id=conv_id, user_id=user.id,
        awaiting_approval=awaiting_approval,
        started_after=_started_after,
    )


@router.get("/executions/evidence", tags=["executions"])
def agent_evidence(
    conversation_id: str = None,
    evidence_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P2 Phase 3 — read shared-evidence references (reuses the existing registry).

    Exposes the in-process/Redis evidence registry over REST so the frontend
    Evidence panel can resolve an evidence_id. Scoped by conversation ownership.
    Never returns raw result rows — only the reference + compact findings.
    """
    if not conversation_id:
        raise HTTPException(
            status_code=400, detail="ERR_BAD_REQUEST: conversation_id is required",
        )
    if not _owns_conversation(db, conversation_id, user):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if evidence_id:
        item = get_evidence(conversation_id, evidence_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Evidence not found")
        return _serialize_evidence(item)
    return [_serialize_evidence(i) for i in get_registry().list_items(conversation_id)]


@router.post("/executions")
def create_execution(
    req: CreateExecutionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create and run a new Synexia FSM execution."""
    if not settings.SYNEXIA_FSM_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Synexia FSM is not enabled. Set SYNEXIA_FSM_ENABLED=true to enable.",
        )

    from app.services.synexia.fsm import SynexiaFSM, ExecutionRequest
    from app.services.synexia.identity import resolve_agent_in_org

    org_id = _user_org(user)
    # Resolve the agent within the CALLER'S org (IC-1). An unknown agent
    # does not fail the request — the FSM fails closed on capabilities —
    # but the resolved PK (when present) threads through as agent_id so
    # memories/config/profiles can never be resolved cross-org.
    agent = resolve_agent_in_org(
        db, agent_name=req.agent_name, org_id=org_id,
    )

    fsm = SynexiaFSM(db)
    result = fsm.run(ExecutionRequest(
        conversation_id=req.conversation_id,
        agent_name=req.agent_name,
        agent_id=agent.id if agent else None,
        user_message=req.user_message,
        user_id=user.id,
        mode=req.mode,
        org_id=org_id,
        app_id=getattr(user, "app_id", None) or "default-app",
    ))

    return {
        "execution_id": result.execution_id,
        "state": result.state,
        "assistant_content": result.assistant_content,
        "tool_calls": result.tool_calls,
        "artifact_ids": result.artifact_ids,
        "confidence": result.confidence,
        "confidence_factors": result.confidence_factors,
        "plan_summary": result.plan_summary,
        "report_card_payload": result.report_card_payload,
        "file_exports": result.file_exports,
        "export_artifact_id": result.export_artifact_id,
    }


@router.get("/executions/{execution_id}")
def get_execution(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get execution state, plan, and observations."""
    execution = _org_scoped_execution(execution_id, db, user)

    result = execution.to_dict()
    result["observations"] = [o.to_dict() for o in execution.observations]

    if execution.plan:
        plan_data = execution.plan.to_dict()
        plan_data["nodes"] = [n.to_dict() for n in execution.plan.nodes]
        result["plan"] = plan_data

    return result


@router.get("/executions/{execution_id}/agent-tree", tags=["executions"])
def agent_execution_tree_by_execution(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P2 Phase 3 — execution tree scoped by execution (org-confined)."""
    execution = _org_scoped_execution(execution_id, db, user)
    conv_id = execution.conversation_id
    if not conv_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return build_agent_tree(
        db, conversation_id=conv_id, user_id=user.id,
        awaiting_approval=bool(getattr(execution, "needs_approval", False)),
    )


class ReplayRequest(BaseModel):
    """Optional overrides for a deterministic replay (IBM framework-audit #2).

    Mirrors LangGraph's time-travel idea: re-run a PAST execution's exact
    plan — same node definitions, same order — against the current (or
    overridden) model/prompt, producing a NEW execution you can diff.
    """
    model: Optional[str] = None
    notes: Optional[str] = None


@router.post("/executions/{execution_id}/replay")
def replay_execution(
    execution_id: str,
    req: ReplayRequest | None = None,
    run: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Deterministic replay of a past execution's plan (time-travel).

    Clones the source execution's Plan + PlanNodes into a new Execution in
    ``frozen`` mode (same node definitions and dependencies, fresh state)
    and returns the new execution WITHOUT running it — the caller drives
    execution via the normal plan-approve pipeline, or with
    ``?run=true`` to execute immediately via execute_plan_nodes.
    """
    from app.models.execution import Execution, Plan, PlanNode

    if req is None:
        req = ReplayRequest()

    # IC-5: the source must belong to the caller's org — foreign plans
    # are rejected BEFORE any clone or execute_plan_nodes call.
    source = _org_scoped_execution(execution_id, db, user)
    if source.plan is None or not source.plan.nodes:
        raise HTTPException(422, "Source execution has no plan to replay")

    from datetime import datetime as _dt, timezone as _tz

    clone = Execution(
        conversation_id=source.conversation_id,
        agent_name=source.agent_name,
        user_message=source.user_message,
        mode="frozen",  # "frozen = replay a previous plan"
        task_spec=source.task_spec,
        current_state="plan",
        started_at=_dt.now(_tz.utc),
        # The clone is the CALLER'S execution — never inherit a stale org
        # (the model default would silently collapse it to default-org).
        org_id=_user_org(user),
    )
    db.add(clone)
    db.flush()

    plan = Plan(
        execution_id=clone.id,
        version=1,
        status="approved",  # frozen replays skip the GATE
        summary=f"Replay of execution {source.id}"
        + (f" — {req.notes}" if req.notes else ""),
        is_acyclic=source.plan.is_acyclic,
    )
    db.add(plan)
    db.flush()

    for node in sorted(source.plan.nodes, key=lambda n: n.seq):
        db.add(PlanNode(
            plan_id=plan.id,
            seq=node.seq,
            node_type=node.node_type,
            name=node.name,
            description=node.description,
            dependencies=node.dependencies,
            inputs=node.inputs,
            expected_output=node.expected_output,
            output_artifact_type=node.output_artifact_type,
            risk_tier=node.risk_tier,
            requires_confirmation=node.requires_confirmation,
        ))
    db.commit()

    result = {
        "source_execution_id": source.id,
        "execution_id": clone.id,
        "plan_id": plan.id,
        "mode": "frozen",
        "nodes_cloned": len(source.plan.nodes),
        "model_override": req.model,
    }

    if run:
        from app.services.synexia.capability_router import execute_plan_nodes

        db.refresh(clone)
        observations = execute_plan_nodes(
            db, clone, clone.plan, user.id,
            endpoint=None,
        )
        result["observations"] = len(observations)
        result["status"] = "executed"

    return result


@router.get("/executions/{execution_id}/plan")
def get_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get the execution plan (PlanDAG)."""
    execution = _org_scoped_execution(execution_id, db, user)
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    plan = execution.plan.to_dict()
    plan["nodes"] = [n.to_dict() for n in execution.plan.nodes]
    return plan


@router.patch("/executions/{execution_id}/plan/approve")
def approve_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Approve the execution plan (pass the GATE state)."""
    execution = _org_scoped_execution(execution_id, db, user)
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    execution.plan.status = "approved"
    db.commit()

    return {"execution_id": execution_id, "plan_status": "approved"}


@router.patch("/executions/{execution_id}/plan/reject")
def reject_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Reject the execution plan."""
    execution = _org_scoped_execution(execution_id, db, user)
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    execution.plan.status = "rejected"
    execution.current_state = "fail"
    db.commit()

    return {"execution_id": execution_id, "plan_status": "rejected"}


@router.get("/executions/{execution_id}/observations")
def get_observations(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get observation records for an execution."""
    from app.models.execution import ObservationRecord  # noqa: F401

    execution = _org_scoped_execution(execution_id, db, user)

    return [o.to_dict() for o in execution.observations]
