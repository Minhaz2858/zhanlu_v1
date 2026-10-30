"""P0 governance hardening — fail-closed ontology action permissions.

Covers: permission_scope enforcement on both execution paths, fail-closed
behavior for unidentified callers and unbound agents, trusted console-user
identity, cross-domain scope isolation, denial auditing, and audit
vocabulary registration (PRD 2026-10-30 Phase 0, gaps G7 + G3-bug).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyAction, OntologyObject
import app.models.identity  # noqa: F401 — register identity tables in metadata (ordering-dependent create_all)

ORG = "org-test"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _make_write_action(db, tool_name="some_tool"):
    from app.models.ontology import OntologyDomain
    dom = OntologyDomain(org_id=ORG, code="procurement", name="Procurement")
    db.add(dom)
    db.flush()
    obj = OntologyObject(
        org_id=ORG, domain_id=dom.id, name="PurchaseOrder",
        backing_kind="manual",
    )
    db.add(obj)
    db.flush()
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="CreatePO",
        tool_name=tool_name, risk_level="write",
        requires_confirmation=True, params_json={"qty": 5},
    )
    db.add(act)
    db.commit()
    return act


def _add_binding(db, agent_name, scope):
    from app.models.ontology import AgentDomainBinding
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name=agent_name,
        domain_id=obj.domain_id, permission_scope=scope,
    ))
    db.commit()


def _async_fake(fn):
    async def _inner(*args, **kwargs):
        return fn(*args, **kwargs)
    return _inner


def test_audit_vocabularies_whitelist_ontology():
    from app.models.audit_event import AUDIT_EVENT_SOURCES, AUDIT_EVENT_TYPES
    assert "ontology" in AUDIT_EVENT_SOURCES
    for t in ("ontology_fanout", "escalation_created", "escalation_resolved",
              "policy_violation"):
        assert t in AUDIT_EVENT_TYPES


# ── Fail-closed rule matrix (RED — implemented in Tasks 4-5) ────────────


def test_write_denied_for_agent_without_binding(db):
    """Fail closed: an agent with NO domain binding cannot execute a write
    action, and NO ApprovalRequest is created."""
    from app.models.governance import ApprovalRequest
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True
    assert "no binding" in res["reason"]
    assert db.query(ApprovalRequest).count() == 0


def test_write_denied_for_read_only_binding(db):
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True
    assert "read-only" in res["reason"]


def test_write_allowed_with_write_binding(db):
    """A write-scope binding lets the normal flow proceed: a pending
    ApprovalRequest is created (risk contract unchanged)."""
    from app.models.governance import ApprovalRequest
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "write")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("blocked") == "confirmation_required"
    assert res.get("approval_id")
    approval = db.query(ApprovalRequest).filter(
        ApprovalRequest.id == res["approval_id"]).one()
    assert approval.status == "pending"


def test_write_denied_for_unidentified_caller(db):
    """Empty context (e.g. some MCP paths) → denied. The historical
    'general_assistant' default must NOT grant identity here."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(db, ORG, "u1", act.id, params={}, context={})
    assert res["success"] is False
    assert res.get("denied") is True


def test_console_user_requesting_write_still_gets_approval(db):
    """Humans via HTTP keep today's semantics: no binding required, the
    governance approval flow gates the run. Console identity is trusted
    only with a non-empty authenticated_user_id."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                 "caller_kind": "console_user"},
    )
    assert res["success"] is False
    assert res.get("blocked") == "confirmation_required"
    assert res.get("approval_id")


def test_multiple_bindings_same_domain_are_impossible(db):
    """Schema guarantee: ONE binding row per (agent, domain) — the
    'conflicting multiple scopes' ambiguity cannot occur; the single
    row's scope is authoritative. The checker's any-write-scope logic
    stays defensive if this constraint is ever relaxed."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.models.ontology import AgentDomainBinding

    _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name="some_agent",
        domain_id=obj.domain_id, permission_scope="write",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_write_denied_for_binding_in_other_domain(db):
    """A write binding on Domain B must NOT authorize a write action of
    Domain A — scope is per-domain, never org-wide."""
    from app.models.ontology import AgentDomainBinding, OntologyDomain
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)  # action's domain: procurement
    other = OntologyDomain(org_id=ORG, code="finance", name="Finance")
    db.add(other)
    db.flush()
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name="some_agent",
        domain_id=other.id, permission_scope="write",
    ))
    db.commit()
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True


def test_console_user_impersonation_is_denied(db):
    """caller_kind='console_user' is only trusted when established by
    authenticated HTTP code — it must arrive with a non-empty
    authenticated_user_id. Anything else is unidentified (fail closed)."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    for ctx in (
        {"caller_kind": "console_user"},
        {"caller_kind": "console_user", "authenticated_user_id": ""},
    ):
        res = execute_action(db, ORG, "u1", act.id, params={}, context=ctx)
        assert res["success"] is False, ctx
        assert res.get("denied") is True, ctx


def test_read_actions_are_unaffected(db):
    """The read=auto contract is untouched: a read action with NO identity
    context passes the permission gate (failure is the honest 'no bound
    tool' proposal reason, never a permission denial)."""
    from app.models.ontology import OntologyAction, OntologyObject
    from app.services.ontology.data import execute_action

    _make_write_action(db)  # creates the PurchaseOrder object (fixture reuse)
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="ListPOs",
        tool_name=None, risk_level="read", requires_confirmation=False,
    )
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id, params={}, context={})
    assert res["success"] is False
    assert "denied" not in res          # NOT a permission refusal
    assert "no bound tool" in res["reason"]


