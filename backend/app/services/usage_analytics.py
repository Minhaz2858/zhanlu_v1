"""Enterprise observability & cost aggregation (Phase 3.5, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Point-in-time cost summaries existed (``/governance/cost``) but nothing gave
administrators an ORG-level operational view: executions by department/agent,
reliability, tool-failure distribution, delegation/recovery frequency, token
consumption, approvals and unauthorized-attempt counts.

This service aggregates EXISTING tables only — AgentInvocation, AgentRunStep,
CostLedger (via its own endpoints), ApprovalRequest, PolicyDecision — no
second telemetry system. Every query is org-scoped via the
``TimestampedBase.org_id`` isolation wall.

Honesty rules (mission §F): unknown model pricing stays UNKNOWN — the cost
total only sums entries with a recorded cost_amount; executions without one
are counted separately as ``unknown_pricing_executions`` and never fabricated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))


def _org_window(db: Session, org_id: str, days: int):
    from app.models.agent_invocation import AgentInvocation

    return (
        db.query(AgentInvocation)
        .filter(
            AgentInvocation.org_id == org_id,
            AgentInvocation.created_date >= _since(days),
        )
        .all()
    )


def _tokens_of(inv) -> dict:
    tu = inv.token_usage if isinstance(inv.token_usage, dict) else {}
    return {
        "prompt_tokens": int(tu.get("prompt_tokens") or 0),
        "completion_tokens": int(tu.get("completion_tokens") or 0),
    }


def agent_stats(db: Session, *, org_id: str, days: int = 30) -> list[dict]:
    """Per-agent rollup: executions, success/failure rates, latency, tokens,
    cost honesty (known vs unknown pricing)."""
    from app.models.agent_app import AgentApp

    invs = _org_window(db, org_id, days)
    app_names = {
        a.id: a.name
        for a in db.query(AgentApp).filter(AgentApp.org_id == org_id).all()
    }
    rollup: dict[str, dict] = {}
    for inv in invs:
        name = app_names.get(inv.agent_app_id, inv.agent_app_id)
        r = rollup.setdefault(name, {
            "agent": name, "executions": 0, "completed": 0, "failed": 0,
            "durations": [], "prompt_tokens": 0, "completion_tokens": 0,
            "known_cost": 0.0, "unknown_pricing": 0,
        })
        r["executions"] += 1
        if inv.status == "completed":
            r["completed"] += 1
        elif inv.status in ("failed", "timeout", "cancelled"):
            r["failed"] += 1
        if inv.duration_ms is not None:
            r["durations"].append(int(inv.duration_ms))
        t = _tokens_of(inv)
        r["prompt_tokens"] += t["prompt_tokens"]
        r["completion_tokens"] += t["completion_tokens"]
        if inv.cost_amount is not None:
            r["known_cost"] += float(inv.cost_amount)
        else:
            r["unknown_pricing"] += 1

    out = []
    for r in rollup.values():
        durations = r.pop("durations")
        execs = r["executions"] or 1
        r["success_rate"] = round(r["completed"] / execs, 4)
        r["failure_rate"] = round(r["failed"] / execs, 4)
        r["avg_duration_ms"] = round(sum(durations) / len(durations)) if durations else None
        r["known_cost_total"] = round(r["known_cost"], 6)
        del r["known_cost"]
        out.append(r)
    out.sort(key=lambda s: -s["executions"])
    return out


def org_overview(db: Session, *, org_id: str, days: int = 30) -> dict:
    """Org-level operational dashboard payload (read-only, existing tables)."""
    from app.models.agent_app import AgentApp
    from app.models.project import Project
    from app.models.agent_run_step import AgentRunStep
    from app.models.governance import ApprovalRequest, PolicyDecision

    invs = _org_window(db, org_id, days)

    by_status: dict[str, int] = {}
    by_department: dict[str, int] = {}
    durations: list[int] = []
    prompt = completion = 0
    known_cost = 0.0
    unknown_pricing = 0
    delegations = 0

    app_ids = {inv.agent_app_id for inv in invs}
    apps = (
        db.query(AgentApp).filter(AgentApp.org_id == org_id).all()
        if app_ids else []
    )
    app_by_id = {a.id: a for a in apps}
    project_ids = {a.project_id for a in apps if a.project_id}
    projects = (
        db.query(Project).filter(Project.id.in_(project_ids)).all()
        if project_ids else []
    )
    dept_by_project = {
        p.id: (p.department or "unassigned") for p in projects
    }

    for inv in invs:
        by_status[inv.status] = by_status.get(inv.status, 0) + 1
        if inv.duration_ms is not None:
            durations.append(int(inv.duration_ms))
        t = _tokens_of(inv)
        prompt += t["prompt_tokens"]
        completion += t["completion_tokens"]
        if inv.cost_amount is not None:
            known_cost += float(inv.cost_amount)
        else:
            unknown_pricing += 1
        if inv.delegation_mode:
            delegations += 1
        app = app_by_id.get(inv.agent_app_id)
        dept = (
            dept_by_project.get(app.project_id, "unassigned")
            if app and app.project_id else "unassigned"
        )
        by_department[dept] = by_department.get(dept, 0) + 1

    # Tool-failure distribution (org-scoped; only failed tool steps).
    failed_tools = (
        db.query(AgentRunStep.tool_name, func.count(AgentRunStep.id))
        .filter(
            AgentRunStep.org_id == org_id,
            AgentRunStep.created_date >= _since(days),
            AgentRunStep.status == "failed",
            AgentRunStep.step_type == "tool_call",
        )
        .group_by(AgentRunStep.tool_name)
        .all()
    )
    tool_failures = [
        {"tool_name": name or "unknown", "failures": count}
        for name, count in failed_tools
    ]
    tool_failures.sort(key=lambda d: -d["failures"])

    approvals_rows = (
        db.query(ApprovalRequest.status, func.count(ApprovalRequest.id))
        .filter(
            ApprovalRequest.org_id == org_id,
            ApprovalRequest.created_date >= _since(days),
        )
        .group_by(ApprovalRequest.status)
        .all()
    )
    approvals = {status: count for status, count in approvals_rows}

    denied = (
        db.query(func.count(PolicyDecision.id))
        .filter(
            PolicyDecision.org_id == org_id,
            PolicyDecision.created_date >= _since(days),
            PolicyDecision.decision == "deny",
        )
        .scalar()
    ) or 0

    # Recovery frequency: invocations whose persisted output carries recovery
    # metadata (written by recover_orphaned_invocations / healing seams).
    recoveries = sum(
        1 for inv in invs
        if isinstance(inv.output_json, dict)
        and isinstance(inv.output_json.get("recovery"), dict)
    )

    return {
        "window_days": days,
        "total_executions": len(invs),
        "by_status": by_status,
        "by_department": by_department,
        "by_agent": agent_stats(db, org_id=org_id, days=days),
        "avg_duration_ms": (
            round(sum(durations) / len(durations)) if durations else None
        ),
        "tool_failures": tool_failures,
        "delegation_frequency": delegations,
        "recovery_frequency": recoveries,
        "token_usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "known_cost_total": round(known_cost, 6),
        "unknown_pricing_executions": unknown_pricing,
        "approvals": approvals,
        "unauthorized_operation_attempts": int(denied),
    }
