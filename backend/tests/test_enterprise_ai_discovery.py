"""EBPM Increment 2 — AI-assisted discovery engine (mocked LLM, deterministic).

Covers: evidence extraction (catalog/ontology/document), honest degradation
(no evidence / malformed LLM), idempotent re-discovery, verified-record
preservation, org isolation, partial failure isolation, and evidence
traceability. No real LLM calls.
"""

import re
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.agent_run import AgentRun
from app.models.automation_task import AutomationTask
from app.models.enterprise_process import (
    BusinessCapability, BusinessProcess, EnterpriseResource, OrgUnit,
    ProcessActivity, ProcessEvidence,
)

from app.models.knowledge_catalog import KBTableRelation
from app.routers.ontology import router as ontology_router
from app.services.ontology import enterprise_discovery as ed
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin")


@pytest.fixture()
def env():
    eng = make_engine()
    Base.metadata.create_all(eng, tables=[
        EnterpriseResource.__table__, AutomationTask.__table__,
        AgentRun.__table__, KBTableRelation.__table__,
        OrgUnit.__table__, BusinessCapability.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__,
    ])
    s = make_session(eng)
    data_path = tempfile.mktemp(suffix=".db")
    create_data_db(data_path)
    build_sandbox(s, data_path)
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _llm_from_prompt(prompt, schema=None, endpoint=None):
    """A deterministic fake LLM: proposes one process + one org unit citing
    ONLY evidence ids actually present in the prompt (traceable)."""
    ids = sorted(set(re.findall(r"\[ev-\d+\]", prompt)))
    use = [i[1:-1] for i in ids][:12]  # strip brackets → real evidence ids
    return {
        "org_units": [{
            "name": "Procurement Department", "unit_type": "department",
            "responsibilities": "Supplier management and purchasing",
            "evidence_ids": use[:1],
        }],
        "capabilities": [{
            "name": "Procurement",
            "description": "Purchase goods from suppliers",
            "evidence_ids": use[:2],
        }],
        "processes": [{
            "name": "Procure-to-Pay",
            "purpose": "Buy goods from suppliers and pay them",
            "trigger_event": "Purchase need identified",
            "objective": "Ensure supply at best cost",
            "start_condition": "Requisition created",
            "completion_condition": "Invoice paid",
            "process_family": "procure-to-pay",
            "discovery_status": "documented",
            "evidence_ids": use,
            "activities": [
                {"seq": 1, "name": "Create requisition",
                 "activity_type": "human_task",
                 "performer": "Procurement Department",
                 "evidence_ids": use[:1]},
                {"seq": 2, "name": "Approve purchase",
                 "activity_type": "approval",
                 "performer": "Procurement Department",
                 "evidence_ids": use[:2]},
                {"seq": 3, "name": "Record goods receipt",
                 "activity_type": "system_task",
                 "evidence_ids": use[:3]},
            ],
        }],
    }


def test_discovery_from_catalog_ontology_and_document_evidence(env):
    s = env
    result = ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    assert result["status"] == "ok" and result["llm_ok"] is True
    assert result["evidence_extracted"] > 0
    assert result["proposals_created"] == 3  # unit + capability + process
    assert result["needs_review"] == 1

    # Every evidence kind available in the sandbox is represented.
    kinds = set()
    for pr in s.query(BusinessProcess).filter(
            BusinessProcess.org_id == ORG).all():
        for ev in s.query(ProcessEvidence).filter(
                ProcessEvidence.process_id == pr.id).all():
            kinds.add(ev.evidence_kind)
            # traceability: the quote is verbatim extracted evidence
            assert ev.quote
    assert "table_catalog" in kinds
    assert "ontology_link" in kinds
    assert "document_catalog" in kinds

    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG, BusinessProcess.name == "Procure-to-Pay").one()
    assert proc.evidence_status == "proposed"  # never auto-verified
    assert proc.process_family == "procure-to-pay"
    acts = s.query(ProcessActivity).filter(
        ProcessActivity.org_id == ORG,
        ProcessActivity.process_id == proc.id).all()
    assert {a.activity_type for a in acts} >= {"human_task", "approval",
                                               "system_task"}
    prov = proc.provenance_json
    assert prov["discovery_method"] == "ai_proposal"
    assert prov["evidence_ids"]  # traceable to extracted evidence


