"""Ontology core tests — P0-2 (Enterprise AI OS strategy).

Covers: idempotent domain seeding, honest grounded reads (unbound → gap,
bound → rows, identifiers validated), risk-tiered action execution
(write NEVER auto-fires), honest coverage scoring, and the agent-facing
query_ontology tool.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyAction, OntologyObject
from app.services.ontology.coverage import compute_coverage
from app.services.ontology.data import execute_action, read_object_data
from app.services.ontology.queries import (
    ensure_domains,
    get_object_by_name,
    list_actions,
    list_domains,
    list_objects,
)


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


ORG = "org-test"


# ── Seeding ──────────────────────────────────────────────────────────────


def test_seeding_creates_14_domains_with_contents(db):
    res = ensure_domains(db, ORG)
    assert res["created"] == 14
    assert len(list_domains(db, ORG)) == 14
    # Starter objects + actions exist and are honestly unwired
    objs = list_objects(db, ORG)
    assert len(objs) >= 30
    assert all(o.backing_kind == "manual" for o in objs)
    acts = list_actions(db, ORG)
    assert len(acts) >= 25


def test_seeding_is_idempotent(db):
    ensure_domains(db, ORG)
    res = ensure_domains(db, ORG)
    assert res["created"] == 0
    assert len(list_domains(db, ORG)) == 14


def test_domains_are_org_scoped(db):
    ensure_domains(db, ORG)
    ensure_domains(db, "org-other")
    assert len(list_domains(db, ORG)) == 14
    assert len(list_domains(db, "org-other")) == 14


# ── Grounded reads ───────────────────────────────────────────────────────


def test_read_unbound_object_is_honest_gap(db):
    ensure_domains(db, ORG)
    obj = get_object_by_name(db, ORG, "Product")
    res = read_object_data(db, ORG, obj, limit=5)
    assert res["success"] is False
    assert "no live table binding" in res["reason"]


def test_read_bound_table_returns_rows(db):
    from sqlalchemy import text

    db.execute(text(
        "CREATE TABLE products (product TEXT, qty REAL)"
    ))
    db.execute(text(
        "INSERT INTO products VALUES ('双环戊二烯', 96.0), ('混三甲苯', 1000.0)"
    ))
    obj = OntologyObject(
        org_id=ORG, domain_id="d1", name="Product",
        backing_kind="table", backing_ref="products",
        properties_json=[{"name": "product", "column": "product"},
                         {"name": "qty", "column": "qty"}],
    )
    db.add(obj)
    db.commit()

    res = read_object_data(db, ORG, obj, limit=10)
    assert res["success"] is True
    assert res["row_count"] == 2
    assert res["rows"][0]["product"] == "双环戊二烯"


def test_read_rejects_injected_identifiers(db):
    obj = OntologyObject(
        org_id=ORG, domain_id="d1", name="Evil",
        backing_kind="table", backing_ref="products; DROP TABLE x",
    )
    db.add(obj)
    db.commit()
    res = read_object_data(db, ORG, obj)
    assert res["success"] is False
    assert "invalid backing table" in res["reason"]


def test_read_kb_route_requires_org_ownership(db):
    obj = OntologyObject(
        org_id=ORG, domain_id="d1", name="Product",
        backing_kind="table", backing_ref="kb:some-kb-id:some_table",
    )
    db.add(obj)
    db.commit()
    res = read_object_data(db, ORG, obj)
    # KB missing entirely → not found in this org (no cross-tenant leak)
    assert res["success"] is False
    assert "not found in this org" in res["reason"]


# ── Action risk contract ─────────────────────────────────────────────────


def test_action_without_tool_is_a_proposal(db):
    act = OntologyAction(org_id=ORG, object_id="o1", name="po_draft",
                         risk_level="write")
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id)
    assert res["success"] is False
    assert "no bound tool" in res["reason"]


def test_write_action_creates_governance_approval(db):
    """P0-3: a write action NEVER auto-fires — it creates a pending
    ApprovalRequest; the params ride in the approval's context."""
    from app.models.governance import ApprovalRequest

    act = OntologyAction(org_id=ORG, object_id="o1", name="po_draft",
                         tool_name="some_tool", risk_level="write")
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id, params={"qty": 5})
    assert res["success"] is False
    assert res["blocked"] == "confirmation_required"
    approval = db.query(ApprovalRequest).filter(
        ApprovalRequest.id == res["approval_id"]).one()
    assert approval.status == "pending"
    assert approval.context_json["action_id"] == act.id
    assert approval.context_json["tool_name"] == "some_tool"
    assert approval.context_json["params"]["qty"] == 5


