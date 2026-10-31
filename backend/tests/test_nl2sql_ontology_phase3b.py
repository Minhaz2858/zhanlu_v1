"""Phase 3B — complete ontology-aware NL2SQL integration.

Covers: ontology provenance persisted additively into
``Nl2sqlQueryLog.lineage_json`` (historical rows stay readable),
datasource-scoped enabled SQL business rules, MetricDefinition-aware
candidate ranking (names/semantics only — never SQL or formulas), and
honest degradation when metric table mappings are missing.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.business_rule import BusinessRule
from app.models.metric_definition import MetricDefinition
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyLink, OntologyObject
from app.models.terminology import Terminology
from app.services.db.nl_answer_service import _shadow_validate_and_log
from app.services.db.table_retrieval import retrieve_for_question
from app.services.enterprise.business_context import (
    build_ontology_planning_context)

ORG = "org-acme"
ORG2 = "org-other"

TOC = [
    {"table_name": "crm_cust_info", "description_zh": "",
     "description_en": "customer master data with contact details",
     "domain_name": "", "row_count": 1000,
     "min_date": "2025-01-01", "max_date": "2026-01-01", "columns": []},
    {"table_name": "sales_order", "description_zh": "",
     "description_en": "sales order headers and totals",
     "domain_name": "", "row_count": 1000,
     "min_date": "2025-01-01", "max_date": "2026-01-01", "columns": []},
    {"table_name": "sys_config", "description_zh": "",
     "description_en": "system configuration parameters",
     "domain_name": "", "row_count": 1000,
     "min_date": "2025-01-01", "max_date": "2026-01-01", "columns": []},
]


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        OntologyObject.__table__, OntologyLink.__table__,
        Terminology.__table__, BusinessRule.__table__,
        MetricDefinition.__table__, Nl2sqlQueryLog.__table__,
    ])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def log_db(db, monkeypatch):
    """Point the log writer's SessionLocal at the ephemeral engine."""
    maker = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.database.SessionLocal", maker)
    return db


# ── Task A: ontology provenance persistence ──────────────────────────────────

def test_ontology_provenance_persists_preserving_lineage(log_db):
    prov = {
        "objects": [{"name": "Customer", "table": "crm_cust_info",
                     "influenced": True}],
        "links": [], "terms": [], "hypotheses": [],
        "enabled_sql_rules": [{"id": "br-1", "influenced": False}],
        "metrics": [],
    }
    _shadow_validate_and_log(
        "kb-1", "customer overview",
        "SELECT id FROM crm_cust_info WHERE order_date >= '2026-01-01'",
        live_success=True, ontology_provenance=prov)
    row = log_db.query(Nl2sqlQueryLog).one()
    lj = row.lineage_json
    # additive ontology section, byte-preserved
    assert lj["ontology"] == prov
    # existing lineage fields preserved
    assert "crm_cust_info" in lj["tables"]
    assert "order_date" in lj["filter_columns"]


def test_historical_lineage_compat(log_db):
    # no provenance → lineage_json untouched (no "ontology" key pretending
    # provenance existed); historical readers keep working
    _shadow_validate_and_log(
        "kb-1", "orders", "SELECT id FROM sales_order",
        live_success=True)
    row = log_db.query(Nl2sqlQueryLog).one()
    assert "ontology" not in (row.lineage_json or {})
    assert "sales_order" in row.lineage_json["tables"]

    log_db.query(Nl2sqlQueryLog).delete()
    log_db.commit()

    # provenance present but lineage not computable → ontology-only record,
    # never an empty dict pretending "no lineage"
    prov = {"objects": [], "metrics": [], "enabled_sql_rules": []}
    _shadow_validate_and_log(
        "kb-1", "orders", None, live_success=False, live_error="x",
        ontology_provenance=prov)
    row = log_db.query(Nl2sqlQueryLog).one()
    assert row.lineage_json == {"ontology": prov}

    log_db.query(Nl2sqlQueryLog).delete()
    log_db.commit()

    # neither → stays NULL exactly as historical rows did
    _shadow_validate_and_log(
        "kb-1", "orders", None, live_success=False, live_error="x")
    row = log_db.query(Nl2sqlQueryLog).one()
    assert row.lineage_json is None


# ── Task B: datasource-scoped business rules ─────────────────────────────────

def _rule(db, rid, statement, *, org=ORG, status="enabled", ds=None,
          deleted=False):
    db.add(BusinessRule(id=rid, org_id=org, statement=statement,
                        status=status, applies_to="sql",
                        datasource_ids=ds, is_deleted=deleted))
    db.commit()


def test_datasource_scoped_rule_filtering(db):
    _rule(db, "br-org", "Org-wide: exclude cancelled orders")
    _rule(db, "br-kb1", "kb-1 only: revenue excludes VAT", ds=["kb-1"])
    _rule(db, "br-kb2", "kb-2 only: revenue includes VAT", ds=["kb-2"])

    ctx1 = build_ontology_planning_context(
        db, ORG, query="revenue", datasource_id="kb-1")
    assert {r["id"] for r in ctx1["enabled_sql_rules"]} == {"br-org", "br-kb1"}

    ctx2 = build_ontology_planning_context(
        db, ORG, query="revenue", datasource_id="kb-2")
    assert {r["id"] for r in ctx2["enabled_sql_rules"]} == {"br-org", "br-kb2"}

    # no datasource → org-wide union (Phase 3A behavior unchanged)
    ctx0 = build_ontology_planning_context(db, ORG, query="revenue")
    assert {r["id"] for r in ctx0["enabled_sql_rules"]} == {
        "br-org", "br-kb1", "br-kb2"}


