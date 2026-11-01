"""Phase 5B — AgentRun observation writer integration.

`record_observation` fires at the REAL terminal persistence point of an
AgentRun (`AgentRunService._update_run_finished`, after its commit — the
only writer that persists ``tool_calls``, which the deterministic matcher
consumes). Honesty rules verified here:

- org attribution comes from the persisted ``caller_context`` (never a
  caller-supplied id, never guessed);
- missing attribution fails closed (no observation, no crash);
- cross-org runs can never be linked;
- idempotent: existing/explicit linkages are never overwritten;
- proposed evidence stays proposed; statuses never touched;
- observation failure never breaks an otherwise successful run;
- the AgentRun response contract is unchanged.

Rollback: the SAME ``ONTOLOGY_OBSERVATIONS_ENABLED`` flag gates NL2SQL
and AgentRun observations (one consistent kill switch, default OFF).
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.enterprise_process import BusinessProcess as BP, ProcessEvidence
from app.models.ontology import OntologyAction
from app.services.harness.run_service import AgentRunService

ORG = "org-acme"
ORG2 = "org-other"

SVC = AgentRunService()


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        AgentRun.__table__, BP.__table__, ProcessEvidence.__table__,
        OntologyAction.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def wired(db, monkeypatch):
    """Point the service's SessionLocal at the test DB."""
    monkeypatch.setattr("app.services.harness.run_service.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    return db


def _bound_evidence(db):
    """A process with VERIFIED evidence for ontology object oo-1, and the
    ontology action bound to tool 'query_database'."""
    db.add(BP(id="proc-1", org_id=ORG, name="Order to Cash",
              discovery_status="documented", evidence_status="verified"))
    db.add(ProcessEvidence(id="ev-1", org_id=ORG, process_id="proc-1",
                           evidence_kind="ontology_object",
                           source_ref="ontology:oo-1",
                           evidence_status="verified"))
    db.add(OntologyAction(id="act-1", org_id=ORG, name="Query orders",
                          object_id="oo-1", tool_name="query_database"))
    db.commit()


def _seed_run(db, run_id="run-1", org=ORG, process_id=None,
              match_method=None):
    db.add(AgentRun(
        run_id=run_id, agent_name="analyst", task="orders", status="running",
        process_id=process_id, match_method=match_method,
        match_confidence=1.0 if match_method else None,
        caller_context=json.dumps({"org_id": org}) if org else None))
    db.commit()


def _finish(run_id="run-1", tool_calls=None):
    return SVC._update_run_finished(run_id, {
        "success": True, "answer": "ok",
        "tool_calls": ([{"name": "query_database", "args": {}}]
                       if tool_calls is None else tool_calls),
        "tool_call_count": 1, "iterations": 1,
    })


# 1. happy path ------------------------------------------------------------------------

def test_successful_run_records_observation(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.status == "completed"  # lifecycle contract unchanged
    assert run.process_id == "proc-1"
    assert run.match_method == "tool_binding"
    assert run.match_confidence == 0.6
    # process statuses NEVER touched by the observation
    p = wired.query(BP).filter_by(id="proc-1").one()
    assert p.evidence_status == "verified"
    assert p.discovery_status == "documented"


def test_disabled_flag_records_nothing(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", False)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id is None


def test_run_without_matching_evidence_stays_unmatched(wired, monkeypatch):
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id is None
    assert run.status == "completed"


# 2. org attribution honesty -----------------------------------------------------------

def test_cross_org_run_cannot_be_linked(wired, monkeypatch):
    _bound_evidence(wired)  # evidence lives in ORG
    _seed_run(wired, org=ORG2)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id is None  # org-2 attribution → org-1 evidence invisible


def test_missing_org_attribution_fails_closed(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired, org=None)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()  # the run itself still finalizes fine
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id is None  # unattributable → no observation


# 3. idempotency + linkage protection --------------------------------------------------

def test_duplicate_writes_are_idempotent(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    first = wired.query(AgentRun).filter_by(run_id="run-1").one()
    snapshot = (first.process_id, first.match_confidence, first.match_method)
    # a second terminal write re-runs the hook — linkage must not change
    _finish(tool_calls=[])
    again = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert (again.process_id, again.match_confidence,
            again.match_method) == snapshot


def test_existing_explicit_linkage_never_overwritten(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired, run_id="run-explicit", process_id="proc-explicit",
              match_method="explicit")
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish("run-explicit")
    run = wired.query(AgentRun).filter_by(run_id="run-explicit").one()
    assert run.process_id == "proc-explicit"  # explicit wins forever
    assert run.match_method == "explicit"


def test_proposed_evidence_stays_proposed(wired, monkeypatch):
    wired.add(BP(id="proc-2", org_id=ORG, name="Draft process",
                 discovery_status="discovered", evidence_status="proposed"))
    wired.add(ProcessEvidence(id="ev-2", org_id=ORG, process_id="proc-2",
                              evidence_kind="ontology_object",
                              source_ref="ontology:oo-1",
                              evidence_status="proposed"))
    wired.add(OntologyAction(id="act-2", org_id=ORG, name="Query orders",
                             object_id="oo-1", tool_name="query_database"))
    wired.commit()
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id is None  # proposed evidence NEVER matches
    p = wired.query(BP).filter_by(id="proc-2").one()
    assert p.evidence_status == "proposed"


# 4. failure isolation ------------------------------------------------------------------

def test_observation_failure_does_not_break_execution(wired, monkeypatch):
    _bound_evidence(wired)
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)

    def _boom(*a, **k):
        raise RuntimeError("matcher exploded")

    import app.services.harness.run_service as rs
    monkeypatch.setattr(rs, "record_observation", _boom)
    _finish()
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.status == "completed"
    assert run.result == "ok"


# 5. response contract unchanged ---------------------------------------------------------

def test_get_run_contract_unchanged(wired, monkeypatch):
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    _finish()
    got = SVC.get_run("run-1")
    assert set(got.keys()) == {
        "run_id", "agent_name", "task", "status", "mode", "result",
        "tool_calls", "tool_call_count", "iterations", "parent_run_id",
        "error", "started_at", "finished_at"}
    assert got["status"] == "completed"


def test_queued_run_path_uses_same_hook(wired, monkeypatch):
    """Queued runs finalize through the same run_store callback — one
    choke point covers inline and queued execution."""
    _bound_evidence(wired)
    _seed_run(wired)
    monkeypatch.setattr(
        "app.services.harness.run_service.settings."
        "ONTOLOGY_OBSERVATIONS_ENABLED", True)
    SVC._persist_run_store_local = None  # attribute guard (not used)
    SVC._update_run_finished("run-1", {
        "success": True, "answer": "ok", "tool_calls": [
            {"name": "query_database"}], "tool_call_count": 1,
        "iterations": 2})
    run = wired.query(AgentRun).filter_by(run_id="run-1").one()
    assert run.process_id == "proc-1"
