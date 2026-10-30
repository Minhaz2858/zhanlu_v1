"""P1-batch tests — fan-out (P1-3), domain value rollups (P1-4),
TrustPanel (P1-5), eval-gated publish + template export (P1-6),
external API key gate (P2-1), media property wrapping (P2-3)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import (
    AgentDomainBinding,
    OntologyLink,
    OntologyObject,
)
import app.models.ontology_revision  # noqa: F401 — register in metadata
# (fanout's notify_object_change writes ontology_revisions; without this
# import create_all misses the table — ordering-dependent latent bug)
from app.services.ontology.fanout import notify_object_change, plan_fanout
from app.services.ontology.queries import ensure_domains, get_object_by_name
from app.services.trust_panel import build_trust_panel, trust_panel_for_message


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


@pytest.fixture()
def linked_org(db):
    """Seeded org where Supplier --supplies--> PurchaseOrder, with a
    procurement-bound agent."""
    ensure_domains(db, ORG)
    supplier = get_object_by_name(db, ORG, "Supplier")
    po = get_object_by_name(db, ORG, "PurchaseOrder")
    db.add(OntologyLink(
        org_id=ORG, from_object_id=supplier.id, to_object_id=po.id,
        link_type="supplies", cardinality="1:N",
    ))
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name="procurement-agent", domain_id=po.domain_id,
        permission_scope="write",
    ))
    db.commit()
    return supplier, po


# ── P1-3: ontology-link fan-out ──────────────────────────────────────────

def test_plan_fanout_traverses_links_both_ways(db, linked_org):
    supplier, po = linked_org
    plan = plan_fanout(db, ORG, "Supplier")
    assert plan["success"] and plan["links_traversed"] >= 1
    assert "procurement-agent" in plan["agents"]

    # reverse traversal: change to PurchaseOrder reaches the same agent
    plan_rev = plan_fanout(db, ORG, "PurchaseOrder")
    assert plan_rev["success"]
    assert "procurement-agent" in plan_rev["agents"]


def test_plan_fanout_without_links_is_honest(db, linked_org):
    ensure_domains(db, "org-empty")
    plan = plan_fanout(db, "org-empty", "Supplier")
    assert plan["success"] and plan["agents"] == []


def test_plan_fanout_unknown_object(db):
    plan = plan_fanout(db, ORG, "Nope")
    assert not plan["success"]


def test_notify_object_change_writes_audit_events(db, linked_org):
    from app.models.audit_event import AuditEvent

    supplier, _ = linked_org
    out = notify_object_change(
        db, ORG, "Supplier", "port closure delayed shipment", changed_by="u1",
    )
    assert "procurement-agent" in out["notified"]
    assert out["audit_logged"] >= 1
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.org_id == ORG,
                AuditEvent.event_type == "ontology_fanout")
        .all()
    )
    assert len(rows) >= 1
    assert rows[0].detail_json["source_object"] == "Supplier"


# ── P1-4: domain value rollups ───────────────────────────────────────────

def test_domain_value_rollups_shape(db, linked_org):
    from app.services.ontology.domain_value import domain_value_rollups

    out = domain_value_rollups(db, ORG, days=30)
    assert out["window_days"] == 30
    codes = {r["domain_code"] for r in out["domains"]}
    assert "procurement" in codes
    row = next(r for r in out["domains"] if r["domain_code"] == "procurement")
    assert row["agents"] == ["procurement-agent"]
    assert row["est_value"] >= 0


# ── P1-5: TrustPanel ─────────────────────────────────────────────────────

def test_trust_panel_grades_are_honest():
    assert build_trust_panel(sources=[], confidence=None)["grade"] == "unsourced"
    panel = build_trust_panel(
        sources=[{"source_name": "erp_t_bd_supplier", "rows": 14}],
        confidence=0.87,
    )
    assert panel["grade"] == "sourced"
    assert panel["citations"][0]["source_name"] == "erp_t_bd_supplier"
    assert panel["confidence"] == 0.87
    verified = build_trust_panel(
        sources=[{"source_name": "t"}], claims_verified=True)
    assert verified["grade"] == "verified"


def test_trust_panel_from_message_and_never_raises():
    msg = {"sources": ["kb:1:erp_t_bd_supplier"],
           "confidence_score": 0.9}
    panel = trust_panel_for_message(msg)
    assert panel["citations"][0]["source_name"] == "kb:1:erp_t_bd_supplier"
    # hostile input must not break the contract
    bad = trust_panel_for_message({"sources": [{"nonsense": 1}, None, 42]})
    assert bad["grade"] in ("unsourced", "sourced")


# ── P1-6: eval-gated publish + template export ───────────────────────────

def _mk_agent(db, name="agent-gate"):
    from app.models.agent_app import AgentApp

    agent = AgentApp(
        id="ag-1", org_id=ORG, app_id="local-zhanlu-app",
        name=name, description="", agent_type="custom",
    )
    db.add(agent)
    db.commit()
    return agent


def test_publish_gate_blocks_failing_suite(db, monkeypatch):
    from app.models.agent_test_case import AgentTestCase
    from app.services import agent_lifecycle

    monkeypatch.setattr(
        "app.config.settings.EVAL_PROMOTE_GATE_ENABLED", True, raising=False)
    agent = _mk_agent(db)
    db.add(AgentTestCase(
        org_id=ORG, agent_app_id=agent.id, name="c1", test_type="golden",
        expected_behavior="x",
        last_result="fail", run_count=10, pass_count=5,
    ))
    db.commit()
    with pytest.raises(agent_lifecycle.EvalGateBlocked) as ei:
        agent_lifecycle.publish_agent(db, agent.id)
    assert ei.value.detail["blocked"] == "eval_gate"
    # force overrides — audited in the revision note
    out = agent_lifecycle.publish_agent(db, agent.id, force=True)
    assert out["status"] == "published"


def test_publish_gate_passing_suite_publishes(db, monkeypatch):
    from app.models.agent_test_case import AgentTestCase
    from app.services import agent_lifecycle

    monkeypatch.setattr(
        "app.config.settings.EVAL_PROMOTE_GATE_ENABLED", True, raising=False)
    agent = _mk_agent(db)
    db.add(AgentTestCase(
        org_id=ORG, agent_app_id=agent.id, name="c1", test_type="golden",
        expected_behavior="x",
        last_result="pass", run_count=10, pass_count=10,
    ))
    db.commit()
    out = agent_lifecycle.publish_agent(db, agent.id)
    assert out["status"] == "published"


def test_publish_no_suite_is_not_blocked(db, monkeypatch):
    from app.services import agent_lifecycle

    monkeypatch.setattr(
        "app.config.settings.EVAL_PROMOTE_GATE_ENABLED", True, raising=False)
    agent = _mk_agent(db)
    assert agent_lifecycle.eval_gate_status(db, agent.id) is None
    assert agent_lifecycle.publish_agent(db, agent.id)["status"] == "published"


def test_export_template_bundle(db, linked_org):
    from app.services.ontology.template_export import export_org_template

    bundle = export_org_template(db, ORG, ["procurement"])
    assert bundle["template_version"]
    codes = [d["code"] for d in bundle["domains"]]
    assert codes == ["procurement"]
    proc = bundle["domains"][0]
    names = {o["name"] for o in proc["objects"]}
    assert "Supplier" in names
    assert bundle["agent_bindings"].get("procurement-agent")


# ── P2-1: external API key gate ──────────────────────────────────────────

def test_external_api_key_gate(monkeypatch):
    from app.routers.ontology_external import _require_api_key
    from fastapi import HTTPException

    monkeypatch.setattr("app.config.settings.ONTOLOGY_API_KEY", "s3cret",
                        raising=False)
    _require_api_key(x_api_key="s3cret")  # passes
    with pytest.raises(HTTPException) as e1:
        _require_api_key(x_api_key="wrong")
    assert e1.value.status_code == 401
    monkeypatch.setattr("app.config.settings.ONTOLOGY_API_KEY", "",
                        raising=False)
    with pytest.raises(HTTPException) as e2:
        _require_api_key(x_api_key="s3cret")  # disabled surface
    assert e2.value.status_code == 401


# ── P2-3: media property wrapping ────────────────────────────────────────

def test_media_property_wrapping(db):
    from app.services.ontology.data import _map_rows_to_properties

    obj = OntologyObject(
        id="o1", org_id=ORG, domain_id="d1", name="Doc",
        backing_kind="table", backing_ref="docs",
        properties_json=[
            {"name": "photo", "type": "image", "column": "IMG_URL"},
            {"name": "title", "type": "string", "column": "TITLE"},
        ],
    )
    rows = [{"IMG_URL": "https://x/y.png", "TITLE": "t",
             "EXTRA": "kept"}]
    out = _map_rows_to_properties(rows, obj)
    assert out[0]["photo"] == {"media_url": "https://x/y.png",
                               "media_type": "image"}
    assert out[0]["title"] == "t"
    # non-URL value stays raw (honest, never wrapped into a lie)
    raw = _map_rows_to_properties([{"IMG_URL": "not-a-url", "TITLE": "t"}], obj)
    assert raw[0]["photo"] == "not-a-url"
