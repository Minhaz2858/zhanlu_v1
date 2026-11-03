"""Phase 8 T8-8 — canonical AgentRun provenance envelope (prov-v1).

finalize_run is the single terminal choke point: after committing the
terminal status it writes an org-stamped, deterministic prov-v1 envelope
onto ``agent_runs.provenance_json``. Trust rules:
* status copied VERBATIM (failed/partial runs never look completed);
* tool entries derived from AgentRunStep rows (retrieved=True — the tool
  really executed; influenced/supports_answer NEVER claimed here);
* no CoT, no raw tool inputs — refs and hashes only;
* envelope failure never breaks finalize (fail-open, 7C E1 pattern);
* historical rows (NULL provenance) remain readable.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.services.harness.run_service import (
    AgentRunService, build_run_provenance)

svc = AgentRunService()
ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentRun.__table__, AgentRunStep.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _make_run(db, status="completed", org=ORG, parent=None):
    run_id = svc.create_run_record(
        agent_name="analyst", task="check orders", mode="inline",
        parent_run_id=parent,
        caller_context={"org_id": org, "user_id": "u1",
                        "conversation_id": "c1"},
        db=db,
    )
    if status != "queued":
        svc.finalize_run(run_id, status=status, db=db)
    return run_id


def _prov(db, run_id):
    raw = db.query(AgentRun).filter_by(run_id=run_id).one().provenance_json
    return json.loads(raw) if raw else None


def test_finalize_writes_valid_prov_v1_envelope(db):
    run_id = _make_run(db)
    prov = _prov(db, run_id)
    assert prov and prov["version"] == "prov-v1"
    assert prov["run"]["org_id"] == ORG
    assert prov["run"]["status"] == "completed"
    assert prov["run"]["agent_name"] == "analyst"
    assert prov["run"]["conversation_id"] == "c1"


def test_failed_run_status_is_verbatim_never_success_looking(db):
    run_id = _make_run(db, status="failed")
    prov = _prov(db, run_id)
    assert prov["run"]["status"] == "failed"


def test_tool_steps_become_retrieved_only_entries(db):
    run_id = _make_run(db, status="queued")
    svc.append_step(run_id, "tool_call", step_index=1,
                    tool_name="ask_data_agent", db=db)
    svc.append_step(run_id, "llm_call", step_index=2, db=db)
    svc.finalize_run(run_id, status="completed", db=db)
    prov = _prov(db, run_id)
    tool_entries = [e for e in prov["entries"]
                    if e["source_type"] == "agent_tool_call"]
    assert len(tool_entries) == 1
    e = tool_entries[0]
    assert e["retrieved"] is True          # the tool really executed
    assert "influenced" not in e           # unknown at this layer — honest
    assert "supports_answer" not in e      # NEVER claimed automatically
    assert e["source_ref"].startswith(f"agent_run:{run_id}:step:")
    assert e["org_id"] == ORG


def test_no_steps_no_entries_no_empty_envelope_lies(db):
    run_id = _make_run(db)
    prov = _prov(db, run_id)
    assert prov["entries"] == []
    assert "evidence" not in json.dumps(prov).lower()


def test_parent_run_linkage(db):
    parent_id = _make_run(db)
    child_id = _make_run(db, parent=parent_id)
    prov = _prov(db, child_id)
    assert prov["run"]["parent_run_id"] == parent_id


def test_serialization_deterministic(db):
    run_id = _make_run(db, status="queued")
    svc.append_step(run_id, "tool_call", step_index=1,
                    tool_name="search_documents", db=db)
    row = db.query(AgentRun).filter_by(run_id=run_id).one()
    one = build_run_provenance(row, db)
    two = build_run_provenance(row, db)
    assert one == two


def test_envelope_failure_never_breaks_finalize(db, monkeypatch):
    run_id = _make_run(db, status="queued")
    import app.services.harness.run_service as rs
    monkeypatch.setattr(rs, "build_run_provenance",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    assert svc.finalize_run(run_id, status="completed", db=db) is True
    row = db.query(AgentRun).filter_by(run_id=run_id).one()
    assert row.status == "completed"


def test_historical_rows_without_envelope_unchanged(db):
    run_id = svc.create_run_record(
        agent_name="legacy", task="t",
        caller_context={"org_id": ORG}, db=db)
    svc.finalize_run(run_id, status="completed", db=db)
    row = db.query(AgentRun).filter_by(run_id=run_id).one()
    # finalize DID write an envelope; simulate a historical row instead
    row.provenance_json = None
    db.commit()
    assert db.query(AgentRun).filter_by(run_id=run_id).one() \
        .provenance_json is None  # readable, not fabricated
