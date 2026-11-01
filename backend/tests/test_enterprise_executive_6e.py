"""Phase 6E — management drilldown, accountability & executive reporting.

Adds to the EXISTING enterprise-map overview (additive only, every metric
traceable via ``definitions``, missing data surfaced as unavailable — never
a fabricated zero):

- configurable reporting window ``days`` ∈ {7, 30, 90} (changes window +
  blocked-action counts); invalid values are rejected, default 30 kept;
- AgentRun terminal outcomes in ``performance.agent_runs`` (completed/failed
  ONLY — running/queued never counted; separate from the nl2sql success
  rate, no double counting);
- ``GET /api/ontology/enterprise/blocked-actions`` — investigation view of
  RECORDED business_rule_check blocks with resolved rule names (missing
  rules honestly marked unavailable; proposed/disabled rules never
  presented as enforced);
- ``GET /api/ontology/enterprise/executive-snapshot`` — HTML export of the
  SAME computed overview payload (generated_at + provenance, unavailable
  metrics marked, no charts, no fabricated trends);
- per-department process breakdown (evidence status + observed volume).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.audit_event import AuditEvent
from app.models.agent_run import AgentRun
from app.models.business_rule import BusinessRule
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessCapability, BusinessProcess, EnterpriseResource, OrgUnit,
    ProcessActivity, ProcessLink)
from app.models.knowledge_base import KnowledgeBase
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyDomain, OntologyLink, OntologyObject
from app.routers.ontology import router as ontology_router
from app.services.ontology.enterprise_discovery import enterprise_map

ORG = "org-acme"
ORG2 = "org-other"

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        BusinessProcess.__table__, OrgUnit.__table__,
        EnterpriseResource.__table__, OntologyDomain.__table__,
        OntologyObject.__table__, OntologyLink.__table__,
        ProcessActivity.__table__, ProcessLink.__table__,
        BusinessCapability.__table__, Nl2sqlQueryLog.__table__,
        AgentRun.__table__, BusinessRule.__table__,
        AuditEvent.__table__, EnterpriseOnboardingRun.__table__,
        KnowledgeBase.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    user = SimpleNamespace(id="u-owner", org_id=ORG, role="admin",
                           is_super_admin=False)

    def _get_db():
        yield db

    def _user():
        return user

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = _user
    c = TestClient(app)
    yield c


def _seed(db):
    db.add(OrgUnit(id="u1", org_id=ORG, name="Operations",
                   unit_type="department"))
    db.add(BusinessProcess(id="p1", org_id=ORG, name="Order to Cash",
                           process_family="Sales",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(BusinessProcess(id="p2", org_id=ORG, name="Procure to Pay",
                           process_family="Procurement",
                           discovery_status="documented",
                           evidence_status="proposed"))
    db.add(ProcessActivity(org_id=ORG, process_id="p1", seq=1,
                           name="Capture order", performer="Operations"))
    db.add(ProcessActivity(org_id=ORG, process_id="p2", seq=1,
                           name="Check budget", performer="Operations"))
    db.commit()


# 1. configurable reporting window -------------------------------------------------

def test_days_window_honored_in_changes(db):
    _seed(db)
    db.add(AuditEvent(org_id=ORG, event_type="datasource_connect",
                      event_source="system", actor_type="user",
                      resource_type="datasource", resource_id="kb-1",
                      occurred_at=NOW - timedelta(days=10)))
    db.commit()
    ch7 = enterprise_map(db, ORG, days=7)["overview"]["changes"]
    assert ch7["window_days"] == 7
    assert all(e["event_type"] != "datasource_connect" for e in ch7["events"])
    ch90 = enterprise_map(db, ORG, days=90)["overview"]["changes"]
    assert ch90["window_days"] == 90
    assert any(e["event_type"] == "datasource_connect"
               for e in ch90["events"])


def test_days_window_honored_for_blocked_counts(db):
    _seed(db)
    db.add(AuditEvent(org_id=ORG, event_type="business_rule_check",
                      event_source="ontology", actor_type="agent",
                      resource_type="action", outcome="denied",
                      detail_json={"decision": "block"},
                      occurred_at=NOW - timedelta(days=10)))
    db.commit()
    r7 = enterprise_map(db, ORG, days=7)["overview"]["risks"]
    assert r7["blocked_actions"] == 0
    r90 = enterprise_map(db, ORG, days=90)["overview"]["risks"]
    assert r90["blocked_actions"] == 1
    assert r90["window_days"] == 90


def test_default_window_is_30(db):
    _seed(db)
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["changes"]["window_days"] == 30
    assert ov["risks"]["window_days"] == 30


def test_invalid_days_rejected(db):
    _seed(db)
    with pytest.raises(ValueError):
        enterprise_map(db, ORG, days=13)


def test_invalid_days_rejected_at_http(client, db):
    _seed(db)
    res = client.get("/api/ontology/enterprise/map", params={"days": 13})
    assert res.status_code == 422
    res = client.get("/api/ontology/enterprise/blocked-actions",
                     params={"days": 45})
    assert res.status_code == 422


# 2. AgentRun terminal outcomes ----------------------------------------------------

def test_agent_outcomes_terminal_only(db):
    _seed(db)
    db.add(AgentRun(run_id="r1", agent_name="a", task="t",
                    status="completed", process_id="p1",
                    caller_context=f'{{"org_id": "{ORG}"}}',
                    finished_at=NOW))
    db.add(AgentRun(run_id="r2", agent_name="a", task="t",
                    status="failed", process_id="p1",
                    caller_context=f'{{"org_id": "{ORG}"}}',
                    finished_at=NOW))
    db.add(AgentRun(run_id="r3", agent_name="a", task="t",
                    status="running", process_id="p1",
                    caller_context=f'{{"org_id": "{ORG}"}}'))
    db.add(AgentRun(run_id="r4", agent_name="a", task="t",
                    status="queued", process_id="p2",
                    caller_context=f'{{"org_id": "{ORG}"}}'))
    db.commit()
    perf = enterprise_map(db, ORG)["overview"]["performance"]
    ar = perf["agent_runs"]
    assert ar["total"] == 2  # running/queued NEVER counted
    assert ar["completed"] == 1
    assert ar["failed"] == 1
    assert ar["success_rate"] == 0.5
    assert "agent_runs.total" in perf["definitions"]
    assert "terminal" in perf["definitions"]["agent_runs.total"]


def test_agent_outcomes_separate_from_nl2sql_no_double_count(db):
    _seed(db)
    db.add(Nl2sqlQueryLog(id="q1", org_id=ORG, datasource_id="kb-1",
                          question="x", process_id="p1", outcome="success",
                          created_date=NOW))
    db.add(AgentRun(run_id="r1", agent_name="a", task="t",
                    status="completed", process_id="p1",
                    caller_context=f'{{"org_id": "{ORG}"}}'))
    db.commit()
    perf = enterprise_map(db, ORG)["overview"]["performance"]
    assert perf["total_executions"] == 1  # nl2sql only
    assert perf["agent_runs"]["total"] == 1  # separate segment


def test_agent_outcomes_no_data_success_rate_null(db):
    _seed(db)
    ar = enterprise_map(db, ORG)["overview"]["performance"]["agent_runs"]
    assert ar["total"] == 0
    assert ar["success_rate"] is None  # never 0% or 100%


def test_agent_outcomes_cross_org_excluded(db):
    _seed(db)
    db.add(AgentRun(run_id="rx", agent_name="a", task="t",
                    status="completed", process_id="p1",
                    caller_context='{"org_id": "org-other"}'))
    db.commit()
    ar = enterprise_map(db, ORG)["overview"]["performance"]["agent_runs"]
    assert ar["total"] == 0


# 3. blocked-action investigation --------------------------------------------------

def _seed_blocked(db):
    db.add(BusinessRule(id="br1", org_id=ORG,
                        statement="PROHIBIT: selling below cost",
                        status="enabled"))
    db.commit()
    db.add(AuditEvent(
        org_id=ORG, event_type="business_rule_check",
        event_source="ontology", actor_type="agent", actor_id="proc-agent",
        resource_type="ontology_action", resource_id="act-1",
        outcome="denied",
        detail_json={"action_name": "place_order", "tool_name": "execute_action",
                     "decision": "block",
                     "applicable_rule_ids": ["br1"],
                     "violated_rule_ids": ["br1"],
                     "approval_required": False,
                     "evaluation_status": "deterministic"},
        occurred_at=NOW - timedelta(days=2)))
    db.add(AuditEvent(
        org_id=ORG, event_type="business_rule_check",
        event_source="ontology", actor_type="agent",
        resource_type="ontology_action", resource_id="act-2",
        outcome="allowed",
        detail_json={"action_name": "query_sales", "decision": "allow"},
        occurred_at=NOW - timedelta(days=1)))
    db.add(AuditEvent(
        org_id=ORG, event_type="business_rule_check",
        event_source="ontology", actor_type="agent",
        resource_type="ontology_action", resource_id="act-3",
        outcome="allowed",
        detail_json={"action_name": "export_report",
                     "decision": "require_approval",
                     "approval_required": True},
        occurred_at=NOW))
    db.commit()


def test_blocked_actions_endpoint_blocked_only_with_rule_names(client, db):
    _seed_blocked(db)
    res = client.get("/api/ontology/enterprise/blocked-actions")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["total_blocked"] == 1  # allow / require_approval excluded
    item = body["items"][0]
    assert item["action_name"] == "place_order"
    assert item["tool_name"] == "execute_action"
    assert item["occurred_at"] is not None
    assert item["violated_rules"][0]["id"] == "br1"
    assert item["violated_rules"][0]["statement"] == \
        "PROHIBIT: selling below cost"
    assert item["violated_rules"][0]["status"] == "enabled"
    assert "definitions" in body


def test_blocked_actions_missing_rule_marked_unavailable(client, db):
    _seed_blocked(db)
    db.add(AuditEvent(
        org_id=ORG, event_type="business_rule_check",
        event_source="ontology", actor_type="agent",
        resource_type="ontology_action", resource_id="act-9",
        outcome="denied",
        detail_json={"action_name": "ghost_action", "decision": "block",
                     "violated_rule_ids": ["br-gone"]},
        occurred_at=NOW))
    db.commit()
    body = client.get(
        "/api/ontology/enterprise/blocked-actions").json()
    ghost = next(i for i in body["items"]
                 if i["action_name"] == "ghost_action")
    vr = ghost["violated_rules"][0]
    assert vr["id"] == "br-gone"
    assert vr["statement"] is None  # honest: rule reference unresolvable
    assert vr["unavailable"] is True


def test_blocked_actions_window_and_cross_org(client, db):
    _seed_blocked(db)
    db.add(AuditEvent(
        org_id=ORG, event_type="business_rule_check",
        event_source="ontology", actor_type="agent",
        resource_type="ontology_action", resource_id="act-old",
        outcome="denied",
        detail_json={"action_name": "old_action", "decision": "block"},
        occurred_at=NOW - timedelta(days=60)))
    db.add(AuditEvent(
        org_id=ORG2, event_type="business_rule_check",
        event_source="ontology", actor_type="agent",
        resource_type="ontology_action", resource_id="act-f",
        outcome="denied",
        detail_json={"action_name": "foreign_action", "decision": "block"},
        occurred_at=NOW))
    db.commit()
    body = client.get("/api/ontology/enterprise/blocked-actions",
                      params={"days": 7}).json()
    names = {i["action_name"] for i in body["items"]}
    assert names == {"place_order"}  # old window + cross-org excluded
    body90 = client.get("/api/ontology/enterprise/blocked-actions",
                        params={"days": 90}).json()
    names90 = {i["action_name"] for i in body90["items"]}
    assert names90 == {"place_order", "old_action"}  # cross-org still excluded


def test_blocked_actions_empty_honest(client, db):
    res = client.get("/api/ontology/enterprise/blocked-actions")
    assert res.status_code == 200
    body = res.json()
    assert body["total_blocked"] == 0
    assert body["items"] == []
    assert body["available"] is True


# 4. executive snapshot export -----------------------------------------------------

def test_snapshot_export_html_with_provenance(client, db):
    _seed(db)
    res = client.get("/api/ontology/enterprise/executive-snapshot")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "attachment" in res.headers.get("content-disposition", "")
    text = res.text
    assert "generated" in text.lower()
    assert ORG in text
    # provenance: every section names its source rows; unavailable marked
    assert "unavailable" in text.lower()
    # real data present — verified process count from seeded rows
    assert "Order to Cash" in text


def test_snapshot_export_matches_map_values(client, db):
    _seed(db)
    db.add(Nl2sqlQueryLog(id="q1", org_id=ORG, datasource_id="kb-1",
                          question="x", process_id="p1", outcome="success",
                          created_date=NOW))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    res = client.get("/api/ontology/enterprise/executive-snapshot")
    assert ov["performance"]["success_rate"] == 1.0
    assert "100%" in res.text  # same computed payload as the UI
    assert "window" in res.text.lower() and "30" in res.text


def test_snapshot_export_days_param(client, db):
    _seed(db)
    res = client.get("/api/ontology/enterprise/executive-snapshot",
                     params={"days": 7})
    assert res.status_code == 200
    assert ">7<" in res.text or "7-day" in res.text or "7 days" in res.text


# 5. department drilldown enhancement ----------------------------------------------

def test_department_process_breakdown(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    db.add(Nl2sqlQueryLog(id="q1", org_id=ORG, datasource_id="kb-1",
                          question="x", process_id="p1", outcome="success",
                          created_date=now))
    db.commit()
    depts = enterprise_map(db, ORG)["overview"]["departments"]
    ops = next(d for d in depts if d["name"] == "Operations")
    assert ops["process_breakdown"] == [
        {"id": "p1", "name": "Order to Cash", "evidence_status": "verified",
         "observed_executions": 1},
        {"id": "p2", "name": "Procure to Pay",
         "evidence_status": "proposed", "observed_executions": 0},
    ]
    assert ops["breakdown_truncated"] is False


def test_department_breakdown_capped_and_flagged(db):
    _seed(db)
    for i in range(25):
        db.add(BusinessProcess(id=f"px{i}", org_id=ORG, name=f"P{i}",
                               discovery_status="documented",
                               evidence_status="verified"))
        db.add(ProcessActivity(org_id=ORG, process_id=f"px{i}", seq=1,
                               name=f"A{i}", performer="Operations"))
    db.commit()
    ops = next(d for d in enterprise_map(db, ORG)["overview"]["departments"]
               if d["name"] == "Operations")
    assert len(ops["process_breakdown"]) == 20
    assert ops["breakdown_truncated"] is True
