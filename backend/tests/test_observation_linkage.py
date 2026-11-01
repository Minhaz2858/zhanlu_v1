"""Phase 3E — observed-execution linkage.

Evidence-based matching between actual execution records (Nl2sqlQueryLog,
AgentRun) and persisted enterprise processes. DETERMINISTIC — no LLM.
Honesty contracts: proposed processes never auto-verify; observed ≠
verified ≠ documented; ambiguous or insufficient evidence stays
unmatched; historical rows are untouched until explicitly observed.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.enterprise_process import (
    BusinessProcess, ProcessActivity, ProcessEvidence, ProcessLink)
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyAction, OntologyObject
from app.services.enterprise.observations import (
    match_execution,
    process_observations,
    record_observation,
)

ORG = "org-acme"
ORG2 = "org-other"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        Nl2sqlQueryLog.__table__, AgentRun.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__, ProcessLink.__table__,
        OntologyObject.__table__, OntologyAction.__table__,
        BusinessRule.__table__, AuditEvent.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _process(db, pid, *, org=ORG, status="verified", discovery="documented"):
    db.add(BusinessProcess(id=pid, org_id=org, name=f"Process {pid}",
                           discovery_status=discovery,
                           evidence_status=status))
    db.add(ProcessEvidence(
        id=f"{pid}-ev1", org_id=org, process_id=pid, activity_seq=1,
        evidence_kind="document_chunk", source_ref="doc://kb-1/terms",
        quote="terms", confidence=0.9, evidence_status="verified"))
    db.commit()


def _object_evidence(db, pid, object_id, *, org=ORG, status="verified"):
    db.add(ProcessEvidence(
        id=f"{pid}-oev-{object_id}", org_id=org, process_id=pid,
        evidence_kind="ontology_link", source_ref=f"ontology:{object_id}",
        quote=None, confidence=0.9, evidence_status=status))
    db.commit()


def _table_evidence(db, pid, table, *, org=ORG, status="verified"):
    db.add(ProcessEvidence(
        id=f"{pid}-tev-{table}", org_id=org, process_id=pid,
        evidence_kind="table_catalog", source_ref=f"table://{table}",
        quote=None, confidence=0.9, evidence_status=status))
    db.commit()


# 1-2. Explicit process / activity linkage -------------------------------------------

def test_explicit_process_linkage(db):
    _process(db, "proc-1")
    d = match_execution(db, ORG, explicit_process_id="proc-1",
                        explicit_activity_seq=2)
    assert d["matched"] is True
    assert d["process_id"] == "proc-1" and d["activity_seq"] == 2
    assert d["confidence"] == 1.0 and d["method"] == "explicit"
    assert d["evidence"]


def test_explicit_linkage_to_unknown_process_is_unmatched(db):
    d = match_execution(db, ORG, explicit_process_id="proc-none")
    assert d["matched"] is False
    assert "not found" in d["unmatched_reason"]


# 3. Verified ontology-object relationship matching ------------------------------------

def test_verified_ontology_object_matches_process(db):
    _process(db, "proc-1")
    _object_evidence(db, "proc-1", "oo-1")
    d = match_execution(db, ORG, object_id="oo-1")
    assert d["matched"] is True and d["process_id"] == "proc-1"
    assert d["method"] == "ontology_object"
    assert d["confidence"] == 0.8
    # PROPOSED object evidence never matches (only verified relationships)
    _process(db, "proc-2")
    _object_evidence(db, "proc-2", "oo-2", status="proposed")
    d2 = match_execution(db, ORG, object_id="oo-2")
    assert d2["matched"] is False


# 4. Datasource/table lineage + tool binding --------------------------------------------

def test_table_lineage_matches_process(db):
    _process(db, "proc-1")
    _table_evidence(db, "proc-1", "sales_order")
    d = match_execution(db, ORG, tables=["sales_order", "sys_config"],
                        datasource_id="kb-1")
    assert d["matched"] is True and d["process_id"] == "proc-1"
    assert d["method"] == "lineage" and d["confidence"] == 0.4


def test_tool_binding_matches_via_verified_object(db):
    _process(db, "proc-1")
    _object_evidence(db, "proc-1", "oo-1")
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="dom-1",
                          name="PurchaseOrder"))
    db.add(OntologyAction(id="act-1", org_id=ORG, object_id="oo-1",
                          name="CancelOrder", tool_name="cancel_order_tool",
                          risk_level="write"))
    db.commit()
    d = match_execution(db, ORG, tool_names=["cancel_order_tool"])
    assert d["matched"] is True and d["process_id"] == "proc-1"
    assert d["method"] == "tool_binding" and d["confidence"] == 0.6


# 5. Insufficient / ambiguous evidence stays unmatched -----------------------------------

def test_insufficient_evidence_unmatched(db):
    _process(db, "proc-1")
    d = match_execution(db, ORG, datasource_id="kb-1", tables=[])
    assert d["matched"] is False
    assert d["process_id"] is None and d["confidence"] is None
    assert d["unmatched_reason"]


def test_ambiguous_evidence_does_not_force_match(db):
    _process(db, "proc-1")
    _process(db, "proc-2")
    _object_evidence(db, "proc-1", "oo-1")
    _object_evidence(db, "proc-2", "oo-1")  # same object backs two processes
    d = match_execution(db, ORG, object_id="oo-1")
    assert d["matched"] is False
    assert "ambiguous" in d["unmatched_reason"]


# 6. Cross-org isolation -------------------------------------------------------------------

def test_cross_org_isolation(db):
    _process(db, "proc-evil", org=ORG2)
    _object_evidence(db, "proc-evil", "oo-evil", org=ORG2)
    _table_evidence(db, "proc-evil", "sales_order", org=ORG2)
    assert match_execution(db, ORG, object_id="oo-evil")["matched"] is False
    assert match_execution(db, ORG, tables=["sales_order"])["matched"] is False
    assert process_observations(db, ORG, "proc-evil") is None


# 7. record_observation stamps honestly; historical rows preserved ------------------------

def test_record_observation_stamps_and_preserves_history(db):
    _process(db, "proc-1")
    _object_evidence(db, "proc-1", "oo-1")
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="dom-1",
                          name="PurchaseOrder"))
    log = Nl2sqlQueryLog(id="log-1", org_id=ORG, datasource_id="kb-1",
                         question="q")
    db.add(log)
    db.add(AgentRun(run_id="run-old", agent_name="helper", task="t",
                    caller_context='{"org_id": "%s"}' % ORG))
    db.commit()
    # historical rows WITHOUT observation: linkage columns stay NULL
    old = db.query(AgentRun).filter_by(run_id="run-old").one()
    assert old.process_id is None and old.match_method is None

    d = record_observation(db, ORG, "nl2sql_query", "log-1")
    assert d["matched"] is False  # no signals → honest unmatched, nothing stamped
    assert db.get(Nl2sqlQueryLog, "log-1").process_id is None

    d2 = record_observation(db, ORG, "agent_run", "run-old",
                            signals={"object_id": "oo-1"})
    assert d2["matched"] is True
    stamped = db.query(AgentRun).filter_by(run_id="run-old").one()
    assert stamped.process_id == "proc-1"
    assert stamped.match_method == "ontology_object"
    assert stamped.match_confidence == 0.8


def test_record_observation_idempotent_never_overwrites_explicit(db):
    _process(db, "proc-1")
    _process(db, "proc-2")
    _object_evidence(db, "proc-2", "oo-1")
    db.add(Nl2sqlQueryLog(id="log-2", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1",
                          activity_seq=3, match_confidence=1.0,
                          match_method="explicit"))
    db.commit()
    d = record_observation(db, ORG, "nl2sql_query", "log-2",
                           signals={"object_id": "oo-1"})
    row = db.get(Nl2sqlQueryLog, "log-2")
    assert row.process_id == "proc-1"  # explicit link never overwritten
    assert row.activity_seq == 3
    assert d["method"] == "explicit"


# 8-9. Lifecycle honesty: observations never promote statuses ------------------------------

def test_observation_never_promotes_process_status(db):
    _process(db, "proc-prop", status="proposed", discovery="proposed")
    _object_evidence(db, "proc-prop", "oo-1")
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="dom-1", name="X"))
    db.add(AgentRun(run_id="run-1", agent_name="helper", task="t",
                    caller_context='{"org_id": "%s"}' % ORG))
    db.commit()
    d = record_observation(db, ORG, "agent_run", "run-1",
                           signals={"object_id": "oo-1"})
    assert d["matched"] is True
    p = db.query(BusinessProcess).filter_by(id="proc-prop").one()
    assert p.evidence_status == "proposed"  # NEVER promoted
    assert p.discovery_status == "proposed"  # observed ≠ verified/documented


def test_observation_keeps_verified_status_unchanged(db):
    _process(db, "proc-1")
    _object_evidence(db, "proc-1", "oo-1")
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="dom-1", name="X"))
    db.add(AgentRun(run_id="run-1", agent_name="helper", task="t",
                    caller_context='{"org_id": "%s"}' % ORG))
    db.commit()
    record_observation(db, ORG, "agent_run", "run-1",
                       signals={"object_id": "oo-1"})
    p = db.query(BusinessProcess).filter_by(id="proc-1").one()
    assert p.evidence_status == "verified"  # unchanged


# 10-12. Observations summary payload --------------------------------------------------------

def test_process_observations_summary(db):
    _process(db, "proc-1")
    db.add(Nl2sqlQueryLog(id="l1", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1",
                          activity_seq=1, match_confidence=1.0,
                          match_method="explicit"))
    db.add(Nl2sqlQueryLog(id="l2", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1",
                          match_confidence=0.8, match_method="ontology_object"))
    db.add(AgentRun(run_id="r1", agent_name="a", task="t",
                    caller_context='{"org_id": "%s"}' % ORG,
                    process_id="proc-1", activity_seq=2,
                    match_confidence=0.6, match_method="tool_binding"))
    db.add(AgentRun(run_id="r-other", agent_name="a", task="t",
                    caller_context='{"org_id": "%s"}' % ORG2,
                    process_id="proc-1"))  # cross-org run must NOT count
    db.commit()
    obs = process_observations(db, ORG, "proc-1")
    assert obs["execution_count"] == 3
    assert obs["activity_counts"] == {"1": 1, "2": 1}
    assert obs["confidence_distribution"]["high"] == 2    # 1.0 + 0.8
    assert obs["confidence_distribution"]["medium"] == 1  # 0.6
    assert obs["time_range"]["count"] == 3
    refs = obs["references"]
    assert len(refs) == 3
    assert {r["record_type"] for r in refs} == {"nl2sql_query", "agent_run"}
    # no sensitive execution payloads leak into references
    for r in refs:
        assert "question" not in r and "generated_sql" not in r \
            and "result" not in r


def test_empty_observation_history_is_honest(db):
    _process(db, "proc-1")
    obs = process_observations(db, ORG, "proc-1")
    assert obs["execution_count"] == 0
    assert obs["activity_counts"] == {}
    assert obs["confidence_distribution"] == {"high": 0, "medium": 0, "low": 0}
    assert obs["references"] == []
    assert obs["time_range"] is None
    assert "note" in obs


def test_no_fabricated_sequence_edges(db):
    # two executions hit the same activity_seq out of order — the summary
    # only counts, it never invents ordering/edges between activities
    _process(db, "proc-1")
    db.add(ProcessActivity(id="pa-1", org_id=ORG, process_id="proc-1",
                           seq=1, name="A"))
    db.add(Nl2sqlQueryLog(id="l1", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1", activity_seq=1))
    db.commit()
    obs = process_observations(db, ORG, "proc-1")
    assert "edges" not in obs and "sequence" not in obs
    assert obs["activity_counts"] == {"1": 1}


# Enterprise map / process detail integration ----------------------------------------

def test_process_detail_carries_observed_summary_separately(db):
    from app.services.ontology.enterprise_discovery import get_process

    _process(db, "proc-1")
    db.add(Nl2sqlQueryLog(id="l1", org_id=ORG, datasource_id="kb-1",
                          question="q", process_id="proc-1",
                          activity_seq=1, match_confidence=1.0,
                          match_method="explicit"))
    db.commit()
    detail = get_process(db, ORG, "proc-1")
    obs = detail["observed_executions"]
    # OBSERVED evidence is separate from documented/configured status —
    # the process's own statuses are untouched by observation
    assert detail["discovery_status"] == "documented"
    assert detail["evidence_status"] == "verified"
    assert obs["execution_count"] == 1
    assert obs["activity_counts"] == {"1": 1}
    assert "note" in obs

    # no observations → honest empty summary (process detail unchanged
    # otherwise)
    _process(db, "proc-quiet")
    detail2 = get_process(db, ORG, "proc-quiet")
    assert detail2["observed_executions"]["execution_count"] == 0
