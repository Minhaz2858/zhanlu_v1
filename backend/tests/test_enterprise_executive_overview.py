"""Phase 6D — executive intelligence sections of the Enterprise Map overview.

Adds FOUR traceable sections to ``_map_overview`` — every metric carries a
machine-readable ``definitions`` entry (source + measurement), and missing
data surfaces as ``available: false`` / ``None``, NEVER as a fabricated
zero or score:

- ``performance``  — REAL execution outcomes from matched nl2sql rows
  (success rate = success / total matched rows; NO SLA claims, NO
  "more executions = better" inference);
- ``departments``  — performer-based process distribution with per-department
  execution evidence (no ranking, no arbitrary scores);
- ``risks``        — enabled prohibitive rules, RECORDED rule blocks (audit),
  failed/partial onboarding, resources whose latest run reported stale
  references — each backed by real rows;
- ``changes``      — org audit events within a 30-day window, grouped by
  type (no manufactured trends from current snapshots).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.audit_event import AuditEvent
from app.models.agent_run import AgentRun
from app.models.business_rule import BusinessRule
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessCapability, BusinessProcess, EnterpriseResource, OrgUnit,
    ProcessActivity, ProcessLink)
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyDomain, OntologyLink, OntologyObject
from app.services.ontology.enterprise_discovery import enterprise_map

ORG = "org-acme"
ORG2 = "org-other"


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
        KnowledgeBase.__table__, KBTableMeta.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _seed(db):
    db.add(OrgUnit(id="u1", org_id=ORG, name="Operations",
                   unit_type="department"))
    db.add(OrgUnit(id="u2", org_id=ORG, name="Finance",
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
                           name="Check budget", performer="Finance"))
    db.commit()


def test_performance_success_rate_from_real_outcomes(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    for i, (pid, outcome) in enumerate([
            ("p1", "success"), ("p1", "success"), ("p1", "error"),
            ("p2", "success")]):
        db.add(Nl2sqlQueryLog(id=f"q{i}", org_id=ORG, datasource_id="kb-1",
                              question="x", process_id=pid, outcome=outcome,
                              created_date=now - timedelta(days=i)))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    perf = ov["performance"]
    assert perf["total_executions"] == 4
    assert perf["successful"] == 3
    assert perf["failed"] == 1
    assert perf["success_rate"] == 0.75
    assert perf["last_7d_executions"] == 4
    assert perf["available"] is True
    # every metric is traceable
    assert "success_rate" in perf["definitions"]
    assert "nl2sql_query_logs.outcome" in perf["definitions"]["success_rate"]


def test_performance_no_outcome_data_is_unavailable_not_zero(db):
    _seed(db)
    ov = enterprise_map(db, ORG)["overview"]
    perf = ov["performance"]
    assert perf["total_executions"] == 0
    assert perf["success_rate"] is None  # undefined, NOT 100% or 0%
    assert perf["available"] is True


def test_departments_performer_based_distribution(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    db.add(Nl2sqlQueryLog(id="q1", org_id=ORG, datasource_id="kb-1",
                          question="x", process_id="p1", outcome="success",
                          created_date=now))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    depts = {d["name"]: d for d in ov["departments"]}
    assert depts["Operations"]["processes"] == 1
    assert depts["Operations"]["verified_processes"] == 1
    assert depts["Operations"]["observed_executions"] == 1
    assert depts["Finance"]["processes"] == 1
    assert depts["Finance"]["verified_processes"] == 0  # p2 is proposed
    assert depts["Finance"]["observed_executions"] == 0
    assert "definitions" in ov["departments"][0] or ov.get("department_definitions")


def test_department_observed_counts_never_count_cross_org(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    db.add(Nl2sqlQueryLog(id="qx", org_id=ORG2, datasource_id="kb-9",
                          question="x", process_id="p1", outcome="success",
                          created_date=now))
    db.add(AgentRun(run_id="rx", agent_name="a", task="t", status="completed",
                    process_id="p1", caller_context='{"org_id": "org-other"}'))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    depts = {d["name"]: d for d in ov["departments"]}
    assert depts["Operations"]["observed_executions"] == 0


def test_risks_prohibitive_rules_blocks_and_stale_references(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    db.add(BusinessRule(id="br1", org_id=ORG,
                        statement="PROHIBIT: selling below cost",
                        status="enabled"))
    db.add(BusinessRule(id="br2", org_id=ORG, statement="Review discounts",
                        status="enabled"))  # informational — not prohibitive
    db.add(BusinessRule(id="br3", org_id=ORG,
                        statement="PROHIBIT: data exfiltration",
                        status="proposed"))  # proposed — not enforced
    db.add(AuditEvent(org_id=ORG, event_type="business_rule_check",
                      event_source="ontology", actor_type="agent",
                      resource_type="action", outcome="denied",
                      # decision 'block' — the exact value written by
                      # business_rules.audit_action_rule_decision (6E fix;
                      # the writer never emits 'blocked')
                      detail_json={"decision": "block"},
                      occurred_at=now - timedelta(days=1)))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="DW", status="completed", attempt=2,
        steps_json=[{"step": "stale_references", "status": "ok",
                     "detail": {"removed_tables": ["old_fact"]}}]))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    risks = ov["risks"]
    assert risks["enabled_prohibitive_rules"] == 1  # proposed never counts
    assert risks["blocked_actions"] == 1
    assert risks["stale_reference_resources"] == 1
    assert "blocked_actions" in risks["definitions"]


def test_changes_from_real_audit_records_only(db):
    _seed(db)
    now = datetime.now(timezone.utc)
    db.add(AuditEvent(org_id=ORG, event_type="resource_onboarded",
                      event_source="ontology", actor_type="user",
                      resource_type="datasource", resource_id="kb-1",
                      occurred_at=now - timedelta(days=2)))
    db.add(AuditEvent(org_id=ORG, event_type="process_confirmed",
                      event_source="ontology", actor_type="user",
                      resource_type="process", resource_id="p1",
                      occurred_at=now - timedelta(days=1)))
    db.add(AuditEvent(org_id=ORG2, event_type="resource_onboarded",
                      event_source="ontology", actor_type="user",
                      resource_type="datasource", resource_id="kb-9",
                      occurred_at=now))  # cross-org — excluded
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    ch = ov["changes"]
    assert ch["window_days"] == 30
    types = {e["event_type"]: e["count"] for e in ch["events"]}
    assert types["resource_onboarded"] == 1  # ORG2's excluded
    assert types["process_confirmed"] == 1
    assert ch["recent"][0]["event_type"] in ("resource_onboarded",
                                             "process_confirmed")
    assert all("occurred_at" in r for r in ch["recent"])


def test_changes_window_excludes_old_events(db):
    _seed(db)
    db.add(AuditEvent(org_id=ORG, event_type="datasource_connect",
                      event_source="system", actor_type="user",
                      resource_type="datasource", resource_id="kb-1",
                      occurred_at=datetime.now(timezone.utc)
                      - timedelta(days=90)))
    db.commit()
    ch = enterprise_map(db, ORG)["overview"]["changes"]
    assert all(e["event_type"] != "datasource_connect" for e in ch["events"])


def test_empty_org_sections_available_with_definitions(db):
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["performance"]["available"] is True
    assert ov["risks"]["available"] is True
    assert ov["changes"]["available"] is True
    assert ov["changes"]["events"] == []
    assert ov["departments"] == []
