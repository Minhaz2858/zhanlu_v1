"""Phase 1B.1 — BusinessRule governance & review lifecycle.

Covers: org isolation (404 cross-org), viewer 403, proposed→verified,
proposed→rejected, proposed→enable blocked (409), verified→enabled,
rejected cannot be enabled, repeated reviews preserve append-only
history, AuditEvent per decision, disable/resubmit paths, and list
filters/pagination.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.audit_event import AuditEvent
from app.models.business_rule import BusinessRule
from app.models.user import User
from app.routers.ontology import router as ontology_router

ORG = "org-acme"


def _user(uid, role="user", org=ORG):
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
        BusinessRule.__table__, AuditEvent.__table__,
    ])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    holder = {"user": _user("u-admin", role="admin")}

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_required] = \
        lambda: holder["user"]
    c = TestClient(app, raise_server_exceptions=False)
    c.holder = holder  # type: ignore[attr-defined]
    return c


def _seed(db, **kw):
    rule = dict(id="br-1", org_id=ORG, scope="domain", scope_ref_id="dom-1",
                statement="Discounts over 10% need manager approval",
                status="proposed", applies_to="sql")
    rule.update(kw)
    db.add(BusinessRule(**rule))
    db.commit()
    return rule["id"]


def _audits(db, rule_id):
    return db.query(AuditEvent).filter(
        AuditEvent.resource_type == "business_rule",
        AuditEvent.resource_id == rule_id).all()


# 1. Org isolation ---------------------------------------------------------

def test_cross_org_read_is_404(client, db):
    rid = _seed(db)
    client.holder["user"] = _user("u-foreign-admin", role="admin",
                                  org="org-evil")
    assert client.get(f"/api/ontology/enterprise/business-rules/{rid}")\
        .status_code == 404
    assert client.post(
        f"/api/ontology/enterprise/business-rules/{rid}/confirm")\
        .status_code == 404
    # org listing never leaks other-org rules
    assert client.get(
        "/api/ontology/enterprise/business-rules").json()["total"] == 0


def test_cross_org_review_is_404_and_writes_nothing(client, db):
    rid = _seed(db, org_id="org-other")
    r = client.post(f"/api/ontology/enterprise/business-rules/{rid}/confirm")
    assert r.status_code == 404
    assert _audits(db, rid) == []
    assert db.get(BusinessRule, rid).status == "proposed"


# 2. Viewer receives 403 ---------------------------------------------------

def test_viewer_gets_403_on_all_write_operations(client, db):
    rid = _seed(db)
    client.holder["user"] = _user("u-viewer")  # role=user, same org
    for action in ("confirm", "reject", "enable", "disable", "resubmit"):
        r = client.post(
            f"/api/ontology/enterprise/business-rules/{rid}/{action}")
        assert r.status_code == 403, action
    # reads stay available to authenticated org members
    assert client.get(
        f"/api/ontology/enterprise/business-rules/{rid}").status_code == 200


# 3-6. Transitions ---------------------------------------------------------

def test_proposed_to_verified_via_confirm(client, db):
    rid = _seed(db)
    r = client.post(f"/api/ontology/enterprise/business-rules/{rid}/confirm")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "verified"
    assert [h["decision"] for h in body["review_history"]] == ["confirmed"]
    assert body["source_evidence_ids"] == []
    assert db.get(BusinessRule, rid).status == "verified"


def test_proposed_to_rejected_with_reason(client, db):
    rid = _seed(db)
    r = client.post(f"/api/ontology/enterprise/business-rules/{rid}/reject",
                    json={"reason": "contradicts pricing policy"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["review_history"][0]["reason"] == \
        "contradicts pricing policy"


def test_proposed_to_enabled_blocked(client, db):
    rid = _seed(db)
    r = client.post(f"/api/ontology/enterprise/business-rules/{rid}/enable")
    assert r.status_code == 409
    assert db.get(BusinessRule, rid).status == "proposed"
    assert (db.get(BusinessRule, rid).review_history or []) == []


def test_verified_to_enabled(client, db):
    rid = _seed(db, status="verified")
    r = client.post(f"/api/ontology/enterprise/business-rules/{rid}/enable")
    assert r.status_code == 200
    assert r.json()["status"] == "enabled"


def test_disable_and_explicit_resubmit_paths(client, db):
    rid = _seed(db, status="enabled")
    assert client.post(
        f"/api/ontology/enterprise/business-rules/{rid}/disable")\
        .json()["status"] == "verified"
    rid2 = _seed(db, id="br-2", status="rejected",
                 statement="second rule")
    r = client.post(
        f"/api/ontology/enterprise/business-rules/{rid2}/resubmit",
        json={"reason": "policy updated"})
    assert r.status_code == 200
    assert r.json()["status"] == "proposed"


def test_rejected_rule_cannot_be_enabled(client, db):
    rid = _seed(db, status="rejected")
    assert client.post(
        f"/api/ontology/enterprise/business-rules/{rid}/enable")\
        .status_code == 409
    # the only path back is explicit resubmission → then enable is 409 again
    assert client.post(
        f"/api/ontology/enterprise/business-rules/{rid}/resubmit")\
        .json()["status"] == "proposed"
    assert client.post(
        f"/api/ontology/enterprise/business-rules/{rid}/enable")\
        .status_code == 409


# 7. Repeated review preserves history -------------------------------------

def test_repeated_reviews_preserve_append_only_history(client, db):
    rid = _seed(db)  # proposed
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/confirm")
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/enable",
                json={"reason": "GA for Q4"})
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/disable",
                json={"reason": "paused for audit"})
    # invalid attempts never append: resubmit only valid from rejected
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/resubmit")
    body = client.get(
        f"/api/ontology/enterprise/business-rules/{rid}").json()
    decisions = [h["decision"] for h in body["review_history"]]
    assert decisions == ["confirmed", "enabled", "disabled"]
    assert body["status"] == "verified"
    assert body["review_history"][1]["reason"] == "GA for Q4"
    assert body["review_history"][2]["reason"] == "paused for audit"


# 8. AuditEvent written -----------------------------------------------------

def test_every_decision_writes_audit_event(client, db):
    rid = _seed(db)
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/confirm")
    client.post(f"/api/ontology/enterprise/business-rules/{rid}/enable",
                json={"reason": "GA for Q4"})
    audits = _audits(db, rid)
    assert [a.event_type for a in audits] == \
        ["business_rule_confirmed", "business_rule_enabled"]
    assert audits[0].org_id == ORG
    assert audits[0].event_source == "ontology"
    assert audits[0].actor_id == "u-admin"
    assert audits[1].detail_json["from_status"] == "verified"
    assert audits[1].detail_json["to_status"] == "enabled"
    assert audits[1].detail_json["reason"] == "GA for Q4"


# Listing: filters + pagination + evidence/history visibility ---------------

def test_list_filters_and_pagination(client, db):
    _seed(db, id="br-1", status="proposed")
    _seed(db, id="br-2", status="verified", applies_to="action",
          scope="org", scope_ref_id=None, statement="rule two")
    _seed(db, id="br-3", status="proposed", scope="org",
          scope_ref_id=None, statement="rule three")
    r = client.get("/api/ontology/enterprise/business-rules",
                   params={"status": "proposed", "scope": "org"}).json()
    assert r["total"] == 1 and r["items"][0]["id"] == "br-3"
    r = client.get("/api/ontology/enterprise/business-rules",
                   params={"applies_to": "action"}).json()
    assert [i["id"] for i in r["items"]] == ["br-2"]
    r = client.get("/api/ontology/enterprise/business-rules",
                   params={"page": 2, "page_size": 2}).json()
    assert r["total"] == 3 and len(r["items"]) == 1 and r["page"] == 2
    item = r["items"][0]
    assert "source_evidence_ids" in item and "review_history" in item


def test_missing_rule_404(client, db):
    assert client.get(
        "/api/ontology/enterprise/business-rules/nope").status_code == 404
    assert client.post(
        "/api/ontology/enterprise/business-rules/nope/confirm")\
        .status_code == 404
