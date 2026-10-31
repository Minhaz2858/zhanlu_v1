"""Phase 3A — ontology-aware NL2SQL planning.

Covers: verified ontology objects boosting relevant table candidates,
terminology aliases aiding retrieval, verified relationships as
candidate hints (NEVER executable joins), proposed content staying
hypotheses, enabled-SQL-rules-only guidance, cross-org isolation,
flag-off behavior identical to before, and multi-datasource dynamism.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.business_rule import BusinessRule
from app.models.ontology import OntologyLink, OntologyObject
from app.models.terminology import Terminology
from app.services.db.table_retrieval import retrieve_for_question
from app.services.enterprise.business_context import (
    build_ontology_planning_context)

ORG = "org-acme"
ORG2 = "org-other"


def _toc(*specs):
    """specs: (table, description_en) — minimal catalog rows."""
    out = []
    for t, d in specs:
        out.append({"table_name": t, "description_zh": "", "description_en": d,
                    "domain_name": "", "row_count": 1000,
                    "min_date": "2025-01-01", "max_date": "2026-01-01",
                    "columns": []})
    return out


TOC = _toc(
    ("crm_cust_info", "customer master data with contact details"),
    ("sales_order", "sales order headers and totals"),
    ("sys_config", "system configuration parameters"),
)

NOTE = "database notes"  # filler to keep descriptions distinct


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
    ])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _object(db, oid, name, *, org=ORG, table=None, status="verified",
            kind="table", ref=None):
    db.add(OntologyObject(
        id=oid, org_id=org, domain_id="dom-1", name=name,
        backing_kind=kind if table else "manual",
        backing_ref=ref or (f"kb:kb-1:{table}" if table else None),
        evidence_status=status))
    db.commit()


def _link(db, lid, a, b, *, org=ORG, ltype="relates_to", status="verified"):
    db.add(OntologyLink(id=lid, org_id=org, from_object_id=a,
                        to_object_id=b, link_type=ltype,
                        evidence_status=status))
    db.commit()


# 1. Verified ontology object improves relevant table selection ---------------

def test_verified_object_boosts_backing_table(db):
    _object(db, "oo-cust", "Customer", table="crm_cust_info")
    ctx = build_ontology_planning_context(db, ORG, query="customer overview")
    assert ctx["objects_verified"][0]["name"] == "Customer"
    assert ctx["objects_verified"][0]["backing_table"] == "crm_cust_info"
    res = retrieve_for_question(
        None, ["kb-1"], "customer overview", TOC, enabled=False,
        ontology_context=ctx)
    cand = {c.table_name: c for c in res.candidates}
    assert "crm_cust_info" in cand
    assert cand["crm_cust_info"].breakdown.get("ontology", 0) > 0
    assert any("verified" in e for e in cand["crm_cust_info"].evidence)
    # provenance carries the selected object + its table mapping
    assert res.ontology_provenance["objects"][0]["table"] == "crm_cust_info"


# 2. Terminology alias improves candidate retrieval -----------------------------

def test_terminology_alias_improves_retrieval(db):
    db.add(Terminology(id="t-1", org_id=ORG, word="churn",
                       description="subscription cancellations and renewals",
                       enabled=True))
    db.commit()
    toc = _toc(("subscriptions", "subscription cancellations and renewals"),
               ("sys_config", "system configuration parameters"))
    ctx = build_ontology_planning_context(db, ORG, query="churn")
    assert ctx["terms"][0]["word"] == "churn"
    res = retrieve_for_question(
        None, ["kb-1"], "churn", toc, enabled=False,
        ontology_context=ctx)
    cand = {c.table_name: c for c in res.candidates}
    assert cand["subscriptions"].breakdown.get("terminology", 0) > 0


# 3-4. Verified relationships are hints; proposed ones never become joins ------

def test_verified_relationship_hint_boosts_partner(db):
    _object(db, "oo-cust", "Customer", table="crm_cust_info")
    _object(db, "oo-order", "SalesOrder", table="sales_order")
    _link(db, "ol-1", "oo-cust", "oo-order", ltype="places")
    ctx = build_ontology_planning_context(db, ORG, query="customer orders")
    res = retrieve_for_question(
        None, ["kb-1"], "customer orders", TOC, enabled=False,
        ontology_context=ctx)
    cand = {c.table_name: c for c in res.candidates}
    assert cand["sales_order"].breakdown.get("ontology-link", 0) > 0
    # the partner is pulled in as a candidate HINT — explicitly not a join
    assert cand["crm_cust_info"].breakdown.get("ontology", 0) > 0
    assert any("not a join" in e for e in cand["sales_order"].evidence)
    assert res.ontology_provenance["links"][0]["tables"] == [
        "crm_cust_info", "sales_order"]


def test_proposed_relationship_stays_hypothesis(db):
    _object(db, "oo-cust", "Customer", table="crm_cust_info",
            status="proposed")
    _object(db, "oo-order", "SalesOrder", table="sales_order")
    _link(db, "ol-h", "oo-cust", "oo-order", ltype="maybe_links",
          status="proposed")
    ctx = build_ontology_planning_context(db, ORG, query="sales orders")
    assert [o["name"] for o in ctx["objects_verified"]] == ["SalesOrder"]
    assert [o["name"] for o in ctx["objects_hypotheses"]] == ["Customer"]
    assert ctx["objects_hypotheses"][0]["hypothesis"] is True
    res = retrieve_for_question(
        None, ["kb-1"], "customer sales", TOC, enabled=False,
        ontology_context=ctx)
    cand = {c.table_name: c for c in res.candidates}
    # proposed OBJECT: at most a labeled hypothesis micro-boost, never "ontology"
    assert cand["crm_cust_info"].breakdown.get("ontology", 0) == 0
    assert cand["crm_cust_info"].breakdown.get("ontology-hypothesis") == 0.25
    assert any("PROPOSED" in e for e in cand["crm_cust_info"].evidence)
    # proposed LINK: zero score, provenance-only
    for c in res.candidates:
        assert c.breakdown.get("ontology-link", 0) == 0
    assert any(h.get("link") for h in res.ontology_provenance["hypotheses"])


# 5-6. Enabled SQL rules only ------------------------------------------------------

def test_enabled_sql_rules_available_and_others_excluded(db):
    db.add_all([
        BusinessRule(id="br-on", org_id=ORG,
                     statement="Exclude cancelled orders from revenue",
                     status="enabled", applies_to="sql",
                     datasource_ids=["kb-2"]),
        BusinessRule(id="br-ver", org_id=ORG, statement="Use fiscal July",
                     status="verified", applies_to="sql"),
        BusinessRule(id="br-prop", org_id=ORG, statement="Maybe rounding",
                     status="proposed", applies_to="sql"),
        BusinessRule(id="br-act", org_id=ORG, statement="Action rule",
                     status="enabled", applies_to="action"),
    ])
    db.commit()
    ctx = build_ontology_planning_context(db, ORG, query="orders revenue")
    ids = {r["id"] for r in ctx["enabled_sql_rules"]}
    assert ids == {"br-on"}  # verified-not-enabled / proposed / non-SQL excluded
    assert ctx["enabled_sql_rules"][0]["datasource_ids"] == ["kb-2"]
    res = retrieve_for_question(
        None, ["kb-1"], "orders revenue", TOC, enabled=False,
        ontology_context=ctx)
    assert res.ontology_provenance["enabled_sql_rules"][0]["id"] == "br-on"


# 7. Cross-org isolation --------------------------------------------------------------

def test_cross_org_isolation(db):
    _object(db, "oo-evil", "EvilObject", org=ORG2, table="evil_table")
    db.add(Terminology(id="t-evil", org_id=ORG2, word="evilterm",
                       description="x", enabled=True))
    db.add(BusinessRule(id="br-evil", org_id=ORG2, statement="evil rule",
                        status="enabled", applies_to="sql"))
    db.commit()
    ctx = build_ontology_planning_context(db, ORG, query="evil")
    assert ctx["objects_verified"] == [] and ctx["objects_hypotheses"] == []
    assert ctx["terms"] == [] and ctx["enabled_sql_rules"] == []
    res = retrieve_for_question(
        None, ["kb-1"], "evil", TOC, enabled=False, ontology_context=ctx)
    assert "evil_table" not in {c.table_name for c in res.candidates}


# 8. Flag/disabled path — existing NL2SQL behavior intact ------------------------------

def test_no_ontology_context_is_byte_identical_behavior(db):
    res = retrieve_for_question(
        None, ["kb-1"], "sales orders", TOC, enabled=False,
        ontology_context=None)
    for c in res.candidates:
        assert "ontology" not in c.breakdown
        assert "ontology-link" not in c.breakdown
        assert "terminology" not in c.breakdown
    assert res.ontology_provenance is None
    # and the flag default is OFF
    assert settings.NL2SQL_ONTOLOGY_ENABLED is False


# 9. Multiple authorized datasources remain dynamically selectable ---------------------

def test_multiple_datasources_remain_selectable(db):
    _object(db, "oo-cust", "Customer", table="crm_cust_info")
    _object(db, "oo-stock", "Warehouse Stock", table="warehouse_stock")
    toc = _toc(
        ("crm_cust_info", "customer master data (kb-1)"),
        ("warehouse_stock", "warehouse stock levels (kb-2)"),
    )
    ctx = build_ontology_planning_context(db, ORG, query="customer stock")
    res = retrieve_for_question(
        None, ["kb-1", "kb-2"], "customer stock", toc,
        enabled=False, ontology_context=ctx)
    names = {c.table_name for c in res.candidates}
    # both datasources' tables remain candidates — no default/first-database
    # selection, ontology only boosts, never excludes
    assert {"crm_cust_info", "warehouse_stock"} <= names
