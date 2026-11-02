"""Phase 2A — Business Context Service foundation.

Covers: empty enterprise context, relevant verified process retrieval,
enabled-vs-proposed rule tiers, project/department context (metadata
only), cross-org isolation, unauthorized project exclusion, token-budget
truncation, evidence provenance preservation, and honest partial /
unavailable states. Runtime integration is explicitly OUT of scope.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.business_rule import BusinessRule
from app.models.enterprise_process import (
    BusinessProcess, OrgUnit, ProcessActivity, ProcessEvidence)
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.ontology import OntologyLink, OntologyObject
from app.models.organization import Organization
from app.models.project import Project
from app.models.terminology import Terminology
from app.models.user import User
from app.services.enterprise.business_context import (
    build_business_context, render_prompt_block)

ORG = "org-acme"
ORG2 = "org-other"


def _user(uid="u-owner", role="user", org=ORG):
    return User(id=uid, email=f"{uid}@t.dev", full_name=uid.title(),
                role=role, password_hash="x", org_id=org)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, Project.__table__, OrgUnit.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__, OntologyObject.__table__,
        OntologyLink.__table__, Terminology.__table__,
        BusinessRule.__table__, MetricDefinition.__table__,
        KnowledgeBase.__table__, User.__table__,
    ])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u-owner", email="o@t.dev", full_name="Owner",
               role="user", password_hash="x", org_id=ORG))
    s.add(Organization(id=ORG, name="ACME Corp", slug="acme"))
    s.add(OrgUnit(id="ou-mfg", org_id=ORG, name="Manufacturing",
                  unit_type="department"))
    s.add(Project(id="p-1", name="Factory rollout", org_id=ORG,
                  created_by_id="u-owner",
                  department_org_unit_id="ou-mfg"))
    s.add(KnowledgeBase(id="kb-1", name="Factory DB", org_id=ORG,
                        source_kind="database", project_id="p-1"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _process(db, pid, name, *, org=ORG, status="verified", purpose=None):
    db.add(BusinessProcess(id=pid, org_id=org, name=name, purpose=purpose,
                           evidence_status=status))
    db.add(ProcessActivity(id=f"act-{pid}", org_id=org, process_id=pid,
                           seq=1, name=f"{name} step 1"))
    db.add(ProcessEvidence(id=f"pe-{pid}", org_id=org, process_id=pid,
                           evidence_kind="document_chunk",
                           source_ref=f"doc://{pid}#1"))
    db.commit()


# 1. Empty enterprise context -------------------------------------------------

def test_empty_enterprise_context_is_honest(db):
    ctx = build_business_context(db, _user(), ORG, query="anything")
    assert ctx["status"] == "unavailable"
    assert ctx["project"]["authorized"] is False
    assert ctx["processes"] == [] and ctx["business_rules"] == []
    assert ctx["token_budget"]["included_items"] == 0
    assert ctx["organization"]["name"] == "ACME Corp"
    # renderer states the gap instead of inventing content
    block = render_prompt_block(ctx)
    assert "Business Context" in block
    assert "UNVERIFIED" not in block or "Proposed" not in block.split(
        "UNAVAILABLE")[0].split("Proposed")[0]


# 2. Relevant verified process retrieval --------------------------------------

def test_relevant_verified_process_retrieved_first(db):
    _process(db, "pr-ship", "Shipping logistics", status="verified",
             purpose="Move goods to customers")
    _process(db, "pr-invoice", "Invoice reconciliation", status="verified",
             purpose="Match invoices to purchase orders")
    ctx = build_business_context(db, _user(), ORG, query="invoice matching")
    names = [p["name"] for p in ctx["processes"]]
    assert names[0] == "Invoice reconciliation"
    top = ctx["processes"][0]
    assert top["evidence_status"] == "verified"
    assert top["activities"][0]["name"] == "Invoice reconciliation step 1"
    assert top["evidence"][0]["ref"] == "doc://pr-invoice#1"
    block = render_prompt_block(ctx)
    assert "## Verified facts" in block
    assert "Invoice reconciliation" in block


# 3. Enabled vs proposed business rules ----------------------------------------

def test_enabled_vs_proposed_rule_tiers(db):
    db.add_all([
        BusinessRule(id="br-on", org_id=ORG, statement="Exclude cancelled orders",
                     status="enabled", applies_to="sql"),
        BusinessRule(id="br-ver", org_id=ORG, statement="Use fiscal July start",
                     status="verified", applies_to="sql"),
        BusinessRule(id="br-prop", org_id=ORG, statement="Maybe flag VIP customers",
                     status="proposed", applies_to="sql",
                     source_evidence_ids=["pe-9"]),
        BusinessRule(id="br-rej", org_id=ORG, statement="Rejected nonsense rule",
                     status="rejected", applies_to="sql"),
    ])
    db.commit()
    ctx = build_business_context(db, _user(), ORG, query="orders revenue")
    statuses = {r["id"]: r["status"] for r in ctx["business_rules"]}
    assert "br-rej" not in statuses          # rejected never included
    assert statuses.get("br-on") == "enabled"
    block = render_prompt_block(ctx)
    assert "## Enabled business rules (executable guidance)" in block
    assert "Exclude cancelled orders" in block.split("## Enabled")[1].split("##")[0]
    assert "Use fiscal July start" in block.split(
        "## Verified but NOT enabled")[1].split("##")[0]
    if "Maybe flag VIP customers" in block:
        assert "UNVERIFIED" in block.split("Maybe flag VIP customers")[0]


# 4. Project and department context --------------------------------------------

def test_project_and_department_context(db):
    ctx = build_business_context(db, _user(), ORG, project_id="p-1",
                                 query="factory")
    assert ctx["project"]["authorized"] is True
    assert ctx["project"]["name"] == "Factory rollout"
    assert ctx["department"]["name"] == "Manufacturing"
    assert ctx["department"]["hierarchy"] == ["Manufacturing"]
    # resources referenced by id/name only (no credentials, no backing data)
    assert ctx["project"]["resources"] == [
        {"kind": "knowledge_base", "id": "kb-1", "name": "Factory DB",
         "source_kind": "database"}]
    assert ctx["authorization_summary"]["department_is_context_only"] is True
    block = render_prompt_block(ctx)
    assert "not an access grant" in block
    assert "Factory DB" in block
    # department relevance boost: same query now ranks the process section
    assert ctx["token_budget"]["budget_tokens"] > 0


# 5. Cross-org isolation ---------------------------------------------------------

def test_cross_org_isolation(db):
    _process(db, "pr-evil", "Evil process", org=ORG2)
    db.add(BusinessRule(id="br-evil", org_id=ORG2, statement="Evil rule",
                        status="enabled", applies_to="sql"))
    db.add(Terminology(id="t-evil", word="evilterm", description="x",
                       org_id=ORG2, enabled=True))
    db.commit()
    ctx = build_business_context(db, _user(), ORG, query="evil process rule")
    all_names = [p["name"] for p in ctx["processes"]]
    assert "Evil process" not in all_names
    assert all(r["id"] != "br-evil" for r in ctx["business_rules"])
    assert all(t["id"] != "t-evil" for t in ctx["terminology"])
    # a project from another org is not authorized even for the same user id
    db.add(Project(id="p-evil", name="Evil project", org_id=ORG2,
                   created_by_id="u-owner"))
    db.commit()
    ctx2 = build_business_context(db, _user(), ORG, project_id="p-evil")
    assert ctx2["project"]["authorized"] is False
    assert "unavailable_reason" in ctx2["project"]


# 6. Unauthorized resource exclusion ----------------------------------------------

def test_unauthorized_project_excluded(db):
    db.add(Project(id="p-other", name="Not yours", org_id=ORG,
                   created_by_id="u-someone-else"))
    db.commit()
    ctx = build_business_context(db, _user("u-nobody"), ORG,
                                 project_id="p-other")
    assert ctx["project"]["authorized"] is False
    assert ctx["project"]["resources"] == []
    assert ctx["project"].get("unavailable_reason")
    block = render_prompt_block(ctx)
    assert "Not yours" not in block
    assert "unavailable" in block.lower()


# 7. Token-budget truncation -------------------------------------------------------

def test_token_budget_truncation(db):
    _process(db, "pr-1", "Alpha process", purpose="x " * 40)
    _process(db, "pr-2", "Beta process", purpose="y " * 40)
    _process(db, "pr-3", "Gamma process", purpose="z " * 40)
    ctx = build_business_context(db, _user(), ORG, query="process",
                                 budget_tokens=60)
    tb = ctx["token_budget"]
    assert tb["budget_tokens"] == 60
    assert tb["estimated_tokens"] <= 60
    assert 0 < tb["included_items"] < 3
    assert tb["omitted_items"] >= 1
    assert ctx["status"] == "partial"
    assert "omitted" in render_prompt_block(ctx).lower()


# 8. Evidence provenance preservation ----------------------------------------------

def test_evidence_provenance_preserved(db):
    _process(db, "pr-ev", "Procure to pay", status="verified")
    db.add(BusinessRule(id="br-ev", org_id=ORG,
                        statement="Three-way match before payment",
                        status="enabled", applies_to="sql",
                        source_evidence_ids=["pe-ev", "pe-x"]))
    db.commit()
    ctx = build_business_context(db, _user(), ORG, query="procure payment match")
    proc = next(p for p in ctx["processes"] if p["id"] == "pr-ev")
    assert proc["evidence"][0] == {"kind": "document_chunk",
                                   "ref": "doc://pr-ev#1"}
    rule = next(r for r in ctx["business_rules"] if r["id"] == "br-ev")
    assert rule["source_evidence_ids"] == ["pe-ev", "pe-x"]
    assert 0 < ctx["evidence_coverage"] <= 1


# 9. Honest partial/unavailable states ----------------------------------------------

def test_partial_state_on_section_failure_and_missing_project(db):
    _process(db, "pr-ok", "Order intake", status="verified")
    ctx = build_business_context(db, _user(), ORG, project_id="p-missing",
                                 query="order")
    # missing project → warning + honest unauthorized flag, other sections live
    assert ctx["project"]["authorized"] is False
    assert ctx["processes"] != []
    # ontology/terminology sections are empty-but-present (honest absence)
    assert ctx["ontology"] == {"objects": [], "links": []}
    assert ctx["terminology"] == []
    assert ctx["status"] in ("partial", "complete")
    block = render_prompt_block(ctx)
    assert "unavailable" in block.lower()
    # forced section failure surfaces as a warning, never fabricated content
    import app.services.enterprise.business_context as bc

    orig = bc._process_candidates
    bc._process_candidates = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        ctx2 = build_business_context(db, _user(), ORG, query="order")
    finally:
        bc._process_candidates = orig
    assert "section_unavailable:processes" in ctx2["warnings"]
    assert ctx2["processes"] == []
    assert "section_unavailable:processes" in render_prompt_block(ctx2)


# ── Phase 7C (T18/T19): scoping + lifecycle honesty in chat context ─────────

@pytest.fixture()
def db7c(db):
    from app.models.agent_app import AgentApp
    eng = db.get_bind()
    Base.metadata.create_all(eng, tables=[AgentApp.__table__])
    db.add(AgentApp(id="app-a", name="DB analyst", org_id=ORG,
                    knowledge_bases=["kb-a"]))
    db.commit()
    return db


def test_scoped_rules_narrow_to_agent_bound_datasources(db7c):
    db7c.add(BusinessRule(
        id="br-db-a", org_id=ORG, statement="Orders ship in 3 days",
        scope="org", applies_to="sql", status="enabled",
        datasource_ids=["kb-a"]))
    db7c.add(BusinessRule(
        id="br-db-x", org_id=ORG, statement="Finance closes on the 5th",
        scope="org", applies_to="sql", status="enabled",
        datasource_ids=["kb-other"]))
    db7c.add(BusinessRule(
        id="br-org", org_id=ORG, statement="All amounts are USD",
        scope="org", applies_to="sql", status="enabled",
        datasource_ids=None))
    db7c.commit()
    ctx = build_business_context(db7c, _user(), ORG, agent_id="app-a")
    ids = [r["id"] for r in ctx["business_rules"]]
    assert "br-db-a" in ids and "br-org" in ids
    assert "br-db-x" not in ids  # scoped away from this agent — narrow only


def test_rule_scoping_never_widens_without_agent(db7c):
    db7c.add(BusinessRule(
        id="br-scoped", org_id=ORG, statement="Returns need approval",
        scope="org", applies_to="sql", status="enabled",
        datasource_ids=["kb-other"]))
    db7c.commit()
    ctx = build_business_context(db7c, _user(), ORG)  # no agent_id
    assert "br-scoped" in [r["id"] for r in ctx["business_rules"]]


def test_rejected_processes_excluded_from_context(db7c):
    db7c.add(BusinessProcess(id="bp-live", org_id=ORG, name="Order intake",
                             evidence_status="verified"))
    db7c.add(BusinessProcess(id="bp-dead", org_id=ORG, name="Legacy intake",
                             evidence_status="rejected"))
    db7c.commit()
    ctx = build_business_context(db7c, _user(), ORG, query="intake")
    names = [p["name"] for p in ctx["processes"]]
    assert "Order intake" in names and "Legacy intake" not in names


def test_draft_and_disabled_metrics_excluded_from_context(db7c):
    db7c.add(MetricDefinition(
        id="m-live", org_id=ORG, name="On-time delivery",
        datasource_id="kb-a", base_sql="SELECT 1", is_enabled=True,
        lifecycle="published"))
    db7c.add(MetricDefinition(
        id="m-draft", org_id=ORG, name="Secret margin",
        datasource_id="kb-a", base_sql="SELECT 2", is_enabled=True,
        lifecycle="draft"))
    db7c.add(MetricDefinition(
        id="m-off", org_id=ORG, name="Disabled kpi",
        datasource_id="kb-a", base_sql="SELECT 3", is_enabled=False,
        lifecycle="published"))
    db7c.commit()
    ctx = build_business_context(db7c, _user(), ORG, query="margin delivery kpi")
    ids = [m["id"] for m in ctx["metrics"]]
    assert "m-live" in ids
    assert "m-draft" not in ids and "m-off" not in ids
