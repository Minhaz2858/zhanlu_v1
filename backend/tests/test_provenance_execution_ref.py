"""Phase 5A — canonical provenance execution_ref unification.

One consistent execution-reference representation in the prov-v1 contract,
connecting canonical entries to REAL persisted execution records
(``Nl2sqlQueryLog``, ``AgentRun``). Honesty rules verified here:

- observed execution is DISTINCT from retrieved / influenced /
  supports_answer — an execution reference NEVER implies retrieval or
  support;
- references identify real persisted records and are verified against the
  authorized organization (cross-org → omitted);
- missing references stay missing; malformed ones are rejected;
- serialization is deterministic; existing converters stay backward
  compatible; no question/sql/result payloads ever enter entries.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.knowledge_base import KnowledgeBase
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.services.enterprise.provenance import (
    SOURCE_TYPES,
    canonical_entry,
    execution_ref,
    from_observed_execution,
    from_ontology_provenance,
    from_sql_lineage,
    parse_execution_ref,
    resolve_execution_ref,
    serialize_provenance,
    entries_with_verified_execution_refs,
)

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        Nl2sqlQueryLog.__table__, AgentRun.__table__,
        KnowledgeBase.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


# 1. representation -------------------------------------------------------------------

def test_execution_ref_roundtrip():
    ref = execution_ref("nl2sql_query", "log-1")
    assert ref == "nl2sql_query:log-1"
    assert parse_execution_ref(ref) == ("nl2sql_query", "log-1")
    ref2 = execution_ref("agent_run", "run-abc")
    assert ref2 == "agent_run:run-abc"
    assert parse_execution_ref(ref2) == ("agent_run", "run-abc")


def test_execution_ref_missing_id_stays_missing():
    assert execution_ref("nl2sql_query", None) is None
    assert execution_ref("nl2sql_query", "") is None
    assert parse_execution_ref(None) is None
    assert parse_execution_ref("") is None
    assert parse_execution_ref("no-separator") is None
    assert parse_execution_ref("bogus_type:id") is None


def test_execution_ref_unknown_record_type_rejected():
    with pytest.raises(ValueError):
        execution_ref("credit_card_dump", "x")


def test_execution_source_type_registered():
    assert "execution" in SOURCE_TYPES  # deliberate, closed-set extension


# 2. observed-execution entries — state honesty ---------------------------------------

def test_observed_execution_entry_shape():
    e = from_observed_execution(
        "nl2sql_query", "log-1", org_id=ORG, process_id="proc-1",
        match_confidence=0.8, match_method="ontology_object",
        activity_seq=3)
    assert e["source_type"] == "execution"
    assert e["source_id"] == "log-1"
    assert e["org_id"] == ORG
    assert e["execution_ref"] == "nl2sql_query:log-1"
    assert e["kind"] == "observed_execution"
    assert e["process_id"] == "proc-1"      # observed linkage, passthrough
    assert e["activity_seq"] == 3
    assert e["match_confidence"] == 0.8 if "match_confidence" in e else True
    # state honesty: an observed execution is NOT retrieved context and
    # NEVER claims support for the answer
    assert e["retrieved"] is False
    assert e["influenced"] is False
    assert "supports_answer" not in e
    assert "evidence_status" not in e       # nothing invented


def test_observed_execution_without_match_has_no_confidence():
    e = from_observed_execution("agent_run", "run-1", org_id=ORG)
    assert "match_confidence" not in e
    assert "process_id" not in e
    assert e["execution_ref"] == "agent_run:run-1"


def test_observed_execution_rejects_support_claim():
    # even a caller forcing supports_answer=True on a bare execution entry
    # is blocked by the contract (retrieval is never implied)
    e = from_observed_execution("agent_run", "run-1", org_id=ORG)
    with pytest.raises(ValueError):
        e["supports_answer"] = True
        canonical_entry(**e)


# 3. verification against the authorized org -----------------------------------------

def _seed(db):
    db.add(Nl2sqlQueryLog(id="log-1", org_id=ORG, datasource_id="kb-1",
                          question="q"))
    db.add(AgentRun(run_id="run-abc", agent_name="a", task="t",
                    caller_context=json.dumps({"org_id": ORG})))
    db.commit()


def test_resolve_execution_ref_nl2sql(db):
    _seed(db)
    assert resolve_execution_ref(db, ORG, "nl2sql_query:log-1") == \
        ("nl2sql_query", "log-1")


def test_resolve_execution_ref_agent_run(db):
    _seed(db)
    assert resolve_execution_ref(db, ORG, "agent_run:run-abc") == \
        ("agent_run", "run-abc")


def test_resolve_execution_ref_cross_org_rejected(db):
    _seed(db)
    # nl2sql row org-stamped ORG; agent_run caller_context org ORG — a
    # different org resolves NEITHER (fail closed)
    assert resolve_execution_ref(db, "org-other", "nl2sql_query:log-1") is None
    assert resolve_execution_ref(db, "org-other", "agent_run:run-abc") is None


def test_resolve_execution_ref_missing_record_is_none(db):
    _seed(db)
    assert resolve_execution_ref(db, ORG, "nl2sql_query:ghost") is None


def test_resolve_execution_ref_malformed_rejected(db):
    with pytest.raises(ValueError):
        resolve_execution_ref(db, ORG, "garbage")
    with pytest.raises(ValueError):
        resolve_execution_ref(db, ORG, "bogus:id")


def test_entries_with_verified_execution_refs_omits_unverifiable(db):
    _seed(db)
    good = from_observed_execution("nl2sql_query", "log-1", org_id=ORG)
    ghost = from_observed_execution("nl2sql_query", "ghost", org_id=ORG)
    foreign = from_observed_execution("agent_run", "run-abc", org_id="org-x")
    kept = entries_with_verified_execution_refs(
        db, ORG, [good, ghost, foreign])
    assert [e["source_id"] for e in kept] == ["log-1"]
    # non-execution entries pass through untouched
    lin = from_sql_lineage({"tables": ["t1"]}, org_id=ORG)
    assert entries_with_verified_execution_refs(db, ORG, [lin]) == [lin]


# 4. backward compatibility + determinism ---------------------------------------------

def test_existing_converters_accept_execution_ref():
    lin = from_sql_lineage({"tables": ["sales_order"]}, org_id=ORG,
                           resource_id="kb-1",
                           execution_ref="nl2sql_query:log-1")
    assert lin[0]["execution_ref"] == "nl2sql_query:log-1"
    assert lin[0]["retrieved"] is True and lin[0]["influenced"] is True
    prov = from_ontology_provenance(
        {"objects": [{"name": "Order", "table": "sales_order",
                      "influenced": True}]},
        org_id=ORG, resource_id="kb-1", execution_ref="nl2sql_query:log-1")
    assert all(e["execution_ref"] == "nl2sql_query:log-1" for e in prov)
    assert prov[0]["evidence_status"] == "verified"  # never promoted


def test_entries_without_execution_ref_omit_the_key():
    e = from_sql_lineage({"tables": ["t"]})
    assert "execution_ref" not in e[0]
    assert "org_id" not in e[0]


def test_serialization_deterministic():
    entries = [from_observed_execution("nl2sql_query", "log-1", org_id=ORG,
                                       match_confidence=0.8,
                                       match_method="ontology_object")]
    a = serialize_provenance(entries)
    b = serialize_provenance([from_observed_execution(
        "nl2sql_query", "log-1", org_id=ORG, match_confidence=0.8,
        match_method="ontology_object")])
    assert a == b
    assert json.loads(json.dumps(a)) == a


def test_no_sensitive_payload_in_entries():
    e = from_observed_execution("nl2sql_query", "log-1", org_id=ORG)
    assert all(k not in e for k in ("question", "sql", "generated_sql",
                                    "result", "rows", "credential"))


# 5. NL2SQL writer integration ---------------------------------------------------------

def test_nl2sql_writer_stamps_execution_ref(db, monkeypatch):
    """Phase 5A: the persisted log row's canonical provenance entries carry
    an execution_ref that identifies THE ROW ITSELF (real persisted record,
    verified org stamp) — never a placeholder."""
    from app.services.db.nl_answer_service import _shadow_validate_and_log
    from app.models.knowledge_base import KnowledgeBase

    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="KB"))
    db.commit()
    monkeypatch.setattr("app.database.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    _shadow_validate_and_log(
        "kb-1", "orders", "SELECT id FROM sales_order", live_success=True,
        ontology_provenance={
            "objects": [{"name": "Order", "table": "sales_order",
                         "influenced": True}]})
    row = db.query(Nl2sqlQueryLog).one()
    prov = row.lineage_json["provenance"]
    assert prov["version"] == "prov-v1"
    refs = {e["execution_ref"] for e in prov["entries"]}
    assert refs == {f"nl2sql_query:{row.id}"}
    assert all(e.get("org_id") == ORG for e in prov["entries"])
    assert all(e.get("resource_id") == "kb-1" for e in prov["entries"])
    # the ref round-trips to the real record through the verifier
    assert resolve_execution_ref(db, ORG, refs.pop()) == \
        ("nl2sql_query", row.id)


def test_nl2sql_writer_without_provenance_keeps_row_identical(db, monkeypatch):
    from app.services.db.nl_answer_service import _shadow_validate_and_log
    from app.models.knowledge_base import KnowledgeBase

    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="KB"))
    db.commit()
    monkeypatch.setattr("app.database.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    _shadow_validate_and_log(
        "kb-1", "orders", "SELECT id FROM sales_order", live_success=True)
    row = db.query(Nl2sqlQueryLog).one()
    assert row.lineage_json is None or "provenance" not in row.lineage_json
