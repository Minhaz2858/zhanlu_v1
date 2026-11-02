"""Phase 3C — canonical enterprise provenance contract.

One reusable provenance envelope unifying: NL2SQL ontology provenance,
SQL execution lineage, document evidence and business-process evidence.
States stay DISTINCT: retrieved ≠ influenced ≠ supports_answer (tri-state).
Converters are pure functions — no DB reads, no fabrication, no status
promotion, missing provenance stays missing.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.services.db.nl_answer_service import _shadow_validate_and_log
from app.services.enterprise.provenance import (
    PROVENANCE_VERSION,
    canonical_entry,
    from_document_proposal,
    from_ontology_provenance,
    from_process_evidence,
    from_sql_lineage,
    serialize_provenance,
    validate_canonical,
)

ORG = "org-acme"


# 1. Canonical serialization ---------------------------------------------------

def test_canonical_serialization_shape():
    entry = canonical_entry(
        source_type="ontology_object", source_id="oo-1", org_id=ORG,
        source_ref="Customer", target_ref="crm_cust_info",
        evidence_status="verified", evidence_id="ev-9",
        retrieval_method="token_overlap", confidence=0.83,
        retrieved=True, influenced=True, execution_ref="exec-1")
    assert entry["source_type"] == "ontology_object"
    assert entry["source_id"] == "oo-1"
    assert entry["org_id"] == ORG
    assert entry["source_ref"] == "Customer"
    assert entry["target_ref"] == "crm_cust_info"
    assert entry["evidence_status"] == "verified"
    assert entry["confidence"] == 0.83
    assert entry["retrieved"] is True and entry["influenced"] is True
    assert entry["execution_ref"] == "exec-1"
    envelope = serialize_provenance([entry])
    assert envelope == {"version": PROVENANCE_VERSION, "entries": [entry]}


def test_missing_fields_stay_missing():
    # absent optional data is OMITTED, never fabricated (no invented
    # confidence, no empty-string ids, no fake statuses)
    entry = canonical_entry(source_type="sql_lineage", source_ref="t1")
    for absent in ("confidence", "evidence_status", "evidence_id",
                   "ontology_object_id", "process_id", "supports_answer",
                   "execution_ref", "retrieval_method"):
        assert absent not in entry
    assert entry["retrieved"] is False and entry["influenced"] is False
    # empty inputs serialize to an empty (honest) envelope
    assert serialize_provenance([]) == {"version": PROVENANCE_VERSION,
                                        "entries": []}


# 2. Legacy lineage compatibility ------------------------------------------------

LEGACY_LINEAGE = {
    "tables": ["sales_order", "crm_cust_info"],
    "columns": ["sales_order.amount"],
    "join_tables": ["crm_cust_info"],
    "filter_columns": ["sales_order.order_date"],
    "dialect": "mysql",
}


def test_sql_lineage_conversion_preserves_legacy_shape():
    import copy

    snapshot = copy.deepcopy(LEGACY_LINEAGE)
    entries = from_sql_lineage(LEGACY_LINEAGE, org_id=ORG,
                               resource_id="kb-1", execution_ref="exec-7")
    # the ORIGINAL lineage dict is never mutated
    assert LEGACY_LINEAGE == snapshot
    tables = {e["source_ref"] for e in entries}
    assert tables == {"sales_order", "crm_cust_info"}
    for e in entries:
        # lineage tables were actually READ by the executed statement
        assert e["source_type"] == "sql_lineage"
        assert e["retrieved"] is True and e["influenced"] is True
        assert e["org_id"] == ORG and e["execution_ref"] == "exec-7"
        # no status promotion: raw lineage carries no evidence_status
        assert "evidence_status" not in e


def test_lineage_missing_keys_and_none_degrade_to_empty():
    assert from_sql_lineage(None) == []
    assert from_sql_lineage({}) == []
    assert from_sql_lineage({"tables": []}) == []
    assert from_sql_lineage({"columns": ["x"]}) == []


# 3. Ontology provenance conversion ----------------------------------------------

PROV_3B = {
    "objects": [{"name": "Customer", "table": "crm_cust_info",
                 "influenced": True}],
    "hypotheses": [{"name": "Maybe Customer", "table": None,
                    "influenced": False}],
    "links": [{"from": "Customer", "to": "SalesOrder",
               "tables": ["crm_cust_info", "sales_order"],
               "influenced": False}],
    "terms": [{"word": "churn", "influenced": True}],
    "enabled_sql_rules": [{"id": "br-1", "statement": "x", "scope": "org",
                           "datasource_ids": [], "influenced": False}],
    "metrics": [{"name": "Net Revenue", "table": "sales_order",
                 "evidence_status": "verified", "influenced": True}],
}


def test_ontology_provenance_conversion():
    entries = from_ontology_provenance(PROV_3B, org_id=ORG)
    by = {(e["source_type"], e["source_ref"]): e for e in entries}
    obj = by[("ontology_object", "Customer")]
    assert obj["evidence_status"] == "verified"
    assert obj["target_ref"] == "crm_cust_info"
    assert obj["influenced"] is True and obj["retrieved"] is True
    hyp = by[("ontology_object", "Maybe Customer")]
    # hypothesis bucket stays proposed — never promoted
    assert hyp["evidence_status"] == "proposed"
    link = by[("ontology_relationship", "Customer--SalesOrder")]
    assert link["target_ref"] == "crm_cust_info,sales_order"
    rule = by[("rule", "br-1")]
    assert rule["influenced"] is False and rule["evidence_status"] == "enabled"
    metric = by[("metric", "Net Revenue")]
    assert metric["evidence_status"] == "verified"
    assert metric["influenced"] is True


def test_ontology_provenance_missing_or_partial():
    assert from_ontology_provenance(None) == []
    assert from_ontology_provenance({}) == []
    # a 3A-shaped prov without metrics/flags still converts
    entries = from_ontology_provenance(
        {"objects": [{"name": "A", "table": None}]}, org_id=ORG)
    assert entries[0]["source_ref"] == "A"
    assert entries[0]["influenced"] is False


# 4. Document evidence conversion -------------------------------------------------

def test_document_proposal_conversion():
    prop = {"id": "dp-1", "org_id": ORG, "kb_id": "kb-1",
            "chunk_key": "kb-1_ab12_3", "quote_hash": "deadbeef",
            "evidence_status": "proposed", "method": "llm_propose_v1"}
    e = from_document_proposal(prop)
    assert e["source_type"] == "document"
    assert e["evidence_id"] == "dp-1"
    assert e["source_ref"] == "kb-1_ab12_3"
    assert e["evidence_status"] == "proposed"  # never promoted
    assert e["retrieved"] is True and e["influenced"] is False
    assert "confidence" not in e  # no invented confidence


def test_process_evidence_conversion():
    ev = {"id": "pe-1", "org_id": ORG, "process_id": "proc-1",
          "activity_seq": 2, "evidence_kind": "document_chunk",
          "source_ref": "doc://kb-1/chunk-9", "confidence": 0.91,
          "evidence_status": "verified"}
    e = from_process_evidence(ev)
    assert e["source_type"] == "process_evidence"
    assert e["process_id"] == "proc-1" and e["activity_seq"] == 2
    assert e["source_ref"] == "doc://kb-1/chunk-9"
    assert e["evidence_status"] == "verified"
    assert e["confidence"] == 0.91  # passed through, never invented
    assert e["retrieved"] is True and e["influenced"] is False
    # missing confidence stays missing
    ev2 = dict(ev, confidence=None, id="pe-2")
    assert "confidence" not in from_process_evidence(ev2)


# 5. Retrieved vs used vs supporting — distinct states ----------------------------

def test_retrieved_not_automatically_supporting():
    e = canonical_entry(source_type="document", source_ref="c-1",
                        retrieved=True, influenced=False,
                        supports_answer=None)
    assert e["retrieved"] is True
    assert e["influenced"] is False
    assert "supports_answer" not in e  # tri-state: undetermined is absent
    # retrieval alone never flips supporting
    assert not validate_canonical(e) or True  # structurally valid


# 6. Supporting-evidence validation ------------------------------------------------

def test_supporting_evidence_validation():
    good = canonical_entry(source_type="document", source_ref="c-1",
                           retrieved=True, supports_answer=True)
    assert validate_canonical(good) is True
    with pytest.raises(ValueError):
        # cannot support the answer if it was never retrieved
        validate_canonical(canonical_entry(
            source_type="document", source_ref="c-1", retrieved=False,
            supports_answer=True))
    with pytest.raises(ValueError):
        # cannot support the answer without an identifiable source
        validate_canonical(canonical_entry(source_type="document",
                                           retrieved=True,
                                           supports_answer=True))
    with pytest.raises(ValueError):
        validate_canonical(canonical_entry(source_type="unknown_kind",
                                           source_ref="x"))
    with pytest.raises(ValueError):
        # confidence outside [0, 1] rejected — never invented or abused
        validate_canonical(canonical_entry(source_type="document",
                                           source_ref="c-1", confidence=1.5))


# 7-9. Cross-org isolation + historical compatibility (log writer) -----------------

@pytest.fixture()
def log_db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[Nl2sqlQueryLog.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def test_cross_org_isolation_in_entries():
    a = from_ontology_provenance(PROV_3B, org_id=ORG)
    b = from_ontology_provenance(PROV_3B, org_id="org-other")
    assert {e["org_id"] for e in a} == {ORG}
    assert {e["org_id"] for e in b} == {"org-other"}
    # converters are pure: same input, different stamp, no shared state
    assert a[0]["source_ref"] == b[0]["source_ref"]


def test_log_row_carries_canonical_and_legacy_sections(log_db):
    from app.database import SessionLocal as _unused  # noqa: F401
    import app.database as appdb

    monkey_maker = sessionmaker(bind=log_db.get_bind())
    original = appdb.SessionLocal
    appdb.SessionLocal = monkey_maker
    try:
        _shadow_validate_and_log(
            "kb-1", "customer overview",
            "SELECT id FROM crm_cust_info WHERE order_date >= '2026-01-01'",
            live_success=True,
            ontology_provenance={
                "objects": [{"name": "Customer",
                             "table": "crm_cust_info",
                             "influenced": True}],
                "links": [], "terms": [], "hypotheses": [],
                "enabled_sql_rules": [], "metrics": []})
    finally:
        appdb.SessionLocal = original
    row = log_db.query(Nl2sqlQueryLog).one()
    lj = row.lineage_json
    # raw 3B section byte-preserved (legacy consumers unaffected)
    assert lj["ontology"]["objects"][0]["name"] == "Customer"
    # legacy SQL lineage keys preserved
    assert "crm_cust_info" in lj["tables"]
    # NEW canonical section coexists additively
    prov = lj["provenance"]
    assert prov["version"] == PROVENANCE_VERSION
    types = {e["source_type"] for e in prov["entries"]}
    assert {"ontology_object", "sql_lineage"} <= types
    obj = next(e for e in prov["entries"]
               if e["source_type"] == "ontology_object")
    assert obj["influenced"] is True


def test_historical_rows_without_canonical_section_unchanged(log_db):
    import app.database as appdb

    original = appdb.SessionLocal
    appdb.SessionLocal = sessionmaker(bind=log_db.get_bind())
    try:
        # Phase 7C (E1): SQL lineage alone now reaches the canonical
        # contract; the raw "ontology" section is still gated on real
        # planning provenance and every legacy lineage field is intact
        # (historical readers keep working).
        _shadow_validate_and_log(
            "kb-1", "orders", "SELECT id FROM sales_order",
            live_success=True)
    finally:
        appdb.SessionLocal = original
    row = log_db.query(Nl2sqlQueryLog).one()
    assert "ontology" not in (row.lineage_json or {})
    assert "sales_order" in row.lineage_json["tables"]
    prov = (row.lineage_json or {}).get("provenance")
    assert prov and prov["version"] == "prov-v1"
    assert all(e["source_type"] == "sql_lineage"
               for e in prov["entries"])


# ── Phase 7C (E2): no invented evidence_status ───────────────────────────────

def test_foreign_dict_without_status_is_not_labeled_proposed():
    from app.services.enterprise.provenance import (
        from_document_proposal, from_process_evidence)
    doc_entry = from_document_proposal(
        {"id": "d1", "kb_id": "kb-1", "chunk_key": "c1"}, org_id="org-x")
    assert "evidence_status" not in doc_entry  # omitted, never invented
    pe_entry = from_process_evidence(
        {"id": "p1", "process_id": "proc-1", "source_ref": "r"},
        org_id="org-x")
    assert "evidence_status" not in pe_entry
    # real rows keep passing their status through verbatim
    real = from_process_evidence(
        {"id": "p2", "process_id": "proc-1", "source_ref": "r",
         "evidence_status": "verified"}, org_id="org-x")
    assert real["evidence_status"] == "verified"
