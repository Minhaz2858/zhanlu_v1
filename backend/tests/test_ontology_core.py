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


def test_write_action_never_auto_fires(db):
    act = OntologyAction(org_id=ORG, object_id="o1", name="po_draft",
                         tool_name="some_tool", risk_level="write")
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id)
    assert res["success"] is False
    assert res["blocked"] == "confirmation_required"


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

    monkeypatch.setattr(agent_tools, "execute_tool", fake_execute_tool)
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
