"""P5-T2 — ontology metric backing through the ONE governed path.

compute_metric resolves a MetricDefinition and computes it DETERMINISTICALLY
through QueryService (whose execution boundary runs execution_gate.enforce
unconditionally — no second SQL pathway, invariant I1). read_object_data's
metric branch and the shared agent tool both consume the same function, so
every caller gets identical semantics (acceptance criterion).

Unavailability is TYPED — {"success": False, "unavailable": True, ...} —
never a fabricated number (invariant I3).
"""

from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import IdentityMatch
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.ontology import (
    AgentDomainBinding,
    OntologyDomain,
    OntologyObject,
)
import app.models.ontology_revision  # noqa: F401 — register in metadata
from app.models.ontology_revision import OntologyRevision

ORG = "org-mb"
OTHER = "org-other"


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
                OntologyRevision.__table__, IdentityMatch.__table__,
                AgentDomainBinding.__table__],
    )
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture()
def seeded(db, tmp_path, monkeypatch):
    """A real SQLite datasource with sales rows + a published metric."""
    db_path = tmp_path / "sales.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sales_order (famount REAL, fdocstatus TEXT)")
    conn.executemany("INSERT INTO sales_order VALUES (?, ?)",
                     [(100.0, "C"), (50.0, "C"), (30.0, "A")])
    conn.commit()
    conn.close()

    kb = KnowledgeBase(org_id=ORG, name="sales-db", source_kind="database",
                       db_type="sqlite", api_url=str(db_path))
    kb.id = "kb-mb-1"
    db.add(kb)
    db.flush()
    metric = MetricDefinition(
        id="met-gross-1", org_id=ORG, datasource_id=kb.id,
        name="Gross Revenue", display_name="Gross Revenue",
        description="Sum of confirmed order amounts",
        base_table="sales_order", base_column="famount",
        aggregation="sum", unit="CNY", synonyms=["revenue"],
        is_enabled=True, lifecycle="published", reviewed_by="admin-1",
        version=1, evidence_status="verified",
        filters_json=[{"column": "fdocstatus", "op": "eq", "value": "C"}],
    )
    db.add(metric)
    domain = OntologyDomain(org_id=ORG, code="sales", name="Sales")
    db.add(domain)
    db.flush()
    obj = OntologyObject(org_id=ORG, domain_id=domain.id, name="GrossRevenue",
                         backing_kind="metric", backing_ref=metric.id,
                         evidence_status="verified")
    db.add(obj)
    db.commit()
    return SimpleNamespace(kb=kb, metric=metric, obj=obj, domain=domain,
                           db_path=db_path)


# ── the governed computation ──────────────────────────────────────────────

def test_compute_metric_happy_path(db, seeded):
    from app.services.ontology.metrics import compute_metric

    out = compute_metric(db, ORG, "Gross Revenue")
    assert out["success"] is True
    assert out["value"] == pytest.approx(150.0)  # filters: fdocstatus = C
    assert out["metric"]["id"] == "met-gross-1"
    assert out["metric"]["version"] == 1
    assert out["metric"]["aggregation"] == "sum"
    assert out["metric"]["unit"] == "CNY"
    assert out["evidence_status"] == "verified"
    assert out["provenance"]["base_table"] == "sales_order"
    assert out["provenance"]["base_column"] == "famount"
    assert out["execution_trace"]["gate"] == "passed"
    assert "SELECT" in out["execution_trace"]["sql"].upper()


def test_metric_and_ontology_read_identical_semantics(db, seeded):
    """THE P5 acceptance invariant: the ontology object view and the
    registry compute return the SAME number from the SAME definition."""
    from app.services.ontology.data import read_object_data
    from app.services.ontology.metrics import compute_metric

    via_registry = compute_metric(db, ORG, seeded.metric.id)
    via_ontology = read_object_data(db, ORG, seeded.obj)
    assert via_ontology["success"] is True
    assert via_registry["value"] == via_ontology["value"] == pytest.approx(150.0)
    assert (via_registry["metric"]["id"] == via_ontology["metric"]["id"]
            == seeded.metric.id)


