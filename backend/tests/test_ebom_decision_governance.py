"""EBOM — Decision & Action Center governance (Experience D backend).

Proves the approval-gated decision workflow is REAL, not UI theater:
  * a write action only executes through an org-scoped, approved,
    action+tool-matching ApprovalRequest (execute_approved_action gate);
  * pending / wrong-org / param-drift approvals are refused honestly;
  * escalation resolution is gated to org admins (authz boundary);
  * PO-draft buyer worklist is org-scoped (no cross-org leakage).
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ebom_gov.db")

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
from app.deps import get_current_user_required, get_db
import app.services.tool_handlers.po_draft_tool  # noqa: F401  register create_po_draft
from app.routers.ontology import router as ontology_router
from app.models.ontology import OntologyAction
from app.models.governance import ApprovalRequest
from app.models.escalation import Escalation
from app.models.audit_event import AuditEvent
from app.models.procurement import ProcurementPoDraft
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin")


def _viewer(org=ORG):
    return SimpleNamespace(id="u-viewer", org_id=org, role="user")


@pytest.fixture()
def client():
    # Ephemeral engine per test (proven pattern — no shared engine state).
    eng = make_engine()
    Base.metadata.create_all(eng, tables=[
        ApprovalRequest.__table__, Escalation.__table__,
        ProcurementPoDraft.__table__, AuditEvent.__table__,
    ])
    s = make_session(eng)
    data_path = tempfile.mktemp(suffix=".db")
    create_data_db(data_path)
    build_sandbox(s, data_path)
    s.commit()
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user_required] = lambda: _admin()
    yield TestClient(app)
    s.close()
    eng.dispose()


def _seed_action(s, action_id="act-po", tool="create_po_draft"):
    a = OntologyAction(
        id=action_id, org_id=ORG, object_id="obj-x", name="po_draft",
        tool_name=tool, risk_level="write", is_active=True, is_deleted=False,
    )
    s.add(a)
    return a


def _seed_approval(s, apr_id, status="approved", org=ORG, action_id="act-po",
                   tool="create_po_draft", params=None):
    ap = ApprovalRequest(
        id=apr_id, org_id=org, status=status, action_type="ontology_write",
        action_description="Approve PO draft creation via governed action",
        reviewed_by="u-owner",
        context_json={"action_id": action_id, "tool_name": tool,
                      "params": params or {"supplier_name": "Acme Co"}},
    )
    s.add(ap)
    return ap


# --- the core approval gate ------------------------------------------------

def test_execute_approved_runs_only_with_valid_approval(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_action(s)
    _seed_approval(s, "apr-ok", params={"supplier_name": "Acme Co"})
    s.commit()

    print("DEBUG count:", s.query(OntologyAction).count(),
          [a.id for a in s.query(OntologyAction).all()])  # noqa: T201
    r = client.post(
        "/api/ontology/actions/act-po/execute-approved",
        json={"approval_id": "apr-ok", "params": {"supplier_name": "Acme Co"}})
    assert r.status_code == 200
    body = r.json()
    print("DEBUG BODY:", body)  # noqa: T201
    assert body["success"] is True
    # The governed write actually happened — a PO draft was born.
    assert s.query(ProcurementPoDraft).filter(
        ProcurementPoDraft.org_id == ORG).count() == 1


def test_execute_blocked_when_approval_pending(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_action(s)
    _seed_approval(s, "apr-pending", status="pending")
    s.commit()

    r = client.post(
        "/api/ontology/actions/act-po/execute-approved",
        json={"approval_id": "apr-pending"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body.get("blocked") == "confirmation_required"
    # No write occurred.
    assert s.query(ProcurementPoDraft).filter(
        ProcurementPoDraft.org_id == ORG).count() == 0


def test_execute_refused_when_approval_in_other_org(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_action(s)
    _seed_approval(s, "apr-other", org="other-org")
    s.commit()

    r = client.post(
        "/api/ontology/actions/act-po/execute-approved",
        json={"approval_id": "apr-other"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "not found in this org" in body["reason"]


def test_execute_refused_on_param_drift(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_action(s)
    _seed_approval(s, "apr-drift",
                   params={"supplier_name": "Acme Co", "quantity": 10})
    s.commit()

    r = client.post(
        "/api/ontology/actions/act-po/execute-approved",
        json={"approval_id": "apr-drift",
              "params": {"supplier_name": "Acme Co", "quantity": 999}})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "changed params" in body["reason"] and "quantity" in body["reason"]


def test_execute_refused_when_action_unknown(client):
    r = client.post(
        "/api/ontology/actions/does-not-exist/execute-approved",
        json={"approval_id": "anything"})
    assert r.status_code == 200
    assert r.json()["success"] is False


# --- escalation resolution authz boundary ----------------------------------

def _seed_escalation(s, esc_id="esc-1"):
    s.add(Escalation(
        id=esc_id, org_id=ORG, summary="Model X margin declining",
        status="open", sentiment="asks_human"))
    s.commit()


def test_resolve_escalation_requires_org_admin(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_escalation(s)
    # Default fixture user is admin -> 200.
    r = client.post("/api/ontology/escalations/esc-1/resolve",
                    json={"note": "approved price review"})
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"


def test_resolve_escalation_denied_for_viewer(client):
    s = client.app.dependency_overrides[get_db]()
    _seed_escalation(s, "esc-2")
    client.app.dependency_overrides[get_current_user_required] = lambda: _viewer()
    r = client.post("/api/ontology/escalations/esc-2/resolve",
                    json={"note": "x"})
    assert r.status_code == 403  # authz boundary enforced


# --- PO-draft worklist org scoping -----------------------------------------

def test_po_drafts_are_org_scoped(client):
    s = client.app.dependency_overrides[get_db]()
    s.add(ProcurementPoDraft(
        id="pd-1", org_id=ORG, supplier_name="Acme Co", product="Widget",
        quantity=10, unit="pcs", status="approved"))
    s.commit()

    # Owner of ORG sees it.
    client.app.dependency_overrides[get_current_user_required] = lambda: _admin(ORG)
    r = client.get("/api/ontology/po-drafts")
    assert r.status_code == 200
    assert any(d["id"] == "pd-1" for d in r.json())

    # A different org sees none of it (fail closed).
    client.app.dependency_overrides[get_current_user_required] = lambda: _admin("other-org")
    r = client.get("/api/ontology/po-drafts")
    assert r.status_code == 200
    assert r.json() == []


# --- the FULL decision lifecycle, end to end through the HTTP app ------------

def test_full_decision_lifecycle_e2e(client):
    """recommendation → decision → approval → governed execution → traceable
    write. Runs the automotive scenario: detect the declining segment
    (governed compute), escalate it, resolve it as the org admin, then run
    the mitigation write ONLY under a matching approval."""
    s = client.app.dependency_overrides[get_db]()

    # 1) Deterministic detection (no LLM): which segment is declining?
    r = client.get(
        "/api/ontology/metrics/met-gross-margin/segments-compare"
        "?segment_column=model_id")
    assert r.status_code == 200
    declining = r.json()["declining_segments"]
    assert declining == ["v-001"]  # the automotive scenario's answer

    # 2) The situation becomes a tracked human handoff.
    s.add(Escalation(
        id="esc-flow", org_id=ORG,
        summary=f"Profitability declining for model {declining[0]}",
        status="open", sentiment="asks_human"))
    s.commit()

    # 3) Org admin resolves the handoff (audited close of the loop).
    r = client.post("/api/ontology/escalations/esc-flow/resolve",
                    json={"note": "approved mitigation PO for Model X"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"

    # 4) The mitigation write executes ONLY under a matching approval.
    _seed_action(s)
    _seed_approval(s, "apr-flow", params={"supplier_name": "Acme Co"})
    s.commit()

    # 4a) Without approval: blocked.
    r = client.post("/api/ontology/actions/act-po/execute-approved",
                    json={"approval_id": "apr-pending-missing"})
    assert r.json()["success"] is False

    # 4b) With the real approval: the write lands.
    r = client.post(
        "/api/ontology/actions/act-po/execute-approved",
        json={"approval_id": "apr-flow",
              "params": {"supplier_name": "Acme Co"}})
    assert r.status_code == 200 and r.json()["success"] is True

    # 5) The outcome is traceable: exactly one born-approved PO draft,
    #    linked to the approval that authorized it.
    drafts = s.query(ProcurementPoDraft).filter(
        ProcurementPoDraft.org_id == ORG).all()
    assert len(drafts) == 1
    assert drafts[0].status == "approved"
    assert drafts[0].approval_id == "apr-flow"
    assert drafts[0].supplier_name == "Acme Co"

    # And the closed escalation is visible as resolved in the queue.
    r = client.get("/api/ontology/escalations?status=resolved")
    assert r.status_code == 200
    assert any(e["id"] == "esc-flow" for e in r.json())
