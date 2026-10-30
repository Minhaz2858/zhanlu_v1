"""P7-T1 — GET /api/ontology/graph aggregate payload.

One read-only call for the Ontology Explorer: domains + objects (with the
SAME per-viewer document filtering as /ontology/objects) + links + agent
bindings + governed metric definitions (all lifecycles — governance state
must be visible, never implied published).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.deps import get_current_user_required, get_db
from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.ontology import (
    AgentDomainBinding,
    OntologyDomain,
    OntologyLink,
    OntologyObject,
)

ORG = "org-graph"
OTHER = "org-graph-other"
ADMIN = SimpleNamespace(id="u-admin", org_id="default-org", role="admin")
MEMBER = SimpleNamespace(id="u-member", org_id=ORG, role="member")
OWNER = SimpleNamespace(id="u-owner", org_id=ORG, role="member")


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        eng,
        tables=[KnowledgeBase.__table__, MetricDefinition.__table__,
                OntologyDomain.__table__, OntologyObject.__table__,
                OntologyLink.__table__, AgentDomainBinding.__table__],
    )
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _domain(db, org=ORG, code="sales"):
    d = OntologyDomain(org_id=org, code=code, name=code.title())
    db.add(d)
    db.commit()
    return d


def _object(db, d, name, *, org=ORG, provenance=None):
    obj = OntologyObject(org_id=org, domain_id=d.id, name=name,
                         backing_kind="table",
                         backing_ref="kb:kb-x:t",
                         evidence_status="verified",
                         provenance_json=provenance)
    db.add(obj)
    db.commit()
    return obj


def _link(db, a, b, kind="relates_to"):
    l = OntologyLink(org_id=a.org_id, from_object_id=a.id,
                     to_object_id=b.id, link_type=kind)
    db.add(l)
    db.commit()
    return l


def _metric(db, org=ORG, name="GMV", lifecycle="published"):
    m = MetricDefinition(
        org_id=org, datasource_id="ds-1", name=name,
        lifecycle=lifecycle, evidence_status="verified",
        base_table="sales_order", aggregation="sum", unit="CNY")
    db.add(m)
    db.commit()
    return m


def _client(db_session, user):
    from app.routers.ontology import router as ontology_router

    app = FastAPI()
    app.include_router(ontology_router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def test_graph_happy_path_shape(db):
    d = _domain(db)
    o1 = _object(db, d, "Order")
    o2 = _object(db, d, "Customer")
    _link(db, o1, o2)
    db.add(AgentDomainBinding(org_id=ORG, agent_name="sales_analyst",
                              domain_id=d.id, permission_scope="read"))
    db.commit()
    _metric(db)
    _metric(db, name="Old GMV", lifecycle="retired")

    r = _client(db, MEMBER).get("/ontology/graph")
    assert r.status_code == 200
    body = r.json()
    assert body["org_id"] == ORG
    assert [dm["code"] for dm in body["domains"]] == ["sales"]
    assert body["domains"][0]["object_count"] == 2
    names = {o["name"] for o in body["objects"]}
    assert names == {"Order", "Customer"}
    assert all(o["evidence_status"] == "verified" for o in body["objects"])
    # P6 binding health rides the node payload (never validated here → None)
    assert all("binding_status" in o and "last_validated_at" in o
               for o in body["objects"])
    assert len(body["links"]) == 1
    assert body["links"][0]["link_type"] == "relates_to"
    assert body["bindings"][0]["agent_name"] == "sales_analyst"
    # governance state visible for ALL lifecycles (G4)
    lifecycle = {m["name"]: m["lifecycle"] for m in body["metrics"]}
    assert lifecycle == {"GMV": "published", "Old GMV": "retired"}
    m0 = body["metrics"][0]
    assert {"id", "name", "lifecycle", "version", "evidence_status",
            "reviewed_by", "base_table", "datasource_id", "unit",
            "aggregation"} <= set(m0.keys())


def test_graph_document_evidence_per_viewer(db):
    """The graph endpoint applies the SAME fail-closed per-viewer document
    filtering as /ontology/objects (G1)."""
    d = _domain(db)
    hidden = {"document_provenance": [{
        "source_kb_id": "kb-missing", "quote": "secret",
        "location": "p.1", "doc_version_id": "v1"}]}
    obj = _object(db, d, "Order", provenance=hidden)

    # missing KB → fail-closed: entry hidden
    body = _client(db, MEMBER).get("/ontology/graph").json()
    got = [o for o in body["objects"] if o["name"] == "Order"][0]
    assert not (got.get("provenance") or {}).get("document_provenance")

    # owner of an existing same-org KB → entry visible
    kb = KnowledgeBase(org_id=ORG, name="doc", source_kind="file",
                       created_by_id=OWNER.id)
    kb.id = "kb-missing"
    db.add(kb)
    db.commit()
    body = _client(db, OWNER).get("/ontology/graph").json()
    got = [o for o in body["objects"] if o["name"] == "Order"][0]
    dp = (got.get("provenance") or {}).get("document_provenance")
    assert dp and dp[0]["quote"] == "secret"
    assert obj.id  # silence linters


def test_graph_cross_org_isolation(db):
    d_other = _domain(db, org=OTHER, code="hr")
    _object(db, d_other, "Employee", org=OTHER)
    _metric(db, org=OTHER, name="Headcount")
    d = _domain(db)
    _object(db, d, "Order")
    _metric(db)  # our org's GMV

    body = _client(db, MEMBER).get("/ontology/graph").json()
    assert {o["name"] for o in body["objects"]} == {"Order"}
    assert [dm["code"] for dm in body["domains"]] == ["sales"]
    assert [m["name"] for m in body["metrics"]] == ["GMV"]


def test_graph_platform_admin_can_target_org(db):
    d = _domain(db)
    _object(db, d, "Order")
    r = _client(db, ADMIN).get("/ontology/graph",
                               params={"org_id": ORG})
    assert r.status_code == 200
    assert r.json()["org_id"] == ORG
    # member CANNOT target another org (404, same as /objects)
    r2 = _client(db, MEMBER).get("/ontology/graph",
                                 params={"org_id": OTHER})
    assert r2.status_code == 404


def test_graph_domain_filter_narrows_objects_and_links_only(db):
    d1 = _domain(db, code="sales")
    d2 = _domain(db, code="hr")
    o1 = _object(db, d1, "Order")
    o2 = _object(db, d1, "Customer")
    o3 = _object(db, d2, "Employee")
    _link(db, o1, o2)
    _link(db, o1, o3)

    body = _client(db, MEMBER).get(
        "/ontology/graph", params={"domain_id": d1.id}).json()
    assert {o["name"] for o in body["objects"]} == {"Order", "Customer"}
    # established list_links semantics: a link survives when EITHER
    # endpoint is in the domain — both links touch Order (d1) → both kept
    assert len(body["links"]) == 2
    # domains + metrics stay whole-org
    assert [dm["code"] for dm in body["domains"]] == ["sales", "hr"]