def test_metric_resolved_by_synonym_case_insensitive(db, seeded):
    from app.services.ontology.metrics import compute_metric

    out = compute_metric(db, ORG, "revenue")
    assert out["success"] is True and out["value"] == pytest.approx(150.0)


def test_caller_filters_narrow_deterministically(db, seeded):
    """Caller filters CONJOIN with the stored basis (fdocstatus=C) —
    narrowing by amount 50 yields exactly that row's sum."""
    from app.services.ontology.metrics import compute_metric

    out = compute_metric(db, ORG, "Gross Revenue", filters={"famount": 50.0})
    assert out["success"] is True
    assert out["value"] == pytest.approx(50.0)


# ── typed unavailable — never a fabricated number ─────────────────────────

def _assert_unavailable(out: dict, needle: str):
    assert out["success"] is False
    assert out.get("unavailable") is True
    assert out.get("evidence_status") == "unavailable"
    assert "value" not in out
    assert needle.lower() in str(out.get("reason", "")).lower()


def test_unknown_metric_typed_unavailable(db, seeded):
    from app.services.ontology.metrics import compute_metric

    _assert_unavailable(compute_metric(db, ORG, "no such metric"), "no such metric")


def test_disabled_and_retired_typed_unavailable(db, seeded):
    from app.services.ontology.metrics import compute_metric

    seeded.metric.is_enabled = False
    db.commit()
    _assert_unavailable(compute_metric(db, ORG, "Gross Revenue"), "disabled")
    seeded.metric.is_enabled = True
    seeded.metric.lifecycle = "retired"
    db.commit()
    _assert_unavailable(compute_metric(db, ORG, "Gross Revenue"), "retired")


def test_cross_org_metric_typed_unavailable(db, seeded):
    from app.services.ontology.metrics import compute_metric

    _assert_unavailable(compute_metric(db, OTHER, "Gross Revenue"), "not found")


def test_execution_failure_typed_unavailable(db, seeded):
    """A metric whose base table vanished must NEVER fabricate — the gate /
    executor failure surfaces as typed unavailable."""
    from app.services.ontology.metrics import compute_metric

    seeded.metric.base_table = "table_that_vanished"
    db.commit()
    out = compute_metric(db, ORG, "Gross Revenue")
    _assert_unavailable(out, "")


def test_custom_aggregation_honestly_refused(db, seeded):
    """'custom' has no deterministic executable definition — refused, not
    guessed (invariant I1/I2)."""
    from app.services.ontology.metrics import compute_metric

    seeded.metric.aggregation = "custom"
    db.commit()
    out = compute_metric(db, ORG, "Gross Revenue")
    _assert_unavailable(out, "custom")
    assert "deterministic" in out["reason"]


def test_invalid_aggregation_typed_unavailable(db, seeded):
    from app.services.ontology.metrics import compute_metric

    seeded.metric.aggregation = "median_of_moons"
    db.commit()
    out = compute_metric(db, ORG, "Gross Revenue")
    _assert_unavailable(out, "aggregation")


def test_in_filter_expands_and_computes(db, seeded):
    """P5 review F1: 'in' must actually compute (expanded placeholders),
    not produce a permanent refusal."""
    from app.services.ontology.metrics import compute_metric

    seeded.metric.filters_json = [
        {"column": "fdocstatus", "op": "in", "value": ["C", "A"]}]
    db.commit()
    out = compute_metric(db, ORG, "Gross Revenue")
    assert out["success"] is True
    assert out["value"] == pytest.approx(180.0)


def test_gate_refusal_is_typed_unavailable(db, seeded, monkeypatch):
    """P5 review F7: the ExecutionValidationError branch surfaces gate
    violations, never a number."""
    import app.services.db.query_service as qs_mod
    from app.services.db.execution_gate import ExecutionValidationError
    from app.services.ontology.metrics import compute_metric

    def _refuse(self, kb_id, sql, **kw):
        raise ExecutionValidationError(
            {"errors": ["metric binding violation (test)"], "detail": {}})

    monkeypatch.setattr(qs_mod.QueryService, "execute", _refuse)
    out = compute_metric(db, ORG, "Gross Revenue")
    _assert_unavailable(out, "gate")
    assert out.get("gate_violations")


