"""P0-P7 integrated acceptance (workstream 2) — the MANDATORY engineering gate.

Exercises the full chain on the shared synthetic sandbox: enterprise resources
→ ontology → governed metrics → authorized execution → verified evidence →
business answer → traceable visualization. One coherent flow, deterministic.

Run: python3 -m pytest tests/test_p0_p7_integrated.py -q
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from app.models.identity import IdentityMatch
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.ontology import OntologyObject
import app.models.ontology_revision  # noqa: F401
from app.services.db.schema_service import SchemaService
from app.services.ontology.doc_access import filter_document_entries
from app.services.ontology.metric_dependencies import (
    build_dependency_edges,
    validate_metric_dependencies,
)
from app.services.ontology.metrics import compute_metric
from app.services.ontology.serialize import object_to_dict
from app.routers.ontology import ontology_graph

from tests.sandbox_seed import (
    KB_DOC, KB_FILE, KB_SALES, ORG, OBJ_GROSS_MARGIN, OBJ_VEHICLE,
    METRIC_COST_DIRECT, METRIC_COST_INDIRECT, METRIC_GROSS_MARGIN,
    METRIC_REVENUE, METRIC_TOTAL_COST, OWNER_ID, RESTRICTED_ID,
    create_data_db, build_sandbox, make_engine, make_session,
)


def _user(uid, role="org_admin"):
    return SimpleNamespace(id=uid, org_id=ORG, role=role)


@pytest.fixture()
def env(tmp_path):
    eng = make_engine()
    data_path = str(tmp_path / "sales.db")
    create_data_db(data_path)
    session = make_session(eng)
    build_sandbox(session, data_path)
    return eng, session, data_path


# 1) Source discovery across heterogeneous sources (DB + file) --------------
def test_cap1_source_discovery_heterogeneous(env):
    eng, session, _ = env
    schema = SchemaService(session).describe_all(KB_SALES)
    names = {t.get("table") for t in schema.get("tables", [])}
    assert "sales_transactions" in names  # database source discovered live
    # second, heterogeneous (file) source present in the catalog
    file_kb = session.query(KnowledgeBase).filter(
        KnowledgeBase.id == KB_FILE).first()
    assert file_kb is not None and file_kb.source_kind == "file"


# 2) Ontology object + relationship resolution ------------------------------
def test_cap2_objects_and_relationships(env):
    eng, session, _ = env
    payload = ontology_graph(org_id=ORG, db=session, user=_user(OWNER_ID), domain_id=None)
    ids = {o["id"] for o in payload["objects"]}
    assert OBJ_VEHICLE in ids and OBJ_GROSS_MARGIN in ids
    assert len(payload["links"]) >= 3


# 3) Cross-source identity resolution ---------------------------------------
def test_cap3_cross_source_identity(env):
    eng, session, _ = env
    match = session.query(IdentityMatch).filter(
        IdentityMatch.object_id == OBJ_VEHICLE,
        IdentityMatch.status == "confirmed").first()
    assert match is not None
    assert match.pk_a == match.pk_b == "v-001"


# 4) Document-grounded evidence + citations --------------------------------
def test_cap4_document_evidence_citation(env):
    eng, session, _ = env
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == OBJ_GROSS_MARGIN).first()
    d = object_to_dict(obj, doc_ctx={"db": session, "user": _user(OWNER_ID)})
    prov = d["provenance"] or {}
    assert "document_provenance" in prov
    assert any(e.get("source_kb_id") == KB_DOC for e in prov["document_provenance"])


# 5) Document-level authorization inheritance -------------------------------
def test_cap5_document_authorization_inheritance(env):
    eng, session, _ = env
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == OBJ_GROSS_MARGIN).first()
    owner_sees = filter_document_entries(
        session, _user(OWNER_ID), obj.provenance_json)
    restricted_sees = filter_document_entries(
        session, _user(RESTRICTED_ID), obj.provenance_json)
    assert len(owner_sees.get("document_provenance", [])) == 1
    # fail-closed: a non-granted org member sees nothing
    assert restricted_sees.get("document_provenance", []) == []


# 6) Governed metric computation -------------------------------------------
def test_cap6_governed_metric_computation(env):
    eng, session, _ = env
    # Model X current month: margins (1000-600)=400 + (1000-650)=350 = 750
    res = compute_metric(
        session, ORG, METRIC_GROSS_MARGIN,
        filters={"model_id": "v-001", "sale_month": "2026-10"})
    assert res["success"] is True
    assert res["value"] == 750.0
    # single deterministic path — same result via the metric name
    res2 = compute_metric(
        session, ORG, "GrossMargin",
        filters={"model_id": "v-001", "sale_month": "2026-10"})
    assert res2["value"] == 750.0


# 7) Deterministic execution + provenance trace -----------------------------
def test_cap7_deterministic_provenance(env):
    eng, session, _ = env
    a = compute_metric(session, ORG, METRIC_GROSS_MARGIN,
                       filters={"model_id": "v-001", "sale_month": "2026-10"})
    b = compute_metric(session, ORG, METRIC_GROSS_MARGIN,
                       filters={"model_id": "v-001", "sale_month": "2026-10"})
    assert a["value"] == b["value"]
    assert a["execution_trace"]["gate"] == "passed"
    assert a["provenance"]["base_table"] == "sales_transactions"
    assert a["provenance"]["datasource_id"] == KB_SALES
    assert "sql" in a["execution_trace"]


# 8) Ontology evolution after a source change (sanity; full transitions=P6) --
def test_cap8_evolution_runs(env):
    eng, session, _ = env
    from app.services.ontology.evolution import reconcile
    report = reconcile(session, ORG, apply=False)
    assert "sources" in report
    assert any(s.get("source_key") == f"db:{KB_SALES}" for s in report["sources"])


# 9) Revision history + rollback (P5-F6 revalidation) ------------------------
def test_cap9_revision_and_rollback(env):
    eng, session, _ = env
    from app.services.ontology.revisions import record_revision
    from app.models.ontology_revision import OntologyRevision

    obj = session.query(OntologyObject).filter(
        OntologyObject.id == OBJ_VEHICLE).first()
    record_revision(session, "object", obj, change_kind="drift",
                    changed_by="test", summary="covered by test")
    revs = session.query(OntologyRevision).filter(
        OntologyRevision.entity_id == OBJ_VEHICLE).all()
    assert len(revs) >= 1

    # Rollback revalidation: a retired metric must never return an old number.
    m = session.query(MetricDefinition).filter(
        MetricDefinition.id == METRIC_GROSS_MARGIN).first()
    m.lifecycle = "retired"
    session.commit()
    res = compute_metric(session, ORG, METRIC_GROSS_MARGIN,
                         filters={"model_id": "v-001", "sale_month": "2026-10"})
    assert res["success"] is False and res["unavailable"] is True


# 10) Graph API + permission-filtered payload -------------------------------
def test_cap10_graph_api_permission_filtered(env):
    eng, session, _ = env
    owner = ontology_graph(org_id=ORG, db=session, user=_user(OWNER_ID), domain_id=None)
    restricted = ontology_graph(org_id=ORG, db=session, user=_user(RESTRICTED_ID), domain_id=None)

    def gm_doc(payload):
        for o in payload["objects"]:
            if o["id"] == OBJ_GROSS_MARGIN:
                return (o.get("provenance") or {}).get("document_provenance", [])
        return []

    assert len(gm_doc(owner)) == 1
    assert gm_doc(restricted) == []

    # metrics carry depends_on + explicit dependency edges
    gm = next(m for m in owner["metrics"] if m["id"] == METRIC_GROSS_MARGIN)
    assert set(gm["depends_on_metrics"]) == {METRIC_REVENUE, METRIC_TOTAL_COST}
    edge_pairs = {(e["from_metric_id"], e["to_metric_id"])
                  for e in owner["metric_dependencies"]}
    assert (METRIC_GROSS_MARGIN, METRIC_REVENUE) in edge_pairs


# --- Explicit acceptance constraints ---------------------------------------

def test_metric_dependency_correctness(env):
    eng, session, _ = env
    # GrossMargin depends on Revenue + TotalCost; TotalCost depends on its
    # two sub-metrics — the graph must represent ALL of these edges.
    gm = session.query(MetricDefinition).filter(
        MetricDefinition.id == METRIC_GROSS_MARGIN).first()
    tc = session.query(MetricDefinition).filter(
        MetricDefinition.id == METRIC_TOTAL_COST).first()
    assert set(gm.depends_on_metrics) == {METRIC_REVENUE, METRIC_TOTAL_COST}
    assert set(tc.depends_on_metrics) == {METRIC_COST_DIRECT, METRIC_COST_INDIRECT}

    edges = build_dependency_edges(session, ORG)
    pairs = {(e["from_metric_id"], e["to_metric_id"]) for e in edges}
    assert (METRIC_GROSS_MARGIN, METRIC_REVENUE) in pairs
    assert (METRIC_GROSS_MARGIN, METRIC_TOTAL_COST) in pairs
    assert (METRIC_TOTAL_COST, METRIC_COST_DIRECT) in pairs
    assert (METRIC_TOTAL_COST, METRIC_COST_INDIRECT) in pairs


def test_metric_dependency_cycle_rejected(env):
    eng, session, _ = env
    # Seed two metrics that would form a cycle, then attempt to declare it.
    a = MetricDefinition(id="cyc-a", org_id=ORG, datasource_id=KB_SALES,
                        name="CycA", base_table="sales_transactions",
                        base_column="margin", aggregation="sum",
                        lifecycle="published", is_enabled=True,
                        reviewed_by="a", version=1)
    b = MetricDefinition(id="cyc-b", org_id=ORG, datasource_id=KB_SALES,
                        name="CycB", base_table="sales_transactions",
                        base_column="margin", aggregation="sum",
                        lifecycle="published", is_enabled=True,
                        reviewed_by="a", version=1)
    session.add_all([a, b])
    session.commit()
    # a -> b then b -> a would close a loop: validate the second edge.
    a.depends_on_metrics = ["cyc-b"]
    session.commit()
    ok, errs = validate_metric_dependencies(
        session, ORG, "cyc-b", ["cyc-a"])
    assert ok is False
    assert any("circular" in e.lower() for e in errs)


def test_p5_rollback_safety_via_graph(env):
    eng, session, _ = env
    # Already asserted in test_cap9; re-assert the core invariant directly:
    res = compute_metric(session, ORG, METRIC_REVENUE,
                         filters={"sale_month": "2026-10"})
    assert res["success"] is True
    m = session.query(MetricDefinition).filter(
        MetricDefinition.id == METRIC_REVENUE).first()
    m.lifecycle = "retired"
    session.commit()
    res2 = compute_metric(session, ORG, METRIC_REVENUE,
                          filters={"sale_month": "2026-10"})
    assert res2["success"] is False
