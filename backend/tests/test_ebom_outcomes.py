"""EBOM — Business outcome measurement (Workstream B).

Proves the expected → measured → verified impact chain is governed and
honest:
  * baseline/observed values come ONLY from the governed MetricDefinition
    (deterministic compute_metric — never an LLM, never a hardcoded number);
  * realized impact follows the declared direction (increase/decrease);
  * verification requires measurement + authorized reviewer;
  * verified measurements are frozen;
  * cross-org isolation and duplicate-measurement idempotency hold.
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ebom_outcomes.db")

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.routers.ontology import router as ontology_router
from app.models.governance import ApprovalRequest
from app.models.audit_event import AuditEvent
from app.models.outcome import OutcomeMeasurement
from tests.sandbox_seed import (
    ORG, build_sandbox, create_data_db, make_engine, make_session,
)


def _admin(org=ORG):
    return SimpleNamespace(id="u-owner", org_id=org, role="admin")


def _viewer(org=ORG):
    return SimpleNamespace(id="u-viewer", org_id=org, role="user")


@pytest.fixture()
def client():
    eng = make_engine()
    Base.metadata.create_all(eng, tables=[
        OutcomeMeasurement.__table__, AuditEvent.__table__])
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


def _record(client, **over):
    body = {
        "metric_ref": "met-gross-margin",
        "direction": "increase",
        "baseline_period": "2026-09",
        "observed_period": "2026-10",
        "expected_impact": 100.0,
        "approval_id": "apr-outcome",
    }
    body.update(over)
    return client.post("/api/ontology/outcomes", json=body)


# --- recording: baseline is governed computation, not a claim ---------------

def test_record_outcome_computes_governed_baseline(client):
    r = _record(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    # Baseline equals the GOVERNED 2026-09 gross margin (1960.0 in the seed)
    # computed through MetricDefinition — not echoed from the request.
    assert body["outcome"]["baseline_value"] == 1960.0
    assert body["outcome"]["measurement_status"] == "pending"
    assert body["outcome"]["verification_status"] == "unverified"
    assert body["outcome"]["metric_definition_id"] == "met-gross-margin"


def test_record_refuses_unknown_metric_honestly(client):
    r = _record(client, metric_ref="met-does-not-exist")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["unavailable"] is True
    assert "reason" in body


def test_record_refuses_identical_periods(client):
    r = _record(client, observed_period="2026-09")
    assert r.status_code == 200
    assert r.json()["success"] is False  # a zero-length comparison is meaningless


def test_duplicate_measurement_is_idempotent(client):
    r1 = _record(client)
    r2 = _record(client)
    assert r1.json()["outcome"]["id"] == r2.json()["outcome"]["id"]
    lst = client.get("/api/ontology/outcomes").json()["outcomes"]
    assert len(lst) == 1


# --- measuring: deterministic realized impact --------------------------------

def test_measure_computes_realized_impact_increase(client):
    oid = _record(client).json()["outcome"]["id"]
    r = client.post(f"/api/ontology/outcomes/{oid}/measure")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    out = body["outcome"]
    # direction=increase → realized = observed − baseline = 1880 − 1960 = −80
    assert out["observed_value"] == 1880.0
    assert out["realized_impact"] == -80.0
    assert out["measurement_status"] == "measured"
    # Evidence carries the governed provenance (SQL trace), not a claim.
    assert out["evidence_refs"]["metric_provenance"]["base_table"]


def test_measure_respects_decrease_direction(client):
    # Same governed numbers, opposite business expectation:
    # realized = baseline − observed = 1960 − 1880 = 80
    oid = _record(client, direction="decrease").json()["outcome"]["id"]
    r = client.post(f"/api/ontology/outcomes/{oid}/measure")
    assert r.json()["outcome"]["realized_impact"] == 80.0


def test_measure_is_idempotent_and_honest_when_already_measured(client):
    oid = _record(client).json()["outcome"]["id"]
    first = client.post(f"/api/ontology/outcomes/{oid}/measure").json()
    second = client.post(f"/api/ontology/outcomes/{oid}/measure").json()
    assert first["outcome"]["realized_impact"] == second["outcome"]["realized_impact"]


# --- verification: authorized review, frozen results -------------------------

def test_verify_requires_admin(client):
    oid = _record(client).json()["outcome"]["id"]
    client.post(f"/api/ontology/outcomes/{oid}/measure")
    client.app.dependency_overrides[get_current_user_required] = lambda: _viewer()
    r = client.post(f"/api/ontology/outcomes/{oid}/verify",
                    json={"decision": "figures confirmed"})
    assert r.status_code == 403
    client.app.dependency_overrides[get_current_user_required] = lambda: _admin()


def test_verify_requires_measured_state(client):
    oid = _record(client).json()["outcome"]["id"]
    r = client.post(f"/api/ontology/outcomes/{oid}/verify",
                    json={"decision": "too early"})
    assert r.status_code == 400  # cannot verify an unmeasured outcome


def test_verify_freezes_and_records_reviewer(client):
    oid = _record(client).json()["outcome"]["id"]
    client.post(f"/api/ontology/outcomes/{oid}/measure")
    r = client.post(f"/api/ontology/outcomes/{oid}/verify",
                    json={"decision": "confirmed against finance close"})
    assert r.status_code == 200
    out = r.json()["outcome"]
    assert out["verification_status"] == "verified"
    assert out["verified_by"] == "u-owner"
    # Frozen: measurement can no longer drift after human certification.
    r2 = client.post(f"/api/ontology/outcomes/{oid}/measure")
    assert r2.json()["success"] is False
    assert "verified" in r2.json()["reason"].lower()


# --- isolation ---------------------------------------------------------------

def test_outcomes_are_org_scoped(client):
    _record(client)
    client.app.dependency_overrides[get_current_user_required] = \
        lambda: _admin("other-org")
    r = client.get("/api/ontology/outcomes")
    assert r.status_code == 200
    assert r.json()["outcomes"] == []
    client.app.dependency_overrides[get_current_user_required] = lambda: _admin()
