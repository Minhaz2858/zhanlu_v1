"""Phase 6B — Enterprise Map executive overview (additive `overview` key).

The map aggregation gains ONE org-scoped overview section computed from
ALREADY-AUTHORITATIVE data (the org's own processes, knowledge rows,
observation linkage and onboarding runs) — no second store, no inference
beyond what's persisted:

- processes: verified/proposed/awaiting-review counts (statuses as-is);
- observed: process-level execution evidence (total, processes covered,
  confidence bands, latest time) — observed NEVER counts as verification;
- knowledge: business objects / connections / terms / governed metrics /
  business rules with verified/proposed/enabled splits;
- attention: only categories backed by real rows (review backlogs, failed
  or partial resource onboardings).

Missing sections surface `available: false` — UNAVAILABLE, never zero.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import json

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.business_rule import BusinessRule
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessCapability, BusinessProcess, EnterpriseResource, OrgUnit,
    ProcessActivity, ProcessLink)
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.models.metric_definition import MetricDefinition
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.ontology import OntologyDomain, OntologyLink, OntologyObject
from app.models.terminology import Terminology
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
        Terminology.__table__, MetricDefinition.__table__,
        BusinessRule.__table__, Nl2sqlQueryLog.__table__,
        AgentRun.__table__, EnterpriseOnboardingRun.__table__,
        KnowledgeBase.__table__, KBTableMeta.__table__,
        BusinessCapability.__table__, ProcessActivity.__table__,
        ProcessLink.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _seed_processes(db):
    db.add(BusinessProcess(id="p1", org_id=ORG, name="Order to Cash",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(BusinessProcess(id="p2", org_id=ORG, name="Procure to Pay",
                           discovery_status="documented",
                           evidence_status="proposed"))
    db.add(BusinessProcess(id="p3", org_id=ORG, name="Hiring",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.commit()


def test_empty_org_has_honest_zeroes_and_availability(db):
    m = enterprise_map(db, ORG)
    ov = m["overview"]
    assert ov["processes"]["total"] == 0
    assert ov["observed"]["total_executions"] == 0
    assert ov["observed"]["processes_with_observations"] == 0
    assert ov["observed"]["available"] is True  # real zero, not unavailable
    assert ov["knowledge"]["available"] is True
    assert ov["knowledge"]["objects"]["total"] == 0
    assert ov["attention"]["partial_onboardings"] == 0


def test_process_status_counts_never_conflated(db):
    _seed_processes(db)
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["processes"]["total"] == 3
    assert ov["processes"]["verified"] == 2
    assert ov["processes"]["proposed"] == 1
    assert ov["processes"]["awaiting_review"] == 1


def test_observed_execution_counts_are_process_level(db):
    _seed_processes(db)
    db.add(Nl2sqlQueryLog(id="q1", org_id=ORG, datasource_id="kb-1",
                          question="x", process_id="p1",
                          match_confidence=0.8, match_method="ontology_object",
                          outcome="success"))
    db.add(Nl2sqlQueryLog(id="q2", org_id=ORG, datasource_id="kb-1",
                          question="y", process_id="p1",
                          match_confidence=0.4, match_method="lineage",
                          outcome="success"))
    db.add(Nl2sqlQueryLog(id="q3", org_id=ORG, datasource_id="kb-1",
                          question="z", process_id=None,  # unmatched
                          match_confidence=None, outcome="success"))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["observed"]["total_executions"] == 2      # matched only
    assert ov["observed"]["processes_with_observations"] == 1
    assert ov["observed"]["confidence_distribution"]["high"] == 1
    assert ov["observed"]["confidence_distribution"]["low"] == 1
    assert ov["observed"]["latest_observed_at"] is not None
    # observed NEVER promotes: p1 stays verified, p2 stays proposed
    procs = {p["id"]: p for p in enterprise_map(db, ORG)["processes"]}
    assert procs["p2"]["evidence_status"] == "proposed"


def test_agent_run_observations_count_with_org_verification(db):
    _seed_processes(db)
    db.add(AgentRun(run_id="r1", agent_name="a", task="t", status="completed",
                    process_id="p3", match_confidence=0.6,
                    match_method="tool_binding",
                    caller_context=json.dumps({"org_id": ORG})))
    db.add(AgentRun(run_id="r2", agent_name="a", task="t", status="completed",
                    process_id="p3", match_confidence=0.6,
                    match_method="tool_binding",
                    caller_context=json.dumps({"org_id": ORG2})))  # cross-org
    db.add(AgentRun(run_id="r3", agent_name="a", task="t", status="completed",
                    process_id="p3", match_confidence=0.6,
                    match_method="tool_binding", caller_context=None))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["observed"]["total_executions"] == 1  # org-verified only
    assert ov["observed"]["processes_with_observations"] == 1


def test_knowledge_counts_with_status_splits(db):
    _seed_processes(db)
    db.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    db.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order",
                          evidence_status="verified"))
    db.add(OntologyObject(id="o2", org_id=ORG, domain_id="d1", name="Lead",
                          evidence_status="proposed"))
    db.add(OntologyLink(id="l1", org_id=ORG, from_object_id="o1",
                        to_object_id="o2", link_type="relates_to"))
    db.add(Terminology(id="t1", org_id=ORG, word="Bookings",
                       description="Signed contracts"))
    db.add(MetricDefinition(id="m1", org_id=ORG, name="Gross Margin",
                            datasource_id="kb-1"))
    db.add(BusinessRule(id="br1", org_id=ORG, statement="No credit sales",
                        status="enabled"))
    db.add(BusinessRule(id="br2", org_id=ORG, statement="Review discounts",
                        status="proposed"))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    k = ov["knowledge"]
    assert k["objects"]["total"] == 2
    assert k["objects"]["verified"] == 1
    assert k["objects"]["proposed"] == 1
    assert k["links"] == 1
    assert k["terminology"] == 1
    assert k["metrics"] == 1
    assert k["rules"]["enabled"] == 1
    assert k["rules"]["proposed"] == 1


def test_cross_org_isolation(db):
    _seed_processes(db)
    db.add(Nl2sqlQueryLog(id="qx", org_id=ORG2, datasource_id="kb-9",
                          question="x", process_id="p1",
                          match_confidence=0.8, outcome="success"))
    db.add(BusinessProcess(id="p9", org_id=ORG2, name="Other org process",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["processes"]["total"] == 3          # ORG2's process excluded
    assert ov["observed"]["total_executions"] == 0  # ORG2's log excluded
    assert ov["knowledge"]["objects"]["total"] == 0


def test_attention_surfaces_review_and_onboarding_backlogs(db):
    _seed_processes(db)
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="Sales DW", status="partial_failed", steps_json=[]))
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="datasource", resource_id="kb-1",
        resource_name="Sales DW", status="completed", steps_json=[],
        attempt=2))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    att = ov["attention"]
    assert att["processes_awaiting_review"] == 1     # p2 proposed
    assert att["partial_onboardings"] == 0           # latest run completed
    # failed LATEST run counts as attention
    db.add(EnterpriseOnboardingRun(
        org_id=ORG, resource_type="mcp_server", resource_id="mcp-1",
        resource_name="ERP MCP", status="failed", steps_json=[]))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["attention"]["partial_onboardings"] == 1


def test_resources_onboarded_counts_inventory(db):
    db.add(EnterpriseResource(org_id=ORG, name="Sales DW",
                              resource_kind="datasource", ref_id="kb-1"))
    db.add(EnterpriseResource(org_id=ORG2, name="Other",
                              resource_kind="datasource", ref_id="kb-9"))
    db.commit()
    ov = enterprise_map(db, ORG)["overview"]
    assert ov["resources_onboarded"] == 1
