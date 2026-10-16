"""P3 batch-2 tests — service KPIs (P3-7), segmentation (P3-14),
campaign planning (P3-13), template import (P1-6 counterpart)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.experience_entry import ExperienceEntry
from app.services.ontology.queries import ensure_domains, get_object_by_name

ORG = "org-b2"


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


# ── P3-7: service KPIs in domain value ───────────────────────────────────

def test_domain_value_includes_service_kpis(db):
    from app.models.agent_app import AgentApp
    from app.models.ontology import AgentDomainBinding
    from app.services.ontology.domain_value import domain_value_rollups

    ensure_domains(db, ORG)
    app = AgentApp(id="ag-x", org_id=ORG, app_id="local-zhanlu-app",
                   name="procurement-agent", agent_type="custom")
    db.add(app)
    proc = next(d for d in __import__("app.services.ontology.queries",
                                      fromlist=["list_domains"]).list_domains(db, ORG)
                if d.code == "procurement")
    db.add(AgentDomainBinding(org_id=ORG, agent_name="procurement-agent",
                              domain_id=proc.id, permission_scope="read"))
    db.add(ExperienceEntry(
        org_id=ORG, entry_type="user_feedback", agent_app_id="ag-x",
        outcome="positive", user_rating=5))
    db.add(ExperienceEntry(
        org_id=ORG, entry_type="user_feedback", agent_app_id="ag-x",
        outcome="negative", user_rating=2))
    db.commit()

    out = domain_value_rollups(db, ORG, days=30)
    row = next(r for r in out["domains"] if r["domain_code"] == "procurement")
    assert row["feedback_count"] == 2
    assert row["csat"] == 70.0  # (5+2)/2 = 3.5 / 5 → 70
    assert row["positive_ratio"] == 0.5


def test_domain_value_no_feedback_is_none(db):
    from app.services.ontology.domain_value import domain_value_rollups

    ensure_domains(db, "org-nofb")
    out = domain_value_rollups(db, "org-nofb", days=30)
    assert all(r["csat"] is None for r in out["domains"])


# ── P3-14: segmentation tool ─────────────────────────────────────────────

def test_segment_records_group_by(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    ensure_domains(db, ORG)
    obj = get_object_by_name(db, ORG, "Customer")
    obj.backing_kind = "table"
    obj.backing_ref = "kb:kb1:party_master"
    obj.properties_json = [{"name": "tier", "type": "string",
                            "column": "party_segment"}]
    db.commit()

    captured = {}

    def fake_execute(self, kb_id, sql, **kw):
        captured["kb_id"], captured["sql"] = kb_id, sql
        return {"rows": [{"segment": "enterprise", "n": 42},
                         {"segment": "mid-market", "n": 17}]}

    import app.services.db.query_service as qs
    monkeypatch.setattr(qs.QueryService, "execute", fake_execute)

    out = asyncio.run(ot._segment_records(
        {"object_name": "Customer", "segment_by": "tier"},
        db, None, {"org_id": ORG}))
    assert out["success"]
    assert out["total_counted"] == 59
    assert out["segments"][0]["segment"] == "enterprise"
    assert "GROUP BY party_segment" in captured["sql"]


def test_segment_refuses_unmapped_property(db):
    import app.services.tool_handlers.ontology_tools as ot

    ensure_domains(db, ORG)
    obj = get_object_by_name(db, ORG, "Customer")
    obj.backing_kind = "table"
    obj.backing_ref = "kb:kb1:party_master"
    obj.properties_json = [{"name": "tier", "type": "string"}]  # no column
    db.commit()
    out = asyncio.run(ot._segment_records(
        {"object_name": "Customer", "segment_by": "tier"},
        db, None, {"org_id": ORG}))
    assert not out["success"] and "bind endpoint" in out["reason"]


# ── P3-13: campaign planning ─────────────────────────────────────────────

def test_plan_campaign_returns_structured_plan(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    plan = {"theme": "Enterprise push", "channels": ["email", "LinkedIn"],
            "steps": [{"order": 1, "action": "seed list", "owner": "marketing"}],
            "kpis": [{"name": "SQLs", "target": "20/mo"}],
            "messages": [{"segment": "enterprise", "angle": "compliance"}]}
    import app.services.llm_service as llm
    monkeypatch.setattr(llm, "chat_completion_json_sync",
                        lambda *a, **kw: plan)

    out = asyncio.run(ot._plan_campaign(
        {"objective": "20 enterprise SQLs per month"},
        db, None, {"org_id": ORG}))
    assert out["success"] and out["plan"]["theme"] == "Enterprise push"
    assert "human-executed" in out["note"]


def test_plan_campaign_requires_objective():
    import app.services.tool_handlers.ontology_tools as ot

    out = asyncio.run(ot._plan_campaign({}, None, None, {"org_id": ORG}))
    assert not out["success"]


def test_campaign_and_segment_tools_registered():
    import app.services.tool_handlers  # noqa: F401
    from app.services.tool_registry import registry

    assert registry.get_entry("segment_records") is not None
    assert registry.get_entry("plan_campaign") is not None


# ── template import ──────────────────────────────────────────────────────

BUNDLE = {
    "domains": [{
        "code": "legal",
        "name": "Legal / Compliance Agents",
        "objects": [
            {"name": "Contract", "display_name": "Contract",
             "properties": [{"name": "counterparty", "type": "string"}]},
            {"name": "Obligation", "properties": [{"name": "due_date"}]},
        ],
    }],
    "links": [{"from": "Contract", "to": "Obligation",
               "link_type": "creates", "cardinality": "1:N"}],
    "agent_bindings": {"legal-agent": [
        {"domain": "legal", "permission_scope": "read"}]},
}


def test_import_creates_unbound_and_idempotent(db):
    from app.services.ontology.template_export import import_org_template

    out = import_org_template(db, ORG, BUNDLE)
    assert out["domains_created"] == 1
    assert out["objects_created"] == 2
    assert out["links_created"] == 1
    assert out["bindings_created"] == 1

    obj = get_object_by_name(db, ORG, "Contract")
    # HONESTY: imported objects are UNBOUND — never copy another org's DB refs
    assert obj.backing_kind == "manual" and obj.backing_ref is None

    # idempotent: second import changes nothing
    out2 = import_org_template(db, ORG, BUNDLE)
    assert out2["domains_created"] == 0 and out2["objects_created"] == 0
    assert out2["objects_skipped"] == 2 and out2["links_skipped"] == 1


def test_import_never_copies_backing_refs(db):
    from app.services.ontology.template_export import import_org_template

    bundle = {"domains": [{
        "code": "legal",
        "objects": [{"name": "Contract", "backing": {
            "kind": "table", "ref": "kb:OTHER-ORG:contracts"}}],
    }]}
    import_org_template(db, ORG, bundle)
    obj = get_object_by_name(db, ORG, "Contract")
    assert obj.backing_ref is None
