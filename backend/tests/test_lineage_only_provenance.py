"""Phase 7C T11 — canonical prov-v1 for lineage-only NL2SQL rows.

Before 7C the whole provenance build sat inside ``if ontology_provenance:``
so a query with SQL lineage but NO planning provenance got no canonical
envelope despite ``from_sql_lineage`` being ready and the org already
hoisted. The envelope must build whenever ANY evidence exists; the raw
``ontology`` section stays gated; no-evidence rows stay byte-identical.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.services.db.nl_answer_service import _shadow_validate_and_log

ORG = "org-acme"
SQL = "SELECT id FROM sales_order WHERE order_date >= '2026-01-01'"


@pytest.fixture()
def log_db(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        KnowledgeBase.__table__, Nl2sqlQueryLog.__table__])
    s = sessionmaker(bind=eng)()
    s.add(KnowledgeBase(id="kb-1", org_id=ORG, name="DB",
                        source_kind="database"))
    s.commit()
    maker = sessionmaker(bind=eng)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    yield s
    s.close()
    eng.dispose()


def test_lineage_only_row_gets_canonical_provenance(log_db):
    _shadow_validate_and_log(
        "kb-1", "orders", SQL, live_success=True,
        ontology_provenance=None)
    row = log_db.query(Nl2sqlQueryLog).one()
    lj = row.lineage_json
    assert "ontology" not in lj  # raw section stays gated
    prov = lj.get("provenance")
    assert prov and prov["version"] == "prov-v1"
    sql_entries = [e for e in prov["entries"]
                   if e.get("source_type") == "sql_lineage"]
    assert sql_entries, "sql lineage evidence must reach the envelope"
    assert all(e["org_id"] == ORG for e in sql_entries)
    # execution_ref names the real persisted row
    assert all(e.get("execution_ref") == f"nl2sql_query:{row.id}"
               for e in sql_entries)


def test_no_evidence_row_stays_byte_identical(log_db):
    # a statement lineage cannot extract (not SQL) → no entries at all
    _shadow_validate_and_log(
        "kb-1", "orders", "not really sql", live_success=True,
        ontology_provenance=None)
    row = log_db.query(Nl2sqlQueryLog).one()
    lj = row.lineage_json
    assert "provenance" not in (lj or {})


def test_both_present_row_unchanged_contract(log_db):
    prov = {"objects": [{"name": "Order", "table": "sales_order",
                         "influenced": True}],
            "links": [], "terms": [], "hypotheses": [],
            "enabled_sql_rules": [], "metrics": []}
    _shadow_validate_and_log("kb-1", "orders", SQL, live_success=True,
                             ontology_provenance=prov)
    row = log_db.query(Nl2sqlQueryLog).one()
    lj = row.lineage_json
    assert lj["ontology"] == prov
    types = {e["source_type"] for e in lj["provenance"]["entries"]}
    assert "sql_lineage" in types and "ontology_object" in types
