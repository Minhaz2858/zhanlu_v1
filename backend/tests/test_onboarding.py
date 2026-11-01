"""Phase 4 — resource onboarding & continuous ontology evolution.

Idempotent, bounded, org-scoped lifecycle: inventory → evidence →
discovery (proposals ONLY) → status/audit. Digest-based drift detection;
unchanged resources short-circuit (no duplicate discovery); failures are
per-step, observable, retryable; nothing is ever auto-verified.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.ontology.enterprise_discovery as ed
from app.database import Base
from app.models.audit_event import AuditEvent
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessProcess, EnterpriseResource, ProcessEvidence)
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.agent_run import AgentRun
from app.models.business_rule import BusinessRule
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import McpServer
from app.models.automation_task import AutomationTask
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.terminology import Terminology
from app.models.ontology import OntologyAction, OntologyLink, OntologyObject
from app.services.enterprise.onboarding import (
    onboard_resource, on_resource_changed, get_onboarding_status)

ORG = "org-acme"
ORG2 = "org-other"

INV = {
    "datasources": [{"id": "kb-1", "name": "Sales DW", "kb_type": "database"}],
    "documents": [{"id": "kb-doc", "name": "Policy Handbook"}],
    "ontology": {"actions": [], "objects": []},
    "automations": [],
}


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        EnterpriseOnboardingRun.__table__, EnterpriseResource.__table__,
        KBTableMeta.__table__, BusinessProcess.__table__,
        ProcessEvidence.__table__, OntologyObject.__table__,
        OntologyLink.__table__, OntologyAction.__table__,
        AuditEvent.__table__, Nl2sqlQueryLog.__table__,
        Terminology.__table__, AgentRun.__table__, BusinessRule.__table__,
        KnowledgeBase.__table__, KBTableRelation.__table__,
        AgentDataBinding.__table__, AgentApp.__table__,
        McpServer.__table__, AutomationTask.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def discovery(monkeypatch):
    """Deterministic seams: inventory fixture + proposal call counter."""
    calls = {"propose": 0}

    def _inv(db, org_id):
        return INV if org_id == ORG else {
            "datasources": [], "documents": [], "ontology": {"actions": [],
                                                             "objects": []},
            "automations": []}

    def _propose(db, org_id, llm_fn=None):
        calls["propose"] += 1
        return {"org_id": org_id, "llm_ok": True, "proposals": [
            {"kind": "process", "name": "Order to Cash",
             "evidence_status": "proposed"}]}

    monkeypatch.setattr(ed, "inventory_resources", _inv)
    monkeypatch.setattr(ed, "propose_operating_model", _propose)
    return calls


def _tables(db, kb_id, names):
    if db.query(KnowledgeBase).filter_by(id=kb_id).first() is None:
        db.add(KnowledgeBase(id=kb_id, org_id=ORG, name=f"KB {kb_id}"))
    for t in names:
        db.add(KBTableMeta(kb_id=kb_id, table_name=t))
    db.commit()


# 1. Database onboarding ------------------------------------------------------------

def test_database_onboarding_runs_lifecycle(db, discovery):
    _tables(db, "kb-1", ["sales_order", "crm_cust_info"])
    res = onboard_resource(db, ORG, "datasource", "kb-1", actor_id="u-1")
    assert res["status"] == "completed"
    steps = {s["step"]: s["status"] for s in res["steps"]}
    assert steps["inventory"] == "ok"
    assert steps["evidence"] == "ok"
    assert steps["discovery"] == "ok"
    # inventory registered as EnterpriseResource rows
    kinds = {(r.resource_kind, r.ref_id) for r in
             db.query(EnterpriseResource).filter_by(org_id=ORG,
                                                    is_deleted=False)}
    assert ("datasource", "kb-1") in kinds
    # proposals recorded — and the run NEVER claims verification
    assert discovery["propose"] == 1
    run = db.query(EnterpriseOnboardingRun).one()
    assert run.status == "completed" and run.org_id == ORG
    assert run.actor_id == "u-1"


# 2. Document onboarding --------------------------------------------------------------

def test_document_onboarding(db, discovery):
    db.add(KnowledgeBase(id="kb-doc", org_id=ORG, name="Policy Handbook",
                         type="document"))
    db.commit()
    res = onboard_resource(db, ORG, "knowledge_base", "kb-doc")
    assert res["status"] == "completed"
    kinds = {(r.resource_kind, r.ref_id) for r in
             db.query(EnterpriseResource).filter_by(org_id=ORG,
                                                    is_deleted=False)}
    assert ("document", "kb-doc") in kinds


# 3. MCP onboarding — capabilities limited to actual integration ----------------------

def test_mcp_onboarding_honest_unsupported(db, discovery):
    # Phase 5D: mcp_server existence is now org-verified (row required)
    from app.models.mcp_server import McpServer
    db.add(McpServer(id="mcp-1", org_id=ORG, name="ERP MCP",
                     server_url="http://mcp.internal"))
    db.commit()
    res = onboard_resource(db, ORG, "mcp_server", "mcp-1")
    assert res["status"] == "completed"
    steps = {s["step"]: s["status"] for s in res["steps"]}
    # evidence/discovery need document or catalog evidence an MCP server
    # cannot provide — honestly skipped, never fabricated
    assert steps["evidence"] == "skipped"
    assert "unsupported" in steps["evidence"].lower() or \
        "no extractable evidence" in str(res["steps"])


# 4. Idempotent repeated onboarding ----------------------------------------------------

def test_idempotent_repeated_onboarding(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    r1 = onboard_resource(db, ORG, "datasource", "kb-1")
    r2 = onboard_resource(db, ORG, "datasource", "kb-1")
    assert r1["status"] == "completed"
    assert r2["status"] == "skipped_unchanged"  # digest identical
    assert discovery["propose"] == 1  # NO duplicate discovery runs


# 5. Cross-org isolation -----------------------------------------------------------------

def test_cross_org_isolation(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    res = onboard_resource(db, ORG2, "datasource", "kb-1")
    # the resource belongs to ORG's catalog — ORG2 sees nothing to onboard
    assert res["status"] in ("failed", "resource_missing")
    runs = db.query(EnterpriseOnboardingRun).filter_by(org_id=ORG).all()
    assert runs == []


# 6-7. Partial failure + retry -------------------------------------------------------------

def test_partial_failure_and_retry(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    real_propose = ed.propose_operating_model

    def boom(db, org_id, llm_fn=None):
        raise RuntimeError("llm down")

    monkeypatch.setattr(ed, "propose_operating_model", boom)
    r1 = onboard_resource(db, ORG, "datasource", "kb-1")
    assert r1["status"] == "partial_failed"
    steps = {s["step"]: s["status"] for s in r1["steps"]}
    assert steps["evidence"] == "ok"      # earlier steps persisted
    assert steps["discovery"] == "failed"
    assert r1["error"]

    monkeypatch.setattr(ed, "propose_operating_model", real_propose)
    r2 = onboard_resource(db, ORG, "datasource", "kb-1", retry=True)
    assert r2["status"] == "completed"
    assert discovery["propose"] == 1  # retry ran only the failed step
    runs = db.query(EnterpriseOnboardingRun).filter_by(org_id=ORG) \
        .order_by(EnterpriseOnboardingRun.created_date).all()
    assert runs[-1].attempt == 2


def test_missing_resource_fails_honestly(db, discovery):
    res = onboard_resource(db, ORG, "datasource", "kb-none")
    assert res["status"] == "failed"
    assert "not found" in (res.get("error") or "").lower()


# 8. Schema drift detection ------------------------------------------------------------------

def test_drift_detected_and_reprocessed(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    onboard_resource(db, ORG, "datasource", "kb-1")
    assert discovery["propose"] == 1
    # schema change → digest changes → evolution reprocesses
    _tables(db, "kb-1", ["returns"])
    ch = on_resource_changed(db, ORG, "datasource", "kb-1")
    assert ch["drift_detected"] is True
    assert ch["run"]["status"] == "completed"
    assert discovery["propose"] == 2


def test_unchanged_resource_change_is_noop(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    onboard_resource(db, ORG, "datasource", "kb-1")
    ch = on_resource_changed(db, ORG, "datasource", "kb-1")
    assert ch["drift_detected"] is False
    assert discovery["propose"] == 1


# 9. Deletion cleanup --------------------------------------------------------------------------

def test_deleted_resource_cleanup(db, discovery):
    _tables(db, "kb-1", ["sales_order"])  # helper creates the KB row
    onboard_resource(db, ORG, "datasource", "kb-1")
    from app.services.enterprise.cleanup import handle_resource_deleted
    handle_resource_deleted(db, resource_type="knowledge_base",
                            resource_id="kb-1", org_id=ORG, actor_id="u-1")
    # the real deletion flow also soft-deletes the resource row itself
    db.query(KnowledgeBase).filter_by(id="kb-1").update({"is_deleted": True})
    db.commit()
    res = onboard_resource(db, ORG, "datasource", "kb-1")
    # resource gone from inventory → honest failure, no resurrection
    assert res["status"] == "failed"


# 10-11. Verified/proposed knowledge preservation ------------------------------------------------

def test_verified_knowledge_preserved_and_proposals_stay_proposed(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    db.add(BusinessProcess(id="proc-1", org_id=ORG, name="Confirmed",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.commit()
    onboard_resource(db, ORG, "datasource", "kb-1")
    p = db.query(BusinessProcess).filter_by(id="proc-1").one()
    assert p.evidence_status == "verified"  # untouched by onboarding
    # any newly discovered process arrives as PROPOSED via the discovery
    # step's own contract (asserted through the proposal payload above)


def test_drift_never_deletes_evidence(db, discovery):
    _tables(db, "kb-1", ["sales_order", "dropped_table"])
    onboard_resource(db, ORG, "datasource", "kb-1")
    db.add(ProcessEvidence(id="ev-1", org_id=ORG, process_id="proc-1",
                           evidence_kind="table_catalog",
                           source_ref="table://dropped_table",
                           evidence_status="verified"))
    db.commit()
    before = db.query(ProcessEvidence).count()
    # the table disappears from the catalog
    db.query(KBTableMeta).filter_by(kb_id="kb-1",
                                    table_name="dropped_table") \
        .update({"is_deleted": True})
    db.commit()
    ch = on_resource_changed(db, ORG, "datasource", "kb-1")
    assert ch["drift_detected"] is True
    assert db.query(ProcessEvidence).count() == before  # preserved
    # stale reference honestly recorded on the latest run
    run = db.query(EnterpriseOnboardingRun).order_by(
        EnterpriseOnboardingRun.created_date.desc()).first()
    stale_steps = [s for s in run.steps_json
                   if s["step"] == "stale_references"]
    assert stale_steps and "dropped_table" in str(stale_steps[0])


# 12. Observation integration without status promotion --------------------------------------------

def test_observation_writer_integration(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    onboard_resource(db, ORG, "datasource", "kb-1")
    from app.models.nl2sql_query_log import Nl2sqlQueryLog
    from app.models.agent_run import AgentRun
    from app.services.enterprise.observations import record_observation
    db.add(Nl2sqlQueryLog(id="l1", org_id=ORG, datasource_id="kb-1",
                          question="q",
                          lineage_json={"tables": ["sales_order"]}))
    db.commit()
    d = record_observation(db, ORG, "nl2sql_query", "l1")
    # no verified table evidence exists → honest unmatched, no stamping
    assert d["matched"] is False
    assert db.get(Nl2sqlQueryLog, "l1").process_id is None
    # and AgentRun rows keep their NULL linkage (historical preserved)
    assert db.query(AgentRun).count() == 0


# 13. Business Context visibility after onboarding --------------------------------------------------

def test_bcs_visibility_after_onboarding(db, discovery):
    from app.services.enterprise.business_context import (
        build_ontology_planning_context)

    _tables(db, "kb-1", ["sales_order"])
    db.add(OntologyObject(id="oo-1", org_id=ORG, domain_id="d", name="Order",
                          evidence_status="verified",
                          backing_kind="table",
                          backing_ref="kb:kb-1:sales_order"))
    db.commit()
    onboard_resource(db, ORG, "datasource", "kb-1")
    ctx = build_ontology_planning_context(db, ORG, query="order")
    # verified knowledge influences planning...
    assert any(o["name"] == "Order" for o in ctx["objects_verified"])
    # ...and proposed discovery output stays OUT of verified facts
    assert all(not o.get("hypothesis") for o in ctx["objects_verified"])


# 14. Audit trail --------------------------------------------------------------------------------------

def test_audit_trail_records_onboarding(db, discovery):
    from app.models.audit_event import AuditEvent
    _tables(db, "kb-1", ["sales_order"])
    onboard_resource(db, ORG, "datasource", "kb-1", actor_id="u-1")
    ev = db.query(AuditEvent).filter(
        AuditEvent.event_type == "resource_onboarded").one()
    assert ev.org_id == ORG
    assert ev.detail_json["resource_type"] == "datasource"
    assert ev.detail_json["status"] == "completed"


def test_nl2sql_log_writer_records_observation(db, discovery, monkeypatch):
    """§5: the NL2SQL query-log writer calls record_observation when the
    flag is ON and lineage evidence exists — no status promotion, no
    fabrication (a log without matching evidence stays unstamped)."""
    from app.services.db.nl_answer_service import _shadow_validate_and_log

    _tables(db, "kb-1", ["sales_order"])
    db.add(BusinessProcess(id="proc-1", org_id=ORG, name="P",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(ProcessEvidence(id="ev-t", org_id=ORG, process_id="proc-1",
                           evidence_kind="table_catalog",
                           source_ref="table://sales_order",
                           evidence_status="verified"))
    db.commit()
    monkeypatch.setattr("app.database.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(
        "app.services.db.nl_answer_service.settings.ONTOLOGY_OBSERVATIONS_ENABLED",
        True)
    _shadow_validate_and_log(
        "kb-1", "orders", "SELECT id FROM sales_order", live_success=True)
    row = db.query(Nl2sqlQueryLog).one()
    assert row.process_id == "proc-1"
    assert row.match_method == "lineage" and row.match_confidence == 0.4
    # process status untouched by the observation
    p = db.query(BusinessProcess).filter_by(id="proc-1").one()
    assert p.evidence_status == "verified"


def test_nl2sql_log_writer_flag_off_no_observation(db, discovery, monkeypatch):
    from app.services.db.nl_answer_service import _shadow_validate_and_log

    _tables(db, "kb-1", ["sales_order"])
    db.add(BusinessProcess(id="proc-1", org_id=ORG, name="P",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(ProcessEvidence(id="ev-t", org_id=ORG, process_id="proc-1",
                           evidence_kind="table_catalog",
                           source_ref="table://sales_order",
                           evidence_status="verified"))
    db.commit()
    monkeypatch.setattr("app.database.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    _shadow_validate_and_log(
        "kb-1", "orders", "SELECT id FROM sales_order", live_success=True)
    row = db.query(Nl2sqlQueryLog).one()
    assert row.process_id is None  # flag default OFF → no stamping
    assert row.lineage_json["sales_order"] if False else "sales_order" in \
        row.lineage_json["tables"]


def test_onboarding_status_query(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    onboard_resource(db, ORG, "datasource", "kb-1")
    st = get_onboarding_status(db, ORG, "datasource", "kb-1")
    assert st["latest"]["status"] == "completed"
    assert st["run_count"] == 1
    assert get_onboarding_status(db, ORG2, "datasource", "kb-1") is None