def test_cross_org_rules_never_leak_through_scoping(db):
    _rule(db, "br-evil", "Other-org rule", org=ORG2, ds=["kb-1"])
    ctx = build_ontology_planning_context(
        db, ORG, query="rule", datasource_id="kb-1")
    assert ctx["enabled_sql_rules"] == []


def test_disabled_or_proposed_rules_excluded_even_when_scoped(db):
    _rule(db, "br-prop", "Maybe rounding", status="proposed", ds=["kb-1"])
    _rule(db, "br-ver", "Verified not yet enabled", status="verified",
          ds=["kb-1"])
    _rule(db, "br-off", "Soft-deleted rule", ds=["kb-1"], deleted=True)
    ctx = build_ontology_planning_context(
        db, ORG, query="rounding", datasource_id="kb-1")
    assert ctx["enabled_sql_rules"] == []


# ── Task C: MetricDefinition-aware ranking ───────────────────────────────────

def _metric(db, mid, name, *, base_table=None, status="verified",
            lifecycle="published", enabled=True, org=ORG, synonyms=None):
    db.add(MetricDefinition(
        id=mid, org_id=org, datasource_id="kb-1", name=name,
        description=f"{name} definition", base_table=base_table,
        aggregation="sum", synonyms=synonyms or [], is_enabled=enabled,
        evidence_status=status, lifecycle=lifecycle))
    db.commit()


def test_metric_aware_candidate_ranking(db):
    _metric(db, "md-1", "Net Revenue", base_table="sales_order",
            synonyms=["turnover"])
    _metric(db, "md-draft", "Draft Revenue", base_table="sales_order",
            lifecycle="draft")
    ctx = build_ontology_planning_context(db, ORG, query="net revenue")
    names = {m["name"] for m in ctx["metrics"]}
    assert "Net Revenue" in names
    assert "Draft Revenue" not in names
    assert ctx["metrics"][0]["base_table"] == "sales_order"
    # no SQL/formula ever leaks into the planning context
    assert "base_sql" not in ctx["metrics"][0]

    res = retrieve_for_question(
        None, ["kb-1"], "net revenue turnover", TOC, enabled=False,
        ontology_context=ctx)
    cand = {c.table_name: c for c in res.candidates}
    assert cand["sales_order"].breakdown.get("ontology-metric", 0) > 0
    mprov = [m for m in res.ontology_provenance["metrics"]
             if m["name"] == "Net Revenue"]
    assert mprov and mprov[0]["influenced"] is True
    assert mprov[0]["table"] == "sales_order"


def test_metric_without_mapping_degrades_honestly(db):
    _metric(db, "md-nomap", "Gross Margin", base_table=None)
    _metric(db, "md-bad", "Mystery Metric", base_table="not_a_real_table")
    _metric(db, "md-unver", "Proposed Metric", base_table="sales_order",
            status="proposed")
    ctx = build_ontology_planning_context(db, ORG, query="margin metric")
    assert {m["name"] for m in ctx["metrics"]} >= {
        "Gross Margin", "Mystery Metric", "Proposed Metric"}

    res = retrieve_for_question(
        None, ["kb-1"], "gross margin metric", TOC, enabled=False,
        ontology_context=ctx)
    # no fabricated boost anywhere: unmapped and unverified metrics never
    # influence ranking, and the provenance says so
    for c in res.candidates:
        assert c.breakdown.get("ontology-metric", 0) == 0
    by_name = {m["name"]: m for m in res.ontology_provenance["metrics"]}
    assert by_name["Gross Margin"]["influenced"] is False
    assert by_name["Gross Margin"]["unmapped"] is True
    assert by_name["Mystery Metric"]["influenced"] is False
    assert by_name["Mystery Metric"]["unmapped"] is True
    # mapped but unverified → provenance-only, marked not-influenced
    assert by_name["Proposed Metric"]["influenced"] is False
    assert by_name["Proposed Metric"].get("unmapped") is not True


# ── Flag-off / backward compatibility ─────────────────────────────────────────

def test_flag_off_and_phase3a_context_compat(db):
    # a Phase 3A-shaped context (no "metrics" key) must keep working and
    # must never fabricate metric provenance
    ctx_3a = {"objects_verified": [], "objects_hypotheses": [],
              "links": [], "terms": [], "enabled_sql_rules": []}
    res = retrieve_for_question(
        None, ["kb-1"], "sales orders", TOC, enabled=False,
        ontology_context=ctx_3a)
    for c in res.candidates:
        assert "ontology-metric" not in c.breakdown
    assert res.ontology_provenance["metrics"] == []
    # no ontology context at all → provenance stays None (flag-off path)
    res_none = retrieve_for_question(
        None, ["kb-1"], "sales orders", TOC, enabled=False,
        ontology_context=None)
    assert res_none.ontology_provenance is None
    # flag default remains OFF
    assert settings.NL2SQL_ONTOLOGY_ENABLED is False
