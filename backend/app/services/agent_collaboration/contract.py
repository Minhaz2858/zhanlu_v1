"""Structured Agent Task Contract (P1/P2 spec §3).

Every delegated task — whether GA→specialist (Pattern A/B) or
specialist→specialist (Pattern C) — carries this contract. We reuse the
existing canonical IDs (conversation_id, organization_id, user_id,
project_id, invocation ids, trace_id) rather than inventing new DB
columns: only the *negotiated collaboration* fields (task_id,
parent_task_id, requesting_agent_id, target_agent_id, delegation_path,
status, execution_budget, deadline) are carried in the runtime context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import uuid


@dataclass
class AgentTaskContract:
    # ── canonical IDs (reused from existing models/context) ──
    conversation_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    parent_invocation_id: Optional[str] = None
    trace_id: Optional[str] = None

    # ── collaboration-negotiated fields (spec §3) ──
    task_id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    parent_task_id: Optional[str] = None
    requesting_agent_id: Optional[str] = None  # who asked (GA or a specialist)
    target_agent_id: Optional[str] = None       # who executes
    department_context: Optional[str] = None
    objective: Optional[str] = None
    constraints: Optional[dict] = None
    required_capabilities: Optional[list] = None
    authorized_resource_context: Optional[dict] = None
    expected_output_schema: Optional[dict] = None
    execution_budget: Optional[dict] = None
    deadline: Optional[str] = None
    token_budget: Optional[int] = None  # total token ceiling for this task tree

    # ── runtime state ──
    status: str = "pending"  # pending|running|completed|partial|failed|blocked|requires_approval
    delegation_path: list = field(default_factory=list)  # agent ids visited, root-first

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "parent_invocation_id": self.parent_invocation_id,
            "conversation_id": self.conversation_id,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
            "requesting_agent_id": self.requesting_agent_id,
            "target_agent_id": self.target_agent_id,
            "project_id": self.project_id,
            "department_context": self.department_context,
            "objective": self.objective,
            "constraints": self.constraints,
            "required_capabilities": self.required_capabilities,
            "authorized_resource_context": self.authorized_resource_context,
            "expected_output_schema": self.expected_output_schema,
            "execution_budget": self.execution_budget,
            "deadline": self.deadline,
            "token_budget": self.token_budget,
            "trace_id": self.trace_id,
            "status": self.status,
            "delegation_path": list(self.delegation_path),
        }


def build_contract(
    *,
    objective: str,
    target_agent_id: str,
    context: Optional[dict],
    requesting_agent_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    parent_invocation_id: Optional[str] = None,
    constraints: Optional[dict] = None,
    required_capabilities: Optional[list] = None,
    expected_output_schema: Optional[dict] = None,
    execution_budget: Optional[dict] = None,
    deadline: Optional[str] = None,
    token_budget: Optional[int] = None,
    delegation_path: Optional[list] = None,
    **extra: Any,
) -> AgentTaskContract:
    """Assemble a contract from the calling context + delegation args.

    Falls back to context-supplied IDs so a nested (Pattern C) delegation
    inherits the original user/project/conversation identity — the user's
    authorization context is NEVER replaced by the requesting agent's.
    """
    ctx = context or {}
    return AgentTaskContract(
        task_id=f"task-{uuid.uuid4().hex[:12]}",
        parent_task_id=parent_task_id,
        parent_invocation_id=parent_invocation_id or ctx.get("parent_invocation_id"),
        conversation_id=ctx.get("conversation_id"),
        organization_id=ctx.get("organization_id"),
        user_id=ctx.get("user_id"),
        project_id=ctx.get("project_id"),
        trace_id=ctx.get("trace_id") or f"trace-{uuid.uuid4().hex[:12]}",
        requesting_agent_id=requesting_agent_id or ctx.get("requesting_agent_id") or ctx.get("agent_name"),
        target_agent_id=target_agent_id,
        department_context=ctx.get("department"),
        objective=objective,
        constraints=constraints,
        required_capabilities=required_capabilities,
        authorized_resource_context={
            "bound_kb_ids": ctx.get("bound_kb_ids"),
        },
        expected_output_schema=expected_output_schema,
        execution_budget=execution_budget,
        deadline=deadline,
        token_budget=token_budget if token_budget is not None else ctx.get("token_budget"),
        delegation_path=list(delegation_path or ctx.get("delegation_path") or []),
        **extra,
    )