# ── the bind trap: no more permanent-refusal bindings ─────────────────────

def test_bind_rejects_metric_ref_to_unknown_or_unpublished_definition(db, seeded):
    from app.deps import get_current_user_required, get_db, require_org_admin
    from app.routers.ontology import router as ont_router
    from fastapi.testclient import TestClient

    admin = SimpleNamespace(org_id=ORG, email="admin@x.io", id="u-admin")
    app = FastAPI()
    app.include_router(ont_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_org_admin] = lambda: admin
    app.dependency_overrides[get_current_user_required] = lambda: admin
    c = TestClient(app)

    r = c.patch(f"/api/ontology/objects/{seeded.obj.id}/bind",
                json={"backing_kind": "metric", "backing_ref": "met-nope"})
    assert r.status_code == 400

    seeded.metric.lifecycle = "draft"
    db.commit()
    r2 = c.patch(f"/api/ontology/objects/{seeded.obj.id}/bind",
                 json={"backing_kind": "metric", "backing_ref": seeded.metric.id})
    assert r2.status_code == 400

    seeded.metric.lifecycle = "published"
    db.commit()
    r3 = c.patch(f"/api/ontology/objects/{seeded.obj.id}/bind",
                 json={"backing_kind": "metric", "backing_ref": seeded.metric.id})
    assert r3.status_code == 200


# ── P5-T3: the shared agent tool ──────────────────────────────────────────

def test_compute_metric_tool_governed_result(db, seeded):
    import asyncio

    from app.services.tool_handlers.ontology_tools import _compute_metric

    out = asyncio.run(_compute_metric(
        {"metric": "Gross Revenue"}, db, None, {"org_id": ORG}))
    assert out["success"] is True
    assert out["value"] == pytest.approx(150.0)
    assert out["metric"]["id"] == seeded.metric.id
    assert out["execution_trace"]["gate"] == "passed"


def test_compute_metric_tool_binding_wall(db, seeded):
    """A metric tied to ontology objects requires the calling agent to hold
    a domain binding — unbound agents get typed unavailable, never a
    number (P0 discipline)."""
    import asyncio

    from app.models.ontology import AgentDomainBinding
    from app.services.tool_handlers.ontology_tools import _compute_metric

    seeded.metric.source_object_refs = [seeded.obj.id]
    db.commit()

    unbound = asyncio.run(_compute_metric(
        {"metric": "Gross Revenue"}, db, None,
        {"org_id": ORG, "agent_name": "some-agent"}))
    assert unbound["success"] is False and unbound["unavailable"] is True
    assert "bound" in unbound["reason"]

    db.add(AgentDomainBinding(org_id=ORG, agent_name="some-agent",
                              domain_id=seeded.domain.id,
                              permission_scope="read"))
    db.commit()
    ok = asyncio.run(_compute_metric(
        {"metric": "Gross Revenue"}, db, None,
        {"org_id": ORG, "agent_name": "some-agent"}))
    assert ok["success"] is True and ok["value"] == pytest.approx(150.0)


def test_bind_metric_stamps_p2_revision(db, seeded):
    from app.deps import get_current_user_required, get_db, require_org_admin
    from app.routers.ontology import router as ont_router
    from fastapi.testclient import TestClient
    import app.models.ontology_revision  # noqa: F401

    admin = SimpleNamespace(org_id=ORG, email="admin@x.io", id="u-admin")
    app = FastAPI()
    app.include_router(ont_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_org_admin] = lambda: admin
    app.dependency_overrides[get_current_user_required] = lambda: admin
    c = TestClient(app)

    r = c.patch(f"/api/ontology/objects/{seeded.obj.id}/bind",
                json={"backing_kind": "metric", "backing_ref": seeded.metric.id})
    assert r.status_code == 200
    from app.services.ontology.revisions import list_revisions

    revs = list_revisions(db, ORG, "object", seeded.obj.id)
    assert revs and revs[0].change_kind == "bind"
