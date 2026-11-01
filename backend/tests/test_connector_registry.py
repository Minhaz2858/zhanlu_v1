"""Phase 5D — connector capability registry & onboarding expansion.

One authoritative, EXPLICIT capability map per resource type; the
onboarding pipeline consults it instead of hardcoding type lists. Every
unsupported operation returns an explicit unsupported result — never a
silent success. Org isolation is strengthened: mcp/automation resources
are now existence-verified against their org-scoped rows (previously any
id passed the authorization step). Credentials never enter onboarding
steps or logs.
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
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.models.mcp_server import McpServer
from app.models.automation_task import AutomationTask
from app.models.ontology import OntologyAction, OntologyLink, OntologyObject
from app.services.enterprise.connectors import (
    CAPABILITIES,
    ConnectorRegistry,
    assert_capability,
    capabilities_for,
    get_connector,
)
from app.services.enterprise.onboarding import onboard_resource

ORG = "org-acme"
ORG2 = "org-other"

INV = {
    "datasources": [{"id": "kb-1", "name": "Sales DW", "kb_type": "database"}],
    "documents": [], "ontology": {"actions": [], "objects": []},
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
        AuditEvent.__table__, KnowledgeBase.__table__,
        McpServer.__table__, AutomationTask.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def discovery(monkeypatch):
    def _inv(db, org_id):
        return INV if org_id == ORG else {
            "datasources": [], "documents": [],
            "ontology": {"actions": [], "objects": []}, "automations": []}

    def _propose(db, org_id, llm_fn=None):
        return {"org_id": org_id, "llm_ok": True, "proposals": []}

    monkeypatch.setattr(ed, "inventory_resources", _inv)
    monkeypatch.setattr(ed, "propose_operating_model", _propose)


# 1. registry semantics ------------------------------------------------------------------

def test_capabilities_are_explicit_and_closed():
    assert set(CAPABILITIES) == {
        "inventory", "catalog", "metadata", "evidence",
        "semantic_discovery", "drift", "deletion_cleanup"}
    for rtype in ConnectorRegistry:
        caps = capabilities_for(rtype)
        assert caps <= set(CAPABILITIES)
        assert "inventory" in caps  # every connector can at least be listed


def test_datasource_and_kb_have_full_pipeline():
    for rtype in ("datasource", "knowledge_base"):
        caps = capabilities_for(rtype)
        assert {"inventory", "catalog", "metadata", "evidence",
                "semantic_discovery", "drift",
                "deletion_cleanup"} <= caps


def test_mcp_and_automation_honestly_limited():
    assert capabilities_for("mcp_server") == {
        "inventory", "deletion_cleanup"}
    assert capabilities_for("automation") == {"inventory"}


def test_unknown_connector_is_none_and_unsupported():
    assert get_connector("credit_card_hsm") is None
    with pytest.raises(Exception) as ei:
        assert_capability("credit_card_hsm", "inventory")
    assert "credit_card_hsm" in str(ei.value)
    with pytest.raises(Exception) as ei2:
        assert_capability("mcp_server", "semantic_discovery")
    assert "semantic_discovery" in str(ei2.value)


def test_drift_capability_matches_digest_reality(db):
    from app.services.enterprise.onboarding import _resource_digest
    for rtype in ("datasource", "knowledge_base", "mcp_server", "automation"):
        caps = capabilities_for(rtype)
        has_digest = _resource_digest(db, ORG, rtype, "whatever") is not None
        assert ("drift" in caps) == has_digest  # registry never overclaims


# 2. onboarding consults the registry (behavior preserved for the 4 types) ----------------

def test_onboarding_still_rejects_unknown_types(db, discovery):
    res = onboard_resource(db, ORG, "credit_card_hsm", "x")
    assert res["status"] == "failed"
    assert "unsupported resource type" in res["error"]


def test_mcp_onboarding_reports_unsupported_steps(db, discovery):
    db.add(McpServer(id="mcp-1", org_id=ORG, name="ERP MCP",
                     server_url="http://mcp.internal"))
    db.commit()
    res = onboard_resource(db, ORG, "mcp_server", "mcp-1")
    assert res["status"] == "completed"
    steps = {s["step"]: s["status"] for s in res["steps"]}
    assert steps["inventory"] == "ok"
    assert steps["evidence"] == "skipped"      # explicit, not silent
    assert steps["discovery"] == "skipped"


def test_automation_onboarding_inventory_only(db, discovery):
    db.add(AutomationTask(id="auto-1", org_id=ORG, name="Nightly sync",
                          type="scheduled"))
    db.commit()
    res = onboard_resource(db, ORG, "automation", "auto-1")
    assert res["status"] == "completed"
    steps = {s["step"]: s["status"] for s in res["steps"]}
    assert steps["inventory"] == "ok"
    assert steps["evidence"] == "skipped"


# 3. org isolation strengthened (existence now verified for mcp/automation) ----------------

def test_mcp_server_cross_org_rejected(db, discovery):
    db.add(McpServer(id="mcp-1", org_id=ORG, name="ERP MCP",
                     server_url="http://mcp.internal"))
    db.commit()
    res = onboard_resource(db, ORG2, "mcp_server", "mcp-1")
    assert res["status"] == "failed"
    assert "not found" in res["error"]


def test_mcp_server_missing_rejected(db, discovery):
    res = onboard_resource(db, ORG, "mcp_server", "ghost")
    assert res["status"] == "failed"


def test_automation_cross_org_rejected(db, discovery):
    db.add(AutomationTask(id="auto-1", org_id=ORG, name="Sync",
                          type="scheduled"))
    db.commit()
    res = onboard_resource(db, ORG2, "automation", "auto-1")
    assert res["status"] == "failed"


# 4. idempotency + credential hygiene -------------------------------------------------------

def _seed_kb(db):
    db.add(KnowledgeBase(id="kb-1", org_id=ORG, name="Sales DW",
                         password="s3cr3t-hunter2"))
    db.add(KBTableMeta(kb_id="kb-1", table_name="sales_order"))
    db.commit()


def test_repeated_onboarding_idempotent(db, discovery):
    _seed_kb(db)
    first = onboard_resource(db, ORG, "datasource", "kb-1")
    assert first["status"] == "completed"
    second = onboard_resource(db, ORG, "datasource", "kb-1")
    assert second["status"] == "skipped_unchanged"


def test_credentials_never_enter_steps(db, discovery):
    _seed_kb(db)
    res = onboard_resource(db, ORG, "datasource", "kb-1")
    blob = repr(res)
    assert "s3cr3t-hunter2" not in blob
    assert "password" not in blob.lower()
