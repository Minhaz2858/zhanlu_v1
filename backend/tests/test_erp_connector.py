"""EBOM — Governed ERP write-back via the sandbox adapter (Workstream C).

Proves the connector-neutral execution contract:
  * LLM direct calls are refused — writes run ONLY through
    execute_approved_action (approval identity, expiry, param match);
  * the sandbox adapter actually receives the submission (external_ref);
  * idempotency: the same approval+draft can never submit twice;
  * adapter failures are honest (failed status + error) and retry cleanly;
  * cross-org / missing drafts are refused; every attempt is audited.
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_erp_connector.db")

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user_required, get_db
from app.services.tool_registry import registry
import app.services.tool_handlers.po_draft_tool  # noqa: F401
import app.services.tool_handlers.erp_submit_tool  # noqa: F401
from app.routers.ontology import router as ontology_router
from app.models.governance import ApprovalRequest
from app.models.audit_event import AuditEvent
from app.models.erp import ErpSubmission
from app.models.ontology import OntologyAction
from app.models.procurement import ProcurementPoDraft
from app.services.ontology.erp import adapter_ledger
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin")


@pytest.fixture(autouse=True)
def _clean_erp():
    from app.services.ontology.erp import reset_adapter_ledger

    reset_adapter_ledger()
    yield
    reset_adapter_ledger()


@pytest.fixture()
def client():
    eng = make_engine()
    from app.database import Base
    Base.metadata.create_all(eng, tables=[
        ErpSubmission.__table__, AuditEvent.__table__,
        ApprovalRequest.__table__, ProcurementPoDraft.__table__])
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


def _seed(client, *, draft_org=ORG, status="approved",
          approval_draft="pd-erp"):
    s = client.app.dependency_overrides[get_db]()
    s.add(ProcurementPoDraft(
        id="pd-erp", org_id=draft_org, supplier_name="Acme Co",
        product="Widget", quantity=10, unit="pcs",
        est_unit_price=5.0, est_amount=50.0, currency="USD",
        status=status))
    s.add(OntologyAction(
        id="act-erp", org_id=ORG, object_id="obj-x", name="erp_submit_po",
        tool_name="erp_submit_po", risk_level="write", is_active=True,
        is_deleted=False))
    s.add(ApprovalRequest(
        id="apr-erp", org_id=ORG, status="approved",
        action_type="ontology_write", reviewed_by="u-owner",
        action_description="Submit approved PO draft to ERP",
        context_json={"action_id": "act-erp", "tool_name": "erp_submit_po",
                      "params": {"draft_id": approval_draft}}))
    s.commit()
    return s


def _execute(client, params=None, approval_id="apr-erp"):
    return client.post(
        "/api/ontology/actions/act-erp/execute-approved",
        json={"approval_id": approval_id,
              "params": params or {"draft_id": "pd-erp"}})


# --- the LLM can never submit directly ---------------------------------------

def test_direct_call_refused():
    import asyncio

    from app.services.agent_tools import execute_tool

    result = asyncio.run(execute_tool(
        "erp_submit_po", {"draft_id": "pd-erp"}, None, "u-llm", context={}))
    assert result["success"] is False
    assert "approval" in result["reason"].lower()


# --- the governed path actually reaches the external system ------------------

def test_execute_approved_reaches_sandbox_erp(client):
    s = _seed(client)
    r = _execute(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True, body
    # The adapter PROVES the external write landed.
    ext_ref = body["result"]["external_ref"]
    assert ext_ref.startswith("SANDBOX-PO-")
    assert ext_ref in adapter_ledger()
    # Ledger row is succeeded and linked to the approval + draft.
    row = s.query(ErpSubmission).filter_by(org_id=ORG).one()
    assert row.status == "succeeded"
    assert row.external_ref == ext_ref
    assert row.approval_id == "apr-erp"
    assert row.draft_id == "pd-erp"
    # Audit trail records the external execution.
    audits = s.query(AuditEvent).filter_by(
        org_id=ORG, event_type="erp_submission_succeeded").all()
    assert len(audits) == 1


def test_duplicate_execution_is_idempotent(client):
    s = _seed(client)
    r1 = _execute(client)
    r2 = _execute(client)
    assert r1.json()["success"] and r2.json()["success"]
    assert (r1.json()["result"]["external_ref"]
            == r2.json()["result"]["external_ref"])
    # Exactly ONE submission reached the external system.
    assert len(adapter_ledger()) == 1
    assert s.query(ErpSubmission).filter_by(org_id=ORG).count() == 1


# --- failure is honest and retry is safe -------------------------------------

def test_adapter_failure_is_honest_and_retry_succeeds(client):
    s = _seed(client)
    import app.services.ontology.erp as erp

    def boom(payload):
        raise ConnectionError("ERP endpoint unreachable")

    original = erp.ADAPTERS["sandbox"]
    erp.ADAPTERS["sandbox"] = type("FailingAdapter", (), {
        "submit_purchase_order": staticmethod(boom)})()
    try:
        r1 = _execute(client)
    finally:
        erp.ADAPTERS["sandbox"] = original  # adapter healthy again
    assert r1.json()["success"] is False
    row = s.query(ErpSubmission).filter_by(org_id=ORG).one()
    assert row.status == "failed"
    assert "unreachable" in (row.error_message or "")
    # The platform's tool-reliability layer retries failed calls — every
    # attempt is audited, but ALL attempts hit the SAME idempotency row.
    failed_audits = s.query(AuditEvent).filter_by(
        org_id=ORG, event_type="erp_submission_failed").all()
    assert len(failed_audits) >= 1
    assert len({a.resource_id for a in failed_audits}) == 1
    assert len(adapter_ledger()) == 0  # nothing ever reached the ERP

    # Retry with the adapter healthy: same idempotency key, now succeeds.
    r2 = _execute(client)
    assert r2.json()["success"] is True
    s.expire_all()
    rows = s.query(ErpSubmission).filter_by(org_id=ORG).all()
    assert len(rows) == 1 and rows[0].status == "succeeded"


# --- input validation and cross-org walls -------------------------------------

def test_missing_draft_refused(client):
    # Approval covers 'pd-nope' (no drift), so the TOOL-level org check runs
    # and refuses a draft that does not exist in this org.
    _seed(client, approval_draft="pd-nope")
    r = _execute(client, params={"draft_id": "pd-nope"})
    assert r.json()["success"] is False
    assert "not found" in r.json()["result"]["reason"].lower()


def test_cross_org_draft_refused(client):
    _seed(client, draft_org="other-org")
    r = _execute(client)
    assert r.json()["success"] is False
    assert "not found" in r.json()["result"]["reason"].lower()
    assert len(adapter_ledger()) == 0  # nothing reached the external system


def test_unapproved_action_still_blocked_by_gate(client):
    s = _seed(client)
    s.query(ApprovalRequest).filter_by(id="apr-erp").update({"status": "pending"})
    s.commit()
    r = _execute(client)
    body = r.json()
    assert body["success"] is False
    assert body.get("blocked") == "confirmation_required"
    assert len(adapter_ledger()) == 0