def test_execute_approved_action_full_contract(db, monkeypatch):
    """P0-3: approve → execute-approved runs the tool ONCE, bound to the
    approval; wrong-action and unapproved approvals are refused."""
    from app.models.governance import ApprovalRequest
    import app.services.agent_tools as agent_tools

    act = OntologyAction(org_id=ORG, object_id="o1", name="po_draft",
                         tool_name="po_tool", risk_level="write")
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id)
    approval_id = res["approval_id"]

    calls = []

    def fake_execute_tool(name, args, session, user_id, context=None):
        calls.append((name, context.get("approval_id")))
        return {"success": True, "draft_id": "d1"}

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))

    from app.services.governance.approval_service import ApprovalService
    from app.services.ontology.data import execute_approved_action

    # Not yet approved → refused
    res = execute_approved_action(db, ORG, "u1", act.id, approval_id)
    assert res["success"] is False and res["blocked"] == "confirmation_required"

    ApprovalService(db).approve(approval_id, reviewed_by="boss")

    # Mismatched action → refused (an approval is proof for ONE write)
    other = OntologyAction(org_id=ORG, object_id="o1", name="other",
                           tool_name="po_tool", risk_level="write")
    db.add(other)
    db.commit()
    res = execute_approved_action(db, ORG, "u1", other.id, approval_id)
    assert res["success"] is False and "mismatch" in res["reason"]

    # The real one → runs, with the approval threaded into context
    res = execute_approved_action(db, ORG, "u1", act.id, approval_id)
    assert res["success"] is True
    assert res["approved_by"] == "boss"
    assert calls == [("po_tool", approval_id)]


def _async_fake(sync_fn):
    async def wrapper(*a, **kw):
        return sync_fn(*a, **kw)
    return wrapper


def test_po_draft_tool_direct_call_is_refused(db):
    """P0-4: the LLM can NOT create PO drafts directly — only through an
    approved ontology action (the approval id is the proof)."""
    import asyncio

    from app.services.tool_handlers.po_draft_tool import _direct_call_guard

    res = asyncio.run(_direct_call_guard(
        {"supplier_name": "X"}, db, "u1", context={}))

    assert res["success"] is False
    assert "governance approval" in res["reason"]

    # With an approval in context → the draft is born approved + bound
    res = asyncio.run(_direct_call_guard(
        {"supplier_name": "测试供应商", "quantity": 500,
         "est_unit_price": 8500},
        db, "u1", context={"org_id": ORG, "approval_id": "appr-1"},
    ))
    assert res["success"] is True
    assert res["status"] == "approved"
    assert res["est_amount"] == 4_250_000.0
    assert res["approval_id"] == "appr-1"


def test_read_action_runs_bound_tool(db, monkeypatch):
    act = OntologyAction(org_id=ORG, object_id="o1", name="stockout_alert",
                         tool_name="stock_tool", risk_level="read")
    db.add(act)
    db.commit()

    captured = {}

    def fake_execute_tool(name, args, session, user_id, context=None):
        captured["name"] = name
        captured["context"] = context
        return {"success": True, "rows": []}

    import app.services.agent_tools as agent_tools

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))
    res = execute_action(db, ORG, "u1", act.id, params={"x": 1})
    assert res["success"] is True
    assert captured["name"] == "stock_tool"
    assert captured["context"]["ontology_action_id"] == act.id


# ── Coverage honesty ─────────────────────────────────────────────────────


def test_coverage_counts_only_real_bindings(db):
    ensure_domains(db, ORG)
    domain = list_domains(db, ORG)[0]
    obj = list_objects(db, ORG, domain.id)[0]
    obj.backing_kind = "table"
    obj.backing_ref = "real_table"
    db.commit()

    cov = compute_coverage(db, ORG)
    row = [c for c in cov if c["domain_id"] == domain.id][0]
    assert row["objects_live"] == 1
    assert row["data_connected"] is True
    assert row["readiness"] == "wired"  # actions not executable yet
    # Untouched domains stay honestly empty
    others = [c for c in cov if c["domain_id"] != domain.id]
    assert all(c["objects_live"] == 0 for c in others)
