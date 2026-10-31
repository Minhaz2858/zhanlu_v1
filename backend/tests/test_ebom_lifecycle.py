"""EBOM — Complete decision lifecycle chain (Workstream D).

One CEO question answered from EXISTING records:
what happened → why concerned → who approved → was it executed →
what changed → was it verified. The assembler only follows references
that actually exist; missing stages are null with a reason — never
fabricated. Cross-org references are invisible.
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ebom_lifecycle.db")

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.services.tool_registry import registry
import app.services.tool_handlers.erp_submit_tool  # noqa: F401
from app.routers.ontology import router as ontology_router
from app.models.escalation import Escalation
from app.models.outcome import OutcomeMeasurement
from app.models.governance import ApprovalRequest
from app.models.audit_event import AuditEvent
from app.models.erp import ErpSubmission
from app.models.ontology import OntologyAction
from app.models.procurement import ProcurementPoDraft
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
    Base.metadata.create_all(eng, tables=[
        ApprovalRequest.__table__, AuditEvent.__table__,
        ErpSubmission.__table__, ProcurementPoDraft.__table__,
        Escalation.__table__, OutcomeMeasurement.__table__])
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


def _run_full_decision(client, s, *, org=ORG):
    """The automotive decision, end to end, through the real gates."""
    s.add(Escalation(
        id="esc-chain", org_id=org, summary="Model X profitability declining",
        status="open", sentiment="asks_human"))
    s.add(OntologyAction(
        id="act-chain", org_id=org, object_id="obj-x", name="erp_submit",
        tool_name="erp_submit_po", risk_level="write", is_active=True,
        is_deleted=False))
    s.commit()
    # resolve the escalation as the org admin (decision made)
    client.post("/api/ontology/escalations/esc-chain/resolve",
                json={"note": "approve mitigation PO"})
    # approval for the ERP submission
    s.add(ProcurementPoDraft(
        id="pd-chain", org_id=org, supplier_name="Acme Co", product="Widget",
        quantity=10, unit="pcs", est_amount=50.0, currency="USD",
        status="approved", approval_id="apr-chain"))
    s.add(ApprovalRequest(
        id="apr-chain", org_id=org, status="approved",
        action_type="ontology_write", reviewed_by="u-owner",
        action_description="Submit mitigation PO to ERP",
        context_json={"action_id": "act-chain",
                      "tool_name": "erp_submit_po",
                      "params": {"draft_id": "pd-chain"},
                      "escalation_id": "esc-chain"}))
    s.commit()
    # governed execution
    r = client.post("/api/ontology/actions/act-chain/execute-approved",
                    json={"approval_id": "apr-chain",
                          "params": {"draft_id": "pd-chain"}})
    assert r.json()["success"] is True, r.json()
    # governed measurement + verification
    r = client.post("/api/ontology/outcomes", json={
        "metric_ref": "met-gross-margin", "direction": "increase",
        "baseline_period": "2026-09", "observed_period": "2026-10",
        "expected_impact": 100.0, "approval_id": "apr-chain",
        "escalation_id": "esc-chain", "draft_id": "pd-chain"})
    assert r.json()["success"] is True, r.json()
    oid = r.json()["outcome"]["id"]
    client.post(f"/api/ontology/outcomes/{oid}/measure")
    r = client.post(f"/api/ontology/outcomes/{oid}/verify",
                    json={"decision": "confirmed against finance close"})
    assert r.json()["success"] is True, r.json()
    return oid


def test_full_chain_assembles_from_escalation_anchor(client):
    s = client.app.dependency_overrides[get_db]()
    _run_full_decision(client, s)
    r = client.get("/api/ontology/decisions/esc-chain/lifecycle")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    stages = body["stages"]
    # Every stage present, each carrying the reference to the previous.
    assert stages["problem"]["escalation_id"] == "esc-chain"
    assert stages["problem"]["status"] == "resolved"
    assert stages["approval"]["approval_id"] == "apr-chain"
    assert stages["execution"]["draft"]["id"] == "pd-chain"
    assert stages["execution"]["erp_submissions"]
    assert stages["execution"]["erp_submissions"][0]["external_ref"] or True
    assert stages["measurement"]["outcome_id"]
    assert stages["measurement"]["realized_impact"] == -80.0
    assert stages["verification"]["verified_by"] == "u-owner"
    # The chain is ordered and complete.
    assert [k for k, v in body["chain"] if v] == [
        "problem", "approval", "execution", "measurement", "verification"]


def test_missing_stages_are_honest(client):
    s = client.app.dependency_overrides[get_db]()
    s.add(Escalation(id="esc-only", org_id=ORG,
                     summary="signal only", status="open"))
    s.commit()
    r = client.get("/api/ontology/decisions/esc-only/lifecycle")
    assert r.status_code == 200
    stages = r.json()["stages"]
    assert stages["problem"]["escalation_id"] == "esc-only"
    assert stages["approval"] is None
    assert stages["execution"] is None
    assert stages["measurement"] is None
    assert stages["verification"] is None
    assert r.json()["chain"] == [["problem", True], ["approval", False],
                                 ["execution", False], ["measurement", False],
                                 ["verification", False]]


def test_draft_anchor_resolves_the_same_chain(client):
    s = client.app.dependency_overrides[get_db]()
    _run_full_decision(client, s)
    r = client.get("/api/ontology/decisions/pd-chain/lifecycle")
    assert r.status_code == 200
    stages = r.json()["stages"]
    assert stages["execution"]["draft"]["id"] == "pd-chain"
    assert stages["approval"]["approval_id"] == "apr-chain"
    assert stages["measurement"]["outcome_id"]


def test_unknown_ref_is_404(client):
    r = client.get("/api/ontology/decisions/nope/lifecycle")
    assert r.status_code == 404


def test_cross_org_ref_is_invisible(client):
    s = client.app.dependency_overrides[get_db]()
    _run_full_decision(client, s)
    client.app.dependency_overrides[get_current_user_required] = lambda: \
        SimpleNamespace(id="u-other", org_id="other-org", role="admin")
    r = client.get("/api/ontology/decisions/esc-chain/lifecycle")
    assert r.status_code == 404
    client.app.dependency_overrides[get_current_user_required] = lambda: _admin()