def test_no_evidence_is_honest_empty(env):
    s = env
    result = ed.propose_operating_model(s, "org-empty", llm_fn=_llm_from_prompt)
    assert result["status"] == "insufficient_evidence"
    assert result["llm_ok"] is False  # LLM never even consulted
    assert result["proposals_created"] == 0
    assert s.query(BusinessProcess).filter(
        BusinessProcess.org_id == "org-empty").count() == 0


def test_malformed_llm_response_degrades_honestly(env):
    s = env
    result = ed.propose_operating_model(
        s, ORG, llm_fn=lambda p, schema=None, endpoint=None: {})
    assert result["status"] == "llm_malformed"
    assert result["proposals_created"] == 0
    # inventory was still refreshed (partial completion is explicit)
    assert s.query(EnterpriseResource).filter(
        EnterpriseResource.org_id == ORG).count() > 0


def test_duplicate_discovery_is_idempotent(env):
    s = env
    first = ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    assert first["proposals_created"] == 3
    second = ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    assert second["proposals_created"] == 0
    assert second["proposals_updated"] == 3
    assert second["evidence_created"] == 0  # same source_refs, no duplicates
    assert s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).count() == 1
    assert s.query(ProcessEvidence).filter(
        ProcessEvidence.org_id == ORG).count() == first["evidence_created"]


def test_verified_records_are_never_weakened(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    proc.evidence_status = "verified"  # human confirmed
    proc.purpose = "HUMAN-CONFIRMED PURPOSE"
    s.commit()

    # A different AI proposal must not overwrite the human confirmation.
    def other_llm(prompt, schema=None, endpoint=None):
        out = _llm_from_prompt(prompt)
        out["processes"][0]["purpose"] = "AI-GENERATED PURPOSE"
        return out

    ed.propose_operating_model(s, ORG, llm_fn=other_llm)
    s.expire_all()
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    assert proc.purpose == "HUMAN-CONFIRMED PURPOSE"
    assert proc.evidence_status == "verified"


def test_unknown_evidence_ids_are_filtered(env):
    s = env

    def hallucinated_llm(prompt, schema=None, endpoint=None):
        out = _llm_from_prompt(prompt)
        out["processes"][0]["evidence_ids"] = ["ev-999", "ev-998"]
        return out

    result = ed.propose_operating_model(s, ORG, llm_fn=hallucinated_llm)
    assert result["status"] == "ok"
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    assert proc.provenance_json["evidence_ids"] == []  # fabrication dropped
    assert s.query(ProcessEvidence).filter(
        ProcessEvidence.process_id == proc.id).count() == 0


def test_org_isolation_of_discovery(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    # Another org's discovery sees nothing and creates nothing shared.
    result = ed.propose_operating_model(s, "org-other", llm_fn=_llm_from_prompt)
    assert result["status"] == "insufficient_evidence"
    assert s.query(BusinessProcess).filter(
        BusinessProcess.org_id == "org-other").count() == 0
    assert s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).count() == 1


def test_partial_failure_is_isolated(env, monkeypatch):
    s = env
    # The catalog read for the datasource explodes during extraction —
    # discovery must still complete, recording the skip honestly.
    orig_query = type(s).query
    orig_extract = ed.extract_evidence

    def extract_with_flag(db, org_id, inv):
        db._in_extract = True
        try:
            return orig_extract(db, org_id, inv)
        finally:
            db._in_extract = False

    def flaky_query(self, *a, **k):
        ent = str(a[0]) if a else ""
        if ("KBTableMeta" in ent and getattr(self, "_in_extract", False)
                and not getattr(flaky_query, "_raised", False)):
            flaky_query._raised = True
            raise RuntimeError("boom")
        return orig_query(self, *a, **k)

    monkeypatch.setattr(type(s), "query", flaky_query)
    monkeypatch.setattr(ed, "extract_evidence", extract_with_flag)
    result = ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    assert result["status"] == "ok"
    assert any("catalog read failed" in x["reason"] for x in result["skipped"])
    assert result["evidence_created"] > 0  # other evidence still persisted
