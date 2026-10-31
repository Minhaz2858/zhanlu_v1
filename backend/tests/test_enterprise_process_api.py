"""EBPM Increment 3 — Enterprise Process Management API (mocked LLM).

Covers: honest empty catalogue, search/filters/counts, detail ordering +
evidence + ontology connections + related processes, org isolation (404),
admin-gated confirm/reject, review history + audit trail, and rediscovery
preservation for BOTH verified and rejected decisions. No real LLM calls.
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
from app.models.audit_event import AuditEvent
from app.models.automation_task import AutomationTask
from app.models.enterprise_process import (
    BusinessProcess, EnterpriseResource, ProcessActivity, ProcessLink,
    ProcessEvidence,
)

from app.models.knowledge_catalog import KBTableRelation
from app.routers.ontology import router as ontology_router
from app.services.ontology import enterprise_discovery as ed
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin",
                           full_name="Owner", email="owner@t.dev")


def _viewer(org=ORG):
    return SimpleNamespace(id="u-viewer", org_id=org, role="user",
                           full_name="Viewer", email="viewer@t.dev")


@pytest.fixture()
def env():
    eng = make_engine()
    Base.metadata.create_all(eng, tables=[
        EnterpriseResource.__table__, AutomationTask.__table__,
        AgentRun.__table__, KBTableRelation.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__, ProcessLink.__table__,
        AuditEvent.__table__,
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
    ids = sorted(set(re.findall(r"\[ev-\d+\]", prompt)))
    use = [i[1:-1] for i in ids][:12]
    return {
        "org_units": [], "capabilities": [],
        "processes": [{
            "name": "Procure-to-Pay",
            "purpose": "Buy goods from suppliers and pay them",
            "process_family": "procure-to-pay",
            "discovery_status": "documented",
            "evidence_ids": use,
            "activities": [
                {"seq": 2, "name": "Approve purchase",
                 "activity_type": "approval",
                 "performer": "Procurement Department", "evidence_ids": use[:1]},
                {"seq": 1, "name": "Create requisition",
                 "activity_type": "human_task",
                 "performer": "Procurement Department", "evidence_ids": use[:1]},
            ],
        }],
    }


def _second_llm(prompt, schema=None, endpoint=None):
    out = _llm_from_prompt(prompt)
    out["processes"] = [{
        "name": "Invoice Check", "process_family": "finance",
        "discovery_status": "configured", "evidence_ids": [], "activities": [],
    }]
    return out


def _client(s, user):
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")

    def _db():
        yield s

    def _user():
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_required] = _user
    return TestClient(app)


def test_catalogue_is_honestly_empty(env):
    client = _client(env, _admin())
    r = client.get("/api/ontology/enterprise/processes")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0 and body["items"] == []
    assert body["families"] in ([], [{"family": None, "count": 0}])


def test_catalogue_search_filters_counts_and_families(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    ed.propose_operating_model(s, ORG, llm_fn=_second_llm)
    client = _client(s, _admin())

    r = client.get("/api/ontology/enterprise/processes").json()
    assert r["total"] == 2
    fam = {f["family"]: f["count"] for f in r["families"]}
    assert fam == {"procure-to-pay": 1, "finance": 1}
    item = next(i for i in r["items"] if i["name"] == "Procure-to-Pay")
    assert item["evidence_status"] == "proposed"
    assert item["needs_review"] is True
    assert item["discovery_status"] == "documented"  # never conflated
    assert item["evidence_count"] > 0
    assert item["activities_count"] == 2
    assert item["responsible_units"] == ["Procurement Department"]

    # family filter
    r2 = client.get("/api/ontology/enterprise/processes",
                    params={"family": "finance"}).json()
    assert r2["total"] == 1 and r2["items"][0]["name"] == "Invoice Check"

    # status filter
    r3 = client.get("/api/ontology/enterprise/processes",
                    params={"status": "proposed"}).json()
    assert r3["total"] == 2

    # search q
    r4 = client.get("/api/ontology/enterprise/processes",
                    params={"q": "invoice"}).json()
    assert r4["total"] == 1 and r4["items"][0]["name"] == "Invoice Check"

    # pagination
    r5 = client.get("/api/ontology/enterprise/processes",
                    params={"page": 2, "page_size": 1}).json()
    assert r5["total"] == 2 and len(r5["items"]) == 1


def test_detail_activities_evidence_and_ontology_connections(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    client = _client(s, _admin())

    r = client.get(f"/api/ontology/enterprise/processes/{proc.id}")
    assert r.status_code == 200
    body = r.json()
    seqs = [a["seq"] for a in body["activities"]]
    assert seqs == sorted(seqs)  # ordered
    assert body["discovery_status"] == "documented"
    assert body["evidence"] and all(e["quote"] for e in body["evidence"])
    # ontology connections resolved from evidence source_refs
    assert any(c["kind"] in ("object", "link")
               for c in body["ontology_connections"])
    assert body["review_history"] == []
    assert body["provenance"]["discovery_method"] == "ai_proposal"

    # unknown id → 404 (honest)
    r2 = client.get("/api/ontology/enterprise/processes/nope")
    assert r2.status_code == 404


def test_detail_cross_org_404(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    # viewer of another org: _org_or_404 pins to own org → 404
    other = SimpleNamespace(id="u-x", org_id="org-other", role="admin")
    client = _client(s, other)
    r = client.get(f"/api/ontology/enterprise/processes/{proc.id}")
    assert r.status_code == 404


def test_confirm_is_admin_gated_and_audited(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()

    # viewer gets 403
    r = _client(s, _viewer()).post(
        f"/api/ontology/enterprise/processes/{proc.id}/confirm")
    assert r.status_code == 403

    # admin confirms
    r2 = _client(s, _admin()).post(
        f"/api/ontology/enterprise/processes/{proc.id}/confirm")
    assert r2.status_code == 200
    assert r2.json()["evidence_status"] == "verified"
    s.expire_all()
    row = s.query(BusinessProcess).filter(
        BusinessProcess.id == proc.id).one()
    assert row.evidence_status == "verified"
    hist = row.provenance_json["review_history"]
    assert hist[-1]["decision"] == "confirmed"
    assert hist[-1]["reviewer_id"] == "u-owner"
    assert hist[-1]["at"]  # timestamp recorded
    # AI provenance preserved
    assert row.provenance_json["discovery_method"] == "ai_proposal"
    assert row.provenance_json["evidence_ids"]
    audit = s.query(AuditEvent).filter(
        AuditEvent.event_type == "process_confirmed").one()
    assert audit.resource_id == proc.id
    assert audit.actor_id == "u-owner"


def test_reject_records_reason_and_survives_rediscovery(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    client = _client(s, _admin())

    r = client.post(f"/api/ontology/enterprise/processes/{proc.id}/reject",
                    json={"reason": "evidence does not support this flow"})
    assert r.status_code == 200
    assert r.json()["evidence_status"] == "rejected"
    s.expire_all()
    row = s.query(BusinessProcess).filter(
        BusinessProcess.id == proc.id).one()
    assert row.evidence_status == "rejected"
    hist = row.provenance_json["review_history"]
    assert hist[-1]["decision"] == "rejected"
    assert hist[-1]["reason"] == "evidence does not support this flow"
    assert s.query(AuditEvent).filter(
        AuditEvent.event_type == "process_rejected").count() == 1

    # rediscovery must NOT weaken the rejection nor re-propose it
    result = ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    s.expire_all()
    assert result["proposals_created"] == 0
    row = s.query(BusinessProcess).filter(
        BusinessProcess.id == proc.id).one()
    assert row.evidence_status == "rejected"
    assert row.purpose  # original fields intact


def test_review_history_appends_not_overwrites(env):
    s = env
    ed.propose_operating_model(s, ORG, llm_fn=_llm_from_prompt)
    proc = s.query(BusinessProcess).filter(
        BusinessProcess.org_id == ORG).one()
    client = _client(s, _admin())
    client.post(f"/api/ontology/enterprise/processes/{proc.id}/reject",
                json={"reason": "first look"})
    client.post(f"/api/ontology/enterprise/processes/{proc.id}/confirm")
    s.expire_all()
    row = s.query(BusinessProcess).filter(
        BusinessProcess.id == proc.id).one()
    hist = row.provenance_json["review_history"]
    assert [h["decision"] for h in hist] == ["rejected", "confirmed"]
    assert row.evidence_status == "verified"
    assert s.query(AuditEvent).filter(
        AuditEvent.event_type.in_(["process_rejected",
                                   "process_confirmed"])).count() == 2
