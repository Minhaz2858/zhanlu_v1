"""Agent lifecycle — publish/version/rollback/unpublish + canary gating.

ADLC P0-3 + P1-5 (IBM agent development lifecycle, 2026-10-08):

- ``publish_agent`` snapshots the full agent definition into an immutable
  ``AgentAppRevision`` and bumps ``AgentApp.version``.
- ``rollback_agent`` restores a revision's snapshot and bumps again
  (append-only history — nothing is ever overwritten).
- ``rollout_allows`` is the deterministic canary gate consulted at the
  protocol/delegation entry points (A2A tasks/send, ACP runs): a caller
  is consistently in or out for a given agent based on a stable hash of
  ``(agent_id, caller)`` vs ``rollout_percent``.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SNAPSHOT_FIELDS = (
    "name", "description", "capabilities", "model", "agent_type",
    "prompt_identity", "prompt_boundary", "prompt_reasoning",
    "prompt_tools", "prompt_output",
    "skills", "knowledge_bases", "topology", "sub_agents",
    "flow_mode", "flow", "max_call_count", "max_retries", "max_iterations",
    "data_read", "data_write", "human_fallback", "temperature", "top_p",
    "max_tokens", "tool_config", "manifest_json", "data_bindings",
    "skill_bindings", "memory_scope", "policy_profile", "output_contract",
    "llm_model_id",
)


def snapshot_agent(agent) -> dict[str, Any]:
    """Full definition snapshot for a revision row."""
    snap: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        snap[field] = getattr(agent, field, None)
    return snap


def _revision(agent, version: int, action: str, user_id: str | None,
              note: str | None) -> "AgentAppRevision":
    from app.models.agent_app_revision import AgentAppRevision

    return AgentAppRevision(
        agent_app_id=agent.id,
        version=version,
        snapshot=snapshot_agent(agent),
        published_by_id=user_id,
        note=note,
        action=action,
        status_at_publish=agent.status,
        published_at=datetime.now(timezone.utc),
    )


class EvalGateBlocked(Exception):
    """Raised when publish is refused because the agent's eval suite is
    failing (P1-6: evals as a release GATE, not just a dashboard)."""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("reason", "eval gate blocked publish"))


def eval_gate_status(db, agent_id: str) -> dict | None:
    """Evaluate the promote gate for an agent. Returns None when the gate
    passes (or doesn't apply); otherwise a detail dict explaining WHY.

    Applies only when the agent HAS golden test cases — an agent with no
    suite is not failed, but the caller may still choose to warn.
    """
    from app.config import settings
    from app.models.agent_test_case import AgentTestCase

    if not getattr(settings, "EVAL_PROMOTE_GATE_ENABLED", True):
        return None
    cases = (
        db.query(AgentTestCase)
        .filter(AgentTestCase.agent_app_id == agent_id)
        .all()
    )
    if not cases:
        return None  # no suite → gate not applicable
    total_runs = sum(int(c.run_count or 0) for c in cases)
    total_pass = sum(int(c.pass_count or 0) for c in cases)
    failing = [c.name for c in cases if (c.last_result or "") == "fail"]
    pass_rate = (total_pass / total_runs) if total_runs else 0.0
    floor = float(getattr(settings, "EVAL_GATE_FLOOR", 0.8))
    if failing or pass_rate < floor:
        return {
            "blocked": "eval_gate",
            "reason": (
                f"eval gate: pass_rate {pass_rate:.2f} < floor {floor:.2f}"
                + (f"; failing cases: {', '.join(failing[:5])}" if failing else "")
            ),
            "pass_rate": round(pass_rate, 3),
            "floor": floor,
            "failing_cases": failing[:10],
            "hint": "fix the failing cases, or publish with force=true to override (audited)",
        }
    return None


def publish_agent(db, agent_id: str, user_id: str | None = None,
                  note: str | None = None,
                  force: bool = False) -> dict[str, Any]:
    """Snapshot + bump version + set status=published. Idempotent-safe:
    re-publishing without changes still creates a new revision (audit).

    P1-6: an agent whose golden eval suite is failing is REFUSED unless
    ``force=True`` — which is recorded in the revision note (audited)."""
    from app.models.agent_app import AgentApp
    from app.models.agent_app_revision import AgentAppRevision

    agent = db.query(AgentApp).filter(
        AgentApp.id == agent_id, AgentApp.is_deleted == False).first()  # noqa: E712
    if agent is None:
        raise LookupError(f"Agent not found: {agent_id}")

    gate = eval_gate_status(db, agent_id)
    if gate and not force:
        raise EvalGateBlocked(gate)

    if gate and force:
        note = f"{note or ''} [eval-gate overridden] {gate['reason']}".strip()

    agent.version = (agent.version or 1) + 1
    agent.status = "published"
    revision = _revision(agent, agent.version, "publish", user_id, note)
    db.add(revision)
    db.commit()
    logger.info("agent_lifecycle: %s published v%d by %s (gate=%s)",
                agent_id, agent.version, user_id,
                "forced" if gate else "pass")
    return {"agent_id": agent_id, "version": agent.version, "status": agent.status}


def unpublish_agent(db, agent_id: str, user_id: str | None = None) -> dict[str, Any]:
    """Pull an agent from the catalog (draft) — protocol discovery stops
    serving it immediately; no data is lost."""
    from app.models.agent_app import AgentApp

    agent = db.query(AgentApp).filter(
        AgentApp.id == agent_id, AgentApp.is_deleted == False).first()  # noqa: E712
    if agent is None:
        raise LookupError(f"Agent not found: {agent_id}")
    agent.status = "draft"
    db.commit()
    logger.info("agent_lifecycle: %s unpublished by %s", agent_id, user_id)
    return {"agent_id": agent_id, "status": agent.status}


def rollback_agent(db, agent_id: str, to_version: int,
                   user_id: str | None = None) -> dict[str, Any]:
    """Restore a revision's snapshot and bump to a new version."""
    from app.models.agent_app import AgentApp
    from app.models.agent_app_revision import AgentAppRevision

    agent = db.query(AgentApp).filter(
        AgentApp.id == agent_id, AgentApp.is_deleted == False).first()  # noqa: E712
    if agent is None:
        raise LookupError(f"Agent not found: {agent_id}")
    revision = (
        db.query(AgentAppRevision)
        .filter(AgentAppRevision.agent_app_id == agent_id,
                AgentAppRevision.version == to_version)
        .order_by(AgentAppRevision.created_date.desc())
        .first()
    )
    if revision is None:
        raise LookupError(f"No revision v{to_version} for agent {agent_id}")

    for field, value in (revision.snapshot or {}).items():
        setattr(agent, field, value)
    agent.version = (agent.version or 1) + 1
    agent.status = "published"
    new_revision = _revision(
        agent, agent.version, "rollback", user_id,
        note=f"rollback to v{to_version}",
    )
    db.add(new_revision)
    db.commit()
    logger.info(
        "agent_lifecycle: %s rolled back to v%d (now v%d) by %s",
        agent_id, to_version, agent.version, user_id,
    )
    return {"agent_id": agent_id, "version": agent.version,
            "rolled_back_to": to_version, "status": agent.status}


def list_revisions(db, agent_id: str) -> list[dict[str, Any]]:
    from app.models.agent_app_revision import AgentAppRevision

    revisions = (
        db.query(AgentAppRevision)
        .filter(AgentAppRevision.agent_app_id == agent_id)
        .order_by(AgentAppRevision.version.desc())
        .all()
    )
    return [r.to_dict() for r in revisions]


def rollout_allows(agent, caller_key: str | None) -> bool:
    """Deterministic canary gate: stable hash of (agent_id, caller) vs
    rollout_percent. A caller is consistently in or out — retrying with a
    new task id does not dodge the bucket (use a caller-keyed seed)."""
    raw = getattr(agent, "rollout_percent", 100)
    percent = 100 if raw is None else int(raw)
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    digest = hashlib.md5(f"{agent.id}:{caller_key or 'anonymous'}".encode()).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < percent


__all__ = [
    "snapshot_agent", "publish_agent", "unpublish_agent",
    "rollback_agent", "list_revisions", "rollout_allows",
]