def test_approved_execution_still_checks_scope(db, monkeypatch):
    """A read-scope agent with a VALID approval is still denied — an
    approval proves a human OK'd THIS write; it does not upgrade the
    agent's domain scope (refinement: both paths protected)."""
    import app.services.agent_tools as agent_tools
    from app.services.governance.approval_service import ApprovalService
    from app.services.ontology.data import execute_action, execute_approved_action

    act = _make_write_action(db, tool_name="po_tool")
    _add_binding(db, "some_agent", "read")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res.get("denied") is True  # ordinary execution denied

    # Approval created via the console path (simulating prior human flow)
    res2 = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                 "caller_kind": "console_user"},
    )
    approval_id = res2["approval_id"]
    ApprovalService(db).approve(approval_id, reviewed_by="boss")

    calls: list = []

    def fake_execute_tool(name, args, session, user_id, context=None):
        calls.append((name, context.get("approval_id")))
        return {"success": True}

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))

    # The read-scope agent re-runs with the approval → DENIED
    res3 = execute_approved_action(
        db, ORG, "u1", act.id, approval_id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res3["success"] is False
    assert res3.get("denied") is True
    assert calls == []

    # A console user re-runs the same approval → runs
    res4 = execute_approved_action(
        db, ORG, "u1", act.id, approval_id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                 "caller_kind": "console_user"},
    )
    assert res4["success"] is True
    assert calls and calls[0][1] == approval_id


def test_denial_writes_structured_audit_event(db):
    """Denials are audited with the structured policy_violation event —
    written via an independent session (failure-safe), still visible here."""
    from app.models.audit_event import AuditEvent
    from app.services.ontology import permissions as perm
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    evt = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.org_id == ORG,
            AuditEvent.event_type == "policy_violation",
            AuditEvent.event_source == "ontology",
            AuditEvent.outcome == "denied",
        )
        .one()
    )
    assert evt.detail_json["caller_kind"] == "agent"
    assert evt.detail_json["action_name"] == "CreatePO"
    assert "read-only" in evt.detail_json["reason"]


def test_denial_audit_failure_never_masks_refusal(db, monkeypatch):
    """Refinement: if the audit write fails through the REAL wiring (the
    denial path in execute_action), the security refusal STILL returns
    cleanly — the audit failure never becomes an execution failure."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")

    # Make the audit INSERT itself fail, through the real wiring:
    class _BoomAudit:
        def __init__(self, *a, **kw):
            raise RuntimeError("audit store down")

    monkeypatch.setattr("app.models.audit_event.AuditEvent", _BoomAudit)

    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False and res.get("denied") is True
    assert "read-only" in res["reason"]


def test_nested_ontology_write_via_read_action_cannot_inherit_identity(db, monkeypatch):
    """Review fix (blocking): a READ action bound to execute_ontology_action
    must not forward its console identity claim into the nested execute —
    the nested WRITE must re-enter the permission gate with no identity and
    fail closed (no approval is created, nothing runs)."""
    import app.services.agent_tools as agent_tools
    from app.models.ontology import OntologyAction
    from app.services.ontology.data import execute_action

    write_act = _make_write_action(db, tool_name="po_tool")

    read_act = OntologyAction(
        org_id=ORG, object_id=write_act.object_id,
        name="TriggerPO", tool_name="execute_ontology_action",
        risk_level="read", requires_confirmation=False,
        params_json={"action_id": write_act.id},
    )
    db.add(read_act)
    db.commit()

    calls: list = []

    def fake_execute_tool(name, args, session, user_id, context=None):
        calls.append((name, dict(context or {})))
        # The nested call re-enters the real execute_tool → for
        # execute_ontology_action the registry handler runs; simulate the
        # dispatcher faithfully by calling the ontology handler logic via
        # execute_action with the context the dispatcher would pass.
        if name == "execute_ontology_action":
            from app.services.ontology.data import execute_action as _ea
            return _ea(session, ORG, user_id, args["action_id"],
                       params=args.get("params") or {}, context=context)
        return {"success": True}

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))

    res = execute_action(
        db, ORG, "u1", read_act.id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                 "caller_kind": "console_user"},
    )
    # The read action itself runs (auto), but the nested write is DENIED —
    # the console identity claim must not have been forwarded.
    nested = [c for c in calls if c[0] == "execute_ontology_action"]
    assert nested, "nested tool should have been invoked"
    ctx = nested[0][1]
    assert "caller_kind" not in ctx and "authenticated_user_id" not in ctx
    assert res["success"] is False           # overall reflects the nested denial
    assert res["result"].get("denied") is True
    assert "requires an identified caller" in res["result"]["reason"]


def test_read_scope_agent_can_still_run_read_actions(db, monkeypatch):
    """Read-scope agents keep the read=auto contract end to end (agent
    path, not just empty context)."""
    import app.services.agent_tools as agent_tools
    from app.models.ontology import OntologyAction
    from app.services.ontology.data import execute_action

    _make_write_action(db)
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    _add_binding(db, "some_agent", "read")
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="ListPOs",
        tool_name="po_lister", risk_level="read", requires_confirmation=False,
    )
    db.add(act)
    db.commit()

    calls: list = []

    def fake_execute_tool(name, args, session, user_id, context=None):
        calls.append(name)
        return {"success": True}

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))

    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is True
    assert calls == ["po_lister"]
