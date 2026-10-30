"""Ontology action permission enforcement — fail closed (PRD P0, 2026-10-30).

Single decision point for ontology action execution. The ONLY two
functions that run an ontology action's tool are execute_action and
execute_approved_action (services/ontology/data.py); every caller —
agent tool handler, HTTP router, MCP, nested execute_tool — funnels
through them, so checking there covers all paths.

Rules (fail closed):
- WRITE actions (ONE shared predicate: risk_level=='write' OR
  requires_confirmation) require an explicit permission_scope='write'
  AgentDomainBinding on the action's OWN domain; multiple bindings
  grant if ANY row is write-scope; read-only rows never do.
- Unidentified callers (no agent_name, no caller_kind) are denied
  writes. The historical 'general_assistant' default in
  ontology_tool.py:29 is deliberately NOT inherited here.
- console_user callers may REQUEST only when the context was
  established by authenticated HTTP code: caller_kind='console_user'
  AND a non-empty authenticated_user_id. Anything else (tool args,
  MCP payloads, user-controlled context keys) can never impersonate a
  console user — it is treated as unidentified.
- READ actions are unchanged (read=auto risk contract).
- Denial auditing is failure-safe: it writes via an INDEPENDENT
  session/transaction, so an audit persistence failure can neither
  mask the refusal nor commit unrelated pending changes on the
  caller's session.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.ontology import AgentDomainBinding, OntologyAction, OntologyObject

logger = logging.getLogger(__name__)


def is_write_action(action: OntologyAction) -> bool:
    """THE write predicate — the single source of truth used by
    execute_action, execute_approved_action and the checker below, so
    every path applies an identical definition."""
    return action.risk_level == "write" or bool(action.requires_confirmation)


def resolve_caller(context: dict | None) -> dict:
    """Classify the caller WITHOUT any identity default (no masquerade).

    console_user is trusted only with proof of authenticated HTTP
    identity (non-empty authenticated_user_id)."""
    ctx = context or {}
    if ctx.get("caller_kind") == "console_user":
        user_id = str(ctx.get("authenticated_user_id") or "").strip()
        if user_id:
            return {"kind": "console_user", "agent_name": None,
                    "user_id": user_id}
        return {"kind": "unidentified", "agent_name": None, "user_id": None}
    agent_name = ctx.get("agent_name") or None
    if agent_name:
        return {"kind": "agent", "agent_name": str(agent_name), "user_id": None}
    return {"kind": "unidentified", "agent_name": None, "user_id": None}


def _domain_id_for_action(db: Session, org_id: str, action: OntologyAction) -> str | None:
    obj = (
        db.query(OntologyObject)
        .filter(
            OntologyObject.id == action.object_id,
            OntologyObject.org_id == org_id,
            OntologyObject.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    return obj.domain_id if obj else None


def check_action_permission(
    db: Session, org_id: str, action: OntologyAction, context: dict | None
) -> dict | None:
    """Return {'denied': True, 'reason': ...} when the caller may not
    proceed, else None. The caller reports denials honestly via
    audit_permission_denial (failure-safe, independent session)."""
    if not is_write_action(action):
        return None

    caller = resolve_caller(context)
    if caller["kind"] == "console_user":
        return None

    if caller["kind"] == "unidentified":
        return {
            "denied": True,
            "reason": (
                f"write action {action.name!r} requires an identified "
                "caller (agent_name) or authenticated console session — "
                "request refused"
            ),
        }

    domain_id = _domain_id_for_action(db, org_id, action)
    if not domain_id:
        return {
            "denied": True,
            "reason": f"action {action.name!r} has no resolvable object/domain",
        }

    bindings = (
        db.query(AgentDomainBinding)
        .filter(
            AgentDomainBinding.org_id == org_id,
            AgentDomainBinding.agent_name == caller["agent_name"],
            AgentDomainBinding.domain_id == domain_id,
            AgentDomainBinding.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    if not bindings:
        return {
            "denied": True,
            "reason": (
                f"agent {caller['agent_name']!r} has no binding on this "
                "domain — write actions are refused (fail closed)"
            ),
        }
    if not any(b.permission_scope == "write" for b in bindings):
        return {
            "denied": True,
            "reason": (
                f"agent {caller['agent_name']!r} is bound read-only to "
                "this domain — write actions are refused"
            ),
        }
    return None


def audit_permission_denial(
    db: Session, org_id: str, action: OntologyAction, context: dict | None,
    reason: str, *, session_factory=None,
) -> None:
    """Failure-safe structured denial audit.

    Writes via an INDEPENDENT session/transaction on the CALLER'S engine
    bind (so the right database is used in every environment) so that:
    - an audit persistence failure NEVER turns the security refusal
      into an execution failure, and
    - the caller's session is never committed as a side effect (no
      unrelated pending changes get flushed by the audit).
    Tests inject session_factory to point at a specific engine.

    Note: on Postgres the independent session uses its own pooled
    connection. Under SQLite StaticPool (tests) the "independent" session
    shares the caller's DBAPI connection, so its commit would also commit
    the caller's pending writes — acceptable in tests where the denial
    path has only run SELECTs, but do not rely on isolation there."""
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session as _Session

    from app.models.audit_event import AuditEvent

    caller = resolve_caller(context)
    if session_factory is None:
        try:
            bind = db.get_bind()

            def session_factory():
                return _Session(bind=bind)
        except Exception as exc:  # noqa: BLE001 — detached session etc.
            logger.warning(
                "ontology permission denial audit: no bind available "
                "(refusal stands): %s", exc,
            )
            return
    try:
        s = session_factory()
        try:
            s.add(AuditEvent(
                org_id=org_id,
                event_type="policy_violation",
                event_source="ontology",
                actor_type="agent" if caller["kind"] == "agent" else caller["kind"],
                actor_id=caller["agent_name"] or caller.get("user_id"),
                resource_type="ontology_action",
                resource_id=action.id,
                outcome="denied",
                detail_json={
                    "action_name": action.name,
                    "tool_name": action.tool_name,
                    "caller_kind": caller["kind"],
                    "reason": reason,
                },
                occurred_at=datetime.now(timezone.utc),
            ))
            s.commit()
        finally:
            s.close()
    except Exception as exc:  # noqa: BLE001 — audit must never mask the refusal
        logger.warning(
            "ontology permission denial audit write failed (refusal stands): %s",
            exc,
        )
