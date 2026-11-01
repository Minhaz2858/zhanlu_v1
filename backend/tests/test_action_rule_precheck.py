"""Phase 3D.2 — governed business-action pre-checks.

Enabled ``applies_to='action'`` BusinessRules are enforced DETERMINISTICALLY
at the ontology action execution boundary (``execute_action`` /
``execute_approved_action``) BEFORE side effects. The LLM never decides:
a statement prefixed ``PROHIBIT:``/``FORBIDDEN:``/``DENY:`` blocks;
``REQUIRE_APPROVAL:``/``APPROVAL_REQUIRED:`` demands the existing human
approval workflow; anything else is applicable-but-informational. Existing
permission checks and approval validity remain authoritative.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.agent_tools as agent_tools_module
from app.database import Base
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.governance import ApprovalRequest
from app.models.ontology import AgentDomainBinding, OntologyAction, OntologyObject
from app.services.governance.approval_service import ApprovalService
from app.services.ontology.business_rules import (
    applicable_action_rules,
    evaluate_action_rules,
)
from app.services.ontology.data import execute_action, execute_approved_action

ORG = "org-acme"
ORG2 = "org-other"
CONSOLE = {"caller_kind": "console_user", "authenticated_user_id": "u-1"}


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        OntologyAction.__table__, OntologyObject.__table__,
        AgentDomainBinding.__table__, BusinessRule.__table__,
        ApprovalRequest.__table__, AuditEvent.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def tool_calls(monkeypatch):
    """Stub the bound registry tool; record invocations (side effects)."""
    calls = []

    async def _stub(tool_name, params, db, user_id, context=None):
        calls.append({"tool": tool_name, "params": params})
        return {"success": True, "rows": []}

    monkeypatch.setattr(agent_tools_module, "execute_tool", _stub)
    return calls


@pytest.fixture()
def world(db):
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="dom-1",
                          name="PurchaseOrder"))
    db.add(OntologyAction(id="act-r", org_id=ORG, object_id="oo-1",
                          name="ListOrders", tool_name="stub_tool",
                          risk_level="read"))
    db.add(OntologyAction(id="act-w", org_id=ORG, object_id="oo-1",
                          name="CancelOrder", tool_name="stub_tool",
                          risk_level="write"))
    db.commit()


def _rule(db, rid, statement, *, org=ORG, status="enabled",
          applies_to="action", scope="org", scope_ref_id=None, ds=None):
    db.add(BusinessRule(
        id=rid, org_id=org, statement=statement, status=status,
        applies_to=applies_to, scope=scope, scope_ref_id=scope_ref_id,
        datasource_ids=ds))
    db.commit()


def _decision(result):
    return result.get("rule_decision") or {}


# 1. Enabled PROHIBIT rule blocks ---------------------------------------------------

def test_enabled_prohibit_rule_blocks(db, world, tool_calls):
    _rule(db, "br-1", "PROHIBIT: order cancellation after shipment")
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res.get("denied") is True
    assert res.get("blocked") == "business_rule"
    d = _decision(res)
    assert d["decision"] == "block"
    assert d["violated_rule_ids"] == ["br-1"]
    assert "br-1" in d["applicable_rule_ids"]
    assert tool_calls == []  # no side effects


# 2. Enabled rule, compliant action → executes ---------------------------------------

def test_enabled_informational_rule_allows(db, world, tool_calls):
    _rule(db, "br-2", "Always log cancellations to the finance ledger")
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res["success"] is True
    assert len(tool_calls) == 1
    d = _decision(res)
    assert d["decision"] == "allow"
    assert d["applicable_rule_ids"] == ["br-2"]  # traceable, not blocking
    assert d["violated_rule_ids"] == []


# 3-5. Proposed / verified-not-enabled / rejected NEVER enforce ----------------------

@pytest.mark.parametrize("status", ["proposed", "verified", "rejected"])
def test_non_enabled_rules_never_block(db, world, tool_calls, status):
    _rule(db, f"br-{status}", "PROHIBIT: everything", status=status)
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res["success"] is True and len(tool_calls) == 1
    assert _decision(res)["decision"] == "allow"


# 6. Cross-org rules are never retrieved ----------------------------------------------

def test_cross_org_rules_never_enforce(db, world, tool_calls):
    _rule(db, "br-evil", "PROHIBIT: everything", org=ORG2)
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res["success"] is True
    assert applicable_action_rules(db, ORG, db.get(OntologyAction, "act-r")) == []


# 7. Datasource-scoped rules cannot affect unrelated datasources ----------------------

def test_datasource_scoped_rules(db, world, tool_calls):
    _rule(db, "br-ds", "PROHIBIT: price edits on the BI mirror", ds=["kb-9"])
    ctx = dict(CONSOLE, datasource_id="kb-1")
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=ctx)
    assert res["success"] is True and len(tool_calls) == 1
    # the same rule DOES bind when the datasource matches
    ctx9 = dict(CONSOLE, datasource_id="kb-9")
    res9 = execute_action(db, ORG, "u-1", "act-r", params={}, context=ctx9)
    assert res9.get("denied") is True
    assert _decision(res9)["violated_rule_ids"] == ["br-ds"]


# 8. REQUIRE_APPROVAL rules demand the existing human workflow ------------------------

def test_require_approval_rule_gates_read_action(db, world, tool_calls):
    _rule(db, "br-app", "REQUIRE_APPROVAL: any order export to third parties")
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res.get("blocked") == "confirmation_required"
    assert res.get("approval_id")  # existing approval workflow used
    d = _decision(res)
    assert d["decision"] == "require_approval"
    assert d["approval_required"] is True
    assert tool_calls == []  # gated before side effects
    assert db.query(ApprovalRequest).count() == 1


# 9. Existing human approval remains authoritative -------------------------------------

def _approved_request(db, action):
    ap = ApprovalService(db).create_request(
        action_type="ontology_action",
        action_description=f"approve {action.name}",
        risk_tier="high",
        context_json={"action_id": action.id, "action_name": action.name,
                      "tool_name": action.tool_name,
                      "object_id": action.object_id, "params": {}},
        org_id=ORG)
    ap.status = "approved"
    db.commit()
    return ap


def test_prohibit_rule_vetoes_even_approved_writes(db, world, tool_calls):
    _rule(db, "br-veto", "PROHIBIT: manual order deletion")
    _approved_request(db, db.get(OntologyAction, "act-w"))
    ap = db.query(ApprovalRequest).one()
    res = execute_approved_action(db, ORG, "u-1", "act-w", ap.id,
                                  params={}, context=dict(CONSOLE))
    assert res.get("denied") is True
    assert _decision(res)["decision"] == "block"
    assert tool_calls == []  # approval does NOT override a prohibition


def test_require_approval_rule_satisfied_by_valid_approval(db, world, tool_calls):
    _rule(db, "br-app", "REQUIRE_APPROVAL: exports")
    _approved_request(db, db.get(OntologyAction, "act-w"))
    ap = db.query(ApprovalRequest).one()
    res = execute_approved_action(db, ORG, "u-1", "act-w", ap.id,
                                  params={}, context=dict(CONSOLE))
    assert res["success"] is True and len(tool_calls) == 1


# 10. Blocked writes create no side effects (no approval request either) ---------------

def test_blocked_write_creates_no_approval(db, world, tool_calls):
    _rule(db, "br-1", "PROHIBIT: cancellations")
    res = execute_action(db, ORG, "u-1", "act-w", params={}, context=dict(CONSOLE))
    assert res.get("denied") is True
    assert db.query(ApprovalRequest).count() == 0  # checked BEFORE approval
    assert tool_calls == []


# 11. Audit preserves decision + rule ids ----------------------------------------------

def test_audit_records_decision_and_rule_ids(db, world, tool_calls):
    _rule(db, "br-1", "PROHIBIT: cancellations")
    execute_action(db, ORG, "u-1", "act-w", params={}, context=dict(CONSOLE))
    ev = db.query(AuditEvent).filter(
        AuditEvent.event_type == "business_rule_check").one()
    assert ev.org_id == ORG
    assert ev.detail_json["decision"] == "block"
    assert ev.detail_json["violated_rule_ids"] == ["br-1"]
    assert "br-1" in ev.detail_json["applicable_rule_ids"]


# 12. Actions without applicable rules remain fully compatible -------------------------

def test_no_rules_full_compatibility(db, world, tool_calls):
    res = execute_action(db, ORG, "u-1", "act-r", params={}, context=dict(CONSOLE))
    assert res["success"] is True and len(tool_calls) == 1
    d = _decision(res)
    assert d["decision"] == "allow"
    assert d["applicable_rule_ids"] == []
    assert d["violated_rule_ids"] == []
    assert d["approval_required"] is False
    assert d["evaluation_status"] == "deterministic"


# 13. Unit: marker protocol + scope matching on evaluate_action_rules ------------------

def test_evaluate_action_rules_marker_protocol(db, world):
    _rule(db, "br-p", "PROHIBIT: x")
    _rule(db, "br-a", "approval_required: y")
    _rule(db, "br-i", "informational note without marker")
    _rule(db, "br-obj", "PROHIBIT: object-scoped", scope="object",
          scope_ref_id="oo-1")
    _rule(db, "br-obj2", "PROHIBIT: other object", scope="object",
          scope_ref_id="oo-2")
    _rule(db, "br-dom", "PROHIBIT: domain-scoped", scope="domain",
          scope_ref_id="dom-1")
    act = db.get(OntologyAction, "act-r")
    d = evaluate_action_rules(db, ORG, act)
    assert d["applicable_rule_ids"] == ["br-p", "br-a", "br-i",
                                        "br-obj", "br-dom"]
    assert d["decision"] == "block"
    assert d["violated_rule_ids"] == ["br-p", "br-obj", "br-dom"]
    # approval_granted satisfies approval rules; prohibit still vetoes
    d2 = evaluate_action_rules(db, ORG, act, approval_granted=True)
    assert d2["decision"] == "block"
    db.query(BusinessRule).filter(BusinessRule.id == "br-p").delete()
    db.query(BusinessRule).filter(BusinessRule.id == "br-obj").delete()
    db.query(BusinessRule).filter(BusinessRule.id == "br-dom").delete()
    db.commit()
    d3 = evaluate_action_rules(db, ORG, act)
    assert d3["decision"] == "require_approval"
    assert d3["approval_required"] is True
    d4 = evaluate_action_rules(db, ORG, act, approval_granted=True)
    assert d4["decision"] == "allow"
    assert d4["approval_required"] is False
